# 本地监督与配对外部草稿

[English](awo-supervision.md) | 中文

标准库模块 [supervision.py](../scripts/supervision.py) 实现 P2-3 只读建议和受限
controller 入口，以及 P2-5 本地配对草稿接收。唯一持久权威是既有工作流 SQLite
账本。模块不运行模型、不发送外部消息、不调度 worker、不合并、不安装 provider，
也不配置客户端。核心 MCP 仍为 13 个工具；仅显式启用消息时为 16 个。监督属于父服务
的独立扩展。

## 集成与事务所有权

父服务在自己的 schema 事务内显式调用 create_supervision_schema(con)，前提是父服务
账本 schema 已存在。六张模块表都使用 v2_supervision_ 前缀：meta、owner_identity、
entry、approval、pair 和 draft。建表不会安装 Owner 身份。域函数不打开连接、不提交、
不回滚、不执行隐式迁移。连接使用 sqlite3.Row；写入由调用方 BEGIN IMMEDIATE，
只读连接可使用 BEGIN。任一步失败，调用方必须回滚整条命令。

父服务为 agent 调用传入完整 service_access.authenticate 结果。模块再次检查凭据记录、
活跃 agent、项目和解析后的根路径、作用域、当前 binding 和 epoch。payload 中的身份或
角色字段不能覆盖授权身份；不能删除或扩大 task、assignment、generation 作用域。
knowledge 作用域凭据不能访问任何 agent 监督入口。认证 principal 或持久 access-token
记录中，只要 knowledge_id 非空就拒绝，缓存 principal 省略该字段也不能扩大权限。
兼容旧 schema 缺少该列的情况；字段不存在或为 NULL 时保留既有访问语义。

普通 apply_command 操作的 command_id、expected_epoch、持久重放、错误映射和拒绝审计
由父服务负责。成功的域写入在同一账本追加事件；查询不会写审计。
controller_entry 和 convert_draft 则在同一连接上，通过 service_api 的真实授权和变更
逻辑及 v2_command_log 执行内嵌的既有服务命令，保留原 command_id 和参数。因此直接
gateway、子进程和 Overseer 共用重放结果。不要以同一个 ID 再记录另一名称的外层命令。
如扩展需要外层信封，使用独立外层 ID，并在外层重放前重新检查所有当前授权门禁。

## Owner 身份由父服务认证

基线 agent token 服务没有 Owner 角色。父服务必须提供独立认证的本地 Owner 设置和
决策接口。模块没有人类身份注册方法或 CLI，不签发凭据，也没有 owner_approved 布尔
开关。测试只在隔离 fixture 中建立 Owner 行，用于验证这个集成契约；不代表已实现
生产 Owner 设置流程。

父服务向 v2_supervision_owner_identity 写入以下确切字段：

| 字段 | 类型与含义 |
| --- | --- |
| project_id | TEXT，引用既有 v2_project |
| principal_id | TEXT，owner: 后接 1..100 个安全标识字符 |
| auth_generation | INTEGER，大于零 |
| linked_agent_id | 可空 TEXT，表示与该 Owner 不独立的 agent |
| expires_at | INTEGER，UTC Unix 秒 |
| revoked_at | 可空 INTEGER，UTC Unix 秒 |

主键为 (project_id, principal_id)。principal_id 满足 owner:[A-Za-z0-9._:-]{1,100}。
父服务在自己的凭据表保存 Owner secret 哈希，先认证，再构造以下 Python 映射：

    {'role':'owner','principal_id':'owner:local-human','project_id':'...',
     'auth_generation':1,'agent_id':None}

身份必须未撤销、未过期，且认证 generation 一致。不能由 agent 或外部 payload 合成
Owner 凭据。增加 auth_generation、撤销或使身份过期，都会使它签发的 binding、approval
和 pair 失效。父服务另外检查每条 Owner 凭据的撤销和过期。Owner 不得绑定或批准其
linked_agent_id 对应的 controller；独立人类 Owner 的 linked_agent_id 可以为 NULL。

## Advisor 与 controller 入口

overseer(..., mode='advisor') 是默认模式，只允许 awo_task_get、awo_task_list 和
awo_assignment_get，并保持现有服务的作用域限制。即使底层凭据是 controller，也拒绝
所有写命令。

query(con, project_id, principal, operation, payload) 支持：

- advisor_snapshot：有界作用域内的任务、当前 epoch 和只读状态；参数为 {limit?,after?}。
- advisor_suggest：{goal}；根据最多 200 个可见任务状态生成确定性规则建议，标注
  unapproved_draft 和 persisted=false，不声称模型推理或隐藏决策者。
- draft_get：{draft_id}，返回明确标为不可信的提案内容。
- draft_list 和 pair_list：{limit?,after?}，仅限项目级 controller/observer 凭据，最多 200 条。

controller_entry 要求 Owner 显式建立的活跃 controller agent、binding ID/version、epoch
和有效期绑定。仅允许 main context 和无 task/assignment 限定的 controller 凭据；仅可
委托四个已实现服务操作：awo_task_create、awo_assignment_claim、awo_resource_claim、
awo_resource_release。每次调用都需要独立 Owner 对确切命令的批准，覆盖 entry revision、
工具、完整参数、原 command_id、expected_epoch 和适用的 expected_revision。
既有服务继续执行准入、所有权、容量和闭环门禁；本模块不会制造入队授权。
assignment claim 可以持久返回 WAITING 而不建立 reservation。即使后来准入条件变化，
原命令重放仍返回 WAITING；新尝试需要新的 command_id、当前 revision 和新的精确 Owner 批准。

不暴露 review 提交、自审、accept/land/merge、pause/cancel 或 send_as_you。未知操作
直接拒绝。重新绑定增加 entry revision，不会恢复旧批准。重放前也检查撤销和过期；
历史成功结果不等于当前授权。

仅 Owner 可调用的 apply_command 操作及严格参数：

| 操作 | payload |
| --- | --- |
| supervision_bind | agent_id、binding_id、binding_version、expected_epoch、expires_at；替换时必须带 expected_revision |
| supervision_revoke_binding | agent_id、expected_revision |
| supervision_approve | approval_id、intent、expires_at |
| supervision_revoke_approval | approval_id、expected_revision |
| supervision_pair | pair_id、local_id、peer_id、key_sha256、expires_at |
| supervision_revoke_pair | pair_id、expected_revision |

command_intent 只读构造精确命令说明，用于向 Owner 展示。批准是另一次独立认证操作；
构造或展示 intent 本身不授予权限。所有未知字段均被拒绝。

## 有界配对草稿协议

协议明确标为 awo.paired-draft.v1，是本地 AWO 适配，不声称实现标准 A2A 或已验证
安装客户端。没有网络监听、发现、密钥交换、外部发送、流传输、远程执行或真实对端
兼容性声明。

Owner 配对绑定 project_id、pair_id、不同的 local_id/peer_id、SHA-256 密钥指纹及有效期。
父服务通过可信 secret-file 边界提供至少 32 字节随机新密钥。模块以独立于日志 payload
的 bytes 参数接受 32..128 字节密钥，只保存指纹。撤销后的 pair ID 不能复用；轮换需
新建 pair。HMAC 是共享密钥认证，不是非对称身份签名或不可抵赖证明。

信封只包含 protocol、project_id、pair_id、sender、recipient、message_id、issued_at、
expires_at、payload 和 signature；payload 只包含 title 和 body。时间为 UTC Unix
整数秒。签名是小写十六进制 HMAC-SHA256，输入为 protocol + LF + 去掉 signature 后
的规范 JSON。规范化使用 Python json.dumps 的 sort_keys=True、separators=(',', ':')、
ensure_ascii=True、allow_nan=False；不接受浮点数。make_envelope 在本地构造信封；
decode_envelope 拒绝重复 key、无效 UTF-8、非有限数、超限字节/深度和不支持的类型。

receive_envelope 要求当前已认证的本地 controller，写入前检查 pair 和 Owner 撤销、
项目、双方身份、密钥指纹、签名及时间限制。同 pair/message ID 且规范信封相同则返回
已有 draft；不同内容冲突。接收只创建 v2_supervision_draft 不可信提案，不创建 Task、
Assignment、dispatch intent 或外部消息。不执行内容、不将其当作指令，也不访问其中
的 URL 或路径。

conversion_intent 接受 draft_id、expected_revision=1、command_id、expected_epoch，
可选 task_id。它将原信封哈希和签名 title/body 绑定到精确的 single/P3 草稿 Task 命令。
Owner 单独批准该 intent。convert_draft 增加 approval_id，重新检查全部门禁，再在
同一连接调用真实服务。Task 创建、命令回执、批准使用和 draft 转换一起提交或回滚。
不同并发命令最多转换同一个 draft 一次。结果 Task 保持 draft；转换不授予 dispatch
权限。pair 撤销或过期会阻止未转换提案；转换也必须发生在原信封过期之前，接收不会
延长其有效期。历史只读投影仍可查看。

## 限额与本地 CLI

| 限制 | 数值 |
| --- | --- |
| 规范协议/CLI JSON | 64 KiB |
| JSON 嵌套 | 12 层 |
| 提案 title/body | 512 / 32000 UTF-8 字节 |
| 信封有效期 / 未来时钟容差 | 900 / 30 秒 |
| pair / controller entry / approval 有效期 | 最多 24 小时 / 4 小时 / 1 小时 |
| 读取列表 / 状态规则扫描 | 200 条 |
| CLI token 文件 | 16 KiB |

CLI 默认 advisor，使用已经签发的 access-token 文件，JSON 请求文件包含 {tool,arguments}：

    python -B scripts/supervision.py --state /path/state --project-root /path/project --token-file /path/access.txt --request-file /path/read.json

读取示例的 tool 为 awo_task_list，arguments 为空 JSON 对象。已批准写操作增加
--mode controller_entry --approval-id APPROVAL_ID，并使用完全相同的已批准参数。
CLI 作为调用方拥有连接/事务，不安装表或 Owner 身份，不暴露 Owner 批准或携带配对
密钥的导入接口。token 不放入命令行参数或诊断输出；失败只返回稳定机器码。
不会自动重试命令；响应丢失时保留原 command_id 进行对账。

## Owner 凭据引导

Owner 凭据是独立的 `awo_owner_` bearer token，绑定项目内容根目录和认证代际。
引导必须在本地交互终端由本机管理员执行，绝不通过 Agent、MCP、HTTP 或草案路由：

```powershell
python -B scripts/owner_access.py --state STATE --project-root ROOT setup --actor CONTROLLER_ID --version VERSION --owner-id OWNER_ID --token-out OWNER_TOKEN.txt [--expected-generation N] [--minutes M] [--linked-agent AGENT_ID]
python -B scripts/owner_access.py --state STATE --project-root ROOT command --token-file OWNER_TOKEN.txt --envelope ENVELOPE.json
```

setup 打印精确短语 `AUTHORIZE OWNER <owner_id>` 并要求原样输入；没有终端时拒绝执行。
项目内容根必须事先绑定，linked Agent 必须属于同一项目。轮换时把当前代际传给
`--expected-generation`，并撤销更早的凭据。命令信封包含 `command_id`、`tool`、
`expected_epoch` 和 `payload`；tool 是上述监督 Owner 命令之一或 `owner.revoke`。
鉴权、过期、撤销、根目录绑定和代际都在 replay 之前检查，之前的结果不构成新的授权。
`owner.revoke` 必须给出当前 `auth_generation`。该凭据不具备任务、审核或派发权限，
只授权上面列出的监督决定。

## 验证与父服务集成

    python -B -m unittest discover -s scripts -p test_supervision.py -v
    python -B scripts/check_encoding.py

测试组合真实 ServiceAccessTests、服务 API、SQLite 和本地子进程，包括生产 gateway
backend。覆盖只读建议、精确批准、角色/身份/作用域拒绝、过期/撤销、重放、事务回滚、
签名/JSON 拒绝和进程竞争。Owner fixture 明确代表父服务认证接缝；父服务现在已在
`create_schema` 安装 supervision 与 Owner schema，并通过 `owner_access.py` 接入真实
Owner 凭据，在一次性本地 SQLite 夹具上验证 setup、轮换、撤销、过期、代际和
权限先于 replay。这些是本地夹具结果，不是生产 Owner 安装或 A2A 认证。
