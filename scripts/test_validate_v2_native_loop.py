"""Offline regression for the V2 native-loop validator (protocol fixture, no model)."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

import validate_v2_native_loop as loop

FAKE = '''import json, pathlib, sys
args = sys.argv[1:]
prompt = args[-1]
cwd = pathlib.Path.cwd()
root = cwd.parents[1]
resume = args[args.index("resume") + 1] if "resume" in args else "thr-fixture-1"
if "Review this artifact" in prompt:
    import re
    task = re.search(r"Task: (\\S+)", prompt).group(1)
    nonce = re.search(r"Nonce: (\\S+)", prompt).group(1)
    text = json.dumps({"task_id": task, "nonce": nonce, "verdict": "accept", "reason": "fixture verified"})
elif "Remember the secret" in prompt:
    import re
    secret = re.search(r"secret ([0-9a-f]+)", prompt).group(1)
    (root / "fixture-secret").write_text(secret, encoding="utf-8")
    text = json.dumps({"secret": secret, "answer": 323})
elif "Using the secret" in prompt:
    secret = (root / "fixture-secret").read_text(encoding="utf-8")
    text = json.dumps({"secret": secret, "answer": 324})
else:
    text = "323"
print(json.dumps({"type": "thread.started", "thread_id": resume}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}}))
'''


class V2NativeLoopFixtureTests(unittest.TestCase):
    def test_session_loop_unlocks_only_after_independent_acceptance(self):
        with tempfile.TemporaryDirectory(prefix="v2-native-fixture-") as temp:
            root = Path(temp) / "run"
            fake = Path(temp) / "fake_codex.py"
            fake.write_text(FAKE, encoding="utf-8", newline="\n")
            loop.prepare(root, "codex", sys.executable, "session", 20, "protocol-fixture", str(fake))
            result = loop.execute(root)
            self.assertEqual(result["status"], "PROTOCOL_FIXTURE_PASS", json.dumps(result)[:2000])
            self.assertFalse(result["model_based_review"])
            self.assertTrue(result["session"]["recall"])
            run = json.loads((root / "run.json").read_text(encoding="utf-8"))
            state = run["state"]
            self.assertEqual(loop.one(state, "SELECT task_state FROM v2_task WHERE task_id='NATIVE-A'")["task_state"],
                             "accepted")
            self.assertEqual(loop.one(state, "SELECT task_state FROM v2_task WHERE task_id='NATIVE-B'")["task_state"],
                             "active")
            evidence = loop.rows(state, "SELECT path,sealed,state FROM v2_evidence WHERE task_id='NATIVE-A' ORDER BY path")
            self.assertEqual([Path(item["path"]).name for item in evidence], ["HANDOFF.md", "RESULT.md"])
            self.assertTrue(all(item["sealed"] == 1 and item["state"] == "sealed" for item in evidence))
            review = loop.one(state, "SELECT verdict,actor_json FROM v2_review WHERE task_id='NATIVE-A'")
            self.assertEqual(review["verdict"], "accept")
            reviewer_binding = loop.one(state, "SELECT id FROM bindings WHERE role='planner' AND status='active'")["id"]
            worker_binding = loop.one(state, "SELECT id FROM bindings WHERE role='executor' AND status='active'")["id"]
            self.assertEqual(json.loads(review["actor_json"])["instance_id"], reviewer_binding)
            self.assertNotEqual(reviewer_binding, worker_binding)
            trace = [json.loads(line) for line in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
            stages = [item["stage"] for item in trace]
            for stage in ("workspace_create", "workspace_execution", "workspace_review",
                          "workspace_freeze", "workspace_create_b",
                          "self_review_denied", "reviewer_issuance_denied",
                          "self_review_same_agent_denied", "dependency_gate"):
                self.assertIn(stage, stages)
            denied = next(item for item in trace if item["stage"] == "self_review_denied")
            self.assertEqual(denied["status"], "DENIED")
            self.assertEqual(denied["code"], "forbidden_role")
            self.assertEqual(denied["message"], "This credential may not call awo_review_submit")
            self.assertTrue(denied["state_unchanged"])
            issuance = next(item for item in trace if item["stage"] == "reviewer_issuance_denied")
            self.assertEqual(issuance["type"], "ValueError")
            self.assertEqual(issuance["message"], "actor does not hold controller permission for review")
            same_agent = next(item for item in trace if item["stage"] == "self_review_same_agent_denied")
            self.assertEqual(same_agent["code"], "forbidden_role")
            self.assertEqual(same_agent["message"], "Executors cannot review their own delivery")
            self.assertTrue(same_agent["state_unchanged"])
            self.assertTrue(same_agent["executor_binding_restored"])
            self.assertEqual(result["self_review"]["worker_role"]["code"], "forbidden_role")
            self.assertEqual(result["self_review"]["same_agent"]["code"], "forbidden_role")

            # Managed directory workspace, bound to each assignment and exercised by
            # the native turn; Task A's accepted workspace is frozen after closure.
            aid = loop.one(state, "SELECT assignment_id FROM v2_assignment WHERE task_id='NATIVE-A'")["assignment_id"]
            workspace_paths = {}
            for task_key, expected_status, expected_paths in (
                    ("NATIVE-A", "frozen", ["HANDOFF.md", "RESULT.md", "REVIEW.md"]),
                    ("NATIVE-B", "active", ["HANDOFF.md", "RESULT.md"])):
                assignment_id = loop.one(state, "SELECT assignment_id FROM v2_assignment WHERE task_id=? "
                                                "ORDER BY created_at DESC", (task_key,))["assignment_id"]
                ws = loop.one(state, "SELECT workspace_id,kind,path,status,spec_json FROM v2_workspace "
                                     "WHERE assignment_id=?", (assignment_id,))
                self.assertIsNotNone(ws, task_key + " has no v2_workspace row")
                self.assertEqual(ws["kind"], "directory")
                self.assertEqual(ws["status"], expected_status)
                bound = loop.one(state, "SELECT workspace_id FROM v2_assignment WHERE assignment_id=?",
                                 (assignment_id,))["workspace_id"]
                self.assertEqual(bound, ws["workspace_id"])
                spec = json.loads(ws["spec_json"])
                self.assertEqual(Path(spec["path"]).resolve(), Path(ws["path"]).resolve())
                self.assertEqual(sorted(spec["allowed_paths"]), expected_paths)
                workspace_paths[task_key] = Path(ws["path"]).resolve()
            dependent = next(item for item in trace if item["stage"] == "dependent_dispatch")
            self.assertEqual(Path(dependent["job_cwd"]).resolve(), workspace_paths["NATIVE-B"])
            workspace_review = loop.one(state, "SELECT workspace_id,verdict,inventory_json "
                                               "FROM v2_workspace_review WHERE assignment_id=?", (aid,))
            self.assertEqual(workspace_review["verdict"], "accept")
            inventory = json.loads(workspace_review["inventory_json"])
            self.assertTrue(inventory["scope_ok"])
            self.assertEqual(sorted(inventory["changed_paths"]),
                             ["HANDOFF.md", "RESULT.md", "REVIEW.md"])
            frozen = next(item for item in trace if item["stage"] == "workspace_freeze")
            self.assertEqual(frozen["status"], "frozen")
            self.assertEqual(frozen["assignment_holds_slot"], 0)
            self.assertTrue(Path(frozen["quiescence_evidence"]).is_file())
            self.assertEqual(loop.one(state, "SELECT state,review_status,holds_slot FROM v2_assignment "
                                            "WHERE assignment_id=?", (aid,)),
                             {"state": "completed", "review_status": "accepted", "holds_slot": 0})
            usage = json.loads((root / "usage.json").read_text(encoding="utf-8"))
            self.assertEqual(usage["status"], "actual")
            self.assertEqual(usage["totals"]["input_tokens"], 33)


if __name__ == "__main__":
    unittest.main()
