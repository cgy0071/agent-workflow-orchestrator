# Agent Workflow Orchestrator

AWO v1.1.1 与 MAID v0.2.1 配对。秘书可读取明确授权的文档；派工前准备任务时再次核验授权。

[English](README.md) | 中文

让一个 AI 负责规划和验收，另一个 AI 负责动手干活。你只需要定好目标、划定允许修改的范围；任务通过本地台账一步步交接，**未经独立审核的改动，一律不算完成**。

## 从哪里开始

- **想让一位“项目秘书”帮你协调多个项目？** 建议从 [MAID 秘书处](https://github.com/cgy0071/MAID#readme) 开始：选好秘书、绑定你的 Git 仓库、审阅草稿后再派工。（注意：MAID 需要匹配特定版本的 AWO，按 MAID 说明安装即可。）
- **想自己直接掌控规划、派工和审核？** 留在当前仓库。花 1 分钟跑通下面的离线演示，熟悉流程后就可以把你自己的项目交给 Planner 了。AWO 可以完全独立于 MAID 运行。

## 第一次用 Windows 版？从这里下载，无需 Git

**[下载 Windows 64 位便携候选版](https://github.com/cgy0071/agent-workflow-orchestrator/releases/download/v1.0.0-rc.1/AWO-Windows-x64-rc-check.zip)** ｜ [版本说明和校验值](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/v1.0.0-rc.1)

1. 下载 ZIP，右键选择 **全部提取**。
2. 打开解压后的文件夹，双击 **开始.cmd**。离线示例自带 Python，无需账号或 API Key。
3. 看到 **A、B、JOIN 三项已验收** 就成功了。关掉窗口，再次双击可查看上次结果。

**[安装 Windows 知识库插件（0.4.1 正式版）](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/kb-v0.4.1)**：用 Python 3.10+ 或 Anaconda 读取一条示例知识记录。

**[试用 Windows 宿主的任务知识库](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/kb-host-v0.1.0-preview.1)** - 从预览发布页下载 AWO-Windows-KB-Host ZIP 附件（请勿选择自动生成的 Source code），解压后按页内三条命令运行。成功时会看到项目、修订、引文和 `AWO KB HOST PREVIEW PASS`。 [查看框架后续交付设计](docs/native-framework-delivery.zh-CN.md)

**[下载 Windows 项目命令正式版 0.1.0](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/project-cli-v0.1.0)** — 从 Assets 下载 `AWO-Windows-Project-CLI-0.1.0.zip`，解压后在该目录用 Python 3.10+ 运行 `python -B try_project.py`。

[当前源码版的项目命令](docs/project-commands.zh-CN.md)

## 1 分钟本地体验（离线演示，不消耗额度）

先确保本机装有 **Git** 和 **Python 3.10+**。这个演示使用本地模拟工人，不需要任何 AI 账号或网络。根据你的终端选择对应命令（下面默认安装到 Codex 的 Skill 目录；如果你用 Claude Code 做 Planner，把 `.codex` 换成 `.claude` 即可；如果已经克隆过 AWO，直接进入仓库目录跳过克隆）：

Windows PowerShell：
```powershell
$awo = "$HOME/.codex/skills/agent-workflow-orchestrator"
git clone https://github.com/cgy0071/agent-workflow-orchestrator.git "$awo"
Set-Location "$awo"
python -B scripts/demo_scheduler.py --directory runtime/first-run
python -B scripts/workflow_control.py --state runtime/first-run/state status --summary --format text
```

macOS / Linux：
```sh
awo="$HOME/.codex/skills/agent-workflow-orchestrator"
git clone https://github.com/cgy0071/agent-workflow-orchestrator.git "$awo"
cd "$awo"
python3 -B scripts/demo_scheduler.py --directory runtime/first-run
python3 -B scripts/workflow_control.py --state runtime/first-run/state status --summary --format text
```

看到 `OFFLINE_DEMO_PASS` 就说明演示跑通了，状态输出里 `A`、`B`、`JOIN` 三个任务都会显示为 `accepted`。之后随时可以用同一个 `--state` 路径重查状态；如果想重新演练一遍，换一个新的 `--directory` 目录名即可。

## 开始管理你自己的项目

1. **打开两个会话**：在你的项目目录下启动一个 **Planner** 客户端（用来规划和审查），再另开一个 **Executor** 会话（用来写代码和跑测试）。两者可以用不同的 AI 工具（例如 Claude 配 Codex）。
2. **加载 AWO**：在 Codex 中输入 `$agent-workflow-orchestrator`，或在 Claude Code 中输入 `/agent-workflow-orchestrator`。
3. **下达你的第一个目标**：告诉 Planner 要做什么、允许改哪些文件、以及怎么算完成。例如：

```text
给本仓库的 /users 接口加分页，只能修改该接口和相关测试。
可以派发一项任务、监控并审核证据；审核后停下，不要推送。
```

4. **过程中的配合**：
   - 先核对 Planner 给出的计划和窗口绑定是否正确；
   - 如果执行者窗口弹出了权限确认提示，请在**执行者自己的窗口**里手动确认；
   - 想看全局进度时，可以让 Planner 打开只读状态面板（也可以直接双击根目录下的 `dashboard.cmd` / `dashboard.command`）；
   - 开启下一项任务前，先让 Planner 汇报当前任务的状态和审核结论。

## 支持的功能

Windows 便携版可直接运行离线示例。接入真实客户端需单独配置账号和权限。要使用项目知识库等可选功能，请在需要它的项目中按对应指南启用。

| 功能 | 可以做什么 | 使用方式 |
| --- | --- | --- |
| 离线体验 | 解压后双击运行；两个模拟执行者完成 A、B，再验收 JOIN，重开可查看结果。 | [直接体验](references/manual-workflow.zh-CN.md) |
| 角色与独立审核 | 配置规划者、执行者和审核者；核对交付证据，决定通过、返工或拒绝。 | [配置指南](references/task-and-review.zh-CN.md) |
| 任务调度 | 声明依赖、并行和串行步骤；单次派发，处理停顿、继续和交接。 | [配置指南](references/scheduling.zh-CN.md) |
| 执行客户端 | 绑定、切换和恢复执行窗口；按客户端版本核验可用操作，支持命令、回执和可选 Herdr 接入。 | [配置指南](references/harness-adapters.zh-CN.md) |
| 运行面板与通知 | 查看进度和异常；启用管理模式可预览并确认操作；授权后发送文件或 Webhook 提醒。 | [配置指南](references/monitoring.zh-CN.md) |
| 外部请求与远程操作 | 接收签名请求供 Planner 审查；可配置远程终端从手机查看工作。 | [配置指南](references/owner-intake.zh-CN.md) · [补充指南](references/remote-access.zh-CN.md) |
| 项目与代码交付 | 在已有仓库准备任务，用隔离的 Git worktree 执行，独立审核后通过本地合并队列落地或恢复。 | [配置指南](references/project-entry.zh-CN.md) · [补充指南](references/worktree-code-loop.zh-CN.md) |
| 服务与身份 | 迁移前预演、冻结或恢复写入；登记 Agent，核验能力与心跳，撤销身份。 | [需在项目中启用](references/awo-service.zh-CN.md) · [补充指南](references/awo-registry.zh-CN.md) |
| 预算与资源 | 按任务或项目预留并核对用量；按优先级、协作槽位和资源占用决定是否接纳任务。 | [需在项目中启用](references/awo-budget.zh-CN.md) · [补充指南](references/awo-scheduler.zh-CN.md) |
| 工作区与合并队列 | 为任务分配隔离目录或 worktree，固定基线，将已审核变更按票据落地。 | [需在项目中启用](references/awo-workspaces.zh-CN.md) · [补充指南](references/awo-merge.zh-CN.md) |
| 消息与多 Agent 协作 | 按任务收发消息、回复和确认；将任务分给多个执行者，汇总结果，限制预算和返工次数。 | [需在项目中启用](references/awo-messaging.zh-CN.md) · [补充指南](references/awo-collaboration.zh-CN.md) |
| 项目知识与文档 | 按权限列出条目，按 ID 读取正文、来源、版本和审核历史；使用前检查可用状态。 | [安装插件](https://github.com/cgy0071/agent-workflow-orchestrator/releases/tag/kb-v0.4.1) / [服务 API 接入](references/awo-knowledge.zh-CN.md) |
| 应用接入 | 通过 MCP 标准输入输出、HTTP 或 WebSocket 调用已启用的服务工具。 | [需在项目中启用](references/awo-gateway.zh-CN.md) |
| 监督与审计 | 查看本地监督建议、接收配对草稿，按项目查看运行记录并导出审计回执。 | [需在项目中启用](references/awo-supervision.zh-CN.md) · [补充指南](references/awo-observability.zh-CN.md) |

## 它是怎么运作的

1. **Planner（规划者）** 把需求写成清晰的任务书，只向 **Executor（执行者）** 发送一次。
2. **后台监控脚本** 定时检查状态。它只看本地回执，从不让模型反复重读聊天记录，因此几乎不消耗 Token。
3. **Executor** 交出约定的结果和证据文件后，**Reviewer（审核者）** 收到通知，对照证据给出结论：**通过（accept）**、**返工（repair）** 或 **拒绝（reject）**。
4. 只有得到你的授权，才会继续下一项任务；否则系统停下来向你汇报。

> 角色和客户端工具是解耦的：谁用 Codex、谁用 Claude 都可以自由组合。所有“只发一次、先审后结、同时只有一个控制者”的硬规则，都由 `scripts/` 里的脚本强制守住，而不是靠提示词碰运气。不同客户端的支持情况以实测为准，详见[适配器说明](references/harness-adapters.zh-CN.md)。

![工作流概览：你给出目标，Planner 只派发一次，Executor 交付文件，monitor_task.py 核对，台账记录一条收件箱事件，Reviewer 作出结论](docs/images/workflow-overview.zh-CN.svg)

## 进阶：第一次真实模型联动测试

如果你想先用真实模型跑一遍端到端联调（需要 Python 3.10+ 和已登录的 Codex CLI），可以在仓库根目录用每次全新的目录运行：

```powershell
Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
python -B scripts/validate_v2_native_loop.py --directory runtime/repro/native-oneshot-1 --harness codex --mode one-shot --timeout 240
```

看到 `V2_NATIVE_LOOP_PASS` 且退出码为 0 即表示通过。这会消耗 3 轮真实模型调用，用来验证当前本机 CLI、单次会话、依赖卡控和独立审核是否正常（它测的是目录产物闭环，不代表 Herdr 分屏或 Git worktree 代码任务）。更多真实运行方式见[第一次真实运行指南](references/first-real-run.zh-CN.md)和 [worktree 代码闭环](references/worktree-code-loop.zh-CN.md)。

## 安全底线（5 条铁律）

- **配置不等于授权**：写进配置文件不算数，派工、发通知、连续跑都需要你明确点头。
- **审核通过才算完成**：窗口变空闲、命令退出码为 0 都不算完成；审核通过也不等于自动合并或推送。
- **每项任务只派发一次**：如果发送结果不确定，系统只记录异常等你排查，绝不盲目重发，也绝不替你点掉授权弹窗。
- **同一时间只有一个控制者，且不能自审**：切换控制窗口后旧窗口立即失效；干活的 Executor 绝对不能自己给自己盖章验收。
- **外部消息只是参考资料**：从聊天机器人等外部渠道进来的请求，没有你的直接确认，绝不会自动触发派工。

> 每次测试只证明它实际跑过的那部分能力（例如目录产物测试不覆盖 Git worktree；优先级和预算账本用于准入控制，不会强行掐断正在进行中的模型调用）。

## 可选增强功能

- **管理台模式**（`dashboard.py --allow-manage`）：可在网页端预览并单次确认“启动、让卡住的工人继续一次、关闭任务、调整上限、分配窗口”等操作。
- **主动提醒推送**（`owner_notify.py`）：遇到授权弹窗、工人提问、报错或久等时，自动通过文件、Webhook 或命令提醒你。
- **外部需求收件箱**（`owner_intake.py`）：让聊天机器人等把带签名的需求放进收件目录，Planner 只拿到编号，等你亲自拍板后再派工。

## 文档导航

### 新手必读（建议先看这几篇）

| 想了解什么 | 去看哪篇 |
|---|---|
| 面向已有仓库的项目入口：准备、执行、独立审核、本地落地、恢复 | [project-entry.zh-CN.md](references/project-entry.zh-CN.md) |
| 怎么绑定窗口、切换控制者、异常恢复 | [setup-and-switching.zh-CN.md](references/setup-and-switching.zh-CN.md) |
| 怎么跑通第一次真实模型联动（含 Herdr 选项） | [first-real-run.zh-CN.md](references/first-real-run.zh-CN.md) |
| 怎么用后台监控、状态面板、提醒与收件箱 | [monitoring.zh-CN.md](references/monitoring.zh-CN.md) |
| 角色分工、任务书/审核模板、任务状态流转 | [roles.zh-CN.md](references/roles.zh-CN.md)、[task-and-review.zh-CN.md](references/task-and-review.zh-CN.md)、[task-state-machine.zh-CN.md](references/task-state-machine.zh-CN.md) |

### 进阶与 V2 子系统参考（按需查阅）

| 主题 | 指南 |
|---|---|
| 逐步手动操作；核心概念、配置文件、完整安全模型、仓库结构 | [manual-workflow.zh-CN.md](references/manual-workflow.zh-CN.md) |
| 派发、并行任务计划、继续执行 | [scheduling.zh-CN.md](references/scheduling.zh-CN.md) |
| 客户端适配器（Herdr、command、receipt） | [harness-adapters.zh-CN.md](references/harness-adapters.zh-CN.md) |
| 可选：用手机远程使用命令行（Windows，经 Tailscale 的 SSH） | [remote-access.zh-CN.md](references/remote-access.zh-CN.md) |
| Owner 请求的通道约定 | [owner-intake.zh-CN.md](references/owner-intake.zh-CN.md) |
| 可选 V2 服务边界：增量 schema、dry-run 迁移、freeze/epoch | [awo-service.zh-CN.md](references/awo-service.zh-CN.md) |
| Agent 注册表：enrollment grant、能力证据、心跳、撤销 | [awo-registry.zh-CN.md](references/awo-registry.zh-CN.md) |
| 预算账本：分 scope 预留、用量/暴露、authoritative close | [awo-budget.zh-CN.md](references/awo-budget.zh-CN.md) |
| 优先级调度器：排队顺序、协作槽位、占用、准入闸门 | [awo-scheduler.zh-CN.md](references/awo-scheduler.zh-CN.md) |
| Assignment 工作区：隔离根目录、Git worktree、冻结源 | [awo-workspaces.zh-CN.md](references/awo-workspaces.zh-CN.md) |
| 持久化 Agent 消息：task/run 范围收件箱、回复、确认 | [awo-messaging.zh-CN.md](references/awo-messaging.zh-CN.md) |
| 本地知识与文档：按 scope 登记和检索 | [awo-knowledge.zh-CN.md](references/awo-knowledge.zh-CN.md) |
| 网关与应用传输：处理器、路由、重放规则 | [awo-gateway.zh-CN.md](references/awo-gateway.zh-CN.md) |
| 协作运行时：Fanout、Ensemble、共享预算、有界返工 | [awo-collaboration.zh-CN.md](references/awo-collaboration.zh-CN.md) |
| 合并队列：已接受基线、物化闸门、按 ticket 绑定的 intent | [awo-merge.zh-CN.md](references/awo-merge.zh-CN.md) |
| 端到端 Git worktree 代码闭环：真实 worktree 执行、独立审核、本地 Merge Queue、物化 | [worktree-code-loop.zh-CN.md](references/worktree-code-loop.zh-CN.md) |
| 本地监督与配对外部草稿（本地标签协议，非认证 A2A） | [awo-supervision.zh-CN.md](references/awo-supervision.zh-CN.md) |
| 只读运行视图与隔离审计回执 | [awo-observability.zh-CN.md](references/awo-observability.zh-CN.md) |
| 各版本更新记录 | [CHANGELOG.zh-CN.md](CHANGELOG.zh-CN.md) |

---

上方链接提供公开的 Windows 便携编排器候选版。11 月 19 日的 Native Agent Framework 归档是另一份内部测试产物。

应用可通过 `python -B scripts/awo_app.py capabilities` 查询协议 1。准备、启动、查询与恢复任务的 JSON 接口见[应用协议](docs/application-protocol.md)。

## 许可证

AWO 采用 [MIT 许可证](LICENSE)。

[秘书汇报规范](docs/secretary-reporting.zh-CN.md) · [Windows 测试与发布排错](docs/troubleshooting-windows.zh-CN.md)
