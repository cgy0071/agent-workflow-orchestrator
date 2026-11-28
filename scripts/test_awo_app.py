import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import awo_app
from test_project_entry import Fixture


class AppBoundaryTests(unittest.TestCase):
    def test_capabilities_readonly_subprocess(self):
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run([sys.executable, "-I", "-B",
                                   str(Path(awo_app.__file__).resolve()), "capabilities"],
                                  cwd=td, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(proc.stdout), {"protocol": 1, "capabilities": ["profile", "maid", "prepare", "start", "status", "recover", "schedule", "evidence-draft", "session", "rework"]})
            self.assertFalse(list(Path(td).iterdir()))

    def test_profile_exclusive_and_scoped(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = Fixture(td).repo
            dest = base / "profile.json"
            thread = base / "one"
            argv = ["profile", "--project", str(repo), "--thread", str(thread),
                    "--allow", "app.py", "--verify-script", "tests/smoke.py", "--profile", str(dest)]
            with patch("harness_probe.binary", return_value=sys.executable), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(awo_app.main(argv), 0)
                raw = dest.read_bytes()
                profile = json.loads(raw)
                self.assertEqual(profile["work_root"], str(thread.resolve()))
                self.assertEqual(profile["target_ref"], "refs/heads/maid-one")
                self.assertFalse(thread.exists())
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    awo_app.main(argv)
                self.assertEqual(dest.read_bytes(), raw)
                another = argv[:-1] + [str(base / "other.json")]
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    awo_app.main([s if s != "tests/smoke.py" else "../profile.json" for s in another])
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    awo_app.main([s if s != "app.py" else "../elsewhere" for s in another])
                self.assertFalse((base / "other.json").exists())

    def test_maid_routes_to_full_application(self):
        for argv, expected in [(["request", "--help"], "--profile"), (["direct", "--help"], "--thread")]:
            proc = subprocess.run([sys.executable, "-B", awo_app.__file__, "maid", *argv],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(expected, proc.stdout)

    def test_machine_replay_and_version_drift(self):
        import awo_app_operations as ops
        with tempfile.TemporaryDirectory() as td:
            f = Fixture(td)
            root = Path(td) / "run"
            spec = Path(td) / "input.json"
            spec.write_text(json.dumps(f.spec(root, "refs/heads/demo")), encoding="utf8")
            args = ["--thread", str(root)]
            prepare = [*args, "--spec", str(spec)]
            self.assertEqual(ops.handle("prepare", prepare)[::2], ("prepared", False))
            self.assertEqual(ops.handle("prepare", prepare)[::2], ("prepared", True))
            with self.assertRaisesRegex(Exception, "confirmation"):
                ops.handle("start", args)
            spec.write_text(spec.read_text() + "\n")
            with self.assertRaisesRegex(Exception, "Different spec"):
                ops.handle("prepare", prepare)
            with patch.object(ops, "backend_version", return_value="other"):
                with self.assertRaisesRegex(Exception, "changed"):
                    ops.handle("start", [*args, "--confirm-execute"])
            self.assertFalse((root / "run.json").exists())

    def test_concurrent_start_and_reopen_never_dispatch_twice(self):
        import os
        import awo_app_operations as ops
        with tempfile.TemporaryDirectory() as td:
            f = Fixture(td)
            root = Path(td) / "run"
            spec = Path(td) / "spec.json"
            spec.write_text(json.dumps(f.spec(root, "refs/heads/demo")), encoding="utf8")
            args = ["--thread", str(root)]
            ops.handle("prepare", [*args, "--spec", str(spec)])
            f.install_fixture_executables(root)
            command = [sys.executable, "-B", awo_app.__file__, "start", *args, "--confirm-execute"]
            env = dict(os.environ, **f.write_mode())
            processes = [subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True) for _ in range(2)]
            outputs = [json.loads(proc.communicate(timeout=240)[0]) for proc in processes]
            self.assertTrue(any(row["state"] == "complete" for row in outputs), outputs)
            jobs = lambda: tuple(sorted(p.name for p in (root / "worker" / "jobs").iterdir()))
            original = jobs()
            self.assertEqual(len(original), 1)
            for operation in ("start", "recover"):
                state, _, duplicate = ops.handle(operation, [*args, "--confirm-execute"])
                self.assertEqual((state, duplicate), ("complete", True))
                self.assertEqual(jobs(), original)

    def test_uncertain_start_fresh_recovery_does_not_send(self):
        import os
        import awo_app_operations as ops
        with tempfile.TemporaryDirectory() as td:
            f = Fixture(td)
            root = Path(td) / "run"
            spec = Path(td) / "spec.json"
            spec.write_text(json.dumps(f.spec(root, "refs/heads/demo")), encoding="utf8")
            args = ["--thread", str(root)]
            ops.handle("prepare", [*args, "--spec", str(spec)])
            f.install_fixture_executables(root)
            lock = root / "worker" / "send.lock"
            lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf8")
            env = dict(os.environ, **f.write_mode())

            def call(op, *flags):
                proc = subprocess.run([sys.executable, "-B", awo_app.__file__, op, *args, *flags],
                                      env=env, capture_output=True, timeout=120, text=True)
                return proc.returncode, json.loads(proc.stdout)

            code, response = call("start", "--confirm-execute")
            self.assertEqual((code, response["error"]["code"]), (2, "dispatch_uncertain"))
            lock.unlink()  # A resend would now succeed, making this a sensitive control.
            before = call("status")[1]
            self.assertEqual(before["state"], "uncertain")
            code, recovery = call("recover", "--confirm-execute")
            self.assertEqual((code, recovery["error"]["code"]), (2, "recovery_blocked"))
            code, restarted = call("start", "--confirm-execute")
            self.assertEqual((code, restarted["state"], restarted["duplicate"]), (2, "uncertain", True))
            after = call("status")[1]
            self.assertEqual(before["result"]["assignment"], after["result"]["assignment"])
            self.assertEqual(after["result"]["journal"]["phase"], "dispatch_uncertain")
            self.assertFalse((root / "worker" / "jobs").exists())

    def test_ack_lost_after_actual_worker_job_no_duplicate_on_recovery(self):
        import os
        import awo_app_operations as ops
        with tempfile.TemporaryDirectory() as td:
            f = Fixture(td)
            root = Path(td) / "run"
            spec = Path(td) / "spec.json"
            spec.write_text(json.dumps(f.spec(root, "refs/heads/demo")), encoding="utf8")
            args = ["--thread", str(root)]
            ops.handle("prepare", [*args, "--spec", str(spec)])
            f.install_fixture_executables(root)
            env = dict(os.environ, **f.write_mode())
            fault = (
                "import sys; sys.path.insert(0, sys.argv.pop(1))\n"
                "import awo_app, project_entry as entry\n"
                "send = entry.scheduler.dispatch_assignment\n"
                "def lose_ack(*args, **kwargs):\n"
                "    reply = send(*args, **kwargs)\n"
                "    assert reply['status'] == 'SUBMITTED', reply\n"
                "    return {'status':'TRANSPORT_LOST'}\n"
                "entry.scheduler.dispatch_assignment = lose_ack\n"
                "raise SystemExit(awo_app.main(sys.argv[1:]))\n"
            )
            failed = subprocess.run([sys.executable, "-B", "-c", fault,
                                     str(Path(awo_app.__file__).parent), "start", *args, "--confirm-execute"],
                                    env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(json.loads(failed.stdout)["error"]["code"], "dispatch_uncertain")
            jobs = lambda: tuple(sorted(p.name for p in (root / "worker" / "jobs").iterdir()))
            original = jobs()
            self.assertEqual(len(original), 1)
            for op in ("start", "recover", "start"):
                proc = subprocess.run([sys.executable, "-B", awo_app.__file__, op, *args, "--confirm-execute"],
                                      env=env, capture_output=True, text=True, timeout=180)
                response = json.loads(proc.stdout)
                self.assertIn(response["state"], ("uncertain", "complete", "refused"))
                self.assertEqual(jobs(), original)

    def test_public_shared_schedule_reads_existing_task_and_checks_identity(self):
        from test_project_schedule import SharedSchedulingTests
        shared = SharedSchedulingTests()
        shared.setUp()
        self.addCleanup(shared.doCleanups)
        shared.enqueue("T-PUBLIC", "executor")
        base = [sys.executable, "-B", awo_app.__file__, "schedule", "--state", str(shared.f.state),
                "--project-root", str(shared.f.root), "--token-file", str(shared.token_path),
                "--expected-project-id"]
        def run(project):
            return subprocess.run([*base, project, "status", "--task", "T-PUBLIC"],
                                  capture_output=True, text=True, timeout=60)
        good = run(shared.f.project)
        self.assertEqual(good.returncode, 0, good.stderr)
        result = json.loads(good.stdout)
        self.assertEqual((result["project_id"], result["task_id"]), (shared.f.project, "T-PUBLIC"))
        denied = run("other-project")
        self.assertNotEqual(denied.returncode, 0)
        self.assertFalse(denied.stdout.strip())
