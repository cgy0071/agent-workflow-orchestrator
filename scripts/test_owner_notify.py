"""Offline Owner notification tests; HTTP servers bind to loopback only."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import owner_notify as notify
import test_scheduler
import workflow_control as control
import workflow_scheduler as scheduler


class OwnerNotifyTests(unittest.TestCase):
    setUp = test_scheduler.SchedulerTests.setUp
    tearDown = test_scheduler.SchedulerTests.tearDown
    spec = test_scheduler.SchedulerTests.spec
    request = test_scheduler.SchedulerTests.request
    message = test_scheduler.SchedulerTests.message
    actor = test_scheduler.SchedulerTests.actor
    send = test_scheduler.SchedulerTests.send
    confirm = test_scheduler.SchedulerTests.confirm

    def config(self, kind="file", **more):
        channel = {"type": kind, "path": str(self.root / "inbox.log")} if kind == "file" else {"type": kind, **more}
        return {"lang": "en", "channels": {"owner": channel}}

    def event(self, status="blocked", *, cause=None, name="OWNER-A"):
        request = self.request(name)
        self.send(request)
        request = self.confirm(request)
        report = {"status": status, "stall_episode": 1}
        if cause:
            report.update(cause=cause, evidence="Question at C:/secret/work.txt nonce-" + name)
        event = control.publish_event(self.state, request, report)
        return request, event

    def test_discovery_filters_history_and_deduplicates_per_channel(self):
        request, event = self.event(cause="worker_question")
        control.publish_event(self.state, request, {"status": "identity_changed"})
        kinds = {item["kind"] for item in notify.discover(self.state)}
        self.assertEqual(kinds, {"permission_prompt", "worker_question", "worker_failed"})
        self.assertNotIn(request["nonce"], json.dumps(notify.discover(self.state)))
        self.assertNotIn("nonce", json.dumps(notify.discover(self.state)).lower())
        config = self.config()
        config["channels"]["owner"]["kinds"] = ["worker_question"]
        before = (self.state / "workflow.sqlite3").read_bytes()
        first = notify.run_once(self.state, config)
        second = notify.run_once(self.state, config)
        self.assertEqual((first["queued"], first["sent"], second["queued"], second["sent"]), (1, 1, 0, 0))
        self.assertEqual((self.state / "workflow.sqlite3").read_bytes(), before)
        sent = json.loads((self.root / "inbox.log").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(sent["kind"], "worker_question")
        self.assertNotIn(str(self.root), sent["text"])
        control.resolve_task(self.state, request["task_id"], *self.actor(), self._termination(request))
        self.assertEqual(notify.discover(self.state), [])

    def _termination(self, request):
        path = Path(request["cwd"]) / "TERMINATION.md"
        path.write_text(request["task_id"] + " " + request["nonce"] + " stopped", encoding="utf-8")
        return path

    def test_summary_warnings_and_review_event(self):
        request = self.request("WAIT-A")
        self.send(request)
        with control.database(self.state) as con:
            row = con.execute("SELECT data FROM dispatches WHERE task=?", (request["task_id"],)).fetchone()
            data = json.loads(row[0]); data["submitted_at"] = "2020-01-01T00:00:00+00:00"
            con.execute("UPDATE dispatches SET data=? WHERE task=?", (json.dumps(data), request["task_id"]))
        self.assertIn("start_unconfirmed", {x["kind"] for x in notify.discover(self.state)})
        request = self.confirm(request)
        control.publish_event(self.state, request, {"status": "review_ready"})
        with control.database(self.state) as con:
            row = con.execute("SELECT id,data FROM events WHERE task=?", (request["task_id"],)).fetchone()
            data = json.loads(row["data"]); data["created_at"] = "2020-01-01T00:00:00+00:00"
            con.execute("UPDATE events SET data=? WHERE id=?", (json.dumps(data), row["id"]))
        self.assertIn("review_waiting", {x["kind"] for x in notify.discover(self.state)})
        # This code is introduced by P0 in a separate branch; a projected warning is enough here.
        import workflow_status
        original = workflow_status.summarize
        def projected(*args, **kwargs):
            result = original(*args, **kwargs)
            result["tasks"][0]["warnings"].append("over_concurrency")
            return result
        with patch.object(workflow_status, "summarize", side_effect=projected):
            self.assertIn("over_concurrency", {x["kind"] for x in notify.discover(self.state)})

    def test_dry_run_and_missing_url_never_write(self):
        self.event()
        config = self.config()
        result = notify.run_once(self.state, config, dry_run=True)
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertFalse(notify.outbox_path(self.state).exists())
        self.assertFalse((self.root / "inbox.log").exists())
        missing = {"channels": {"phone": {"type": "webhook", "url_env": "MISSING_AWO_NOTIFY"}}}
        result = notify.run_once(self.state, missing, environ={})
        self.assertEqual(result["status"], "NO_CHANNELS")
        self.assertEqual(len(result["notices"]), 1)
        self.assertFalse(notify.outbox_path(self.state).exists())

    def test_retry_offsets_give_up_and_crash_recovery(self):
        self.event()
        config = self.config()
        start = datetime(2026, 11, 5, tzinfo=timezone.utc)
        failures = []
        def fail(*args):
            failures.append(1)
            raise RuntimeError("connection to https://secret.example/token failed")
        for minute in (0, *notify.RETRY_MINUTES):
            now = start + timedelta(minutes=minute)
            notify.run_once(self.state, config, now=now, sender=fail)
            with notify.connection(notify.outbox_path(self.state), readonly=True) as con:
                row = con.execute("SELECT * FROM messages").fetchone()
            self.assertNotIn("secret.example", row["last_error"])
            expected = notify.RETRY_MINUTES[len(failures) - 1] if len(failures) <= len(notify.RETRY_MINUTES) else 57
            self.assertEqual(notify.parsed(row["next_at"]), start + timedelta(minutes=expected))
        self.assertEqual(len(failures), 9)
        notify.run_once(self.state, config, now=start + timedelta(minutes=57), sender=fail)
        self.assertEqual(len(failures), 9)
        with notify.connection(notify.outbox_path(self.state), readonly=True) as con:
            self.assertEqual(con.execute("SELECT status FROM messages").fetchone()[0], "given_up")
        # A separate run crashes after claiming, so the same row is retried after five minutes.
        other = self.root / "crash-box.sqlite3"
        with notify.connection(other) as con:
            notify.create_outbox(con)
            con.execute("INSERT INTO messages (channel,dedupe_key,kind,task_id,body,status,next_at,created_at,sending_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", ("owner", "crash", "permission_prompt", "T", "body", "sending",
                                                  notify.stamp(start), notify.stamp(start), notify.stamp(start)))
            con.commit()
            self.assertIsNone(notify.claim(con, {"owner": {}}, start + timedelta(minutes=4)))
            self.assertIsNotNone(notify.claim(con, {"owner": {}}, start + timedelta(minutes=5)))

    def test_webhook_formats_status_and_url_redaction(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            code = 200
            response = b'{"errcode":0,"code":0}'
            def do_POST(inner):
                requests.append(json.loads(inner.rfile.read(int(inner.headers["Content-Length"]))))
                inner.send_response(Handler.code); inner.end_headers(); inner.wfile.write(Handler.response)
            def log_message(inner, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            url = "http://127.0.0.1:" + str(server.server_port) + "/secret"
            item = {"body": "hello", "kind": "worker_question", "task_id": "T"}
            for fmt, expected in (("generic", {"text": "hello"}),
                                  ("feishu", {"msg_type": "text", "content": {"text": "hello"}}),
                                  ("dingtalk", {"msgtype": "text", "text": {"content": "hello"}}),
                                  ("wecom", {"msgtype": "text", "text": {"content": "hello"}})):
                notify.send({"type": "webhook", "url_env": "URL", "format": fmt}, item,
                            datetime.now(timezone.utc), {"URL": url})
                self.assertEqual(requests[-1], expected)
            Handler.response = b'{"errcode":1}'
            with self.assertRaisesRegex(RuntimeError, "nonzero errcode"):
                notify.send({"type": "webhook", "url_env": "URL", "format": "dingtalk"}, item,
                            datetime.now(timezone.utc), {"URL": url})
            with self.assertRaisesRegex(ValueError, "loopback"):
                notify.webhook_url({"url_env": "URL"}, {"URL": "http://example.com/secret"})
            Handler.response = b'{"errcode":0,"code":0}'
            Handler.code = 500
            self.event()
            config = self.config("webhook", url_env="URL", format="generic")
            t = datetime.now(timezone.utc)
            first = notify.run_once(self.state, config, now=t, environ={"URL": url})
            self.assertEqual(first["results"][0]["status"], "pending")
            self.assertNotIn(url, json.dumps(first))
            Handler.code = 200
            second = notify.run_once(self.state, config, now=t + timedelta(minutes=1), environ={"URL": url})
            self.assertEqual(second["sent"], 1)
            self.assertNotIn(url, notify.outbox_path(self.state).read_bytes().decode("latin1"))
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_command_argv_exit_timeout_and_validation(self):
        target = self.root / "command.txt"
        argv = [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                str(target), "{text}"]
        config = self.config("command", argv=argv)
        self.assertEqual(notify.config_of(self._write_config(config))["channels"]["owner"]["argv"], argv)
        item = {"body": "argument with spaces", "kind": "worker_question", "task_id": "T"}
        notify.send(config["channels"]["owner"], item, datetime.now(timezone.utc), {})
        self.assertEqual(target.read_text(), item["body"])
        with self.assertRaisesRegex(RuntimeError, "code 7"):
            notify.send({"type": "command", "argv": [sys.executable, "-c", "import sys; sys.exit(7)", "{text}"]},
                        item, datetime.now(timezone.utc), {})
        with patch.object(notify.subprocess, "run", side_effect=subprocess.TimeoutExpired(argv, 15)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                notify.send(config["channels"]["owner"], item, datetime.now(timezone.utc), {})
        config["channels"]["owner"]["argv"].remove("{text}")
        with self.assertRaisesRegex(ValueError, "standalone"):
            notify.config_of(self._write_config(config))

    def _write_config(self, config):
        path = self.root / "notify.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_cli_ledger_bytes_interval_and_dashboard_summary(self):
        self.event()
        config = self.config()
        path = self._write_config(config)
        ledger = self.state / "workflow.sqlite3"
        before = ledger.read_bytes()
        done = subprocess.run([sys.executable, "-B", str(Path(notify.__file__)), "--state", str(self.state),
                               "--config", str(path), "--once"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(notify.outbox_summary(self.state)["pending"], 0)
        stop = self.state / "STOP_NOTIFY"
        calls = []
        def sleep(_seconds):
            calls.append(1); stop.write_text("stop", encoding="ascii")
        with redirect_stdout(io.StringIO()):
            notify.run_loop(self.state, config, 10, sleep=sleep)
        self.assertEqual(calls, [1])
        import test_dashboard
        fixture = test_dashboard.DashboardTests('test_status_never_writes_or_queries_adapters')
        fixture.setUp()
        try:
            self.assertIsNone(fixture.http("GET", "/api/status")[1]["owner_notify"])
            with notify.connection(notify.outbox_path(fixture.state)) as con:
                notify.create_outbox(con)
                con.execute("INSERT INTO messages (channel,dedupe_key,kind,task_id,body,status,next_at,created_at,sent_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?)", ("owner", "one", "permission_prompt", "T", "body", "sent",
                                                      notify.stamp(datetime.now(timezone.utc)),
                                                      notify.stamp(datetime.now(timezone.utc)),
                                                      notify.stamp(datetime.now(timezone.utc))))
                for key, status in (("two", "pending"), ("three", "given_up")):
                    con.execute("INSERT INTO messages (channel,dedupe_key,kind,task_id,body,status,next_at,created_at) "
                                "VALUES (?,?,?,?,?,?,?,?)", ("owner", key, "worker_question", "T", "body", status,
                                                          notify.stamp(datetime.now(timezone.utc)),
                                                          notify.stamp(datetime.now(timezone.utc))))
            data = fixture.http("GET", "/api/status")[1]
            self.assertEqual((data["owner_notify"]["pending"], data["owner_notify"]["given_up"]), (1, 1))
            self.assertIsNotNone(data["owner_notify"]["last_sent"])
            self.assertEqual(fixture.fake.status_calls, 0)
        finally:
            fixture.doCleanups(); fixture.tearDown()


class WindowsBatchTests(unittest.TestCase):
    def test_batch_file_command_is_refused_on_windows(self):
        config = {"channels": {"bat": {"type": "command", "argv": ["C:/bot/send.cmd", "{text}"]}}}
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "notify.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        # Pass the platform in: faking os.name makes pathlib build Windows paths on Linux.
        with self.assertRaisesRegex(ValueError, "bat or .cmd"):
            notify.config_of(path, windows=True)
        self.assertIn("bat", notify.config_of(path, windows=False)["channels"])  # POSIX has no cmd.exe re-parse.
        config["channels"]["bat"]["argv"] = ["node", "C:/bot/send.js", "{text}"]
        path.write_text(json.dumps(config), encoding="utf-8")
        self.assertIn("bat", notify.config_of(path, windows=True)["channels"])


if __name__ == "__main__":
    unittest.main()
