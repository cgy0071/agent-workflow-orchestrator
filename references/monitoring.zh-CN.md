# 状态监控与自动通知

[English](monitoring.md) | Chinese

监控只负责检查与通知，审核角色根据任务/配置形成工程判断。脚本不读取任何特定工程文件，也不创建任务或批准下一原型。

初始化、实际窗口选择、会话策略、切换和审核门见 [初始化与切换指南](setup-and-switching.zh-CN.md)。请求填写 workflow_state 后启用台账；harness 名称不会自行选择窗口。登记及监控开始时冻结最终请求，审核者切换使用独立的绑定版本，不修改旧请求。

## 请求文件

参照 `examples/request.herdr.json`。每项任务单独目录，request.json 在投递前冻结；确认 Worker 开始后设置 dispatch_confirmed 并记录真实完成序列。自动化产生 monitor_status、summary、completion_receipt 和 notification_attempt/result，不覆盖任务交付。

所有适配使用相同任务合同：task_id、nonce、cwd、worker、reviewer、completion、authorization。角色名/harness 为元数据，可任意调整。

| 配置 | 含义 |
|---|---|
| worker.expected_identity | 派工前核验的实际身份字段；至少包含能区分会话/作业的标识与目录 |
| worker.baseline_completion_seq | 派工前完成序列，新完成必须大于它 |
| completion.artifacts | 必需文件列表；对象可设 nonempty 和已知 sha256 |
| completion.binding_artifact | 含本任务 ID/nonce 的 Markdown 或结构化结果文件 |
| completion.marker | 所选来源中必须包含的标记；若采用另一可靠合同可省略 |
| completion.marker_source | artifact 检查绑定文件，output 检查有限终端输出，both 同时检查；旧请求默认 output，新示例采用 artifact |
| monitor.interval_seconds | 默认300秒；可按任务改为1–86400秒 |
| monitor.command_timeout_seconds | 单次适配命令超时，默认15秒 |
| monitor.settle_grace_seconds | idle/done 后等待文件齐全的宽限，默认120秒 |
| monitor.max_consecutive_errors | 只读查询的连续错误预算，默认3，必须是正整数 |
| monitor.retry_interval_seconds | 只读重试间隔，默认5秒 |
| monitor.overall_timeout_seconds | 默认 null，一直监控到完成/异常；可设置任务总监控时限 |
| status_map | 将适配器自有状态映射到 working/idle/done/blocked/exited/failed |
| authorization.notify_reviewer | 当前用户是否已授权向审核者发一次通知；还需非空 source |
| reviewer.context_policy | 默认 inbox_only，直接通知需真实线程或专用窗口依据 |
| workflow_state | 可选，已初始化台账的绝对目录 |

默认三份 Markdown 可以改为其他文件。未提供预期输出哈希时，监控只封存实际哈希，不判断业务内容正确。

## 启动

```sh
python scripts/monitor_task.py --request /absolute/task/request.json --check
python scripts/monitor_task.py --request /absolute/task/request.json --preflight
python scripts/monitor_task.py --request /absolute/task/request.json --notify
```

--check 不联系 Agent，返回 STRUCTURE_OK；--preflight 只读核查身份和完成序号，不发送任务。台账请求必须先登记再监控。--notify 需要授权和相应会话策略。持续执行依赖本机进程、审核会话及接口可用，网络/额度/会话退出会产生异常。

Windows 可后台启动（路径替换为本机路径）：

```powershell
Start-Process -FilePath 'python.exe' -ArgumentList @(
  'C:/skills/agent-workflow-orchestrator/scripts/monitor_task.py',
  '--request', 'C:/tasks/TASK-ID/request.json', '--notify'
) -WindowStyle Hidden -RedirectStandardOutput 'C:/tasks/TASK-ID/monitor.stdout.log' `
  -RedirectStandardError 'C:/tasks/TASK-ID/monitor.stderr.log'
```

使用符合当前权限的已授权启动方式，不修改系统策略或自动同意批准框。Linux/macOS 可由已有进程管理器运行；技能不安装后台服务。

## 完成与续接

working 时只查状态。新完成且交付齐全后才读有限输出验证 marker 并保存 receipt。新的完成序号重新计算交付宽限，不受旧 idle 计时挤占；working 会清空旧计时。无效序号按响应错误处理，不截断小数或隐式转换字符串。

台账模式先持久化事件，再按策略通知；不带 --notify 也会保存收件箱。默认 inbox_only 不发送提示；独立旧模式在 --notify 已授权时写 pending_event.json。直接通知前核验身份与会话策略。发送、确认收到和验收通过分别记录，提示不是新 Owner 指令。确认、登记 REVIEW.md 和续派检查见初始化指南。

通知前用独占文件记录尝试；即使投递超时也不自动重发。出现 delivery_uncertain 要检查实际会话，避免重复指令。monitor.lock 防止并发监控；意外退出留下锁时先核对进程，不能盲目删锁重开。监控任务目录不得跨任务复用。

台账模式在 SQLite 事务中记录通知尝试，包含目标、请求/授权哈希和 UTC 时间。只读查询错误有限重试；身份变化、blocked、未知状态和失败停止等待检查。检查异常后显式 --resume，可保留旧摘要为历史并使用冻结输入恢复监控；不会重发已经尝试的提示，也不会把已待审核交付当成新执行。

## 非 Herdr 适配

Herdr 优先读取 completion_seq，缺失时读取 state_change_seq；计数器缺失或类型错误会报错，不再静默返回 0。派工前将实际观测的归一化值写入 baseline_completion_seq。state_change_seq 记录所有状态变化，其递增只证明发生了新活动，仍须满足 idle/done、任务 ID/nonce 绑定文件及 marker 合同。其交付宽限从第一次新活动后的 idle/done 开始，不因计数变化或中间的 working 状态重新计时。真正的 completion_seq 仍为每次完成提供新宽限。回执记录 sequence_source。

新任务建议使用示例的 marker_source=artifact，并将完整标记写入 HANDOFF.md。终端文本可能包含命令回显，不能作为作者身份凭证；文件标记加强交付合同，也不能认证作者或证明验收通过，仍需独立审核。已冻结的旧请求保持默认 output 行为，不原地修改。

- command：worker.status_argv 返回规范 JSON，output_argv 返回完成后的有限文本；reviewer.identity_argv 返回 identity，prompt_argv 以独立 `{message}` 参数接收通知。所有命令按参数数组执行，无 shell 插值。
- receipt：worker.status_path 指向任务绑定 JSON，output_path 为有限读取的 stdout。适合 `run_job.py` 的单次作业；见 examples/job.json。先确认具体 harness 的非交互命令，不能把交互 TUI 进程当单次作业。

适配样式见 harness-adapters.zh-CN.md。各角色均可使用任意 harness；发送通知的实际接口必须能核验目标身份并精确投递会话。没有该接口先返回 ADAPTER_NOT_VERIFIED，不宣称全自动已运行。

在任务目录创建 STOP_MONITOR 会于下一轮停止监控，不发送续接通知，也不取消 Worker。实际取消按调度器授权处理。原始失败和父任务记录保持不变。

## 状态面板

标准库本地面板仅监听回环地址，打印每次启动生成的带令牌地址，并用默认浏览器打开（加 `--no-browser` 则只打印）。可双击 `dashboard.cmd`（Windows）或 `dashboard.command`（macOS），或运行：

```sh
python -B scripts/dashboard.py --state /absolute/workflow-state
```

不带 `--state` 时，页面会请你选择状态目录，并列出 `~/.agent-workflow-orchestrator/dashboard-recent.json` 中最多 10 个最近目录；该文件只保存路径。未启用派发时，可用 **Choose workflow** 切换查看的目录。默认端口 8765 被占用时，服务会自动改用空闲端口。可设置 `AWO_PYTHON` 指定启动文件使用的解释器。

页面只读台账，不改动它，也不自动查询 Agent；显示绑定、任务阶段与证据、DAG 依赖、收件箱事件、资源占用和告警。自动刷新只读台账；**Check workers now** 按钮才执行有超时的实时状态查询。服务校验令牌、Host 和 POST Origin，使用严格 CSP，并把台账文字作为文本渲染。不要外传带令牌地址。

要启用表单派发，启动时与 `--state` 一起增加 `--allow-dispatch --actor INSTANCE --version 1 --context main --tasks-root /absolute/tasks`；此时状态目录固定，不能切换。actor 必须是当前有效控制者；`main` 是操作者的明确声明。预览实时检查空闲 Worker，返回任务绑定请求、nonce、消息与摘要，不写文件。确认时重新检查预览和完成序列，创建新任务目录，并交给 `workflow_scheduler.dispatch` 单次发送；不自动重试不确定派发。独立确认执行后，由操作者自行编写 `EXECUTION.md` 并运行页面显示的 `reconcile` 命令，再启动监控。

任务详情中的 **View result files** 只读取该任务声明的文件（绑定文件、交付文件、审核文件），每个最多 200000 字节，并与完成回执中的哈希比对。派发模式下，属于 actor 自身审核角色的完成更新会显示 **Acknowledge** 和 **Record review**。两者都先做只读预览，再一次性确认，确认时以绑定的 actor 调用 `workflow_control.acknowledge` 或 `record_review`，并重新执行全部审核门检查。页面从不写入审核文件：请自行编写 `REVIEW.md`（或冻结的 `review_path`），包含任务 ID、nonce 和 `Verdict:` 行；文件在预览后改动会使预览失效。页面不执行 reconcile、绑定切换、续派检查或 DAG tick。
