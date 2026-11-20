"""Offline protocol-fixture checks for the project entry.

Real disposable Git repositories, real subprocesses and the real SQLite ledger
are used throughout. The worker and reviewer executables are a labelled
protocol fixture: a Python script that emits Codex JSONL events and applies
the configured file writes. These tests never call a model and never touch a
real project.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "scripts" / "project_entry.py"
PYTHON = sys.executable

FAKE = r'''"""Protocol fixture: emit Codex-style JSONL events; never call a model."""
import json
import os
from pathlib import Path
import re
import sys

mode_path = os.environ["AWO_ENTRY_FIXTURE_MODE"]
mode = json.loads(Path(mode_path).read_text(encoding="utf-8"))
prompt = sys.argv[-1]


def emit(text):
    print(json.dumps({"type": "thread.started", "thread_id": "fixture-thread"}), flush=True)
    print(json.dumps({"type": "turn.started"}), flush=True)
    print(json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": text}}), flush=True)
    print(json.dumps({"type": "turn.completed",
                      "usage": {"input_tokens": 100, "output_tokens": 25}}), flush=True)


if "Candidate SHA-256" in prompt:
    tail = prompt.split("Copy these values exactly as given here: ", 1)[1]
    binding = json.loads(tail.split(". Do not modify anything.", 1)[0])
    emit(json.dumps(dict(binding, verdict=mode.get("integration_verdict", "accept"),
                         reason="fixture integration review")))
elif "Nonce: " in prompt and "Return JSON only" in prompt:
    task = re.search(r"Task: (\S+)", prompt).group(1)
    nonce = re.search(r"Nonce: (\S+)", prompt).group(1)
    emit(json.dumps({"task_id": task, "nonce": nonce,
                     "verdict": mode.get("code_verdict", "accept"),
                     "reason": "fixture code review"}))
else:
    for rel, content in mode.get("worker_files", {}).items():
        path = Path.cwd() / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    for rel in mode.get("worker_delete", []):
        path = Path.cwd() / rel
        if path.exists():
            path.unlink()
    for rel, target in mode.get("worker_rename", {}).items():
        source, destination = Path.cwd() / rel, Path.cwd() / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
    emit(mode.get("worker_message", "fixture worker done"))
'''

APP = "def total(items):\n    return 0\n"
APP_FIXED = "def total(items):\n    return sum(items)\n"
SMOKE = """import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import total

assert total(range(5)) == 10, total(range(5))
print("SMOKE_PASS")
"""


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode:
        raise AssertionError("git " + " ".join(args) + ": " + result.stderr)
    return result.stdout.strip()


def git_opt(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=120)
    return result.stdout.strip() if result.returncode == 0 else ""


class Fixture:
    def __init__(self, base):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.repo = self.base / "source"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "app.py").write_text(APP, encoding="utf-8", newline="\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "smoke.py").write_text(SMOKE, encoding="utf-8", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base_commit = git(self.repo, "rev-parse", "HEAD")
        self.fake = self.base / "fake_codex.py"
        self.fake.write_text(FAKE, encoding="utf-8", newline="\n")
        self.mode = self.base / "mode.json"

    def spec(self, root, target, **overrides):
        value = {
            "version": 1,
            "repository": str(self.repo.resolve()),
            "goal": "Make total(items) return the arithmetic sum of the items. Only app.py may change.",
            "allowed_paths": ["app.py"],
            "verify_argv": [PYTHON, "-B", "tests/smoke.py"],
            "worker": {"harness": "codex", "executable": PYTHON, "timeout_seconds": 120},
            "reviewer": {"harness": "codex", "executable": PYTHON, "timeout_seconds": 120},
            "authorization": {"source": "Offline protocol-fixture run; no model is called.",
                              "dispatch_worker": True, "allow_local_land": True},
            "budget": {"tokens.input": 40000, "tokens.output": 8000},
            "target_ref": target,
            "work_root": str(Path(root).resolve()),
            "change_name": "fixture",
            "max_real_turns": 12,
            "timeout_seconds": 120,
        }
        value.update(overrides)
        return value

    def write_mode(self, **mode):
        settings = {"worker_files": {"app.py": APP_FIXED},
                    "worker_message": "fixture worker wrote app.py"}
        settings.update(mode)
        self.mode.write_text(json.dumps(settings), encoding="utf-8", newline="\n")
        return {"AWO_ENTRY_FIXTURE_MODE": str(self.mode)}

    def prepare(self, root, target, **overrides):
        spec_path = Path(root).parent / (Path(root).name + "-spec.json")
        spec_path.write_text(json.dumps(self.spec(root, target, **overrides), indent=2),
                             encoding="utf-8", newline="\n")
        result = self.cli("prepare", "--spec", str(spec_path), "--out", str(root))
        if result[0] == 0:
            self.install_fixture_executables(root)
        return result

    def commit_file(self, rel, content, message=None):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message or ("add " + rel))
        return git(self.repo, "rev-parse", "HEAD")

    def install_fixture_executables(self, root):
        """Point the two native bridges at the labelled protocol fixture.

        This replaces only the local command prefix; all bridge state,
        launch paths, sandbox flags and receipt parsing stay as prepared.
        """
        for name in ("worker", "reviewer"):
            config = json.loads((Path(root) / name / "config.json").read_text(encoding="utf-8"))
            config["argv_prefix"] = [PYTHON, "-B", str(self.fake)]
            (Path(root) / name / "config.json").write_text(
                json.dumps(config), encoding="utf-8", newline="\n")

    def cli(self, *args, env=None):
        merged = dict(os.environ)
        merged.update(env or {})
        result = subprocess.run([PYTHON, "-B", str(ENTRY), *args], cwd=str(ROOT),
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=300, env=merged)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        payload = json.loads(lines[-1]) if lines else None
        return result.returncode, payload, result.stderr


class ProjectEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awo-entry-test-")
        self.fixture = Fixture(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_dir(self, name):
        return Path(self.temp.name) / ("run-" + name)

    def target(self, name):
        return "refs/heads/project-entry/" + name.replace("_", "-")

    def assert_status(self, result, code, status, stage=None):
        rc, payload, stderr = result
        self.assertEqual(rc, code, msg=(payload, stderr))
        self.assertEqual(payload.get("status"), status, msg=(payload, stderr))
        if stage is not None:
            self.assertEqual(payload.get("stage"), stage, msg=(payload, stderr))

    # 1. Spec validation ---------------------------------------------------
    def test_spec_refuses_reserved_namespace_and_shell_argv(self):
        root = self.run_dir("spec")
        self.assert_status(self.fixture.prepare(root, "refs/heads/awo/entry-test"), 2,
                           "REFUSED", "spec")
        self.assertFalse((root / "plan.json").exists())
        self.assert_status(self.fixture.prepare(
            root, self.target("shell"), verify_argv=["cmd", "/c", "tests/smoke.py"]), 2,
            "REFUSED", "spec")

    # 2. Preparation is side-effect free and hash-sealed -------------------
    def test_prepare_launches_nothing_and_refuses_tampered_plan(self):
        root = self.run_dir("tamper")
        rc, payload, err = self.fixture.prepare(root, self.target("tamper"))
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertFalse(payload["models_launched"])
        self.assertFalse(payload["target_ref_created"])
        self.assertEqual(git_opt(root / "repo", "rev-parse", "--verify", "--quiet",
                                 self.target("tamper")), "")
        self.assertFalse((root / "worker" / "jobs").exists())
        plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
        plan["derived"]["goal"] = "tampered"
        (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8", newline="\n")
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute"), 2, "REFUSED", "plan")

    # 3. Wrong source identity / missing reviewer -------------------------
    def test_source_moved_is_refused_before_any_turn(self):
        root = self.run_dir("moved")
        self.assertEqual(self.fixture.prepare(root, self.target("moved"))[0], 0)
        (self.fixture.repo / "later.txt").write_text("later\n", encoding="utf-8")
        git(self.fixture.repo, "add", "-A")
        git(self.fixture.repo, "commit", "-q", "-m", "later")
        env = self.fixture.write_mode()
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "environment")
        self.assertFalse((root / "worker" / "jobs").exists())

    def test_missing_reviewer_credential_is_refused_before_worker(self):
        root = self.run_dir("reviewer")
        self.assertEqual(self.fixture.prepare(root, self.target("reviewer"))[0], 0)
        (root / "secrets" / "reviewer.token").unlink()
        env = self.fixture.write_mode()
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "environment")
        self.assertFalse((root / "worker" / "jobs").exists())

    # 4. Out-of-scope and failed verification stop before commit ----------
    def test_out_of_scope_change_stops_before_commit_and_landing(self):
        root = self.run_dir("scope")
        self.assertEqual(self.fixture.prepare(root, self.target("scope"))[0], 0)
        env = self.fixture.write_mode(worker_files={"app.py": APP_FIXED,
                                                    "extra.txt": "not allowed\n"})
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "scope")
        self.assertEqual(git(root / "repo", "rev-parse", self.target("scope")),
                         self.fixture.base_commit)
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertNotIn("source_commit", journal)

    def test_failed_acceptance_refuses_to_commit(self):
        root = self.run_dir("verify")
        self.assertEqual(self.fixture.prepare(root, self.target("verify"))[0], 0)
        env = self.fixture.write_mode(worker_files={"app.py": "def total(items):\n    return 1\n"})
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "verify")
        self.assertEqual(git(root / "repo", "rev-parse", self.target("verify")),
                         self.fixture.base_commit)
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertNotIn("source_commit", journal)

    # 5. Independent code review rejection --------------------------------
    def test_code_review_reject_stops_before_landing(self):
        root = self.run_dir("reject")
        self.assertEqual(self.fixture.prepare(root, self.target("reject"))[0], 0)
        env = self.fixture.write_mode(code_verdict="reject")
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "code_review")
        self.assertEqual(git(root / "repo", "rev-parse", self.target("reject")),
                         self.fixture.base_commit)
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["code_review"]["verdict"], "reject")

    # 6. End-to-end fixture landing and repeated-run refusal ---------------
    def test_fixture_lands_then_plain_rerun_is_refused(self):
        root = self.run_dir("land")
        target = self.target("land")
        self.assertEqual(self.fixture.prepare(root, target)[0], 0)
        env = self.fixture.write_mode()
        rc, payload, err = self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["status"], "LANDED", msg=(payload, err))
        candidate = payload["candidate_commit"]
        self.assertEqual(git(root / "repo", "rev-parse", target), candidate)
        self.assertEqual(git(root / "repo", "show", target + ":app.py"), APP_FIXED.strip())
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["phase"], "done")
        self.assertEqual(journal["turns"], 3)
        self.assertFalse((root / "workspaces" / "change").exists())
        self.assertEqual(git(self.fixture.repo, "status", "--porcelain"), "")
        self.assertEqual(git(self.fixture.repo, "rev-parse", "HEAD"), self.fixture.base_commit)
        rc, payload, err = self.fixture.cli("status", "--plan", str(root / "plan.json"))
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["status"], "STATUS", msg=(payload, err))
        self.assertEqual(payload["journal"]["phase"], "done")
        self.assertEqual(payload["target_tip"], candidate)
        self.assertFalse(payload["workspace"].get("error"))
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "already_run")
        self.assertEqual(git(root / "repo", "rev-parse", target), candidate)

    # 7. Uncertain send is diagnosed, never resent ------------------------
    def test_uncertain_send_is_not_resent(self):
        root = self.run_dir("uncertain")
        self.assertEqual(self.fixture.prepare(root, self.target("uncertain"))[0], 0)
        # Injected failure: an owned live lock makes the send adapter raise
        # after the intent is durably marked attempted. No job is created.
        lock = root / "worker" / "send.lock"
        lock.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        env = self.fixture.write_mode()
        rc, payload, err = self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assert_status((rc, payload, err), 2, "REFUSED", "dispatch")
        self.assertEqual(payload["code"], "dispatch_uncertain")
        self.assertFalse((root / "worker" / "jobs").exists())
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assert_status((rc, payload, err), 2, "REFUSED", "recover")
        self.assertEqual(payload["code"], "recovery_blocked")
        self.assertIn("do not resend", payload["error"])
        self.assertFalse((root / "worker" / "jobs").exists())
        self.assertEqual(json.loads((root / "run.json").read_text(encoding="utf-8"))["phase"],
                         "dispatch_uncertain")

    # 8. Interrupted landing is recovered once without a resend -----------
    def test_interrupted_landing_is_recovered_without_resend(self):
        root = self.run_dir("landing")
        target = self.target("landing")
        self.assertEqual(self.fixture.prepare(root, target)[0], 0)
        env = self.fixture.write_mode()
        # Injected interruption: the process dies after merge.begin records a
        # started land intent and before the intent executes. Real ledger,
        # real Git; this is the only injected step.
        wrapper = Path(self.temp.name) / "interrupt.py"
        wrapper.write_text(
            "import os, sys\n"
            "sys.path.insert(0, " + repr(str(ENTRY.parent)) + ")\n"
            "import merge_queue, project_entry, sqlite3\n"
            "real = merge_queue.execute_intent\n"
            "def stop(db, intent_id, token):\n"
            "    con = sqlite3.connect(db)\n"
            "    row = con.execute('SELECT kind FROM v2_merge_intent WHERE intent_id=?',\n"
            "                      (intent_id,)).fetchone()\n"
            "    con.close()\n"
            "    if row and row[0] == 'land':\n"
            "        os._exit(17)\n"
            "    return real(db, intent_id, token)\n"
            "merge_queue.execute_intent = stop\n"
            "sys.exit(project_entry.main(['run', '--plan', "
            + repr(str(root / "plan.json")) + ", '--confirm-execute']))\n",
            encoding="ascii", newline="\n")
        merged = dict(os.environ)
        merged.update(env)
        killed = subprocess.run([PYTHON, "-B", str(wrapper)], cwd=str(ROOT),
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=300, env=merged)
        self.assertEqual(killed.returncode, 17, msg=(killed.stdout, killed.stderr))
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertNotEqual(journal.get("phase"), "done")
        self.assertEqual(git(root / "repo", "rev-parse", target), self.fixture.base_commit)
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["status"], "RECOVERY_COMPLETED", msg=(payload, err))
        self.assertEqual(payload["result_status"], "LANDED", msg=(payload, err))
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        candidate = journal["candidate_commit"]
        self.assertEqual(git(root / "repo", "rev-parse", target), candidate)
        self.assertEqual(journal["phase"], "done")
        self.assertEqual(len(list((root / "worker" / "jobs").iterdir())), 1)
        self.assertEqual(len(list((root / "reviewer" / "jobs").iterdir())), 2)


    # -- repair regression helpers ----------------------------------------
    def run_wrapper(self, argv, body, env=None):
        wrapper = Path(self.temp.name) / ("wrap-" + uuid.uuid4().hex + ".py")
        source = ("import os, sys, sqlite3\n"
                  "sys.path.insert(0, " + repr(str(ENTRY.parent)) + ")\n"
                  "import merge_queue, project_entry\n" + body +
                  "\nsys.exit(project_entry.main(" + repr(list(argv)) + "))\n")
        wrapper.write_text(source, encoding="ascii", newline="\n")
        merged = dict(os.environ)
        merged.update(env or {})
        return subprocess.run([PYTHON, "-B", str(wrapper)], cwd=str(ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300, env=merged)

    def interrupt_landing(self, root, env, code=17):
        wrapper = Path(self.temp.name) / ("land-" + uuid.uuid4().hex + ".py")
        wrapper.write_text(
            "import os, sys, sqlite3\n"
            "sys.path.insert(0, " + repr(str(ENTRY.parent)) + ")\n"
            "import merge_queue, project_entry\n"
            "real = merge_queue.execute_intent\n"
            "def stop(db, intent_id, token):\n"
            "    con = sqlite3.connect(db)\n"
            "    row = con.execute('SELECT kind FROM v2_merge_intent WHERE intent_id=?',\n"
            "                      (intent_id,)).fetchone()\n"
            "    con.close()\n"
            "    if row and row[0] == 'land':\n"
            "        os._exit(" + str(code) + ")\n"
            "    return real(db, intent_id, token)\n"
            "merge_queue.execute_intent = stop\n"
            "sys.exit(project_entry.main(['run', '--plan', "
            + repr(str(root / "plan.json")) + ", '--confirm-execute']))\n",
            encoding="ascii", newline="\n")
        merged = dict(os.environ)
        merged.update(env)
        return subprocess.run([PYTHON, "-B", str(wrapper)], cwd=str(ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300, env=merged)

    def database(self, root):
        import sqlite3
        plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
        con = sqlite3.connect(str(Path(plan["derived"]["paths"]["state"]) / "workflow.sqlite3"))
        con.row_factory = sqlite3.Row
        return con

    # -- R1 ----------------------------------------------------------------
    def test_r1_reject_cannot_be_promoted_by_recover_or_cache(self):
        root = self.run_dir("r1-reject")
        self.assertEqual(self.fixture.prepare(root, self.target("r1-reject"))[0], 0)
        env = self.fixture.write_mode(code_verdict="reject")
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "code_review")
        for _ in range(2):
            self.assert_status(self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                                "--confirm-execute", env=env), 2,
                               "REFUSED", "recover")
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "CODE_REVIEW_REJECTED")
        self.assertEqual(journal["code_review"]["verdict"], "reject")
        con = self.database(root)
        try:
            reviews = [dict(row) for row in con.execute(
                "SELECT state,verdict FROM v2_review")]
            assignment = dict(con.execute("SELECT state,review_status,holds_slot "
                                          "FROM v2_assignment").fetchone())
        finally:
            con.close()
        self.assertEqual(reviews, [{"state": "sealed", "verdict": "reject"}])
        self.assertEqual(assignment, {"state": "rejected", "review_status": "rejected",
                                      "holds_slot": 0})
        self.assertEqual(git(root / "repo", "rev-parse", self.target("r1-reject")),
                         self.fixture.base_commit)
        self.assertEqual(len(list((root / "reviewer" / "jobs").iterdir())), 1)
        # A mutable cache cannot manufacture acceptance.
        journal.update({"status": "LANDED", "candidate_commit": self.fixture.base_commit,
                        "code_review": {"verdict": "accept"}})
        (root / "run.json").write_text(json.dumps(journal), encoding="utf-8", newline="\n")
        self.assert_status(self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "recover")
        self.assertEqual(git(root / "repo", "rev-parse", self.target("r1-reject")),
                         self.fixture.base_commit)
        status = self.fixture.cli("status", "--plan", str(root / "plan.json"))[1]
        self.assertTrue(status["next_action"].startswith("terminal"), msg=status["next_action"])

    def test_r1_repair_verdict_is_terminal(self):
        root = self.run_dir("r1-repair")
        self.assertEqual(self.fixture.prepare(root, self.target("r1-repair"))[0], 0)
        env = self.fixture.write_mode(code_verdict="repair")
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "code_review")
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "REPAIR_REQUIRED")
        self.assert_status(self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "recover")

    def test_r1_tampered_reviewer_result_fails_closed_without_resend(self):
        root = self.run_dir("r1-result")
        self.assertEqual(self.fixture.prepare(root, self.target("r1-result"))[0], 0)
        env = self.fixture.write_mode()
        crashed = self.run_wrapper(
            ["run", "--plan", str(root / "plan.json"), "--confirm-execute"],
            "def stop(plan, label, value, attempt):\n"
            "    os._exit(18)\n"
            "project_entry._record_decision = stop\n", env=env)
        self.assertEqual(crashed.returncode, 18, msg=(crashed.stdout, crashed.stderr))
        reviews = root / "reviewer" / "jobs"
        before = sorted(path.name for path in reviews.iterdir())
        self.assertEqual(len(before), 1)
        result = root / "review-runs" / "code-out" / "RESULT.md"
        original = result.read_bytes()
        result.write_bytes(original + b"tampered\n")
        self.assert_status(self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "code")
        self.assertEqual(sorted(path.name for path in reviews.iterdir()), before)
        result.write_bytes(original)
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["status"], "RECOVERY_COMPLETED")
        self.assertEqual(len(list(reviews.iterdir())), 2, msg="the review must not be resent")

    def test_r1_tampered_sealed_review_note_fails_closed(self):
        root = self.run_dir("r1-note")
        self.assertEqual(self.fixture.prepare(root, self.target("r1-note"))[0], 0)
        env = self.fixture.write_mode()
        crashed = self.run_wrapper(
            ["run", "--plan", str(root / "plan.json"), "--confirm-execute"],
            "real = project_entry.command\n"
            "def stop(state, actor, epoch, tool, payload):\n"
            "    if tool == 'assignment.close':\n"
            "        os._exit(19)\n"
            "    return real(state, actor, epoch, tool, payload)\n"
            "project_entry.command = stop\n", env=env)
        self.assertEqual(crashed.returncode, 19, msg=(crashed.stdout, crashed.stderr))
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        note = Path(journal["code_review"]["path"])
        original = note.read_bytes()
        note.write_bytes(original + b"forged line\n")
        self.assert_status(self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "code_review")
        note.write_bytes(original)
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["status"], "RECOVERY_COMPLETED")

    # -- R2 ----------------------------------------------------------------
    def test_r2_output_inside_source_refused_without_writes(self):
        base = self.fixture.repo
        (base / "app.py").write_text(APP + "# dirty tracked\n", encoding="utf-8", newline="\n")
        (base / "scratch.bin").write_bytes(b"secret-bytes\n")
        tracked = hashlib.sha256((base / "app.py").read_bytes()).hexdigest()
        untracked = hashlib.sha256((base / "scratch.bin").read_bytes()).hexdigest()
        status_before = git(base, "status", "--porcelain=v1", "--untracked-files=all")

        intended = self.fixture.base / "intended"
        out = base / "surprise-run"
        spec = self.fixture.base / "spec-inside.json"
        spec.write_text(json.dumps(self.fixture.spec(intended,
                                                     "refs/heads/project-entry/r2-inside")),
                        encoding="utf-8", newline="\n")
        self.assert_status(self.fixture.cli("prepare", "--spec", str(spec), "--out", str(out)),
                           2, "REFUSED")
        self.assertFalse(out.exists())
        self.assertFalse(intended.exists())

        same = base / "surprise2"
        spec2 = self.fixture.base / "spec-overlap.json"
        spec2.write_text(json.dumps(self.fixture.spec(same,
                                                      "refs/heads/project-entry/r2-overlap")),
                         encoding="utf-8", newline="\n")
        self.assert_status(self.fixture.cli("prepare", "--spec", str(spec2),
                                            "--out", str(same)), 2, "REFUSED", "spec")
        self.assertFalse(same.exists())

        mismatch = self.fixture.base / "mismatch-out"
        spec3 = self.fixture.base / "spec-mismatch.json"
        spec3.write_text(json.dumps(self.fixture.spec(intended,
                                                      "refs/heads/project-entry/r2-mismatch")),
                         encoding="utf-8", newline="\n")
        self.assert_status(self.fixture.cli("prepare", "--spec", str(spec3),
                                            "--out", str(mismatch)), 2, "REFUSED", "prepare")
        self.assertFalse(mismatch.exists())
        self.assertFalse(intended.exists())
        self.assertEqual(hashlib.sha256((base / "app.py").read_bytes()).hexdigest(), tracked)
        self.assertEqual(hashlib.sha256((base / "scratch.bin").read_bytes()).hexdigest(),
                         untracked)
        self.assertEqual(git(base, "status", "--porcelain=v1", "--untracked-files=all"),
                         status_before)

    # -- R3 ----------------------------------------------------------------
    def test_r3_allowed_subset_addition_deletion_and_unicode(self):
        self.fixture.commit_file("notes.txt", "keep me\n")
        root = self.run_dir("r3-add-del")
        self.assertEqual(self.fixture.prepare(
            root, self.target("r3-add-del"),
            allowed_paths=["app.py", "notes.txt", "new dir/naïve.txt"])[0], 0)
        env = self.fixture.write_mode(
            worker_files={"app.py": APP_FIXED, "new dir/naïve.txt": "unicode\n"},
            worker_delete=["notes.txt"])
        rc, payload, err = self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["status"], "LANDED")
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(journal["changed_paths"]),
                         ["app.py", "new dir/naïve.txt", "notes.txt"])
        diff = git(root / "repo", "diff", "--name-status", journal["source_commit"] + "^",
                   journal["source_commit"])
        self.assertIn("D\tnotes.txt", diff.splitlines())

    def test_r3_rename_is_supported_and_unsafe_paths_refused(self):
        root = self.run_dir("r3-rename")
        self.assertEqual(self.fixture.prepare(
            root, self.target("r3-rename"),
            allowed_paths=["tests/smoke.py", "tests/smoke_moved.py"],
            verify_argv=[PYTHON, "-B", "-c", "pass"])[0], 0)
        env = self.fixture.write_mode(
            worker_files={}, worker_rename={"tests/smoke.py": "tests/smoke_moved.py"})
        rc, payload, err = self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["status"], "LANDED")
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(journal["changed_paths"]),
                         ["tests/smoke.py", "tests/smoke_moved.py"])

        unsafe = self.run_dir("r3-unsafe")
        for name, paths in (("directory", ["tests"]), ("under-file", ["app.py/child"]),
                            ("parent", ["../outside.py"]), ("absolute", ["/etc/passwd"])):
            self.assert_status(self.fixture.prepare(
                Path(str(unsafe) + "-" + name), self.target("r3-" + name),
                allowed_paths=paths), 2, "REFUSED", "spec")

    # -- R4 ----------------------------------------------------------------
    def test_r4_concurrent_and_stale_lock_ownership(self):
        root = self.run_dir("r4-lock")
        self.assertEqual(self.fixture.prepare(root, self.target("r4-lock"))[0], 0)
        env = self.fixture.write_mode()
        crashed = self.run_wrapper(
            ["run", "--plan", str(root / "plan.json"), "--confirm-execute"],
            "real = project_entry.command\n"
            "def stop(state, actor, epoch, tool, payload):\n"
            "    if tool == 'assignment.close':\n"
            "        os._exit(19)\n"
            "    return real(state, actor, epoch, tool, payload)\n"
            "project_entry.command = stop\n", env=env)
        self.assertEqual(crashed.returncode, 19, msg=(crashed.stdout, crashed.stderr))
        lock = root / "run.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "token": "other"}),
                        encoding="ascii", newline="\n")
        self.assert_status(self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "lock")
        lock.write_text(json.dumps({"pid": 999999, "token": "stale"}),
                        encoding="ascii", newline="\n")
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertFalse(lock.exists())

    def test_r4_recovery_after_landing_and_release_is_idempotent(self):
        root = self.run_dir("r4-post")
        self.assertEqual(self.fixture.prepare(root, self.target("r4-post"))[0], 0)
        env = self.fixture.write_mode()
        crashed = self.run_wrapper(
            ["run", "--plan", str(root / "plan.json"), "--confirm-execute"],
            "def stop(plan, state, epoch, workspace_id):\n"
            "    os._exit(20)\n"
            "project_entry.release_workspace = stop\n", env=env)
        self.assertEqual(crashed.returncode, 20, msg=(crashed.stdout, crashed.stderr))
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        candidate = journal["candidate"]["candidate_commit"]
        turns = journal["turns"]
        evidence = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (root / "evidence").iterdir() if path.is_file()}
        jobs_before = sorted(path.name for path in (root / "reviewer" / "jobs").iterdir())
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["result_status"], "LANDED")
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "LANDED")
        self.assertEqual(journal["turns"], turns)
        self.assertEqual(git(root / "repo", "rev-parse", self.target("r4-post")), candidate)
        self.assertEqual(sorted(path.name for path in (root / "reviewer" / "jobs").iterdir()),
                         jobs_before)
        for name, digest in evidence.items():
            self.assertEqual(hashlib.sha256((root / "evidence" / name).read_bytes()).hexdigest(),
                             digest, msg=name)
        # Repeated recovery is also idempotent.
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(json.loads((root / "run.json").read_text())["turns"], turns)

    def test_r4_started_merge_intent_recovered_once(self):
        root = self.run_dir("r4-started")
        self.assertEqual(self.fixture.prepare(root, self.target("r4-started"))[0], 0)
        env = self.fixture.write_mode()
        crashed = self.run_wrapper(
            ["run", "--plan", str(root / "plan.json"), "--confirm-execute"],
            "real = merge_queue._store_report\n"
            "def stop(con, identity, token, report):\n"
            "    if report.get('outcome') == 'landed':\n"
            "        os._exit(21)\n"
            "    return real(con, identity, token, report)\n"
            "merge_queue._store_report = stop\n", env=env)
        self.assertEqual(crashed.returncode, 21, msg=(crashed.stdout, crashed.stderr))
        con = self.database(root)
        try:
            intent = dict(con.execute("SELECT state,executor_pid FROM v2_merge_intent "
                                      "WHERE kind='land'").fetchone())
        finally:
            con.close()
        self.assertEqual(intent["state"], "started")
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(git(root / "repo", "rev-parse", self.target("r4-started")),
                         journal["candidate"]["candidate_commit"])
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["result_status"], "LANDED")
        self.assertEqual(len(list((root / "reviewer" / "jobs").iterdir())), 2)

    def test_r4_recovery_validates_plan_source_and_receipt(self):
        cases = ("plan", "source", "receipt")
        for case in cases:
            root = self.run_dir("r4-check-" + case)
            self.assertEqual(self.fixture.prepare(root, self.target("r4-check-" + case))[0], 0)
            env = self.fixture.write_mode()
            crashed = self.interrupt_landing(root, env)
            self.assertEqual(crashed.returncode, 17, msg=(crashed.stdout, crashed.stderr))
            base = json.loads((root / "plan.json").read_text(
                encoding="utf-8"))["derived"]["base"]
            if case == "plan":
                plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
                plan["derived"]["goal"] = "tampered"
                (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8",
                                                newline="\n")
                stage = "plan"
            elif case == "source":
                self.fixture.commit_file("later.txt", "moved\n")
                stage = "environment"
            else:
                receipt = root / "evidence" / "worker-receipt.json"
                value = json.loads(receipt.read_text(encoding="utf-8"))
                value["returncode"] = 9
                receipt.write_text(json.dumps(value), encoding="utf-8", newline="\n")
                stage = "worker_turn"
            self.assert_status(self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                                "--confirm-execute", env=env), 2,
                               "REFUSED", stage)
            self.assertEqual(git(root / "repo", "rev-parse", self.target("r4-check-" + case)),
                             base)

    # -- R2/F1: pinned verification inputs --------------------------------
    def f1_script(self, name, sentinel, changed):
        script = self.fixture.base / (name + ".py")
        script.write_text('print("VERIFY_OK")\n', encoding="utf-8", newline="\n")
        if changed:
            script.write_text(
                "from pathlib import Path\n"
                "Path(" + repr(str(sentinel)) + ').write_text("changed verification code ran")\n'
                'print("CHANGED_VERIFY")\n', encoding="utf-8", newline="\n")
        return script

    def test_f1_pinned_input_refused_before_dispatch_and_before_verify(self):
        for allow in (True, False):
            for checkpoint in ("prepare", "verify"):
                name = "f1-%s-%s" % (allow, checkpoint)
                root = self.run_dir(name)
                sentinel = self.fixture.base / (name + "-sentinel.txt")
                script = self.f1_script(name, sentinel, changed=False)
                auth = {"source": "Offline protocol-fixture run; no model is called.",
                        "dispatch_worker": True, "allow_local_land": allow}
                self.assertEqual(self.fixture.prepare(
                    root, self.target(name), verify_argv=[PYTHON, "-B", str(script)],
                    authorization=auth)[0], 0)
                env = self.fixture.write_mode()
                if checkpoint == "prepare":
                    self.f1_script(name, sentinel, changed=True)
                    result = self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                              "--confirm-execute", env=env)
                    self.assert_status(result, 2, "REFUSED", "environment")
                    self.assertEqual(result[1].get("code"), "pins_changed")
                    self.assertFalse((root / "worker" / "jobs").exists())
                else:
                    changed_source = (
                        "from pathlib import Path\n"
                        "Path(" + repr(str(sentinel)) +
                        ").write_text('changed verification code ran')\n"
                        "print('CHANGED_VERIFY')\n")
                    body = (
                        "from pathlib import Path\n"
                        "real = project_entry.collect_turn\n"
                        "def stop(plan, journal, workspace_path):\n"
                        "    receipt, job_dir = real(plan, journal, workspace_path)\n"
                        "    Path(" + repr(str(script)) + ").write_text(" +
                        repr(changed_source) + ")\n"
                        "    return receipt, job_dir\n"
                        "project_entry.collect_turn = stop\n")
                    crashed = self.run_wrapper(
                        ["run", "--plan", str(root / "plan.json"), "--confirm-execute"],
                        body, env=env)
                    self.assertEqual(crashed.returncode, 2,
                                     msg=(crashed.stdout, crashed.stderr))
                    payload = json.loads(crashed.stdout.strip().splitlines()[-1])
                    self.assertEqual(payload["status"], "REFUSED")
                    self.assertEqual(payload["stage"], "environment")
                    self.assertEqual(payload["code"], "pins_changed")
                    self.assertEqual(len(list((root / "worker" / "jobs").iterdir())), 1)
                    self.assertFalse((root / "reviewer" / "jobs").exists())
                self.assertFalse(sentinel.exists(), msg="changed verification code ran")
                tip = git_opt(root / "repo", "rev-parse", "--verify", "--quiet",
                              self.target(name))
                self.assertIn(tip, ("", self.fixture.base_commit))
                journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
                self.assertNotIn("source_commit", journal)

    def test_f1_missing_verification_input_and_executable_refused(self):
        import shutil
        # A pinned absolute input that disappears is refused before dispatch.
        root = self.run_dir("f1-missing-input")
        sentinel = self.fixture.base / "f1-missing-input-sentinel.txt"
        script = self.f1_script("f1-missing-input", sentinel, changed=False)
        self.assertEqual(self.fixture.prepare(
            root, self.target("f1-missing-input"),
            verify_argv=[PYTHON, "-B", str(script)])[0], 0)
        script.unlink()
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute",
                                            env=self.fixture.write_mode()), 2,
                           "REFUSED", "environment")
        self.assertFalse((root / "worker" / "jobs").exists())
        # A pinned executable that disappears is refused before dispatch.
        root = self.run_dir("f1-missing-exe")
        copied = self.fixture.base / "pinned-python.exe"
        shutil.copy2(PYTHON, copied)
        self.assertEqual(self.fixture.prepare(
            root, self.target("f1-missing-exe"),
            verify_argv=[str(copied), "-B", "tests/smoke.py"])[0], 0)
        copied.unlink()
        self.assert_status(self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute",
                                            env=self.fixture.write_mode()), 2,
                           "REFUSED", "environment")
        self.assertFalse((root / "worker" / "jobs").exists())

    # -- R2/F2: accepted-not-landed terminal recovery ---------------------
    def no_land_authorization(self):
        return {"source": "Offline protocol-fixture run; no model is called.",
                "dispatch_worker": True, "allow_local_land": False}

    def test_f2_accepted_not_landed_recovery_is_terminal_and_idempotent(self):
        root = self.run_dir("f2-terminal")
        target = self.target("f2-terminal")
        self.assertEqual(self.fixture.prepare(
            root, target, authorization=self.no_land_authorization())[0], 0)
        env = self.fixture.write_mode()
        rc, payload, err = self.fixture.cli("run", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["status"], "ACCEPTED_NOT_LANDED")
        for _ in range(2):
            rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                                "--confirm-execute", env=env)
            self.assertEqual(rc, 0, msg=(payload, err))
            self.assertEqual(payload["status"], "RECOVERY_COMPLETED")
            self.assertEqual(payload["result_status"], "ACCEPTED_NOT_LANDED")
        self.assertEqual(git(root / "repo", "rev-parse", target), self.fixture.base_commit)
        self.assertEqual(len(list((root / "worker" / "jobs").iterdir())), 1)
        self.assertEqual(len(list((root / "reviewer" / "jobs").iterdir())), 1)
        con = self.database(root)
        try:
            task = dict(con.execute("SELECT task_state FROM v2_task").fetchone())
            assignment = dict(con.execute("SELECT state,review_status,holds_slot "
                                          "FROM v2_assignment").fetchone())
            workspace = dict(con.execute("SELECT status FROM v2_workspace").fetchone())
            merges = con.execute("SELECT COUNT(*) AS n FROM v2_merge_request").fetchone()["n"]
        finally:
            con.close()
        self.assertEqual(task, {"task_state": "accepted"})
        self.assertEqual(assignment, {"state": "completed", "review_status": "accepted",
                                      "holds_slot": 0})
        self.assertEqual(workspace, {"status": "released"})
        self.assertEqual(merges, 0)
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["status"], "ACCEPTED_NOT_LANDED")
        status = self.fixture.cli("status", "--plan", str(root / "plan.json"))[1]
        self.assertTrue(status["next_action"].startswith("complete; accepted without landing"))
        # Retained evidence is revalidated, not trusted blindly.
        verify = root / "evidence" / "verify-1.json"
        original = verify.read_bytes()
        value = json.loads(original)
        value["returncode"] = 1
        verify.write_text(json.dumps(value), encoding="utf-8", newline="\n")
        self.assert_status(self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "worker_turn")
        self.assertEqual(git(root / "repo", "rev-parse", target), self.fixture.base_commit)
        verify.write_bytes(original)

    def test_f2_crash_before_and_after_release_is_recovered(self):
        for phase in ("before", "after"):
            name = "f2-crash-" + phase
            root = self.run_dir(name)
            target = self.target(name)
            self.assertEqual(self.fixture.prepare(
                root, target, authorization=self.no_land_authorization())[0], 0)
            env = self.fixture.write_mode()
            exit_code = 22 if phase == "before" else 23
            body = ("real = project_entry.release_workspace\n"
                    "def stop(plan, state, epoch, workspace_id):\n"
                    + ("    real(plan, state, epoch, workspace_id)\n"
                       if phase == "after" else "")
                    + "    os._exit(" + str(exit_code) + ")\n"
                    "project_entry.release_workspace = stop\n")
            crashed = self.run_wrapper(
                ["run", "--plan", str(root / "plan.json"), "--confirm-execute"],
                body, env=env)
            self.assertEqual(crashed.returncode, exit_code,
                             msg=(crashed.stdout, crashed.stderr))
            for _ in range(2):
                rc, payload, err = self.fixture.cli(
                    "recover", "--plan", str(root / "plan.json"), "--confirm-execute", env=env)
                self.assertEqual(rc, 0, msg=(payload, err))
                self.assertEqual(payload["status"], "RECOVERY_COMPLETED")
                self.assertEqual(payload["result_status"], "ACCEPTED_NOT_LANDED")
            self.assertEqual(git(root / "repo", "rev-parse", target),
                             self.fixture.base_commit)
            self.assertEqual(len(list((root / "worker" / "jobs").iterdir())), 1)
            self.assertEqual(len(list((root / "reviewer" / "jobs").iterdir())), 1)
            con = self.database(root)
            try:
                assignment = dict(con.execute("SELECT state,review_status,holds_slot "
                                              "FROM v2_assignment").fetchone())
                workspace = dict(con.execute("SELECT status FROM v2_workspace").fetchone())
                merges = con.execute("SELECT COUNT(*) AS n FROM v2_merge_request").fetchone()["n"]
            finally:
                con.close()
            self.assertEqual(assignment["state"], "completed")
            self.assertEqual(assignment["review_status"], "accepted")
            self.assertEqual(assignment["holds_slot"], 0)
            self.assertEqual(workspace["status"], "released")
            self.assertEqual(merges, 0)
            journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(journal["status"], "ACCEPTED_NOT_LANDED")

    # -- R2/F3: crash after the controller commit -------------------------
    def f3_commit_wrapper(self, root, env):
        return self.run_wrapper(
            ["run", "--plan", str(root / "plan.json"), "--confirm-execute"],
            "real = project_entry.commit_change\n"
            "def stop(*args, **kwargs):\n"
            "    real(*args, **kwargs)\n"
            "    os._exit(29)\n"
            "project_entry.commit_change = stop\n", env=env)

    def test_f3_commit_crash_adopts_verified_content_without_resend(self):
        root = self.run_dir("f3-adopt")
        target = self.target("f3-adopt")
        self.assertEqual(self.fixture.prepare(root, target)[0], 0)
        env = self.fixture.write_mode()
        crashed = self.f3_commit_wrapper(root, env)
        self.assertEqual(crashed.returncode, 29, msg=(crashed.stdout, crashed.stderr))
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertNotIn("source_commit", journal)
        self.assertNotIn("changed_paths", journal)
        workspace = root / "workspaces" / "change"
        commit = git(workspace, "rev-parse", "HEAD")
        self.assertNotEqual(commit, self.fixture.base_commit)
        self.assertEqual(git(root / "repo", "rev-parse", target), self.fixture.base_commit)
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(payload["result_status"], "LANDED")
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["source_commit"], commit)
        self.assertEqual(journal["changed_paths"], ["app.py"])
        self.assertEqual(git(root / "repo", "rev-parse", target), commit)
        self.assertEqual(git(root / "repo", "show", target + ":app.py"), APP_FIXED.strip())
        self.assertEqual(len(list((root / "worker" / "jobs").iterdir())), 1)
        self.assertEqual(len(list((root / "reviewer" / "jobs").iterdir())), 2)
        turns = journal["turns"]
        rc, payload, err = self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env)
        self.assertEqual(rc, 0, msg=(payload, err))
        self.assertEqual(json.loads((root / "run.json").read_text())["turns"], turns)
        self.assertEqual(git(root / "repo", "rev-parse", target), commit)

    def test_f3_altered_committed_content_is_refused(self):
        root = self.run_dir("f3-tamper")
        target = self.target("f3-tamper")
        self.assertEqual(self.fixture.prepare(root, target)[0], 0)
        env = self.fixture.write_mode()
        crashed = self.f3_commit_wrapper(root, env)
        self.assertEqual(crashed.returncode, 29, msg=(crashed.stdout, crashed.stderr))
        workspace = root / "workspaces" / "change"
        git(workspace, "reset", "--hard", "--quiet", self.fixture.base_commit)
        (workspace / "app.py").write_text("def total(items):\n    return 99\n",
                                          encoding="utf-8", newline="\n")
        git(workspace, "add", "-A")
        git(workspace, "-c", "user.name=AWO Project Entry",
            "-c", "user.email=awo@example.invalid", "commit", "-q", "-m", "entry: fixture")
        self.assertNotEqual(git(workspace, "rev-parse", "HEAD"), self.fixture.base_commit)
        self.assert_status(self.fixture.cli("recover", "--plan", str(root / "plan.json"),
                                            "--confirm-execute", env=env), 2,
                           "REFUSED", "commit")
        self.assertEqual(git(root / "repo", "rev-parse", target), self.fixture.base_commit)
        self.assertEqual(len(list((root / "worker" / "jobs").iterdir())), 1)
        self.assertFalse((root / "reviewer" / "jobs").exists())
        journal = json.loads((root / "run.json").read_text(encoding="utf-8"))
        self.assertNotIn("source_commit", journal)


if __name__ == "__main__":
    unittest.main()

