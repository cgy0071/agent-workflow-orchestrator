# Harness 无关合同

## 独立记录四种身份

```text
role: planner | executor | paper-lead
harness: codex | pi | claude | dsh-tui | grok | 其他实际工具
model: 实际模型，未知时标 unknown
transport: herdr | command | receipt
```

`Grok`、`DeepSeek` 也可能只是 Pi 或其他客户端中的模型；按实际运行形态记录，不能因为模型名称推断 CLI。昵称 `excuter-grok` 不决定角色权限、agent 类型或模型。Codex 可以做 Executor，Pi 可以做 Planner，Claude 可以做 Paper Lead，反向组合也成立。

## 统一边界

任务书、冻结输入、nonce、三份 Markdown、比较要求、审核结论保持同一合同。不同工具的退出码、stdout、状态事件由适配层转换，不把特定供应商的会话格式写进角色职责。

自动化按实际能力选择：

1. **Herdr 原生状态/消息接口**：实时发现 pane / terminal / agent / cwd，将 get 返回值转成统一状态；只在完成/异常时读取有限终端输出。工具名可以任意，按 Herdr 实际返回的 agent 类型核验，不硬编码 codex。
2. **工具官方 CLI/API 适配**：先用当前版本的帮助和只读身份查询确认命令，再提供返回统一 JSON 的状态/身份命令及精确会话的 prompt/resume 命令。参数列表执行，不拼 shell 字符串。随附 monitor 的 command 适配接受这些合同，但不会猜测各工具命令。
3. **独立单次 CLI 作业**：可使用 `run_job.py` 启动经确认的非交互命令，产生任务绑定的状态、真实进程返回码和 stdout 完成标记，再由 receipt 适配读取。适合提供单次运行模式的任意 harness。交互 TUI 不因进程存在就视为正在执行任务，也不因进程未退出而永远假定任务未完成。

只有交互 TUI、无官方状态/完成事件时，需要已经验证的 TUI 插件或受控外层桥接；在其就绪前标 `ADAPTER_NOT_VERIFIED`。不要通过盲目键盘注入、回答批准对话框或更改配置来补“兼容性”。

## Command 适配的规范响应

Worker 的 `status_argv` 返回 JSON：

```json
{
  "identity": {"kind": "pi", "terminal": "verified-terminal", "launch_cwd": "E:/project"},
  "status": "working",
  "completion_seq": 17
}
```

允许状态：working、idle、done、blocked、exited、failed。`terminal_argv` 只在新完成序列和文件合同满足后读取输出，包含精确 completion marker。Planner 的 `identity_argv` 返回带 identity 的 JSON；`prompt_argv` 中用一个独立的 `{message}` 参数接收通知。目标会话必须在派工前验证，不能投递到“当前窗口”。

路径、身份值、命令和会话来源需冻结并按项目台账保存。不要记录凭证，不能通过此适配自行切换模型/认证/供应商。

## 兼容性验收

本技能对五种 harness 使用相同角色和交付合同。离线测试覆盖任意身份标签、跨 harness 通知和失败分支；这不等于五种客户端都完成了现场连接测试。每个真实适配在首次使用时验证：身份、working、新完成、文件/nonce、单次通知、异常不误判成功。既有可用适配直接复用；版本或身份改变后重新核验。
