# OMP one-shot bridge

English | [Chinese](omp.zh-CN.md)

The optional OMP bridge uses the existing `command` worker and per-job `receipt` adapter. It requires an installed `omp` CLI and a working model credential. Herdr is not involved. Tested CLI flags are from OMP 18.12.6; recheck `omp --help` after upgrading. Install OMP separately with Bun using the [official instructions](https://github.com/can1357/oh-my-pi), then check `omp --version`.

```powershell
bun install -g @oh-my-pi/pi-coding-agent
omp --version
```

To validate your installation without changing the workflow configuration, run this from the repository root with a new, ignored evidence directory:

```powershell
python -B scripts/validate_native.py --directory runtime/omp-check-001 --harness omp --executable C:/path/to/omp.exe --model provider/model --timeout 120
```

The validator dispatches two dependent arithmetic tasks, runs an independent reviewer, and checks both the command bridge and raw receipt. It reports `NATIVE_LOOP_PASS` only after accepted reviews. Set provider credentials in your environment; keep keys out of command arguments, task requests, and commits. On Windows, an `omp.cmd` npm launcher needs Bun on `PATH`; a native `omp.exe` also works.

For real tasks, the controller calls `native_worker.initialize(directory, "omp", executable, timeout=...)` and uses its returned `command` specification as the worker or reviewer in the normal scheduler flow. Each dispatch starts a fresh `omp --print --mode json --no-session` process in the task's absolute working directory. Coding tools (`read`, `bash`, `edit`, `write`, `grep`, `glob`) remain enabled; the task must be scoped to an authorized workspace. The bridge stores raw JSONL in `output.log`, diagnostics in `stderr.log`, and task-bound status in `status.json`.

Success requires a terminal OMP `agent_end`, a normal final assistant response, complete JSONL, exit code 0, and no timeout. Monitor then checks nonce and delivery artifacts before independent review. A process exit or an `agent_end` alone cannot accept a task. OMP conversation reuse, in-flight restore, live message sending, Swarm, and cancellation are outside this bridge's verified scope.

If OMP reports “No default model selected”, supply `--model` to the validator and configure that model's credential. A 401 means the provider credential or proxy login needs repair. Inspect the ignored validation directory's `result.json`, per-job `status.json`, `output.log`, and `stderr.log` before retrying in a new directory; uncertain dispatches must be reconciled rather than resent.
