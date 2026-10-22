# Harness 无关合同

[English](harness-adapters.md) | Chinese

窗口选择与 harness 标签是两件事，绑定和切换命令见 [初始化与切换指南](setup-and-switching.zh-CN.md)。只有客户端实际提供时才记录 conversation_id/branch_id；终端 ID 不能证明对话分支。元数据不足时使用 inbox_only，或明确预留专用主窗口，不编造标识。

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

允许状态：working、idle、done、blocked、exited、failed。`output_argv` 只在新完成序列和文件合同满足后读取输出，包含精确 completion marker。Planner 的 `identity_argv` 返回带 identity 的 JSON；`prompt_argv` 中用一个独立的 `{message}` 参数接收通知。目标会话必须在派工前验证，不能投递到“当前窗口”。

路径、身份值、命令和会话来源需冻结并按项目台账保存。不要记录凭证，不能通过此适配自行切换模型/认证/供应商。

## 兼容性验收

本技能对五种 harness 使用相同角色和交付合同。离线测试覆盖任意身份标签、跨 harness 通知和失败分支；这不等于五种客户端都完成了现场连接测试。每个真实适配在首次使用时验证：身份、working、新完成、文件/nonce、单次通知、异常不误判成功。既有可用适配直接复用；版本或身份改变后重新核验。

## 按版本记录的实测反馈

以下来自外部评测者提供的 issue-0 至 issue-3、reply-0 至 reply-3，不是受支持版本承诺，也不是维护者独立复测。原报告环境为 Skill 提交 84371b3、Windows build 10.0.26200、Herdr 0.9.3（protocol 22）、Python 3.12.4；回复称在同一机器上针对 6556c34 跑通完整闭环。未提供 harness 客户端版本，原始日志和响应文件未随本文发布。

| Herdr | Harness | agent_session | agent prompt | agent start | 序列字段 |
|---|---|---|---|---|---|
| 0.9.3 | pi；客户端版本未提供 | 报告称存在 | 报告称实际送达，审核门通过 | 报告称在空闲 cmd/PowerShell 中返回 agent_pane_busy，改用 pane run | 待原始响应确认 |
| 0.9.3 | dsh-tui；客户端版本未提供 | 报告称缺失 | 报告称返回 agent_not_ready；该环境采用 inbox_only | 所提供报告中未测试 | 待原始响应确认 |

原报告将 state_change_seq 标为 Herdr 原始字段，后续回复却引用 sequence_source=completion_seq 的回执。缺少对应的原始 agent get 响应及适配器配置，暂无法解释这一差异；归一化后的 completion_seq 数值本身不能证明原始字段来源。因此序列字段保持待确认。这里没有 Codex、Claude、Grok 或其他版本组合的实机结论。

后续记录应保留操作系统、Herdr 与 harness 版本、Skill 提交、适配器配置、原始身份及状态响应、实际启动命令和投递／审核证据，不包含凭证。此表仅作为排障起点，仍需核验当前安装版本与目标会话。Herdr 的 pane 和相关命令仅适用于 Herdr 适配器。
