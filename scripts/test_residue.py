"""Offline crash residue and new-registration field regressions."""
from ctypes import wintypes
import ctypes
import json
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import native_worker
import process_util
import test_scheduler
import workflow_control as control


class ProcessUtilTests(unittest.TestCase):
    def test_current_exited_and_invalid(self):
        self.assertTrue(process_util.pid_alive(os.getpid()))
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        self.assertFalse(process_util.pid_alive(child.pid))
        for value in (0, -1, "42", True):
            with self.assertRaises(ValueError):
                process_util.pid_alive(value)

    def test_windows_queries_never_signal(self):
        for handle, error, exit_code, expected in ((0, 87, None, False), (0, 5, None, True),
                                                    (23, 0, 259, True), (23, 0, 0, False)):
            with self.subTest(error=error, exit_code=exit_code):
                def get_exit(_handle, pointer):
                    ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = exit_code
                    return True
                kernel = SimpleNamespace(OpenProcess=Mock(return_value=handle),
                                         GetExitCodeProcess=Mock(side_effect=get_exit),
                                         CloseHandle=Mock())
                with patch.object(process_util.ctypes, "WinDLL", return_value=kernel, create=True), \
                     patch.object(process_util.ctypes, "get_last_error", return_value=error, create=True), \
                     patch.object(process_util.os, "kill", side_effect=AssertionError("os.kill called")):
                    self.assertIs(process_util._windows_pid_alive(42), expected)
                    kernel.OpenProcess.assert_called_once_with(0x1000, False, 42)
                    self.assertEqual(kernel.CloseHandle.call_count, int(bool(handle)))


class ResidueTests(unittest.TestCase):
    setUp = test_scheduler.SchedulerTests.setUp
    tearDown = test_scheduler.SchedulerTests.tearDown
    spec = test_scheduler.SchedulerTests.spec
    request = test_scheduler.SchedulerTests.request
    message = test_scheduler.SchedulerTests.message
    actor = test_scheduler.SchedulerTests.actor
    send = test_scheduler.SchedulerTests.send

    def test_stale_lock_and_explicit_release(self):
        worker = self.root / "bridge"
        native_worker.initialize(worker, "codex", sys.executable)
        lock = worker / "send.lock"
        lock.write_text(json.dumps({"pid": 12345, "created_at": "2026-11-05T00:00:00Z"}), encoding="ascii")
        payload = {"task_id": "T", "nonce": "N", "cwd": str(self.root), "prompt": "x",
                   "authorization": {"source": "test"}}
        with patch.object(native_worker, "pid_alive", return_value=False), \
             patch.object(native_worker.subprocess, "Popen", side_effect=AssertionError("spawned")):
            with self.assertRaisesRegex(ValueError, "release-lock"):
                native_worker.send(worker, payload)
            self.assertTrue(lock.exists())
            with self.assertRaises(ValueError):
                native_worker.release_lock(worker, "")
            with self.assertRaises(ValueError):
                native_worker.release_lock(worker, "x" * 501)
            self.assertEqual(native_worker.release_lock(worker, "checked dead pid")["status"], "lock_released")
            self.assertFalse(lock.exists())
        lock.write_text(json.dumps({"pid": os.getpid()}), encoding="ascii")
        with patch.object(native_worker, "pid_alive", return_value=True):
            with self.assertRaises(ValueError):
                native_worker.release_lock(worker, "still running")
        self.assertTrue(lock.exists())

    def test_cli_requires_reason_and_releases_confirmed_dead_lock(self):
        worker = self.root / "bridge"
        native_worker.initialize(worker, "codex", sys.executable)
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        lock = worker / "send.lock"
        lock.write_text(json.dumps({"pid": child.pid, "created_at": "2026-11-05T00:00:00Z"}), encoding="ascii")
        base = [sys.executable, "-B", str(native_worker.__file__), "--worker", str(worker), "release-lock"]
        missing = subprocess.run(base, capture_output=True, text=True)
        self.assertEqual(missing.returncode, 2)
        self.assertTrue(lock.exists())
        done = subprocess.run(base + ["--reason", "confirmed dead child"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["status"], "lock_released")
        self.assertFalse(lock.exists())

    def test_runner_gone_old_active_and_release(self):
        worker = self.root / "bridge"
        native_worker.initialize(worker, "codex", sys.executable)
        job = worker / "jobs" / "dead"
        job.mkdir(parents=True)
        active = {"task_id": "T", "nonce": "N", "directory": str(job), "baseline": 0}
        from monitor_task import save_json, read_json
        save_json(worker / "active.json", active)
        self.assertEqual(native_worker.status(worker)["status"], "working")
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            native_worker.release(worker, "old record")
        active["runner_pid"] = 12345
        save_json(worker / "active.json", active)
        with patch.object(native_worker, "pid_alive", return_value=None):
            self.assertEqual(native_worker.status(worker)["status"], "working")
            with self.assertRaisesRegex(ValueError, "not confirmed"):
                native_worker.release(worker, "unknown")
        with patch.object(native_worker, "pid_alive", return_value=False), \
             patch.object(native_worker.subprocess, "Popen", side_effect=AssertionError("spawned")):
            status = native_worker.status(worker)
            self.assertEqual((status["status"], status["exit_reason"], status["job_directory"]),
                             ("exited", "runner_gone", str(job)))
            self.assertEqual(native_worker.release(worker, "inspected")["status"], "released")
        self.assertEqual(native_worker.status(worker)["status"], "idle")
        self.assertFalse((worker / "active.json").exists())
        self.assertEqual(read_json(job / "released.json")["release_reason"], "inspected")
        # A runner that writes its outcome and exits between the two checks is a finished job, not a lost one.
        racing = worker / "jobs" / "racing"
        racing.mkdir()
        save_json(worker / "active.json", dict(active, directory=str(racing), runner_pid=12345))
        def finishes(pid):
            save_json(racing / "outcome.json", {"status": "done", "native_session_id": None})
            return False
        with patch.object(native_worker, "pid_alive", side_effect=finishes):
            self.assertEqual(native_worker.status(worker)["status"], "idle")
        (worker / "active.json").unlink()
        job2 = worker / "jobs" / "finished"
        job2.mkdir()
        active["directory"] = str(job2)
        active.pop("runner_pid")
        save_json(worker / "active.json", active)
        save_json(job2 / "outcome.json", {"status": "failed"})
        self.assertEqual(native_worker.release(worker, "outcome inspected")["status"], "released")
        self.assertEqual(read_json(job2 / "released.json")["released_status"], "failed")

    def test_new_fields_block_both_entries_without_send(self):
        for key, values in (("task_id", ["bad\nid", "bad" + chr(96) + "id", "bad/id", "x" * 65, ".", ".."]),
                            ("nonce", ["bad\nnonce", "bad" + chr(96) + "nonce", "bad/nonce", "x" * 129])):
            for value in values:
                with self.subTest(key=key, value=value):
                    req = self.request("FIELD-" + str(len(self.fake.messages)))
                    req[key] = value
                    before = len(self.fake.messages)
                    with self.assertRaises(ValueError):
                        self.send(req)
                    self.assertEqual(len(self.fake.messages), before)
                    req["dispatch_confirmed"] = True
                    with self.assertRaisesRegex(ValueError, "invalid task ID|invalid nonce"):
                        control.register_task(self.state, req, *self.actor())

    def test_already_registered_legacy_fields_remain_valid(self):
        req = self.request("LEGACY")
        req["task_id"] = "old\nid"
        req["nonce"] = "old" + chr(96) + "nonce"
        req["dispatch_confirmed"] = True
        with control.database(self.state) as con:
            con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?)",
                        (req["task_id"], req["nonce"], control.digest(req), "executor", "running", "{}"))
        self.assertEqual(control.register_task(self.state, req, *self.actor())["status"], "ALREADY_REGISTERED")


if __name__ == "__main__":
    unittest.main()
