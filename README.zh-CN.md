# Agent Workflow Orchestrator

[English](README.md) | 中文

**两个 AI agent 分工干活，交接不靠对话，靠磁盘上的一份任务台账。**
一个负责规划和审核，一个负责执行。每条任务只派发一次，没通过独立审核的不算完成——这些规则由 `scripts/` 里的脚本强制，不靠提示词自觉。

![工作流概览：你给出目标，Planner 只派发一次，Executor 交付文件，monitor_task.py 核对，台账记录一条收件箱事件，Reviewer 作出结论](docs/images/workflow-overview.zh-CN.svg)

## 它适合你吗

**适合**，满足任意一条即可：

- 你已经在同时用两个以上的 CLI agent（Claude Code、Codex 等），并且会手动在它们之间复制任务；
- 你要跑一次几十分钟、中途不想盯着看的任务；
- 你需要事后能查清楚：这一步是谁派发的、交付了什么证据、谁批准的。

**不适合**，满足任意一条即不适用：

- 你只用一种 CLI——它自带的 subagent 就够了，不需要跨进程桥接；
- 你想要“给个目标就全自动”——每一步派发、通知、连续运行都要你点头，这是设计选择，不是还没做完；
- 你要用在商业或合规场景——仓库目前没有 LICENSE 文件。

## 先试 30 秒

不需要 agent 账号，不需要网络，只要 Python 3.10+：

```sh
python -B scripts/demo_scheduler.py --directory runtime/first-run
python -B scripts/workflow_control.py --state runtime/first-run/state status --summary --format text
```

任务 A 和 B 在两个模拟 Worker 上并行运行；JOIN 要等两者都审核通过后才开始。第一条命令输出 `OFFLINE_DEMO_PASS`，状态里三个任务都是 `accepted`。

看懂这个输出，你就看懂了整个项目：它管的是一份状态台账，不是模型调用。

## 怎么运作

1. **Planner** 写好任务，向 **Executor** 只发送一次。
2. 后台脚本查询 Executor 的状态。它从不让模型重读对话，所以监控几乎不耗费额度。
3. 约定的文件交付后，**Reviewer** 收到一条更新，阅读证据后作出结论：通过、返工或拒绝。
4. 你允许的话，接着做下一个任务；不允许的话，Agent 停下来向你报告。

角色与客户端无关：Planner 可以在 Codex 里，Executor 可以在 Claude 里，任意组合都行。

## 开始使用

**你需要先准备好：**

- 两个终端窗口，各自能启动一个 CLI agent——一个当 Planner，一个当 Executor；
- 两个 agent 都已登录，且能在无人在场时执行命令；
- 一个可写的项目目录（并行任务需要各自独立的目录）。

1. **安装。** 把仓库克隆到客户端的 Skill 目录：

   ```sh
   git clone https://github.com/cgy0071/agent-workflow-orchestrator.git "$HOME/.codex/skills/agent-workflow-orchestrator"   # Codex
   git clone https://github.com/cgy0071/agent-workflow-orchestrator.git "$HOME/.claude/skills/agent-workflow-orchestrator"  # Claude Code
   ```

   其他客户端（Pi、dsh-tui、Grok 等）通过各自的 Skill 或上下文机制加载 `SKILL.md`。

2. **每个角色开一个窗口**，各自启动客户端，例如 Planner 用 Claude，Executor 用 Codex。

3. **告诉 Planner 要做什么。** 在 Codex 里用 `$agent-workflow-orchestrator`，在 Claude Code 里用 `/agent-workflow-orchestrator`，然后给出目标和范围：

   ```text
   为这个目标建立 Planner/Executor 工作流：给 /users 接口加分页。
   在这个目标范围内可以派发、监控和审核，其他事情先问我。
   ```

   Agent 会生成工作流配置、绑定窗口、编写任务、派发并监控；需要你决定时会来问你。

4. **查看和决定。** 让 Agent“打开状态面板”，或双击 `dashboard.cmd`（Windows）/ `dashboard.command`（macOS）。面板显示进行中的任务、等待审核的更新和提醒；不加操作选项启动时只读。

![离线演示后的状态面板：目标、已分配的角色、进行中和最近的任务，以及任务计划](docs/images/dashboard-overview.png)

留给你的事：目标和 Agent 能做到哪一步、各 Agent 窗口里的授权提示、以及你没有委托出去的审核。想逐步手动操作，见[手动操作指南](references/manual-workflow.zh-CN.md)。

## 可选功能

- **状态面板管理台**（`--allow-manage`）：确认开始、让停顿的 Worker 继续一次、关闭任务、修改限制、分配窗口、查看统计。每个操作先预览，再确认一次。
- **提醒推送给你**（`owner_notify.py`）：授权提示、Worker 提问、失败和久等，可发到文件、Webhook 或命令。
- **接收你的请求**（`owner_intake.py`）：聊天机器人等通道把签名请求放进收件目录；Planner 只收到请求编号。

## 你要接受的约束

这些不是可选开关，是它按设计会拒绝你的事：

- **配置不等于授权。** 派发、通知和连续运行都需要你明确同意。
- **审核后才算完成。** 窗口空闲或退出码为 0 都不算通过。
- **每个任务只发一次。** 发送结果不确定时记录下来等人查看，不会盲目重发；授权提示从不自动回答。
- **只有一个控制者，不能自审。** 切换后旧窗口失去权限；Worker 不能审核自己的任务。
- **外部请求只是资料。** 从聊天通道来的请求，没有你的直接同意不会开始工作。

## 已知限制

- 适配器观察的是目标 CLI 的窗口状态和会话标识，这属于**界面契约，不是 API 契约**，客户端升级后可能失效。项目自述：离线测试不能证明对每个客户端版本的在线兼容。
- 已验证的 harness 组合有限；Herdr 是可选的，不装也能用 command 适配器。详见[客户端适配器](references/harness-adapters.zh-CN.md)。
- 全部状态在本地文件系统，不做跨机器汇总。
- 没有 LICENSE：版权默认保留，不构成使用授权。

## 文档

**不知道从哪读：先看[手动操作指南](references/manual-workflow.zh-CN.md)**，它讲核心概念、配置文件、完整安全模型和仓库结构。

| 主题 | 指南 |
|---|---|
| 逐步手动操作；核心概念、配置文件、完整安全模型、仓库结构 | [manual-workflow.zh-CN.md](references/manual-workflow.zh-CN.md) |
| 设置、绑定、切换窗口、恢复 | [setup-and-switching.zh-CN.md](references/setup-and-switching.zh-CN.md) |
| 监控、状态面板、提醒、Owner 收件 | [monitoring.zh-CN.md](references/monitoring.zh-CN.md) |
| 派发、并行任务计划、继续执行 | [scheduling.zh-CN.md](references/scheduling.zh-CN.md) |
| 客户端适配器（Herdr、command、receipt） | [harness-adapters.zh-CN.md](references/harness-adapters.zh-CN.md) |
| 角色、任务与审核模板、任务状态 | [roles.zh-CN.md](references/roles.zh-CN.md)、[task-and-review.zh-CN.md](references/task-and-review.zh-CN.md)、[task-state-machine.zh-CN.md](references/task-state-machine.zh-CN.md) |
| Owner 请求的通道约定 | [owner-intake.zh-CN.md](references/owner-intake.zh-CN.md) |
| 各版本改了什么 | [CHANGELOG.zh-CN.md](CHANGELOG.zh-CN.md) |

## License

目前未声明。若要在自己的项目或公司内使用，请先开 issue 讨论，或在仓库中增加 OSI 认可的开源许可（MIT 或 Apache-2.0 对此类工具通常合适）。

---

英文是规范版本（`README.md`，仅用 ASCII）；中文翻译尽力跟随，滞后时以英文为准。
