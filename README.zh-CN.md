# Agent Workflow Orchestrator (AWO)

[English](README.md) | 中文

让多个 AI 一起写代码或做科研时，最怕三件事：没经同意就乱改文件、干活的 AI 自己说“完成了”其实没测过、任务一多状态全乱。**AWO 是一套本地多智能体工作流编排与验收引擎**：让一个 AI 负责规划，另一个负责执行，再由独立的审核者对照证据盖章验收——**所有规则由本地脚本和台账强制守住，而不是靠提示词碰运气**。

> AWO 可以完全独立使用，也可以搭配 [MAID](https://github.com/cgy0071/MAID) 这类项目秘书一起用。

## 核心工作流

![从请求到独立审核](docs/images/workflow-overview.zh-CN.svg)

你只需定好目标和允许修改的范围。**Planner（规划者）**把需求拆成清晰的任务书，向 **Executor（执行者）**只派发一次；后台脚本低成本盯住交付进度；交付完成后，**Reviewer（审核者）**独立检查测试与证据，给出**通过、返工或拒绝**的结论。互不依赖的任务可以并行，有先后依赖的步骤必须等前置审核通过才会启动。

## 架构与可选模块

![客户端、知识授权、状态面板和通知分别接在哪里](docs/images/integrations-overview.zh-CN.svg)

AWO 的各个模块围绕核心台账解耦设计，你可以按项目需要逐步启用：

### 1. 多角色编排与独立审核
把规划、执行和审核拆成独立职责，支持单任务、串行依赖和并行任务图（DAG），从机制上杜绝“执行者自己给自己验收”。在 Codex 中输入 `$agent-workflow-orchestrator`（或在 Claude Code 中输入 `/agent-workflow-orchestrator`），用自然语言给出目标和范围即可启动：

```text
给本仓库的 /users 接口加分页，只能修改该接口和相关测试。
可以派发一项任务、监控并审核证据；审核后停下，不要推送。
```

### 2. 隔离工作区与本地合并队列
每项代码任务在独立的 Git worktree 中运行，多任务并行互不踩踏。审核通过的改动进入本地合并队列，严格区分**“已验收”**和**“已合并”**，遇到冲突或目标分支变化会明确拦截，不会悄悄覆盖你的工作区。

### 3. 低成本后台监控与可视化状态面板
后台监控脚本只读取本地回执，不让大模型反复重读聊天记录，几乎不消耗额度。想看全局进度时，直接让 Planner 打开状态面板，或双击根目录下的 `dashboard.cmd`（Windows）/ `dashboard.command`（macOS）；默认只读，按需可加 `--allow-manage` 开启带二次确认的管理台。

### 4. 多客户端适配（角色与工具解耦）
Planner、Executor 和 Reviewer 可以自由混搭不同客户端（如 Codex、Claude、Pi、OMP 或可选 Herdr 分屏），不用为了协作被迫全换成同一个工具。每个客户端的能力按实际版本核验，详见[适配器说明](references/harness-adapters.zh-CN.md)。

### 5. 项目知识库、免 ID 主题检索与术语表约束
支持只读提取本地 Markdown / Obsidian 笔记和文本型 PDF（保留原位置与页码，绝不改动源文件）；无需记住文档编号，直接按主题模糊搜索获准资料。支持中英双语扩展、项目级专业术语表与易混词拦截，也支持接入兼容 `/v1/embeddings` 的本地或在线向量服务；**权限过滤永远先于排序，撤权后立即拒读**：

```powershell
python -B scripts/awo_project.py materials --state <状态目录> --project-root <项目目录> --token-file <凭据> --query "想了解推理吞吐量" --top-k 3 --strict-glossary
```

### 6. 外部收件箱与主动提醒
外部聊天渠道可通过 `owner_intake.py` 把签名请求送入收件箱供 Planner 审阅；当执行者遇到权限弹窗、卡住或交付待审核时，可通过 `owner_notify.py` 按授权主动向文件、Webhook 或命令推送提醒。

## 五条不可逾越的安全底线

1. **配置不等于授权**：写进配置文件不会自动派工、发通知或连续运行，必须由你明确同意。
2. **交付不等于验收，验收不等于合并**：窗口空闲或退出码为 0 都不是完成，审核通过也不会擅自推送代码。
3. **每项任务只派发一次**：发送结果不确定时只记录异常等你排查，绝不盲目重发，也不代点权限弹窗。
4. **单一控制者，严禁自审**：切换控制窗口后旧窗口立即失效；干活的执行者绝不能审核自己的交付。
5. **外部消息只是参考资料**：外部渠道发来的消息不能直接授权开工。

## 让 AI 帮你一键部署与体验

不用自己手动敲命令。把下面这段话直接发给你的本地 AI 助手（Codex、Claude Code、Antigravity CLI (agy) 或 Cursor），1 分钟跑通离线演示（不耗任何模型额度）：

~~~text
请按 agent-workflow-orchestrator 仓库里的 docs/setup.zh-CN.md 帮我部署并体验。
先检查本机的 Git、Python 3.10+ 和已安装的 AI 客户端；问清楚我要装在哪个目录，只在新子目录里使用手册指定的正式版本。
装完后只运行 demo_scheduler.py 和 workflow_control.py status 跑一遍离线演示（把演示状态写到新的临时目录）。
不要改动我现有的项目、账号或密钥，不要发起付费模型调用或真实派工。
最后告诉我演示结果和下一步该怎么接管我自己的项目，等我决定。
~~~

## 深入阅读

### 上手与实操
- [手动安装、Windows 便携版与完整文档索引](docs/setup.zh-CN.md)
- [面向已有仓库的项目入口（准备、独立审核、本地落地与恢复）](references/project-entry.zh-CN.md)
- [第一次真实模型联动指南](references/first-real-run.zh-CN.md) ｜ [Git worktree 代码闭环](references/worktree-code-loop.zh-CN.md)
- [窗口绑定、切换控制者与异常恢复](references/setup-and-switching.zh-CN.md)

### 核心模块指南
- [角色分工](references/roles.zh-CN.md) ｜ [任务与审核模板](references/task-and-review.zh-CN.md) ｜ [任务状态机](references/task-state-machine.zh-CN.md)
- [后台监控、可视化状态面板、提醒与外部收件箱](references/monitoring.zh-CN.md)
- [多客户端适配器（Codex / Claude / Pi / OMP / Herdr）](references/harness-adapters.zh-CN.md)
- [项目知识检索、双语扩展与术语表](references/material-packets.md) ｜ [知识治理 API](references/awo-knowledge.zh-CN.md)

### 排错与版本记录
- [Windows 测试与发布排错指南](docs/troubleshooting-windows.zh-CN.md)
- [更新记录（CHANGELOG）](CHANGELOG.zh-CN.md)
