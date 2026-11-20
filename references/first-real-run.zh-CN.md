# 第一次真实运行：从新克隆到一个已审核任务

[English](first-real-run.md)

本页给出从新克隆到一次真实“先审核后接受”运行的最短可复制路径，并说明结果证明和不证明什么。选择其中一条路径：

| 路径 | 需要 | 真实模型轮次 |
|---|---|---|
| 原生单次（推荐作为第一次检查） | Python 3.10+、已安装的 Codex CLI | 3 |
| 原生会话续用 | 同上，外加 Codex 会话恢复 | 3 |
| Herdr 分屏（可选） | Python、Herdr CLI、两个活动分屏 | 取决于你的任务 |
| Git worktree 代码闭环（可选） | Python 3.10+、Git、已安装并登录的 Codex CLI | 5 |
| 离线演示（仅机制） | Python | 0 |

各路径彼此独立。原生和 worktree 路径不启动 Herdr；若设置了 `HERDR_ENV=1`，它们会拒绝运行。Herdr 路径不使用原生校验器。

[Git worktree 代码闭环](worktree-code-loop.zh-CN.md)是完整的代码任务路径：五次真实 Codex 轮次，从 worktree 执行、独立审核、本地 Merge Queue 一直到下游物化。

## 开始之前

- **Python 3.10+。** 先确认解释器：

  ```sh
  python -c "import sys; print(sys.executable, sys.version)"
  ```

  微软商店的 `python` 别名可能看起来已安装却无法使用。请使用真实解释器或其完整路径。

- **在仓库根目录运行。** 脚本从 `scripts/` 导入同级模块，并把它们复制到运行目录。
- **Codex CLI 已安装并已登录。** 用 `codex --version` 确认。这些路径使用你 CLI 的默认 provider 和 model；不会设置、更改或读取 provider 凭证。若 `codex` 不在 `PATH` 中，传入 `--executable C:/path/to/codex.cmd`。
- **每次运行都使用新的输出目录。** 校验器和演示脚本都拒绝已存在的目录。
- **原生路径不要带 Herdr 环境。** PowerShell：

  ```powershell
  Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
  ```

  POSIX shell 则在命令前加 `env -u HERDR_ENV`。
- **网络与模型额度。** 原生路径会产生三次真实模型轮次。token 用量会被记录；费用记为未知，而不是零。

## 路径 A：原生单次

这是最小的真实检查。在仓库根目录执行：

```powershell
Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
python -B scripts/validate_v2_native_loop.py --directory runtime/repro/native-oneshot-1 --harness codex --mode one-shot --timeout 240
```

参数：`--harness codex|pi|omp`、`--executable PATH`、`--mode one-shot|session`、`--timeout SECONDS`。校验器在 `--directory` 下自行创建一次性的项目、状态、凭证和交付目录；不需要工作流配置或用户状态。

通过时输出含 `"status": "V2_NATIVE_LOOP_PASS"` 的 JSON 并以 0 退出。其他结果以 2 退出。运行目录保留 `result.json`、`trace.json`、`usage.json`、`provenance.json`、封存交付和独立审核。

谁做什么：

| 步骤 | 执行者 |
|---|---|
| 创建一次性项目、状态和限定范围的测试凭证 | 校验器 |
| 携带任务 ID 和 nonce 派发任务 A 一次 | 校验器（以 controller 身份） |
| 在一次性目录工作区执行 A | 已安装的 Codex CLI，worker |
| 对账完成、封存证据、请求独立审核 | 校验器 |
| 在独立 Codex 进程中审核 A | Reviewer 角色，不是 worker |
| 接受 A，然后派发依赖任务 B | 校验器 |
| 核对封存证据并作出判断 | 你（如需信任该结果） |

通过证明：

- 真实的已安装 Codex CLI 轮次已运行并报告完成。
- 依赖闸门在 A 被接受前不让 B 获得分配。
- 接受前审核在独立进程中发生。
- 单次模式使用不同的 Codex 会话。
- 两项服务层拒绝：服务拒绝使用测试 worker 凭证发起的审核调用，签发方拒绝为正在执行的绑定签发 reviewer 凭证。这些是围绕真实模型轮次进行的协议检查，不是模型行为。
- 第三项拒绝是**仅限测试夹具的故障注入**，不是模型轮次。校验器临时修改一次性夹具台账中 executor 绑定的模式，以合成一个错误签发的 reviewer 凭证，确认服务以 `forbidden_role` 拒绝，然后逐字恢复绑定记录并吊销令牌。这在一套一次性服务数据库中检验同一 Agent 守卫；它不证明真实 worker 尝试审核过自己的工作。

通过不证明：

- Herdr、分屏行为或持久终端会话。
- 运行中转向、排队输入或重启恢复。
- provider 或 model 身份：记录为 `null` / `cli_default`。
- 费用：记录为未知。
- Git worktree 或代码任务行为。测试使用目录工作区和产物任务；worktree 见[工作区](awo-workspaces.zh-CN.md)。
- 你自己的任务正确或被接受。这是对一次性测试的兼容性与协议检查，不是你的工作交付。

## 路径 B：原生会话续用

```powershell
Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
python -B scripts/validate_v2_native_loop.py --directory runtime/repro/native-session-1 --harness codex --mode session --timeout 240
```

会话模式为依赖任务恢复第一个 worker 的 Codex 会话。校验器要求两轮报告同一个原生会话 ID，并要求第二轮回忆起只有第一轮见过的秘密。审核者仍在独立的单次进程中运行。

这证明进程正常退出后的续用，不证明持久进程、运行中转向或崩溃后恢复。`--resume` 不是这个模式：在旧版校验器中它恢复被中断的校验运行，而不是模型会话。

已记录的会话模式通过只验证它运行过的校验器源码和它实际执行的协议路径，并不等于当前版本中的每个文件都被重新运行。请在你自己的克隆上运行该命令，以验证你的版本。

## 路径 C：Herdr 分屏（可选）

这是通过本地台账的活动分屏路径。需要运行中的 Herdr 会话、每个角色一个分屏，以及一个真实可编辑的任务。提示到达时 Herdr 会自行检查 Agent 是否仍是前台进程，因此 preflight 通过不保证投递。

**授权来自你，而不是来自文件。** 下文路径假定 `C:/awo`，任务名为 `TASK-001`，nonce 为 `0123456789abcdef`；请替换为你自己的绝对路径和新 nonce，并保持所有路径一致。在此路径发送任何内容之前，Owner 必须授权该具体任务，并且你必须把该决定写入 `request.json`：

- `authorization.source` —— 对 Owner 授权内容的真实描述（批准了什么、何时、在哪里）。它是必填项，并会写入台账和任何通知尝试。
- `authorization.dispatch_worker: true` —— Owner 允许发送这一个任务。加载图、选择 harness、编辑 `workflow.json` 或复制示例都不会授予该权限。示例省略了此字段，因此复制示例并不构成派发授权。
- `authorization.notify_reviewer: true` —— 仅当 Owner 还授权一次直接通知时才设置。即便如此，`inbox_only` 审核者绑定只记录事件，不会提示审核者；见第 6 步。

1. **发现实际分屏**（只读），记录每个分屏的 agent kind、terminal ID、启动目录、pane ID 和完成序号：

   ```sh
   herdr --session SESSION agent list
   herdr --session SESSION agent get PANE
   ```

   把 agent 对象的字段复制到配置的期望身份中：`agent` → `expected_identity.kind`，`terminal_id` → `expected_identity.terminal`，`cwd` → `expected_identity.launch_cwd`，`pane_id` → `pane`。把 `completion_seq` 记录为派发前基线；当它为 null 时，适配器改用 `state_change_seq`。不要为了让检查通过而重命名标签或虚构身份。

   截至 Herdr 0.9.3，原生 prompt 接受 `codex`、`claude`、`pi` 等原生 Agent 标签。自定义标签会被拒绝并返回 `agent_not_ready`；请改用 `inbox_only` 或另行验证的 command 桥接。

2. **编写四个文件**（由你，也就是操作者）。从 `examples/workflow.json` 和 `examples/request.herdr.json` 开始，然后编辑。以下提取命令从示例请求生成两个绑定配置：

   ```sh
   python -B -c "import json; r=json.load(open('examples/request.herdr.json')); r['worker'].pop('baseline_completion_seq'); json.dump(r['worker'], open('C:/awo/worker.json','w'), indent=2); json.dump(r['reviewer'], open('C:/awo/planner.json','w'), indent=2)"
   ```

   | 文件 | 内容与一致性规则 |
   |---|---|
   | `workflow.json` | 目标、角色、转换、调度限制。角色名必须与请求一致：本示例中 `executor` 执行，`planner` 审核并控制。 |
   | `planner.json` | 要绑定的审核者／controller 配置；以 `request.reviewer` 为来源（适配器配置、期望身份、`context_policy`）。`bind` 会用 `--policy` 的值写入绑定策略，因此请传入与请求中相同的策略。两份副本保持一致。 |
   | `worker.json` | 恰好等于去掉 `baseline_completion_seq` 的 `request.worker`。dispatch 会把请求中的 worker（去掉该字段）与活动绑定比较，不一致就拒绝。 |
   | `request.json` | 任务 ID、新 nonce、绝对 `cwd`、绝对 `workflow_state`、generation、授权、两个配置、完成契约和监控设置。 |

   - `cwd` 是 worker 写入的现有任务目录；完成文件路径相对它解析。`workflow_state` 是仓库之外的新私有台账目录。
   - 在第 5 步之前保持 `dispatch_confirmed: false`。每次尝试使用新的任务 ID 和 nonce；nonce 不是密码，但它把消息、交付和审核绑定在一起。
   - 派发前立即把实际 `completion_seq` 复制到 `worker.baseline_completion_seq`。
   - 完成契约声明文件和 worker 必须写入的精确标记：

     ```json
     "completion": {
       "artifacts": ["RESULT.md", "EVIDENCE.md"],
       "binding_artifact": "HANDOFF.md",
       "marker_source": "artifact",
       "marker": "TASK_DONE TASK-001 0123456789abcdef"
     }
     ```

     监控器相对 `cwd` 读取这些文件；`HANDOFF.md` 必须包含任务 ID、nonce 和精确的标记行。
   - 授权块必须记录 Owner 的实际决定：

     ```json
     "authorization": {
       "source": "Owner approved this task on 2026-01-01: <what was approved>",
       "dispatch_worker": true,
       "notify_reviewer": false
     }
     ```

3. **初始化并绑定**（由 Planner／controller 执行）：

   ```sh
   python scripts/monitor_task.py --check-workflow C:/awo/workflow.json
   python scripts/workflow_control.py --state C:/awo/state init --config C:/awo/workflow.json
   python scripts/workflow_control.py --state C:/awo/state bind --role planner --spec C:/awo/planner.json --mode controller --policy inbox_only
   python scripts/workflow_control.py --state C:/awo/state bind --role executor --spec C:/awo/worker.json --mode worker
   python scripts/workflow_control.py --state C:/awo/state bindings
   ```

   `bind` 会查询实际分屏，身份缺失或不匹配就拒绝。它输出的 JSON 含有新绑定的 `instance_id` 和 `version`；`bindings` 把同一对值显示为 planner 行的 `id` 和 `version`。这两个确切值就是第 4、5、7 步的 `--actor` 和 `--version`，并配 `--context main`。它们按状态生成，请从输出中复制；过期、已退役或上下文不符的绑定会被拒绝。`--context main` 是操作者声明，不是来自分屏焦点的证明。

   除非你刻意授权直接通知，否则使用 `--policy inbox_only`。`dedicated_window` 需要在 `planner.json` 中提供真实的独占窗口声明（`context_declaration`）并使用该 `--policy`；示例请求使用 `inbox_only`。一个 Agent 不能同时持有 executor 绑定和 controller／reviewer 绑定；`bind` 会拒绝。

4. **编写任务消息、检查并只派发一次**（由 Planner／controller 执行）。消息就是 worker 收到的提示；它必须包含任务 ID 和 nonce，dispatch 会强制检查。有效的 `C:/awo/task/task.md`：

   ````text
   Task ID: TASK-001
   Nonce: 0123456789abcdef
   Cwd: C:/awo/task
   Deliverables: RESULT.md, EVIDENCE.md, HANDOFF.md.
   Before the final line, end HANDOFF.md with a fenced awo-result block such as
   ```awo-result {"outcome": "done", "summary": "one line", "remaining": []} ```
   The final line of HANDOFF.md must be exactly:
   TASK_DONE TASK-001 0123456789abcdef
   There is no time limit; finish the task before stopping and save partial
   results as you go.
   ````

   然后检查并发送：

   ```sh
   python scripts/monitor_task.py --request C:/awo/task/request.json --check
   python scripts/monitor_task.py --request C:/awo/task/request.json --preflight
   python scripts/workflow_scheduler.py --state C:/awo/state dispatch --request C:/awo/task/request.json --message-file C:/awo/task/task.md --actor INSTANCE_ID --version 1 --context main
   ```

   `INSTANCE_ID` 代表 `bind` 打印的 `instance_id`；`1` 是新状态下第一个绑定的 `version`，如果打印的值不同就用实际值。

   `--check` 校验请求结构和台账角色链接。`--preflight` 查询实际身份并比较精确的完成基线；它不发送任何内容。两者都不检查 `authorization.dispatch_worker` 或消息，因此请求可能通过两项检查却仍在这里被拒绝。dispatch 会重新检查身份、消息中的任务／nonce 绑定和授权，预留状态并只发送一次。`SUBMITTED` 记录已尝试一次发送；它不是已确认的开始，也不是投递。

5. **确认开始**（Planner／controller）。使用已实现的 `confirm-start` 路径；它观察 worker，只有确实可见开始时才写入证据说明：

   ```sh
   python scripts/workflow_scheduler.py --state C:/awo/state confirm-start --task TASK-001 --request-out C:/awo/task/confirmed.json --wait 120 --actor INSTANCE_ID --version 1 --context main
   ```

   它在 `--wait` 秒内每五秒轮询绑定的 worker。只有观察到 `working`、完成序号高于基线，或 worker 输出中出现任务 ID 和 nonce 时，才写入 `C:/awo/task/EXECUTION.md`、把派发对账为 `running`，并写入 `confirmed.json`。如果打印 `NOT_OBSERVED`，则没有写入任何内容，台账也未改变：检查窗格，只有在真正开始后再重试。`IDENTITY_CHANGED` 意味着重新绑定角色，而不是重试发送。如果 `EXECUTION.md` 或 `confirmed.json` 已存在，请检查它；命令拒绝覆盖。

   手动路径用于你自己做出并能描述的观察：写 `EXECUTION.md`，包含任务 ID、nonce、一行精确的 `Outcome: running`，以及你实际看到的内容和位置；然后运行：

   ```sh
   python scripts/workflow_scheduler.py --state C:/awo/state reconcile --task TASK-001 --outcome running --evidence C:/awo/task/EXECUTION.md --request-out C:/awo/task/confirmed.json --actor INSTANCE_ID --version 1 --context main
   ```

   台账会保存证据的哈希。绝不在没有观察的情况下写这份说明。

6. **监控与通知**（由你或 Planner 启动的后台 `monitor_task.py` 进程）：

   ```sh
   python scripts/monitor_task.py --request C:/awo/task/confirmed.json --notify
   ```

   设置了 `workflow_state` 时，即使 `notify_reviewer` 为 false，监控器也会记录收件事件。`--notify` 随后按授权和策略行事：

   - `inbox_only`：记录事件；不会提示 Reviewer。
   - `dedicated_window` 或 `exact_thread`，且请求中 `notify_reviewer: true`：重新核对身份后，只做一次通知尝试并记录。没有盲重试。

   监控成功运行既不是投递，也不是接受。只有读取并记录了任务绑定产物的字节后，监控器才以 `review_ready` 退出。

7. **接收与审核**（Reviewer 阅读收件事件、冻结请求、交付和原始证据，然后写 `REVIEW.md`；Planner／controller 记录结论）：

   ```sh
   python scripts/workflow_control.py --state C:/awo/state inbox
   python scripts/workflow_control.py --state C:/awo/state settle --event EVENT_ID --file C:/awo/task/REVIEW.md --verdict accept --actor INSTANCE_ID --version 1 --context main
   ```

   `REVIEW.md` 必须包含任务 ID、nonce 和一行精确的结论。有效示例：

   ```text
   # Review

   Task: TASK-001
   Nonce: 0123456789abcdef
   Verdict: accept

   ## Findings
   ...
   ```

   结论必须是 `workflow.transitions` 中配置的决策之一（`examples/workflow.json` 中为 `accept`、`repair`、`reject`、`blocked`），并且 `--verdict` 必须与 `Verdict:` 后的那一行相同。`accept` 只适用于 `review_ready` 的交付。该行后面有任何多余文字、缺少该行或结论未配置，都会被拒绝。如果声明了 `request.review_path`，文件必须是这个精确路径。

   `--actor` 必须是事件审核者角色（此处为 `planner`）在 `main` 上下文中的活动绑定。`settle` 会记录审核并关闭该尝试，因此该绑定必须同时拥有审核和派发权限：请用 `--mode controller` 绑定审核者角色，而不是 `--mode reviewer`。在本例中 planner 绑定同时是 controller 和审核者；如果你绑定了单独的 reviewer 角色，请以 controller 模式绑定它，并使用它的 `instance_id` 和 `version`。`settle` 记录审核者的结论；它本身并不使交付被接受，也不会把改动落到你的项目。非接受的结论会记录配置的转移（在 `examples/workflow.json` 中，`repair` 映射为 `new_scoped_repair_task`），但 `settle` 本身不会创建或重发任务。后续行动是一个单独的新请求，带有自身的授权。

这条路径的状态：AWO 的各阶段任务书在使用它。它们的冻结请求绑定 Herdr 窗格，dispatch 在 Herdr 的 prompt 调用返回后返回 `SUBMITTED`，其结算记录已接受的审核。这些是项目运行的示例，不是对你的窗格的测试；本页不声称已为每个当前源文件重新执行实时窗格。原生校验器运行中的 `herdr_invoked=false` 字段只描述那个内部的一次性测试装置，不是这条外层路径。Herdr 仍会重新检查前台进程；请检查你的实际窗格和任务。

## 离线演示：仅机制

在仓库根目录运行。每次使用新目录：

```sh
python -B scripts/demo_scheduler.py --directory runtime/first-run
python -B scripts/workflow_control.py --state runtime/first-run/state status --summary --format text
```

任务 A 和 B 是两个独立节点，在两个模拟 Worker 上并行提交。JOIN 依赖两者，只有两个上游节点都取得接受审核后才开始。演示把每个交付的 `RESULT.md` 与期望字节比较，记录审核，并断言三个节点最终都是 `accepted`。

这证明本地 DAG、依赖、审核和接受机制。它不证明 Harness 兼容性、模型输出、provider 或 model 选择、通知投递、费用或 worktree／代码任务行为：worker 是本地 Python 子进程，不是 AI 客户端。

## 证据状态与你自己的文件

| 路径 | 已记录的证据 | 它不能证明什么 |
|---|---|---|
| 原生单次 | 一次真实 Codex CLI 运行在已审核的校验器源码上报告 `V2_NATIVE_LOOP_PASS`，记录 3 个模型作业。 | 整个当前修订版被重新执行，或你的任务正确。 |
| 原生会话续用 | 会话模式在已审核的校验器字节上有一份通过的记录运行，两轮使用同一原生会话 ID，且结果中成功回忆了秘密。 | 每个当前源文件都被重跑、崩溃恢复或实时转向。 |
| Herdr 分屏 | 项目各阶段任务书使用冻结的 Herdr 绑定，并记录 `SUBMITTED` 派发和已接受的结算。 | 你的窗格行为相同，或本页重新执行了实时窗格。 |
| 离线演示 | 确定性演示记录 `OFFLINE_DEMO_PASS` 和三个已接受节点。 | Harness 兼容性、模型输出或通知投递。 |

这些是项目中的示例运行。对于你自己的克隆，请运行命令并阅读它写出的文件；不要依赖他人的总结。

你自己的证据出现的位置：

- **原生路径**：在 `--directory` 下，`result.json`、`trace.json`、`usage.json` 和 `provenance.json`。
- **Herdr 路径**：dispatch 命令的 JSON；任务目录中的 `EXECUTION.md` 和 `confirmed.json`（由 `confirm-start` 或你的检查写入）；请求目录中的 `monitor_status.json`、`monitor_summary.json`、`completion_receipt.json` 和 `frozen_request.json`；`workflow_control.py inbox` 显示的事件；以及 `workflow_control.py inspect --task TASK-001` 显示的已记录审核。
- **离线演示**：`demo_scheduler.py` 打印的 JSON 和 `workflow_control.py status` 的台账摘要。

记录的通过只验证实际运行的内容。它不是对当前修订版中无关文件的承诺，也不是对你工作的接受。

## 一次通过不会消除的限制

- **目录产物校验不是 worktree 或代码任务校验。** 它认证受管目录中声明的产物字节，不认证 Git worktree、分支、补丁、合并或代码任务检查。
- **接受不等于落地。** 接受记录审核者的结论，不会合并或物化代码。
- **优先级不等于运行中转向。** 排队顺序和准入闸门不会中断已在运行的模型调用。
- **预算准入与结算不等于强制停止。** 它们是账本计量；不会停止进行中的调用，账本也不创建供应商硬上限。
- **旧版知识来源可能仍然未知。** 未经认证的旧记录不等于缺失，也不等于已认证。

## 故障排查

| 现象 | 原因与下一步安全操作 |
|---|---|
| `native CLI executable not found` | `codex` 不在 `PATH` 中；用 `--executable` 传入精确的 `.cmd` 路径 |
| 因目录已存在而拒绝 | 换一个新的 `--directory`；不要复用已完成的运行 |
| 关于 `HERDR_ENV` 的拒绝 | 为原生路径清除 `HERDR_ENV` |
| `python` 启动了商店别名 | 使用真实解释器路径 |
| 模型轮次失败或超时 | AWO 不会重发不确定的发送，也不会更改凭证；检查运行目录并解决客户端／账号原因 |

运行任何路径都不会为你的任务授权派发、通知或连续运行。这些权限始终属于你。
