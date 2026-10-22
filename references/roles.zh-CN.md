# 可修改的角色模板

[English](roles.md) | Chinese

下列为默认模板，不依赖外部项目协议。用户可修改职责、数量、权限及工作产物；以当前配置和指令为准。

| 默认角色 | 主要责任 | 默认边界 | 典型输出 |
|---|---|---|---|
| Planner / Engineering Lead | 设计、拆分、安排实验、审查证据、选择后续任务 | 不代替 Executor 修改实现或操作正式实验 | 方案、任务书、审核、决策 |
| Executor | 实现指定任务、运行、测量、定位首差 | 不自行改变架构、验收和授权范围 | 变更、日志、RESULT/EVIDENCE/HANDOFF |
| Paper Lead | 研究问题、文献差距、贡献、论文叙事、实验充分性 | 不将工程收益自动提升为新颖性，也不绕过规划直接改实现 | claim–evidence、补证需求、稿件 |

## 默认交接

Paper Lead 提需求 → Planner 写任务 → Executor 执行 → 监控通知 Planner → Planner 审核/续派 → Paper Lead 解释已接收结果。

角色与 harness、模型无关。任一角色可由 Codex、Pi、Claude、dsh-tui、Grok 或其他工具承担。更换工具保留任务 ID、输入、验收和未完成项，不按模型品牌判断结果。

## 调整示例

- 软件项目：Planner / Implementer / Reviewer，取消 Paper Lead。
- 快速原型：合并 Planner 与 Executor，但将独立审核保留给另一个角色。
- 论文项目：加入 Literature Reviewer 和 Experiment Auditor。
- 多分支实验：多个 Executor 各用独立可写目录，统一审核。

配置中为每个角色写 responsibilities、allowed_actions、forbidden_actions、harness 和 adapter。权限变化须来自用户授权，不能靠 Worker 编辑配置自行扩大。
