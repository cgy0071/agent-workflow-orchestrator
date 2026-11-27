"""Read-only task projection and replayable local recipient inbox."""
import argparse
from contextlib import closing
import json
import sqlite3
import time
from pathlib import Path


def card(journal, reviews, task, merge, worker, attempt, nonce):
    j = journal or {}
    review = next((r["verdict"] for r in reversed(reviews) if r["state"] == "sealed"), None)
    delivered = bool(j.get("event_id") or review)
    live = worker.get("status") if (worker.get("task_id"), worker.get("nonce")) == (attempt, nonce) else None
    decision = None
    issues = []
    if j.get("phase") == "evidence_blocked":
        state = "blocked"
        issues.append("Evidence access or integrity check blocked the next read.")
    elif review in ("repair", "reject"):
        state = review
        issues.append("Inspect the sealed independent review before planning another attempt.")
    elif review == "accept":
        state = "accepted"
    elif journal is None:
        state = "decision"
        decision = {"question": "Start the prepared task?", "options": [
            ["Confirm", "Authorize the first dispatch."], ["Wait", "Leave the task unstarted."]]}
    elif j.get("phase") == "dispatch_uncertain":
        state = "unknown"
    elif delivered:
        state = "awaiting_review"
    elif live == "working":
        state = "running"
    elif live in ("blocked", "failed", "exited"):
        state = "blocked"
        issues.append("Worker reported " + live + "; inspect before recovery.")
    else:
        state = "unknown"
    check = j.get("verify") or {}
    checks = ([{"name": "Project verification", "returncode": check["returncode"]}]
              if type(check.get("returncode")) is int else [])
    if checks and checks[0]["returncode"] != 0:
        issues.append("Project verification failed.")
    return dict(version=1, state=state, decision=decision, delivered=delivered,
                review=review, merge=(merge or {}).get("state"),
                accepted_evidence_id=(task or {}).get("accepted_evidence_id") if review == "accept" else None,
                changes=j.get("changed_paths"), checks=checks, issues=issues)


EVENTS = {"review_ready": "awaiting_review", "evidence_blocked": "blocked",
          "dispatch_uncertain": "unknown", "review_refused": "review_refused",
          "closed": "review_complete", "integration_rejected": "integration_rejected",
          "accepted_not_landed": "accepted", "done": "completed"}


def deliver(db, recipient, d, journal, current):
    """Commit directly to the recipient inbox: no send/ack crash window."""
    events = [(str(i) + ":" + row["phase"], EVENTS[row["phase"]])
              for i, row in enumerate((journal or {}).get("events", [])) if row["phase"] in EVENTS]
    if (current["state"] in ("decision", "blocked")
            and (journal or {}).get("phase") != "evidence_blocked"):
        events.append(("observed:" + current["state"] + ":" + str(len((journal or {}).get("events", []))),
                       current["state"]))
    with closing(sqlite3.connect(db)) as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS reminders(project,task,attempt,event,recipient,state,"
                    "PRIMARY KEY(project,task,attempt,event,recipient))")
        added = []
        for event, state in events:
            row = (d["project_id"], d["task_id"], d["attempt_id"], event, recipient, state)
            if con.execute("INSERT OR IGNORE INTO reminders VALUES(?,?,?,?,?,?)", row).rowcount:
                added.append(dict(zip(("project", "task", "attempt", "event", "recipient", "state"), row)))
    return added


def main():
    import project_entry as entry
    p = argparse.ArgumentParser(description="Follow a prepared run into a local recipient inbox.")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--inbox", type=Path, required=True)
    p.add_argument("--recipient", required=True)
    p.add_argument("--once", action="store_true")
    a = p.parse_args()
    plan = entry.load_plan(a.plan)
    d = plan["derived"]
    while True:
        journal = entry.load_journal(Path(d["paths"]["root"]) / "run.json")
        current = entry.status(a.plan)["continuity"]
        try:
            added = deliver(a.inbox, a.recipient, d, journal, current)
        except (OSError, sqlite3.OperationalError):
            if a.once:
                raise
        else:
            for reminder in added:
                print(json.dumps(reminder), flush=True)
            if a.once or (journal or {}).get("phase") in ("done", "accepted_not_landed", "review_refused"):
                return
        time.sleep(5)


if __name__ == "__main__":
    main()
