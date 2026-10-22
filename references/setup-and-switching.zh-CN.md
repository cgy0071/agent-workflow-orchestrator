# 初始化、窗口选择与会话切换

[English](setup-and-switching.md) | 中文

## 1. 分开指定角色、客户端和实际窗口

从 `examples/workflow.json` 配置角色。`harness` 只记录客户端名称，不会自动选择或启动窗口。每个角色都需要通过已验证的接口发现实际会话，并保存适配器配置。

术语约定：本文中的 **pane 特指 Herdr 的终端分屏单元**；“窗口／会话”是通用概念。其他客户端使用各自已验证的接口和标识，不适用 Herdr 的 pane 命令。

Herdr 用户通过本机已安装版本的只读发现和身份查询，记录可执行文件、session、pane、实际 agent kind、terminal ID 和启动目录。其他 CLI/API 按 [适配器说明](harness-adapters.zh-CN.md) 提供身份与状态查询命令。启动参数取自用户选择的运行配置。

打开或选择客户端会话，核查身份和当前任务后再绑定。

### 打开并核验会话

1. 在选定客户端中手动打开专用窗口或会话（使用 Herdr 时为 pane），进入任务工作目录。
2. 启动已安装的 harness，按需选择 provider、model 和 thinking。
3. 核对实际运行配置，通过已验证的适配器读取会话身份及 cwd；Herdr 还需记录 pane、terminal 标识。
4. 保存角色适配器配置，继续第 2 节的绑定流程；派工前再记录实际序列基线。

已核验的交互会话绑定后可以监控、通知和执行已授权的后续流程。

核验后，将填写后的 `request.json` 中 `worker`、`reviewer` 对象分别保存为 `worker.json`、`planner.json`。`worker.json` 删除 `baseline_completion_seq`，该字段保留在每份任务请求中；其余适配器字段保持一致。Paper Lead 可以使用另一份 reviewer 配置。昵称、模型名和窗口标题都不能代替真实身份。

## 2. 初始化并绑定角色

在 Skill 仓库中运行；实际部署请使用绝对路径。状态目录必须是新目录，并应保存在公开仓库之外。

```sh
python scripts/monitor_task.py --check-workflow workflow.json
python scripts/workflow_control.py --state /absolute/workflow-state init --config workflow.json
python scripts/workflow_control.py --state /absolute/workflow-state bind --role planner --spec planner.json --mode controller --policy inbox_only
python scripts/workflow_control.py --state /absolute/workflow-state bind --role executor --spec worker.json --mode worker
python scripts/workflow_control.py --state /absolute/workflow-state bindings
```

绑定会实际查询身份。保存 controller 返回的 `instance_id` 和 `version`。同一流程只允许一个活动 controller；其他角色可设置为 `worker`、`reviewer` 或 `advisor`。默认 controller 可派工和审核，reviewer 只能审核，worker/advisor 没有这两项权限。角色的 `permissions` 列表可进一步限制权限。

SQLite 台账保存配置快照、绑定版本、冻结任务、待审核事件、通知尝试、审核和审计记录。需要更改角色架构或流程策略时，修改配置并初始化新的状态目录，保留旧记录并明确交接未完成任务。更换角色的客户端或窗口使用下面的切换命令。

## 3. 主对话和侧对话策略

| 策略 | 所需依据 | 通知行为 |
|---|---|---|
| `inbox_only` | 已验证的角色与窗口身份 | 只持久化事件，不发送提示。默认策略。 |
| `exact_thread` | 真实、固定且可观察的 `conversation_id` 和 `branch_id` | 发送前同时核对两个字段。 |
| `dedicated_window` | 操作者明确声明该窗口专用于控制流程的主对话 | 核对窗口身份后发送；不要在该窗口开侧对话。 |

客户端没有暴露对话和分支标识时，无法仅凭 terminal ID 或标题可靠识别侧对话。应使用收件箱，或明确预留专用主窗口。直接使用旧版独立监控时，在 reviewer 中填写 `context_policy`；专用窗口还需 `context_declaration`。使用台账时，通过绑定的 `--policy` 选择策略，专用窗口需在 `--reason` 中明确声明。

台账 bind/switch 会将所选策略保存到适配器配置；dedicated_window 的 context_declaration 缺失时从 --reason 填入。独立监控请求仍需自己的声明。绑定和 preflight 对直接通知的 Herdr 目标执行只读 agent_session 检查；发送前再次核对身份和该前提，不发送测试提示。字段存在只是能力前提，不保证投递成功，超时及不确定投递规则仍适用。

报告的 Windows / Herdr 0.9.3 实测中，Pi 暴露 agent_session 且可接收提示，dsh-tui 没有该字段。此类目标使用 inbox_only，或另行验证的 command 桥接。不能根据 harness 名称或可为空的 name 字段推断兼容性；其他版本以及 Codex/Claude/Grok 均须独立实测。

**信任边界：** `--context main|side|unknown` 来自可信桥接程序或知情操作者的声明，不是自动分支识别或身份认证。脚本会拒绝声明为 side/unknown 的操作和失效绑定；它无法阻止拥有台账写权限的程序谎报 main。桥接程序必须先核对实际调用方元数据，再传入 main。做不到时使用收件箱模式，不编造分支 ID，也不根据窗口焦点判断主对话。

侧对话可以阅读和建议，不能确认事件、登记准入或续派任务。转发一条完成消息不会转移控制权。重开客户端且身份变化后，应重新核验绑定。

## 4. 检查、派工、登记与监控

每份任务请求增加 `workflow_state`，指向台账绝对路径。填写真实目录、身份、nonce、授权来源和派工前的完成序号。

```sh
python scripts/monitor_task.py --request /absolute/task/request.json --check
python scripts/monitor_task.py --request /absolute/task/request.json --preflight
```

`--check` 只做离线结构校验；`--preflight` 只读查询双方身份并核对完成序号，不派工。由现有调度器核查执行者空闲状态和目录归属，再提交一次已获批准的任务。

确认执行开始后，将 `dispatch_confirmed` 设为 true，登记最终请求，随后保持文件内容不变：

```sh
python scripts/workflow_control.py --state /absolute/workflow-state register-task --request /absolute/task/request.json --actor CONTROLLER_INSTANCE --version 1 --context main
python scripts/monitor_task.py --request /absolute/task/request.json --notify
```

register-task 用于登记现有控制器已经提交的任务。也可使用[持久派工与 DAG 调度](scheduling.zh-CN.md)先登记再单次发送、核实执行，并在共享状态库中约束目录／资源占用。监控要求已确认请求与台账一致，并保存 `frozen_request.json`。续派登记检查父审核，DAG 依赖另行要求审核通过的证据；业务审核仍由获授权的审核者负责。

## 5. 收取、确认、审核与续派

先保存事件，再尝试通知。发送成功、主对话确认、审核通过是三个不同状态。通知误入侧对话或审核者离线时，事件仍可找回。

```sh
python scripts/workflow_control.py --state /absolute/workflow-state inbox
python scripts/workflow_control.py --state /absolute/workflow-state ack --event EVENT_ID --actor CONTROLLER_INSTANCE --version 1 --context main
```

独立阅读冻结请求、交付、变更和原始证据，再写审核文件，格式例如：

```markdown
# TASK-EXAMPLE 审核
Task ID: TASK-EXAMPLE
Nonce: 实际任务的 nonce
Verdict: accept

已核查的证据：……
未执行项目与剩余限制：……
结论与下一项获授权行动：……
```

```sh
python scripts/workflow_control.py --state /absolute/workflow-state review --event EVENT_ID --file /absolute/task/REVIEW.md --verdict accept --actor CONTROLLER_INSTANCE --version 1 --context main
python scripts/workflow_control.py --state /absolute/workflow-state next-check --event EVENT_ID --actor CONTROLLER_INSTANCE --version 1 --context main
```

把返回的 `parent_review` 对象写入下一份请求，同时将其 `task_id` 写入 `parent_task`。脚本核对台账和当前审核文件哈希。默认 accept/repair 可以继续，通过 `workflow.continuation_verdicts` 修改。通过门槛只允许在既有授权内进入配置的下一阶段，不会自行派工或证明技术判断正确。reviewer 与 controller 可以是不同角色。

## 6. 安全切换客户端或窗口

将同一角色切换到新核验的配置：

```sh
python scripts/workflow_control.py --state /absolute/workflow-state switch --role planner --spec new-planner.json --mode controller --policy inbox_only --reason "移交到新的主对话"
```

版本递增，旧 instance/version 立即失去控制权限。旧任务保持原始请求；待审核事件仍归属 reviewer 角色，新绑定可以确认它们。如果还没有发送尝试，获授权的 `deliver --event EVENT_ID` 可通知当前绑定。已经提交或发送结果不确定的消息不会盲目重发。每次交接都检查收件箱。

执行者仍在运行或状态不明时，先登记延后切换：

```sh
python scripts/workflow_control.py --state /absolute/workflow-state switch --role executor --spec new-worker.json --mode worker --defer --reason "当前任务结束后更换执行者"
python scripts/workflow_control.py --state /absolute/workflow-state activate --role executor
```

激活需要显式执行，会重新核验身份，并在旧任务运行或状态不明时拒绝。有效完成交付或观察到失败、退出才可结束任务。适配器错误、blocked、超时、缺交付不能证明执行已停止。

核查旧执行状态和外部租约；若已独立确认停止，写明任务 ID/nonce 的终止证据，再由当前 controller 使用 `resolve-task --task TASK_ID --evidence /absolute/TERMINATION.md`，附带 actor/version/context。此操作只登记操作者的判断，不取消实际进程，也不代表审核通过。然后激活新执行者，后续请求使用新绑定。

## 7. 恢复已检查的监控异常

只读查询有连续错误重试预算，默认 3 次；发送提示不会自动重试。新的完成序号有独立交付宽限期。检查异常后，可保持原始输入显式恢复：

```sh
python scripts/monitor_task.py --request /absolute/task/request.json --resume --notify
```

旧摘要保存为历史文件，冻结输入和通知尝试保留。已交付待审核的任务不能通过恢复变成第二次执行。遗留 `monitor.lock` 必须先核查进程，再人工清理。不要复用任务 ID、目录或覆盖历史证据。
