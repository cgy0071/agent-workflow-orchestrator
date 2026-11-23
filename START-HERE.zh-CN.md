# MAID 秘书处 Windows 预览版 0.1.0-preview.1

解压完整 ZIP，在包含 apps 和 scripts 的目录打开 PowerShell。需要 Python 3.10+、Git 和已登录的 Codex CLI。默认秘书使用已登录的 agy。可以用 Anaconda 提示符中的 python。以下示例会在 runtime/maid-demo 创建一个独立 Git 项目。

```powershell
python apps/maid/setup_demo.py
python apps/maid/maid.py request --thread runtime/maid-demo/thread --profile runtime/maid-demo/profile.json "修复 total(items)，正确累加正负数，空输入返回零。只修改 app.py。"
```

看到新草稿后核对目标和文件范围，再执行：

```powershell
python apps/maid/maid.py --thread runtime/maid-demo/thread start --confirm-execute
python apps/maid/maid.py --thread runtime/maid-demo/thread status
```

关闭终端，重新从解压目录执行 status，检查 landed、accept 和同一个 accepted_evidence_id。记录 Windows 和 Python 版本、首次卡点及证据编号。真实调用使用当前账户的额度。

要直接跟刚才的秘书对话，在 Herdr 窗格或喜欢的终端运行：

```powershell
python apps/maid/maid.py direct --thread runtime/maid-demo/thread
```

要换秘书，可在 request 或 chat 命令后添加 --secretary examples/maid-secretary-claude.json。Claude 示例使用已登录的 claude 命令，每次开启新会话。项目、工程师和知识库设置见 docs/maid-setup.zh-CN.md。