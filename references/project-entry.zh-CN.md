# 项目入口：准备、执行、评审、落地与恢复

[English](project-entry.md) | 中文

`scripts/project_entry.py` 是面向用户的入口：针对一个已有的本地 Git 仓库完成一次有界变更。它先冻结一份可评审的计划，再驱动已有的 AWO 原语：Task 与 Assignment、优先级调度器、受管 Git worktree、原生一次性桥接、证据封存、独立评审和本地合并队列。它不新增调度器、数据库、适配器或代理。

## 它不是什么

- `prepare` 期间不发送任何内容。不运行模型，不创建目标 ref，也不暗示 Owner 已批准。
- 它不是持久会话或实时操控通道。每个执行者和评审者回合都是一次有界的一次性进程。
- 它不是远程或跨主机路径，也不认证已安装 Codex CLI 之外的 Harness。已验证范围是 Windows + Codex。
- 它不替代仓库自身的评审规则。独立评审者检查冻结的变更、diff 和验收输出，不认证项目的无关部分。

## 前提条件

- Python 3.10 或更高版本；模块仅使用标准库。
- 一个已有的本地 Git 仓库，具有完整历史、在冻结提交处干净的工作树，并使用绝对路径。入口会克隆它；源仓库永远不会被写入。
- 已安装并完成认证的 Codex CLI。入口用它执行一次 worker 回合和两次独立评审回合。它不会安装或重新配置 provider。
- 验收命令需要一个绝对路径的原生可执行文件，例如 Node 项目的 `node.exe`。

## 操作者配置

将以下内容保存为源仓库之外的 `spec.json`：

```json
{
  "version": 1,
  "repository": "E:/work/my-project",
  "goal": "对缺失的 --out 值以退出码 2 和清晰消息拒绝。",
  "allowed_paths": ["lib/build.mjs", "tests/smoke.mjs"],
  "verify_argv": ["C:/Program Files/nodejs/node.exe", "tests/smoke.mjs"],
  "worker": {"harness": "codex", "executable": "C:/Users/you/AppData/Roaming/npm/codex.cmd", "timeout_seconds": 300},
  "reviewer": {"harness": "codex", "executable": "C:/Users/you/AppData/Roaming/npm/codex.cmd", "timeout_seconds": 300},
  "authorization": {
    "source": "Owner 已在任务请求中批准这次有界变更。",
    "dispatch_worker": true,
    "allow_local_land": true
  },
  "budget": {"tokens.input": 40000, "tokens.output": 8000},
  "target_ref": "refs/heads/project-entry/my-change",
  "work_root": "E:/work/awo-runs/my-change",
  "change_name": "my-change",
  "max_real_turns": 12,
  "timeout_seconds": 300
}
```

入口强制执行的规则：

- `repository`、`verify_argv[0]`、两个桥接可执行文件和 `work_root` 必须是绝对路径。`--out` 必须解析为与声明的 `work_root` 完全相同的目录；二者都会在任何写入之前解析，必须是新目录，且必须与源仓库互不重叠。符号链接或 junction 上级目录会被拒绝，被拒绝的 prepare 不会留下部分输出目录。
- 验收命令是冻结的 argv 列表，以 `shell=False` 执行。第一个元素必须是绝对路径的原生可执行文件，不能是 shell。任何绝对路径的文件参数都会在准备阶段被哈希固定。`run` 和 `recover --confirm-execute` 会在入口处、任何模型或命令生效之前重新校验固定的可执行文件和输入哈希，并在验收命令执行前、合并前以及每个恢复副作用前再次校验。被改动或缺失的固定输入会在这些检查点被拒绝，被修改的命令绝不会执行，也不会因此消耗 worker 或 reviewer 回合。
- `allowed_paths` 是允许的最大集合，而不是必须修改的集合。执行者可以只修改其中一部分、新增文件、删除文件，或在集合内重命名；结果不得包含其他路径。Git 路径通过 `--name-status -z` 解析，因此带引号、空格和非 ASCII 的名称都能正确处理。入口无法安全评审的条目（目录、位于文件之下的路径、符号链接、`.git` 或父目录逃逸）会在 `prepare` 阶段被拒绝。
- `target_ref` 是新的 `refs/heads/...` ref，不能是 `main` 或 `master`，不能是源分支，也不能位于 `refs/heads/awo/`（受管 worktree 命名空间）之下。
- `max_real_turns` 为 1 到 12，按桥接收据计数。

## 命令

```text
python -B scripts/project_entry.py prepare --spec spec.json --out RUN
python -B scripts/project_entry.py run     --plan RUN/plan.json --confirm-execute
python -B scripts/project_entry.py status  --plan RUN/plan.json
python -B scripts/project_entry.py recover --plan RUN/plan.json [--confirm-execute]
```

每条命令输出一个 JSON 对象。退出码 `0` 表示成功或已完成只读状态查询；`2` 表示拒绝、阻塞、验收或评审失败、身份不匹配。

`prepare` 写入配置的字节副本、哈希封存的 `plan.json`、`plan.sha256`、人类可读的 `PLAN.md`、冻结提交处的可丢弃克隆、`state/` 下的空账本以及桥接根目录。执行前请审阅 `PLAN.md`。

`run` 需要显式 `--confirm-execute`。它重新检查源提交、克隆、令牌和目标 ref，然后入队、认领、创建 worktree、发送一次 worker 回合、检查变更路径并运行冻结的验收 argv，只提交允许的路径，封存证据，发送一次独立代码评审回合，以 accepted 关闭 Assignment；当 `allow_local_land` 为 true 时，在合并队列中构建并评审候选提交，再用比较交换更新 ref 落地。Accepted 只表示控制者和独立评审者批准；landed 表示目标 ref 的尖端等于被评审的候选提交。`allow_local_land` 为 false 时，运行以 `ACCEPTED_NOT_LANDED` 结束并释放其 worktree。

`status` 只读，报告日志阶段、账本绑定、目标尖端和下一安全动作。

`recover` 先诊断。它绝不会重发已尝试的 Assignment，也绝不会重复发送评审：每次评审尝试都由发送前写入的持久记录标识，并与桥接任务及其原始输出核对。带 `--confirm-execute` 时，它会在产生任何恢复副作用之前重新校验计划、源仓库、令牌、绑定、已提交的 worker 收据、验收证据和已保存的合并候选，然后只恢复可安全重复的控制者步骤。已预留但从未开始的合并票在确认后只执行一次；已开始的票先确认记录的执程已退出，并复用绑定的静默证据，然后再恢复。已完成的落地是幂等的：重复恢复会保留目标 ref、证据和回合计数。已完成但未落地的接受运行同样是终局：恢复会从账本记录识别它，在不访问已释放 worktree 的情况下重新校验保留的评审和验收证据，如果崩溃发生在释放之前则释放冻结的 worktree，并再次报告 `ACCEPTED_NOT_LANDED`，不运行验收命令、不合并、也不发送任何回合。合并运行期间持有执行锁，因此两个 `run`/`recover` 进程无法驱动同一计划。结果不确定时，它报告持久状态并要求人工检查，而不是猜测。

评审权威来自账本，而不是 `run.json`。已封存的 `reject` 或 `repair` 判决对该 Assignment 是终局：任何重启、缓存修改或恢复都不会将其提升；需要重新准备新的尝试。在落地之前，验收说明、评审者身份、task/nonce、Assignment 代次和被评审提交都必须与封存的账本记录一致。

控制者依据持久意图提交。提交前它会暂存整个 worktree，要求暂存的路径集合等于已验证的变更集，并把确切的暂存树哈希与验收哈希一起记录。提交后它要求新提交的树等于该哈希。如果进程在提交之后、写入日志之前退出，恢复只会在提交的树、父提交、消息、路径和保留的验收证据都与意图一致时才采纳该提交，并从该已验证提交重建缺失的变更路径。被改写或篡改的提交会被拒绝而不是采纳，且恢复是幂等的。

## 授权边界

入口通过文档化的本地路径引导一个新账本和机器身份，然后签发限定范围的令牌：controller、merge、worker、reviewer 和 merge-reviewer。worker 与 reviewer 凭据分别签发，reviewer 令牌绝不会交给 worker。

这证明的是操作者授权的有界执行，不会伪造人类 Owner 凭据。人类授权由冻结的 `authorization.source` 文本、准备计划这一行为和单独的 `--confirm-execute` 行为共同表示。若周边安装要求权威人类控制者，则必须先通过既有配置的授权路径获得该控制者的授权，再执行计划。

## 失败行为

| 情况 | 行为 |
|---|---|
| 源仓库移动、基线错误、计划被篡改 | 在任何模型回合之前拒绝 |
| 固定的验收输入或可执行文件被改动或缺失 | 在派发前、执行前和恢复副作用前拒绝；命令不会运行 |
| 令牌缺失或评审者身份变化 | 在 worker 回合之前拒绝 |
| 越界变更、验收 argv 失败 | 在提交、评审和落地之前停止并保留证据 |
| 提交后、日志更新前崩溃 | 只采纳确切的已记录暂存树并重建路径；被改动的内容会被拒绝 |
| 未落地即接受（`allow_local_land` 为 false） | 终局；恢复会重新校验保留证据，绝不执行、落地或重发 |
| 评审者拒绝或要求修复 | 停止并按封存的否定判决关闭；重启后仍为终局；不声称存在修复循环 |
| 计划、收据、评审说明或缓存判决被篡改或缺失 | 拒绝；绝不从可变缓存制造接受 |
| 发送结果不确定 | 停止并报告；`recover` 绝不重发 |
| 合并意图中断 | 诊断；确认时先校验证据，要求执行者已退出，复用绑定的静默证据，使用既有合并队列恢复 |

源仓库中的用户脏文件绝不会被 stash、reset 或删除。`prepare` 记录源是否脏，并只克隆冻结提交处的已提交历史，因此未提交的工作不属于变更内容。请在准备前提交需要评审的工作。

## 限制

- 已验证路径是 Windows + 已安装 Codex CLI 的一次性回合。Herdr、其他 Harness、可信后台子进程存活判断、跨主机操作和生产迁移属于后续阶段。
- Token 计数来自原生 `turn.completed` 事件。金额成本报告为未知；不应用 provider 定价。
- 本入口组合既有组件；它们各自文档中声明的限制仍然适用。
