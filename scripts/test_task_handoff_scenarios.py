"""Recovery scenarios use a disposable Git repository and a protocol fixture."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import project_entry as entry
from test_project_entry import APP_FIXED, ENTRY, Fixture

MAID = Path(os.environ.get("AWO_TEST_MAID_ROOT", ENTRY.parents[2] / "maid")) / "maid.py"


class HandoffScenarios(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hc-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.f = Fixture(self.base)
        self.root = self.base / "r"
        self.spec = self.base / "spec.json"
        self.spec.write_text(json.dumps(self.f.spec(self.root, "refs/heads/demo")), encoding="utf-8")
        self.index = self.base / "index.json"
        self.index.write_text(json.dumps({"projects": {"p": {"repository": str(self.f.repo),
            "runs": {"r": {"spec": str(self.spec), "thread": str(self.root)}}}}}), encoding="utf-8")
        self.env = dict(os.environ, MAID_AWO_ROOT=str(ENTRY.parents[1]), **self.f.write_mode())

    def run_process(self, args):
        return subprocess.run([sys.executable, "-B", *map(str, args)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", env=self.env, timeout=300)

    def call(self, action, *flags, maid=False, ok=True):
        args = ([MAID, "runs", "--index", self.index, "--project", "p", "--run", "r", action]
                if maid else [ENTRY.with_name("awo_app.py"), action, "--thread", self.root])
        proc = self.run_process([*args, *flags])
        self.assertEqual(proc.returncode == 0, ok, (proc.stdout, proc.stderr))
        return json.loads(proc.stdout)

    def prepare(self, maid=False):
        self.call("prepare", *([] if maid else ["--spec", self.spec]), maid=maid)
        self.f.install_fixture_executables(self.root)

    def interrupt(self, root, point):
        # Stop the controller at real durable transitions, without launching live models.
        scripts = str(ENTRY.parent)
        boundary = {
            "before": "e.scheduler.dispatch_assignment = lambda *args, **kw: os._exit(63)",
            "after": ("def send(*args):\n"
                      "    Path(marker).write_bytes(b'sent')\n"
                      "    os._exit(63)\n"
                      "e.scheduler.monitor.Transport.send = send"),
            "written": ("real = e.collect_turn\n"
                        "def received(*args):\n"
                        "    real(*args)\n"
                        "    os._exit(63)\n"
                        "e.collect_turn = received"),
        }[point]
        wrapper = self.base / "stop.py"
        wrapper.write_text("import sys, os\nfrom pathlib import Path\nsys.path.insert(0, " +
                           repr(scripts) + ")\nimport project_entry as e\nmarker=" +
                           repr(str(self.base / "sent")) + "\n" + boundary +
                           "\ne.execute_plan(Path(" + repr(str(root / "plan.json")) + "))\n",
                           encoding="utf-8")
        proc = self.run_process([wrapper])
        self.assertEqual(proc.returncode, 63, (proc.stdout, proc.stderr))

    def test_no_receipt_denies_every_takeover_and_never_resends(self):
        self.prepare()
        self.interrupt(self.root, "after")
        self.assertEqual((self.base / "sent").read_bytes(), b"sent")
        old = (self.root / "run.json").read_bytes()
        data = self.call("recover")["result"]["recovery"]
        self.assertEqual(data["send_state"], "attempted")
        version = data["handoff"]["state_version"]
        for action in ("continue", "manual", "reassign"):
            self.call("recover", "--handoff", action, "--state-version", version,
                      "--recipient", "operator", "--confirm-execute", ok=False)
        self.assertEqual((self.root / "run.json").read_bytes(), old)
        self.assertFalse(any((self.root / "worker").rglob("job.json")))

    def test_written_before_delivery_reopens_dirty_file_without_accepting(self):
        self.prepare()
        self.interrupt(self.root, "written")
        report = self.call("recover")["result"]["recovery"]
        ws = Path(report["workspace_inspection"]["path"])
        self.assertEqual((ws / "app.py").read_text(encoding="utf-8"), APP_FIXED)
        self.assertTrue(any("app.py" in item for item in report["workspace_inspection"]["changes"]))
        self.assertEqual(report["sealed_reviews"], [])
        self.assertFalse((self.root / "recover.json").exists())
        self.assertFalse(self.call("status")["result"]["continuity"]["delivered"])

    def test_stale_selection_and_competing_manual_handoffs(self):
        from concurrent.futures import ThreadPoolExecutor
        self.prepare()
        self.interrupt(self.root, "before")
        report = self.call("recover")["result"]["recovery"]["handoff"]
        self.assertEqual(report["send_state"], "ready")
        workspace = Path(report["workspace"])
        text = (workspace / "app.py").read_bytes() + b"# retained draft\n"
        (workspace / "app.py").write_bytes(text)
        flags = ["--handoff", "manual", "--state-version", report["state_version"],
                 "--recipient", "human1", "--confirm-execute"]
        self.call("recover", *flags, ok=False)
        flags[3] = self.call("recover")["result"]["recovery"]["handoff"]["state_version"]
        def click(name):
            args = [*flags]
            args[5] = name
            p = self.run_process([ENTRY.with_name("awo_app.py"), "recover", "--thread",
                                  self.root, *args])
            return p.returncode, json.loads(p.stdout)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(click, ["human1", "human2"]))
        self.assertEqual(sorted(code for code, _ in responses), [0, 2], responses)
        accepted = next(r for code, r in responses if code == 0)["result"]["recovery"]
        self.assertFalse(accepted["accepted"])
        self.assertEqual((Path(accepted["workspace"]) / "app.py").read_bytes(), text)
        self.assertEqual((workspace / "app.py").read_bytes(), text)
        flags[5] = accepted["recipient"]
        replay = self.call("recover", *flags)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["result"]["recovery"], accepted)
        direct = self.f.cli("recover", "--plan", str(self.root / "plan.json"), "--confirm-execute",
                            env=self.env)
        self.assertNotEqual(direct[0], 0, direct)
        plan = entry.load_plan(self.root / "plan.json")
        with self.assertRaisesRegex(ValueError, "retired"):
            entry.native_worker.send(self.root / "worker", json.loads(plan["derived"]["message"]))
        self.assertFalse(any((self.root / "worker").rglob("job.json")))

    @unittest.skipUnless(MAID.is_file(), "optional paired MAID checkout not present")
    def test_maid_repair_interruption_replacement_and_reopened_evidence(self):
        self.prepare(maid=True)
        self.f.write_mode(code_verdict="repair")
        self.call("start", "--confirm-execute", maid=True, ok=False)
        previous = self.call("status", maid=True)["result"]["continuity"]
        self.assertEqual(previous["review"], "repair")
        self.assertTrue(previous["delivered"])
        link = self.call("rework", maid=True)["result"]
        child = Path(link["thread"])
        before = hashlib.sha256(Path(link["previous"]["review_path"]).read_bytes()).digest()
        self.f.install_fixture_executables(child)
        self.interrupt(child, "before")
        self.f.write_mode(worker_files={"app.py": APP_FIXED + "# addressed independent review\n"})
        report = self.call("recover", "--rework-attempt", maid=True)["result"]["recovery"]["handoff"]
        self.assertEqual(report["writer_isolation"], "not_dispatched")
        result = self.call("recover", "--rework-attempt", "--handoff", "reassign",
                           "--state-version", report["state_version"], "--recipient", "replacement",
                           "--confirm-execute", maid=True)["result"]["recovery"]
        self.assertFalse(result["dispatched"])
        self.assertFalse(result["accepted"])
        self.assertNotEqual(result["new_instance"], result["old_instance"])
        # Re-enter from the public client in a fresh process for each operation.
        replacement = Path(result["thread"])
        self.f.install_fixture_executables(replacement)
        select = ["--rework-attempt", "--handoff-attempt"]
        completed = self.call("start", *select, "--confirm-execute", maid=True)
        card = completed["result"]["continuity"]
        self.assertEqual(card["review"], "accept")
        self.assertTrue(card["accepted_evidence_id"])
        self.assertEqual(self.call("status", *select, maid=True)["result"]["continuity"]["accepted_evidence_id"],
                         card["accepted_evidence_id"])
        self.assertEqual(before, hashlib.sha256(Path(link["previous"]["review_path"]).read_bytes()).digest())

    def test_continue_requires_original_executor_and_dispatches_once(self):
        self.prepare()
        self.interrupt(self.root, "before")
        info = self.call("recover")["result"]["recovery"]["handoff"]
        flags = ["--handoff", "continue", "--state-version", info["state_version"],
                 "--confirm-execute", "--recipient"]
        self.call("recover", *flags, "not-the-bound-executor", ok=False)
        first = self.call("recover", *flags, info["instance"])
        self.assertEqual(first["result"]["recovery"]["result_status"], "LANDED")
        jobs = list((self.root / "worker").rglob("job.json"))
        self.assertEqual(len(jobs), 1)
        again = self.call("recover", *flags, info["instance"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["result"]["recovery"], again["result"]["recovery"])
        self.assertEqual(list((self.root / "worker").rglob("job.json")), jobs)
