# P1-1 持久化 Agent 消息

[English](awo-messaging.md) | 中文

`scripts/agent_messages.py` 在现有 AWO SQLite 数据库内实现消息领域逻辑。
应用服务继续负责认证、controller epoch/freeze 策略、命令幂等、外层命令事件和
事务提交/回滚。该模块不注册端点，也不修改 `awo_service.py`；由主集成负责人接入。

## 集成契约

```python
create_message_schema(con)
apply_command(con, project_id, principal, tool, payload)
read_messages(con, project_id, principal, filters=None)
expire_deliveries(con, project_id, principal)
```

- `con` 是启用外键的权威数据库 `sqlite3.Connection`。领域逻辑兼容默认 tuple
  行和 `sqlite3.Row`；父服务的重放 helper 要求 `sqlite3.Row`。
- 建表、命令和过期维护都要求调用方已开启事务。调用方应使用 `BEGIN IMMEDIATE`，
  将领域结果、命令日志和外层事件一起提交；任何异常都整体回滚。模块不创建连接，
  不开始/提交事务，不调用 `executescript`，也不改变共享 schema 版本。
- 父服务先建立已接受的 service/registry schema，再在获授权的 schema 事务中调用
  `create_message_schema`。该函数可重复执行，只添加 `v2_message`、
  `v2_message_delivery` 及其索引。
- `principal` 是服务端派生的普通 dict，必须包含 `agent_id`、`role`、
  `principal_id`、`project_id`。`principal_id` 是父服务定义的审计/重放身份，
  不是凭据。领域逻辑检查项目、active 注册项及角色一致性；已接受的访问服务可将现有
  Agent 的凭据降级为 observer，只读自己的收件箱，不增加写权限。领域逻辑不能认证
  不可信调用者直接提供的字典。
- 命令仅接受 `message.send` 和 `message.ack`，返回普通 dict；拒绝时抛出延迟导入的
  父服务 `ServiceError`，带 `invalid_request`、`permission_denied`、
  `state_conflict`、`revision_conflict` 或 `transaction_required` 等错误码前缀。
- 领域事件延迟调用已有 `awo_service._event`，actor 为 principal ID，source 为
  `v2`，`source_seq=None`。模块不写命令日志、不执行命令重放。父服务先校验认证和
  epoch，再按 `(project_id, principal_id, command_id)` 查找、核对原请求哈希；
  重放时直接返回已记录的响应，不再次调用领域逻辑。

## 发送

必填字段为 `to`、`kind`、`body`。`to` 是本项目内 1..50 个已注册 Agent ID
组成的列表，收件人必须 active；重复 ID 合并为一次投递。正文必须为非空 UTF-8
文本，编码后最多 65,536 字节，不含 NUL。支持七种消息：

| kind | 领域行为 |
|---|---|
| `question` | 拒绝向自己提问；最多一个答案。 |
| `answer` | `reply_to` 必须指向发给当前回答者的未过期问题；只发送给提问者。 |
| `handoff` | 投递上下文，不转移 Assignment 所有权。 |
| `notice` | 投递信息或声称的状态，不应用该状态。 |
| `consult` | 征求意见，不转移所有权。 |
| `state_request` | 请求状态报告；对方可用 `notice` 回复。 |
| `steer` | 为当前运行的 Assignment 记录方向修正请求，不执行注入。 |

可选字段为 `task_id`、`assignment_id`、`generation`、`thread_id`、`reply_to`、
`priority`（`P0`..`P4`，默认 `P3`）以及 `expires_at`（带时区的未来 ISO 时间，
统一存为 UTC）。可选绑定字段为 null 表示未绑定。`generation` 必须为正整数，
不能为布尔值；有 `assignment_id` 时必填，无 Assignment 时不能提供。
只提供 Assignment 时推导 Task；显式 Task 必须匹配。同一 Task slot 出现更新代际后，
不能再发送绑定旧代际的消息。
Assignment 所有者也必须是同项目内的已注册 Agent。

Worker 只能主动发送绑定自己 Assignment/Task 的消息；controller 和 reviewer
可以发送绑定本项目任意 Task 的消息。收件人可在已收到消息的完全相同绑定下回复，
无需拥有对应 Assignment，因此同行或 reviewer 可以回答 worker 的问题。
回复继承 Task、Assignment、generation、thread，冲突字段被拒绝；不能通过回复恢复
已被新代际替代的 Assignment。

Controller、worker、reviewer 可以发送，observer 不可发送。只有 controller/reviewer
可以发送 steer，收件人必须恰好是匹配代际、持有 slot 且处于 running 状态的 Assignment
所属 Agent。包括写有批准或完成措辞的答案在内，任何消息都不会改变 Task、Assignment、
review、所有权、slot 或预算状态。

根消息默认以自身 message ID 作为 thread ID，也可指定新 thread ID。加入已有线程必须
是本项目该线程先前的发送者或收件人。线程参与资格不会扩大对其他收件人消息的读权限。

发送成功返回：

```text
{
  status: "CREATED", message_id, thread_id, revision: 1, event_id,
  deliveries: [{delivery_id, recipient_agent_id, state: "held", revision: 1}, ...]
}
```

Answer 在同一事务中插入答案及投递，将回答者收到的问题投递改为 `answered`，并递增其
revision；其他收件人的投递保留原收悉状态。`v2_message_one_answer` 部分唯一索引保证
即使绕过 Python 前置检查，也无法插入第二个答案。`answer` 只回复 `question`；其他
会话可使用带 `reply_to` 的 `notice`/`consult`。

## 投递、ACK、TTL 与重放

Delivery ID 根据 message ID 和收件人 Agent ID 生成一次并持久化，
`(message_id, recipient_agent_id)` 唯一约束防止重复投递。重复读取、重连和父服务命令
重放都返回同一 ID。ACK 丢失时保留同一持久化投递，便于再次投递，不新建消息或 delivery。
不同的发送命令即使正文相同，也创建不同消息。

`message.ack` 只能包含必填的 `delivery_id`、`state`（`read` 或 `acknowledged`）
和 `expected_revision`（正整数）。领域逻辑会在父服务检查之外再次校验 revision。
只有该投递的收件人能 ACK，controller/reviewer 也不能代替其他收件人 ACK；observer
不能 ACK。未读可进入 `read` 或直接 `acknowledged`，`read` 可进入 `acknowledged`。
每次变化递增 revision 并写领域事件。相同状态和当前 revision 的 ACK 为无操作
（`status: "UNCHANGED"`）。状态倒退、已过期/已回答投递和陈旧 revision 均拒绝。

ACK 成功返回 `status: "UPDATED"`、`message_id`、`delivery_id`、`state`、
新 `revision`、`event_id`；无操作响应不含 `event_id`。父服务重放已提交 ACK 时，
即使原 expected revision 已陈旧，仍返回原结果；使用旧 revision 的新命令则失败。

TTL 在 `now >= expires_at` 时生效。未读投递（`held`、`sending`、`pushed`、
`notified`）的有效状态变为 `expired`，消息本身保留。截止时间存储在 SQLite 中；
纯读取通过投影表现过期，不写数据库。Active controller 可以用 `expire_deliveries`
在调用方事务中持久化本项目的未读过期状态、递增 revision 并写 `message.expired`
事件。返回 `{status: "EXPIRED", expired_count}`；重复维护不会再次修改已过期投递。

已读、已确认、已回答投递不会被维护函数过期。但问题超过截止时间后一律不能回答，
即使此前已读或已确认。此前已读投递仍可在消息截止时间后确认收悉；这只记录 ACK，
不构成答案。历史投递 ACK 校验收件人/revision，不授予新的 Assignment 执行权限。

## 纯收件箱读取

`read_messages` 返回 `{messages: [...], limit, offset}`。每项包括消息存储字段、
`delivery_id`、`recipient_agent_id`、`state`（考虑 TTL 的有效状态）、`stored_state`、
`revision`（用于 ACK 的投递 revision）、`message_revision`、收悉时间以及
`message_expired`。

支持 `message_id`、`delivery_id`、`thread_id`、`task_id`、`assignment_id`、
`kind`、`state`、`limit`（1..500，默认 100）、`offset`（非负，默认 0）过滤。
offset 的范围限制为非负 64 位有符号整数。
按投递创建时间和 ID 稳定排序，状态过滤先于分页。任何角色只能读取直接发给自己的投递，
没有发件箱或隐含的 controller 全局收件箱权限。其他项目/其他收件人的 ID 返回空结果。
可使用 SQLite 只读连接；读取不标记已读、不更新心跳、不持久化过期、不写事件或 revision。
拒绝 `mark_read`、收件人覆盖和未知字段。

## Steering 能力边界

领域逻辑读取已接受注册表中的 `steer` 能力。Supported 记录必须匹配当前 harness 版本、
OS、host，具备合格证据来源和带时区且不晚于当前时间的检查时间，并且证据文件存在、
SHA-256 仍匹配。缺失、unknown、陈旧、自声明或不可读证据在消息快照中保持
`capability_level: "unknown"`，不会改写注册表。

- 支持且证据有效：`steer_status: "queued"`，`capability_level: "supported"`。
- 缺失/未经验证：`steer_status: "queued"`，`capability_level: "unknown"`。
- 上下文匹配且明确不支持：`steer_status: "unsupported"`，`capability_level: "unsupported"`。

响应还包含 `capability_reason`。每条 steer 都保留为持久化收件箱投递；`queued` 只表示
收件人可以读取，不表示已发往 harness 或应用到进程。模块不调用传输、子进程或 provider，
不伪造注入，不改变任务状态，也不自动重试。

## 范围和验证

已实现直接注册 Agent ID。Human/task/run 地址别名、频道、gate/审批执行、推送传输和
长轮询尚未实现，不支持的载荷/地址形式会明确拒绝。Owner 需要用服务授权的注册身份表示。
消息载荷不能设置身份、command ID、epoch 或 gate。应用服务已把
`awo_message_send`、`awo_message_read`、`awo_message_ack` 接入现有 MCP/HTTP 目录、
`api` CLI 和命令重放路径。任务或运行作用域凭据只能操作记录中 Assignment 精确匹配的
投递；Assignment 为 NULL 的 task-only 消息不能通过这类凭据读取、ACK、回复或发送，
durable replay 也不能绕过该检查。

```powershell
python -B scripts/awo_service.py --state STATE api --project-root ROOT --token-file TOKEN --tool awo_message_send --arguments ARGS.json
python -B scripts/awo_service.py --state STATE api --project-root ROOT --token-file TOKEN --tool awo_message_read --arguments ARGS.json
```

运行真实 SQLite 测试：

```sh
python -B -m unittest discover -s scripts -p "test_agent_messages.py" -v
python -B scripts/check_encoding.py
```

测试覆盖四角色矩阵、无效 principal/收件人/绑定、严格 ACL、七种消息、单答案 SQL 约束
和多连接竞争、TTL/只读纯度、ACK revision、事件原子回滚、真实能力边界、父服务重放
作用域，以及新 Python 进程中的未读/ACK/重放持久化。这些是离线领域测试，不能视为
真实 harness 注入或已安装服务端点的证据。
