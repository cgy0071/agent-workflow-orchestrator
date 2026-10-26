# OMP 单次执行集成结果

日期：2026-10-26。范围：可选 OMP Executor/Reviewer 单次 CLI 作业；Herdr、Swarm 和交互式会话不参与。本文件是 [Phase 0 审计](OMP_INTEGRATION_AUDIT.md)之后的实施记录；审计文件保留其当时的只读结论。

## 证据分级

| 能力 | 状态 | 证据与边界 |
|---|---|---|
| 启动及版本校验 | implemented；tested with real OMP | 隔离安装的 `@oh-my-pi/pi-coding-agent@18.12.6` 返回 `omp/18.12.6`；`validate_native.py` 经真实 `command` 桥接启动了 OMP。版本变化会在派发前拒绝。 |
| JSONL、退出码、日志与 Receipt | implemented；tested with real OMP | 成功及 401 失败均保留真实 JSONL、退出码、独立 stdout/stderr 和任务回执。正常 `agent_end` 只证明原生轮次结束；外层还检查进程与交付。 |
| 编码工具、交付与审核续接 | implemented；tested with real OMP | 受控桥接的 `write` 调用生成了 `PROBE.txt`；另一次真实双任务 DAG 经两次独立审核，A/B 均为 `accepted`，`command` 和原始 `receipt` 检查均通过。 |
| 离线故障回归 | tested with protocol fixtures | OS 子进程夹具覆盖非零退出、超时、坏 JSONL 尾行及部分产物；这些夹具不是 OMP 实机故障注入。 |
| 崩溃恢复、重复派工、资源占用 | existing shared scheduler；OMP-specific real run unverified | 仍沿用现有 SQLite 预留、`reconcile`、Monitor、Evidence 和 Review Gate。未对真实 OMP 注入崩溃或并发故障；不宣称这些场景已完成真实验收。 |
| 会话复用、运行中恢复、常驻会话消息、Swarm、取消 | unsupported by this bridge / unverified upstream | 仅创建 `--no-session` 作业。超时可请求终止进程树；没有经验证的 OMP 显式取消接口。 |

## 真实运行记录

隔离安装位于被 Git 忽略的 `runtime/omp-tooling-20261026/`，CLI 脚本 SHA-256 为 `4475566bfd0a89b045d4b41c7efe905360aac9662d84746a3e8ad5e735542078`。`--help` 已核对 `--print`、`--mode json`、`--cwd`、`--no-session`、`--auto-approve`、`--tools` 和 `--model`。首次无模型探针退出码 1；使用本机原有的 OpenCode Go 环境凭证时，真实 JSONL 返回 HTTP 401，提示凭证已过期。没有保存或输出密钥。

401 失败证据位于 `runtime/omp-real-adapter-final-20261026/`：`NATIVE_LOOP_FAILED`、`stage=failed`，退出码 1，`native_turn=failed`、`completion_seq=0`；`manifest.json` SHA-256 为 `fd7bbe546b7159afde18fb0ee61d0f11c51b07b959ffa49d99d198217317bc65`。失败没有触发 Reviewer 或下游任务。

成功验证使用被忽略的 `runtime/omp-proxy-profile-20261026/models.yml`，把 OMP 自定义模型指向本机 `/v1/responses` 代理；不修改全局配置或密钥。只读 smoke test 的真实回复为 `323`，退出码 0，stdout SHA-256 为 `1cc292a585c8971ef1f0cc0584868ccba833cc08d9131239c4ca4828edc45a51`。最终无 Herdr 全闭环位于 `runtime/omp-proxy-loop-final-20261026/`：`NATIVE_LOOP_PASS`，A/B 与两次 Reviewer 作业均为 `done`、退出码 0、`completion_seq=1`，账本中 A/B 均为 `accepted`；`manifest.json` SHA-256 为 `b27a90482a421b235e361e4bc6cb73e6bdb092ddb010e53e9e464481cb00520d`。

真实工具探针位于 `runtime/omp-tool-probe-restricted-20261026/`：受控 OMP 作业发出 `tool_execution_start/end`（`write`），`delivery/PROBE.txt` 内容为 `323` 加换行，SHA-256 为 `88205dc0197f62724868b8e229ddef8c9f78a07dba532fb83ba2aeac2ec19855`；回执为 `done`、退出码 0、`completion_seq=1`。默认开放全部工具时，同类探针曾在 90 秒内没有助手事件并超时；桥接现仅开放六个编码工具，后续成功测试均使用该配置。原始日志只保存在 Git 忽略的目录。

## 待完成的真实验收

上述成功只覆盖单次执行、工具写入、交付及独立审核。真实 OMP 崩溃恢复、并发、显式取消和子进程残留仍需单独故障注入；会话复用、运行中恢复、常驻会话消息和 Swarm 未接入。不同模型或提供商在首次使用时应按 [OMP 指南](references/omp.zh-CN.md)重新运行真实闭环验证。
