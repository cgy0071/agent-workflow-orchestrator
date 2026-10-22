# 可修改的任务与审核合同

[English](task-and-review.md) | Chinese

## 默认任务书

```markdown
# TASK-ID — 单项目标
Owner authorization / parent task:
Assigned role / reviewer role:
Cwd / branch / inspected revision:
Inputs and identity:

## Objective
本任务要回答的问题及价值。
## Allowed / forbidden changes
具体目录、实现范围、只读输入和阶段边界。
## Acceptance
正确比较对象、检查条件、指标口径、证据复用条件。
## Deliverables
RESULT.md / EVIDENCE.md / HANDOFF.md，以及实际变更和原始日志路径。
## Failure and stop
首差、环境问题、超范围时的上报、保存和停止点。
```

该模板可适应其他领域。并非每个任务都需要 Git、综合或论文证据；按任务设置实际需要的验收。

## 默认审核

核验任务/身份和变更范围，读取关键原始证据，区分已执行/未执行、模型/实测、环境/观测/实现错误。写接收、返修、拒绝或缺证据结论，并注明基线是否更新和下一步门。

比较程序不能把空文件、丢记录或双方同错当成功。必要反例针对实际风险，不扩大成无关工具开发。原始失败保留，返修复用已核验部分。

## 可配置的续接

workflow.transitions 定义每个审核结论后的动作：next_task、repair_task、stop 或 owner_input。生成下一任务前检查持续执行授权、范围、预算、准入和 Worker 身份/目录。

新任务使用新 ID/nonce，关联父任务；不覆盖模糊投递或旧结果。收到完成通知不是新的 Owner 指令。通知/派工允许自动化，接收结论仍须由指定审核角色形成。
