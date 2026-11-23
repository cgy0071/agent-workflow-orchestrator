# 秘书处：在同一个本地对话里跟进项目任务

先按[项目任务指南](../references/project-entry.zh-CN.md)准备任务说明 JSON，写清目标仓库、允许改动的文件、执行者、审核者和验证命令。以下目录应与说明中的 work_root 一致。

    python scripts/maid.py --thread C:/work/awo-demo chat

进入会话后逐行输入（把文件名换成真实路径）：

    submit C:/work/spec.json
    进度
    start --confirm-execute
    结果
    退出

提交只准备任务；明确输入 start 命令才开始执行。执行期间可从另一个终端用同一目录查询 status。下次打开同一目录，输入“进度”或“做完了吗”，就能读取账本中的任务、审核结论和证据编号。已执行任务不会因重复输入 start 再次派发；中断恢复沿用项目任务指南。

这个原型每个会话目录对应一个任务，支持上面列出的固定说法。下一阶段增加项目经理登记和跨项目读写授权，再验证模型与 Harness 分工，最后接入 QQ。
