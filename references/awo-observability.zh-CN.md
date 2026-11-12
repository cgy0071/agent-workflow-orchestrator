# 只读可观测视图与审计回执

P2 可观测模块是现有应用服务后端的可复用组件，不建立第二个控制器、Dashboard 服务、迁移权威或操作网关。父集成负责鉴权、页面接线、独立审查、启用和验收。

## 公开 API

```python
from runtime_views import (
    VIEW_NAMES, BUCKETS, ViewError, build_runtime_views,
    bucket_for_task, explain_state, render_html,
)
from receipt_export import (
    ReceiptError, build_receipt, export_receipt,
    verify_receipt, rebuild_receipt,
)

build_runtime_views(con, project_id, *, limit=200, now=None)
bucket_for_task(task, *, blockers=(), needs_attention=False)
explain_state(state, *, entity="task")
render_html(snapshot, *, view=None)

build_receipt(con, project_id, *, max_rows=10000)
export_receipt(receipt, destination, *, enabled=False, protected_roots)
verify_receipt(repository, *, expected_manifest_sha256=None, expected_commit=None)
rebuild_receipt(con, repository, *, expected_manifest_sha256=None, expected_commit=None)
```

数据库快照构建器要求调用者已开启事务，并已授权读取该项目。查询使用明确列、项目范围过滤和只读 schema 检查。旧 dag_runs 没有 project_id，因此只进行有界扫描，并仅返回 task_id 对应可见项目任务的节点；请求内容和其他项目节点标识不返回。构建器不迁移、不提交或开启事务、不轮询 worker、不运行进程、不读取证据文件。支持普通元组和 sqlite3.Row，推荐 mode=ro 连接。

项目根路径、凭据、nonce、原始命令/请求、私有消息正文和运行配置不进入视图。Dashboard 可以展示任务标题和注册表元数据；回执导出采用更严格的策略。调用者构建脱离连接的快照后结束读取事务，外部 Git 导出应在运行数据库事务之外进行。没有回执函数会自行打开生产数据库。领域错误为 ViewError 或 ReceiptError，其他数据库访问异常由调用者处理。

## 13 视图契约与覆盖矩阵

build_runtime_views 返回 format_version=1、project_id、source_max_event_seq、views、buckets、sources、limitations。每个视图包含 title、available、rows、truncated、explanation；sources 逐表记录可用性和截断情况。缺表或列不兼容时明确不可用，缺少项目本身则报错。limit 为每表 1..10000 行，相关视图继承截断标记；计数和顺序只描述有界快照。畸形、重复键、超限或包含非有限数的 JSON 会拒绝处理。

| 视图键 | 已记录来源和展示内容 | 明确边界 |
| --- | --- | --- |
| project | 项目、服务暂停状态、五桶任务数、待答问题/线程 ID | Knowledge/Docs 需要独立鉴权的子页 |
| milestones | dag_runs 中关联可见项目任务的节点，以及队列冻结契约的 DAG/依赖声明 | 没有独立里程碑/阶段 schema；隐藏未知或跨项目节点身份；不推断环路 |
| task_board | Task、Assignment、依赖、agent/能力、预算及人工关注 | 不授权派发；不完整关联可能导致阻塞分类 |
| registry | agent 批准、最后观察时间、能力声明 | 不证明存活或能力的新鲜度 |
| assignments | 代次、slot、review 状态、持久化发送观察 | worker 交付不等于接受 |
| queue | 复用 scheduler.admission_key，显示期限、老化、优先级覆盖 | 仅在选中行内排序；最终门禁仍属 scheduler |
| workspaces | 生命周期、spec 哈希、snapshot manifest/inventory 哈希 | 不读文件、不证明所有权或写者静止、不实现合并队列 |
| transcript | Run/Assignment、事件元数据、transcript/diff/test 证据 ID | 不读正文；缺验证不意味着检查通过 |
| evidence | 证据 ID、封存/当前哈希、大小、记录状态 | 哈希声明不是对原件的新验证 |
| review | review 记录、等待独立审查的任务 | accepted 与落地分离 |
| budget | Run/Task/Workflow 范围；实际、估计、未知和预留分别展示 | 不推算价格、不结算流式用量、不补零 |
| resources | v2 未释放 slot 与冻结契约的资源单位 | 不证明旧版占用、可用容量或 OS/GPU 隔离 |
| recovery | attempted/uncertain 发送、失败/阻塞/历史 Assignment、丢失/孤立 workspace | 保留占用；不自动释放或重发 |

13 个组件键对应本实施契约。架构导航会合并部分组件，并将五桶作为页面；父集成可组合这些组件，但不创建第二个状态权威。

milestones 明确包含 milestone_metadata_available=false、complete_graph=false 和缺少独立里程碑注册表的 unavailable_reason。已有 DAG 声明仍可展示。畸形 run 被跳过并说明原因，缺少 dag_runs 列会明确报告，没有相关记录时明确说明“有界快照中没有项目关联图声明”。旧图扫描和节点输出均受限。这里展示真实持久化数据，不虚构里程碑，也不授权依赖放行。

## 五桶与面向新手的解释

| 桶 | 含义 |
| --- | --- |
| Coming In | ready/queued 且没有已记录阻塞，仍需最新准入检查 |
| Going Out | active 且没有已记录阻塞或人工门禁 |
| Needs You | 草稿批准、待答问题、预算批准、review_ready、修复或拒绝决定 |
| Blocked | 前提/环境/能力/预算问题、不确定状态、不完整准入观察或无法解释的状态 |
| Completed | 仅 Task accepted、landed、finalized，各有独立标签 |

review_ready 永不进入 Completed。Assignment completed 仅表示 worker 交付，仍需独立 review。accepted 明示“已接受，尚未落地”，landed 与非代码 finalized 分开。未完成任务中，人工关注优先于普通阻塞；cancelled 不表示成功完成。

未知成本为 null，不是 0。known_actual 和 known_estimated 仅汇总相应已记录数值。任何成员缺少实际用量、存在估计或来源不完整时 total_actual 都为 null。held_reservation 是独立预留敞口，不计为花费；明确记录的实际 0 保持为 0。

explain_state 返回 label、explanation、suggested_action。例如 no_monitor 被解释为“没有近期监控观察”，并建议检查，不直接展示原始传输标签作为诊断。建议均为文字。

render_html 返回被转义的 HTML 片段：动态值和键均转义，导航仅使用固定本地锚点。不含脚本、表单、写按钮、内联事件、外链、原始运行 JSON 或修改 URL。父集成嵌入现有鉴权页面；任何后续写操作只能使用现有鉴权网关和既有命令检查。

## 回执隐私与哈希语义

build_receipt 读取有界项目来源，拒绝截断，生成脱离数据库连接的白名单审计投影。项目和事件表必须存在，可选来源缺失会明确记录。它不读取证据或 transcript 文件，只保留 Task/Assignment/review/evidence 元数据、合法生命周期、类型化数值用量和有序项目事件。ID、关联键、事件类型身份、能力键和用量维度采用稳定 SHA-256 假名，保留等值关联但不是匿名化；低熵原始标识仍可能被猜出。

凭据及凭据摘要、nonce/token、路径、提示词、消息/命令正文、自由文本、配置和原始事件 JSON 不导出。任意 payload 字段不会直接复制。非法枚举、时间、哈希或数值变为 null。白名单 SHA-256 声明（包括 evidence、request、content、spec、inventory、manifest、revision）保留原始拼写和值；嵌套声明的位置用假名表示。凭据、配置、命令、提示词、消息正文等敏感子树不会被遍历收集哈希。未知字段省略，归档不声称无损复制它们。

每个事件区分三类校验：

1. source_payload_utf8_sha256 对原始 payload_json 的确切 UTF-8 字节求哈希，包含被省略字段；它是原文承诺，不是原文副本。
2. exported_fields_sha256 和 exported_payload_sha256 分别对净化后的字段和 payload 求哈希。
3. previous_sha256 将当前导出事件链接到上一个导出项目事件；首项为 64 个 0。global_seq 必须递增，允许其他项目事件造成的间隙。

证据封存/当前哈希、review note 哈希和 workspace snapshot 哈希仍是原始对象声明。导出并不验证缺失原件，也不接受证据。manifest 引用的快照和每份回执都保留可重建范围说明。

## 隔离本地 Git 导出

默认关闭。enabled 必须严格为 Boolean True；可信控制器必须提供非空 protected_roots 列表/元组，涵盖源 checkout、运行状态、delivery/evidence 和其他受保护位置。destination 必须是已有私有父目录中全新的绝对本地路径。拒绝已有目标、双向路径重叠、嵌套 Git/bare 仓库、路径穿越、UNC/歧义 Windows 路径、符号链接以及 junction/reparse 祖先。不接管、追加、覆盖、删除、fetch、push 或自动修复。

调用者必须保证父目录私有并防止其他进程恶意替换文件系统对象。可移植路径检查不是 OS 沙箱，也不能抵御同权限并发写者的全部竞态；Windows 文件 mode 不是 ACL 配置。

新仓库包含 manifest.json 和 objects/<sha256>.json，并创建一个根提交。规范 JSON 使用 ASCII 安全的 UTF-8、排序键、紧凑分隔符、有限数和单个末尾 LF。对象以 SHA-256 寻址；manifest 记录精确大小、地址、record/event 顺序、链尾，并通过快照记录覆盖信息。相同快照产生相同内容和固定元数据的提交。API 不修改已有回执；不可变性依赖内容寻址和校验，文件系统本身并非只写一次介质。

Git 采用有界 argv、无 shell、清理后的环境、禁用 system/global 配置、空模板、禁用 hook/签名/自动维护、不获取可选锁、不允许网络协议。本地配置仅允许 Git init 的基本 core 项。拒绝 alternates、grafts、shallow、外部 Git 目录、特殊文件和硬链接文件。默认每次操作 20 秒超时、合计输出上限 8 MiB。输入 JSON 上限 1 MiB；对象/manifest 4 MiB；归档 64 MiB；脱离数据库的对象数上限 100000。任何上限都可拒绝导出。外部操作失败后的部分目标保留，不删除、不接管重试。

## 验证与审计重建

verify_receipt 验证精确文件清单、规范 JSON、大小/哈希、来源覆盖/计数、净化 schema、有序事件链、Git 根提交和提交中的 blob 身份。不刷新 index、不 checkout、不迁移、修复或写文件。缺少原始字节时不能验证其哈希声明。调用者可从仓库外提供 expected_manifest_sha256 或 expected_commit 作为可信固定值；没有外部固定值时结果明确为“仅内部一致性”。Git 历史本身不是鉴权。

rebuild_receipt 先验证，再要求调用者事务中的空 SQLite 目标，只创建：

| 审计表 | 列 |
| --- | --- |
| receipt_meta | key 主键、value_json |
| receipt_record | ordinal 主键、source_table、object_sha256、data_json |
| receipt_event | global_seq 主键、event_sha256、data_json |

净化后的完整记录/事件连同哈希和顺序被精确重建。失败时 savepoint 撤销部分审计表；外层 commit/rollback 仍属于调用者。已有主 schema 或临时 schema 对象被拒绝。不恢复 v2_* 运行表、凭据、可执行请求、文件原件、权限、未知省略字段或原始 ID。这是审计重建，不是生产恢复或事件重放。

## 应用 CLI 与看板路由

应用服务通过已签发的访问凭据暴露同一只读投影，不新增监听端口、写路由、调度动作或审核门禁。

```powershell
python -B scripts/awo_service.py --state STATE views --project-root ROOT --token-file TOKEN [--limit N]
python -B scripts/awo_service.py --state STATE views-html --project-root ROOT --token-file TOKEN [--view VIEW] [--limit N]
```

凭据必须是项目级 `controller` 或 `observer`，且没有任务、运行或知识范围。`views` 返回带 `can_write=false` 的 JSON 快照；`views-html` 额外生成 13 个视图键（`project`、`milestones`、`task_board`、`registry`、`assignments`、`queue`、`workspaces`、`transcript`、`evidence`、`review`、`budget`、`resources`、`recovery`）之一的转义 HTML 片段。每个表的 `limit` 为 1..1000。凭据通过校验后，如果台账只有访问表而缺少 V2 服务投影，返回 `{"available": false, "reason": "v2_ledger_unavailable", "can_write": false}`，而不是空结果猜测；没有访问 schema 的台账按未认证拒绝，不返回空快照。

回执操作使用同一凭据，并保持显式 opt-in：

```powershell
python -B scripts/awo_service.py --state STATE receipt-export --project-root ROOT --token-file TOKEN --destination NEW_REPO --enable-export --protected-root ROOT --protected-root STATE
python -B scripts/awo_service.py --state STATE receipt-verify --project-root ROOT --token-file TOKEN --repository REPO [--manifest-sha256 H] [--commit C]
python -B scripts/awo_service.py --state STATE receipt-rebuild --project-root ROOT --token-file TOKEN --repository REPO --audit-db EMPTY_DB [--manifest-sha256 H] [--commit C]
```

没有 `--enable-export` 时导出被拒绝。`--destination` 必须是不与任何 `--protected-root` 重叠的新绝对本地路径；至少给出一个受保护根，应包含源码 checkout 与运行状态目录。verify 和 rebuild 从不修改回执仓库；rebuild 只写入新的 SQLite 文件，并拒绝已存在的目标。

现有看板通过正常的页面令牌、Host、Origin 和 `Cache-Control: no-store` 检查，经 `GET /api/v2/views?limit=N` 提供同一投影。它以只读方式打开台账；V2 台账不存在时返回 `{"available": false, "reason": "v2_ledger_not_initialized", "can_write": false}`。页面用通俗语言标出缺失的 transcript、diff 或 milestone。POST 及其他写方法没有路由；读模型不具备派发、审核或结案权限。

## 验证与启用

在 scripts 目录使用 Python 3.10+ 与本地 Git CLI：

```text
python -B -m unittest test_runtime_views test_receipt_export -v
```

测试使用真实 SQLite、mode=ro 数据库 URI、HTML 解析、只提交合成 fixture 内容的一次性 Git 仓库，以及 manifest/content/chain 篡改、可信固定值、空库重建与回滚、环境隔离、受保护路径、硬链接和 Windows junction。平台权限不足时目录符号链接测试跳过。将 AWO_VISIBILITY_TEST_ROOT 指向外部证据目录，可使全部临时工作位于 checkout 之外。没有运行真实模型、Ubuntu 主机、GPU 或远端仓库操作。

父集成负责服务/页面接线、鉴权操作控件、独立审查和启用。实现及 fixture 测试不构成验收。停用 UI 或关闭导出开关即可停止暴露；这些模块不改变生产 schema。
