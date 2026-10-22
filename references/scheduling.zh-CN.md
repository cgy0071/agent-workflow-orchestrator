# 持久派工与 DAG 调度

[English](scheduling.md) | 中文

`scripts/workflow_scheduler.py` 提供单次派工、目录／资源占用及经过独立审核的
DAG 分支与汇合。交付检测和独立审核沿用原有 monitor 与 workflow_control 接口。

## 初始化、迁移与状态查询

新建状态库自动包含调度表。旧状态库由当前控制器执行增量迁移：

各维度状态及事件格式见[任务状态合同](task-state-machine.zh-CN.md)。

```sh
python scripts/workflow_scheduler.py --state STATE migrate --actor INSTANCE --version 1 --context main
python scripts/workflow_control.py --state STATE status
python scripts/workflow_control.py --state STATE inspect --task TASK-ID
```

status/inspect 使用只读 SQLite 事务，不迁移数据库、不查询真实客户端。输出包括
绑定、执行状态、派工尝试、交付、通知、审核、最近观测、错误次数、事件记录、
目录／资源占用、依赖及最近一次延后调度原因。观测时间戳不能证明失联进程已经结束。

## 单次派工与核实

先核验并绑定真实 Worker。支持 `command` 和 `herdr` 发送接口；command Worker
还需配置含一个独立 `{message}` 参数的 `prompt_argv`。请求必须填写
`workflow_state`、`dispatch_confirmed: false`、正整数 `generation`（默认 1），
以及明确的 `authorization.dispatch_worker: true` 和实际授权来源。
任务消息必须包含任务 ID 与 nonce。加载图或填写 harness 标签本身不产生授权。

```sh
python scripts/workflow_scheduler.py --state STATE dispatch --request request.json --message-file task.md --actor INSTANCE --version 1 --context main
```

脚本先读取身份、空闲状态和完成序列，在同一事务中登记未确认请求及目录／资源占用，
再次核验控制器和目标后只发送一次。最终查询和发送共用 SQLite 写事务，串行保护
交接与派工。发送返回后记录 submitted，执行仍需核实；进程崩溃留下 reserved，
发送结果不确定则记录 uncertain。这两种状态均不自动重发，也不释放执行者与占用。

独立查看真实会话后，写 EXECUTION.md：包含实际任务 ID、nonce、单独一行
`Outcome: running`，以及已经开始执行的证据；此时执行也可能已经完成。

```sh
python scripts/workflow_scheduler.py --state STATE reconcile --task TASK-ID --outcome running --evidence EXECUTION.md --request-out confirmed.json --actor INSTANCE --version 1 --context main
python scripts/monitor_task.py --request confirmed.json
```

此步骤只改变已登记派工请求的确认标记，保留原始请求哈希和核实证据哈希。
使用新导出的请求启动监控，不能编辑已有冻结请求。输出文件不会覆盖；输出写入中断后
可以重复核实并换一个新输出文件名。

确认未发送或已终止时使用 `not_sent` 或 `terminated`，证据中填写对应的精确
`Outcome:` 行。这会关闭本次尝试并释放占用，不会取消真实进程，也不自动重试。
返工或重新执行使用新任务 ID、nonce，并按需要递增 generation。

## 目录与资源隔离

工作流配置可填写 `scheduler.max_concurrency`（默认 1）和正整数容量，例如
`"resources": {"gpu": 1}`；节点的 resources 指定所需单位。
目录占用同时覆盖交付 cwd 和 Worker 真实绑定的 launch_cwd，检测相同目录、父子目录
及解析后的符号链接。仅分开交付文件夹不能掩盖共享源码目录。旧登记接口也遵守调度占用。

并行 Worker 应预先使用独立 clone/worktree 并核验绑定。同一来源目录／资源的所有
控制器必须共用状态库；占用只在该库内生效，没有自动过期。系统权限隔离、创建工作树、
资源发现及合并仍由操作者或现有调度器负责。

失败、失联和结果不确定时保留占用，直至独立核实终止。审核通过后可用
`release --task TASK-ID` 加 actor/version/context 释放；DAG tick 会在选择下游前
释放该图中仍有有效审核证据的占用。审核与交付文件需要持续保留。

## 冻结 DAG、分支与汇合

图的 nodes 数组中，每个节点包含 id、needs、未确认 request、包含任务 ID／nonce
的 message，以及可选 resources。节点与任务 ID 必须唯一；未知依赖、自依赖及环路
会被拒绝。加载后图与消息冻结，run ID 不覆盖，多个图不能复用任务 ID。
结构见 `examples/dag.json`；先替换占位值并绑定真实角色。

```sh
python scripts/workflow_scheduler.py --state STATE dag-load --run RUN-ID --file dag.json --actor INSTANCE --version 1 --context main
python scripts/workflow_scheduler.py --state STATE tick --run RUN-ID --actor INSTANCE --version 1 --context main
```

一次 tick 在容量范围内提交当前就绪节点；各 Worker 在提交后可异步执行。
下游要求所有依赖都有 review_ready 交付、绑定任务／nonce 的回执和 accept 独立审核，
每次使用前重新核对 REVIEW.md 与交付文件哈希。repair 不满足 DAG 依赖。
拒绝、失败、证据变化及已核实终止会阻断下游，其余分支继续。
直接派工或旧登记接口均不能绕开已冻结图的依赖。

DAG 节点就绪时采集真实完成序列，再冻结实际派工请求，并在 dag_binding 中记录
模板哈希。仅完成序列基线刷新，身份、授权与适配器不变。监控使用核实后导出的请求，
因此长期会话连续执行多个节点时不需要猜测未来序列值。

tick 是有限的一轮操作；控制器在记录审核或处理异常后再次调用即可。
它不创建真实 Agent、不自动给出审核结论、不重试不确定的发送。
返工使用新的限定范围图及新的任务身份。

## 监控恢复

monitor_checkpoint.json 持久保存请求哈希、起始时间、交付宽限及连续读取错误数。
核查并清理残留锁后重启监控会恢复这些值；显式 --resume 重置整体监控预算与读取错误
预算，保留交付宽限历史。只读失败按指数退避，retry_interval_seconds 默认 5 秒，
retry_max_seconds 默认 60 秒；此策略不重试发送操作。

若摘要已写入、事件尚未入库就崩溃，重启会连同回执补记事件，即使未传 --notify；
事件 ID 保证去重。监控超时只结束观测。系统时钟变化会影响时间判断，但不会产生
审核或派工许可。

## 本地可复现演示

```sh
python -B scripts/demo_scheduler.py --directory runtime/new-demo
python -B -m unittest discover -s scripts -p "test_*.py" -v
```

演示拒绝已有目录，以真实本地 Python 子进程作为测试 Worker，执行 A/B 与 JOIN，
逐字节核对结果并登记测试审核，生成 dag.json、demo-result.json 和台账。
没有创建外部 Agent 或发送外部通知。真实客户端兼容性按
[能力证据合同](capabilities.zh-CN.md) 单独记录。
