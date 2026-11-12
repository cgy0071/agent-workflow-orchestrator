"""Offline regression for the V2 recovery validator (protocol fixture, no model)."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

import validate_v2_native_loop as loop
import validate_v2_recovery as recovery

FAKE = '''import json, pathlib, re, sys
args = sys.argv[1:]
prompt = args[-1]
cwd = pathlib.Path.cwd()
if "Review this artifact" in prompt:
    task = re.search(r"Task: (\\S+)", prompt).group(1)
    nonce = re.search(r"Nonce: (\\S+)", prompt).group(1)
    text = json.dumps({"task_id": task, "nonce": nonce, "verdict": "accept", "reason": "fixture verified"})
else:
    text = "323"
print(json.dumps({"type": "thread.started", "thread_id": "thr-recovery-fixture"}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}}))
'''


class V2RecoveryFixtureTests(unittest.TestCase):
    def test_recovery_scenarios_survive_real_process_kills(self):
        with tempfile.TemporaryDirectory(prefix="v2-recovery-fixture-",
                                         ignore_cleanup_errors=True) as temp:
            root = Path(temp) / "run"
            fake = Path(temp) / "fake_codex.py"
            fake.write_text(FAKE, encoding="utf-8", newline="\n")
            loop.prepare(root, "codex", sys.executable, "one-shot", 20, "protocol-fixture", str(fake))
            result = recovery.execute_recovery(root)
            self.assertEqual(result["status"], "PROTOCOL_FIXTURE_RECOVERY_PASS",
                             json.dumps(result)[:3000])
            self.assertFalse(result["model_based_review"])
            self.assertEqual(result["evidence_class"], "protocol-fixture")
            scenarios = result["scenarios"]
            for name in ("1a_post_send_kill", "2_observer_restart", "3_concurrent_claims",
                         "4_negative_inputs", "5a_actual_usage_overrun", "5b_fenced_reserved_run",
                         "5c_admission_refused", "1b_pre_send_kill", "5d_unknown_blocks_admission",
                         "final_state"):
                self.assertIn(name, scenarios)

            # 1a: one durable attempted intent, one sent native job, no blind resend.
            post = scenarios["1a_post_send_kill"]
            self.assertEqual(post["restart_result"]["value"]["status"], "ALREADY_ATTEMPTED")
            self.assertFalse(post["restart_result"]["value"]["sent"])
            self.assertEqual(post["send_count"], 1)
            self.assertEqual(post["prekill_intent"]["send_state"], "attempted")
            self.assertIsNone(post["prekill_intent"]["submitted_at"])
            self.assertIn("dispatch_attempted", post["postkill_journal"])
            self.assertNotIn("dispatch_submitted", post["postkill_journal"])
            killed = post["kill"]
            self.assertEqual(killed["pid"], post["driver"]["identity"]["pid"])
            self.assertEqual(killed["start_time"], post["driver"]["os_process_start"])
            self.assertEqual(post["database"]["integrity_check"], "ok")

            # 2: observer restart reconciles once; the duplicate keeps event identity.
            observer = scenarios["2_observer_restart"]
            self.assertEqual(observer["completion"]["status"], "REVIEW_READY")
            self.assertEqual(observer["duplicate"]["event_id"], observer["completion"]["event_id"])
            self.assertEqual(len(observer["event_rows"]), 1)
            self.assertTrue(observer["receipt_task_nonce_ok"])
            self.assertEqual(observer["assignment"]["holds_slot"], 1)
            self.assertEqual(observer["assignment"]["review_status"], "awaiting_review")
            self.assertEqual(observer["reviews"], [])
            self.assertEqual(observer["evidence"], [])
            self.assertEqual(observer["restarted_status"], "REVIEW_READY")

            # 3: duplicate concurrent claims keep one Assignment and one command.
            claims = scenarios["3_concurrent_claims"]
            self.assertEqual(claims["winner_assignment_id"],
                             claims["db_rows"]["assignments"][0]["assignment_id"])
            self.assertEqual(len(claims["db_rows"]["assignments"]), 1)
            self.assertEqual(len(claims["db_rows"]["reservations"]), 6)
            self.assertEqual(len(claims["db_rows"]["command_log"]), 1)
            self.assertEqual(claims["db_rows"]["intents"][0]["send_state"], "ready")
            self.assertIn(claims["loser_kind"], ("stale_expected_revision", "waiting_admission"))
            self.assertTrue(claims["replay"]["replayed"])
            self.assertEqual(claims["conflict"]["message"],
                             "command_id was reused with a different tool or payload")

            # 4: intended rejections, and synthetic inputs are labelled as such.
            negatives = scenarios["4_negative_inputs"]
            self.assertTrue(negatives["stale_close"]["message"].startswith(
                "stale or invalid expected_revision; current is "))
            self.assertEqual(negatives["wrong_generation"]["code"], "state_conflict")
            self.assertEqual(negatives["wrong_generation"]["message"], "Assignment generation changed")
            self.assertEqual(negatives["wrong_identity"]["status"], "IDENTITY_CHANGED")
            self.assertTrue(negatives["wrong_identity"]["claims_retained"])
            self.assertEqual(negatives["corrected_observation"]["status"], "REVIEW_READY")
            self.assertEqual(negatives["wrong_nonce"]["status"], "identity_changed")
            self.assertEqual(negatives["wrong_nonce"]["detail"],
                             "Receipt belongs to another task/nonce.")
            synthetic = {item["name"] for item in result["synthetic_negative_inputs"]}
            self.assertEqual(synthetic, {"wrong_nonce_receipt", "wrong_identity_observation"})

            # 5a: only post-hoc detection of an actual overrun; no cancellation claim.
            overrun = scenarios["5a_actual_usage_overrun"]
            self.assertTrue(overrun["detected_post_hoc"])
            self.assertFalse(overrun["mid_call_cancellation"])
            self.assertEqual(overrun["budget_status"]["state"], "exceeded")
            self.assertEqual(overrun["job_usage"]["totals"]["input_tokens"], 11)

            # 5b: the lowered cap fences the reserved run before any send.
            fence = scenarios["5b_fenced_reserved_run"]
            self.assertEqual(fence["fence"]["message"], "Budget cap now blocks this reserved run.")
            self.assertEqual(fence["intent_after"]["send_state"], "ready")
            self.assertEqual(fence["send_count_for_run"], 0)

            # 5c: admission refuses the over-cap estimate.
            refusal = scenarios["5c_admission_refused"]
            self.assertEqual(refusal["claim"]["status"], "WAITING")
            self.assertIn("budget cap exceeded for task:tokens.input",
                          [item["reason"] for item in refusal["claim"]["reasons"]])

            # 1b: killed before the bridge, restarted dispatch does not resend.
            before = scenarios["1b_pre_send_kill"]
            self.assertEqual(before["prekill_intent"]["send_state"], "attempted")
            self.assertIsNone(before["prekill_intent"]["submitted_at"])
            self.assertIn("dispatch_attempted", before["journal"])
            self.assertNotIn("dispatch_submitted", before["journal"])
            self.assertEqual(before["restart_result"]["status"], "ALREADY_ATTEMPTED")
            self.assertFalse(before["restart_result"]["sent"])
            self.assertEqual(before["uncertain_assignment"]["holds_slot"], 1)
            self.assertTrue(before["observations"][-1]["result"]["claims_retained"])
            self.assertEqual(before["cancel"]["status"], "CLOSED")

            # 5d: unknown usage keeps blocking a later admission.
            unknown = scenarios["5d_unknown_blocks_admission"]
            self.assertGreater(unknown["workflow_after_close"]["unknown"].get("tokens.input", 0), 0)
            self.assertEqual(unknown["claim_r7"]["status"], "WAITING")
            self.assertIn("unknown usage blocks admission for tokens.input",
                          [item["reason"] for item in unknown["claim_r7"]["reasons"]])

            # Final authority state: no held slot, a real acceptance, idle workers.
            final = scenarios["final_state"]
            self.assertEqual(final["open_slots"], [])
            self.assertEqual(final["worker_status"]["status"], "idle")
            self.assertEqual(final["reviewer_status"]["status"], "idle")
            task_a = next(item for item in final["tasks"] if item["task_id"] == "NATIVE-A")
            self.assertEqual(task_a["task_state"], "accepted")
            for assignment in final["assignments"]:
                self.assertEqual(assignment["holds_slot"], 0)
            trace = [json.loads(line) for line in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
            stages = [item["stage"] for item in trace]
            for stage in ("fault_after_send_marker", "fault_after_send_killed",
                          "dispatch_after_restart", "observer_killed", "observer_restart_complete",
                          "worker_turn_recovered", "negatives", "concurrent_claims",
                          "budget_fence", "budget_admission_refused", "pre_send_kill_complete",
                          "unknown_usage_blocked", "budget_overrun_detected", "final_state"):
                self.assertIn(stage, stages)
            verified = [item for item in result["processes"] if item.get("verified")]
            self.assertTrue(verified)
            for record in verified:
                self.assertEqual(record["verified"]["start_time"], True)
                self.assertEqual(record["identity"]["pid"], record["pid"])
                self.assertTrue(record.get("kill") or record.get("exit_code") is not None)


if __name__ == "__main__":
    unittest.main()
