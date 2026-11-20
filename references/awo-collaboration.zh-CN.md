# 协作运行时：Fanout、Ensemble 与有界返工

[English](awo-collaboration.md) | 中文

`scripts/collaboration_runtime.py` 实现 P1-3/P1-4 领域侧模块。
SQLite 是唯一权威，一个稳定的原生 Task 拥有不同 Assignment、generation、
slot、legacy attempt 和 nonce，不复制业务 Task。
模块不派发模型 Worker、不接受 Task、不合并代码、不停止竞争者、不释放
工作区/资源/预算占用。父实现负责调度器/服务接入及最终集成验收。

## API 与事务

```python
create_collaboration_schema(con)
apply_command(con, project_id, principal, tool, payload)
query(con, project_id, principal, query, payload)
prepare_enqueue(con, project_id, principal, task_id, slot_id,
                contract, expected_revision)
check_admission(con, project_id, principal, ticket_id)
admit_in_transaction(con, project_id, principal, ticket_id)
review_guard(con, project_id, principal, task_id)
acceptance_guard(con, project_id, principal, assignment_id, event_id)
closure_projection(con, project_id, principal, assignment_id, outcome)
run_verification(con, project_id, principal, verification_id)
```

领域写入和建表要求调用方事务，通常使用 `BEGIN IMMEDIATE`。
这些函数不提交、不回滚。任何失败（包括插入 Assignment 后预算预留失败）
都必须由父实现回滚整个命令，不能捕获异常后提交部分结果。
建表使用事务化语句，不调用 `executescript`，不修改原生服务版本。

`run_verification` 是明确的外部效果入口例外：必须使用专用的**空闲连接**，
数据库持久化到磁盘、启用日志，且 `synchronous=FULL` 或 `EXTRA`。
它拥有进程前后的两个短事务，不能在服务命令事务内执行。

受信 principal：`{"role": ..., "agent_id": ..., "principal_id": ...}`。
父服务负责认证、scope、epoch、replay 和外层 revision；
模块检查项目内有效注册、角色、Task/Assignment/slot 所有权、
策略 revision 和独立 review。可选 `principal.project_id` 必须匹配。
payload 不能冒充 principal。领域异常为带 `code` 的
`CollaborationError(ValueError)`；原生门禁失败使用 `native_gate`。

## 命令、查询与上限

名称允许可选 `awo_` 前缀；拒绝未知字段。

| 命令 | payload | 返回 |
|---|---|---|
| `collaboration_configure` | `task_id, expected_revision, mode, slots, max_attempts, max_repairs, verifier` | 策略/原生 Task revision、配置哈希 |
| `collaboration_verify` | `task_id, expected_revision, assignment_id, event_id` | `verification_id, status=READY, process_started=false, revision` |
| `collaboration_propose` | `task_id, expected_revision, assignment_id, event_id`；可选 `contributors` | 唯一 proposal ID、manifest 哈希、`accepted=false`、revision |
| `collaboration_withdraw` | `task_id, expected_revision, reason` | 保留历史的撤回 proposal ID、revision |
| `collaboration_cancel_ticket` | `ticket_id, expected_revision, reason` | 取消未 admission 的 ticket ID、revision |

Configure 检查**原生 Task** revision，只允许 draft/ready Task，
且早于任何 Assignment/原生队列项；生成的策略不可变。
其他 revision 都是**协作策略** revision。入队、admission、
验证批准/尝试/回执均递增策略 revision；原生 Task revision 是父外层独立约束。

`slots` 为 `{"slot_id": int, "agent_id": str}` 列表。
Single 用 slot 0；fanout 用连续竞争 slot 1..N，N=2..8。
Ensemble 额外要求明确批准 slot 0 的 synthesis Worker；
原生 `fanout_slots` 只记 N，不含 synthesis。
所有批准 Worker 必须有不同的原生物理身份。

`verifier` 必填 `argv`（字符串列表）、`timeout_seconds`、
`max_output_bytes`、`max_runs_per_assignment`，例如：

```json
{
  "argv": ["python", "-B", "-m", "unittest", "test_deliverable"],
  "timeout_seconds": 60,
  "max_output_bytes": 65536,
  "max_runs_per_assignment": 2
}
```

该 argv 是 Controller 明确批准，不是自动默认动作。配置时固定 executable
路径/哈希；相对参数在交付 cwd 解析；拒绝 shell 批处理文件。
库、环境和脚本仍是 Controller 的受信输入，固定 executable 哈希
不等于操作系统沙箱或完全封闭的构建环境。

最大值：每个 Task 128 个已 admission/排队 attempt、64 次已消费 repair、
256 个保留的批准 ticket；每个 Assignment 8 次验证批准；
64 个 argv 参数、单次 300 秒、stdout/stderr 合计保留 1 MiB。
配置可以更小。取消初始 ticket 保留身份/历史，但不算已执行 attempt；
取消 repair ticket 不归还其单次消费的 review。

| 查询 | payload | 只读结果 |
|---|---|---|
| `collaboration_get` | `task_id` | Task、策略/配置、ticket、验证 intent 状态/哈希、当前 proposal |
| `collaboration_history` | `task_id`；可选 `after_seq=0, limit=100`（1..100） | 审计事件和 `next_after_seq` |

查询不写入、不 reconcile、不使记录过期、不释放、不启动进程。
Worker 只可查询为其批准 slot 的 Task；有效 Controller/Reviewer/Observer
可查询本项目。

## 调度器接入

1. `_enqueue` 构造原生 contract 后调用 `prepare_enqueue`。
   必填键：`task_id, agent_id, request, message, budget`。
   原生可选键：`required_capabilities, resources, dependencies,
   worker_binding, workspace_mode, task_kind, dag_run, dag_node,
   approved_controller_id, approved_controller_version, approved_epoch`。
   模块固定 generation/binding、补默认值，返回 ticket ID、slot、
   generation、revision、冻结 contract 及哈希。
2. 按 **ticket** 排队/admission，不覆盖单个 Task 队列行。
   generation 包含待处理 ticket 和历史 Assignment，不同 slot 可乱序 admission。
3. 最终事务保留原生授权、epoch、DAG、能力、live-idle、工作区、容量、资源门禁。
   不要套用旧 single-only 检查或“已 admission 最大 generation 加一”检查。
   `check_admission` 只读，不预留。
4. 最终事务中 `admit_in_transaction` 重检领域门禁、插入原生 Assignment，
   调用原生 `budget._reserve_in_transaction`，返回
   `assignment_id, run_id, reservation_ids, generation, contract,
   revision, sent=false`。父实现在**同一事务**持久化 legacy
   task/map/reservation/dispatch intent 和原生 Task 运行状态；提交后才发送。
5. 原生 `review_in_transaction` 前调用 `review_guard`，排除所有批准的
   competitor/synthesis 身份及历史 Assignment/冻结 binding，
   包括别名和 losing Worker。
6. 接受候选前调用 `acceptance_guard`；关闭 Assignment 前使用
   `closure_projection`，采用建议 Task 状态，替代旧路径对 Task 的无条件覆盖。
   原生终止/quiescence 检查和真实逐 Assignment 释放仍是必要条件；
   投影本身不释放任何占用。

唯一 winner proposal 要求当前完成证据和成功验证；它不接受 Task，也不选
先完成者。授权/admit 新工作前必须撤回 proposal。
Ensemble 固定 synthesis 输出及至少两个不同 competitor event 快照；
独立 review 审 synthesis，来源输出不被隐式接受。
候选或 contributor 哈希改变即阻止接受。

## 有界 repair

消费现有 `reviews/events/tasks` sealed 链及已经准备好的原生
`workflow_scheduler.follow_up` request/message；不另建 review 存储，
也不在本模块生成 follow-up 文件。Parent 必须为本稳定 Task、同一 slot
的最近 legacy attempt，且精确 review 字段必须允许 repair。

旧 Assignment 必须 `holds_slot=0, review_status=repair`，有已关闭原生 intent、
匹配的原生 review/map seal，以及原 closure 的权威 quiescence/identity observation。
TTL、心跳丢失、uncertain、取消请求及编造的 closure 均不合格。
变化/失效 review、错误 Task/nonce/hash、重复父 event 均拒绝。
取消 repair 批准不会归还其单次使用的父 event。

竞争者和 repair 共享原生 Task/workflow 累计预算，原生 unknown usage、
repair 计数和 reservation 结算语义保持不变。有限本地 attempt/repair
上限叠加在原生门禁上。不释放 held/unknown reservation。
部分失败/取消保留其他 slot 和已 accepted Task。

## 持久化验证与崩溃

1. `collaboration_verify` 在调用方事务内写不可变批准并返回 READY，
   不执行进程；调用方提交。
2. 父实现通过专用空闲连接明确调用 `run_verification`。
   Runner 重检 intent 所有者、原生 Controller/epoch、启用/冻结状态、
   executable/完成哈希，随后在 `Popen(shell=False)` **之前**
   提交带唯一 token 的 `attempted`。
3. Runner 限制超时/输出，记录真实 PID、exit、起止时间、
   观察/保留输出字节数及哈希、artifact 前后快照；
   提交不可变 receipt 和 `recorded`。
4. Recorded 重入返回既有回执；attempted 重入返回 UNCERTAIN，绝不再启动。
   Pending/uncertain 阻止接受及该 Assignment 的再次验证批准。

Attempt 提交后、Popen 前崩溃，与已启动但丢回执无法区分；
即使实际未产生效果，两者都保持 uncertain。回执持久化失败同理。
无 reset/自动 retry API，父实现必须检查真实进程和文件效果；
SQLite 无法撤销它们。正常已记录失败允许在有限次数内再次明确批准。

超时/输出溢出终止直接 verifier，pipe EOF 缺失则拒绝通过。
这不证明后代或 Worker 进程树已静止，仍可能需要外部检查。
Verifier 退出不释放占用；优先使用只读、有界且子进程行为受控的命令。

## Schema、写 ACL 与证据

新增 `v2_collaboration_` 表：`meta, policy, ticket, proposal,
verify_intent, verification, audit`。策略/批准身份不可变；
ticket/验证 attempt 不可 reset；撤回 proposal 保留历史；
receipt/audit 仅追加。原生 schedule/evidence/review schema 不变。

所有命令、enqueue/admission、acceptance、closure、显式 runner
仅限 Controller；review guard 允许 Controller/Reviewer；
查询遵守上述项目/Worker ACL。Configure 更新原生 Task mode/slot/revision；
admission 插入原生 Assignment 并调用已有预算权威；
其他写入仅在协作表。

## 应用 CLI

`scripts/awo_service.py` 暴露有界验证 runner：

```powershell
python -B scripts/awo_service.py --state STATE collaboration-run --project-root ROOT --token-file TOKEN --verification VERIFICATION_ID --expected-epoch EPOCH
```

凭据必须是项目级 controller，带范围的凭据一律拒绝。命令使用一条
`synchronous=FULL` 的专用连接，完成鉴权并检查当前 epoch 与冻结/启用状态后回滚，
然后在同一条空闲连接上调用 `run_verification`。Runner 在启动 verifier 之前提交
`attempted`，且绝不重试不确定的尝试。进程丢失时检查已记录回执和外部效果，
用原 command_id 对账，而不是再次启动。

```powershell
python -X utf8 -B -m unittest discover -s scripts -p test_collaboration_runtime.py -v
```

测试使用真实 SQLite、竞争的独立进程、本地 verifier 和 Git。
Worker adapter 明确标注 synthetic fixture；父持久化接缝是 fixture wiring，
不能证明 fanout dispatch 已集成。启动前崩溃测试在效果接缝显式注入
`os._exit`；另一测试在 verifier 写文件后终止真实 runner。
验证不调用 provider、不进行生产迁移、commit 或 push。
最终父派发/关闭集成验收另行进行。
