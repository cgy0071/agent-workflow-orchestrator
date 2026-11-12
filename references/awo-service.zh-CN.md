# 可选的 V2 服务边界

`scripts/awo_service.py` 是 V2 应用服务边界的可选基础。它是增量、默认关闭的，不替换现有的 controller、调度、监控、证据、审核或 claim 逻辑。

## 可用操作

可以在不写入的情况下查看本地 state：

```sh
python -B scripts/awo_service.py --state STATE status
```

迁移默认只读；只有带 `--apply` 才写入。apply 需要当前 controller，并在一个事务中写入增量 v2 投影：

```sh
python -B scripts/awo_service.py --state STATE migrate
python -B scripts/awo_service.py --state STATE migrate --apply \
  --actor INSTANCE --version 1 --context main
```

apply 之后，controller 可以冻结派发、在清理 V2 slot/uncertain 记录后解冻，或切换权威。`freeze` 与 `unfreeze` 是两条独立命令；`enable` 与 `disable` 也是两条独立命令：

```sh
python -B scripts/awo_service.py --state STATE freeze \
  --actor INSTANCE --version 1 --context main --reason "..."
python -B scripts/awo_service.py --state STATE unfreeze \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE enable \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE disable \
  --actor INSTANCE --version 1 --context main
```

V2 启用后，controller 可以通过信封创建/更新 draft Task。信封提供幂等键、工具、当前 controller epoch 与 payload；principal 与 project 来自已验证的本地绑定与账本：

```json
{
  "command_id": "cmd-demo-0001",
  "tool": "task.create",
  "expected_epoch": 1,
  "payload": {
    "task_id": "T-DEMO",
    "title": "Draft task",
    "body": "Local draft only.",
    "priority": "P3"
  }
}
```

```sh
python -B scripts/awo_service.py --state STATE command \
  --envelope envelope.json --actor INSTANCE --version 1 --context main
```

读取不需要启用 V2：

```sh
python -B scripts/awo_service.py --state STATE task-get --task T-DEMO \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE task-list \
  --actor INSTANCE --version 1 --context main
```

`status` 只读，不创建 schema 或 WAL 文件；迁移 dry-run 也只读。`migrate --apply`、freeze、unfreeze、enable、disable 与服务写命令需要当前 controller 与现有 dispatch 权限；显式空权限列表不会获得任何新权限。

## 已实现

- 增量 `v2_*` SQLite 表：project、稳定 Task、Assignment、Evidence、Review、legacy 映射、command log、event log、service state、migration run 与 migration-uncertain。
- 保守的 legacy→v2 投影：
  - `migrate` 默认只读 dry-run。
  - `migrate --apply` 需要当前 controller，并在一个 `BEGIN IMMEDIATE` 事务中写入。
  - 可重复执行；不删除或改写 legacy 表。重复 apply 会刷新 legacy 拥有的投影（Task 状态、Assignment slot、Evidence/Review 完整性状态），但保留已封存的原哈希与 V2 自有 draft 字段；不会让已变更的证据继续显示为 accepted。
  - 包含每个 legacy task 行，包括无 dispatch 的 registered-only 行；这些行得到 `historical_registered` Assignment，并保留请求中记录的 generation。
  - `resolved` 从不等于 `accepted`。接受复用现有 `workflow_scheduler.acceptance()` 门禁，并额外校验冻结请求哈希、task/nonce 绑定、review 事件与封存的 review/artifact 哈希。
  - 缺失、变更、不可读或未封存的 evidence/review 变成 `migration_uncertain`，绝不升级为 `review_ready` 或 `accepted`。
  - legacy 已关闭的 Task 以 `tasks.status = resolved` 为权威；`release()` 不会写 `dispatches.status = resolved`，因此 dispatch_status 仅作为 provenance 另存，不把已关闭 Task 显示为 running Assignment。
  - 经独立记录且校验通过的 termination note（audit 中的 path、hash、task/nonce）可以在没有 acceptance 的情况下释放 slot；Task 仍是 `migration_uncertain`，不伪造接受。缺失/变更/未绑定 task+nonce 的 termination note、心跳丢失、超时与静默都不释放 slot。acceptance 与 quiescence 相互独立。
  - 历史没有 usage 行；基础层不伪造零成本。
- 带命令信封的服务命令：
  - `command_id`、`tool`、`expected_epoch`、`payload`。
  - principal 与 project 来自已验证的本地绑定与工作流账本；绝不信任请求体自报角色。
  - 相同 principal/command/payload 重放返回已存响应；同一 command 不同输入则冲突。
  - 只实现 `task.create` 与 `task.update`；后者需要 `expected_revision`，且只编辑 draft Task。
- freeze/epoch 控制：
  - `freeze` 阻断 legacy register/dispatch、dispatch 的最终发送，以及 `continue_task` 的最终发送。
  - `unfreeze` 与 `disable` 在 V2 Assignment 仍持有 slot 或处于 uncertain 时拒绝。
  - freeze 从不声称运行中的外部进程已停止；读取、证据收集、审核、settle 与经检查的关闭仍可用。
  - V2 默认关闭。启用要求已 apply 的迁移、没有 running task 或真正未关闭的 dispatch，并要求在当前锁内重新计算完整源快照、确认与 v2 投影无漂移；新出现的已关闭 legacy task 必须先重新 apply，不能静默遗漏。
  - V2 启用后 legacy dispatch/register 被阻断。V2 执行走可选的优先级调度器及其 Application 命令边界；启用服务本身不会派发工作，也不替代 controller 决策。

## 字段权威与写入边界

- legacy `tasks`/`dispatches`/`events`/`reviews`/`audit` 是既有 attempt 的执行安全权威：nonce、冻结请求哈希、dispatch 状态、review 原件与 claim 关闭口径都从它们读取，不重新推导。
- v2 Task/Assignment 表默认只读；只有 V2 启用后，Task 生命周期元数据写操作只有 `task.create`/`task.update`（编辑 draft Task）。注册表表（Agent/enrollment/capability）在增量 schema apply 后即可写，与 `v2_enabled` 无关；它们不派发工作。
- 服务只写 V2 draft Task 行与注册表行，不写 legacy 生命周期行。V2 启用后 legacy dispatch/register 被阻断，因此没有“双写生命周期”的声明；legacy 行只是 provenance 与执行安全证据。
- 注册表入口认证不同：controller 命令使用当前 controller 绑定；`agent-register` 使用一次性 grant secret 文件；`agent-self-register` 是未批准 pending 请求；心跳与状态使用 Agent token。见 [awo-registry.zh-CN.md](awo-registry.zh-CN.md)。

## 限制

服务边界本身不提供调度、预算执行、消息发送、worktree 创建、merge 执行、MCP server 或 UI。这些能力是 `references/awo-*.md` 中各自记录的可选模块；`enable` 是写入闸门，不是可工作的调度替代。不要把空的 v2 表当作已实现能力。

## 测试

`scripts/test_awo_service.py` 覆盖：dry-run 非写入、事务回滚、重复迁移、registered-only 与 generation 保留、resolved 拒绝、带封存证据的 accepted、缺失/变更 artifact/review、非 review_ready 的 accept 行、冻结请求摘要篡改、重复 apply 刷新证据、enable 对新增/未迁移任务与源漂移的拒绝、closed accepted 的 dispatch provenance、uncertain slot 保留、accepted 但未关闭 slot 保留、角色权限、幂等、修订冲突、过期 epoch、freeze 阻断 register/dispatch/continuation、畸形 receipt，以及在临时 SQLite state 上的真实 CLI 子进程演练。

## 其他限制

- 未应用于任何生产或共享控制状态；迁移只对显式本地 `--apply` 的给定 state 执行。
- 没有完整双向生命周期路由；因此 V2 默认关闭，启用后阻断 legacy dispatch。
- 本基础层不声称任何 live Harness 能力。
