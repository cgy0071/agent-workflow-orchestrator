# 任务状态与事件合同

[English](task-state-machine.md) | 中文

执行、派工、交付、通知、审核和续接分别记录。命令见[调度指南](scheduling.zh-CN.md)，
机器可读的日志外壳见 `task-event.schema.json`。

| 维度 | 状态 | 转换依据 |
|---|---|---|
| 派工 | reserved -> submitted／uncertain -> running／not_sent／terminated | 当前控制器、持久请求、nonce、代际及独立核实证据 |
| 执行占用 | running -> finished -> resolved | 已登记监控的交付／退出，有效验收或核实终止 |
| 交付 | working、settling、review_ready、incomplete、failed、exited、adapter_error、monitor_timeout、monitoring_stopped | 冻结请求、身份、完成序列、文件与哈希 |
| 通知 | 仅收件箱、reserved、submitted、delivery_uncertain | 授权、目标身份及持久单次发送尝试 |
| 审核 | pending -> acknowledged -> reviewed | 已绑定主审核会话、配置的 verdict、REVIEW.md 哈希 |
| DAG | waiting -> ready -> reserved／submitted／running -> under_review -> accepted | 所有依赖有效验收及独立目录／资源占用 |
| DAG 阻断 | blocked、evidence_changed、blocked_dependency | 拒绝、失败、证据变化、核实终止或上游阻断 |

tasks.status 的 running 表示执行槽位被占用或结果不确定，包含尚未核实的派工；
dispatches.status 的 running 表示已独立核实执行。两者都不证明当前 CPU 进程活跃。
finished 使旧接口的角色槽位在交付／失败后可用，但调度器的目录／资源仍保留，
直至有效验收或核实终止。resolved 表示占用释放；只要证据未变，DAG 仍能引用有效验收。

调度预留、发送、不确定结果、核实、交付事件、审核和释放均记录事件 ID、任务、代际、
操作者、UTC 时间、类型及证据索引。控制器版本保存在预留／审计记录中。
监控以冻结请求哈希标识生产者；这是本地来源约定，不是身份认证。

任务 ID 是不可变的尝试身份，不能换输入复用。重复相同未确认预留不会再发一次。
监控事件 ID 由请求哈希和报告状态生成，重复完成事件不会产生第二次审核入口。
迟到异常不能授予验收；accept 必须来自 review_ready，DAG 还会核对任务／nonce
回执以及当前审核／交付文件。

不确定发送不能变为相同 ID 的全新派工；已确认执行不能改判 not_sent；关闭尝试不能
重新打开。reject／repair 不满足 DAG 依赖，审核文件变化会阻止下游。
旧控制器、侧对话及 unknown 上下文不能派工、核实、释放或审核。

返工新建限定范围任务，使用新 ID／nonce，按需要填写新 generation 和已封存父审核。
调度器不自动重做失败任务，不根据缺少更新推断取消。

`incomplete`、`blocked` 和 `adapter_error` 是可恢复的暂停：每次停顿一个事件，监控继续，
尝试未关闭时不能审核。受限的 `continue`（日志依次为 `continue_reserved`，然后是
`continue_submitted`、`continue_uncertain` 或 `continue_not_sent`）让同一次尝试在
`incomplete` 后继续，不是新的派工。
