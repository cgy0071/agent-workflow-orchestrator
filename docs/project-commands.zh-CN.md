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

## 跨源码版本查询项目

在源码目录中，使用项目级服务凭据，以及控制者已用 `project.bind_root` 绑定的资料根目录：

```powershell
python scripts/awo_project.py domain --state "$state" --project-root "$project" --token-file "$readerToken"
```

命令返回账本项目 ID、规范化根路径的摘要及根目录绑定版本。提交代码或切换分支后，项目身份保持稳定。重开会话时，用 `--expected-project-id` 传入此前保存的 ID。账本身份不同、状态目录是复制品、资料根不匹配或凭据过期，都会使查询失败。路径摘要只标识位置，文件内容仍需按证据校验。

同一秘书服务两个项目时，为每个项目分别配置状态目录、绑定根、凭据和预期 ID。现有项目任务入口按次创建运行状态，仅凭运行 ID 无法归集长期项目的多个任务。本命令读取已有项目身份，跨项目材料仍需单独授权。

后续集成分三步。先将任务交给目标项目的控制者，派发前协调共享目录及 Harness 的占用。然后由该控制者预留和核销任务预算。最后验证重启恢复，按原项目 ID 查询对应的 assignment、评审和证据，再决定是否继续派工。
