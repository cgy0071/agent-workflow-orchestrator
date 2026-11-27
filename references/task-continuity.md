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
