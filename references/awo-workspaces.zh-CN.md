# Assignment 工作区核心

[English](awo-workspaces.md) | 中文

`scripts/workspace_manager.py` 提供 P0-4 Git worktree 与非代码目录工作区核心，位于应用服务之后，复用现有 SQLite 数据库和 `v2_assignment`。模块不创建 Task、不派发 Worker、不接受证据，也不建立第二个 Controller。应用服务负责身份、项目、Controller epoch、冻结、revision、命令幂等与事件授权。`workspace_runtime.py` 在已鉴权的 Controller 边界提供服务/CLI 接入；调度器的依赖与物化证明仍是调度器自己的门禁。启用与独立验收顺序仍为 P0-3、P0-4。

## 事务与外部操作契约

所有接收 `con` 的写函数要求调用方已开启事务。使用 `BEGIN IMMEDIATE`，服务命令全部成功后提交，异常时回滚。函数不会隐式提交或迁移。

1. 在服务迁移事务中调用 `create_workspace_schema(con)`，新增 `v2_workspace`、`v2_workspace_journal`、`v2_workspace_meta`，局部 schema 版本为 1。触发器禁止修改预留的身份字段。
2. 事务外调用 `prepare_workspace(...)`：只读路径检查和有时限的 Git 检查，返回规范化的冻结规格。
3. 在服务预留事务中调用 `reserve_workspace(con, spec)` 和 `begin_provision(con, workspace_id)`，保存返回的操作 token，然后提交。预留绑定 `v2_assignment.workspace_id` 并递增 Assignment revision，不修改 Assignment state 或 `holds_slot`。
4. 提交后调用 `provision_workspace(database_path, workspace_id, operation_token)`。独立的短 SQLite 事务先一次性消费已提交的操作票据，再执行外部操作。未提交、过期或已消费票据不能创建工作区。
5. 新服务事务调用 `finalize_workspace(con, workspace_id, report)`，传入创建函数实际返回的报告。函数不运行 Git 子进程，而是重验所有权与物化指纹；只有在冻结基线上的干净、已检查创建结果才能成为 `ready`，其他结果保留为隔离状态。

消费票据的小事务不是第二个授权入口；仅可在服务授权后调用。内部规格、报告和 Controller 证明是可信服务输入，不可直接信任 Worker 提交的 JSON。外部结果未知时必须检查，不因命令响应丢失而重放创建。

```python
# 已授权的应用服务处理器内，根目录和目标的直接父目录事先存在。
spec = prepare_workspace(
    task_id=task_id, assignment_id=assignment_id, host=local_host,
    kind="git_worktree", root=workspace_root, path=workspace_path,
    repository_id=repository_key, repo_root=source_checkout,
    base_commit=full_commit_oid, allowed_paths=["scripts/**", "references/**"],
    artifact_paths=[delivery_directory], harness=harness_version,
    resource_claims=resource_claim_ids,
)
con.execute("BEGIN IMMEDIATE")
try:
    reservation = reserve_workspace(con, spec)
    ticket = begin_provision(con, reservation["workspace_id"])
    con.commit()
except Exception:
    con.rollback()
    raise

report = provision_workspace(database_path, ticket["workspace_id"], ticket["operation_token"])
con.execute("BEGIN IMMEDIATE")
try:
    workspace = finalize_workspace(con, ticket["workspace_id"], report)
    con.commit()
except Exception:
    con.rollback()
    raise
```

`prepare_workspace` 必填关键字为 `task_id`、`assignment_id`、`host`、`kind`、`root`、`path`、`allowed_paths`。可选参数为 `repository_id`、`repo_root`、`base_commit`、`branch`、`artifact_paths=()`、`harness=""`、`resource_claims=()`。Git 模式要求配置的稳定仓库 ID 和完整 40/64 位十六进制 commit OID，默认分支为 `awo/<task>/<assignment>`。目录模式拒绝携带 Git 身份字段。工作区 ID 和所有权 token 在预检查时生成一次。

## 调度器最终检查与 claim

`check_workspace_ready(con, workspace_id, *, assignment_id=None, host=None, allow_active=False)` 是只读函数，应在调度器最终预留事务内执行。返回 `{ready, reasons, workspace_id}`；就绪时额外返回 `path`、`assignment_id`、`host`、`revision`、`base_commit`。

默认必须处于 ready 状态。检查包括当前 Assignment 绑定与状态、物理 claim、规范路径和祖先身份、所有权标记、完整且有上限的文件树指纹，以及 Git HEAD/index/ref/config/反向指针。丢失目录、迟到写入、分支切换、引用变化或标记替换都会关闭门禁。检查没有 Git 子进程、提交、迁移或数据修改。完整内容哈希可能延长调度事务；当前实现优先保证正确性和明确上限，不以缓存代替最终检查。

检查成功后调用 `mark_workspace_active(con, workspace_id)`，必须在 Assignment.state 从 `reserved`/`blocked` 改变之前执行。函数再次检查就绪、设置 `active`、清除旧静默证明，并记录 writer 状态未知。它应与 slot、预算、资源和 outbox 预留在同一事务中；回滚后恢复就绪。调度器仍须执行自己的依赖证据和 accepted 未 landed 产物物化检查；工作区指纹本身不证明依赖已应用。本模块不实现 merge queue。

在第二次有界发送事务中，父控制器必须先核验已经持久化的 `send_state="attempted"` 意图，再传入 `allow_active=True`。只有显式布尔值 True 才额外允许工作区状态为 `active`；绑定的 Assignment 仍须为 `reserved` 或 `blocked`。其他所有工作区或 Assignment 状态均拒绝，非布尔参数也关闭门禁。两种允许的工作区状态都执行相同的所有权、身份、host、claim 和完整指纹检查，不修改状态、revision、claim、意图或静默证明。核心不读取或授权发送意图；这项守卫及进程死亡后保留 attempted 意图由父控制器负责。此参数不授予重试、复用工作区或认定旧 writer 静默的权限。`mark_workspace_active` 始终使用默认门禁，因此拒绝已经 active 的工作区。

`workspace_claims(con, host=None)` 返回带 host 的物理 claim，包括工作区/Assignment/Task ID、状态、`workspaces`（启动路径和声明的 artifact 路径）以及资源关联 ID。除 `released` 外，所有状态都保留 claim。调用方还须在同一事务检查自己的调度与资源 claim。核心会检查旧版活动 dispatch/task 路径以及保留的证据/review 路径。过期、断连或 PID 消失不会自动释放 claim。不同 worktree 可编辑相同相对文件；这是后续合并风险，不是物理目录冲突。

## 所有权与恢复

一个不可变工作区属于一个 Assignment。Task、Assignment、host、仓库/checkout 身份、基线、分支、根目录/路径、允许路径、artifact、资源关联和 token 冻结在规格中。需要新工作区的重试必须新建 Assignment。释放后的记录仍保留历史，本模块不转移或循环复用所有权。

目标必须不存在，严格位于已存在的根目录内，且位于源检出与 Git 元数据之外。创建器先独占创建目录，再运行 Git，之后核验目录文件系统身份。Git 使用 argv、`worktree add --no-track -b` 和冻结提交，不使用 shell 拼接、force、prune、网络操作或自动重试。禁用 hooks，排除全局 Git 配置，拒绝本地 checkout filter 配置。

Git 工作区通过 `git rev-parse --git-path awo-workspace.json` 解析标记，必须落在已注册的 worktree 私有 Git 目录。标记包含不可变规格哈希、所有权 token 和目录身份。`.git` 按指针文件检查，同时核验私有/公共 Git 目录与反向指针。目录模式使用 `.awo-workspace.json`。已有、变化或缺失标记不能授权接管；Git 创建后、标记发布前崩溃会留下隔离资源。

`inspect_workspace(workspace)` 只读并返回外部检查报告。`recover_workspace(con, workspace_id, report, quiescence=None)` 在调用方事务应用新报告。路径缺失成为 `workspace_lost`；无所有权的资源保持隔离。存在所有权但没有静默证明时，保持 active 或转为 orphaned。服务须自行将丢失工作区投影为 Assignment/dispatch 阻塞；核心不改写这些生命周期状态。

恢复就绪或申请清理时，服务必须提供以下证明：

```python
quiescence = {
    "workspace_id": ws["workspace_id"], "assignment_id": ws["assignment_id"],
    "host": ws["host"], "revision": ws["revision"], "verified": True,
    "controller": controller_id, "reason": inspected_process_or_host_fencing_reason,
    "evidence_path": preserved_observation_path, "evidence_sha256": observation_sha256,
}
```

证据必须位于工作区之外并匹配 SHA-256。revision 绑定阻止旧证明复用。服务验证 Controller 身份以及实际进程终止/主机 fencing 证据。单个布尔值、PID 消失、分支/基线一致或标记匹配都不足。只有可预留 Assignment、原基线且内容干净并完整可知时才能恢复 ready，其他已确认静默的工作被冻结。普通检查报告不能代替成功创建报告调用 `finalize_workspace`。

## 完整清单、范围与证据保留

`collect_inventory(workspace)` 读取全部文件系统条目，包括 Git 忽略和未跟踪文件，以及 base/index 的 mode/object ID、已跟踪修改、删除、暂存/未暂存状态、检测到的重命名、相对冻结基线的已提交变化。重命名两端都计入范围。当前普通文件记录 SHA-256、字节数、mode、二进制标记和推断 MIME。删除路径保留 base/index object ID，不伪造为空内容哈希。

Glob 是相对路径且按路径段匹配：`*` 匹配一段，`**` 可跨目录。未声明修改或未知内容阻塞 scope review。链接/junction 只记录本体与目标，不跟随读取内容，并阻塞封存；逃逸会显式记录。硬链接、特殊文件、Git 编码的符号链接、无独立清单的子模块以及未合并 index 同样阻塞封存，避免把不完整材料当作完整证据。

上限：10,000 个文件系统条目、目录深度 128、单普通文件 64 MiB、每轮扫描总内容 256 MiB、每个子进程输出流 32 MiB、命令默认超时 30 秒。大文件显式标记 unknown；不完整扫描关闭门禁。缺失哈希或被截断补丁不会静默成为有效快照。

`snapshot_workspace(workspace, evidence_root)` 将当前文件字节（含未跟踪、忽略、二进制）、四种 full-index binary patch（base-to-tree、staged、unstaged、committed）与 manifest 写入内容寻址存储。证据根目录必须在仓库和工作区之外。文件先 fsync，再用原子硬链接发布；POSIX 同时 fsync 目录，不宣称 Windows 目录 fsync 持久性。证据文件系统必须支持硬链接。归档后再次读取清单以发现并发变化；失败可能留下部分 CAS blob，但不会返回有效最终 manifest。

`verify_snapshot(snapshot)` 只读核验 manifest 与全部内容/补丁哈希，不触碰工作区或数据库。干净工作区删除后仍可读取快照。生成快照不等于 Reviewer 接受；即使保留了 `scope_ok=False` 的快照，也必须通过独立范围与评审门禁。

## 显式释放与非代码任务

顺序为 `begin_release(con, workspace_id, inspection, snapshot, quiescence=...)`、提交、`release_workspace(database_path, workspace_id, operation_token)`，再在新事务执行 `finalize_release(con, workspace_id, report)`。外部清理前重新检查快照、所有权、内容和静默证据。脏目录、忽略/未跟踪内容、外来或变化的资源保留。Git 删除不用 force，分支保留。非代码目录只能删除除自身标记外为空的目录。删除失败或结果不明确时隔离并保留 claim。工作区清理永不删除 CAS 证据。

研究/非代码任务使用 `kind="directory"`，明确输出 glob（如 `["outputs/**"]`），不携带 Git 字段。保存 artifact 快照供评审，由服务控制 Task 最终归档，不要求 commit、MergeRequest 或 landed。`resource_claims` 只关联已有 claim，不预留 GPU 容量。目录模式未实现 dataset 只读挂载、GPU 进程隔离或权限控制。

Worktree 和允许路径是评审边界，不是 OS 安全沙箱。Claim 是调度记录，不是文件系统/进程锁或 GPU 隔离。使用单本地主机 SQLite ledger；不支持跨主机共享 SQLite 和 UNC/device/alternate-stream 根目录。检查 Windows 大小写/短路径规范化与 symlink/junction 祖先。同用户恶意并发替换文件系统仍需 OS 权限隔离和 fencing，本模块不会自动建立这些边界。

## 应用 CLI

`scripts/awo_service.py` 在已鉴权的 Controller 边界包装核心函数。核心函数仍负责各自的事务与外部操作契约；包装层只增加 Controller 绑定、epoch、revision 与命令幂等。

```powershell
python -B scripts/awo_service.py --state STATE workspace-create --actor ACTOR --version VERSION --envelope CREATE.json
python -B scripts/awo_service.py --state STATE workspace-recover --actor ACTOR --version VERSION --envelope RECOVER.json
python -B scripts/awo_service.py --state STATE workspace-release --actor ACTOR --version VERSION --envelope RELEASE.json
python -B scripts/awo_service.py --state STATE workspace-freeze --actor ACTOR --version VERSION --envelope FREEZE.json
python -B scripts/awo_service.py --state STATE workspace-inspect --actor ACTOR --version VERSION --workspace WORKSPACE_ID
```

每个 envelope 都是含 `command_id`、`tool`（`workspace.create`、`workspace.recover`、`workspace.release` 或 `workspace.freeze`）、`expected_epoch` 与 `payload` 的命令信封。重复执行同一命令返回已记录的结果。create 只接受 `assignment_id`、`expected_revision`、`kind`、`root`、`path`、`allowed_paths`，以及可选的 Git 字段 `repository_id`、`repo_root`、`base_commit`、`branch`。recover 接受 `workspace_id` 与 `expected_revision`；release 和 freeze 还需 `evidence_root`。调度器冻结的 Git 任务必须使用精确冻结的启动目录和物化基线提交；缺少源工作区时发送被阻断，不会退回到交付目录。Git worktree 任务的冻结消息还必须携带等于该工作区路径的绝对 `execution_cwd` 与 `sandbox: workspace-write`；交付 `cwd` 必须位于源工作区之外，native receipt 同时记录两个目录。`workspace-inspect` 只读，且不会输出 operation 或 ownership token。

## 验证

运行 `python -B -m unittest discover -s scripts -p test_workspace_manager.py -v`。设置 `AWO_WORKSPACE_TEST_ROOT` 为外部证据目录，隔离临时 Git/SQLite/process fixtures。Fixture 提交只含合成内容。测试覆盖 worktree、完整清单、scope、CAS、claim、回滚、双进程竞争、真实进程退出、脏/外来路径保留与 Windows junction。OS 不允许创建符号链接时明确 skip。这些本地测试不证明实时 Ubuntu、GPU 或 Harness/provider 兼容性。
