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

使用 `status --summary --format text` 或 `inspect --task TASK-ID --format text`
查看通用阶段、harness／adapter／identity、下一角色／动作、观测年龄、DAG 依赖、
派工状态、占用、审核结论、阻塞原因、告警、资源用量／容量及当前占用。
JSON 摘要使用 --summary；--stale-after 900 控制观测过期提醒，不推断失败、取消或
原生心跳。尚未登记的 DAG 节点也会显示；摘要不查询 Adapter，支持 Command、Receipt
及可选 Herdr 绑定。

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

两次派工准入查询与监控均使用请求中的 status_map。已核验的客户端／版本如果以
done 表示轮次结束后仍可接收新任务，应在冻结请求或加载 DAG 前明确填写
`"status_map": {"done": "idle"}`。没有映射时仍只接受 idle，不把所有 Harness 的
done 一概视为可复用。原始 working、blocked、exited、failed 即使映射为 idle 也不能
获得派工资格。身份、序列基线、任务占用、审核门及资源检查仍然生效，发送前再次核对。

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

串行节点可以复用 Worker 及其执行目录。交付证据应使用每任务独立的 cwd，或在共享
cwd 中为交付文件、绑定文件及审核文件设置任务专属路径。释放占用后仍需保留先前
已验收文件，不能覆盖；依赖检查会重新计算其哈希。加载 DAG 允许共享 cwd，因为
其中可以使用独立文件路径保留历史证据。加载时拒绝图内计划证据文件路径重叠；登记时
在共享台账内永久保留交付、绑定及审核路径，即使释放占用或终止也不解除文件名保护。
旧请求适用同样检查。这保护经台账进行的分配，不是操作系统写保护或跨台账锁。

在冻结新图之前生成任务专属路径：

```sh
python scripts/workflow_scheduler.py prepare-dag --file draft-dag.json --root /absolute/new-delivery --output prepared-dag.json
```

分配器独占创建 task-<任务ID与nonce哈希>/ 目录，固定 REVIEW.md，保留相对交付路径，
加载前替换消息中的 {task_cwd}、{review_path}、{marker}。已有目录／输出文件不覆盖。
派工前检查生成消息；若执行目录改变，重新核验绑定。不能迁移已经登记的请求。

## 冻结 DAG、分支与汇合

图的 nodes 数组中，每个节点包含 id、needs、未确认 request、包含任务 ID／nonce
的 message，以及可选 resources。节点与任务 ID 必须唯一；未知依赖、自依赖及环路
会被拒绝。加载后图与消息冻结，run ID 不覆盖，多个图不能复用任务 ID。
结构见 `examples/dag.json`；先替换占位值并绑定真实角色。

模板使用第二个 Worker 角色；初始化前在配置中声明该角色及所需并发数。
每个节点都明确给出绑定任务／nonce 的 marker，并要求 Worker 将其写入 HANDOFF.md。
marker_source 仅选择搜索位置；非空 marker 才启用这项附加检查。为兼容现有请求，
仍允许省略 marker，此时保留正常的文件、身份及 nonce 检查。文件 marker 约束交付
内容，不认证作者，也不证明验收通过；独立审核仍然必要。

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

当 scripts/ 已位于 Python 导入路径时，控制器可直接使用同样的状态目录接口：

```python
from workflow_scheduler import dag_view, snapshot, tick

graph, node_states = dag_view(state, "RUN-ID")
status = snapshot(state)
result = tick(state, "RUN-ID", actor, version, "main")
```

dag_view(state, run) 和 snapshot(state) 均只读且不查询客户端；前者返回冻结图与
节点状态字典。私有 _dag_view(connection, run) 仅供已有事务内部调用。

## 监控恢复

monitor_checkpoint.json 持久保存请求哈希、起始时间、交付宽限及连续读取错误数。
核查并清理残留锁后重启监控会恢复这些值；显式 --resume 重置整体监控预算与读取错误
预算，保留交付宽限历史。只读失败按指数退避，retry_interval_seconds 默认 5 秒，
retry_max_seconds 默认 60 秒；此策略不重试发送操作。

若摘要已写入、事件尚未入库就崩溃，重启会连同回执补记事件，即使未传 --notify；
事件 ID 保证去重。监控超时只结束观测。系统时钟变化会影响时间判断，但不会产生
审核或派工许可。

## 中途停顿、权限提示与受限继续

`incomplete`（Worker 空闲但没有有效交付，常见于额度、限流或网络中断后）、`blocked`
（通常是 harness 权限/审批提示或用量上限）和 `adapter_error`（查询持续失败）属于暂停，
不是结束。监控在每次停顿中只发布一个事件、发送一次通知，然后继续观察同一次尝试；之后
完成的交付仍会变成 `review_ready`。停顿期间同样受整体监控超时限制。控制者关闭本次尝试
（`reconcile --outcome terminated` 或 `release`）后，监控停止，不再产生事件。

尝试未关闭时，停顿事件不能登记审核，避免对暂停的结论盖过真正的交付。对 `incomplete`，
控制者可在 Worker 恢复可用后发送一次受限的继续消息：

```sh
python scripts/workflow_scheduler.py --state STATE continue --task TASK-ID --actor INSTANCE --version 1 --context main
```

前提：派发已确认执行、最新观测为 `incomplete`、Worker 绑定未变、实时身份一致、Worker
空闲且能接收输入。它发送同一任务 ID 和 nonce，不是新任务；每次停顿最多一次，每次尝试最多
`scheduler.max_continue_prompts` 次（默认 2，设为 0 关闭）。发送前先记入日志
（`continue_reserved`）；发送不确定也计入次数，且不重试。`blocked` 和 `adapter_error`
不会被继续：权限提示由 Owner 在 Worker 自己的 harness 中处理，Worker 继续后监控自动接上。
如果是 harness 拒绝了权限，继续消息无济于事，应修改 harness 权限或关闭本次尝试。
Worker 显示权限提示时派发会在发送前被拒绝，并给出这一原因。

## 本地演示

```sh
python -B scripts/demo_scheduler.py --directory runtime/new-demo
python -B scripts/demo_scheduler.py --directory runtime/new-done-demo --completion-status done
```

演示拒绝已有目录，以真实本地 Python 子进程作为 Worker，执行 A/B 与 JOIN，
逐字节核对结果并登记审核，生成 dag.json、demo-result.json 和台账。
没有创建外部 Agent 或发送外部通知。演示只展示本地编排行为；真实客户端兼容性按
[能力证据合同](capabilities.zh-CN.md) 单独记录。

第二条演示让同一个 Worker 在每轮完成后持续保持 done，通过显式映射复用
A 与 JOIN 的同一绑定，不在轮次间重置 Worker。
