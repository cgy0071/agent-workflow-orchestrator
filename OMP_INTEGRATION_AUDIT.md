# OMP Integration Audit — Phase 0

后续实施与真实测试记录见 [OMP_INTEGRATION_RESULT.md](OMP_INTEGRATION_RESULT.md)；以下保留 Phase 0 当时的只读审计结论。

日期：2026-10-25。范围：只读审计；未修改运行逻辑、安装 OMP、创建 Adapter 或执行任务派发。仓库审计基准为 `feat/dashboard-chat-remote-layout` 的 `fa524d8`；本地 `main` 为 `f37d193`，关键文件 `run_job.py`、`native_events.py`、`native_worker.py`、`monitor_task.py` 与本地 `main` 无差异。本结论不代表已核对远端最新提交。

## 结论与证据等级

**现有通用单次运行器可通过 `job.json` 的 `argv` 启动经核实的非交互命令，但目前不能直接构成可信的 OMP Executor 闭环。** `run_job.py` 不限制可执行文件，却只允许 `codex`/`pi` 的原生完成契约；不指定契约时只凭退出码产生完成状态。`native_events.py` 同样只识别这两种 harness。`native_worker.py` 可把 Codex/Pi 包装成 `command` worker，但其 CLI 参数、Windows 启动器白名单和会话处理均不支持 OMP。`receipt` 可供 Monitor 读取一次性作业状态，却不能作为 `workflow_scheduler.dispatch()` 的发送目标。[源码：run_job.py](scripts/run_job.py)、[native_events.py](scripts/native_events.py)、[native_worker.py](scripts/native_worker.py)、[workflow_scheduler.py](scripts/workflow_scheduler.py)、[monitor_task.py](scripts/monitor_task.py)。

证据分类：上述是 **implemented（源码可见）**。`test_monitor.py` 的真实 OS 子进程使用 `python -c`，`test_native_completion.py` / `test_native_protocol.py` 使用事件夹具或模拟桥接，属于 **tested with local child/fixtures**，不是 **tested with real OMP**。本次只阅读测试，未重跑。仓库的 Codex/Pi 历史验证不能迁移成 OMP 结论。

仓库未声明额外 Python 依赖；[CI](.github/workflows/test.yml) 仅运行离线 `unittest` 和编码检查，没有 OMP 安装或实机集成任务。这适合作为将来保持 OMP 可选的基线，但不构成 OMP 通过证据。

## 目标环境与官方资料

`Get-Command omp,omp-swarm`、`where.exe omp` 均未定位命令；全局 `npm.cmd list -g --depth=0 --json` 及检查过的用户常见 bin、下载目录也未发现 OMP。`pi --version` 返回 `1.3.0`，但其启动脚本明确加载 `@earendil-works/pi-coding-agent`，**不是 OMP 安装版本**。因此 `omp --version`、`omp --help`、`omp launch --help`、`omp -p --help` 和最小 JSONL smoke test **未执行**；OMP 版本、精确参数、退出码与脱敏原始事件样本均为 **unverified**。这些发现只覆盖当前 PATH 和所检查位置，不证明机器上绝无其他副本。

[OMP 官方 CLI 参考](https://github.com/can1357/oh-my-pi/blob/main/docs/cli-reference.md)描述 `--cwd`、`-p`/`--print`、`--mode json`、`--no-session`、`--no-tools`，并称 JSON 模式输出 session header 和逐行事件；这只是**官方文档证据，不是本机实测**。官方 [Agent 事件参考](https://github.com/can1357/oh-my-pi/blob/main/packages/agent/README.md)列出 `agent_start`、`message_end`、`turn_end`、`agent_end` 等类型。当前 Pi parser 与这一结构有表面交集，但未验证 OMP 的具体字段、失败/中止、终止事件或 JSONL 尾部行为，不能直接复用并宣称兼容。[Swarm Extension 文档](https://github.com/open-horizon-labs/oh-omp/tree/main/packages/swarm-extension)描述独立的 `omp-swarm` 与 `.swarm_<name>/`；本机也未定位该入口，且它不属于本轮单任务验证。

## 能力矩阵（针对当前仓库承接 OMP）

| 能力 | 当前状态 | 证据与边界 |
|---|---|---|
| `launch` | unverified | `run_job.py` 的通用 `argv` 可启动任意已核实命令；OMP 实机命令、Windows 启动入口和授权派发桥接未验证。 |
| `status` | unverified | `receipt` 可读 `status.json`；目前只能反映受控 Python 作业状态，不证明 OMP 自身可查询或恢复。 |
| `collect` | unverified | 通用收集已实现：保存 `output.log`、退出码和状态；stdout/stderr 被合并，尚无 OMP 原始事件分流和任务交付清单。 |
| `cancel` | unsupported | 有经授权的超时终止分支，无独立取消 API；子进程树终止对 OMP 未验证。 |
| `resume` | unsupported | OMP 文档存在 `--resume`，但仓库没有 OMP 会话绑定、恢复和身份复核。 |
| `send` | unsupported | 通用 `command` 可调用配置的 `prompt_argv`；没有已验证的 OMP 存量会话发送目标。 |
| `identity` | unverified | 通用 Receipt 绑定已实现：关联外层 `task_id`/nonce/桥接 identity；尚未绑定真实 OMP run/session ID 与版本。 |
| `events` | unsupported | `native_events.py` 拒绝 `harness="omp"`；Pi 事件分支不能视为 OMP schema 验证。 |

`unsupported` 指**当前 Orchestrator 的 OMP 能力**，并非断言 OMP CLI 本身不提供该功能。

## 可复用控制面与关键风险

- **implemented：** Scheduler 在副作用前持久化派发预留，重复任务不盲发；Monitor 核对身份、nonce、完成序列、声明路径及文件 SHA-256 后才产生 `review_ready`；SQLite 事件、Inbox、独立 `record_review()` 和 DAG `acceptance()` 继续充当外层权威。[调度](scripts/workflow_scheduler.py)、[监控](scripts/monitor_task.py)、[审核](scripts/workflow_control.py)。这些已有检查不能替代 OMP 实机验收。
- **必须修正的进程门控：** `run_job.finalize()` 只要原生 turn 被解析为 completed，就可把先前的超时或非零退出改成 `status=done`、`completion_seq=1`（只留下 warning）；`native_worker.run()` 又以该状态判断作业成功。OMP 接入前必须使 `process_exited`、`turn_completed`、`artifacts_validated`、`review_accepted` 保持独立，非零退出/终止不确定不能升级为执行成功。
- **原始证据：** 运行器把 stderr 并入 stdout；JSONL parser 跳过坏行，包括截断尾行。若未来直接套用 Pi 分支，可能丢失 OMP 特有终止/失败信息。必须保存原始输出、退出码和解析诊断，遇不完整终止事件按未验证处理。
- **恢复与凭证：** Runner 记录 PID 但无完整的开始/结束时间、版本/模型来源或重启后对遗留 `working` Receipt 的自动核对；`argv` 及错误可能含敏感信息。作业配置和日志须限制秘密、隔离任务目录，且不能因固定 sleep 或单个事件释放工作区。
- **权限路径：** `run_job.py` 仅检查 `authorization` 为真值，不自行核对 SQLite 控制者权限或工作区归属；它必须由已授权、已预留的上层流程调用。`receipt` worker 不能由 Scheduler 直接 dispatch；直接把 OMP CLI 填进 `command.prompt_argv` 会让同步发送承担整个执行时间并受 `Transport.run()` 超时限制。应复用已有异步桥接/作业运行器，而不是让 OMP 或 Swarm 绕过预留、reconcile、Monitor、Evidence、Review Gate。

## 最小必要修改建议（**未实施；需后续授权**）

1. **先补真实环境证据。** 在受控的独立工作区安装/定位明确版本 OMP；记录可执行文件路径与哈希、四条 `--help`/`--version` 输出及退出码。依据该版本帮助运行最小只读 `-p --mode json` smoke test，保存脱敏 stdout/stderr 原件、返回码、事件类型/字段与完整末尾；核对模型/提供商标识而不保存密钥。若命令或认证不可用，继续标为 unverified。
2. **优先扩展现有桥接，不新增通用 Adapter 种类。** 在确认版本后为 OMP 增加受控单任务 `argv`、绝对 `cwd`、唯一 job/nonce、可靠启动入口与 `command` 状态/输出桥接；继续由 `run_job.py` 写一次性 Receipt。不得照搬 Pi 专有参数，也不默认支持交互会话。
3. **补齐完成判定和证据。** 为所验版本解析 OMP JSONL 原生事件，保留独立 stdout/stderr、原始日志哈希、版本、进程与时间信息；非零退出、超时、缺终止事件、坏尾行、部分产物均不能成为成功 Receipt。外层 Monitor 再校验声明文件与哈希，Reviewer 独立决定 accept/repair/reject。
4. **验证恢复与隔离。** 启动前/后崩溃、重复派工、控制器重启、旧 Receipt、同工作区并发、取消与子进程残留分别做离线故障测试；真实 OMP 测试单独报告。Swarm、`resume`、向现有会话 `send` 均留在后续可选范围。

## 后续验收门槛

- **真实 OMP：** 在无 Herdr 的隔离工作区，以记录的版本和已验证参数完成一项非交互任务；原始 JSONL/退出码/Receipt/产物哈希可追溯到同一 `task_id`、nonce、工作目录和 OMP run。实机结果与 Mock 分列。
- **执行与审核分离：** 进程退出、原生 turn 完成、交付验证、Reviewer accept 四个事实分别可见；退出码 0、单个事件或 Swarm completed 均不能直接 accept。只有固定交付哈希与独立审核通过后，外层 Scheduler 才能续接。
- **失败关闭：** OMP 缺失、版本不符、坏 JSONL、非零退出、超时/取消未确认、缺文件、旧回执及重复派工都有可复现结果；不得盲重发或提前释放占用。其他现有 Adapter 在 OMP 缺失时仍可用。

**Phase 0 停点：** 本报告是审计与实施建议，不是 OMP 兼容性通过证明。等待进一步授权后才进入安装/代码变更与真实集成验证。
