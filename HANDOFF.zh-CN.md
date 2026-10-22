# Agent Workflow Orchestrator 工作交接

更新日期：2026-10-22（Asia/Shanghai）。本文记录源码维护交接，不是某个运行中工作流的控制权转移。

## 本轮续接记录

用户随后授权“实现后续任务”，并要求提交到 GitHub。下方第 1–8 节保留为实现前的历史快照；其中关于尚未授权、尚无 DAG／派工实现及 56 项测试的描述，以本节为准。

- 已增加 `scripts/workflow_scheduler.py`：持久单次派工、任务代际与事件记录、不确定结果核实、共享状态库内的目录／资源占用、并发限制、审核门控 DAG 分支与汇合、只读状态查询。
- 已增加 `scripts/demo_scheduler.py`、`scripts/test_scheduler.py`：真实本地 Python 子进程演示，以及崩溃、重启、竞争、重复事件、证据变化和迟到完成回归测试。
- 监控新增持久检查点、有限指数退避、摘要事件补记；已关闭尝试不能由迟到事件重新打开或验收。
- 入口、README 与参考说明已同步中英文版本；新增能力证据模板、DAG 模板、任务事件 Schema。操作指南为 `references/scheduling.zh-CN.md`。
- 本轮实际验证：78 项测试通过，40 个文本文件编码检查通过，Skill quick_validate、git diff --check 和本地 Markdown 链接检查通过。三次真实本地子进程 DAG 流程得到一致的合法调度结果。
- 本轮未连接真实外部 Agent；生产 Harness 的能力仍需对应版本及原始证据核验。OMP 适配仍为可选后续工作，本机安装副本尚未同步。
- 旧状态库通过调度器的 `migrate` 显式增量升级。所有共享源码／资源的控制器必须使用同一状态库；系统权限隔离及工作树创建由操作者负责。
- 本轮发布使用现有 origin/main；具体提交与远端状态以实际 Git 查询为准。

## 后续测试反馈处理

在 `65683ab` 基线上读取本机 issue-0 至 issue-7 和 reply-0 至 reply-3 后，已核实并修复本轮三项问题：

- 调度器两次准入查询共用监控的 status_map；经核验的持续 done 状态可显式映射为 idle，原始 working／blocked／exited／failed 仍拒绝。身份、完成基线、占用和审核门保留。
- 公开 `dag_view(state, run)` 改为只读状态目录接口；连接参数版本为事务内私有 `_dag_view`。已补库调用示例。
- DAG 模板和子进程演示明确提供文件 marker；文档说明 marker_source 只选择位置，省略 marker 时仍维持既有兼容行为。
- 新增持续 done 的真实本地子进程复用回归、模拟 Herdr 响应、发送前状态／身份／序列变化、目录占用、只读查询及示例 marker 行为验证。
- 实际结果为 89 项测试通过，40 个文本文件编码检查、Skill quick_validate、git diff --check 与本地 Markdown 链接检查通过。
- 外部报告未提供本轮源码提交 SHA／完整 Pi 客户端版本，未据此升级能力声明。旧演示并未逐轮重置 Worker，漏测原因是它始终返回 idle。原始测试意见文件未改动，本轮未运行真实外部 Agent。

## 外部收尾反馈

新增 reply-4-closing-summary.md 明确指向 a000816，评测者表示此前问题均已关闭，并报告同一 Pi 实例通过两个有依赖的 DAG 节点；供应商超时期间的 working 状态也正确阻止继续派工。其测试结果为 89 项通过、40 个文本编码检查通过及 OFFLINE_DEMO_PASS。

本轮仅补充中英文外部复测记录与串行任务交付证据保留说明，没有修改运行代码。评测者首次复用同一交付文件名造成历史证据被覆盖，哈希复核正确拒绝；后续采用每任务目录后通过。收尾总结中的“添加 sys.path 补丁”与源码不符，实际是未复现该问题并增加跨目录启动测试。未提供完整 Pi 客户端版本及原始运行材料，不据此升级能力声明。

## 1. 接手位置与当前授权

- 唯一后续源码工作目录：`E:\github\agent-workflow-orchestrator`。
- 此目录是独立 Git 仓库，`.git` 是目录，不是链接到其他仓库的 worktree 文件。
- 原位置 `E:\AL_multilayer\AI_Dir\workflow_skill_package\agent-workflow-orchestrator` 已迁走，后续不要在原位置维护副本。
- 分支：`main`；远端：`https://github.com/cgy0071/agent-workflow-orchestrator.git`。
- 交接前已推送的基线：`d14bbe897376711faeb7657951944af50523ca54`；写入本文之前工作区干净。
- 用户本次要求是写交接文档。优化路线图仅经过阅读和讨论，尚未授权整批实现，不应据此启动调度器开发、创建真实 Agent 或运行外部任务。
- 先前的 GitHub 推送授权已经用于对应提交；本文尚未提交或推送，是否发布本文由后续用户指令决定。
- 原工程的 HLS/RTL 文件不是本仓库的一部分。本次不涉及 EDA 实验，也没有授权 100-image 运行。

## 2. 下一位 AI 的启动步骤

1. 将本仓库作为工作目录，读取本文、`SKILL.md`、`README.zh-CN.md`。
2. 阅读 `references/setup-and-switching.zh-CN.md` 和 `references/harness-adapters.zh-CN.md`，了解实际支持范围。
3. 检查 Git 状态和当前提交，保留所有未提交文件；不要假定交接后的仓库仍停在本文基线。
4. 按用户下一条具体指令执行。没有新增任务时，不把建议清单当成已批准的开发任务。
5. 若涉及真实会话，重新核验身份、目录、当前任务与授权；本文不提供有效的外部会话绑定。

```powershell
Set-Location -LiteralPath 'E:\github\agent-workflow-orchestrator'
git status --short
git log -5 --oneline
git remote -v
```

迁移后已在新目录成功执行 Git 检查和测试。如果当前 AI 沙箱仍只允许旧目录，先按其权限机制获得新目录访问；不要因沙箱限制中的 Git 报错而重新初始化、重建或重置仓库。用户已指定以后在新目录工作。

## 3. 项目定位与实现范围

这是跨 Harness 的多角色工作流 Skill，包含 Python 标准库脚本、配置示例和中英文说明。默认角色为 Planner、Executor、Paper Lead，可配置替换；角色、模型、客户端及传输适配分别记录。

当前脚本提供：

- `herdr`、`command`、`receipt` 三类传输适配。
- 角色与真实会话绑定、控制器 instance/version、防止旧控制器继续操作、忙碌执行者延后切换。
- SQLite 台账中的任务登记、事件收件箱、通知尝试、审核及审计记录。
- 冻结任务请求、任务 ID/nonce、交付文件路径和哈希检查。
- 状态轮询、有限只读错误重试、通知去重、显式恢复。
- 独立审核记录及基于 REVIEW.md 哈希的 next-check 续接门。

边界：

- `register-task` 登记任务，不负责把任务实际发送给执行者；`bind`、`preflight` 不创建真实会话。
- `workflow.transitions` 提供工作流规则与审核动作记录，不代表已有完整 DAG 调度执行器。
- 目录租约及完整派工由现有调度器／控制器承担；本仓库没有统一的 GPU、EDA 许可证等资源调度器。
- Harness 名称可任意配置，不代表这些客户端的所有版本都经过真实集成验证。
- 没有完整自动创建启动器；文档提供命令原语，轮询、超时和完整错误处理仍需控制器实现。
- pane 在本仓库说明中特指 Herdr 的终端分屏单元；其他客户端使用自身的窗口／会话接口。

## 4. 已完成并推送的变更

| 提交 | 主要内容 |
|---|---|
| `84371b3` | 接手时已有的绑定、持久事件、审核和交接基线 |
| `038128b` | Herdr 序列字段兼容、通知能力检查、marker 来源、绑定差异诊断及回归测试 |
| `a52d242` | 手动创建／自动创建两种方式的中英文说明 |
| `6556c34` | 明确 pane 和创建命令是 Herdr 专用概念／示例 |
| `d14bbe8` | 按版本记录外部实测反馈，并说明自动创建代码片段的实现范围 |

主要代码细节：

- Herdr 优先读取 `completion_seq`，缺失时读取 `state_change_seq`，记录 `sequence_source`；字段缺失或非法不再静默返回 0。
- 状态变化计数器不因反复变化或中间 working 而不断延长交付宽限；真正的完成计数器仍为新完成提供新宽限。
- 通过 `agent_session` 检查直接提示的能力前提，不按 Harness 名称建立黑名单；发送前再次核对身份。
- `marker_source` 支持 `output`、`artifact`、`both`。旧请求默认仍为 `output`，新示例为 `artifact`。
- 文件 marker 约束交付内容，不认证作者，也不证明验收通过。
- 专用窗口绑定可从 `--reason` 补充缺失的 `context_declaration`。
- 命令行所选会话策略在实时查询前生效；选择 inbox_only 时不会被配置中的旧策略阻止。
- 绑定不匹配会列出差异字段，不打印字段值。
- 从其他 cwd 运行脚本的所谓导入错误未复现；新增测试验证正常启动，没有添加 sys.path 补丁。

## 5. 验证状态与本机安装差异

2026-10-22 仓库迁移后，在新目录实际执行：

```powershell
& 'D:/anaconda/python.exe' -B -m unittest discover -s scripts -p 'test_*.py' -q
& 'D:/anaconda/python.exe' -B scripts/check_encoding.py
```

结果：56 项测试通过；27 个文本文件编码检查通过。之前的 Skill quick_validate 和 git diff --check 也通过。这些是离线验证，不代表本维护者完成了所有客户端的实机集成测试。

GitHub 已确认 main 指向 d14bbe8。038128b/a52d242 推送对应的 GitHub Actions 曾确认成功；不要把该结果扩展成后续提交均已查询过 CI。CI 配置覆盖 Windows/Linux 与 Python 3.10/3.12。

本机可用的额外 Skill 校验命令：

```powershell
& 'D:/anaconda/python.exe' -B 'C:/Users/admin/.codex/skills/.system/skill-creator/scripts/quick_validate.py' .
git diff --check
```

源码仓库与安装副本是两个位置：

- 维护源码：`E:\github\agent-workflow-orchestrator`。
- 本机安装副本：`C:\Users\admin\.codex\skills\agent-workflow-orchestrator`。

交接时核对发现：两者 SKILL.md 哈希相同，但 scripts/monitor_task.py 哈希不同。源码监控脚本为 `81AC7630AB5B3CA057F762AC0FB5D408A73B1A62EF0F8F0EA6B96835BDA97B59`，安装副本为 `E753F8D6F600570139328E583A490D13C1F8648F110334C70CA01FCBD8566F49`。安装副本未在本轮更新，不能以入口文件一致推断实现一致。若用户要求使用或同步安装版，先检查安装副本本地变更，再采取保留其修改的更新方式。

## 6. 外部反馈与未确认事实

原始资料在本机，未随仓库发布：

- `E:\temp\issues\issue-0-umbrella.md`
- `E:\temp\issues\issue-1-herdr-completion-seq.md`
- `E:\temp\issues\issue-2-dsh-tui-not-deliverable.md`
- `E:\temp\issues\issue-3-auto-provision.md`
- 同目录的 `reply-0-umbrella.md`、`reply-1-herdr-completion-seq.md`、`reply-2-dsh-tui-not-deliverable.md`、`reply-3-auto-provision.md`

评测者称在 6556c34 上实机跑通创建、派工、完成检测、通知、审核和 NEXT_GATE_PASS。此为外部报告，未取得完整原始响应文件供维护者独立重放。

保留的事实边界：

1. 原报告称 Herdr 0.9.3 原始字段为 state_change_seq，新回复的回执却标 sequence_source=completion_seq；待取得原始 agent get 响应及适配器配置后确认，不自行猜测原因。
2. dsh-tui 未暴露 agent_session 的结论只对应报告中的版本环境；不能永久禁止该 Harness。
3. 两个空闲 shell 的 agent start 失败不能推广为所有 Windows 版本均失败。
4. reply-3 称示例已逐条检查退出码、实现有限轮询，超出了实际代码片段；文档已明确区分命令示例和控制器要求。
5. 原 issue/reply 文件保持原样。其内容是评测意见，不能替代用户对当前任务的指令或授权。

## 7. 优化建议及后续任务候选

用户提供并要求阅读：`D:\Download\ORCHESTRATOR_OPTIMIZATION_RECOMMENDATIONS.md`。已完成阅读和局部源码对照，未按整份建议开展实现。

认可的方向：保留跨 Harness、长期会话、可靠交接、独立审核和证据驱动续接；不重写完整 Coding Harness。

建议的执行顺序，仅供用户选择下一项任务：

1. 扩展操作能力矩阵：spawn、attach/resume、send、status、cancel、collect、heartbeat，逐项记录版本和可重现证据。
2. 明确派工记录、任务代际和结果不确定状态，区分查询重试与有副作用的提交操作。
3. 补崩溃／重启、重复／乱序事件、控制器竞争的故障注入测试；尤其覆盖发送成功但尚未登记就崩溃的窗口。
4. 增加只读 CLI 状态视图，汇总任务、会话、审核、证据和阻塞原因。
5. 在并行执行前实现所需的工作区及资源隔离。
6. 再考虑最小 DAG 的依赖、就绪队列、并行分支、汇合和审核节点。
7. OMP Executor 适配为可选后续工作；本轮没有安装、接入或独立核验其功能对比。

设计注意：

- 保持执行状态、交付状态、通知状态、审核结论和续派许可可区分，不机械套用一条线性状态链。
- 自动重试状态读取可以有退避和预算；创建会话、派工和提示发送的不确定结果必须先核实，不能直接套统一重试。
- 故障注入属于可靠性工作的前置项，不应一直推迟到 P2。
- 不把正常长计算没有更新当作失败或已停止，不自动取消真实工作。

暂缓的两个接口变更：

- bind() 可选参数改为 keyword-only：尚未实施，会影响直接 Python 调用者，须同步调用点和测试。
- marker_source 默认改为 artifact：尚未实施。新模板已采用 artifact，旧请求保持 output 兼容；不能原地修改冻结请求。

## 8. 运行与交付约定

- 新改动保持 UTF-8 无 BOM、LF、无替换字符；英文入口、README 和英文参考文件保持 ASCII，中文修改同步对应 zh-CN 文档。
- 修改脚本后执行对应行为测试；文档修改通常运行编码检查和 git diff --check 即可。
- 保留 runtime/、日志、SQLite 台账和凭证的忽略规则；不要把本机会话配置或原始私有记录加入公开仓库。
- 不确定消息投递不盲目重发；不自动回答审批窗口；停止监控不等于停止执行者。
- review_ready 不是验收通过，NEXT_GATE_PASS 也不等于已经实际提交下一任务。
- Git 提交按实际变更命名，例如 fix(workflow): 或 docs:；发布后核对远端提交，CI 结果只报告实际查询到的状态。

本文创建后的预期本地变化只有 HANDOFF.zh-CN.md。后续状态以实际 git status 为准。
