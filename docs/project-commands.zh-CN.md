# 在 Windows 项目中使用框架命令

**[下载 Windows 项目命令正式版 0.1.0](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/project-cli-v0.1.0)** — 从 Assets 下载 `AWO-Windows-Project-CLI-0.1.0.zip`，解压后在该目录用 Python 3.10+ 运行 `python -B try_project.py`。 两种检索方式都应输出 `201 HV`，随后显示 `WINDOWS PROJECT COMMANDS PASS`。示例不需要现有项目或令牌。

从包含 README 的源码目录运行 `python scripts/awo_project.py --help`。这些命令随当前源码提供；已发布的宿主预览 ZIP 仍使用它发布页上的演示命令。

| 我想做什么 | 从哪里开始 |
|---|---|
| 给已登记的 Agent 授予、撤销或检查项目角色 | `python scripts/awo_project.py role --help` |
| 在已发布的项目文档中检索，取得引用和修订 | `python scripts/awo_project.py retrieve --help` |
| 读取已授权任务的知识库清单 | `python scripts/awo_project.py inventory --help` |
| 准备、执行、查看或恢复一个项目任务 | `python scripts/awo_project.py task --help`；见[项目任务指南](../references/project-entry.zh-CN.md) |
| 检查本机执行端 | `python scripts/awo_project.py probe codex` 做本地协议握手；`probe claude` 向已配置的 DeepSeek Flash 路由发送一次计费测试 |

**检索示例。** 已有 AWO 项目、已发布文档及读取凭据时，将下面的变量设置为自己的状态目录、项目根目录、凭据文件和文档 ID：

```powershell
python scripts/awo_project.py retrieve --state "$state" --project-root "$project" --token-file "$readerToken" --doc "$doc" --query "Hardness" --backend sqlite --skill examples/project-domains/materials.json
```

输出包含文档 ID、修订和内容摘要。将 `sqlite` 换成 `lexical` 可复用同一请求和权限。领域文件是声明式 JSON；材料示例读取 Assay、Hardness 两个字段。仅查文本时省略 `--skill`。项目文档检索与任务知识库清单是不同的读取入口，使用对应的凭据。

`--project-root` 必须是已存在目录的绝对路径；`--state` 指向已初始化的 AWO 状态目录。令牌文件支持绝对或相对路径；令牌和 Skill JSON 均支持带或不带 BOM 的 UTF-8。授予或撤销角色时，需要提供控制器 epoch 和 `role check` 返回的修订号，详见 `role grant --help` 或 `role revoke --help`。任务知识清单还需要安装可选知识库插件。
