# Agent 注册表与能力证据

可选的 V2 服务包含一个最小本地 Agent 注册表。它是增量能力，不执行派发、不启动 Harness，也不会因为声明就把能力变成真实。

## Agent 记录

Agent 有稳定的项目级 `agent_id`、项目内唯一的小写 `handle`、角色（`worker`、`observer`、`reviewer` 或 `controller`）、声明的 Harness/版本/host/OS、生命周期（`pending`、`active` 或 `revoked`）以及 `last_seen_at` 心跳时间。现有活跃本地绑定会作为带 provenance 的身份导入：保留角色，Harness 版本记为 `unknown`，除非确实存有实测版本；所有导入操作保持 unknown。绑定版本号绝不当作 Harness 版本。

历史 Assignment 使用任务冻结的 `worker_binding.instance_id`，不使用该角色当前绑定。已退出的 worker 导入为 revoked，不会被误认成替代者。撤销 Agent 不改变任何 Task、Assignment、claim 或 slot；本基础层没有 Agent 过期，只有 enrollment grant 过期。

## 注册授权

controller 发放单次、项目绑定、会过期的 enrollment grant，角色只能是 `worker` 或 `observer`。grant 明确列出允许的 capability keys；客户端不能因为列表长度相同就请求 `cancel` 或 `steer`。grant secret 只写入显式选择的独占本地文件，并在平台支持时使用 POSIX 0600；Windows 上保密依赖所选目录的 ACL，0600 不是 Windows 的强制保证。数据库、事件、command log、重放响应与 CLI 参数只出现其哈希或元数据，绝不出现原始 secret。

enrollment grant 创建对应角色的 active Agent，并把 Agent token 写入另一个独占本地文件。同一命令重放返回原元数据；不同 payload 冲突。已使用、过期或撤销的 grant 被拒绝。绝不向 worker 发放 controller token。

未批准的 self-registration 创建 `pending` worker，能力为空/unknown，没有 token；它不能自批、不能成为 reviewer/controller，也没有执行权。controller 批准后发放 token，角色仍是 `worker`。

## 能力

能力行保存 operation key、level（`supported`、`unsupported`、`unknown`）、Harness 版本、OS、host、检查时间、source 与证据 hash/path。注册时的声明只以 `unknown` + `self_declared` 保存，本身永远不会成为 verified。

controller 只有同时提供显式实测 `harness_version`（不能是 `unknown`）、匹配 Agent 的 OS/host、ISO `checked_at`，以及一个存在且 SHA-256 匹配 `evidence_sha256` 的本地证据文件时，才能记录 `supported`；证据 path 会保存。匹配时会重核上下文与证据 hash；缺失、变更或不匹配会降级为 missing，required 的 `unknown` 能力匹配失败。声明或导入绑定不能验证支持。原生 session resume/send/steer 必须与 one-shot CLI 执行分别声明和取证。

## 心跳与撤销

心跳用 Agent token 认证，只更新 `last_seen_at` 与 Agent revision；绝不改变 Task/Assignment 状态、续期证据或释放 slot。心跳缺失只表示 stale/unknown，不是死亡证明。同一心跳命令重放会去重。

撤销阻止未来认证使用，保留 Agent 记录、能力历史与所有活跃 claim。controller 也可以撤销未使用的 enrollment grant。服务冻结时，撤销与心跳仍可用；新的 enroll/register/approve/capability admission 被阻断。

## CLI

controller 命令使用同一 command envelope（`command_id`、`tool`、`expected_epoch`、`payload`）。不同入口的认证不同：

- controller admission/管理（`agent-enroll`、`agent-approve`、`agent-revoke`、`grant-revoke`、`agent-capability`、`agent-list`、`agent-get`、`agent-match`）要求当前 controller 绑定与其 dispatch 权限；
- `agent-register` 用一次性 grant secret 文件认证，不用 controller 绑定；
- `agent-self-register` 是未批准本地请求，只创建没有 token 的 pending worker；
- `agent-heartbeat` 与 `agent-status` 用 Agent token 文件认证。

```sh
python -B scripts/awo_service.py --state STATE agent-enroll \
  --envelope enroll.json --secret-out /local/private/grant.txt \
  --actor INSTANCE --version 1 --context main

python -B scripts/awo_service.py --state STATE agent-register \
  --envelope register.json --grant-file /local/private/grant.txt \
  --token-out /local/private/agent-token.txt
```

```json
{
  "command_id": "cmd-enroll-0001",
  "tool": "agent.enroll",
  "expected_epoch": 0,
  "payload": {
    "role": "worker",
    "allowed_capabilities": ["status", "collect"],
    "expires_minutes": 10
  }
}
```

```json
{
  "command_id": "cmd-register-0001",
  "tool": "agent.register",
  "expected_epoch": 0,
  "payload": {
    "handle": "builder-1",
    "harness": "claude",
    "harness_version": "1.2.3",
    "host": "local",
    "os": "win32",
    "capabilities": ["status", "collect"]
  }
}
```

其他操作：

```sh
python -B scripts/awo_service.py --state STATE agent-self-register --envelope self.json
python -B scripts/awo_service.py --state STATE agent-approve \
  --envelope approve.json --token-out /local/private/approved-token.txt \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE agent-capability \
  --envelope capability.json --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE agent-heartbeat \
  --envelope heartbeat.json --token-file /local/private/agent-token.txt
python -B scripts/awo_service.py --state STATE agent-status \
  --token-file /local/private/agent-token.txt
python -B scripts/awo_service.py --state STATE agent-list \
  --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE agent-match --agent AGENT_ID \
  --required status,collect --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE agent-revoke \
  --envelope revoke.json --actor INSTANCE --version 1 --context main
python -B scripts/awo_service.py --state STATE grant-revoke \
  --envelope grant-revoke.json --actor INSTANCE --version 1 --context main
```

secret 从文件读取（`--grant-file`、`--token-file`），绝不出现在 envelope 或 argv。`--secret-out` 与 `--token-out` 必须是绝对本地路径；已存在文件绝不覆盖。

## 限制

本注册表没有 MCP server、消息、调度启用、预算、fanout 或 worktree 操作。注册声明或本地 SQLite 测试不是 live Harness 认证。未使用 V2 服务时，legacy 工作流仍是默认且继续可用。服务冻结时，新 enrollment/registration/approval/capability admission 被阻断；心跳、撤销、读取与恢复仍可用。
