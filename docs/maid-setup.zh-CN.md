# 第一次配置 MAID 秘书处

以下命令从包含 `apps/maid/` 的源码根目录运行。例子中，学生项目负责产出，材料项目只提供一份已发布的校准记录。请将示例路径换成本机目录。两份 Git 仓库各自独立，会话目录放在两者之外。

## 选择秘书和窗口

秘书默认使用 agy。要改为 Claude，在 chat、request 或 direct 后传入 `--secretary examples/maid-secretary-claude.json`。也可传入自己的本地 JSON 配置：turn 是命令参数数组，从标准输入读取提问并将回复写到标准输出；direct 是在当前终端打开原生界面的命令。命令可用 {thread} 和 {model} 占位符。需要续聊时，适配器应按 thread 保存和恢复会话。Claude 示例每次启动新会话。只使用自己信任的命令配置。

安装并登录 agy 后，用第一条命令查看当前账户能选的模型，把模型 ID 填到第二条命令。在 Herdr 窗格、Windows Terminal 或 PowerShell 中均可启动。先发送一条消息，再输入 `exit`。第三条命令在当前终端直接打开同一段 Gemini 对话。

```powershell
agy models
python apps/maid/maid.py --thread D:/MAID/threads/student-01 chat --model gemini-3.8-flash-low
python apps/maid/maid.py direct --thread D:/MAID/threads/student-01
```

已有 Claude、Codex 等交互窗口可继续直接输入。自动执行和审核使用一次性进程，进展从 `status` 查询。

## 指定项目、负责人和执行工程师

复制[项目目录示例](../examples/maid-directory.json)为本地 `directory.json`。`projects.student.root` 填学生项目的 Git 根目录，`manager` 填用于路由和回执的负责人标识。填写负责人名称不会自动创建窗口或授予权限。

按[任务配置完整示例](../references/project-entry.md)创建 `spec.json`。其中 `repository` 应与 student 的根目录一致，`work_root` 与下文的 thread 一致；使用新的会话目录。`worker` 指定执行工程师的 Harness 和可执行文件绝对路径，`reviewer` 指定审核进程。当前这条自动执行路径两者都使用已登录的 Codex。用 `allowed_paths` 限定可改文件，`verify_argv` 指定验收命令。项目内角色授权另见[项目命令](project-commands.md)。

## 让学生项目使用材料项目的一份记录

在材料项目中选定已发布文档及其修订，并由来源方提供仅可读该文档的凭据文件。将文档 ID、修订 SHA-256 和凭据文件路径填入示例的 `exports.calibration`。例如把自己的本科生工程对应到 student，把供材工程对应到 materials。`source` 和 `recipient` 指定此次共享方向；此导出只供读取。反向共享需要单独授权。凭据文件保存在接收方仓库之外。

```powershell
python apps/maid/maid.py --thread D:/MAID/threads/student-01 --directory directory.json --project student --material calibration submit --spec spec.json
python apps/maid/maid.py --thread D:/MAID/threads/student-01 start --confirm-execute
python apps/maid/maid.py --thread D:/MAID/threads/student-01 status
```

提交后核对返回的项目、负责人和文档修订，再启动。重开后用同一 thread 查询 `review` 和 `accepted_evidence_id`。需要授权材料时使用上述路由提交命令；当前自然语言 `request` 入口仅接受项目任务配置。

## 指定知识库位置并查询

`--project-root` 是项目目录，`--state` 指向该项目已初始化的 AWO 状态目录。使用宿主知识插件时，管理员在该状态目录下的 `kb-host.json` 中，按已注册项目 ID 配置 `projects.<项目ID>.vault`（记录库目录）和 `source_root`（原文证据的根目录）。例如可分别使用 `D:/MAID/kb/materials` 和 `D:/Research/materials/sources`。这些字段是已有宿主配置的一部分；插件还需启用，并给任务绑定修订、读操作和来源授权。仅把 PDF 放到目录里不会完成登记。

```powershell
python scripts/awo_project.py inventory --state D:/MAID/state/materials --project-root D:/Research/materials --task T-A --token-file D:/MAID/credentials/task-reader.token
```

把 T-A 和凭据路径换成实际获授权的任务和读取者。要按关键词检索已发布文档，使用[文档检索示例](project-commands.md)中的 `retrieve`；它使用文档读取凭据。
