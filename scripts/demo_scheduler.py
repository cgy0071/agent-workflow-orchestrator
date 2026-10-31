"""Run a local Python-child DAG fixture; no providers, agents, or notifications."""
import argparse
import json
from pathlib import Path
import sys

from monitor_task import Transport, inspect_once, read_json, save_json
import workflow_control as control
import workflow_scheduler as scheduler


def worker(directory, message=None):
    path = Path(directory) / "worker_status.json"
    status = read_json(path)
    if message is not None:
        try:
            task = json.loads(message)
        except json.JSONDecodeError:
            lines = message.splitlines()
            names = ("Task ID: ", "Nonce: ", "Cwd: ")
            if len(lines) < 4 or any(not line.startswith(prefix) for line, prefix in zip(lines[:3], names)):
                raise ValueError("fixture message needs three task-bound header lines and a completion marker")
            markers = [line.split(": ", 1)[1] for line in lines[3:]
                       if line.startswith("Completion marker: ") or
                       (line.startswith("The final line of ") and " must be exactly: " in line)]
            if len(markers) != 1:
                raise ValueError("fixture message needs one exact completion marker")
            task = {"task_id": lines[0][len(names[0]):], "nonce": lines[1][len(names[1]):],
                    "cwd": lines[2][len(names[2]):], "marker": markers[0],
                    "completion_status": "idle"}
            if not task["task_id"] or not task["nonce"] or task["marker"] != (
                    "TASK_DONE " + task["task_id"] + " " + task["nonce"]):
                raise ValueError("fixture message marker does not bind task and nonce")
        if status.get("task_id") == task["task_id"]:
            raise ValueError("fixture refuses duplicate task submission")
        output = Path(task["cwd"])
        for name in ("HANDOFF.md", "RESULT.md"):
            text = task["task_id"] + " " + task["nonce"] + "\nOffline fixture result.\n"
            if name == "HANDOFF.md" and task.get("marker"):
                text += task["marker"] + "\n"
            (output / name).write_text(text, encoding="utf-8")
        status.update(task_id=task["task_id"], nonce=task["nonce"], completion_seq=status["completion_seq"] + 1,
                      status=task.get("completion_status", "idle"))
        save_json(path, status)
    print(json.dumps(status))


def demo(directory, completion_status="idle"):
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=False)
    state = root / "state"
    config = {"goal": "Offline demo: run A and B in parallel, then JOIN after both are accepted",
              "roles": {"planner": {}, "executor": {}, "executor-b": {}},
              "workflow": {"reviewer_role": "planner", "transitions": {"accept": "next", "reject": "stop", "repair": "repair"}},
              "scheduler": {"max_concurrency": 2, "resources": {}}}
    control.initialize(state, config)
    script = str(Path(__file__).resolve())
    specs = {}
    for role in config["roles"]:
        workspace = root / (role + "-workspace")
        workspace.mkdir()
        identity = {"kind": "python-child", "terminal": role, "launch_cwd": str(workspace)}
        save_json(workspace / "worker_status.json", {"identity": identity, "status": "idle", "completion_seq": 0})
        status_argv = [sys.executable, "-B", script, "--worker", str(workspace)]
        specs[role] = {"role": role, "adapter": "command", "harness": "python-child", "expected_identity": identity,
                       "status_argv": status_argv, "identity_argv": status_argv,
                       "output_argv": status_argv, "prompt_argv": status_argv + ["--message", "{message}"]}
    controller = control.bind(state, "planner", specs["planner"], Transport({}).reviewer_identity(specs["planner"]), "controller")
    who = (controller["instance_id"], controller["version"], "main")
    for role in ("executor", "executor-b"):
        control.bind(state, role, specs[role], Transport({}).status(specs[role])["identity"])
    nodes = []
    for name, role, deps, baseline in (("A", "executor", [], 0), ("B", "executor-b", [], 0), ("JOIN", "executor", ["A", "B"], 1)):
        cwd = root / "delivery" / name
        cwd.mkdir(parents=True)
        req = {"task_id": name, "nonce": "nonce-" + name, "generation": 1, "cwd": str(cwd),
               "workflow_state": str(state), "worker": dict(specs[role], baseline_completion_seq=baseline),
               "reviewer": specs["planner"], "dispatch_confirmed": False,
               "authorization": {"source": "local Python-child fixture only", "dispatch_worker": True, "notify_reviewer": False},
               "completion": {"binding_artifact": "HANDOFF.md", "artifacts": ["RESULT.md"],
                              "marker": "TASK_DONE " + name + " nonce-" + name, "marker_source": "artifact"}}
        if completion_status == "done":
            req["status_map"] = {"done": "idle"}
        nodes.append({"id": name, "needs": deps, "request": req,
                      "message": json.dumps({"task_id": name, "nonce": req["nonce"], "cwd": str(cwd),
                                             "marker": req["completion"]["marker"], "completion_status": completion_status})})
    graph = {"nodes": nodes}
    save_json(root / "dag.json", graph)
    scheduler.load_dag(state, "demo", graph, *who)
    passes = []
    for _ in range(3):
        report = scheduler.tick(state, "demo", *who)
        passes.append(report)
        for item in report["results"]:
            if item["status"] != "SUBMITTED":
                raise RuntimeError("fixture dispatch failed: " + json.dumps(item))
            node = next(node for node in nodes if node["id"] == item["node"])
            req = node["request"]
            observed = Transport(req).status(req["worker"])
            if (observed.get("task_id"), observed.get("nonce")) != (req["task_id"], req["nonce"]):
                raise ValueError("fixture execution evidence mismatch")
            note = Path(req["cwd"]) / "EXECUTION.md"
            note.write_text(req["task_id"] + " " + req["nonce"] + "\nOutcome: running\n" + json.dumps(observed), encoding="utf-8")
            req = scheduler.reconcile(state, req["task_id"], "running", note, *who)["request"]
            save_json(Path(req["cwd"]) / "request.json", req)
            status, _, _, receipt = inspect_once(req, Transport(req))
            if status != "review_ready":
                raise ValueError("fixture delivery missing")
            expected = req["task_id"] + " " + req["nonce"] + "\nOffline fixture result.\n"
            if (Path(req["cwd"]) / "RESULT.md").read_text(encoding="utf-8") != expected:
                raise ValueError("fixture independent result comparison failed")
            event = control.publish_event(state, req, {"status": status}, receipt)
            control.acknowledge(state, event, *who)
            review = Path(req["cwd"]) / "REVIEW.md"
            review.write_text(req["task_id"] + " " + req["nonce"] + "\nVerdict: accept\nChecked against expected fixture bytes.\n", encoding="utf-8")
            control.record_review(state, event, *who, review, "accept")
    snapshot = scheduler.snapshot(state)
    if set(snapshot["dags"]["demo"]["nodes"].values()) != {"accepted"}:
        raise ValueError("fixture did not finish")
    result = {"status": "OFFLINE_DEMO_PASS", "passes": passes, "state": str(state), "snapshot": snapshot}
    save_json(root / "demo-result.json", result)
    print(json.dumps({"status": result["status"], "state": str(state), "nodes": snapshot["dags"]["demo"]["nodes"]}))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--directory", help="new output directory; existing paths are refused")
    group.add_argument("--worker", help=argparse.SUPPRESS)
    parser.add_argument("--completion-status", choices=("idle", "done"), default="idle",
                        help="fixture worker's persistent status after a turn")
    parser.add_argument("--message", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.message)
    else:
        demo(args.directory, args.completion_status)


if __name__ == "__main__":
    main()
