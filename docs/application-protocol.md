# AWO application protocol 1: machine operations

Run python -B scripts/awo_app.py OPERATION. All four operations require --thread DIR.
prepare also requires --spec FILE. start and recover require --confirm-execute to execute;
recover without it is diagnostic. status never dispatches. Responses are a single JSON
object: protocol (1), operation, ok, state, duplicate, result (object), error (null or
an object with code and message). Exit 0 for success, 2 for refusal or uncertainty.
Repeated prepare requires identical spec bytes and the same thread. Repeated start
reports the existing run and never resends. For an uncertain result read status and
request authorized recovery. Backend changes require returning to the original revision.

## Shared scheduler

Use the advertised `schedule` capability with a project controller bearer file:
`awo_app.py schedule --state DIR --project-root REPO --token-file FILE --expected-project-id ID ACTION`.
Use `command --file ENVELOPE.json`, `status --task TASK`, `dispatch --assignment ID --epoch N`,
or `reconcile --assignment ID --epoch N`. Success returns the existing scheduler JSON result;
a refusal returns nonzero with a diagnostic on stderr. See [project commands](project-commands.md)
for the shared authority and envelope details. This interface does not use a private per-thread ledger.
