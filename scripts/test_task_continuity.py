import sqlite3
import tempfile
import unittest
from pathlib import Path

from task_continuity import card, deliver


class TaskContinuity(unittest.TestCase):
    def view(self, journal, worker=None, reviews=(), merge=None):
        return card(journal, reviews, {"accepted_evidence_id": "E1"}, merge, worker or {}, "a", "n")

    def test_observations_and_missing_receipts(self):
        active = dict(task_id="a", nonce="n", status="working")
        self.assertEqual(self.view(None)["state"], "decision")
        self.assertEqual(len(self.view(None)["decision"]["options"]), 2)
        self.assertEqual(self.view({"updated_at": "2000-01-01"}, active)["state"], "running")
        self.assertEqual(self.view({}, dict(active, nonce="old"))["state"], "unknown")
        self.assertEqual(self.view({}, dict(active, status="blocked"))["state"], "blocked")
        self.assertEqual(self.view({"phase": "dispatch_uncertain"}, active)["state"], "unknown")
        j = dict(event_id="event", changed_paths=["README.md"], verify={"returncode": 0})
        pending = self.view(j)
        self.assertEqual(pending["state"], "awaiting_review")
        self.assertIsNone(pending["accepted_evidence_id"])
        done = self.view(j, reviews=[dict(state="sealed", verdict="accept")])
        self.assertEqual(done["accepted_evidence_id"], "E1")
        self.assertIsNone(done["merge"])
        self.assertEqual(done["checks"][0]["returncode"], 0)

    def test_offline_replay_and_privacy(self):
        d = dict(project_id="p", task_id="t", attempt_id="a")
        j = {"events": [{"phase": "review_ready", "secret": "DO_NOT_SEND"}]}
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "offline" / "inbox.db"
            with self.assertRaises(sqlite3.OperationalError):
                deliver(db, "owner", d, j, self.view(j))
            db.parent.mkdir()
            for who in ("owner", "owner", "planner"):
                deliver(db, who, d, j, self.view(j))
            con = sqlite3.connect(db)
            try:
                rows = con.execute("select * from reminders").fetchall()
                self.assertEqual(len(rows), 2)
                self.assertNotIn("DO_NOT_SEND", str(rows))
            finally:
                con.close()
