# 合并元数据、队列与已接受依赖基线

[English](awo-merge.md) | 中文

scripts/merge_queue.py 是仅使用 Python 标准库的领域适配器。SQLite 是唯一的
Task/Assignment 权威。代码接受、Git 落地和非代码归档分别记录。父服务负责显式注册
路由与 schema；导入模块不会执行迁移。

## 集成契约

| 函数 | 契约 |
| --- | --- |
| create_merge_schema(con) | 在调用方事务内创建附加元数据表；不提交，不修改共享 schema 版本。 |
| apply_command(con, project_id, principal, tool, payload) | 在调用方事务内验证并写元数据；不启动子进程。 |
| execute_intent(database_path, intent_id, operation_token) | 消费已提交的一次性凭据，执行外部操作，在同一数据库保存回执。 |
| recover_intent(database_path, intent_id, operation_token) | 控制器提供静止证明后检查中断操作；不重放外部副作用。 |
| read_queue(con, project_id, principal, filters=None) | 纯读取请求与候选；过滤项为 task_id、merge_request_id、state、limit（1..100）。 |
| read_intents(con, project_id, principal, task_id) | 纯读取恢复信息，包括独立 stack/archive 意图、revision 和执行器 PID；不返回令牌。 |
| materialization_gate(con, project_id, task_id, base_commit) | 检查下游 Task 的精确基线证明，重新哈希全部父项证据与评审。 |

principal 必须由服务派生，包含 agent_id、role、principal_id、project_id；不能来自
worker 请求体。领域层校验有效注册、项目、角色、Task/Assignment 所有权、generation
与 revision。写操作默认仅控制器可用；merge.review 也允许 reviewer。评审者必须
独立于该 Task 的全部 Assignment，包括竞争槽位。worker 只能读取自己的请求；
reviewer、observer、controller 可读取项目内请求。

注册为 controller 的 Agent 可以使用收窄到 reviewer 的凭据，但 agent_id 必须保持
真实身份。此时 principal 必须包含 binding_id 和整数 binding_version。模块验证服务的
binding 到 Agent 映射及当前 review 权限。integration review_json 保存 binding_id、
binding_version、binding_sha256；落地前重新验证有效绑定及全部竞争 worker 的独立性。
缺失、过期、属于其他 Agent 或只有 worker 权限的绑定均拒绝。

父服务负责认证、command ID/幂等重放、epoch 准入、freeze 策略与顶层事件。领域事件
延迟调用 awo_service._event。调用方必须使用 BEGIN IMMEDIATE，失败时回滚；模块
不提交调用方事务。Python SQLite 连接不公开 BEGIN 模式，所以 IMMEDIATE 是调用方
契约。执行器只在消费凭据与存储回执时短暂持有写事务；Git、验证器与 provider 进程
运行期间不持有 SQLite 写锁。

意图记录已启用且未冻结服务的 epoch。执行前及落地前重新检查。控制器切换后可以
重新验证绑定并完成已记录副作用的数据库确认。即使 CAS 已成功，执行器也不修改 Task。

## 命令与请求字段

所有请求拒绝未知字段。

| 命令 | 请求字段 |
| --- | --- |
| merge.policy | repository_id、repo_root、operation_root、archive_root、verify_argv、pins；可选 policy_id、timeout、max_attempts、provider_argv、receipt_key_path |
| merge.propose | task_id、assignment_id、generation、workspace_id、policy_id、target_ref、expected_revision（Task）；可选 parents、pr_id |
| merge.enqueue | merge_request_id、expected_revision（请求） |
| merge.begin | merge_request_id、expected_revision（请求）；可选 kind：build（默认）、land、poll |
| merge.review | merge_request_id、expected_revision（请求）、note_path、note_sha256 |
| merge.finish | intent_id、expected_revision（意图，来自执行结果或 read_intents） |
| merge.recover | intent_id、expected_revision（意图）、evidence_path、evidence_sha256 |
| merge.cancel | merge_request_id、expected_revision（请求） |
| merge.materialize | 下游 task_id、expected_revision（Task）、policy_id、target_ref、parents |
| artifact.begin | task_id、assignment_id、generation、policy_id、expected_revision（Task） |

parents 是有序列表，每项包含 task_id、assignment_id、generation，最多八个，不允许
自身或重复项。Git 对象必须为完整哈希，target_ref 必须为安全的 refs/heads/...，
不得等于 source。repository_id 是配置的稳定仓库身份，不是路径哈希。

policy 是不可变的控制器批准配置。三个根目录必须存在，operation/archive 必须在仓库外
且互不重叠。verify_argv 是参数列表，不能接收 worker 提供的 shell 字符串。可执行文件
及绝对路径输入文件必须在 pins 中用 SHA-256 固定；自动固定已安装 Git 的哈希。
安装程序的硬链接允许通过稳定内容检查，worker 证据硬链接仍拒绝。使用解析后的
可执行文件路径。进程使用 shell=False，有界输出和 1..300 秒超时（默认 30 秒）；
max_attempts 为 1..10（默认 3）。不得替 worker 批准其任意脚本或解释器命令。

## 原始接受证据

Task 必须 accepted；精确 Assignment 必须 completed、评审 accepted、已释放槽位，
且同槽位没有更新 generation。其独占 workspace 必须 frozen，并具有控制器核实的
静止证据及哈希。外部执行时再次检查所有权标记。父服务 v2 表中必须存在逐 Assignment
的 sealed evidence 和独立 sealed accept review。

### 原生调度器接受路径

存在 v2_scheduler_intent 的 Assignment 使用既有原生接受权威。accepted_evidence_id
可以引用 RESULT.md 或 HANDOFF.md；原始 REVIEW.md 保持 Markdown，不要求 worker
另外创建 JSON manifest 或评审。模块用原始 legacy_task_id 和 review event 调用
priority_scheduler._verified_delivery，验证已关闭且 accepted 的 intent，并重新哈希
逐 Assignment 的完整交付集合。通过 service._agent_id_for_binding 解析原始 actor 的
instance_id，验证有效绑定及版本，并检查全部竞争 Assignment 的身份独立性。
保留原始评审路径、字节、哈希及 actor，不以导入评审替代。

父服务必须使用 workspace.freeze 冻结已接受工作区。仅有 frozen 标签不足：带哈希的
证明必须包含原始 accepted closure，以及身份和 completion sequence 匹配的已观察
空闲 worker。v2_workspace_review 的 inventory 必须独立接受、哈希有效、范围合法，
且仍与独占 workspace 一致。调用方事务内使用 workspace._fingerprint 重新验证内容、
所有权及 Git 元数据，不启动子进程。事务外执行器还调用
workspace_runtime.verify_reviewed_workspace，比较完整 inventory。外部落地前及
数据库完成确认时重复相应检查。

内部派生 manifest 的精确字段：

| 类型 | 字段 |
| --- | --- |
| 共有 | task_id、assignment_id、generation、workspace_id、inventory_sha256、review_event_id、review_path、review_sha256 |
| Git | kind="git"、repository_id、source_ref、source_commit、base_commit |
| Artifact | kind="artifact"、files（每项 path、sha256、bytes） |

Git source_commit 来自封存 inventory 的 head；source_ref、base 和 repository 来自
独占 workspace。代码必须已经提交且工作树干净。Artifact files 来自封存 inventory 中
已知的 outputs/ 普通文件；空列表拒绝。派生 manifest 保存在 intent/request seal 内，
不写入 worker 工作区。原生验证失败不会回退到导入路径。父调度器的 materialization
接入仍由父集成负责。

### 已批准导入

对于服务已批准接受、且没有原生 scheduler intent 的 Assignment，Git evidence JSON 包含：

~~~json
{
  "kind": "git",
  "task_id": "T",
  "assignment_id": "A",
  "generation": 1,
  "repository_id": "configured/repo",
  "source_ref": "refs/heads/awo/T/A",
  "source_commit": "FULL_COMMIT_ID",
  "base_commit": "FULL_BASE_ID"
}
~~~

原始评审 JSON 绑定 task_id、assignment_id、generation、evidence_id、
evidence_sha256、reviewer_agent_id 和 verdict="accept"。v2_review.actor_json
必须包含服务解析的 reviewer agent_id。提出请求、预留意图、执行和成功确认时，都会
重新检查证据大小、哈希、评审内容及 workspace 绑定。

## 本地队列流程

1. 对 accepted 代码创建请求并入队；Task 仍为 accepted。
2. merge.begin 预留 build 意图。调用方先提交，再把 intent_id 和 operation_token
   交给 execute_intent。
3. 执行器冻结目标 tip，在新建 detached 操作 worktree 中进行 merge-tree 冲突预检、
   多父项真实合并、保留 merge 结构的 rebase 和批准的验证。冲突时终止临时
   rebase/merge；保留原 workspace。
4. 执行器保存回执。merge.finish 记录精确候选、验证日志/哈希和物化证明；不落地。
5. 独立 reviewer 评审 read_queue 返回的候选。评审 JSON 包含 candidate_sha256、
   task_id、assignment_id、generation、reviewer_agent_id、verdict="accept"。
   候选哈希算法为排序键、逗号/冒号紧凑分隔、ensure_ascii=True、allow_nan=False
   的 JSON，再计算 SHA-256。
6. 预留 kind=land、提交、执行、确认。执行器重新哈希两层评审，检查 source/candidate
   清洁及目标安全性，使用真实 expected-old update-ref CAS；仅 finish 写 Task landed。

build 回执 outcome=verified，并包含 candidate。所有执行结果包含 intent_id 和
intent_revision。begin 返回 intent_id、operation_token、kind、revision（意图）、
merge_request_id、state、merge_revision。遇到阻塞时返回 state=waiting_blockers
及 merge_revision，不创建意图/令牌；阻塞解除后重新准入。读取接口从不返回令牌。

同一目标只有一个未解决的外部操作。等待构建按创建时间和 ID 排序。CAS 失败重新入队，
消耗一次失败预算；耗尽后要求修复。冲突、验证失败、评审证据改变导致请求 failed，
Task repair_required。source/主检出脏、目标已检出、环境错误进入 blocked_environment，
不消耗失败次数；修复环境后可以再次 enqueue。取消仅更改元数据，不取消运行中或
已落地请求。不强制 reset 或删除 workspace。

拒绝任何已观察到的 checked-out target（即使干净）、自定义 filter/merge driver、
浅历史及 source 中的 symlink/submodule。验证后 candidate 必须保持干净；验证日志
通过捕获 stdout/stderr 保存，不能在候选目录遗留未经评审的输出。

## 派工前的堆叠基线

merge.materialize 用于没有活跃 Assignment 槽位的 draft/ready/queued/blocked 下游
Task，预留独立 stack 意图。执行器构造并验证包含全部指定 accepted 父项的基线，
不更新目标 ref。finish 保存证明，不改变 Task/Assignment 状态。执行结果为
outcome=materialized，基线在 candidate.stack_base。

父调度器在创建下游 workspace 前，针对该 Task 和精确 stack_base 调用
materialization_gate。成功返回 materialized_from、base_commit、materialization_id、
base_artifact_sha。父服务必须将 materialized_from 与完整代码依赖集合比较，并以
该精确 commit 创建 workspace。缺少证明或父项证据变化返回 ready=false、
reason=unmaterialized_dependency。本模块不派工、不隐式绕过现有调度 gate。

## 恢复与重放边界

意图状态为 ready -> started -> reported -> finished。重复执行 reported/finished
只返回保存的回执。未提交或错误令牌不能执行。started 无回执表示 uncertain，即使
超时、输出溢出或 PID 消失也保留目标锁；不会因 TTL 自动重试。

控制器在外部核实执行器及后代进程已静止后，封存包含 intent_id、executor_pid、
quiescent=true、reason 的 JSON。merge.recover 保存证明路径/哈希，随后
recover_intent 检查：

- target 等于已评审 candidate：重新验证绑定，记录恢复的 CAS 成功。
- target 等于 expected old：记录未生效/需要 rebase，不重放。
- target 为第三个值：保留不确定性与锁，要求人工对账。
- build/poll 中断：保留目录，记录已停止的环境阻塞，确认后可以创建新意图。
- archive 有完整 manifest：验证全部归档 blob 后恢复回执；缺失或部分写入则保留，
  要求显式对账。

恢复不接收调用方任意“成功”报告。finish 读取内部持久化回执及哈希，其数据库变化
随调用方事务回滚；已发生的 Git CAS 不会回滚或再次执行。

## 非代码归档

directory workspace 的 accepted manifest 使用 kind=artifact、相同身份字段以及
files 有序列表，每项为 path、sha256、bytes；唯一相对路径必须在 outputs/ 下。
执行器重新哈希源文件并写入 archive_root/EVIDENCE_SHA256/FILE_SHA256，再发布
manifest。workspace inventory 必须符合 allowed scope。继承 workspace 的
64 MiB/文件、256 MiB/inventory 限制，不作为大规模数据仓库。

artifact.begin -> execute_intent -> merge.finish 在重新验证 manifest 和每个 blob
之后将 accepted 改为 finalized。不创建 MergeRequest，不改 Git ref，不写 landed。

## PR 状态适配器：本地协议，外部兼容性未验证

P2-6 包含本地多父项引擎和批准的 argv 状态适配协议，不实现远程写入或隐式 fetch。
设置 provider_argv 及至少 32 字节的 receipt_key_path 后，请求可绑定 pr_id。
候选评审后执行 kind=poll。适配器收到批准 argv，附加：
--request ABSOLUTE_JSON_PATH --receipt ABSOLUTE_JSON_PATH。

request 字段为 format=1、intent_id、project_id、repository_id、pr_id、target_ref、
candidate_commit、expected_old_tip、candidate_sha256。receipt 包含 body 和
hmac_sha256；body 重复全部请求字段，增加 state（pending/merged/closed）、ci，
merged 时还包含 merge_commit。使用批准的本地密钥对规范 ASCII JSON 做 HMAC-SHA256。
回执绑定精确操作、PR、候选和基线，不能跨意图重放。密钥必须置于 worker workspace
之外并由控制器管理权限。本地 HMAC 证明批准适配器的身份，不是远程主机签名。

PR/CI 未就绪进入 waiting_remote，finish 后释放目标锁。merged 必须 ci=passed，
且本地已有 Git 对象的 tree 与评审候选一致，祖先包含 expected old tip，才能通过
authenticated_pr_receipt 确认 landed。远程分支状态声明由批准适配器负责；部署方必须
另行提供、评审真实只读 provider 实现及授权。

simulated_provider 测试仅运行本地 Python 模拟程序，不访问网络，验证签名、绑定及
队列行为，不证明 GitHub、GitLab 或其他真实 provider 的兼容性。不声称已完成真实远程
连接或生产 PR 落地。

## 应用 CLI

`scripts/awo_service.py` 在不暴露操作 token 的前提下执行已提交的合并意图：

```powershell
python -B scripts/awo_service.py --state STATE merge-execute --project-root ROOT --token-file TOKEN --intent INTENT_ID --expected-epoch EPOCH
python -B scripts/awo_service.py --state STATE merge-recover --project-root ROOT --token-file TOKEN --intent INTENT_ID --expected-epoch EPOCH
```

凭据必须是项目级 controller；任务、运行、代际和知识范围一律拒绝。服务先鉴权、核对
当前 controller epoch 与冻结状态，在短事务中读取已提交票据，然后在启动外部执行器前
回滚。`execute_intent` 在启动 Git 或 provider 之前一次性消费持久票据；`merge-recover`
只恢复已记录外部尝试的意图。命令响应丢失时用原 command_id 对账，绝不盲目重跑外部操作。

## 运行边界与验证

worktree 和 SQLite reservation 是协作式进程约束，不是 OS 隔离。同用户的外部 writer
仍可能在检查与 CAS 之间改文件或检出目标；服务之外的 writer 需要主机 fencing。
expected-old CAS 仍可阻止覆盖已被独立推进的 ref。验证器/provider 是控制器信任的
配置。保留 worktree、回执与不确定操作；不安装后台 daemon 或自动 prune 策略。

~~~powershell
python -B -m unittest discover -s scripts -p "test_merge_queue.py" -v
python -B scripts/check_encoding.py
~~~

测试使用真实一次性 Git 仓库、SQLite、子进程验证器，并让真实子进程在 CAS 成功后、
回执保存前退出。NativeMergeTests 还使用父项目实际 scheduler 和 workspace_runtime
夹具，验证 Markdown 接受、观察后冻结、收窄 reviewer 凭据、身份独立性、重启执行器、
Git CAS、artifact 归档和篡改拒绝。缺少 workspace_runtime 的旧基线会显式跳过该类；
跳过不等于原生验证通过。worker-evidence 单独记录加载父项目当前依赖和这四个独占
源文件的测试，包含依赖哈希及原始输出。父服务已通过 `application_runtime.py` 注册
`merge-execute` 与 `merge-recover`，在 `create_schema` 安装 merge schema，并要求代码
依赖提供 `merge_queue.materialization_gate` 证明。该接线由父集成测试覆盖，不把 worker
的夹具结果扩展到真实 provider。
