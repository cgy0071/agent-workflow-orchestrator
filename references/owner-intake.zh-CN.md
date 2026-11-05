# Owner 收件适配接口

[English](owner-intake.md) | 中文

外部通道适配器做两件事：写入带签名的请求文件，并提供投递 Owner 更新的命令。请求是**不可信资料，绝不构成派发授权**。通道写文件前负责核实 Owner 身份。Planner 只有在自己的会话里直接收到 Owner 授权后才能派发。

## 请求文件

以 UTF-8 JSON 写入 `<inbox>/<request_id>.json`。先在同一目录创建临时文件（文件名以 `.` 开头或以 `.tmp` 结尾），再重命名为正式文件。AWO 忽略临时文件、其他扩展名和子目录。收件目录由适配器创建。编号须持久、无冲突，符合 `[A-Za-z0-9._-]{1,64}`；AWO 不强制通道前缀。例如 QQ 适配器可使用 `qq-20261105-0007`，把每日序号独立持久化，并在写入前检查收件目录及 `processed/` 是否已有此编号。

[JSON Schema](owner-intake.schema.json) 描述 schema 1。必填字段为 `schema: 1`、与文件名一致的 `request_id`、只用于记录的 `channel`、`text`、`requested_at`、`confirmed_at`、`source` 和 `hmac`。只有 Owner 明确指定项目时才写可选的 `project`，其值必须等于项目 workflow 的 `name`。`source` 包含通道自己的 `kind`、`owner: true` 和由原始消息 ID 派生的 16 位小写十六进制 `message_ref`；不要写入原始 ID。

`text` 是去掉通道前缀和项目名后的正文，长度为 1–4000 个 Unicode 码点（Python `len`、JavaScript `[...text].length`）。允许制表符和换行，不允许其他控制字符。时间用带数字时区偏移的 ISO 8601、整秒、无毫秒，例如 `2026-11-05T10:00:30+08:00`；AWO 也接受 `Z`。

密钥只从环境变量 `AWO_INTAKE_HMAC_KEY` 读取。按顺序用 LF 连接 `request_id`、`text`、`requested_at`、`confirmed_at`、`project`（没有时用空串），对其 UTF-8 字节计算 HMAC-SHA256，写入小写十六进制。AWO 用常量时间比较，拒绝未签名或变更的文件。密钥不得进入 JSON、日志或仓库。

## 发送 Owner 更新

在指定的 `owner_notify.py` 命令通道中显式订阅 `intake_update`。argv 数组必须恰有一个独立的 `{text}` 元素，且不经过 shell。退出码 0 表示已发送或已安全存入积压；非 0 由 AWO 的至少一次发件箱重试。若通道桥使用 HTTP，立即发送成功可返回 200，持久排队可返回 202；桥应在 10 秒内结束（AWO 命令超时 15 秒）。通道侧应按 `sha256(text)` 在一小时内去重，吸收重试。AWO 将脱敏正文限制为 300 字符。

请求文件的 `channel` 不决定回复去向。只有项目 workflow 中的 `owner_intake.notify_channel` 决定目标。[workflow 配置片段](../examples/owner-intake.workflow-snippet.json)和[命令通道示例](../examples/notify-qq.json)均使用占位路径。同一接口可供 QQ、Telegram、邮件等经身份核实的通道使用，仓库不包含 Bot 专用实现。
