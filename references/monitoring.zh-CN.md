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
| completion.marker | 完成后有限输出中必须包含的标记；若采用另一可靠合同可省略 |
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

- command：worker.status_argv 返回规范 JSON，output_argv 返回完成后的有限文本；reviewer.identity_argv 返回 identity，prompt_argv 以独立 `{message}` 参数接收通知。所有命令按参数数组执行，无 shell 插值。
- receipt：worker.status_path 指向任务绑定 JSON，output_path 为有限读取的 stdout。适合 `run_job.py` 的单次作业；见 examples/job.json。先确认具体 harness 的非交互命令，不能把交互 TUI 进程当单次作业。

适配样式见 harness-adapters.zh-CN.md。各角色均可使用任意 harness；发送通知的实际接口必须能核验目标身份并精确投递会话。没有该接口先返回 ADAPTER_NOT_VERIFIED，不宣称全自动已运行。

在任务目录创建 STOP_MONITOR 会于下一轮停止监控，不发送续接通知，也不取消 Worker。实际取消按调度器授权处理。原始失败和父任务记录保持不变。
