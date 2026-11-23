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

## 按项目选择负责人和材料

复制[项目目录示例](../examples/maid-directory.json)到操作者管理的位置，分别登记各项目的独立根目录和负责人。需要共享材料时，在 exports 中写明来源项目、接收项目、已发布文档的 ID、该修订的 SHA-256 及限读该文档的 AWO 凭据文件路径。凭据应放在仓库和执行者工作区之外。来源项目的账本中须已有这份已发布修订。

    python scripts/maid.py --thread C:/awo/student-task --directory C:/awo/projects.json --project student --material calibration submit --spec C:/awo/student-spec.json
    python scripts/maid.py --thread C:/awo/student-task start --confirm-execute
    python scripts/maid.py --thread C:/awo/student-task status

任务说明应指定请求项目的仓库和同一会话目录。提交时会把已授权材料复制到冻结的任务提示中，之后撤销凭据仍会保留接收方已获得的副本。省略 --material 可以只选择负责人。重新查询 status 可看到项目负责人、选用材料的来源及修订，以及审核通过后的证据 ID。落地结果在会话目录下请求仓库副本的目标分支上。

本地入口按项目键选择已登记的负责人，每个会话对应一项任务，支持上述显式命令。
