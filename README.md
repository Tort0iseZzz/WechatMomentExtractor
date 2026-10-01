# WechatMomentExtractor

[English](README.en.md) · 中文

Windows 微信朋友圈只读备份工具：查看朋友圈及评论，按关键词、发布者、日期筛选，逐条勾选后导出 JSON。支持离线打开备份，界面为中文。

## 下载与使用

要求：Windows 10/11 **64 位**，PC 微信 **4.x**。exe 无需安装 Python；微信版本变化可能影响数据库读取。

1. 从 [Releases](https://github.com/Tort0iseZzz/WechatMomentExtractor/releases) 下载 Windows 压缩包，解压后运行 `WechatMomentExtractor.exe`。
2. 在 PC 微信登录自己的账号，打开目标朋友圈及评论区，等待加载。
3. 点击「扫描账号」，确认账号后「读取 / 刷新」。找不到数据时，用「选择目录」选择微信的 `xwechat_files` 或其账号目录。
4. 按正文关键词、朋友圈 ID、发布者或日期查找，日期格式为 `YYYY-MM-DD`。点选结果，核对正文、发布者与时间。
5. 筛选评论；「不包括全部回复消息」可排除回复。「手动筛选（大窗口）」可逐条勾选，选择结果自动保存。
6. 点击绿色「导出筛选的评论」保存 JSON；另有完整朋友圈及搜索结果导出。以后可用「打开 JSON 备份」离线查看。

小窗口可通过右侧滚动条查看所有步骤，导出按钮固定在底部。修改评论筛选条件后，主界面点「筛选评论」，弹窗点「应用筛选」或按回车。

## 导出与限制

- 保存朋友圈和评论的文本、发布者、发布时间及回复关系；显示北京时间，保留原始 Unix 时间，缺失时间为 `null`。
- 筛选导出仅包含符合条件且勾选的评论，不携带包含全部评论的原始 XML；完整导出保留原始 XML。
- **只能读取本机已缓存的内容，不能保证历史评论完整。** 在微信打开目标内容后重新刷新；工具会提示已知的计数差异。
- 不发送消息、不点赞、不评论，也不修改微信数据库或进程；仅读取 `sns.db` 和 `contact.db`。不下载图片、视频或聊天记录。
- 工具缓存、日志与筛选记录位于 `%LOCALAPPDATA%\WechatMomentExtractor`，可用环境变量 `MOMENT_EXTRACTOR_DATA_DIR` 更改。解密缓存、密钥及导出文件请勿公开。

若读取失败，先确认微信已登录、目录正确、微信与工具权限一致。反馈问题请到 [Issues](https://github.com/Tort0iseZzz/WechatMomentExtractor/issues)，注明 Windows、微信及工具版本，隐去日志中的私人信息。

## 源码运行与打包

安装带 Tk 的 **64 位 Python 3.12+**，克隆本仓库并在仓库目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[build]"
.\.venv\Scripts\python.exe run.py
```

无需微信的演示：给最后一条命令加 `--demo`；打开已有备份：加 `--open "backup.json"`。

执行 `powershell -ExecutionPolicy Bypass -File build.ps1`，先运行离线测试，再生成 `dist\WechatMomentExtractor.exe`。单独测试：`.\.venv\Scripts\python.exe -m unittest discover -s tests -v`。

## 许可与致谢

[Apache-2.0](LICENSE)。数据库解密及只读内存扫描改编自 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)，保留 [NOTICE](NOTICE)；本项目仅发布只读朋友圈备份所需代码。
