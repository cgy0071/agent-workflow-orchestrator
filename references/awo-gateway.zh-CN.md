# AWO 网关与应用传输

[English](awo-gateway.md) | 中文

`scripts/mcp_gateway.py` 实现 stdio 上的旧版 MCP；`scripts/service_http.py`
实现应用层 HTTP/JSON 与 WebSocket 调用。仅使用 Python 3.10+ 标准库。
传输层不打开 SQLite，不实现业务授权，不签发凭证、不迁移数据库，也不重试命令。

## 服务边界与启用条件

主应用服务提供三个入口：

```python
gateway_principal(state, project_root, token)
gateway_tool_specs(messages_enabled=False)
gateway_call(state, project_root, token, tool, arguments)
```

认证返回非空 principal 字典或抛出异常。凭证过期/撤销、项目根绑定、角色、注册、
Assignment 所有权、revision/epoch、幂等、事务、journal 和 outbox 都由服务负责。
传输层仅验证协议结构与服务发布的 schema；工具出现在目录中不代表调用者获得授权。

生产调用在有界独立子进程中执行真实服务，不替换数据库或服务实现。凭证通过子进程
stdin 传递，不出现在命令行。目录查询和每次调用都会重新认证，包括已建立 WebSocket
上的后续调用。响应不暴露 principal。服务异常通过 `.code` 提供稳定错误码；未分类异常
返回带 trace ID 的 `internal`，不返回异常正文、堆栈或文件系统路径。在 principal 认证边界，
若服务明确抛出其拒绝异常类型但未提供更具体代码，则映射为 `unauthenticated`，不解析异常正文。

缺少服务入口或完整目录时拒绝提供工具。默认 P0 恰好包含以下 13 个工具：

| 类别 | 工具 |
| --- | --- |
| Task | `awo_task_get`、`awo_task_list`、`awo_task_create`、`awo_task_next` |
| Assignment | `awo_assignment_claim`、`awo_assignment_get`、`awo_assignment_complete` |
| Agent | `awo_agent_register`、`awo_agent_heartbeat` |
| 证据/评审 | `awo_evidence_record`、`awo_review_submit` |
| 资源 | `awo_resource_claim`、`awo_resource_release` |

只有显式指定 `--messages-enabled` 才请求最终 16 工具目录，增加 `awo_message_send`、
`awo_message_read`、`awo_message_ack`。服务必须提供全部 16 个已实现工具；消息模块可以
import 不代表已启用或已验收。默认仍为 13。没有接受结果覆盖、controller 接管或隐式取消
Assignment 的工具。完成交付不等于 accepted 或 landed。

所有写 schema 必须显式接受并要求平铺的 `command_id`、`expected_epoch`；更新操作还要求
`expected_revision`。Task/Agent 创建与 message send 属于创建操作。读 schema 拒绝
`command_id`。对象使用显式 properties 和 `additionalProperties: false`。标准库验证器
支持有界子集：基本类型、required/properties、本地 `$defs`/`$ref`、数组、范围、enum/const、
pattern、date-time/URI 格式和布尔组合。不支持的关键字或引用使目录验证失败，不静默跳过。
业务策略仍必须由服务再次验证。

## 明确的协议版本

支持 **2025-11-25 旧版 initialize 生命周期**，这是明确兼容目标，不声称是最新规范。
2026-11-10 已获取该固定版本的官方 lifecycle、transports、tools、cancellation 页面。
实现检查点另行识别了现代 `2026-07-28`；本实现不支持其 `server/discover` 生命周期。
未来 fixture 中的 `2026-11-25` 不视为支持版本。

请求支持版本时回显 `2025-11-25`；其他版本协商为 `2025-11-25`，不兼容的客户端应断开。
仅替换版本日期字符串不能实现相应协议。

支持的方法：`initialize`、`ping`、`tools/list`、`tools/call`；通知：
`notifications/initialized`、`notifications/cancelled`。仅声明 tools 能力，不声明 resources、
prompts、sampling、elicitation、任务扩展、订阅或目录变更通知。输入输出为 UTF-8 NDJSON，
每条响应一行。拒绝批量请求、重复 JSON 键、非有限数值、无效 UTF-8、过深嵌套。通知不执行
工具，也不产生响应。

官方参考：

- [旧版生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)
- [旧版传输](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [旧版工具](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [旧版取消](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)
- [WebSocket RFC 6455](https://www.rfc-editor.org/rfc/rfc6455)

## 本地启动与首次读取

使用已初始化并启用的测试项目，以及 controller 通过服务 CLI 明确签发、绑定项目根的访问
凭证文件。不要把凭证字符串放入命令行或 harness 配置。参见[服务配置](awo-service.md)和
[Agent 注册](awo-registry.md)。网关不会引导或迁移生产账本。

在仓库根运行，替换以下示例路径为实际签发路径：

```powershell
python -B scripts/mcp_gateway.py --state E:/awo-test/state --project-root E:/awo-test/project --token-file E:/awo-test/secrets/access.txt
```

省略 `--token-file` 时可通过 `AWO_AGENT_TOKEN_FILE` 提供文件路径。`--state` 和
`--project-root` 必填，不能回退到工作目录。服务可以允许绑定项目根的 enrollment grant
认证引导会话，但只授权注册，不授权普通读写；其他操作需要显式签发的访问凭证。

使用等待响应时保持 stdin 打开的 NDJSON 客户端。先发送 initialize 并读取响应，再发送
initialized 和 tools/list，之后调用工具：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"local-driver","version":"1"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"awo_task_list","arguments":{}}}
```

写操作字段以返回的 schema 为准；先通过服务读取当前 revision/epoch，不要猜测。
恢复不确定的传输结果时保留原始 arguments 与 `command_id`。完成对账后，新的意图或
不同负载才使用新命令 ID。

## HTTP 与 WebSocket 应用调用

这些是**应用传输，不是 MCP Streamable HTTP**。没有 `/mcp`、SSE、MCP HTTP 会话或
浏览器 Cookie 认证。

```powershell
python -B scripts/service_http.py --state E:/awo-test/state --project-root E:/awo-test/project --host 127.0.0.1 --port 8765
```

仅允许回环 IP 字面量（包括 `::1`）。拒绝非回环、通配地址与主机名绑定。没有 LAN 开关、
TLS 终止、代理信任、外部 Origin 例外或自动暴露。`--port 0` 选择临时端口，只把端口号写到
stderr。缺少服务时入口保持不可用；启动监听本身不授予任何调用权限。

`POST /call` 要求 `Authorization: Bearer <token>`、`Content-Type: application/json`
和单一规范 `Content-Length`。Host 必须为绑定地址及端口，或同端口 localhost；若带 Origin，
必须严格等于 `http://` 加该 Host。拒绝跨 Origin、通配 CORS、Cookie/查询参数凭证、重复安全
头、Transfer-Encoding、Expect、旧式折叠头与无效正文编码。

HTTP 请求正文和 WebSocket 文本消息使用相同信封：

```json
{"id":"query-1","tool":"awo_task_list","arguments":{}}
```

成功为 `{"id":"query-1","result":...}`；失败为
`{"id":"query-1","error":{"code":...,"message":...,"retryable":false,
"uncertain":false,"trace_id":...}}`。HTTP/WS 共享 `application_call`，不移动或更改 arguments
字段。无效传输输入不调用业务命令，适配器不重试。成功 HTTP 200；认证 401/403；输入 400；
冲突 409；容量 429；服务不可用 503；截止时间 504。

WebSocket 使用 `GET /ws`、相同 Bearer 头及 RFC 6455 version 13 握手。客户端必须能够设置
Authorization；浏览器 JavaScript WebSocket 构造器不能任意设置该头。查询参数、Cookie
和子协议不能替代认证。服务检查 key、升级头、mask、保留位/opcode、规范长度、分片顺序、
累计消息大小、UTF-8 和关闭码/原因。支持文本分片、ping/pong、close；拒绝二进制、扩展和
子协议协商。同一连接上的调用串行执行。不确定断线后以原 command_id 重连；不提供事件流。

## 限额、取消与结果

| 限额 | 数值/默认值 |
| --- | --- |
| 客户端 JSON 行/正文/消息 | 1 MiB |
| 编码后的响应 | 2 MiB |
| JSON 嵌套 | 32 层 |
| 凭证文件 | 16 KiB；服务可有更严格限制 |
| MCP 请求 ID | 至多 128 字符的字符串或安全整数 |
| MCP 并发调用/服务子进程 | 8 |
| HTTP 请求行/头总量 | 4 KiB / 16 KiB |
| HTTP/WS 连接 | 16 |
| 调用截止时间 | 30 秒；`--call-timeout` 可缩短 |
| HTTP/WS 空闲超时 | 10 秒，可配置至 30 秒 |
| HTTP/WS 绝对连接寿命 | 60 秒，可配置至 300 秒 |
| 每条 WS 文本消息分片数 | 1024 |

绝对寿命也约束持续滴入的请求头、分片和控制帧。stdio 超长输入会关闭会话，不无限排空。
stdin EOF 取消未完成调用。WebSocket 到达有限寿命后需重连。消息调用当前也受 30 秒限制，
不声称实现了 60 秒可取消长轮询。

超时或取消只终止本次服务子进程，不取消 Assignment 或独立运行的 worker。SQLite 回滚
未提交事务，但提交可能先于进程终止。因此超时、子进程异常或响应丢失意味着**结果不确定**，
不是回滚证明。调用超时返回 `retryable: false`、`uncertain: true`；MCP 取消遵循固定规范，
抑制迟到响应。先向服务对账，再对相同命令复用原 command_id；不得自动重发结果未知的外部派发。

stdout 仅供协议输出；stderr 诊断仅包含安全代码。子进程抑制服务 stdout/stderr，不返回任意
异常正文，拒绝回显当前 Bearer 凭证的响应。凭证不是 OS 隔离：具有相同文件系统权限的其他
进程仍可能读取凭证文件或账本。

## 验证边界

```powershell
python -B -m unittest discover -s scripts -p "test_mcp_gateway.py" -v
python -B -m unittest discover -s scripts -p "test_service_http.py" -v
python scripts/check_encoding.py
```

`ProtocolOnlyBackend` 是明确标记的测试夹具，仅验证 framing、schema、错误脱敏和信封转发，
不证明业务行为。`RealServiceSubprocessTests`、`RealServiceHTTPTests` 使用隔离 SQLite、
真实服务入口、真实子进程与套接字，并组合 `test_service_access.ServiceAccessTests`，覆盖
认证/根绑定、角色拒绝、重启重放、跨传输幂等、撤销、超时/取消以及错误不写入。若主服务
网关 API 缺失，集成测试直接失败。所有领域工作就绪后，主代理必须运行最终完整服务/集成测试。

原始 worker 证据保存在仓库外实现工作区的 `P0-5/worker-evidence` 与
`P1-6/worker-evidence`。版本探测不等于真实 harness 连接验证；除非另行实测已配置客户端，
Codex/Claude/Pi/DSH/OMP 的连接兼容性保持未验证。
