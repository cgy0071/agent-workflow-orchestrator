---
name: agent-workflow-orchestrator
description: Configure and run multi-agent workflows with editable roles, architecture rules, and automation policies. Supports Planner, Executor, and Paper Lead as defaults across Codex, Pi, Claude, dsh-tui, Grok, and other harnesses through Herdr, command, or receipt adapters, with completion notification and independent review.
---

# Agent Workflow Orchestrator

[English](SKILL.md) | Chinese

组织可配置的多角色工作流，将任务、状态检测、交付、审核和下一步动作连接起来。本 Skill 是 `scripts/` 中工具的操作规程：脚本强制执行单次派发、身份核验、通知去重和审核关卡，本文档说明何时以及如何运行它们。两者都独立于具体工程、路径、模型及 harness。Planner、Executor、Paper Lead 是默认模板，用户可以修改或替换。

## 读取配置

从用户指定位置读取工作流配置；未提供时，以 `examples/workflow.json` 为起点。先确认目标、角色职责、可写范围、工程约束和自动化授权。已有项目协议只在用户选择时作为输入；不依赖外部角色文档或另一个 Skill 才能工作。

配置可修改：角色数量和权限、设计约束、阶段及准入条件、调度与审核角色、轮询间隔、完成判据、异常处理、后续动作及停止条件。配置不提供新授权；当前用户指令优先。

## 角色和 harness 分离

默认职责见 [roles.zh-CN.md](references/roles.zh-CN.md)。它们可以重命名、合并、增加，或改为其他领域的角色；不要把默认 Planner 的限制强加给用户改设后的角色。

任一角色可用 Codex、Pi、Claude、dsh-tui、Grok 或其他 harness。角色、harness、模型、状态接口分别记录。按实际能力选用 [harness-adapters.zh-CN.md](references/harness-adapters.zh-CN.md) 的 Herdr、命令或作业回执适配，不根据昵称推断 CLI。实际工具没有可靠接口时报告适配缺口，不伪造已连接。

## 调度与执行

需要持久单次派工或 DAG 分支／汇合时，读取[调度指南](references/scheduling.zh-CN.md)。按每项任务的明确授权及已核验 Worker 命令操作；调度器先保存尝试，再发送，并在共享状态库中约束目录／资源占用，以独立审核证据控制依赖。真实客户端能力按[能力证据合同](references/capabilities.zh-CN.md)记录版本与原始证据，不能由标签推断。

首次派工或更换客户端/窗口前，阅读 [初始化与切换指南](references/setup-and-switching.zh-CN.md)。先建立角色绑定、核验实际目标并保存控制器 instance/version。侧对话仅提供建议；默认只写收件箱，精确路由需真实对话/分支元数据，专用主窗口需明确声明。context 是可信适配器/操作者声明，不是自动分支识别或身份认证。

1. 按配置恢复目标、当前任务、已接收结果和未关闭门，不从聊天猜测已完成工作。
2. 写可审核任务，明确 ID、目标、目录、允许改动、冻结输入、验收、交付和停止点；模板见 [task-and-review.zh-CN.md](references/task-and-review.zh-CN.md)。任务细节可随工程调整。
3. 重新核验目标身份和工作目录，检查当前任务与活动租约。同一可写目录串行，独立目录才并行。复用现有调度台账，避免竞争控制器。
4. 保存唯一 task ID / nonce、目标身份、任务书及投递尝试；单次投递，确认已开始再启动监控。模糊投递先检查，不盲目补发。
5. Executor 按配置执行并交付。默认是 RESULT / EVIDENCE / HANDOFF 三份 Markdown；其他交付形式可在任务中明确。

Herdr 是可选传输。在已有 Herdr 调度器环境中复用其身份/租约机制；没有 Herdr 时可用经核验的官方 CLI/API 适配。技能自身不安装或修改供应商、认证或模型设置。

## 状态监控与通知

`scripts/monitor_task.py` 按请求读取状态，工作期间不反复拉取对话。默认 300 秒查询，可由配置调整；每次命令有限超时，整体默认等到完成或异常，没有一小时总上限。监控合同与启动方式见 [monitoring.zh-CN.md](references/monitoring.zh-CN.md)。同一任务不启动重复监控。

完成事件必须绑定本任务、身份和新完成序列，并满足请求中的交付/标记合同。`review_ready` 表示可以审核，不是成功。记录交付哈希，在既有通知授权下，向核验后的审核角色发送一次通知；审核角色不必是 Codex，也不必叫 Planner。

使用 workflow_state 时，监控前登记冻结请求，通知前持久化事件。交接后由新核验绑定找回收件箱事件。提示发送不代表确认收到。只读查询采用有限重试预算；不确定的提示投递不自动重试。Worker 提前停止（额度、限流、网络）或停在权限提示上时，监控继续运行：交付未完成、blocked 和查询中断在每次停顿中只报告一次，不当作结束。对 `incomplete`，在 Worker 恢复可用后最多使用受限的 `workflow_scheduler.py continue` 让同一任务继续；不要重新发送任务。权限提示请 Owner 在 Worker 的 harness 中处理，不要自行回答。不要审核停顿事件；等待 review_ready 或关闭本次尝试。其他异常检查后可显式恢复，输入保持不变。

异常、blocked、缺交付、身份变化和通知不确定分别保存；不回答批准框、不盲目重发。停止监控不等于停止执行进程，取消工作按配置和授权另行处理。

用户要求打开状态面板或可视化查看状态时，在后台运行 `python -B scripts/dashboard.py --state /abs/state`。它会打开本机浏览器；同时把控制台打印的带令牌地址只交给用户本人。除非用户在本次对话中明确要求表单派发或页面审核操作，否则不加 `--allow-dispatch`；该参数同时启用页面的确认收到与审核登记。同样，只有用户明确要求在页面上与已绑定角色对话时，才加 `--allow-chat`；发送者仍是当前有效的控制者。只有用户明确要求在页面上管理工作流时，才加 `--allow-manage`（与派发相同，需要控制者和 `--tasks-root` 参数）；每次管理确认都以已绑定的控制者执行，并重复命令行中的全部检查。只有用户明确要求局域网访问时才加 `--lan`；只有用户明确同意局域网控制时才加 `--lan-allow-control`。其他设备也可使用 SSH 隧道，或按照[监控说明](references/monitoring.zh-CN.md)配置 `--public-origin` 和私有 HTTPS 代理。用户用完后停止该进程。

## 审核与自动续接

审核角色收到通知后，读取冻结请求、交付、改动和原始验收证据，写明确的接收/返修/拒绝/缺证据结论，更新当前状态。仅按配置推进下一阶段；工程接收与论文贡献判断可设为独立门。

使用台账时，从第二项任务起用 `workflow_scheduler.py follow-up` 根据封存的父审核生成请求和标准消息，`dispatch` 只派发一次，只读观察后用 `confirm-start` 确认开始，再监控已确认请求；独立审核 `REVIEW.md` 后用 `workflow_control.py settle` 结案。未观察到开始时保持未确认，先检查而不重发。仅当配置的续派门允许时，`settle` 才返回下一份父审核。旧控制者和声明为侧对话／未知的上下文不能操作；更换执行者须重新核验身份，监控失败本身不证明任务停止。

持续执行授权覆盖的范围内，可自动创建下一项或窄范围返修任务，使用新 ID / nonce 关联父任务。超出目标、预算或权限时报告具体缺口。配置的目标达成、暂停、取消或终止条件满足时停止，不为了持续运行而另造任务。

Paper Lead 默认负责科学问题、文献差距、主贡献和 claim–evidence；工程审核结果交给它，补证需求返回规划角色。用户可将这一角色换成产品评审、质量负责人或其他职责。

## 本地验证

新增/修改适配先用离线响应验证身份、新完成、缺输出、blocked 和通知去重，再按授权做小范围连接验证。运行 `python -B -m unittest discover -s scripts -p "test_*.py" -v` 和 `python scripts/check_encoding.py`。离线通过不等于所有客户端已经完成现场连接测试。README.zh-CN.md 提供安装、配置和使用示例。
