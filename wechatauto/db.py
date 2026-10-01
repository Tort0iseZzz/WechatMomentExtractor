"""wechatauto 数据库读取模块（微信 4.x）

通过读取微信本地 SQLCipher 加密数据库读取朋友圈及联系人，不依赖 UI 自动化。

原理：
    1. 从微信配置文件（%APPDATA%/Tencent/xwechat/config/*.ini）定位数据目录；
    2. 从 Weixin.exe 进程内存中只读扫描 ``com.Tencent.WCDB.Config.Cipher``
       配置对象，提取每个数据库独立的 32 字节密钥（SQLCipher 4 格式，
       PBKDF2-HMAC-SHA512, 256000 迭代）；
    3. 按页解密数据库到临时目录（带缓存），再用标准 sqlite3 查询。

限制：
    - 微信必须处于登录状态（密钥存在于进程内存中，首次提取后本地缓存）；
    - 合并 -wal 时若微信正在 checkpoint，可能触发一次全量重建重试；
    - 仅支持读取，不支持发送。
"""

from __future__ import annotations

import ctypes

import glob

import hashlib

import hmac as hmac_mod

import json

import os

import re

import shutil

import sqlite3

import struct

import sys

import tempfile

import threading

import time

from ctypes import wintypes

from typing import Dict, List, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechatauto.logger import wxlog
from pathlib import Path

PAGE_SZ = 4096

RESERVE_SZ = 80  # IV(16) + HMAC(64)

STAMP_VERSION = 4  # v3: stamp 内 mtime 改用 %r 完整精度（%f 只留 6 位小数，与 Windows 7 位小数比较恒不等→每秒重建缓存→磁盘 50MB/s 读+写）

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"

CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)

HEX_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

MASTER_DLL_PATTERN = bytes.fromhex(
    "83ec404889d64889cb0f57c00f1142100f11024c8bb1c8020000"
    "4883b9d0020000107209488b9bb8020000eb074881c3b8020000"
    "4d85f60f880a0200004983fe10736d4c89761048c746180f0000"
    "000f10030f110648b8"
)

MASTER_DLL_VERIFY = (b"488944242048b8", b"488944242848b8", b"488944243048b8")

CFG_LANDMARK = b"global_config"   # cfg 对象地标字符串(SSO 内联)

CFG_PTR_BACK = 0x138              # 地标前指针链回退偏移(版本敏感: 4.1.10.31=0x130)

CFG_OFFSET = 0x68                 # v18 → cfg 指针偏移(版本敏感)

CFG_DWORD_OFF = 0x40              # cfgDword(图片密钥派生源)

CFG_WXID_OFF = 0x48               # wxId std::string

CFG_CIPHER_OFF = 0x2B8            # dbKey 密文 std::string

class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]

class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

_k32.OpenProcess.restype = wintypes.HANDLE

_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL

_k32.VirtualQueryEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t,
]

_k32.VirtualQueryEx.restype = ctypes.c_size_t

_k32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]

def _pbkdf2(passwd: bytes, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passwd, salt, iters, dklen=32)

def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()

def _split_key(key: bytes) -> Tuple[bytes, Optional[bytes]]:
    """拆分密钥形态：32 字节 = 标准裸 key（salt 用文件头前 16 字节）；
    48 字节 = SQLCipher 4 "Raw Key with Explicit Salt"（前 32B key + 后 16B salt，
    用于 cipher_plaintext_header_size 明文头模式）。返回 (enc_key, salt_or_None)。"""
    if len(key) == 48:
        return key[:32], key[32:]
    return key, None

def _verify_enc_key(enc_key: bytes, page1: bytes, salt: Optional[bytes] = None) -> bool:
    """验证 enc_key 是否为 page1 的 SQLCipher 4 密钥。

    - enc_key 为 32 字节裸 key：默认用文件头前 16 字节作 salt（标准形式）；
    - enc_key 为 48 字节 key+salt：显式 salt 优先于文件头（明文头模式）；
    - 也允许单独传 salt 参数覆盖（供提取逻辑按 96hex 拆分尝试）。
    """
    if len(page1) < PAGE_SZ:
        return False
    if len(enc_key) == 48 and salt is None:
        enc_key, salt = enc_key[:32], enc_key[32:]
    if salt is None:
        salt = page1[:16]
    elif len(salt) != 16:
        return False
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = _pbkdf2(enc_key, mac_salt, 2)
    hmac_data = page1[16: PAGE_SZ - RESERVE_SZ + 16]
    stored_hmac = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac

def _sqlite_text_factory(data: bytes):
    """sqlite TEXT 列解码：合法 UTF-8 返回 str，否则原样返回 bytes（图片等二进制内容）"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data

def _find_weixin_module(pid: int) -> Optional[Tuple[int, int, str]]:
    """返回 (模块基址, 模块大小, 路径); weixin.dll 为微信 4.x 主模块"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.Module32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    k32.Module32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MODULEENTRY32W)]
    snap = k32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)
    if not snap or snap == -1 or snap == (1 << 64) - 1:
        return None
    try:
        me = _MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(me)
        ok = k32.Module32FirstW(snap, ctypes.byref(me))
        while ok:
            if me.szModule and me.szModule.lower() == "weixin.dll":
                return (me.modBaseAddr or 0), me.modBaseSize, me.szExePath
            me.dwSize = ctypes.sizeof(me)
            ok = k32.Module32NextW(snap, ctypes.byref(me))
    finally:
        k32.CloseHandle(snap)
    return None

def _extract_movabs_xor_key(dll_path: str) -> Optional[bytes]:
    """从 weixin.dll 特征码后提取 4×movabs 立即数拼接成 32 字节 XOR 材料。

    特征码(sub_1803308D0 前缀)后紧跟 4 个 '48 b8 <imm64>' movabs 指令,
    立即数即主密钥的 XOR 材料。小版本通常不改此代码段, 大版本需重采。
    """
    try:
        with open(dll_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    hit = data.find(MASTER_DLL_PATTERN)
    if hit < 0:
        return None
    take = min(200, len(data) - hit)
    hex_txt = data[hit:hit + take].hex()
    hex_txt = hex_txt[len(MASTER_DLL_PATTERN) * 2:]  # 丢弃特征码自身
    key = ""
    for vf in MASTER_DLL_VERIFY:
        if len(hex_txt) < 30 or hex_txt[16:30] != vf.decode():
            return None
        key += hex_txt[0:16]
        hex_txt = hex_txt[30:]
    if len(hex_txt) < 16:
        return None
    key += hex_txt[0:16]
    try:
        return bytes.fromhex(key)
    except ValueError:
        return None

def _read_remote_string(h, read, addr: int) -> str:
    """跨进程读 MSVC x64 std::string(SSO: size<=15 内联, 否则堆指针)"""
    sz_buf = read(addr + 16, 8)
    if not sz_buf:
        return ""
    size = struct.unpack_from("<Q", sz_buf)[0]
    if size <= 0 or size > 0x7FFFFFFF:
        return ""
    if size <= 15:
        data = read(addr, size)
    else:
        p_buf = read(addr, 8)
        if not p_buf:
            return ""
        data = read(struct.unpack_from("<Q", p_buf)[0], size)
    if not data:
        return ""
    return data[:size].decode("utf-8", "replace")

def _read_remote_bytes(h, read, addr: int) -> Optional[bytes]:
    """跨进程读字节缓冲(std::string 布局: data@+0, size@+16, cap@+24)"""
    sz_buf = read(addr + 16, 8)
    if not sz_buf:
        return None
    size = struct.unpack_from("<Q", sz_buf)[0]
    if size <= 0 or size > 0x400:
        return None
    cap_buf = read(addr + 24, 4)
    cap = struct.unpack_from("<I", cap_buf)[0] if cap_buf else 0
    if (cap | 0xF) == 0xF:
        data = read(addr, size)          # SSO 内联
    else:
        p_buf = read(addr, 8)
        if not p_buf:
            return None
        data = read(struct.unpack_from("<Q", p_buf)[0], size)
    if not data or len(data) != size:
        return None
    return data[:size]

def extract_master_key_from_cfg(pid: int) -> Optional[Tuple[str, int, str]]:
    """从 Weixin.exe 进程提取 (主密钥hex, cfgDword, wxId)。

    流程(ReadWeixinKey-rev 同源): 整块读 weixin.dll 映像 → 扫 global_config
    SSO 地标 → 指针链 cfg → 读 cfg+0x2B8 密文与 cfg+0x40 cfgDword →
    密文 XOR DLL movabs 材料得主密钥。锚点(CFG_PTR_BACK/CFG_OFFSET/特征码)
    每版本重采; 提取失败返回 None。
    """
    base, mod_size, dll_path = _find_weixin_module(pid) or (0, 0, "")
    if not base or not dll_path or mod_size <= 0 or mod_size >= 0x40000000:
        return None
    h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return None
    try:
        def read(addr: int, n: int):
            buf = ctypes.create_string_buffer(n)
            br = ctypes.c_size_t(0)
            if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                return buf.raw[: br.value]
            return None

        image = read(base, mod_size)
        if not image or len(image) != mod_size:
            return None
        # 扫 global_config SSO 地标(size==13@+16, cap==15@+24, 内容内联@+0)
        pos = -1
        for i in range(len(image) - 8, 0, -8):
            if struct.unpack_from("<I", image, i)[0] == len(CFG_LANDMARK):
                cap = struct.unpack_from("<I", image, i + 8)[0]
                if cap and (cap | 0xF) == 0xF and i - 16 >= 0:
                    if image[i - 16:i - 16 + len(CFG_LANDMARK)] == CFG_LANDMARK:
                        pos = i
                        break
        if pos < 0:
            return None
        # 指针链: v18 = *(base + pos - CFG_PTR_BACK); cfg = *(v18 + CFG_OFFSET)
        v18_buf = read(base + pos - CFG_PTR_BACK, 8)
        if not v18_buf:
            return None
        v18 = struct.unpack_from("<Q", v18_buf)[0]
        cfg_buf = read(v18 + CFG_OFFSET, 8)
        if not cfg_buf:
            return None
        cfg = struct.unpack_from("<Q", cfg_buf)[0]
        if not (0x10000 <= cfg < 0x800000000000):
            return None
        # cfgDword + wxId
        dw_buf = read(cfg + CFG_DWORD_OFF, 4)
        cfg_dword = struct.unpack_from("<I", dw_buf)[0] if dw_buf else 0
        wxid = _read_remote_string(h, read, cfg + CFG_WXID_OFF)
        # dbKey 密文
        cipher = _read_remote_bytes(h, read, cfg + CFG_CIPHER_OFF)
        if not cipher:
            return None
        material = _extract_movabs_xor_key(dll_path)
        if not material or len(material) != len(cipher):
            return None
        master = bytes(a ^ b for a, b in zip(cipher, material))
        return master.hex(), cfg_dword, wxid
    finally:
        _k32.CloseHandle(h)

def _decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        # 48 字节 key（key+salt）= cipher_plaintext_header_size 明文头模式：
        # 页 1 前 16 字节是明文头（非加密 salt），加密数据从 offset 16 开始；
        # 解密后拼接明文头 + 明文数据 + reserve。
        if len(enc_key) == 48:
            enc = page[16: PAGE_SZ - RESERVE_SZ]
            return page[:16] + _aes_cbc_decrypt(enc_key[:32], iv, enc) + b"\x00" * RESERVE_SZ
        enc = page[16: PAGE_SZ - RESERVE_SZ]
        return b"SQLite format 3\x00" + _aes_cbc_decrypt(enc_key, iv, enc) + b"\x00" * RESERVE_SZ
    enc = page[: PAGE_SZ - RESERVE_SZ]
    return _aes_cbc_decrypt(enc_key[:32] if len(enc_key) == 48 else enc_key, iv, enc) + b"\x00" * RESERVE_SZ

def _find_account_dirs(db_dir: str) -> List[str]:
    """列出 db_dir 下所有含 db_storage 子目录的账号目录。

    微信号目录不一定以 wxid_ 开头（如自定义微信号），这里只依赖
    db_storage 子目录的存在性判断。
    """
    out = []
    try:
        for name in os.listdir(db_dir):
            p = os.path.join(db_dir, name, "db_storage")
            if os.path.isdir(p):
                out.append(os.path.join(db_dir, name))
    except OSError:
        pass
    return sorted(out)

class WeChatDB:
    """微信 4.x 朋友圈本地数据库只读解密器。"""

    """微信 4.x 本地数据库读取器"""

    def __init__(
        self,
        db_dir: Optional[str] = None,
        keys_file: Optional[str] = None,
        workdir: Optional[str] = None,
        account: Optional[str] = None,
        master_key: Optional[str] = None,
    ):
        self.snapshot_warnings = []
        self.db_dir = db_dir or auto_detect_db_dir()
        if not self.db_dir:
            raise RuntimeError("未找到微信数据库目录，请通过 db_dir 参数手动指定")
        self._explicit_account = bool(account)
        self.account = account or self._pick_account()
        self.account_dir = os.path.join(self.db_dir, self.account)
        self.workdir = workdir or os.path.join(
            tempfile.gettempdir(), "wechatauto_db", self.account
        )
        self.keys_file = keys_file or os.path.join(self.workdir, "keys.json")
        self._keys: Dict[str, bytes] = {}
        self._db_files = self._collect_db_files()
        self.master_key: Optional[str] = None
        self.cfg_dword: Optional[int] = None
        self._load_or_extract_keys(master_key=master_key)

    def _pick_account(self) -> str:
        candidates = []
        for d in _find_account_dirs(self.db_dir):
            if os.path.isdir(os.path.join(d, "db_storage")):
                recent = max(
                    (
                        os.path.getmtime(os.path.join(root, f))
                        for root, _, files in os.walk(os.path.join(d, "db_storage"))
                        for f in files
                        if f.endswith(".db") and not f.endswith("-wal")
                    ),
                    default=0,
                )
                candidates.append((recent, os.path.basename(d)))
        if not candidates:
            raise RuntimeError("未找到任何已登录账号的数据库")
        candidates.sort(reverse=True)
        return candidates[0][1]

    def _collect_db_files(self) -> List[Tuple[str, str, int]]:
        files = []
        base = os.path.join(self.account_dir, "db_storage")
        for root, _, names in os.walk(base):
            # migrate 目录下的 unspportmsg.db 是微信保留的未支持消息库，
            # 进程内存中不存在对应密钥、代码从不访问，排除以免误触发密钥提取
            if os.path.normcase(os.path.relpath(root, base)).startswith("migrate"):
                continue
            for name in names:
                if name not in {"sns.db", "contact.db"}:
                    continue
                path = os.path.join(root, name)
                files.append((os.path.relpath(path, base), path, os.path.getsize(path)))
        return files

    @property
    def wxid(self) -> str:
        """当前账号的微信号（去掉目录名末尾的 4 位哈希后缀）"""
        return re.sub(r"_\w{4}$", "", self.account)

    def get_self_info(self) -> dict:
        """当前登录账号的昵称等信息"""
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "contact.db":
                continue
            conn = self._open(rel)
            row = conn.execute(
                "SELECT username, nick_name, remark FROM contact WHERE username=? LIMIT 1",
                (self.wxid,),
            ).fetchone()
            if row:
                return {"username": row[0], "nick_name": row[1], "remark": row[2]}
        return {"username": self.wxid, "nick_name": "", "remark": ""}

    KDF_ITER = 256000  # 主密钥→库密钥 PBKDF2 迭代(微信魔改 WCDB, 实测确认)

    def _try_other_accounts(self) -> bool:
        """账号自愈：当前账号目录解不开时，改用同一主密钥能解开的其它账号目录。

        多账号机器上 `_pick_account()` 的“最近修改 .db”启发式可能选错账号：
        内存里提取到的主密钥属于**当前登录账号**，拿去解另一个账号的库会全部
        页1 HMAC 校验失败（表现为“已加载 0/N 个密钥”、`-N/N`）。
        这里用同一主密钥对其余账号目录重新派生/校验，选可用库最多的那个并
        切换过去。返回是否发生了切换。
        """
        try:
            dirs = [d for d in _find_account_dirs(self.db_dir)
                    if os.path.basename(d) != self.account]
        except Exception:
            return False
        if not dirs:
            return False

        master = self.master_key
        if not master:
            try:
                auto = self.extract_master_key()
            except Exception:
                auto = None
            if auto:
                master = auto[0]
                self.master_key = master
        if not master:
            return False

        def probe(acct: str):
            """在不动全局状态的前提下，试算某账号可用库数。"""
            acct_dir = os.path.join(self.db_dir, acct)
            workdir = os.path.join(tempfile.gettempdir(), "wechatauto_db", acct)
            keys_file = os.path.join(workdir, "keys.json")
            saved = (self.account, self.account_dir, self._db_files, self.workdir,
                     self.keys_file, self._keys)
            try:
                self.account, self.account_dir = acct, acct_dir
                self._db_files = self._collect_db_files()
                self.workdir, self.keys_file = workdir, keys_file
                self._keys = {}
                if os.path.exists(keys_file):     # 先用该账号已有缓存
                    try:
                        with open(keys_file, encoding="utf-8") as f:
                            for rel, hexkey in json.load(f).items():
                                try:
                                    self._keys[rel] = bytes.fromhex(hexkey)
                                except ValueError:
                                    pass
                    except Exception:
                        pass
                if not any(self._key_works(rel) for rel, _, _ in self._db_files):
                    self._keys.update(self.derive_keys_from_master(master))
                ok = sum(1 for rel, _, _ in self._db_files if self._key_works(rel))
                return ok, dict(self._keys), list(self._db_files), workdir, keys_file
            except Exception:
                return 0, {}, [], workdir, keys_file
            finally:
                (self.account, self.account_dir, self._db_files, self.workdir,
                 self.keys_file, self._keys) = saved

        cur_ok = sum(1 for rel, _, _ in self._db_files if self._key_works(rel))
        best = None
        for d in dirs:
            ok, keys, files, workdir, keys_file = probe(os.path.basename(d))
            if ok and (best is None or ok > best[0]):
                best = (ok, os.path.basename(d), keys, files, workdir, keys_file)
        if best is None or best[0] <= cur_ok:
            return False

        ok, acct, keys, files, workdir, keys_file = best
        self.account = acct
        self.account_dir = os.path.join(self.db_dir, acct)
        self._keys, self._db_files = keys, files
        self.workdir, self.keys_file = workdir, keys_file
        try:
            os.makedirs(self.workdir, exist_ok=True)
            self._save_keys()
        except Exception:
            pass
        print("[wechatauto] 已自动切换账号目录: %s（%d 个库可用密钥）" % (acct, ok),
              file=sys.stderr)
        return True

    def _load_or_extract_keys(self, master_key: Optional[str] = None) -> None:
        """加载/提取密钥, 五层优先级:

        1. 显式 master_key(构造参数) → 主密钥派生;
        2. 本地缓存 keys.json(已验证);
        3. Config.Cipher 内存扫描(4.1+ 主路径);
        4. cfg 自动提取(老版本回退);
        5. 密钥提取(最终回退)。

        派生/扫描结果均经 SQLCipher4 页1 HMAC 强校验, 零误报。
        """
        if master_key:
            self._keys.update(self.derive_keys_from_master(master_key))
            self.master_key = master_key
            self.cfg_dword = None
            self._save_keys()
        else:
            self.master_key = None
            self.cfg_dword = None
            
            # 优先级1: 尝试已保存的密钥——多位置合并（稳定副本 > 工作缓存 > .bak >
            # 其它账号缓存），全部经页1 HMAC 校验，只保留真能用的
            cache_paths = []
            sf = self._stable_key_file()
            if sf:
                cache_paths.append(sf)
            cache_paths += [self.keys_file, self.keys_file + ".bak"]
            try:
                for other in _find_account_dirs(self.db_dir):
                    oa = os.path.basename(other)
                    if oa != self.account:
                        cache_paths.append(os.path.join(
                            tempfile.gettempdir(), "wechatauto_db", oa, "keys.json"))
            except Exception:
                pass
            for cp in cache_paths:
                if not os.path.exists(cp):
                    continue
                for rel, key in self._load_key_cache(cp).items():
                    self._keys.setdefault(rel, key)
            # 只保留能通过页1 校验的（丢弃错账号/陈旧条目）
            if self._keys:
                valid: Dict[str, bytes] = {}
                for rel, k in self._keys.items():
                    try:
                        if self._key_works(rel):
                            valid[rel] = k
                    except Exception:
                        continue          # 缓存里可能残留已不存在的库条目，忽略
                self._keys = valid
                # 规范化存储形式：48 字节（key+salt）只适用于「明文头」库；
                # 若能以 32 字节裸密钥通过页1 校验，说明是标准库 → 截回 32 字节。
                # （否则解密会走错分支，产出 file is not a database 的文件）
                for rel in list(self._keys):
                    k = self._keys[rel]
                    if len(k) != 48:
                        continue
                    try:
                        with open(self._db_path(rel), "rb") as f:
                            _p1 = f.read(PAGE_SZ)
                    except Exception:
                        continue
                    if _verify_enc_key(k[:32], _p1):
                        self._keys[rel] = k[:32]
                self._save_keys()
            
            missing = [
                rel for rel, path, _ in self._db_files
                if rel not in self._keys or not self._key_works(rel)
            ]
            
            # 优先级2: Config.Cipher 内存扫描(4.1+ 主路径)
            if missing:
                extracted = self.extract_keys()
                self._keys.update(extracted)
                self._save_keys()
            
            # 尝试提取cfg_dword用于图片密钥派生
            if self.cfg_dword is None:
                auto = self.extract_master_key()
                if auto:
                    _, cfg_dword, _ = auto
                    self.cfg_dword = cfg_dword
            
            missing = [
                rel for rel, path, _ in self._db_files
                if rel not in self._keys or not self._key_works(rel)
            ]
            
            # 优先级3: cfg 自动提取(老版本回退)
            # 注意：cfg 路径在微信 4.1.13+ 会返回**不可信的主密钥**（v1.1.9 起已把
            # Config.Cipher 内存扫描提为主路径，此处仅作老版本回退）。若它一把库都
            # 复现不出来，要明确告警，而不是静默当成成功。
            if missing:
                auto = self.extract_master_key()
                if auto:
                    master, cfg_dword, _ = auto
                    derived = self.derive_keys_from_master(master)
                    self.cfg_dword = cfg_dword
                    if derived:
                        self.master_key = master
                        self._keys.update(derived)
                        self._save_keys()
                    else:
                        import sys as _sys
                        print(
                            "[wechatauto] 提示: cfg 主密钥无法复现任何库密钥"
                            "（微信 4.1.13+ 已知问题，cfg 路径已不可信）。当前依赖 "
                            "Config.Cipher 内存扫描；若扫描也失败，请运行 "
                            "在工具中确认账号、数据目录与微信登录状态后重试",
                            file=_sys.stderr,
                        )
        def _still_now():
            return [rel for rel, _, _ in self._db_files if not self._key_works(rel)]

        still = _still_now()
        # 账号自愈（第一步，便宜）：先试其它账号目录已有的缓存/主密钥派生
        if still and not self._explicit_account and self._try_other_accounts():
            still = _still_now()
        if still:
            # 账号自愈（第二步，彻底）：一次内存扫描收集候选密钥材料，对**每个账号
            # 目录**分别做 HMAC 打分，选能解开的那一个——不再依赖 mtime 启发式，
            # 从根上解决“微信每次更新后重写 .db → 选错账号 → 0 密钥”。
            try:
                cands = self._all_key_candidates()
            except Exception:
                cands = set()
            if cands and not self._explicit_account and self._select_account_by_keys(cands):
                still = _still_now()
        if still:
            import sys as _sys
            total = len(self._db_files)
            print(
                "[wechatauto] 警告: 以下库无可用密钥，无法解密: %s"
                % ", ".join(still),
                file=_sys.stderr,
            )
            print(
                "[wechatauto] 已加载 %d/%d 个密钥 (缓存: %s)。请确认微信已登录，"
                "可运行 在工具中确认账号、数据目录与微信登录状态后重试 排查"
                % (total - len(still), total, self.keys_file),
                file=_sys.stderr,
            )
            try:
                accounts = sorted(os.path.basename(d)
                                  for d in _find_account_dirs(self.db_dir))
            except Exception:
                accounts = []
            if len(accounts) > 1:
                print(
                    "[wechatauto]  >> 检测到多个微信账号目录: %s；当前使用: %s。"
                    "若仍报错，请用 WeChatDB(account=\"当前登录账号\") 显式指定"
                    % (", ".join(accounts), self.account),
                    file=_sys.stderr,
                )
        self.unkeyed = still

    def derive_keys_from_master(self, master_hex: str) -> Dict[str, bytes]:
        """主密钥派生逐库密钥: PBKDF2-HMAC-SHA512(主密钥, 库头salt, KDF_ITER)。

        微信 4.x 为单一主密钥 + 每库随机 salt 派生独立库密钥(SQLCipher4
        passphrase 语义)。仅返回通过页1 HMAC 校验的派生密钥。
        """
        try:
            master = bytes.fromhex(master_hex)
        except ValueError:
            raise ValueError("主密钥必须为 64 位 hex 字符串")
        if len(master) != 32:
            raise ValueError("主密钥必须为 32 字节(64 位 hex)")
        keys: Dict[str, bytes] = {}
        for rel, path, _ in self._db_files:
            try:
                with open(path, "rb") as f:
                    page1 = f.read(PAGE_SZ)
            except OSError:
                continue
            if len(page1) < PAGE_SZ:
                continue
            derived = _pbkdf2(master, page1[:16], self.KDF_ITER)
            if _verify_enc_key(derived, page1):
                keys[rel] = derived
        return keys

    def extract_master_key(self) -> Optional[Tuple[str, int, str]]:
        """从 Weixin.exe 进程 cfg 自动提取 (主密钥hex, cfgDword, wxId)。

        遍历微信进程调 extract_master_key_from_cfg; 主密钥可离线派生全部库。
        微信未运行或锚点漂移(版本变更)时返回 None, 由调用方回退。
        """
        pids = self._find_weixin_pids()
        for pid in pids:
            got = extract_master_key_from_cfg(pid)
            if got:
                return got
        return None

    def _key_works(self, rel: str) -> bool:
        """该库密钥是否能通过页1 校验（rel 已不存在等异常一律 False）。"""
        key = self._keys.get(rel)
        if not key:
            return False
        try:
            path = self._db_path(rel)
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
        except Exception:
            return False
        return _verify_enc_key(key, page1)

    def _db_path(self, rel: str) -> str:
        for r, path, _ in self._db_files:
            if r == rel:
                return path
        raise KeyError(rel)

    def extract_keys(self) -> Dict[str, bytes]:
        """从 Weixin.exe 进程内存扫描 Config.Cipher 对象，提取各库密钥"""
        pids = self._find_weixin_pids()
        if not pids:
            raise RuntimeError("未检测到 Weixin.exe，请先登录微信再运行")
        keys: Dict[str, bytes] = {}
        tested: set = set()
        for pid in pids:
            keys.update(self._extract_keys_pid(pid, tested))
            if len(keys) >= len(self._db_files):
                break
        return keys

    def _collect_key_candidates(self, pid: int, seen: set) -> set:
        """收集候选密钥材料（**不做账号校验**）：{(cand, salt_or_None), ...}

        与 `_extract_keys_pid` 的区别：这里只负责“找出可能是密钥的 32 字节
        （或 32+16 带显式 salt）”，校验交给调用方对**每个账号目录**分别做。
        原因：内存里的密钥属于**当前登录账号**，只有对正确的账号目录才能通过
        页1 HMAC；这样账号选择由密码学校验决定，不再依赖“最近修改 .db”启发式
        （微信每次更新会重写 .db，mtime 全变 → 启发式会选错账号 → 0 密钥）。
        """
        h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            return set()
        out: set = set()
        try:
            def read(addr: int, n: int):
                buf = ctypes.create_string_buffer(n)
                br = ctypes.c_size_t(0)
                if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n,
                                          ctypes.byref(br)) and br.value:
                    return buf.raw[: br.value]
                return None

            needles = self._find_bytes(h, read, CONFIG_CIPHER_NAME)
            pairs = [
                struct.pack("<Q", addr) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                for addr in needles
            ]
            for pair in pairs:
                for qaddr in self._find_bytes(h, read, pair):
                    node = read(qaddr - 0x10, 0x50)
                    if not node or len(node) < 0x40:
                        continue
                    if struct.unpack_from("<Q", node, 0x10)[0] not in needles:
                        continue
                    if struct.unpack_from("<Q", node, 0x18)[0] != len(CONFIG_CIPHER_NAME):
                        continue
                    config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                    if not (0x10000 <= config_ptr < 0x800000000000):
                        continue
                    obj = read(config_ptr + 0x88, 0x28)
                    if not obj or len(obj) < 0x18:
                        continue
                    data_ptr = struct.unpack_from("<Q", obj, 0x8)[0]
                    data_len = struct.unpack_from("<Q", obj, 0x10)[0]
                    if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                        continue
                    blob = read(data_ptr, int(data_len))
                    if not blob or len(blob) != data_len:
                        continue
                    decoded = bytes(
                        v ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)]
                        for i, v in enumerate(blob)
                    )
                    for m in HEX_LITERAL_RE.finditer(decoded):
                        run = m.group(1).decode().lower()
                        starts = [0]
                        if len(run) > 96:
                            starts += list(range(0, len(run) - 63, 32))
                            starts.append(len(run) - 64)
                        for s in dict.fromkeys(starts):
                            if s + 64 > len(run):
                                continue
                            cand = bytes.fromhex(run[s:s + 64])
                            if cand in seen or not self._probable_key(cand):
                                continue
                            seen.add(cand)
                            out.add((cand, None))
                            if s + 96 <= len(run):
                                out.add((cand, bytes.fromhex(run[s + 64: s + 96])))
        finally:
            _k32.CloseHandle(h)
        return out

    def _all_key_candidates(self) -> set:
        """一次内存扫描，收集所有候选密钥材料（与账号无关）。"""
        seen: set = set()
        out: set = set()
        for pid in self._find_weixin_pids():
            try:
                out |= self._collect_key_candidates(pid, seen)
            except Exception:
                continue
        return out

    def _keys_from_candidates(self, cands) -> Dict[str, bytes]:
        """把候选材料对**当前 self._db_files** 逐个 HMAC 校验，返回可用密钥。

        存储形式按库的实际布局决定：**先按标准形式（文件头 salt）校验**，通过就
        存 32 字节裸密钥；只有标准形式验不过时（明文头库）才用候选自带的显式
        salt 并存成 48 字节 key+salt。
        顺序很关键：若先试显式 salt，标准库也会被存成 48 字节，解密时就会走
        「明文头」分支，产出非 SQLite 文件（实测踩过：file is not a database）。
        """
        keys: Dict[str, bytes] = {}
        for cand, salt in cands:
            for rel, path, _ in self._db_files:
                if rel in keys:
                    continue
                try:
                    with open(path, "rb") as f:
                        page1 = f.read(PAGE_SZ)
                except OSError:
                    continue
                if _verify_enc_key(cand, page1):
                    keys[rel] = cand                      # 标准库：32 字节
                    break
                if salt and _verify_enc_key(cand, page1, salt=salt):
                    keys[rel] = cand + salt               # 明文头库：48 字节
                    break
        return keys

    def _select_account_by_keys(self, cands) -> bool:
        """用候选密钥给每个账号目录打分，选可用库最多的那个并切换。

        这是“微信更新后选错账号”的根除手段：不再看 mtime，而看**能不能解开**。
        返回是否发生了切换/改善。
        """
        if not cands:
            return False
        best = None
        try:
            dirs = _find_account_dirs(self.db_dir)
        except Exception as exc:
            wxlog.debug(f'枚举账号目录失败，跳过账号自愈：{exc!r}')
            return False
        for d in dirs:
            acct = os.path.basename(d)
            saved = (self.account, self.account_dir, self._db_files, self.workdir,
                     self.keys_file, self._keys)
            try:
                self.account, self.account_dir = acct, d
                self.workdir = os.path.join(tempfile.gettempdir(), "wechatauto_db", acct)
                self.keys_file = os.path.join(self.workdir, "keys.json")
                self._db_files = self._collect_db_files()
                if not self._db_files:
                    continue
                merged = {}
                if self.master_key:
                    try:
                        merged.update(self.derive_keys_from_master(self.master_key))
                    except Exception:
                        pass
                merged.update(self._keys_from_candidates(cands))
                ok = len(merged)
                if ok and (best is None or ok > best[0]):
                    best = (ok, acct, merged, list(self._db_files))
            except Exception:
                continue
            finally:
                (self.account, self.account_dir, self._db_files, self.workdir,
                 self.keys_file, self._keys) = saved
        if best is None:
            return False
        cur = sum(1 for rel, _, _ in self._db_files if self._key_works(rel))
        if best[0] <= cur:
            return False
        ok, acct, keys, files = best
        self.account = acct
        self.account_dir = os.path.join(self.db_dir, acct)
        self._keys, self._db_files = keys, files
        self.workdir = os.path.join(tempfile.gettempdir(), "wechatauto_db", acct)
        self.keys_file = os.path.join(self.workdir, "keys.json")
        try:
            os.makedirs(self.workdir, exist_ok=True)
            self._save_keys()
        except Exception:
            pass
        print("[wechatauto] 已按密钥校验选定账号目录: %s（%d/%d 个库可用密钥）"
              % (acct, ok, len(files)), file=sys.stderr)
        return True

    def _find_weixin_pids(self) -> List[int]:
        import subprocess

        try:
            r = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="gbk", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return []
        # 中文 Windows 的 tasklist 输出是 GBK：不显式指定编码时，UTF-8 模式
        # （-X utf8 / PYTHONUTF8=1）下解码失败会让 stdout 变成 None，
        # “取密钥”这一步就会以 AttributeError 收场（已实测复现）。
        pids = []
        for line in (r.stdout or "").strip().splitlines():
            parts = line.strip('"').split('","')
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(int(parts[1]))
        return pids

    def _extract_keys_pid(self, pid: int, tested: set) -> Dict[str, bytes]:
        h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            return {}
        try:
            def read(addr: int, n: int):
                buf = ctypes.create_string_buffer(n)
                br = ctypes.c_size_t(0)
                if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                    return buf.raw[: br.value]
                return None

            needles = self._find_bytes(h, read, CONFIG_CIPHER_NAME)
            pairs = [
                struct.pack("<Q", addr) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                for addr in needles
            ]
            keys: Dict[str, bytes] = {}
            for pair in pairs:
                for qaddr in self._find_bytes(h, read, pair):
                    node = read(qaddr - 0x10, 0x50)
                    if not node or len(node) < 0x40:
                        continue
                    if struct.unpack_from("<Q", node, 0x10)[0] not in needles:
                        continue
                    if struct.unpack_from("<Q", node, 0x18)[0] != len(CONFIG_CIPHER_NAME):
                        continue
                    config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                    if not (0x10000 <= config_ptr < 0x800000000000):
                        continue
                    obj = read(config_ptr + 0x88, 0x28)
                    if not obj or len(obj) < 0x18:
                        continue
                    data_ptr = struct.unpack_from("<Q", obj, 0x8)[0]
                    data_len = struct.unpack_from("<Q", obj, 0x10)[0]
                    if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                        continue
                    blob = read(data_ptr, int(data_len))
                    if not blob or len(blob) != data_len:
                        continue
                    decoded = bytes(
                        v ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)]
                        for i, v in enumerate(blob)
                    )
                    for m in HEX_LITERAL_RE.finditer(decoded):
                        run = m.group(1).decode().lower()
                        starts = [0]
                        if len(run) > 96:
                            starts += list(range(0, len(run) - 63, 32))
                            starts.append(len(run) - 64)
                        for s in dict.fromkeys(starts):
                            if s + 64 > len(run):
                                continue
                            cand = bytes.fromhex(run[s:s + 64])
                            if cand in tested or not self._probable_key(cand):
                                continue
                            tested.add(cand)
                            # 96hex 形式：后 32 hex 是显式 salt（Raw Key with
                            # Explicit Salt），与文件头 salt 都要尝试
                            explicit = None
                            if s + 96 <= len(run):
                                explicit = bytes.fromhex(run[s + 64: s + 96])
                            salt_choices = [None]
                            if explicit:
                                salt_choices.append(explicit)
                            for salt_opt in salt_choices:
                                for rel, path, _ in self._db_files:
                                    if rel in keys:
                                        continue
                                    with open(path, "rb") as f:
                                        page1 = f.read(PAGE_SZ)
                                    if _verify_enc_key(cand, page1, salt=salt_opt):
                                        # 显式 salt 通过 → 存 key+salt（明文头模式）；
                                        # 文件头 salt 通过 → 存裸 key
                                        keys[rel] = cand + (salt_opt or b"")
                                        break
        finally:
            _k32.CloseHandle(h)
        return keys

    @staticmethod
    def _probable_key(b: bytes) -> bool:
        return (
            len(b) == 32
            and len(set(b)) >= 15
            and b not in {b"\x00" * 32, b"\xff" * 32}
        )

    @staticmethod
    def _find_bytes(h, read, needle: bytes) -> List[int]:
        hits = []
        addr = 0
        while True:
            mbi = _MBI()
            r = _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if r == 0:
                break
            if (
                mbi.State == 0x1000
                and (mbi.Protect & 0xFF) & 0xE6
                and not (mbi.Protect & 0x100)
                and 0 < mbi.RegionSize < 0x10000000
            ):
                buf = read(mbi.BaseAddress or 0, mbi.RegionSize)
                if buf:
                    base = mbi.BaseAddress or 0
                    pos = 0
                    while True:
                        pos = buf.find(needle, pos)
                        if pos < 0:
                            break
                        hits.append(base + pos)
                        pos += 1
            addr = (mbi.BaseAddress or 0) + mbi.RegionSize
        return hits

    def _stable_key_dirs(self) -> List[str]:
        """密钥只写入本工具的缓存目录，不覆盖上游或微信的文件。"""
        return [self.workdir]

    def _stable_key_file(self, account: Optional[str] = None) -> Optional[str]:
        dirs = self._stable_key_dirs()
        if not dirs:
            return None
        return os.path.join(dirs[0], (account or self.account) + ".json")

    def _load_key_cache(self, path: str) -> Dict[str, bytes]:
        """读一个 keys.json（只返回格式合法的条目）。"""
        out: Dict[str, bytes] = {}
        try:
            with open(path, encoding="utf-8") as f:
                for rel, hexkey in json.load(f).items():
                    try:
                        out[rel] = bytes.fromhex(hexkey)
                    except (ValueError, TypeError):
                        pass
        except (OSError, json.JSONDecodeError):
            pass
        return out

    def _save_keys(self) -> None:
        """写密钥缓存。

        两个保护：
        1. **绝不把非空缓存覆盖成空缓存**——提取偶发失败（权限/版本/未登录）
           不该毁掉已有的好缓存，否则下次再无回退可用；
        2. 原子写入 + 保留 .bak，并同步一份到稳定目录（跨 TEMP 清理/微信更新）。
        """
        if not self._keys:
            return
        data = {k: v.hex() for k, v in self._keys.items()}
        try:
            os.makedirs(os.path.dirname(self.keys_file), exist_ok=True)
            tmp = self.keys_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            if os.path.exists(self.keys_file):
                try:
                    with open(self.keys_file, "rb") as src, \
                            open(self.keys_file + ".bak", "wb") as dst:
                        dst.write(src.read())
                except OSError:
                    pass
            os.replace(tmp, self.keys_file)
        except OSError:
            pass
        # 稳定副本
        sf = self._stable_key_file()
        if sf:
            try:
                os.makedirs(os.path.dirname(sf), exist_ok=True)
                with open(sf, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except OSError:
                pass

    WAL_HEADER_SZ = 32   # WCDB WAL 文件头

    WAL_FRAME_SZ = 4120  # 帧头 24 字节(大端 pgno + 校验等) + 4096 加密页

    def _auto_diagnose_key_failure(self, rel: str) -> None:
        """密钥缺失报错时的自动诊断，向 stderr 输出三项最常见根因：
        1) Python 位数（32 位 Python 读不了 64 位微信进程内存）；
        2) 微信进程读取权限（管理员权限不匹配 → OpenProcess 失败 → 0 密钥）；
        3) 多账号目录与所选账号对比（选错账号 → 密钥验证不过）。
        """
        print("[wechatauto] 密钥诊断: '%s' 无可用密钥" % rel, file=sys.stderr)
        bits = 64 if sys.maxsize > 2**32 else 32
        if bits != 64:
            print("[wechatauto]  >> 当前 Python 是 %d 位，而微信 4.x 是 64 位进程。"
                  "请改用 64 位 Python（python -c \"import struct; print(struct.calcsize('P')*8)\" 应输出 64）"
                  % bits, file=sys.stderr)
        import subprocess as _sp
        try:
            r = _sp.run(
                ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="gbk", errors="replace",
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            return
        pids = []
        for line in (r.stdout or "").strip().splitlines():
            parts = line.strip('"').split('","')
            if len(parts) >= 2 and parts[1].isdigit():
                pids.append(int(parts[1]))
        if not pids:
            print("[wechatauto]  >> 未检测到 Weixin.exe 进程，请先登录微信并保持窗口打开",
                  file=sys.stderr)
            return
        perms = []
        for pid in pids:
            h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
            if not h:
                err = ctypes.get_last_error()
                perms.append((pid, False, err))
            else:
                _k32.CloseHandle(h)
                perms.append((pid, True, 0))
        blocked = [p for p, ok, err in perms if not ok]
        if blocked:
            print("[wechatauto]  >> 部分微信进程无法读取内存 (PID %s，错误码 %s)："
                  "请用管理员身份运行 Python（若微信本身以管理员运行），"
                  "或取消微信的\"以管理员身份运行\"后重新登录"
                  % (", ".join(str(p) for p, _, _ in blocked),
                     ", ".join(str(e) for _, _, e in blocked)),
                  file=sys.stderr)
        accounts = [
            os.path.basename(d)
            for d in _find_account_dirs(self.db_dir)
        ]
        if len(accounts) > 1:
            print("[wechatauto]  >> 检测到多个微信账号目录: %s；当前自动选择: %s。"
                  "若报错，请用 WeChatDB(account=\"当前登录账号\") 显式指定"
                  % (", ".join(sorted(accounts)), self.account),
                  file=sys.stderr)
        print("[wechatauto] 密钥诊断完成，以上为自动检测结果。完整排查请运行 "
              "在工具中确认账号、数据目录与微信登录状态后重试", file=sys.stderr)

    def _build_lock_for(self, rel: str) -> threading.Lock:
        """按库文件取一把构建锁（注册表本身用一把小锁保护）。"""
        reg = getattr(self, "_build_locks", None)
        if reg is None:
            reg = self._build_locks = {}
            self._build_locks_guard = threading.Lock()
        with self._build_locks_guard:
            return reg.setdefault(rel, threading.Lock())

    def _open(self, rel: str) -> sqlite3.Connection:
        """同一份库的并发解密要串行：两个线程各自解一遍是几秒级的重复重活，
        还会互相踩中间产物（监听所有会话时几百个会话同时开库必现）。"""
        with self._build_lock_for(rel):
            return self._open_unlocked(rel)

    def _open_unlocked(self, rel: str) -> sqlite3.Connection:
        """打开解密(并合并 -wal 增量)后的只读库。

        解密结果缓存到 workdir；主库或 WAL 有变化时：
        - 主库被 checkpoint 改写（mtime/size 变化）或 WAL 被重置 → 全量重建；
        - 仅 WAL 追加了新帧 → 增量合并新帧（秒级）。
        """
        if rel not in self._keys:
            self._auto_diagnose_key_failure(rel)
            raise RuntimeError(
                "数据库无可用密钥: %s。请确认微信已登录且保持窗口打开。"
                "常见原因：① 32 位 Python 读不了 64 位微信内存；"
                "② 本脚本权限低于微信（微信以管理员运行时，脚本也要以管理员运行）；"
                "③ 多账号机器选错账号（用 WeChatDB(account=\"当前登录账号\") 指定）。"
                "仍复现请运行 在工具中确认账号、数据目录与微信登录状态后重试 并把完整输出发给维护者。"
                "也可删除密钥缓存强制重新提取后重试: %s"
                % (rel, self.keys_file)
            )
        src = self._db_path(rel)
        dst = os.path.join(self.workdir, rel.replace(os.sep, "__"))
        key = self._keys[rel]
        if len(key) == 48:
            # 兼容历史上误存的形式：48 字节 = key+salt，仅适用于「明文头」库；
            # 标准 SQLCipher 库若存成 48 字节，解密会保留文件头 16 字节、从
            # offset 16 开始解 → 产出非 SQLite 文件（file is not a database）。
            # 用页1 校验判定：文件头 salt 能验过 → 标准形式，截回 32 字节。
            try:
                with open(src, "rb") as _f:
                    _p1 = _f.read(PAGE_SZ)
                if _verify_enc_key(key[:32], _p1):
                    key = key[:32]
            except OSError:
                pass
        src_mtime = os.path.getmtime(src)
        src_size = os.path.getsize(src)
        wal_path = self._wal_path(rel)
        wal_mtime = os.path.getmtime(wal_path) if wal_path else 0.0
        wal_size = os.path.getsize(wal_path) if wal_path else 0
        stamp = dst + ".stamp"
        old = None
        if os.path.exists(stamp):
            try:
                with open(stamp, "r") as f:
                    parts = f.read().split(",")
                old = {
                    "ver": int(parts[0]),
                    "mtime": float(parts[1]),
                    "size": int(parts[2]),
                    "wal_mtime": float(parts[3]),
                    "wal_size": int(parts[4]),
                    "applied": int(parts[5]),
                }
                if old["ver"] != STAMP_VERSION:
                    old = None
            except (ValueError, OSError, IndexError):
                old = None
        build = (not old or old["mtime"] != src_mtime or old["size"] != src_size
                 or old["wal_mtime"] != wal_mtime or old["wal_size"] != wal_size)
        # 构建策略：**先做主库自洽快照当底线，再尝试合并 WAL**。
        # 主库在上次 checkpoint 时是自洽的（SQLite 保证），所以「仅解密主库」
        # 一定可读，代价是可能缺最近少量消息；WAL 合并成功则用更新的那份。
        # 关键：任何中间产物都写在 tmp，**成功才原子替换** dst，失败不会毁掉
        # 上一份已验证副本。
        # 中间产物按「进程 + 线程」唯一命名：固定名 dst+".tmp" 会被并发打开同一份
        # 库的另一个线程用 os.replace 掉，这边就 FileNotFoundError（监听所有会话、
        # 几百个会话同时开库时必现）。唯一命名后异常退出会留下残留文件，故构建前
        # 顺手清掉 10 分钟前的陈旧中间产物。
        tmp = "%s.%d.%x.tmp" % (dst, os.getpid(), threading.get_ident())
        applied = 0
        if build:
            for stale in glob.glob(dst + ".*.tmp*"):
                try:
                    if time.time() - os.path.getmtime(stale) > 600:
                        os.remove(stale)
                except OSError:
                    pass
            # 微信 checkpoint 会**就地改写主库页**：单次读取可能读到“撕裂”状态
            # （页头与内容来自不同时刻）→ quick_check 会失败。故解密后必须校验，
            # 失败就重读（每次重读都是一次新的快照）。
            best = None
            for attempt in range(1, 5):
                self._decrypt_file(src, tmp, key)
                if self._check_merged(tmp):
                    best = tmp
                    break
                wxlog.debug('主库解密快照校验失败（第 %d/4 次）: %s' % (attempt, rel))
                time.sleep(0.35)
            if best is None:
                if os.path.exists(dst) and self._check_merged(dst):
                    self.snapshot_warnings.append('数据库正在更新，本次使用上次可用快照，可能缺少新评论：' + rel)
                    wxlog.warning('主库持续处于撕裂状态，改用上一份可用副本（可能略旧）: %s'
                                  % rel)
                    build = False
                else:
                    raise RuntimeError("数据库解密结果校验失败(微信正在 checkpoint): %s" % rel)
            if build:
                best_applied = 0
                if wal_path and wal_size > self.WAL_HEADER_SZ:
                    merged = tmp + ".wal"
                    ok_wal = False
                    for attempt in (1, 3):
                        try:
                            shutil.copyfile(best, merged)
                            got = self._merge_wal(merged, wal_path, key, 0)
                            if self._check_merged(merged):
                                os.replace(merged, best)
                                best_applied, ok_wal = got, True
                                break
                        except Exception as exc:
                            wxlog.debug('WAL 合并第 %d 次失败：%r' % (attempt, exc))
                        time.sleep(0.3)
                    if not ok_wal:
                        self.snapshot_warnings.append('WAL 合并失败，本次仅使用主库，可能缺少新评论：' + rel)
                        best_applied = -1          # 标记：本轮未合并 WAL
                        wxlog.warning('WAL 合并失败（微信持续写入），改用仅主库快照'
                                      '（可能缺少最近消息）: %s' % rel)
                os.replace(best, dst)
                applied = best_applied
                os.makedirs(os.path.dirname(stamp), exist_ok=True)
                with open(stamp, "w") as f:
                    f.write("%d,%r,%d,%r,%d,%d"
                            % (STAMP_VERSION, src_mtime, src_size, wal_mtime, wal_size, applied))
            build = False
        if old and old.get('applied') == -1:
            self.snapshot_warnings.append('缓存未合并 WAL，可能缺少新评论：' + rel)
        conn = sqlite3.connect(Path(dst).resolve().as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        conn.text_factory = _sqlite_text_factory
        return conn

    @staticmethod
    def _check_merged(dst: str) -> bool:
        """校验解密/合并结果可完整读取。

        只兜底 sqlite_master 无法发现数据页损坏：WAL 若带入过期/错位页，
        schema 树可能仍正常，但表数据页已损坏，直到 SELECT 才抛
        "database disk image is malformed"。这里用 PRAGMA quick_check
        全库校验（含数据页与索引页），损坏时返回 False，触发全量重建重试。
        """
        try:
            conn = sqlite3.connect(Path(dst).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                rows = conn.execute("PRAGMA quick_check").fetchall()
            finally:
                conn.close()
            return bool(rows) and all(str(r[0]) == "ok" for r in rows)
        except sqlite3.Error:
            return False

    def _wal_path(self, rel: str) -> Optional[str]:
        wal = self._db_path(rel) + "-wal"
        return wal if os.path.exists(wal) else None

    def _merge_wal(self, dst: str, wal_path: str, key: bytes, from_frame: int) -> int:
        """把 -wal 中的加密帧按页号覆盖进已解密的主库文件，返回已应用帧数。

        帧结构（WCDB，全部大端）：[0:4] 页号, [4:8] 提交标记, [8:16] salt, [16:24] 校验。
        帧内页面与主库页相同加密格式，直接用库密钥解密。
        页 1 帧用页 1 专用布局（数据区 [16:4016]，IV 在 [4016:4032]）解密。

        只合并 salt 与当前 WAL 头一致的帧：微信 checkpoint 会重置 WAL（salt+1 并
        清零写游标），旧世代帧若被合并会用过期页覆盖新数据，造成库损坏。
        """
        if not os.path.exists(dst):
            return 0
        out = open(dst, "r+b")
        try:
            db_pages = (os.path.getsize(dst) + 4095) // PAGE_SZ
            max_pgno = 0
            last = from_frame
            with open(wal_path, "rb") as wal:
                wal_hdr = wal.read(self.WAL_HEADER_SZ)
                wal_salt = wal_hdr[16:24]
                wal_size = os.path.getsize(wal_path)
                n = (wal_size - self.WAL_HEADER_SZ) // self.WAL_FRAME_SZ
                for i in range(from_frame, n):
                    wal.seek(self.WAL_HEADER_SZ + i * self.WAL_FRAME_SZ)
                    hdr = wal.read(24)
                    page = wal.read(PAGE_SZ)
                    if len(page) < PAGE_SZ:
                        break
                    pgno = struct.unpack(">I", hdr[:4])[0]
                    last = i + 1
                    if hdr[8:16] != wal_salt:
                        continue
                    pt = _decrypt_page(key, page, pgno)
                    if pgno == 1:
                        # 明文头模式（48B key）页 1 解密后保留明文头，
                        # 头部 magic 可能不是标准 SQLite；用版本字节兜底校验。
                        if len(key) == 48:
                            if len(pt) < PAGE_SZ or pt[16:18] != b"\x01\x01":
                                continue
                        elif pt[:16] != b"SQLite format 3\x00":
                            continue
                    elif pt[0] not in (0, 2, 5, 10, 13):
                        continue
                    out.seek((pgno - 1) * PAGE_SZ)
                    out.write(pt)
                    max_pgno = max(max_pgno, pgno)
            out.flush()
            db_pages = (os.path.getsize(dst) + 4095) // PAGE_SZ
            out.seek(0)
            page1 = out.read(PAGE_SZ)
            hdr_pages = struct.unpack(">I", page1[28:32])[0]
            new_pages = max(hdr_pages, max_pgno, db_pages)
            if new_pages != hdr_pages:
                page1 = page1[:28] + struct.pack(">I", new_pages) + page1[32:]
                out.seek(0)
                out.write(page1)
            out.flush()
        finally:
            out.close()
        return last

    def _decrypt_file(self, src: str, dst: str, key: bytes) -> None:
        size = os.path.getsize(src)
        pages = size // PAGE_SZ + (1 if size % PAGE_SZ else 0)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            for pgno in range(1, pages + 1):
                page = fin.read(PAGE_SZ)
                if not page:
                    break
                if len(page) < PAGE_SZ:
                    page = page + b"\x00" * (PAGE_SZ - len(page))
                fout.write(_decrypt_page(key, page, pgno))

    def get_nickname(self, user: str) -> str:
        """通过微信号查昵称（用于显示）"""
        for rel, path, _ in self._db_files:
            if os.path.basename(path) != "contact.db":
                continue
            conn = self._open(rel)
            try:
                row = conn.execute(
                    "SELECT nick_name, remark FROM contact WHERE username=? LIMIT 1",
                    (user,),
                ).fetchone()
            finally:
                conn.close()
            if row:
                return row["remark"] or row["nick_name"] or user
            break
        return user

def list_accounts(db_dir: Optional[str] = None) -> List[dict]:
    """扫描数据目录下的所有微信账号目录。

    返回: [{"account": "wxid_xxx_abcd", "wxid": "wxid_xxx",
            "path": ..., "last_activity": mtime, "self_nick": 昵称或空}]
    """
    db_dir = db_dir or auto_detect_db_dir()
    if not db_dir:
        return []
    out = []
    for d in _find_account_dirs(db_dir):
        recent = max(
            (
                os.path.getmtime(os.path.join(root, f))
                for root, _, files in os.walk(os.path.join(d, "db_storage"))
                for f in files
                if f.endswith(".db") and not f.endswith("-wal")
            ),
            default=0,
        )
        account = os.path.basename(d)
        out.append({
            "account": account,
            "wxid": re.sub(r"_\w{4}$", "", account),
            "path": d,
            "last_activity": recent,
        })
    out.sort(key=lambda x: -x["last_activity"])
    return out

def _extract_path_from_config(content: str) -> Optional[str]:
    """从配置内容中提取数据目录路径，兼容 JSON 字段 / 纯路径 / 任意文本。

    微信 4.x 不同版本配置文件格式不一：有的是纯路径，有的是 JSON
    （字段如 dataDir / fileSavePath）。这里统一兜底提取第一个 Windows 路径。
    """
    content = (content or "").strip().lstrip("\ufeff")
    if not content:
        return None
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            for key in ("dataDir", "data_dir", "fileSavePath", "savePath",
                        "path", "defaultFileSavePath"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, str) and re.match(r"^[A-Za-z]:[\\/]", item):
                    return item
    except Exception:
        pass
    if re.match(r"^[A-Za-z]:[\\/]", content):
        return content
    m = re.search(r"[A-Za-z]:[\\/][^\s\x00-\x1f\"']+", content)
    if m:
        return m.group(0).rstrip("\\/")
    return None

def _config_candidates() -> List[str]:
    """可能的微信 4.x 配置目录（按新旧版本与 32/64 位安装差异）。"""
    out = []
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.environ.get(env, "")
        if base:
            out.extend([
                os.path.join(base, "Tencent", "xwechat"),
                os.path.join(base, "Tencent", "xwechat", "config"),
                os.path.join(base, "Tencent", "WeChat"),
            ])
    return out

def _registry_data_dirs() -> List[str]:
    """从注册表读取可能指向数据目录的值（用户自定义保存位置时补充来源）。"""
    import winreg
    dirs = []
    for hive, sub in (
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\xwechat"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\xwechat\config"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"),
    ):
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if not isinstance(data, str) or not data.strip():
                    continue
                low = name.lower()
                if "path" in low or "dir" in low or "save" in low:
                    dirs.append(data.strip())
        finally:
            winreg.CloseKey(key)
    return dirs

def _locate_account_root(root: Optional[str]) -> Optional[str]:
    """在候选根目录下定位「包含账号目录」的目录。

    账号目录以含 db_storage 子目录为准（微信号不一定以 wxid_ 开头）。
    兼容两种布局：
      <root>/xwechat_files/<account>/db_storage
      <root>/<account>/db_storage
    返回的目录即 WeChatDB.db_dir（账号目录的父目录）。
    """
    if not root or not os.path.isdir(root):
        return None
    root = root.rstrip("\\/")
    candidates = [root]
    for name in ("xwechat_files", "WeChat Files", "xwechat_files_data"):
        candidates.append(os.path.join(root, name))
    seen = set()
    for cand in candidates:
        cand = cand.rstrip("\\/")
        if cand in seen or not os.path.isdir(cand):
            continue
        seen.add(cand)
        try:
            dirs = os.listdir(cand)
        except OSError:
            continue
        if any(
            os.path.isdir(os.path.join(cand, d, "db_storage"))
            for d in dirs
        ):
            return cand
    return None

def auto_detect_db_dir() -> Optional[str]:
    """自动定位微信 4.x 数据目录（不同电脑存储位置不同）。

    探测顺序：
      1. 微信配置文件（%APPDATA%/%LOCALAPPDATA%，支持 JSON/纯路径/任意文本）；
      2. 注册表；
      3. 常见默认目录（Documents / 用户主目录）。
    """
    # 1) 配置文件
    for cfg_dir in _config_candidates():
        if not os.path.isdir(cfg_dir):
            continue
        for fp in glob.glob(os.path.join(cfg_dir, "*")):
            if os.path.isdir(fp):
                continue
            try:
                raw = open(fp, "r", encoding="utf-8").read(8192)
            except (UnicodeDecodeError, OSError):
                try:
                    raw = open(fp, "r", encoding="gbk").read(8192)
                except (UnicodeDecodeError, OSError):
                    continue
            path = _extract_path_from_config(raw)
            if not path:
                continue
            hit = _locate_account_root(path)
            if hit:
                return hit
    # 2) 注册表
    for p in _registry_data_dirs():
        hit = _locate_account_root(p)
        if hit:
            return hit
    # 3) 常见默认目录兜底
    userprofile = os.environ.get("USERPROFILE", "")
    for base in (os.path.join(userprofile, "Documents"), userprofile):
        hit = _locate_account_root(base)
        if hit:
            return hit
    return None

