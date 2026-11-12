# 更新记录

[English](CHANGELOG.md) | 中文

项目还没有编号版本。下面按改动合并到 `main` 的日期分组，括号里是对应的 PR 编号。所有改动都遵守同一套安全规则：每个任务只发送一次，不自动回答授权提示，独立审核记录在案才算完成，配置不等于授权。

## Unreleased

### 新增

- **可选的 V2 服务边界。** `scripts/awo_service.py` 增加增量 `v2_*` 表、默认只读的 dry-run 与由 controller 执行的保守迁移、带持久幂等的命令信封，以及针对 legacy register/dispatch 与 continuation 最终发送的 freeze/epoch 门禁。V2 默认关闭；下面列出的其他 V2 组件都是独立的可选模块。迁移复用现有 acceptance 门禁，绝不把缺失或变更的证据升级。此改动尚未提交，也未进入 `main` 或 GitHub。见 [references/awo-service.zh-CN.md](references/awo-service.zh-CN.md)。
- **最小 Agent 注册表。** controller 发放单次 enrollment grant、pending self-registration、能力证据级别、认证心跳与撤销。secret 只写入独占本地文件；supported 能力要求上下文匹配并重核本地证据哈希。不增加 scheduler/MCP/消息权限。见 [references/awo-registry.zh-CN.md](references/awo-registry.zh-CN.md)。
- **预算账本。** 从 Assignment/Task/项目推导 run/task/workflow scope，原子分 scope 预留，delta/累计 sample，per-run 暴露，authoritative close 对账，按维度 unknown，以及 controller limit/action 命令。不增加调度器启用或供应商硬上限声明。见 [references/awo-budget.zh-CN.md](references/awo-budget.zh-CN.md)。
- **其他 V2 组件（未发布）。** 以下可选模块扩展同一个台账，不新增权威：优先级调度器（排队顺序、协作槽位、准入闸门）、Assignment 工作区（隔离根目录、Git worktree、冻结源）、持久化 Agent 消息、本地知识、应用网关、协作运行时、合并队列、本地监督与配对外部草稿，以及只读运行视图和隔离审计回执。每个可写模块仍由服务 enable 开关控制，并各自有 `references/awo-*.md` 指南；只读运行视图只要 V2 schema 存在就提供经认证的快照。这些组件的独立验收另行跟踪，本条目不声称已验收。
- **Git worktree 代码闭环（未发布）。** `scripts/validate_v2_worktree_code.py` 验证可选的 V2 路径：从真实 Git worktree 执行、独立审核、本地 Merge Queue，一直到下游物化。它在一个一次性夹具上产生五次真实的已认证 Codex 轮次，创建 Task A 和依赖它的 Task B，落地 Task A，并让 Task B 保持 accepted 但未 landed。费用仍为未知；已测试范围是 Windows 加 Codex CLI。它不是任意项目的启动器。此改动尚未提交，也未进入 `main` 或 GitHub。见 [references/worktree-code-loop.zh-CN.md](references/worktree-code-loop.zh-CN.md)。

## 2026-11-06

### 改动

- **看板：每个 Agent 只在一处显示。** **立即检查 Agent** 把实时状态显示在各角色卡片上、紧挨着它的进行中任务，不再放在页面下方一个重复列出角色的独立面板里。“工作中”但台账里没有任务时，注明它在处理工作流之外的事情；空闲但有进行中任务时，提示去看任务阶段。

### 新增

- **用手机远程使用命令行**（可选，Windows；[references/remote-access.zh-CN.md](references/remote-access.zh-CN.md)）。`contrib/windows-remote/setup-ssh-tailscale.ps1` 把 Windows OpenSSH 配置为只能经 Tailscale 连接：只认密钥、只监听 Tailscale 地址、防火墙只放行 `100.64.0.0/10`、默认 shell 可选 cmd 或 PowerShell，并用开机任务等 Tailscale 就绪后再启动。`hd.cmd`/`hd.ps1` 在 SSH 里列出、读取 Herdr 窗口并向其输入；Herdr 的全屏界面在这种方式下显示不正常。
- **看板的 Tailscale 身份检查。** 在 `tailscale serve` 后面使用 `--public-origin` 时，`--tailscale-user LOGIN` 只放行 `Tailscale-User-Login` 属于所列登录名的请求（该标头由 Serve 设置，并会删除客户端自带的副本）。令牌仍然必需；本机回环请求不受影响。响应新增 `Referrer-Policy: no-referrer`。监控文档增加了通过 Tailscale 用手机访问的步骤和隐私说明。

## 2026-11-05

### 新增

- **Owner 请求收件**（`scripts/owner_intake.py`，#19）：可选的 `owner_intake` 配置。经过认证的外部通道（例如聊天机器人）把带 HMAC 签名的请求写进项目收件目录。
  - `forward` 只给空闲且身份核对通过的 Herdr Planner 发一行请求编号，不发正文。
  - `claim` 把正文作为不可信资料返回。派发仍需 Owner 直接同意。
  - `list` 只读。`forward --once` 和 `--interval` 共用一个锁。
  - 通道约定见 [references/owner-intake.zh-CN.md](references/owner-intake.zh-CN.md)。
- **Owner 通知发件箱**（`scripts/owner_notify.py`，#14）：可选的推送提醒，覆盖以下情况：
  - 授权提示、Worker 提问、失败；
  - 开始未确认、审核等待过久、超出并发上限。

  通道可选文件、Webhook（通用、飞书、钉钉、企业微信）或命令。它有独立发件箱，至少送达一次，按固定次数重试。它只读台账，不改变任务状态。

### 改动

- **强制独立审核**（#13）：同一个 Agent 不能同时担任一个任务的 Worker 和审核者，旧台账也适用。并强制台账不变量。
- **字段规则**（#15）：新任务的 ID 和 nonce 只接受字母、数字和 `._-`。
- **原生桥接残留**（#15）：桥接进程在记录结果前退出时，报告为 `exited`，任务不再一直等待。用 `native_worker.py release-lock` 或 `release` 明确清理。

### 文档

- README 改为简短介绍和快速开始；手动操作步骤、核心概念、配置文件、完整安全模型和仓库结构移到 [references/manual-workflow.zh-CN.md](references/manual-workflow.zh-CN.md)。
- README 说明了禁止自审、字段规则和桥接残留（#18）。收件文档补充了 `inbox_only` 下的提示行为和未知项目名的处理（#20）。

## 2026-11-04

### 新增

- **追问与 settle**（#11）：
  - `settle` 一步记录审核，并释放或关闭本次执行。
  - `follow-up` 根据封存的审核和当前绑定，生成下一个任务的请求。
  - `confirm-start` 和 `observe-start` 确认 Worker 确实已经开始。
  - 短任务可以用快速轮询。
- **Worker 结果块**（#12）：Worker 可以在最后输出一个 `awo-result` 代码块，结果为 `done`、`partial` 或 `blocked`。监控和看板把它显示为 Worker 的自述，不算验收通过。
- **引导续跑与历史**（#12）：
  - `continue` 可以附带简短说明，开头注明检测到的停滞原因。
  - 返工链有次数上限，防止无限返工。
  - `workflow_control.py history` 显示单个任务的流水。

## 2026-10-31

### 新增

- **看板管理控制台**（#9，`--allow-manage`）：
  - 支持核对执行、开始或停止监控、继续、关闭、释放、推进检查、设置、窗口命名和分配 Agent。
  - 每个操作先预览，再确认一次，确认时所有检查重新执行。
- **工作流统计**（#9）：从台账流水统计各阶段耗时和每日数量。

### 改动

- **停滞诊断**（#10）：
  - 监控会说明 Worker 停下的原因，例如服务商错误、额度、网络、授权提示，并给出处理建议。
  - 开始一直未确认的任务、久候未审的任务会被标出。

## 2026-10-29

### 新增

- 看板：
  - 旧的已完成任务按时间范围收起；
  - 需要处理的事项排在前面，已处理的记录折叠；
  - 任务链更易读，追问链可以切换成连线图（#3、#4、#8）。

### 文档

- 流程图加入停滞处理，README 配图已更新（#5、#6、#7）。

## 2026-10-26

### 新增

- Worker 停滞后可以对同一任务有限次地继续一次。Herdr 窗口名与角色保持同步（#2）。
- 可选的 OMP 单次任务。

## 2026-10-22 至 2026-10-25

最早的公开版本：

- 可配置的多 Agent 工作流 Skill（`SKILL.md`）、SQLite 台账、控制者绑定、经核验的交接、持久的审核关卡。
- 单次派发、带审核的 DAG 调度、资源占用、回执、离线演示。
- Herdr、command、receipt 三种适配器；Codex 和 Pi 的原生单次与会话桥接。
- 本地看板：状态、受控派发、审核关卡、结果查看、与控制者聊天、中英文界面、私有局域网 HTTPS。
- 英文入口文件，配套维护中文翻译；编码检查工具。
