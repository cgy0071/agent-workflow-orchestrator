# AWO 优先级调度器

[English](awo-scheduler.md) | [应用服务](awo-service.zh-CN.md) | [预算](awo-budget.zh-CN.md)

## 当前功能

V2 调度器需显式启用，用于排队处理 Controller 批准的任务、为每个已批准 Task 槽位预留一个 Assignment，并复用现有 command/Herdr 传输、完成回执和独立审核。Task 是稳定目标；每次执行有新的 Assignment、Run、generation、attempt ID 和 nonce。队列和发送意图是同一账本中的元数据，不是第二套任务状态权威。

入队采用既有目录契约。普通任务沿用槽位 0 的单次执行；显式协作策略可通过同一个原生运行时启用有界 Fanout 或 Ensemble 槽位。调度器本身不创建 Worktree，由独立工作区服务负责。Steering、抢占及已审核代码物化不属于当前调度器功能。代码上游即使 accepted，也须等待后续物化门禁；非代码产物在字节与独立审核重新校验后可解锁下游。目录 Claim 不等于操作系统安全沙箱。

## 配置和命令

使用 Python 3.10+。按[配置指南](setup-and-switching.zh-CN.md)初始化工作流，核实 Controller、Worker、Reviewer 绑定，再按[服务指南](awo-service.zh-CN.md)迁移并显式启用 V2。已有 V2 自主管理的状态应使用受控的 schema-upgrade 做增量升级，不能重复导入旧状态。

入队前，为实际 Worker 的注册身份记录 send/status/collect 的 supported 能力证据，包括可读取的证据文件及哈希、匹配的 Harness 版本、主机和操作系统。额外必需能力累加检查，unknown 不放行。初期执行身份必须是从同一个实际 Worker binding 导入的 Agent；同 Harness 的另一个 Agent 或新注册身份不能替代。新注册身份需要后续实现并由 Controller 确认的绑定桥接，才可代表该会话执行。

Controller 若在预留后绑定了 managed Workspace，派发会延迟加载工作区模块，检查 Assignment、主机和内容指纹，在提交 attempted 的同一事务内标为 active。第二次发送前检查仅对该 attempted 意图允许 active。交付目录 request.cwd 保持独立。审核调用 workspace_runtime.review_gate 并传入 verdict，记录返回的 inventory；accept 要求范围合规。接受结案和下游消费前调用 verify_reviewed_workspace 检查审核后变更。未托管目录不调用这些可选钩子。

    python scripts/awo_service.py --state runtime/example command --actor CONTROLLER_ID --version 1 --envelope enqueue.json
    python scripts/awo_service.py --state runtime/example command --actor CONTROLLER_ID --version 1 --envelope claim.json
    python scripts/awo_service.py --state runtime/example scheduler-queue --actor CONTROLLER_ID --version 1
    python scripts/awo_service.py --state runtime/example assignment-dispatch --actor CONTROLLER_ID --version 1 --assignment ASSIGNMENT_ID --expected-epoch EPOCH
    python scripts/awo_service.py --state runtime/example assignment-reconcile --actor CONTROLLER_ID --version 1 --assignment ASSIGNMENT_ID --expected-epoch EPOCH

将 ID、epoch 和 version 替换为实际值。信封包含 command_id、expected_epoch、tool 和 payload。相同信封可获取幂等重放结果。Claim 只预留；Dispatch 才发送一次。不要再通过旧 CLI 派发同一请求。

## 集成契约

应用服务认证角色、项目、上下文、Controller version/epoch 和命令重放，并持有外层 BEGIN IMMEDIATE。以下函数不 commit，也不另开连接：

    create_scheduler_schema(con)
    apply_command(con, project_id, controller_actor, tool, payload)
    queue_status(con, project_id, now=None)
    next_task(con, project_id, agent_id=None, now=None)
    assignment_get(con, project_id, assignment_id)
    claim_for_agent(con, project_id, agent_id, payload=None)
    complete_for_agent(con, project_id, agent_id, assignment_id, *, transport=None)
    review_in_transaction(con, project_id, actor, version, context, event_id, evidence_path, verdict)
    reservation_status(con, project_id, assignment_id)
    reconcile_in_transaction(con, project_id, controller_actor, assignment_id, *, transport=None)

controller_actor 必须是已经验证的 Controller binding ID，不能从 Worker 提交的负载取得。服务从认证 principal 推导权限范围。apply_command 重查 active Controller 和启用/冻结状态；查询不写入、不轮询 Harness。

| Tool | Payload |
|---|---|
| scheduler.enqueue | 必填 task_id、expected_revision、request、message、agent_id、budget；可选 required_capabilities、resources、dependencies、workspace_mode、task_kind、due_at、dag_run、dag_node、slot_id。已配置协作策略的 Task 还须提供 expected_collaboration_revision。 |
| scheduler.claim | 空对象，或 agent_id 筛选及成对的 task_id / expected_revision；可选 slot_id 须与该 Task/revision 对一起提供，只能缩小已批准队列范围。 |
| scheduler.priority | task_id、expected_revision、priority、reason；P0 另需未来的 expires_at。 |
| assignment.close | assignment_id、expected_revision、outcome；审核结案需 event_id，取消需 evidence_path。结案为 accepted、repair_required、rejected 或 cancelled。 |

request 沿用现有完整请求结构，dispatch_confirmed 为 false，包含显式派发授权、冻结的绑定规格和交付路径。request.task_id 是 attempt ID，可与稳定 task_id 不同。每次执行使用新 nonce 和交付目录。generation 未填时使用下一代；显式填写须一致。message 须绑定 attempt ID 和 nonce。budget 是维度与估算值映射，例如 {"tokens.input": 1000}；设置上限的每个维度都要提供估算。dependencies 使用稳定 Task ID。workspace_mode 默认 directory；task_kind 默认为 artifact，也可为 code。既有冻结 DAG 节点须同时提供 dag_run 和 dag_node。

MCP task.next 调用 next_task，只读检查依赖、证据、能力、批准、容量和预算。CANDIDATE 仍是临时候选，明确等待现场 Worker 检查。最终 Claim 在一个事务内重验所有门禁，包括身份、baseline、空闲状态和实际预算预留。

Worker assignment.claim 调用 claim_for_agent，只接受可选且成对的 task_id / expected_revision 以及可选 slot_id，从队列推导已批准 Controller 并核对冻结的 version/epoch。Controller 代理调用 scheduler.claim。成功返回 RESERVED，包含 assignment_id、run_id、generation、slot_id、revision、task_revision 和 reservation_ids；WAITING 包含等待原因。二者都不发送任务。

assignment.complete 调用 complete_for_agent，核验 Agent 所有权后请求真实观察，不能提供自报完成回执。没有另一张可独立修改的资源 Claim 表；reservation_status 展示意图中冻结的资源、目录 Claim 和现有预算预留。网关 Claim 可确认这些预留；holds_slot=1 时 Release 必须拒绝。

## 协作槽位与独立择优

使用这些钩子前，须通过应用服务创建或升级协作域 Schema。Controller 在 Task 为 draft/ready 时配置策略，冻结不同的已验证 Worker、有界尝试/返工次数及验证器。仅修改 execution_mode 不会启用协作。

每个已批准槽位单独入队。例如额外字段为：

    {"slot_id": 1, "expected_revision": 2, "expected_collaboration_revision": 1}

这不是完整入队负载。expected_revision 是当前 Task 修订号，expected_collaboration_revision 是独立的策略修订号；每次修改后重新读取。入队还返回 collaboration_ticket_id 和 collaboration_revision。generation 在整个 Task 内分配，包含尚未派发的 Ticket，因此不同槽位可逆序领取。采用旧协议 follow-up 创建返工时可省略 request.generation，由 Controller 队列分配下一代；显式填写必须匹配。返工仍须消费同槽位上一代已经权威结案的原始封存 repair 审核，并使用新的 attempt ID 与 nonce。

队列主键为 (task_id,slot_id)。升级在调用方事务内把旧记录保留为槽位 0，原批准内容、等待时间和通知标记不变。其他槽位运行或待审核时，待领取槽位仍可见。优先级只影响待领取项，不中断执行。调度器自身进展只延续仍匹配的修订门禁；外部 Task 修改造成的过期不会被其他槽位进展修复。各槽位共享 Task 依赖和产物类型，交付目录与 Worker 目录必须分离，每个槽位独立计入累计预算预留。

调度器调用 collaboration_runtime 的 prepare_enqueue、check_admission、admit_in_transaction。最后一个函数唯一地创建 Assignment 和预算预留；原生 tasks/map/发送意图在同一事务中补齐，候选项任何拒绝都整体回滚。发送前再次核对不可变 Ticket 与原生安全门禁，不另建派发器。选定候选胜者后，未启动的竞争槽位停止放行，但保留占用。

review_guard 排除所有冻结的竞争者和综合者身份，包括历史别名；reviews/events 仍是原审核权威。执行完成或独立审核通过不自动接受任务：Controller 须先取得成功的有界验证并显式择优。Ensemble 只能选择综合槽位，并绑定来自不同竞争槽位的冻结贡献回执。collaboration_verify 只登记意图；独立的 run_verification 在启动进程前提交 attempted。调度器不启动验证器，也不重试不确定的验证。

Controller assignment.close 调用 acceptance_guard 和 closure_projection，再核对原生审核、产物哈希、托管工作区封存以及实际 Worker 静止状态。结案只释放当前槽位；其他槽位的完成、返工、拒绝或取消不得覆盖已接受胜者及其证据。其他槽位保留 Claim 和未知预算，直至各自完成权威结案/用量结算。不自动终止落选者，也不自动合并代码；配置、验证、择优命令见协作域参考文档。

## 排队顺序和等待原因

查询与 Claim 共用一个比较器。P0 最先；原生 P1 在晋升的 P1 前。同级按入队时间 FIFO，再按稳定 Task ID 和槽位 ID 排序。时间戳必须带时区，以 UTC 比较；重启保留等待时长。

| 基础优先级 | 老化间隔 | 晋升上限 |
|---|---|---|
| P1 | 480 分钟 | P1 |
| P2 | 240 分钟 | P1 |
| P3 | 120 分钟 | P1 |
| P4 | 60 分钟 | P2 |

距截止时间不超过 24 小时，最多升到 P1。独立的七天防饥饿规则将仍处于较低优先级的任务升至 P1，排在普通晋升任务前、原生 P1 后。P0 必须有理由和到期时间；到期恢复原基础优先级，查询不写数据库。Claim 对每次入队只记录一次截止风险和防饥饿事件。本模块不自动启动后台轮询或通知投递循环。

WAITING 表示等待，不是任务失败。原因会指出独立审核未通过、证据变化、能力未验证、Worker 或目录被占用、工作流/主机/Harness/资源容量已满，或预算受限。先解决指定条件或取得新的 Controller 批准；提高优先级不能绕过门禁，也不会中断运行中的模型调用。

容量沿用工作流配置中的 scheduler.max_concurrency、scheduler.per_host、scheduler.per_harness 和 scheduler.resources，限制值均为正整数。占用以 v2_assignment.holds_slot 为准，即使旧记录已经 finished/resolved 仍保留。Run、Task 和工作流预算与 admission 同事务预留，失败时 Assignment、预算、旧协议记录和发送意图一起回滚。

## 生命周期、恢复和审核

以下运行入口自己管理连接与事务，不能嵌套在服务事务中：

    dispatch_assignment(state, assignment_id, actor, version, context="main", *, expected_epoch, transport=None)
    reconcile_assignment(state, assignment_id, actor, version, context="main", *, expected_epoch, transport=None)

派发在外部发送前提交 attempted 标记。之后崩溃或传输失败均视为不确定，即使尚未看到完成结果。再次调用返回 ALREADY_ATTEMPTED，不重复发送。Reconcile 通过原 monitor 观察实际 Worker 身份、完成序列和交付字节。新鲜回执进入 REVIEW_READY，但保留执行槽、资源和目录 Claim。断连、超时、心跳缺失或取消请求均不释放占用。

独立 Reviewer 使用原 reviews/events 表记录结论；网关通过 review_in_transaction 在调用方事务内完成此事，不直接接受 V2 Task 或释放占用。旧 acknowledge/record_review 仍兼容，也不能覆盖已记录结论或直接改变 V2 结案结果。

Controller 的 assignment.close 消费已封存审核，重验产物和审核文件哈希，确认原 Worker 在审核对应序列上空闲后才释放占用。已通过 API 登记的证据只有在 Assignment、哈希、大小及封存状态一致时才复用原 evidence ID，否则拒绝结案。

取消需要绑定 task/nonce 的终止检查文件，其中须有独立一行 Outcome: terminated，并实际观察到空闲。这是记录已检查的终止结果，不会杀进程。Accepted 不等于 landed。返工需引用上一代封存的 repair 审核并创建新执行；旧 Assignment、Evidence、Review 历史保留。结案不结算预算，不把未知费用记作零；权威用量单独提交给预算服务。

## 验证

    python -B -m unittest discover -s scripts -p test_priority_scheduler.py -v
    python scripts/check_encoding.py

回归使用隔离 SQLite 账本和真实本地子进程，验证完成与审核、多进程抢占同一槽位、发送后进程退出。协作回归覆盖同槽/异槽多进程竞争、并行修订、预算原子回滚、返工、经验证的胜者/Ensemble 择优与落选者结案。超时探针明确使用合成 Transport。它们证明本地协议和事务行为，不代表 Codex/Pi/Claude/OMP 实机或跨主机认证；测试使用目录工作区和产物任务，也不认证 Git worktree 或代码任务行为。主控集成和独立审核是另外的门禁。
