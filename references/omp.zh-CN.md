# OMP 单次执行桥接

[English](omp.md) | 中文

可选的 OMP 桥接复用现有 `command` Worker 和逐作业 `receipt` 适配器。需要已安装的 `omp` CLI 和有效模型凭证，不依赖 Herdr。以下参数按 OMP 18.12.6 核对；升级后请重新检查 `omp --help`。按[官方说明](https://github.com/can1357/oh-my-pi)用 Bun 单独安装 OMP，再运行 `omp --version` 确认版本。

```powershell
bun install -g @oh-my-pi/pi-coding-agent
omp --version
```

在仓库根目录使用新的、被忽略的证据目录验证本机安装：

```powershell
python -B scripts/validate_native.py --directory runtime/omp-check-001 --harness omp --executable C:/path/to/omp.exe --model provider/model --timeout 120
```

验证器派发两个有依赖关系的算术任务，由独立 Reviewer 审核，并检查 `command` 桥接及原始回执。只有审核通过才报告 `NATIVE_LOOP_PASS`。通过环境变量配置提供商凭证；不要把密钥放进命令参数、任务请求或提交。Windows 上的 npm `omp.cmd` 启动器需要 `PATH` 中有 Bun；原生 `omp.exe` 也可使用。

实际任务中，控制者调用 `native_worker.initialize(directory, "omp", executable, timeout=...)`，将返回的 `command` 配置放入常规调度流程的 Worker 或 Reviewer。每次派发都会在任务的绝对工作目录启动新的 `omp --print --mode json --no-session` 进程。编码工具 `read`、`bash`、`edit`、`write`、`grep`、`glob` 保持可用，任务必须限定在已授权的工作区。原始 JSONL、诊断和任务状态分别保存到 `output.log`、`stderr.log` 和 `status.json`。

成功要求终态 `agent_end`、正常的最终助手回复、完整 JSONL、退出码 0 且未超时。之后 Monitor 还需校验 nonce 和交付文件，再由独立 Reviewer 审核。单靠进程退出或 `agent_end` 不能验收任务。会话复用、运行中恢复、向常驻进程发消息、Swarm 和取消不属于此桥接已验证范围。

出现 “No default model selected” 时，在验证命令中指定 `--model` 并配置对应凭证。HTTP 401 表示提供商凭证或代理登录需要修复。重试前检查被忽略的验证目录中的 `result.json`、逐作业 `status.json`、`output.log` 与 `stderr.log`；不确定的派发应先核对，不得盲目重发。
