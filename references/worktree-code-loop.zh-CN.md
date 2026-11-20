# Git worktree 代码闭环：执行、审核、合并与物化

[English](worktree-code-loop.md) | 中文

这是 V2 路径上可选的端到端代码任务检查。它用一个一次性 Git 仓库，走完已安装 Codex CLI 的真实
worktree 执行、独立代码审核、本地 Merge Queue 和下游物化闸门。它是在自己的夹具上做的兼容性与
协议检查，不是启动你自己项目的工具。

## 它运行什么

校验器会创建新目录、一次性 Git 仓库、SQLite 状态、夹具凭据和两个 Task：

| Task | 工作 | 最终结果 |
|---|---|---|
| A | 在自己的 worktree 中把 `add()` 修正为求和，并扩展测试。 | 已接受（accepted），随后落到本地 integration ref。 |
| B | 在由 A 物化出的基线的第二个 worktree 中，增加 `mul()` 并扩展测试。 | 已接受（accepted），未落地（not landed）。 |

它会产生五次真实的 Codex 单次轮次：worker A、reviewer A、集成审核者、worker B、reviewer B。
控制面工作（冻结、派发、提交、合并、释放）由本地 Python 完成，不调用模型。

实际走过的路径：

1. 领取 Task A，并绑定到真实的 worktree、分支、基线和声明的路径。
2. 冻结的执行契约以 `--sandbox workspace-write` 把 worker 送入该 worktree；Harness 修改真实字节。
3. 控制面只提交发生变化的路径。独立审核者阅读代码、任务、补丁和测试证据，记录接受或拒绝。
4. 只有已接受、已关闭并冻结的工作区才能进入本地 Merge Queue。队列构建候选、运行固定测试，并要求
   独立的集成审核。
5. 落地是本地 compare-and-swap ref 更新；目标 ref 必须等于被审核的候选提交。
6. 物化记录把 Task B 绑定到已落地的基线。Task B 随后在自己的 worktree 中走同一套执行、审核和
   接受路径。

## 需要

- Python 3.10+，且 Git 在 `PATH` 中。
- 已安装并已登录的 Codex CLI。校验器使用 CLI 默认的 provider 和 model，不读取也不修改凭据。
- 一个尚不存在的新输出目录。
- 网络和模型额度，用于五次轮次。会记录 token 用量；费用未知，不能当作零。
- `HERDR_ENV` 不能为 `1`；设置了该变量时校验器会拒绝启动。

权限由你的客户端决定。Codex 单次任务以 `--sandbox workspace-write` 运行，校验器从不回答授权
提示。如果你的 CLI 配置会弹出授权提示，该轮次会按该配置等待或失败。

## 运行

在仓库根目录运行，每次使用新目录。Codex 启动器在 `PATH` 中时：

```powershell
Remove-Item Env:HERDR_ENV -ErrorAction SilentlyContinue
python -B scripts/validate_v2_worktree_code.py --directory runtime/repro/worktree-code-1
```

要使用指定安装而不是 `PATH` 搜索时，传入 `--executable`：

```powershell
python -B scripts/validate_v2_worktree_code.py --directory runtime/repro/worktree-code-2 --executable C:/path/to/codex.cmd
```

命令行参数：

| 参数 | 含义 |
|---|---|
| `--directory` | 必需。新的校验目录；已存在的路径会被拒绝。 |
| `--executable` | 可选。Codex 启动器。不提供时，校验器在 `PATH` 中查找 `codex.exe`、`codex.cmd` 或 `codex`。 |
| `--timeout` | 每轮次超时预算，单位秒，默认 300；不是整体运行上限。 |

在 POSIX shell 中，用 `env -u HERDR_ENV` 清除该变量，并使用正斜杠路径。

## 结果

完成的校验结果打印 `"status": "V2_WORKTREE_CODE_LOOP_PASS"` 并以 0 退出。其他已完成的校验结果都以 2
退出，包括真实的审核拒绝，并指出失败阶段。启动失败与此不同：如果初始化在校验根目录创建之前失败，
异常会向上抛出，进程以解释器的非零退出码（通常为 1）结束，打印 traceback，且不会留下完整的运行
目录。参数错误（例如 `--directory` 已存在，或找不到启动器）以 2 退出并只显示用法，不会创建运行
目录。运行目录保留 `result.json`、`trace.json`、`trace.jsonl`、`usage.json`、`provenance.json`、
`manifest.json`、源码快照、原生任务回执、补丁、测试结果和合并证据。

通过意味着，在这个夹具上：

- 每个 Assignment 都绑定到真实的 Git worktree、分支和基线提交，原生进程在该 worktree 中运行，
  而不是在交付目录中运行。
- 改动只触及声明的路径，测试针对这些字节运行，并由独立审核者作出决定。
- Merge Queue 验证候选，并通过本地 ref 更新落地；落地提交与被审核的候选一致。
- 下游 Task 只能从已记录的物化基线启动。
- accepted 表示已审核并关闭；landed 表示合并队列移动了目标 ref。Task A 两者都达到；Task B 已接受，
  但未落地。

通过不证明：

- 操作系统级沙箱。Git worktree 隔离的是工作文件，不是整个文件系统；越界写入会被检测并拒绝，
  而不是在操作系统层阻止。
- 通用任意项目启动器。夹具控制仓库、任务和路径。
- 其他 Harness 或操作系统。已测试范围是 Windows 加已安装的 Codex CLI；Pi、OMP、Claude、DSH、
  Ubuntu、远程 provider 和 PR 都未认证。
- provider 或 model 身份（CLI 记录为未知）、费用、崩溃恢复、调用中断或持久会话。
- 对你自己的工作给予接受。

这些路径背后的组件见 [Assignment 工作区](awo-workspaces.zh-CN.md) 和
[合并队列](awo-merge.zh-CN.md)。更小的目录产物检查见[第一次真实运行](first-real-run.zh-CN.md)。
