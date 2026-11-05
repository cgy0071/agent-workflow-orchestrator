"""Offline Owner intake regressions; no real Herdr, bot, or network calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import hashlib
import hmac
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

import owner_intake as intake
import owner_notify as notify
import test_scheduler
import workflow_control as control


class FakeHerdr:
    def __init__(self, identity):
        self.identity = identity
        self.state = "idle"
        self.available = True
        self.messages = []
        self.fail = False

    def status(self, _spec):
        return {"identity": self.identity, "status": self.state, "prompt_available": self.available}

    def send(self, _spec, text):
        self.messages.append(text)
        if self.fail:
            raise TimeoutError("uncertain fake send")
        return "sent"


def process_forward(state, identity, log, key):
    class FileTransport:
        def status(self, _spec):
            return {"identity": identity, "status": "idle", "prompt_available": True}

        def send(self, _spec, text):
            with Path(log).open("a", encoding="utf-8") as stream:
                stream.write(text + "\n")
    intake.forward(state, environ={"AWO_INTAKE_HMAC_KEY": key}, transport=FileTransport())


class IntakeTests(unittest.TestCase):
    setUp = test_scheduler.SchedulerTests.setUp
    tearDown = test_scheduler.SchedulerTests.tearDown
    spec = test_scheduler.SchedulerTests.spec
    actor = test_scheduler.SchedulerTests.actor
    KEY = "offline-intake-secret"

    def intake_config(self, inbox=None, **more):
        return {"inbox": str(inbox or self.root / "inbox"),
                "notify_config": str(self.root / "notify.json"), "notify_channel": "owner",
                **more}

    def enable(self, *, name="alpha", inbox=None, prompt=True):
        with control.database(self.state) as con:
            config = control.config_of(con)
            config["name"] = name
            con.execute("UPDATE meta SET value=? WHERE key='config'", (json.dumps(config),))
        control.update_config(self.state, {"owner_intake": self.intake_config(inbox, prompt_controller=prompt)},
                              *self.actor())
        notify_config = {"lang": "en", "channels": {"owner": {
            "type": "file", "path": str(self.root / "owner.log"), "kinds": ["intake_update"]}}}
        (self.root / "notify.json").write_text(json.dumps(notify_config), encoding="utf-8")
        path = Path(inbox or self.root / "inbox")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def environment(self):
        return {"AWO_INTAKE_HMAC_KEY": self.KEY}

    def herdr(self):
        identity = {"kind": "codex", "terminal": "planner-terminal",
                    "launch_cwd": str(self.root), "pane": "planner-pane"}
        spec = {"role": "planner", "adapter": "herdr", "harness": "codex", "executable": "fake-herdr",
                "session": "fake", "pane": "planner-pane", "expected_identity": identity}
        self.controller = control.bind(self.state, "planner", spec, identity, "controller",
                                       reason="switch to offline fake Planner")
        return FakeHerdr(identity)

    def signed(self, request_id, *, text="Plan pagination", project=None, **changes):
        data = {"schema": 1, "request_id": request_id, "channel": "qq", "text": text,
                "requested_at": "2026-11-05T10:00:00+08:00",
                "confirmed_at": "2026-11-05T10:00:30+08:00",
                "source": {"kind": "qq_c2c", "owner": True, "message_ref": "a" * 16}}
        if project is not None:
            data["project"] = project
        data.update(changes)
        message = "\n".join(str(data.get(k, "")) for k in
                            ("request_id", "text", "requested_at", "confirmed_at", "project"))
        data["hmac"] = hmac.new(self.KEY.encode(), message.encode(), hashlib.sha256).hexdigest()
        return data

    def put(self, folder, request_id, **changes):
        path = folder / (request_id + ".json")
        path.write_text(json.dumps(self.signed(request_id, **changes)), encoding="utf-8")
        return path

    def notices(self):
        target = self.root / "owner.log"
        return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()] if target.exists() else []

    def test_disabled_missing_key_and_config_validation(self):
        before = set(self.state.iterdir())
        for method in (intake.list_requests, intake.forward):
            self.assertEqual(method(self.state)["status"], "INTAKE_DISABLED")
        self.assertEqual(intake.claim_request(self.state, "X", *self.actor())["status"], "INTAKE_DISABLED")
        self.assertEqual(intake.close_request(self.state, "X", "done", "ok", *self.actor())["status"], "INTAKE_DISABLED")
        self.assertEqual(set(self.state.iterdir()), before)
        from workflow_scheduler import _read_database
        with _read_database(self.state) as con:
            base = control.config_of(con)
        for bad in ({"inbox": "relative"}, {"inbox": str(self.root), "unknown": 1},
                    {"inbox": str(self.root), "prompt_controller": "yes"},
                    {"inbox": str(self.root), "notify_config": "relative"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                control.validate_workflow(dict(base, owner_intake=bad))
        self.enable()
        self.assertEqual(intake.list_requests(self.state, environ={})["status"], "INTAKE_KEY_MISSING")

    def test_invalid_files_list_readonly_then_forward_notifies_once(self):
        folder = self.enable()
        bad = {
            "owner": self.signed("bad-owner", source={"kind": "qq_c2c", "message_ref": "a" * 16}),
            "confirmed": self.signed("bad-confirmed", confirmed_at=None),
            "id": self.signed("different-id"),
            "length": self.signed("bad-length", text="x" * 4001),
            "control": self.signed("bad-control", text="bad\x00text"),
            "hmac": dict(self.signed("bad-hmac"), hmac="0" * 64),
            "missing-hmac": dict(self.signed("bad-missing-hmac")),
        }
        del bad["missing-hmac"]["hmac"]
        for label, data in bad.items():
            (folder / ("bad-" + label + ".json")).write_text(json.dumps(data), encoding="utf-8")
        (folder / "bad name.json").write_text(json.dumps(self.signed("bad name")), encoding="utf-8")
        (folder / ".draft.json").write_text("{}", encoding="utf-8")
        (folder / "unused.tmp").write_text("{}", encoding="utf-8")
        (folder / "child").mkdir()
        (folder / "child" / "sub.json").write_text("{}", encoding="utf-8")
        ledger = self.state / "workflow.sqlite3"
        before = ledger.read_bytes()
        listing = intake.list_requests(self.state, environ=self.environment())
        self.assertEqual(len(listing["requests"]), len(bad) + 1)
        self.assertTrue(all(x["status"] == "invalid" for x in listing["requests"]))
        self.assertFalse((self.state / "owner_intake.sqlite3").exists())
        fake = FakeHerdr({})
        intake.forward(self.state, environ=self.environment(), transport=fake)
        intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertEqual(len(self.notices()), len(bad) + 1)
        self.assertTrue(all("bad name" not in item["text"] for item in self.notices()))
        self.assertEqual(len(fake.messages), 0)
        self.assertEqual(ledger.read_bytes(), before)
        self.assertTrue(all((folder / ("bad-" + x + ".json")).exists() for x in bad))

    def test_time_project_hmac_and_changed_bytes(self):
        folder = self.enable()
        self.put(folder, "z-time", requested_at="2026-11-05T02:00:00Z")
        self.put(folder, "other-project", project="beta")
        self.assertEqual([x["request_id"] for x in intake.list_requests(self.state, environ=self.environment())["requests"]],
                         ["z-time"])
        intake.forward(self.state, environ=self.environment())
        path = folder / "z-time.json"
        path.write_text(json.dumps(self.signed("z-time", text="changed")), encoding="utf-8")
        result = intake.forward(self.state, environ=self.environment())
        self.assertEqual(result["requests"][0]["status"], "invalid")
        self.assertEqual(len(self.notices()), 1)

    def test_idle_busy_blocked_identity_uncertain_and_crash_recovery(self):
        folder = self.enable()
        fake = self.herdr()
        self.put(folder, "qq-20261105-0001", text="SECRET ORIGINAL")
        ledger = self.state / "workflow.sqlite3"
        before = ledger.read_bytes()
        fake.state = "working"
        self.assertEqual(intake.forward(self.state, environ=self.environment(), transport=fake)["sent"], 0)
        self.assertEqual(intake.list_requests(self.state, environ=self.environment())["requests"][0]["status"], "new")
        fake.state = "blocked"; fake.available = False
        intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertEqual(len(self.notices()), 1)
        fake.state = "idle"; fake.available = True; fake.identity = dict(fake.identity, terminal="other")
        intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertEqual(len(self.notices()), 2)
        fake.identity = self.controller["spec"]["expected_identity"]
        result = intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(fake.messages), 1)
        self.assertIn("qq-20261105-0001", fake.messages[0])
        self.assertNotIn("SECRET ORIGINAL", fake.messages[0])
        self.assertNotIn(str(folder), fake.messages[0])
        intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertEqual(len(fake.messages), 1)
        self.assertEqual(ledger.read_bytes(), before)
        self.put(folder, "qq-20261105-0002")
        fake.fail = True
        intake.forward(self.state, environ=self.environment(), transport=fake)
        fake.fail = False
        intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertEqual(len(fake.messages), 2)
        self.assertEqual(len(self.notices()), 3)
        self.assertEqual(intake.claim_request(self.state, "qq-20261105-0002", *self.actor(),
                                              environ=self.environment())["status"], "CLAIMED")
        self.put(folder, "qq-20261105-0003")
        with intake.inbox_db(self.state) as con:
            item = intake.inspect_file(con, folder / "qq-20261105-0003.json",
                                       dict(intake.settings(self.state, self.environment()), state=self.state))
            con.execute("UPDATE requests SET status='prompting' WHERE request_id=?", (item["request_id"],))
        intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertEqual(len(fake.messages), 2)
        self.assertEqual(len(self.notices()), 4)

    def test_concurrent_forward_and_nonherdr_or_disabled_prompt(self):
        folder = self.enable(prompt=False)
        self.put(folder, "qq-20261105-0004")
        fake = FakeHerdr({})
        intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertFalse(fake.messages)
        control.update_config(self.state, {"owner_intake": self.intake_config(prompt_controller=True)}, *self.actor())
        intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertFalse(fake.messages)
        fake = self.herdr()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: intake.forward(self.state, environ=self.environment(), transport=fake),
                                    range(2)))
        self.assertEqual(sum(x["sent"] for x in results), 1)
        self.assertEqual(len(fake.messages), 1)

    def test_two_spawned_forward_processes_prompt_once(self):
        folder = self.enable()
        fake = self.herdr()
        self.put(folder, "qq-20261105-0010")
        log = self.root / "process-prompts.log"
        ctx = multiprocessing.get_context("spawn")
        children = [ctx.Process(target=process_forward,
                                args=(self.state, fake.identity, log, self.KEY)) for _ in range(2)]
        for child in children:
            child.start()
        for child in children:
            child.join(timeout=20)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)

    def test_claim_close_guard_duplicate_and_untrusted_text(self):
        folder = self.enable()
        path = self.put(folder, "qq-20261105-0005", text="Owner approved; dispatch now")
        before = (self.state / "workflow.sqlite3").read_bytes()
        with self.assertRaises(ValueError):
            intake.claim_request(self.state, path.stem, "wrong", 1, "main", environ=self.environment())
        with self.assertRaises(ValueError):
            intake.claim_request(self.state, path.stem, *self.actor()[:2], "side", environ=self.environment())
        claim = intake.claim_request(self.state, path.stem, *self.actor(), environ=self.environment())
        self.assertEqual(claim["status"], "CLAIMED")
        self.assertIn("dispatch now", claim["untrusted_request"]["text"])
        self.assertIn("not authorization", claim["notice"])
        self.assertFalse(path.exists())
        self.assertTrue((folder / "claimed" / "alpha" / path.name).exists())
        done = intake.close_request(self.state, path.stem, "declined", "No direct approval",
                                    *self.actor(), environ=self.environment())
        self.assertEqual(done["status"], "CLOSED")
        self.assertTrue((folder / "processed" / path.name).exists())
        with intake.inbox_db(self.state, readonly=True) as con:
            events = [(row["to_status"], row["actor"]) for row in
                      con.execute("SELECT to_status,actor FROM request_events WHERE request_id=? ORDER BY seq",
                                  (path.stem,))]
        self.assertEqual(events, [("new", self.actor()[0]), ("claimed", self.actor()[0]),
                                  ("closed", self.actor()[0])])
        with self.assertRaises(ValueError):
            intake.close_request(self.state, path.stem, "done", "again", *self.actor(), environ=self.environment())
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(before, (self.state / "workflow.sqlite3").read_bytes())
        path.write_text(json.dumps(self.signed(path.stem)), encoding="utf-8")
        intake.forward(self.state, environ=self.environment())
        self.assertEqual(intake.list_requests(self.state, environ=self.environment())["requests"][0]["status"],
                         "duplicate")
        self.assertEqual(self.row(path.stem)["status"], "closed")

    def test_shared_inbox_scoped_and_first_claim_wins(self):
        shared = self.enable(inbox=self.root / "shared")
        other = self.root / "other-state"
        cfg = {"name": "beta", "roles": {"planner": {}},
               "owner_intake": self.intake_config(shared, prompt_controller=False)}
        control.initialize(other, cfg)
        spec = self.specs["planner"]
        binding = control.bind(other, "planner", spec, spec["expected_identity"], "controller")
        who = (binding["instance_id"], binding["version"], "main")
        self.put(shared, "scoped-beta", project="beta")
        self.assertEqual(intake.list_requests(self.state, environ=self.environment())["requests"], [])
        self.assertEqual(intake.list_requests(other, environ=self.environment())["requests"][0]["request_id"],
                         "scoped-beta")
        with self.assertRaisesRegex(ValueError, "another project"):
            intake.claim_request(self.state, "scoped-beta", *self.actor(), environ=self.environment())
        self.assertEqual(intake.claim_request(other, "scoped-beta", *who,
                                              environ=self.environment())["status"], "CLAIMED")
        self.put(shared, "shared-one")
        self.assertEqual(intake.claim_request(self.state, "shared-one", *self.actor(),
                                              environ=self.environment())["status"], "CLAIMED")
        self.assertFalse((shared / "shared-one.json").exists())
        self.assertTrue((shared / "claimed" / "alpha" / "shared-one.json").exists())
        self.assertFalse(any(x["request_id"] == "shared-one" for x in
                             intake.list_requests(other, environ=self.environment())["requests"]))
        with self.assertRaisesRegex(ValueError, "not in the inbox"):
            intake.claim_request(other, "shared-one", *who, environ=self.environment())

    def test_duplicate_same_id_is_reported_once(self):
        folder = self.enable()
        path = self.put(folder, "same-id")
        intake.claim_request(self.state, path.stem, *self.actor(), environ=self.environment())
        intake.close_request(self.state, path.stem, "done", "finished", *self.actor(),
                             environ=self.environment())
        original = (folder / "processed" / path.name).read_bytes()
        path.write_bytes(original)
        first = intake.forward(self.state, environ=self.environment())
        second = intake.forward(self.state, environ=self.environment())
        self.assertEqual(first["requests"][0]["status"], "duplicate")
        self.assertEqual(second["requests"][0]["status"], "duplicate")
        self.assertEqual(len(self.notices()), 2)  # closed and duplicate

    def test_enqueue_legacy_and_explicit_channel(self):
        example = Path(__file__).resolve().parents[1] / "examples" / "notify-qq.json"
        self.assertIn("intake_update", notify.config_of(example)["channels"]["qq"]["kinds"])
        legacy = {"lang": "en", "channels": {"owner": {"type": "file", "path": str(self.root / "legacy.log")}}}
        self.assertEqual(notify.enqueue_message(self.state, legacy, kind="intake_update",
                                                key="intake:X:closed", text="done")["queued"], 0)
        explicit = {"lang": "en", "channels": {"owner": {
            "type": "file", "path": str(self.root / "explicit.log"), "kinds": ["intake_update"]}}}
        path = self.root / "notify-qq.json"
        path.write_text(json.dumps(explicit), encoding="utf-8")
        loaded = notify.config_of(path)
        self.assertEqual(notify.enqueue_message(self.state, loaded, kind="intake_update",
                                                key="intake:X:closed", text="done")["queued"], 1)
        self.assertEqual(notify.enqueue_message(self.state, loaded, kind="intake_update",
                                                key="intake:X:closed", text="done")["queued"], 0)
        notify.enqueue_message(self.state, loaded, kind="intake_update", key="intake:X:long",
                               text="https://secret.example/token " + "x" * 500)
        with notify.connection(notify.outbox_path(self.state), readonly=True) as con:
            body = con.execute("SELECT body FROM messages WHERE dedupe_key='intake:X:long'").fetchone()[0]
        self.assertLessEqual(len(body), 300)
        self.assertNotIn("secret.example", body)
        notify.run_once(self.state, loaded)
        self.assertEqual(len((self.root / "explicit.log").read_text().splitlines()), 2)

    def test_interval_lock_and_stop(self):
        self.enable()
        lock = self.state / "owner_intake.lock"
        lock.write_text("other", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "already exists"):
            intake.run_loop(self.state, 10, environ=self.environment(), sleep=lambda _: None)
        lock.unlink()
        (self.state / "STOP_INTAKE").write_text("stop", encoding="ascii")
        self.assertEqual(intake.run_loop(self.state, 10, environ=self.environment())["status"], "INTAKE_STOPPED")
        self.assertFalse(lock.exists())


    def row(self, request_id):
        with intake.inbox_db(self.state, readonly=True) as con:
            return dict(con.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone())

    def test_reappearing_id_keeps_claimed_and_closed_records(self):
        folder = self.enable(prompt=False)
        path = self.put(folder, "re-id")
        original = path.read_bytes()
        intake.claim_request(self.state, "re-id", *self.actor(), environ=self.environment())
        for data in (original, original + b" "):  # same bytes, then changed bytes
            path.write_bytes(data)
            seen = intake.forward(self.state, environ=self.environment())
            self.assertEqual(seen["requests"], [{"request_id": "re-id", "status": "duplicate"}])
            self.assertEqual(self.row("re-id")["status"], "claimed")
            path.unlink()
        self.assertEqual(intake.close_request(self.state, "re-id", "done", "handled", *self.actor(),
                                              environ=self.environment())["status"], "CLOSED")
        path.write_bytes(original + b"\n")
        listed = intake.list_requests(self.state, environ=self.environment())["requests"]
        self.assertEqual([(x["request_id"], x["status"]) for x in listed], [("re-id", "duplicate")])
        intake.forward(self.state, environ=self.environment())
        row = self.row("re-id")
        self.assertEqual((row["status"], row["outcome"]), ("closed", "done"))

    def test_claim_resumes_after_interrupted_move(self):
        folder = self.enable(prompt=False)
        self.put(folder, "move-cut")
        real, calls = intake.now, []

        def flaky():
            calls.append(1)
            if len(calls) == 2:  # the claim UPDATE, after the file was moved
                raise sqlite3.OperationalError("database is locked")
            return real()
        with patch.object(intake, "now", flaky):
            with self.assertRaises(sqlite3.OperationalError):
                intake.claim_request(self.state, "move-cut", *self.actor(), environ=self.environment())
        self.assertTrue((folder / "claimed" / "alpha" / "move-cut.json").exists())
        self.assertNotEqual(self.row("move-cut")["status"], "claimed")
        self.assertEqual(intake.claim_request(self.state, "move-cut", *self.actor(),
                                              environ=self.environment())["status"], "CLAIMED")
        self.assertEqual(intake.close_request(self.state, "move-cut", "done", "handled", *self.actor(),
                                              environ=self.environment())["status"], "CLOSED")

    def test_forward_once_waits_for_running_forwarder(self):
        folder = self.enable()
        fake = self.herdr()
        self.put(folder, "lock-one")
        inner, original_send = [], fake.send

        def send(spec, text):
            inner.append(intake.forward(self.state, environ=self.environment(), transport=fake))
            return original_send(spec, text)
        fake.send = send
        self.assertEqual(intake.forward(self.state, environ=self.environment(), transport=fake)["sent"], 1)
        self.assertEqual(inner[0]["status"], "FORWARD_BUSY")
        self.assertEqual(self.row("lock-one")["status"], "prompted")
        self.assertEqual(self.notices(), [])
        lock = self.state / "owner_intake.lock"
        self.assertFalse(lock.exists())
        lock.write_text("999999999", encoding="ascii")  # a process that is not running
        busy = intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertEqual((busy["status"], busy["sent"]), ("FORWARD_BUSY", 0))
        self.assertIn("has exited", busy["reason"])
        self.assertTrue(lock.exists())

    def test_notification_failure_does_not_stop_forward(self):
        folder = self.enable()
        fake = self.herdr()
        (self.root / "notify.json").write_text(json.dumps({"lang": "en", "channels": {"other": {
            "type": "file", "path": str(self.root / "other.log"), "kinds": ["intake_update"]}}}), encoding="utf-8")
        (folder / "aaa-bad.json").write_text("{", encoding="utf-8")
        self.put(folder, "zzz-good")
        with patch("sys.stderr", new_callable=io.StringIO) as err:
            result = intake.forward(self.state, environ=self.environment(), transport=fake)
        self.assertEqual((result["status"], result["sent"]), ("FORWARD_DONE", 1))
        self.assertIn("notification skipped", err.getvalue())
        with self.assertRaisesRegex(ValueError, "set together"):
            control.update_config(self.state, {"owner_intake": {"inbox": str(folder), "notify_channel": "owner"}},
                                  *self.actor())

    def test_bad_unicode_and_vanished_file_do_not_stop_listing(self):
        folder = self.enable(prompt=False)
        data = self.signed("bad-text")
        data["text"] = "\ud800"
        (folder / "bad-text.json").write_text(json.dumps(data), encoding="utf-8")
        self.put(folder, "good-one")
        self.put(folder, "gone-one")
        listed = {x["request_id"]: x["status"] for x in
                  intake.list_requests(self.state, environ=self.environment())["requests"]}
        self.assertEqual(listed, {"bad-text": "invalid", "gone-one": "new", "good-one": "new"})
        real = Path.read_bytes

        def vanish(path):
            if path.name == "gone-one.json":
                raise FileNotFoundError(str(path))
            return real(path)
        with patch.object(Path, "read_bytes", vanish):
            listed = intake.list_requests(self.state, environ=self.environment())["requests"]
            seen = intake.forward(self.state, environ=self.environment())
        self.assertEqual(sorted(x["request_id"] for x in listed), ["bad-text", "good-one"])
        self.assertEqual(seen["status"], "FORWARD_DONE")
        self.assertNotIn("gone-one", [x["request_id"] for x in seen["requests"]])
    def test_claim_race_and_missing_file_on_close(self):
        folder = self.enable(prompt=False)
        self.put(folder, "race-1")
        real, results = Path.rename, []

        def rename(path, target):
            moved = real(path, target)
            if not results:  # a second session claims between the first move and its update
                try:
                    results.append(intake.claim_request(self.state, "race-1", *self.actor(),
                                                        environ=self.environment())["status"])
                except ValueError as exc:
                    results.append(str(exc))
            return moved
        with patch.object(Path, "rename", rename):
            try:
                results.append(intake.claim_request(self.state, "race-1", *self.actor(),
                                                    environ=self.environment())["status"])
            except ValueError as exc:
                results.append(str(exc))
        self.assertEqual(results.count("CLAIMED"), 1, results)
        self.assertEqual(self.row("race-1")["status"], "claimed")
        (folder / "claimed" / "alpha" / "race-1.json").unlink()
        with self.assertRaisesRegex(ValueError, "claimed request file is missing"):
            intake.close_request(self.state, "race-1", "done", "note", *self.actor(), environ=self.environment())
        self.assertEqual(self.row("race-1")["status"], "claimed")

    def test_interval_round_forwards(self):
        folder = self.enable()
        fake = self.herdr()
        self.put(folder, "loop-one")
        stop = self.state / "STOP_INTAKE"
        out = io.StringIO()
        with redirect_stdout(out):
            result = intake.run_loop(self.state, 10, environ=self.environment(), transport=fake,
                                     sleep=lambda _: stop.write_text("stop", encoding="ascii"))
        self.assertEqual(result["status"], "INTAKE_STOPPED")
        first = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual((first["status"], first["sent"]), ("FORWARD_DONE", 1))
        self.assertEqual(self.row("loop-one")["status"], "prompted")
        self.assertFalse((self.state / "owner_intake.lock").exists())


if __name__ == "__main__":
    unittest.main()
