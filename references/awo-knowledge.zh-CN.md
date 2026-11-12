# 本地知识与文档

[English](awo-knowledge.md) | 中文

`scripts/project_knowledge.py` 是仅使用 Python 标准库的领域模块，沿用现有
SQLite 单一权威。父服务集成与最终集成验收另行执行。本模块没有外部写入、
远程传输、provider 连接、UI 或第二套数据库，也没有复制 Dispatch 代码。

## API 与事务契约

```python
create_knowledge_schema(con)
apply_command(con, project_id, principal, tool, payload)
query(con, project_id, principal, query, payload)
```

可信服务提供 `principal = {role, principal_id, agent_id}`。人类
controller/reviewer/observer/advisor 的 agent_id 可以为 null；worker 必须有
已注册的 Agent 身份。若 principal 带有 project_id，则必须与调用项目一致。
Agent 必须存在于本项目的 `v2_agent` 中、处于 active 生命周期且角色匹配；
可信 observer 凭据可以将已有 Agent 的权限缩减为只读。所有权和独立性同时
比较 principal ID 与 agent ID，更换会话名称不能绕过 Agent 身份检查。

父服务负责认证、项目授权、envelope 授权、deadline、freeze/epoch 隔离、
命令重放/幂等和服务日志。本模块另外检查领域角色、所有权、项目隔离、
实体当前修订号和独立证据复核。不得直接用工具 payload 构造可信 principal。

建表与命令都要求调用方已开启事务；写操作使用 `BEGIN IMMEDIATE`。
模块不调用 begin/commit/rollback 或 `executescript`。任何错误都应由父服务
回滚整个命令及其服务 envelope。成功变更在同一事务内追加领域审计记录；
父服务在回滚后记录失败命令。并发写入时，调用方应使用读事务获得一致的
多次查询快照。

仅在父服务显式迁移中调用建表函数。它添加 `v2_knowledge*` 表和只追加历史
触发器，不更改父 schema 版本或 registry/service 表。版本 1 建表可以重复
调用；已有不同模块版本会被拒绝。查询不会创建或修复 schema。支持 tuple
与 `sqlite3.Row` 连接，且不修改 row_factory。

`KnowledgeError(ValueError)` 提供稳定的 `code` 和可读消息。错误码包括：
`invalid_payload`、`invalid_principal`、`wrong_project`、`not_found`、
`forbidden_role`、`forbidden_owner`、`revision_conflict`、`state_conflict`、
`independent_review_required`、`evidence_required`、`evidence_changed`、
`evidence_unavailable`、`unsafe_source`、`stale_knowledge`、`integrity_error`、
`transaction_required`、`schema_conflict`、`unknown_command`、`unknown_query`。
SQLite 错误也向事务所有者传播。

## 命令与 payload

精确命令名由 `COMMANDS` 导出；允许可选的 `awo_` 前缀。worker、reviewer、
controller 可创建提案；observer/advisor 不能写入。创建操作可带
`expected_revision: 0`。修改已有实体必须带当前正整数 `expected_revision`，
不接受布尔值或数字字符串。来源撤销检查来源自身的修订号。父服务重放必须
返回先前结果，而不是再次执行变更。

| 命令 | Payload 与门禁 |
|---|---|
| `knowledge_propose` | 完整知识内容；始终创建为 proposed |
| `knowledge_revise` | knowledge_id、expected_revision、完整替换内容；所有者或项目 controller |
| `knowledge_review` | ID、expected_revision、revision_sha256、evidence_sha256、verdict、comment、evidence_reviewed；独立 reviewer/controller |
| `knowledge_promote` | ID、expected_revision、review_id、revision_sha256；独立 reviewer/controller |
| `knowledge_retire` | ID、expected_revision、reason；所有者或项目 controller |
| `knowledge_source_approve` | relative_path、sha256；仅 controller |
| `knowledge_source_revoke` | source_id、expected_revision、reason；仅 controller |
| `knowledge_sync_import` | proposal envelope；仅生成不可信的 team 提案 |
| `doc_import` | 完整文档内容；始终创建为 proposed |
| `doc_revise` | doc_id、expected_revision、完整替换内容；所有者或项目 controller |
| `doc_review` | 与知识评审字段相同，但 ID 为 doc_id；独立 reviewer/controller |
| `doc_publish` | doc_id、expected_revision、review_id、revision_sha256、content_sha256；仅 controller |
| `doc_retire` | doc_id、expected_revision、reason；所有者或项目 controller |

知识内容要求 `kind`、`title`、`body`、`scope`、`source`、`source_refs`。
可选字段：`confidence`（默认 unknown）、`expires_at`、`verify_after_seconds`、
`reviewer_principal_ids`、`rationale`、`alternatives`。decision 必须有非空理由
和至少一个备选方案。source 枚举为 `human|agent|doc|review|evidence|external`。

七种 kind 为 `preference|convention|constraint|hazard|decision|fact|reference`。
拒绝 personal convention。fact 必须有引用和已知 confidence
（`high|medium|low`）；高置信度不授予评审或 promotion 权限。reference 始终
是指针，不会自动变为 fact。任何 fact 无论声明何种 `source`，都必须至少有一个
可重放的本地 `file`、已登记 `evidence` 或已发布 `doc` 引用。内联文本和外部哈希
声明只是主张，不是本地证明；仅引用这些的 fact 提案会在任何评审之前以
`evidence_required` 被拒绝。`source` 是声明式来源，不是经过认证的作者身份：
`human`、`agent`、`review`、`evidence` 等标签只是提交者的声明，独立评审仍是
拒绝无法验证主张的门禁。缺少可重放字节的推断应改记为 decision、reference
或草稿提案。

文档内容要求 title、scope，且只能选择一种输入：有界内联 `content`
（可选 content_sha256），或已批准的 `source_id` 加必填 content_sha256。
同样支持过期时间、复核间隔和个人评审者字段。导入字节保存在 SQLite 中。
修订已发布文档会使其回到 proposed；旧发布哈希只保留为历史，不再是当前发布。

结果包含 entity_id、knowledge_id 或 doc_id、project_id、revision、
content_revision、state、review_id、revision_sha256、content_sha256。
来源操作返回 source_id、来源 revision 和 state。实体 revision 在每次状态
变更时递增；content_revision 仅在内容修订时递增。

## 哈希、独立复核与发布

Content SHA-256 覆盖精确 UTF-8 正文字节，不归一化换行。Revision SHA-256
以排序、紧凑、ASCII 转义的 JSON 绑定完整规范化 manifest 与 provenance。
Evidence SHA-256 绑定正文哈希和有序来源引用。修订、评审、导入 envelope
及审计历史在 SQLite 中只追加。未知 payload 字段和 author/state 覆盖被拒绝。

所有知识 kind 都遵循 proposed -> reviewed -> active；文档遵循
proposed -> reviewed -> published。拒绝评审使条目变为 rejected 且不可用。
评审本身不会 promotion 或发布。任何修订都会清除评审和复核时间，包括正文
字节不变的元数据修订。所有历史修订作者均不得评审或 promotion 该知识。

接受评审必须显式给出 `evidence_reviewed: true`、非空 comment，以及 get
返回的当前 revision/evidence 精确哈希。评审和 promotion/发布时会重新检查
所有本地引用。fact 至少需要一个本地可验证引用；仅提供外部 URL 和声称的
哈希不够。评审者仍需判断证据是否支持断言，字节匹配本身不等于事实成立。

已登记证据的生产者和被引用文档的作者不能评审依赖自身证据的 fact。
controller 可以在他人独立评审后发布自己创建的文档，但不能自审。知识的
promotion 操作者也必须独立于全部知识修订作者。Promotion 会重新检查
Agent 评审者仍处于 active 生命周期且角色未变。

发布仅在 SQLite 中记录已复核哈希，不写入 Word 文件、仓库文件、远程文档、
网站或其他外部发布目标。

## 提交溯源与声明式来源

每个新修订在 manifest 旁保存服务端生成的溯源：`submitted_by`（principal_id、
agent_id、role、binding_id、binding_version）、`source_assertion`（`declared`
标签及 `authenticated: false`），以及原有的 type/origin 字段。这些值来自经过
认证的服务 principal 映射，绝不取自 payload；未知的 author/role/binding 字段
按 invalid payload 拒绝。修订会为实际提交该修订的 principal 刷新
`submitted_by`，并保留同步 origin、`supersedes_sha256` 等既有结构字段。

投影新增 `source_authenticated`（始终为 false）、`source_assertion`、
`submitted_by` 和 `provenance_class`（有提交者记录时为
`authenticated-submission`；此元数据出现之前的旧行为 `legacy-unknown`）。
声明式 human 来源无法被认证：没有知识命令接受 Owner 或人类凭据，Owner 凭据
是交互式的、仅限监督命令。即使经过独立评审，`source` 仍是声明。合法的人类
来源主张可以配合可重放的本地证据记录，但标签本身永远不是证明。

在此元数据之前写入的行保留精确字节、哈希和原有衰减行为，绝不静默改标签。
处于 active 的纯内联旧 fact 保持既有可用性；但只要仍只依赖内联或外部引用，
它就不能被重新评审、promotion 或修订，因为这些门禁都会执行可重放证据规则。旧修订可能没有
`submitted_by`；该状态被显式记录，不会被视为认证。

重放命令对同一 principal、command_id 和 payload 哈希返回已记录结果；用相同
ID 提交不同字节会得到幂等冲突，因此重放不能附加新的溯源。已撤销或过期的
凭据在查询重放之前即认证失败。

## 证据与批准的文件

source_refs 是 1..16 个互异、有序的对象，每个对象都带小写 sha256：

| kind | 其他必填字段 | 验证方式 |
|---|---|---|
| inline | label、content | 对保留的 UTF-8 字节计算哈希 |
| file | source_id | 重新读取已批准的有界路径并核对哈希 |
| evidence | evidence_id、source_id | 实际字节及项目/task/assignment 所有权、sealed/current 哈希、状态和登记路径 |
| doc | doc_id、content_revision | 当前 fresh 且已发布的文档，正文哈希必须一致 |
| external | uri | 不可信指针；不抓取，不作为本地证明 |

外部 URI 支持 http://、https://、urn:、awo-local:。来源变更、丢失或撤销会
关闭写入门禁，并使读取投影变为 stale。

只有 controller 可以批准精确的项目相对路径和哈希。唯一根目录是服务管理的
`v2_project.root_path`，payload 不能覆盖它。父服务必须填入预期的本地根目录。
若部署把根目录设为状态目录，则在父服务显式配置其他根目录之前，应使用内联
文本或该目录内的文件。批准后，项目成员可引用这些字节；不要把私有文件批准为
共享项目来源。

模块拒绝绝对路径、遍历、UNC、设备、备用数据流、反斜杠、不明确的 Windows
名称、Git 内部路径、符号链接、junction/reparse point、硬链接、非常规文件及
超限文件。读取前后比较文件身份、大小、mtime 和同一 API 内的 ctime，再核对
内容哈希。Windows stat/fstat 的 ctime 语义不要求彼此相同。文件系统与 SQLite
不是同一个事务；后续文件变更会在下次查询或门禁检查时体现为 stale。这些检查
并非用于抵御本地管理员竞态替换路径的操作系统沙箱。

## Scope、查询、衰减与退休

Scope 为 personal|project|team，始终局限于一个项目。team 是本地可见性与导出
范围，不表示远程成员关系。personal 条目仅对所有者及显式指定的
reviewer/controller principal ID 可见；controller 没有通用私有内容访问权。
指定评审者可 review/promote，但不能编辑他人 personal 条目。Scope 不可变，
改变范围需要新建并单独评审提案。

精确查询名由 `QUERIES` 导出：

- knowledge_get / doc_get：对应 ID，可选 content_revision 读取保留的历史快照；
  历史结果不可用，也不会被重新激活。
- knowledge_list / doc_list：可选 limit、after_id、scope，以及知识 kind；返回
  元数据与 next_after_id。
- knowledge_history / doc_history：对应 ID，可选 limit/after_seq；返回审计事件、
  关联评审和 next_after_seq。
- knowledge_sync_export：team 条目的 knowledge_id；返回可审计的 proposal
  envelope，不是命令，也不是远端 active 记录。

查询不写入 recall 计数、过期状态或 schema。页大小默认 50，范围 1..100。
先过滤可见性再分页，私有 ID 不会通过游标泄漏。Kind/scope 过滤作用于有界
可见页，稀疏页或空页仍可能带后续游标。

复核间隔默认 30 天，允许 1 秒至 365 天。过期时间必须带时区并规范化为 UTC。
只有接受评审会设置 last_verified_at。投影返回 decay=fresh|stale|expired、
stale_reasons、effective_state、usable。Stale 的 active/published 记录保留
存储状态，但 effective_state=stale、usable=false。消费者必须使用投影，
不能只读取 active 标志。

Fresh 且接受的条目具有 trust=confirmed；其他情况下，来源身份为 human 或
agent。外部引用（包括混合来源条目）及同步 provenance 即使经过本地评审，
仍保留 source_trust=untrusted。Confidence 独立于证明，
confidence_is_proof=false。已过期内容需要新修订；仅复核时间过久的内容可
通过独立复核和显式 promotion/发布续期。退休保留全部历史且不可恢复。

投影还会返回 `source`、`source_authenticated`、`source_assertion`、
`submitted_by`、`provenance_class`、`expires_at`、`verify_after_seconds`、
`author_principal_id`、`author_agent_id`，以及当前 `review` 记录
（评审者 principal/Agent/角色、verdict、reviewed_at），该记录可能是拒绝结论。
每个修订行保留该修订自己的作者与 manifest；`knowledge_get` 显式指定
`content_revision` 时返回该修订的 source、作者、`expires_at`、
`verify_after_seconds` 与 `evidence_sha256`，但其 `review` 为 `None`，
因为 review 指针属于当前修订。`knowledge_history` 返回审计事件，
仅在动作指向评审时附带该评审记录。

## 提案交换边界

格式 `awo.knowledge.proposal.v1` 包含 project_id、origin、entry、sha256。
Envelope 摘要覆盖除自身 sha256 之外的全部字段。Origin 保留实体 ID、内容
修订号、修订哈希、导出者和导出时间。本地 file/evidence/doc 引用转换为不可信
的 awo-local 指针，有界内联证据予以保留。导出是纯读取，其自包含 envelope
就是审计产物；传输及访问日志属于父服务职责。

导入验证项目、schema、大小、哈希和类型，保存完整原始 envelope 与导入者，
并创建由本地导入者拥有的新提案。只接受 team/external 条目。拒绝远端本地
来源 ID、评审者分配、作者覆盖、active 状态、review ID 和 trust 覆盖。
相同 envelope 摘要不能重复导入。同一 origin 实体与内容修订号如果以不同
envelope 出现，会作为另一份不可信提案被接受并标记 `sync.origin_conflict: true`；
模块不做冲突解决，两份副本都必须经过本地评审才能激活。fact 条目会在导入时以
`evidence_required` 被拒绝：导出会把本地 file/evidence/doc 引用降级为外部指针，
而内联或外部引用不是本地证明。team fact 必须在本地重新提案、附带可重放证据，
并通过本地独立评审；经过认证的导入者是本地提交者，不是原始作者。

哈希检测 envelope 变更，不认证发送者。修改内容并重新计算摘要只会产生另一份
不可信提案，仍需本地独立证据复核和显式 promotion。仅导出本地来源指针的证据
必须补充本地可验证证据才能接受 fact。本模块没有传输、凭据处理、peer 发现、
冲突解决、签名验证或 federation。

## 经过认证的应用入口

工作流状态目录与项目内容目录分开。显式升级 schema 后，由 Controller 使用普通
command envelope 一次性绑定内容目录：

~~~json
{"command_id":"bind-content-root-001","tool":"project.bind_root","expected_epoch":1,"payload":{"project_root":"E:/projects/example","expected_revision":0}}
~~~

替换为当前实际 epoch，保存到文件后执行 awo_service.py --state STATE command
--actor ACTOR --version VERSION --envelope FILE。目录绑定不可变；恢复结果时重放
同一个 command_id。这个操作不移动文件，也不更改旧工作流状态目录。

通过 access-issue 签发绑定项目目录的凭据。Reviewer 凭据指定 knowledge_id
（知识或文档 ID）及当前具备审核权限的 binding。服务保留真实 Agent 身份，
缩小权限后的凭据也不能审核该 Agent 自己的内容。Task/run 凭据不授予知识访问权。
本地来源审批只读取显式绑定的内容目录。

~~~powershell
python -B scripts/awo_service.py --state STATE extension --project-root ROOT --token-file TOKEN --tool doc_import --arguments ARGS.json
python -B scripts/service_http.py --state STATE --project-root ROOT --extensions-enabled
~~~

扩展参数包含 payload；写操作另需 command_id 和 expected_epoch。本文的命令
payload 不变。CLI 与显式开启的 HTTP/WS 共用认证和持久幂等记录。MCP 核心工具
仍为 13 个，开启消息后为 16 个；项目扩展另行提供。发布只记录经过审核的 SQLite
文档，不进行网络上传。除领域测试外，运行 test_service_extensions.py 可验证实际
应用入口和协议链路。

## 上限与测试

规范 JSON payload 上限 1 MiB；文档/文件字节上限 256 KiB；知识正文及每份
内联证据正文上限 64 KiB。最多 16 个引用、8 位个人评审者、16 个备选方案。
内容限制按 UTF-8 字节计数；payload 限制按规范 ASCII 转义 JSON 字节计数。
校验字符串、时间戳、整数修订号、未知字段和有限 JSON 值。

```powershell
python -X utf8 -B -m unittest discover -s scripts -p "test_project_knowledge.py" -v
python -B scripts/check_encoding.py
```

测试使用真实 SQLite、已接受的父服务 DDL 和隔离本地文件。
P1KnowledgeTests 覆盖 P1-5 最小知识、证据/评审/promotion、变更证据、修订、
衰减与退休。DomainBoundaryTests 覆盖事务、schema、principal、项目、所有权、
并发和只追加历史。P2DocumentTests 覆盖 P2-1 文档工作流及文件安全；
P2MemoryTests 覆盖 P2-2 kind/scope/隐私/提案交换、fact 证据门禁、
投影溯源，以及同步冲突/过期/退休。ProvenanceTests 覆盖全部声明式来源标签、
服务端提交元数据、修订改标签、legacy-unknown 行和同步导入者溯源。应用套件
（test_service_extensions.py）增加真实 CLI/gateway/SQLite 的恶意来源标签拒绝、
重放幂等、凭据撤销顺序和限定实体拒绝。这些测试不构成生产迁移或 federation
验收。若宿主缺少创建符号链接的权限，该真实文件系统
测试会明确跳过；硬链接和文件变更检查仍会执行。
