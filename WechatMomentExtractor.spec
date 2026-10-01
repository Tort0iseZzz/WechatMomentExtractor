# 单文件 Windows 桌面程序，只打包本目录的只读模块。
from pathlib import Path
root = Path(SPECPATH)
a = Analysis([str(root / 'run.py')], pathex=[str(root)], binaries=[],
             datas=[(str(root / 'LICENSE'), '.'), (str(root / 'NOTICE'), '.')], hiddenimports=['wechatauto.db'],
             excludes=['uiautomation', 'pyautogui', 'pyperclip', 'winsdk', 'cv2'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='WechatMomentExtractor', debug=False,
          strip=False, upx=False, console=False, disable_windowed_traceback=False)
