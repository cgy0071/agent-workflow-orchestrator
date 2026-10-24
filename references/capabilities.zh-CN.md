# 按版本记录 Harness 能力证据

[English](capabilities.md) | 中文

分别记录 spawn、attach_resume、send、status、cancel、collect、heartbeat。
`examples/capability-profile.json` 提供完整模板，各项状态为 unverified、unsupported
或 verified，不能由 harness 标签推断。

| 传输实现 | spawn | attach/resume | send | status | cancel | collect | heartbeat |
|---|---|---|---|---|---|---|---|
| herdr | 未实现 | 显式绑定 | 有条件 prompt | agent get | 未实现 | agent read | 未实现 |
| command | 未实现 | 显式绑定 | 配置 argv | 配置 argv | 未实现 | 配置 argv | 未实现 |
| receipt | run_job 单次作业 | 未实现 | 未实现 | 状态文件 | 未实现 | 输出文件 | 未实现 |

此表描述本包接口；身份绑定不等于恢复真实对话，观测时间戳也不是原生 Worker 心跳。
缺少操作会报错，调度器不会猜测供应商命令或创建会话。

本地 Python 子进程演示 command 传输和审核流程。接入真实客户端时，应核验
已安装版本的实际身份、命令响应和任务交付，并为该版本建立档案；仓库不预配置客户端。

标记 verified 前记录操作系统、Harness／传输版本、源码提交、精确复现 argv 数组，
以及非空原始证据文件的 SHA-256。按操作保留身份／状态响应、任务／nonce、退出码、
交付及审核证据。私有记录保存在公开仓库外，证据相对路径必须位于 profile 所在目录内。

```sh
python scripts/workflow_scheduler.py capabilities --file /absolute/profile.json
```

校验器检查完整性、版本、命令形状与文件哈希，不执行命令、不独立判断证据内容，
也不授予操作权限。首次真实接入仍需实际授权范围内的连接验证；版本变化后另建记录。
