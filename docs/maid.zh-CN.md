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

## 交给指定项目经理，引用另一个项目的材料

例如，学生项目负责产出结果，材料项目只提供一份已发布的标定记录。先在本机建立项目目录 JSON，登记两个独立项目的路径与负责人，并为这份材料指定接收项目、文档编号、修订摘要和文档级读取凭据。完整字段见[目录示例](maid.md#route-a-task-to-a-project-manager)。凭据文件单独保管在仓库之外。

    python scripts/maid.py --thread C:/work/awo-demo --directory C:/awo/directory.json --project student --material calibration submit --spec C:/work/spec.json

任务说明中的仓库必须属于 `student`。提交时秘书核对授权和修订，把选中的材料复制进待执行的任务提示。后续撤销授权会阻止新的导出，已交付的副本仍留在会话目录，请限制该目录的访问。只用本项目材料时省略 `--material`。

按上文的 start 命令执行。结果保存在任务副本的目标 Git 分支，重新打开同一会话可查项目、负责人、材料出处和通过独立审核的证据编号。目前每个本地会话对应一个任务，目录中的负责人名称用于标识路由。
