# WechatMomentExtractor

English · [中文](README.md)

A read-only Windows desktop tool for backing up WeChat Moments and comments to JSON. Filter by text, author and date, select individual comments, and reopen backups offline. The interface is currently in Chinese.

## Download and use

Requires **64-bit Windows 10/11** and desktop **WeChat 4.x**. The packaged exe needs no Python installation. WeChat updates may affect database compatibility.

1. Download the Windows zip from [Releases](https://github.com/Tort0iseZzz/WechatMomentExtractor/releases), extract it, and run `WechatMomentExtractor.exe`.
2. Sign in to your own account in desktop WeChat. Open the target Moment and its comments, and wait for them to load.
3. Click **扫描账号** (scan accounts), confirm the account, then **读取 / 刷新** (read / refresh). If detection fails, use **选择目录** (choose folder) to select `xwechat_files` or your account folder.
4. Search by post text, Moment ID, author or date (`YYYY-MM-DD`). Select a result and verify its text, author and time.
5. Filter comments. **不包括全部回复消息** excludes replies; **手动筛选（大窗口）** opens a large selection dialog. Individual selections are saved automatically.
6. Click the green **导出筛选的评论** button to export selected, matching comments. Separate buttons export the complete Moment or search results. Use **打开 JSON 备份** to reopen a backup offline.

In a small window, use the right scrollbar; export buttons stay at the bottom. Apply comment filters with **筛选评论** in the main window, or **应用筛选** / Enter in the dialog.

## Data and limitations

- Exports text, authors, timestamps and reply references. Displayed times use Asia/Shanghai; original Unix timestamps are retained, and missing times are `null`.
- Filtered exports contain only matching, checked comments and omit the original XML. Full exports preserve the XML.
- **Only locally cached content can be read; complete historical comments are not guaranteed.** Open the content in WeChat and refresh. Known comment-count discrepancies are reported.
- Reads only `sns.db` and `contact.db`; does not send messages, like posts, comment, or modify WeChat databases or processes. Images, videos and chat history are not downloaded.
- Cache, logs and selections are stored under `%LOCALAPPDATA%\WechatMomentExtractor`; override with `MOMENT_EXTRACTOR_DATA_DIR`. Keep decrypted caches, keys and exported files private.

If reading fails, check that WeChat is signed in, the data folder is correct, and both applications have matching permissions. Report issues in [Issues](https://github.com/Tort0iseZzz/WechatMomentExtractor/issues) with Windows, WeChat and tool versions; redact private information from logs.

## Run from source and build

Install **64-bit Python 3.12+ with Tk**, clone this repository, and run these commands in its folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[build]"
.\.venv\Scripts\python.exe run.py
```

Append `--demo` to the last command for a demo without WeChat, or `--open "backup.json"` to open an existing backup.

Run `powershell -ExecutionPolicy Bypass -File build.ps1` to run offline tests and build `dist\WechatMomentExtractor.exe`. Tests only: `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`.

## License and credits

[Apache-2.0](LICENSE). Database decryption and read-only memory scanning are adapted from [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica), with attribution in [NOTICE](NOTICE). Only code needed for read-only Moments backups is distributed.
