"""离线验证文本不丢失、SQLite 只读与备份保存行为。"""
import ast
import hashlib
import hmac
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from wechatauto.app import demo_feeds
from wechatauto.backup import filtered_feed, load_backup, make_backup, save_backup
from wechatauto.db import PAGE_SZ, RESERVE_SZ, WeChatDB, _decrypt_page, _pbkdf2, _verify_enc_key
from wechatauto.moment import MomentDB, date_bound, filter_comments, filter_feeds, is_reply, parse_feed

class ReadonlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.feed = demo_feeds()[0]

    def tearDown(self): self.temp.cleanup()

    def test_cdata_entities_and_long_text(self):
        content = '第一行\n<&> 和 emoji 🌙' + '长文本' * 3000
        xml = self.feed['raw_xml'].replace('今天散步，看到了很好看的晚霞。', content)
        feed = parse_feed(xml, tid=42)
        self.assertEqual(feed['comments'][0]['content'], content)
        self.assertEqual(feed['comments'][1]['content'], '秋天真好 & 希望天气继续晴朗')

    def test_real_tid_and_large_ids(self):
        self.assertEqual(self.feed['tid'], '42')
        self.assertEqual(self.feed['id'], '9000000000000000001')
        self.assertEqual(self.feed['comments'][1]['ref_comment_id'], '101')

    def test_missing_timestamp_and_author_preserved(self):
        xml = self.feed['raw_xml'].replace('<create_time>1790848800</create_time>', '').replace('<username>wxid_demo</username>', '')
        feed = parse_feed(xml, fallback_user='fallback', tid=42)
        self.assertEqual(len(feed['comments']), 3)
        self.assertIsNone(feed['comments'][0]['create_time'])
        self.assertIsNone(feed['comments'][0]['published_at'])
        self.assertTrue(feed['warnings'])

    def test_deleted_and_missing_cache_warning(self):
        xml = self.feed['raw_xml'].replace('<b_deleted>0</b_deleted>', '<b_deleted>1</b_deleted>').replace('<commentCount>3</commentCount>', '<commentCount>100</commentCount>')
        feed = parse_feed(xml)
        self.assertEqual(len(feed['comments']), 3)
        self.assertEqual(feed['comments'][0]['deleted'], 1)
        self.assertIn('100', feed['warnings'][0])

    def test_date_boundaries_in_beijing(self):
        self.assertEqual(date_bound('2026-10-01', True) - date_bound('2026-10-01'), 86399)
        self.assertEqual(date_bound('1970-01-01'), -28800)
        self.assertIsNone(date_bound(''))
        with self.assertRaises(ValueError): date_bound('2026-02-30')

    def test_filters_do_not_shorten_backup(self):
        self.assertEqual(len(filter_comments(self.feed['comments'], author='朋友')), 1)
        self.assertEqual(len(filter_feeds([self.feed], keyword='废话条')), 1)
        self.assertEqual(filter_feeds([self.feed], since=1893456000), [])
        self.assertEqual(make_backup([self.feed])['comment_count'], 3)

    def test_reply_zero_and_alternative_reply_markers(self):
        self.assertFalse(is_reply({'ref_comment_id': '0', 'ref_comment_64id': ' 0 ', 'ref_username': ''}))
        self.assertFalse(is_reply({'ref_comment_id': None}))
        self.assertTrue(is_reply({'ref_comment_id': '101'}))
        self.assertTrue(is_reply({'ref_comment_id': '0', 'ref_comment_64id': '9000000000000000001'}))
        self.assertTrue(is_reply({'ref_comment_id': '0', 'ref_username': 'wxid_friend'}))
        self.assertEqual(len(filter_comments(self.feed['comments'], exclude_replies=True)), 2)
        self.assertEqual(filter_comments(self.feed['comments'], author='朋友', exclude_replies=True), [])

    def test_filtered_export_has_no_hidden_comment_xml(self):
        selected = [self.feed['comments'][2]]
        result = filtered_feed(self.feed, selected, {'exclude_replies': True})
        path = self.root / 'filtered.json'
        save_backup(path, make_backup([result]))
        backup = load_backup(path)
        self.assertEqual(backup['comment_count'], 1)
        self.assertEqual(backup['moments'][0]['source_comment_count'], 3)
        self.assertNotIn('raw_xml', result)
        self.assertNotIn(self.feed['comments'][0]['content'], path.read_text(encoding='utf-8'))
        self.assertNotIn(self.feed['comments'][1]['content'], path.read_text(encoding='utf-8'))
        self.assertEqual(len(self.feed['comments']), 3)
        self.assertIn('raw_xml', self.feed)
        result['comments'][0]['content'] = '修改副本'
        self.assertNotEqual(self.feed['comments'][2]['content'], '修改副本')

    def _sqlite(self, malformed=False):
        path = self.root / '中文 # 与空格 sns.db'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE SnsTimeLine(tid INTEGER, user_name TEXT, content TEXT)')
            conn.execute('INSERT INTO SnsTimeLine VALUES(?,?,?)', (42, 'wxid_demo', '<broken' if malformed else self.feed['raw_xml']))
            conn.commit()
        return path

    def test_sqlite_readonly_and_unchanged_file(self):
        path = self._sqlite()
        before = hashlib.sha256(path.read_bytes()).digest()
        reader = MomentDB(sqlite_path=path)
        result = reader.get_moments()
        self.assertEqual(result[0]['tid'], '42')
        self.assertEqual(len(result[0]['comments']), 3)
        conn = reader._open_sns()
        try:
            with self.assertRaises(sqlite3.OperationalError): conn.execute('DELETE FROM SnsTimeLine')
        finally: conn.close()
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)

    def test_malformed_xml_stops_instead_of_silent_omission(self):
        reader = MomentDB(sqlite_path=self._sqlite(malformed=True))
        with self.assertRaisesRegex(RuntimeError, 'tid=42'): reader.get_moments()

    def test_json_roundtrip_and_metadata(self):
        path = self.root / '废话条.json'
        save_backup(path, make_backup([self.feed], 'wxid_demo'))
        backup = load_backup(path)
        self.assertEqual(backup['moments'][0], self.feed)
        self.assertEqual(backup['completeness'], 'local_cache_only')
        self.assertIn('废话条', path.read_text(encoding='utf-8'))

    def test_atomic_save_failure_preserves_old_backup(self):
        path = self.root / 'backup.json'
        path.write_text('original', encoding='utf-8')
        with patch('wechatauto.backup.os.replace', side_effect=OSError('locked')):
            with self.assertRaises(OSError): save_backup(path, make_backup([self.feed]))
        self.assertEqual(path.read_text(), 'original')
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_export_cannot_write_into_wechat_directory(self):
        with self.assertRaises(ValueError): save_backup(self.root / 'backup.json', make_backup([self.feed]), self.root)
        self.assertFalse((self.root / 'backup.json').exists())

    def test_invalid_backup_rejected(self):
        path = self.root / 'bad.json'
        path.write_text(json.dumps({'schema_version': 1, 'moments': [{'text': 'test'}]}))
        with self.assertRaises(ValueError): load_backup(path)

    def test_sqlcipher_page_hmac_and_aes_roundtrip(self):
        key, salt, iv = os.urandom(32), os.urandom(16), os.urandom(16)
        plaintext = os.urandom(PAGE_SZ - RESERVE_SZ - 16)
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        cipher = enc.update(plaintext) + enc.finalize()
        mac_key = _pbkdf2(key, bytes(b ^ 0x3a for b in salt), 2)
        digest = hmac.new(mac_key, cipher + iv + struct.pack('<I', 1), hashlib.sha512).digest()
        page = salt + cipher + iv + digest
        self.assertTrue(_verify_enc_key(key, page))
        self.assertFalse(_verify_enc_key(os.urandom(32), page))
        self.assertEqual(_decrypt_page(key, page, 1), b'SQLite format 3\x00' + plaintext + bytes(RESERVE_SZ))

    def test_only_sns_contact_collected(self):
        db = WeChatDB.__new__(WeChatDB)
        db.account_dir = str(self.root)
        storage = self.root / 'db_storage'
        storage.mkdir()
        for name in ('sns.db', 'contact.db', 'message_0.db', 'sns.db-wal'): (storage / name).write_bytes(b'x')
        self.assertEqual({Path(row[1]).name for row in db._collect_db_files()}, {'sns.db', 'contact.db'})

    def test_no_wechat_mutation_apis_and_readonly_process_permissions(self):
        for path in (Path(__file__).resolve().parents[1] / 'wechatauto').glob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr, {'WriteProcessMemory', 'SendInput', 'mouse_event', 'keybd_event', 'SetClipboardText'})
                    if node.func.attr == 'OpenProcess': self.assertEqual(eval(compile(ast.Expression(node.args[0]), '', 'eval')), 0x0410)

if __name__ == '__main__': unittest.main()
