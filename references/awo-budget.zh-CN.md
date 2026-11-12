# 预算账本与预留

可选的 V2 服务包含一个位于同一应用服务信封之后的本地预算账本。它记录准入与用量；不启用调度器、不调用供应商，也不认证供应商硬上限。

## 范围与维度

budget scope 为 `run`、`task` 或 `workflow`。run/task/workflow 从实际 Assignment、其 Task 与项目推导，不能由调用者自选：

- 一个 run 必须且只能绑定一个 `(assignment_id, generation)`；之后不能换绑其他 Assignment，也不能结算其他 run；
- Task 是 Assignment 的 task；Workflow 是该 Task 的项目。

维度名如 `tokens.input`、`tokens.output`、`cost.usd`、`turns`、`tool_calls`、`wall_clock_sec`。limit 与金额必须是有限非负数；拒绝布尔、NaN、Infinity 与数字字符串。无 limit 表示不限，不是免费执行。每个有界维度在预留时都必须提供估算。

## 预留

预留在一个 `BEGIN IMMEDIATE` 中跨全部推导出的 scope/dimension 完成；任一步失败会回滚所有 scope、预留与 run link。后续调度器可使用调用方自持事务的 in-transaction API。

预留键为 `(budget_id, assignment_id, generation, dimension)`。相同金额即使使用新 command key 也是 no-op，包括恰好到上限；不同金额或状态冲突。unknown 用量默认阻断准入（`on_unknown: "block"`）；策略可显式设为 `warn`。服务冻结时拒绝新预留。

已关闭的 Run 不能新增预留，包括之前没有预留的维度。重放关闭前成功的原始命令仍是无副作用操作。

## 用量、暴露与关闭

样本序号必须为非负整数，generation 必须为正整数；拒绝布尔值、字符串和小数。

sample 按 run/dimension 记录，可为增量或累计快照。累计快照替换到该序列为止的增量总和；之后的增量继续相加。维度结算前，有效用量取同一 run 的预留、流式总量及报告计量/估算值中的最大值。这些是同一用量的重叠记录，不能相加；各 run 的最大值再求和，不能直接取整个 Workflow 的最大值。

estimated 与 known actual 分开统计。estimated close 不是 actual：它保留保守的 unknown 预留/暴露，并默认阻断新准入，直到 authoritative 对账。显式 warn 策略仅放宽未知数据门禁，数值上限仍按保留的暴露检查。例如上限 USD10、已预留 USD10，close 报 unknown 或 estimated USD1，即使 `on_unknown: "warn"`，名义剩余仍为0；若估算为 USD12，则暴露为12，而不是10或22。unknown 按维度保留；tokens 已知而 USD 未知是合法的，但不是免费执行。缺失或未知的 per-run 维度会显示为 unknown，包括没有 usage 的历史导入 Assignment。仅 stream 的 partial close 会投影为 `partial_actual`，带来源与残余不确定性；partial 观测值不是最终总量。

只有 authoritative 且 usage state 为 `actual` 的维度才能结算，一次替换该维度的流式/预留暴露；其他维度可仍为 unknown。混合报告的整体 close state 应为 `partial`。报告具有权威性不代表其中 unknown/estimated 维度已实际结算。非 authoritative 的 actual 观测值会显示在 `known_actual`，但不能证明结算：预留仍为 unknown，有界维度总量未完整确认时默认阻断新准入。

`authoritative` 与 `reconcile` 必须是 JSON 布尔值。整体 `actual` 必须包含非空且全部 actual 的维度；整体 `unknown` 不能包含计量值。重放不重复计费。reconcile 必须 authoritative、使用当前 close revision、且 run/assignment/generation 相同；一个 run 不能被换绑或结算两次。改变 authority 或价格表引用不视为重复报告。未决修订不能丢弃或降低先前计量；释放容量需提供 authoritative actual 替代值。close 事件保留前一快照供审计。

Task/Workflow 的 attempt 与 repair 上限从持久 Assignment/review 记录推导，不信任调用者传入的零值。准入会把 prospective assignment 计一次；新 Assignment 不能重置 attempt 总数。token、wall-clock、attempt 与 repair 聚合跨 Assignment 保留；资源单位仍是独立占用 claim，绝不因预算过期或断连自动释放。

reconcile 必须有已存在的 close 及其当前 revision；首次关闭不能使用 reconcile。

状态中的 `effective_usage` 是准入、警告和 `nominal_remaining = max(0, limit - effective_usage)` 共同采用的总量。不要再加上 `known_actual`、`estimated`、`held`、`unknown_reserved` 或原始 `stream_exposure` 展示字段，它们存在重叠。名义剩余只反映已有信息，实际剩余可能更低，不是保证下界。

## 现有数据库

R1 数据库无需改变结构。读取历史 `authority_json` 时会结合 actual usage 过滤：错误列入的 unknown/estimated 维度仍保留暴露。旧预留行的状态标签本身不能证明结算。对于没有 authority 元数据的更早数据库，读取会保守保留暴露且不修改结构；写入前使用现有授权迁移命令，再以 authoritative actual 报告对账。不能从整体 close 标签或估算推断缺失的 authority。

## 动作与权限

只有当前 controller 能设置 limit、approve、reserve、close 或记录 warning。修订 CAS、项目绑定、controller epoch 与幂等均适用。warning、pause-request、approval-required、cancel-request 只是账本状态与请求，不证明 worker 已暂停或终止。准入、预留与结算是账本计量，不会停止或中断已在进行的模型调用。供应商硬上限是独立验证能力，本账本不创建它。资源单位仍是占用 claim，与 spend 分离，不因预算过期或断连释放。

## CLI

```sh
python -B scripts/awo_service.py --state STATE budget-status \
  --scope-type task --scope-id TASK-ID

python -B scripts/awo_service.py --state STATE budget-limit \
  --envelope limit.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-reserve \
  --envelope reserve.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-sample \
  --envelope sample.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-close \
  --envelope close.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-action \
  --envelope action.json --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE budget-approve \
  --envelope approve.json --actor INSTANCE --version 1 --context main
```

预留信封示例：

```json
{
  "command_id": "cmd-budget-reserve-0001",
  "tool": "budget.reserve",
  "expected_epoch": 0,
  "payload": {
    "run_id": "run-0001",
    "assignment_id": "asg-0001",
    "generation": 1,
    "dimensions": {"tokens.input": 10000, "cost.usd": 2.0},
    "basis": "estimate"
  }
}
```

authoritative close 示例：

```json
{
  "command_id": "cmd-budget-close-0001",
  "tool": "budget.close",
  "expected_epoch": 0,
  "payload": {
    "run_id": "run-0001",
    "assignment_id": "asg-0001",
    "generation": 1,
    "state": "partial",
    "authoritative": true,
    "usage": {
      "tokens.input": {"state": "actual", "value": 9000, "source": "harness-result"},
      "cost.usd": {"state": "unknown", "reason": "provider_did_not_report", "source": "run-close"}
    }
  }
}
```

## 限制

没有调度器启用、消息、供应商设置、MCP server、fanout、worktree、merge、供应商硬上限或 live Harness 认证。Agent 与资源 claim 与本账本分离。读取不创建或迁移状态。
