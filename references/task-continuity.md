# Read a task and follow its reminders

The application status response can include a version 1 continuity card. Status
does not send or retry a task. A prepared run asks whether to start. No recent
output alone does not imply failure. A delivery, a sealed review and a local
merge are three distinct records.

To register a local recipient for a prepared run, start this observer in the
caller's terminal or process supervisor:

    python -B scripts/task_continuity.py --plan PATH/plan.json --inbox PATH/reminders.db --recipient secretary

Use an existing private parent directory for the inbox. The observer prints
newly committed reminders as JSON lines and stops at a terminal journal phase.
Restart it with the same arguments after a disconnect. It replays recorded
milestones; each recipient has one SQLite row per project/task/attempt/event.
The durable delivery is the inbox row, not the terminal print. After a terminal
disconnect the caller can read the reminders table for its recipient. Separate
trust domains should use separate inbox files. No material excerpts, command
arguments, logs or credentials are copied. This observer never starts a worker.

## Original worker session

The session details distinguish records, terminal focus and conversation resume.
Default Codex and DS Claude workers are one-shot; even a session identifier in
their output does not make them resumable. Use records to find the delivery
and workspace. A Codex worker can opt into session_mode "session" in its spec
before preparation, for a task without linked material. The reviewer remains
independent and ephemeral. Secretary conversations are never worker targets.

The optional AWO_SESSION_HOST points to an absolute, trusted local executable.
No terminal package is required for record lookup. This is a host extension
interface; no live Herdr connector is installed by setting up AWO. The host
receives one operation (inspect, focus or resume) and the expected binding as
JSON on stdin. It must independently match its local owner record and live
occupant, including project, task, attempt, nonce, role, worker instance and
native conversation. Keep pane locators in the host's private local storage.
Inspect returns the verified binding and boolean focus/resume capabilities.
Focus/resume must atomically compare the current binding before opening the
target, then return the binding and opened:true. If comparison is not possible,
refuse. Focus must not send keys; resume opens a conversation without a prompt
or model turn. Never implement focus as an unguarded switch to a cached pane.
