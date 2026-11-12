"""Additive V2 budget ledger: scopes, reservations, samples, and closes.

P0-6 only. This module is behind the P0-1 application service: it adds tables
and controller commands, but it does not activate the scheduler or claim that a
provider hard cap exists. Estimator reservations are admissions, not settled
spend.
"""
from __future__ import annotations

import json
import math
import re
import uuid

import awo_service as service

BUDGET_STATES = ("ok", "warning", "pause_requested", "approval_required",
                 "cancel_requested", "paused", "overrun_pending", "exceeded",
                 "unknown_blocked", "closed")
SCOPE_TYPES = ("run", "task", "workflow")
DIMENSION = re.compile(r"[a-z][a-z0-9._-]{0,63}\Z")
DEFAULT_POLICY = {"on_unknown": "block", "warning_ratio": 0.8}


def _number(value, label):
    if type(value) is bool or not isinstance(value, (int, float)):
        raise service.ServiceError(label + " must be a finite nonnegative number")
    if not math.isfinite(float(value)) or value < 0:
        raise service.ServiceError(label + " must be a finite nonnegative number")
    return float(value)


def _dimensions(value, label="dimensions", allow_empty=False):
    if not isinstance(value, dict) or (not value and not allow_empty):
        raise service.ServiceError(label + " must be a nonempty object")
    result = {}
    for key, amount in value.items():
        if not isinstance(key, str) or not DIMENSION.fullmatch(key):
            raise service.ServiceError("invalid dimension key: " + str(key))
        result[key] = _number(amount, key)
    return result


def _scope(scope_type, scope_id):
    if scope_type not in SCOPE_TYPES:
        raise service.ServiceError("scope_type must be run, task, or workflow")
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise service.ServiceError("scope_id must be a nonempty string")
    return scope_type, scope_id


def _budget_id(scope_type, scope_id):
    import hashlib
    return "bdg-" + hashlib.sha256((scope_type + ":" + scope_id).encode()).hexdigest()[:20]


def create_budget_schema(con):
    con.execute("""CREATE TABLE IF NOT EXISTS v2_budget (
      budget_id TEXT PRIMARY KEY,
      project_id TEXT NOT NULL REFERENCES v2_project(project_id),
      scope_type TEXT NOT NULL CHECK (scope_type IN ('run','task','workflow')),
      scope_id TEXT NOT NULL,
      limits_json TEXT NOT NULL,
      policy_json TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN
        ('ok','warning','pause_requested','approval_required','cancel_requested',
         'paused','overrun_pending','exceeded','unknown_blocked','closed')),
      version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (scope_type, scope_id)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS v2_run_link (
      run_id TEXT PRIMARY KEY,
      assignment_id TEXT NOT NULL REFERENCES v2_assignment(assignment_id),
      generation INTEGER NOT NULL CHECK (generation >= 1),
      created_at TEXT NOT NULL,
      UNIQUE (assignment_id, generation)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS v2_budget_reservation (
      reservation_id TEXT PRIMARY KEY,
      budget_id TEXT NOT NULL REFERENCES v2_budget(budget_id),
      assignment_id TEXT NOT NULL,
      generation INTEGER NOT NULL CHECK (generation >= 1),
      run_id TEXT,
      dimension TEXT NOT NULL,
      amount REAL NOT NULL CHECK (amount >= 0),
      settled_amount REAL,
      state TEXT NOT NULL CHECK (state IN ('held','settled','released','unknown')),
      basis TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      UNIQUE (budget_id, assignment_id, generation, dimension)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS v2_usage_sample (
      run_id TEXT NOT NULL,
      sample_seq INTEGER NOT NULL CHECK (sample_seq >= 0),
      dimension TEXT NOT NULL,
      value REAL NOT NULL CHECK (value >= 0),
      is_delta INTEGER NOT NULL CHECK (is_delta IN (0,1)),
      source TEXT NOT NULL,
      at TEXT NOT NULL,
      PRIMARY KEY (run_id, sample_seq, dimension)
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS v2_usage_close (
      run_id TEXT PRIMARY KEY,
      assignment_id TEXT NOT NULL,
      generation INTEGER NOT NULL CHECK (generation >= 1),
      state TEXT NOT NULL CHECK (state IN ('actual','partial','unknown')),
      usage_json TEXT NOT NULL,
      authority_json TEXT NOT NULL DEFAULT '[]',
      price_table_id TEXT,
      closed_at TEXT NOT NULL,
      revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1)
    )""")
    service._ensure_column(con, "v2_budget_reservation", "run_id", "TEXT")
    service._ensure_column(con, "v2_usage_close", "authority_json", "TEXT NOT NULL DEFAULT '[]'")


def _budget(con, project_id, scope_type, scope_id, create=True):
    row = con.execute("SELECT * FROM v2_budget WHERE scope_type=? AND scope_id=?",
                      (scope_type, scope_id)).fetchone()
    if row or not create:
        return row
    now = service._now()
    budget_id = _budget_id(scope_type, scope_id)
    con.execute("INSERT INTO v2_budget(budget_id,project_id,scope_type,scope_id,limits_json,"
                "policy_json,state,version,created_at,updated_at) "
                "VALUES (?,?,?,?, '{}', ?, 'ok',1,?,?)",
                (budget_id, project_id, scope_type, scope_id,
                 json.dumps(DEFAULT_POLICY), now, now))
    return con.execute("SELECT * FROM v2_budget WHERE budget_id=?", (budget_id,)).fetchone()


def _limits(budget):
    return json.loads(budget["limits_json"])


def _policy(budget):
    value = json.loads(budget["policy_json"])
    return {**DEFAULT_POLICY, **value}


def _known_actual(con, budget):
    """Known actual by dimension for one scope. Unknown is never zero.

    Run scope reads its run's close. Task/workflow scopes aggregate closes for
    every Assignment belonging to the Task/project.
    """
    actual = {}
    estimated = {}
    unknown = {}
    if budget["scope_type"] == "run":
        rows = [con.execute("SELECT * FROM v2_usage_close WHERE run_id=?",
                            (budget["scope_id"],)).fetchone()]
    elif budget["scope_type"] == "task":
        rows = con.execute("SELECT c.* FROM v2_usage_close c JOIN v2_assignment a "
                           "ON a.assignment_id=c.assignment_id WHERE a.task_id=?",
                           (budget["scope_id"],)).fetchall()
    else:
        rows = con.execute("SELECT c.* FROM v2_usage_close c JOIN v2_assignment a "
                           "ON a.assignment_id=c.assignment_id JOIN v2_task t "
                           "ON t.task_id=a.task_id WHERE t.project_id=?",
                           (budget["scope_id"],)).fetchall()
    for row in rows:
        if row is None:
            continue
        try:
            usage = json.loads(row["usage_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(usage, dict):
            continue
        for key, item in usage.items():
            if not isinstance(item, dict):
                continue
            state = item.get("state")
            if state == "actual":
                actual[key] = actual.get(key, 0.0) + _number(item.get("value", 0), key)
            elif state == "estimated":
                estimated[key] = estimated.get(key, 0.0) + _number(item.get("value", 0), key)
            elif state == "unknown":
                unknown[key] = unknown.get(key, 0) + 1
    return actual, estimated, unknown, len(rows)


def _reserved(con, budget, states):
    rows = con.execute("SELECT dimension,amount FROM v2_budget_reservation "
                       "WHERE budget_id=? AND state IN (%s)" % ",".join("?" for _ in states),
                       (budget["budget_id"],) + tuple(states))
    result = {}
    for row in rows:
        result[row["dimension"]] = result.get(row["dimension"], 0.0) + float(row["amount"])
    return result


def _held(con, budget):
    return _reserved(con, budget, ("held",))


def _unknown_reserved(con, budget):
    return _reserved(con, budget, ("unknown",))


def _stream_exposure(con, budget):
    """Exposure from open stream samples without double counting.

    A cumulative sample replaces the stream total up to that sequence; deltas
    after it add. Deltas alone sum. This is exposure, not a settled close.
    """
    if budget["scope_type"] == "run":
        runs = [budget["scope_id"]]
    elif budget["scope_type"] == "task":
        runs = [row[0] for row in con.execute(
            "SELECT l.run_id FROM v2_run_link l JOIN v2_assignment a "
            "ON a.assignment_id=l.assignment_id WHERE a.task_id=?", (budget["scope_id"],))]
    else:
        runs = [row[0] for row in con.execute(
            "SELECT l.run_id FROM v2_run_link l JOIN v2_assignment a "
            "ON a.assignment_id=l.assignment_id JOIN v2_task t ON t.task_id=a.task_id "
            "WHERE t.project_id=?", (budget["scope_id"],))]
    totals = {}
    for run in runs:
        by_dimension = {}
        for row in con.execute("SELECT dimension,value,is_delta FROM v2_usage_sample "
                               "WHERE run_id=? ORDER BY sample_seq", (run,)):
            if row["is_delta"]:
                by_dimension[row["dimension"]] = by_dimension.get(row["dimension"], 0.0) + float(row["value"])
            else:
                by_dimension[row["dimension"]] = float(row["value"])
        for key, value in by_dimension.items():
            totals[key] = totals.get(key, 0.0) + value
    return totals




def _partial_actual(con, budget):
    totals = {}
    if budget["scope_type"] == "run":
        rows = [con.execute("SELECT * FROM v2_usage_close WHERE run_id=?",
                            (budget["scope_id"],)).fetchone()]
    elif budget["scope_type"] == "task":
        rows = con.execute("SELECT c.* FROM v2_usage_close c JOIN v2_assignment a "
                           "ON a.assignment_id=c.assignment_id WHERE a.task_id=?",
                           (budget["scope_id"],)).fetchall()
    else:
        rows = con.execute("SELECT c.* FROM v2_usage_close c JOIN v2_assignment a "
                           "ON a.assignment_id=c.assignment_id JOIN v2_task t "
                           "ON t.task_id=a.task_id WHERE t.project_id=?",
                           (budget["scope_id"],)).fetchall()
    for row in rows:
        if not row:
            continue
        try:
            usage = json.loads(row["usage_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(usage, dict):
            continue
        for key, item in usage.items():
            if isinstance(item, dict) and item.get("state") == "partial":
                totals[key] = totals.get(key, 0.0) + _number(item.get("value", 0), key)
    return totals

def _scope_assignments(con, budget):
    if budget["scope_type"] == "run":
        row = con.execute("SELECT assignment_id FROM v2_run_link WHERE run_id=?",
                          (budget["scope_id"],)).fetchone()
        return [row[0]] if row else []
    if budget["scope_type"] == "task":
        return [row[0] for row in con.execute(
            "SELECT assignment_id FROM v2_assignment WHERE task_id=?", (budget["scope_id"],))]
    return [row[0] for row in con.execute(
        "SELECT a.assignment_id FROM v2_assignment a JOIN v2_task t "
        "ON t.task_id=a.task_id WHERE t.project_id=?", (budget["scope_id"],))]


def _coverage_unknown(con, budget, limits, exclude_assignment=None):
    """Missing/unknown per-run dimensions for every bounded dimension."""
    if not limits:
        return {}
    assignments = _scope_assignments(con, budget)
    unknown = {}
    closes = {}
    if budget["scope_type"] == "run":
        rows = [con.execute("SELECT * FROM v2_usage_close WHERE run_id=?",
                            (budget["scope_id"],)).fetchone()]
    elif budget["scope_type"] == "task":
        rows = con.execute("SELECT c.* FROM v2_usage_close c JOIN v2_assignment a "
                           "ON a.assignment_id=c.assignment_id WHERE a.task_id=?",
                           (budget["scope_id"],)).fetchall()
    else:
        rows = con.execute("SELECT c.* FROM v2_usage_close c JOIN v2_assignment a "
                           "ON a.assignment_id=c.assignment_id JOIN v2_task t "
                           "ON t.task_id=a.task_id WHERE t.project_id=?",
                           (budget["scope_id"],)).fetchall()
    for row in rows:
        if row:
            closes[row["assignment_id"]] = row
    for assignment in assignments:
        if assignment == exclude_assignment:
            continue
        if assignment not in closes:
            row = con.execute("SELECT legacy_task_id FROM v2_assignment WHERE assignment_id=?",
                              (assignment,)).fetchone()
            link = con.execute("SELECT 1 FROM v2_run_link WHERE assignment_id=?",
                               (assignment,)).fetchone()
            # A run in flight is not missing usage yet; its close will settle it.
            # An imported historical attempt without a run/close is unknown.
            if row and row["legacy_task_id"] and not link:
                for key in limits:
                    unknown[key] = unknown.get(key, 0) + 1
    for row in closes.values():
        try:
            usage = json.loads(row["usage_json"])
        except (TypeError, ValueError):
            usage = {}
        if not isinstance(usage, dict):
            usage = {}
        settled = _settled_dimensions(row, usage)
        for key in limits:
            item = usage.get(key)
            if not isinstance(item, dict):
                unknown[key] = unknown.get(key, 0) + 1
            elif item.get("state") == "unknown":
                continue  # already counted by _known_actual as unknown
            elif key not in settled:
                unknown[key] = unknown.get(key, 0) + 1
    return unknown



def _derived_attempts(con, budget, exclude_assignment=None):
    if budget["scope_type"] == "run":
        return 1
    if budget["scope_type"] == "task":
        rows = con.execute("SELECT assignment_id FROM v2_assignment WHERE task_id=?",
                           (budget["scope_id"],)).fetchall()
    else:
        rows = con.execute("SELECT a.assignment_id FROM v2_assignment a JOIN v2_task t "
                           "ON t.task_id=a.task_id WHERE t.project_id=?",
                           (budget["scope_id"],)).fetchall()
    return sum(1 for row in rows if row[0] != exclude_assignment)


def _derived_repairs(con, budget):
    if budget["scope_type"] == "run":
        row = con.execute("SELECT assignment_id FROM v2_run_link WHERE run_id=?",
                          (budget["scope_id"],)).fetchone()
        if not row:
            return 0
        task = con.execute("SELECT task_id FROM v2_assignment WHERE assignment_id=?",
                           (row[0],)).fetchone()
        return con.execute("SELECT count(*) FROM v2_review WHERE task_id=? AND verdict='repair'",
                           (task[0],)).fetchone()[0] if task else 0
    if budget["scope_type"] == "task":
        return con.execute("SELECT count(*) FROM v2_review WHERE task_id=? AND verdict='repair'",
                           (budget["scope_id"],)).fetchone()[0]
    return con.execute("SELECT count(*) FROM v2_review r JOIN v2_task t ON t.task_id=r.task_id "
                       "WHERE t.project_id=? AND r.verdict='repair'",
                       (budget["scope_id"],)).fetchone()[0]

def _close_usage(close):
    if close is None:
        return {}
    try:
        usage = json.loads(close["usage_json"])
    except (TypeError, ValueError) as exc:
        raise service.ServiceError("invalid stored usage; reconciliation required") from exc
    if not isinstance(usage, dict):
        raise service.ServiceError("invalid stored usage; reconciliation required")
    return usage


def _settled_dimensions(close, usage):
    """Authority proves settlement only for explicitly actual dimensions.

    R1 also listed unknown/estimated dimensions. Older databases may have no
    authority column at all. Neither case is permission to release exposure.
    """
    if close is None or close["state"] not in ("actual", "partial"):
        return set()
    try:
        authority = json.loads(close["authority_json"]) if "authority_json" in close.keys() else []
    except (TypeError, ValueError):
        authority = []
    if not isinstance(authority, list):
        return set()
    return {key for key in authority if isinstance(key, str)
            and isinstance(usage.get(key), dict)
            and usage[key].get("state") == "actual"
            and isinstance(usage[key].get("source"), str)
            and usage[key]["source"].strip()}


def _exposure(con, budget):
    """Total effective usage: per-run, per-dimension max until settlement.

    Authoritative actual replaces that dimension once. Otherwise reservation,
    stream, and reported measurement overlap, so take their maximum. Sum these
    results across runs. Do not add actual/estimated projections a second time.
    """
    held_by_run, stream_by_run, direct = {}, {}, {}
    for row in con.execute("SELECT r.*,l.run_id AS linked_run FROM v2_budget_reservation r "
                           "LEFT JOIN v2_run_link l ON l.assignment_id=r.assignment_id "
                           "AND l.generation=r.generation WHERE r.budget_id=?",
                           (budget["budget_id"],)):
        # Include legacy settled/released rows: their labels alone do not prove
        # settlement. Older schemas may lack the reservation run_id column.
        run = (row["run_id"] if "run_id" in row.keys() else None) or row["linked_run"]
        if not run:
            direct[row["dimension"]] = direct.get(row["dimension"], 0.0) + float(row["amount"])
        else:
            held_by_run.setdefault(run, {})
            held_by_run[run][row["dimension"]] = max(
                held_by_run[run].get(row["dimension"], 0.0), float(row["amount"]))
    if budget["scope_type"] == "run":
        runs = [budget["scope_id"]]
    elif budget["scope_type"] == "task":
        runs = [row[0] for row in con.execute(
            "SELECT l.run_id FROM v2_run_link l JOIN v2_assignment a "
            "ON a.assignment_id=l.assignment_id WHERE a.task_id=?", (budget["scope_id"],))]
    else:
        runs = [row[0] for row in con.execute(
            "SELECT l.run_id FROM v2_run_link l JOIN v2_assignment a "
            "ON a.assignment_id=l.assignment_id JOIN v2_task t ON t.task_id=a.task_id "
            "WHERE t.project_id=?", (budget["scope_id"],))]
    for run in runs:
        values = {}
        for row in con.execute("SELECT dimension,value,is_delta FROM v2_usage_sample "
                               "WHERE run_id=? ORDER BY sample_seq", (run,)):
            if row["is_delta"]:
                values[row["dimension"]] = values.get(row["dimension"], 0.0) + float(row["value"])
            else:
                values[row["dimension"]] = float(row["value"])
        stream_by_run[run] = values
    all_runs = set(held_by_run) | set(stream_by_run) | set(runs)
    effective = dict(direct)
    for run in all_runs:
        held = held_by_run.get(run, {})
        stream = stream_by_run.get(run, {})
        close = con.execute("SELECT * FROM v2_usage_close WHERE run_id=?", (run,)).fetchone()
        usage = _close_usage(close)
        settled = _settled_dimensions(close, usage)
        for key in set(held) | set(stream) | set(usage):
            item = usage.get(key, {})
            measured = 0.0
            if isinstance(item, dict) and item.get("state") in ("actual", "estimated", "partial"):
                measured = _number(item.get("value"), key)
            value = measured if key in settled else max(
                held.get(key, 0.0), stream.get(key, 0.0), measured)
            effective[key] = effective.get(key, 0.0) + value
    return effective


def budget_status(state, scope_type, scope_id):
    scope_type, scope_id = _scope(scope_type, scope_id)
    con = service._connect(service._db_path(state), readonly=True)
    try:
        if not service.schema_present(con):
            raise service.ServiceError("v2 schema is absent; run migrate --apply first")
        budget = _budget(con, service._state_row(con)["project_id"], scope_type, scope_id, create=False)
        if not budget:
            return {"scope_type": scope_type, "scope_id": scope_id, "exists": False,
                    "limits": {}, "known_actual": {}, "held": {}, "unknown": {},
                    "known_only": True, "may_be_lower": True, "state": "absent"}
        actual, estimated, unknown, closes = _known_actual(con, budget)
        held = _held(con, budget)
        unknown_reserved = _unknown_reserved(con, budget)
        stream = _stream_exposure(con, budget)
        limits = _limits(budget)
        for key, count in _coverage_unknown(con, budget, limits).items():
            unknown[key] = unknown.get(key, 0) + count
        partial_actual = _partial_actual(con, budget)
        exposure = _exposure(con, budget)
        remaining = {}
        warning = {}
        for key, limit in limits.items():
            used = exposure.get(key, 0.0)
            remaining[key] = max(0.0, float(limit) - used)
            warning[key] = used >= float(limit) * _policy(budget)["warning_ratio"]
        attempts = len(_scope_assignments(con, budget))
        repairs = 0
        if scope_type == "task":
            repairs = con.execute("SELECT count(*) FROM v2_review WHERE task_id=? AND verdict='repair'",
                                  (scope_id,)).fetchone()[0]
        return {"scope_type": scope_type, "scope_id": scope_id, "exists": True,
                "budget_id": budget["budget_id"], "limits": limits,
                "known_actual": actual, "estimated": estimated,
                "partial_actual": partial_actual, "unknown": unknown,
                "held": held, "unknown_reserved": unknown_reserved,
                "stream_exposure": stream, "effective_usage": exposure,
                "nominal_remaining": remaining, "warning": warning,
                "known_only": True, "may_be_lower": True,
                "state": budget["state"], "version": budget["version"],
                "attempts": attempts, "repairs": repairs, "closes": closes}
    finally:
        con.close()


BLOCKED_STATES = ("pause_requested", "approval_required", "cancel_requested",
                  "paused", "overrun_pending", "exceeded", "unknown_blocked")


def resolve_scopes(con, project_id, run_id, assignment_id, generation):
    """Derive run/task/workflow scopes from the Assignment and its Task."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise service.ServiceError("run_id is required")
    assignment = con.execute("SELECT * FROM v2_assignment WHERE assignment_id=?",
                             (assignment_id,)).fetchone()
    if not assignment:
        raise service.ServiceError("assignment not found: " + str(assignment_id))
    if assignment["generation"] != generation:
        raise service.ServiceError("assignment generation does not match")
    task = con.execute("SELECT * FROM v2_task WHERE task_id=?", (assignment["task_id"],)).fetchone()
    if not task or task["project_id"] != project_id:
        raise service.ServiceError("assignment/task is not in this project")
    existing = con.execute("SELECT * FROM v2_run_link WHERE run_id=?", (run_id,)).fetchone()
    if existing:
        if (existing["assignment_id"], existing["generation"]) != (assignment_id, generation):
            raise service.ServiceError("run is already linked to another assignment/generation")
    else:
        other = con.execute("SELECT run_id FROM v2_run_link WHERE assignment_id=? AND generation=?",
                            (assignment_id, generation)).fetchone()
        if other:
            raise service.ServiceError("assignment/generation already has a different run")
        con.execute("INSERT INTO v2_run_link(run_id,assignment_id,generation,created_at) VALUES (?,?,?,?)",
                    (run_id, assignment_id, generation, service._now()))
    return [("run", run_id), ("task", assignment["task_id"]), ("workflow", project_id)]


def _reserve_in_transaction(con, project_id, run_id, assignment_id, generation, dimensions, basis):
    """In-transaction API for a later scheduler; caller owns BEGIN IMMEDIATE."""
    if not isinstance(assignment_id, str) or not assignment_id:
        raise service.ServiceError("assignment_id is required")
    if type(generation) is not int or generation < 1:
        raise service.ServiceError("generation must be a positive integer")
    dimensions = _dimensions(dimensions)
    scopes = resolve_scopes(con, project_id, run_id, assignment_id, generation)
    if con.execute("SELECT 1 FROM v2_usage_close WHERE run_id=?", (run_id,)).fetchone():
        raise service.ServiceError("a closed run cannot receive new reservations")
    created = []
    for scope_type, scope_id in scopes:
        budget = _budget(con, project_id, scope_type, scope_id, create=True)
        if budget["state"] in BLOCKED_STATES:
            raise service.ServiceError("budget state blocks admission: " + budget["state"])
        limits = _limits(budget)
        missing = sorted(set(limits) - set(dimensions))
        if missing:
            raise service.ServiceError("bounded dimensions require estimates: " + ", ".join(missing))
        actual, estimated, unknown, closes = _known_actual(con, budget)
        for key, count in _coverage_unknown(con, budget, limits, exclude_assignment=assignment_id).items():
            unknown[key] = unknown.get(key, 0) + count
        exposure = _exposure(con, budget)
        policy = _policy(budget)
        existing_rows = {}
        for key in dimensions:
            row = con.execute("SELECT * FROM v2_budget_reservation WHERE budget_id=? AND "
                              "assignment_id=? AND generation=? AND dimension=?",
                              (budget["budget_id"], assignment_id, generation, key)).fetchone()
            if row:
                if row["state"] != "held" or float(row["amount"]) != dimensions[key]:
                    raise service.ServiceError("reservation id was reused with a different amount/state")
                existing_rows[key] = row
        pending = {key: value for key, value in dimensions.items() if key not in existing_rows}
        for key, estimate in pending.items():
            limit = limits.get(key)
            if limit is None:
                continue
            if key == "attempts":
                if _derived_attempts(con, budget, exclude_assignment=assignment_id) + 1 > int(limit):
                    raise service.ServiceError("task/workflow attempt cap reached")
                continue
            if key == "repairs":
                if _derived_repairs(con, budget) >= int(limit):
                    raise service.ServiceError("task/workflow repair cap reached")
                continue
            if unknown.get(key) and policy["on_unknown"] == "block":
                raise service.ServiceError("unknown usage blocks admission for " + key)
            used = exposure.get(key, 0.0)
            if used + estimate > float(limit):
                raise service.ServiceError("budget cap exceeded for " + scope_type + ":" + key)
        for key, estimate in pending.items():
            rid = "res-" + uuid.uuid4().hex
            now = service._now()
            con.execute("INSERT INTO v2_budget_reservation(reservation_id,budget_id,assignment_id,"
                        "generation,run_id,dimension,amount,settled_amount,state,basis,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,NULL,'held',?,?,?)",
                        (rid, budget["budget_id"], assignment_id, generation, run_id, key, estimate,
                         basis, now, now))
            created.append(rid)
            limit = limits.get(key)
            if budget["state"] == "ok" and limit is not None:
                used = exposure.get(key, 0.0) + estimate
                if used >= float(limit) * policy["warning_ratio"]:
                    con.execute("UPDATE v2_budget SET state='warning',version=version+1,updated_at=? "
                                "WHERE budget_id=?", (now, budget["budget_id"]))
                    budget = con.execute("SELECT * FROM v2_budget WHERE budget_id=?",
                                         (budget["budget_id"],)).fetchone()
    return created


def _controller_env(env, tool):
    env = service._command_envelope(env, (tool,))
    return env


def budget_reserve(state, actor, version, context, envelope):
    env = _controller_env(envelope, "budget.reserve")
    payload = env["payload"]
    required = {"assignment_id", "generation", "run_id", "dimensions"}
    if set(payload) - required - {"basis"}:
        raise service.ServiceError("budget.reserve has unknown fields")
    con = service._registry_open(state)
    try:
        state_row = service._registry_controller(con, actor, version, context, env)
        service._refuse_frozen(state_row)
        project_id = state_row["project_id"]
        principal = str(service._binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = service._request_sha(env)
        replay = service._replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        created = _reserve_in_transaction(con, project_id, payload["run_id"],
                                          payload["assignment_id"], payload["generation"],
                                          payload["dimensions"], payload.get("basis", "estimate"))
        response = {"status": "RESERVED", "reservation_ids": created,
                    "assignment_id": payload["assignment_id"], "run_id": payload["run_id"],
                    "generation": payload["generation"], "dimensions": payload["dimensions"],
                    "replayed": False}
        service._record(con, project_id, principal, env, request_sha, response)
        service._event(con, project_id, "budget", payload["assignment_id"], "budget.reserved",
                       actor, "v2", None, {"run_id": payload["run_id"],
                                            "dimensions": payload["dimensions"]})
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def budget_sample(state, actor, version, context, envelope):
    env = _controller_env(envelope, "budget.sample")
    payload = env["payload"]
    required = {"run_id", "assignment_id", "generation", "sample_seq", "dimension", "value", "is_delta", "source"}
    if set(payload) != required:
        raise service.ServiceError("budget.sample needs run_id, assignment_id, generation, sample_seq, "
                                   "dimension, value, is_delta, source")
    if type(payload["sample_seq"]) is not int or payload["sample_seq"] < 0:
        raise service.ServiceError("sample_seq must be a nonnegative integer")
    if type(payload["generation"]) is not int or payload["generation"] < 1:
        raise service.ServiceError("generation must be a positive integer")
    if not isinstance(payload["dimension"], str) or not DIMENSION.fullmatch(payload["dimension"]):
        raise service.ServiceError("invalid dimension")
    value = _number(payload["value"], "value")
    if type(payload["is_delta"]) is not bool:
        raise service.ServiceError("is_delta must be boolean")
    if not isinstance(payload["source"], str) or not payload["source"].strip():
        raise service.ServiceError("sample source must be nonempty")
    con = service._registry_open(state)
    try:
        state_row = service._registry_controller(con, actor, version, context, env)
        project_id = state_row["project_id"]
        principal = str(service._binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = service._request_sha(env)
        replay = service._replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        link = con.execute("SELECT * FROM v2_run_link WHERE run_id=?", (payload["run_id"],)).fetchone()
        if not link or (link["assignment_id"], link["generation"]) != (payload["assignment_id"], payload["generation"]):
            raise service.ServiceError("run is not linked to this assignment/generation")
        if con.execute("SELECT 1 FROM v2_usage_close WHERE run_id=?", (payload["run_id"],)).fetchone():
            raise service.ServiceError("sample after an authoritative/closed run is rejected")
        existing = con.execute("SELECT * FROM v2_usage_sample WHERE run_id=? AND sample_seq=? AND dimension=?",
                               (payload["run_id"], payload["sample_seq"], payload["dimension"])).fetchone()
        if existing:
            if (float(existing["value"]) != value or bool(existing["is_delta"]) != payload["is_delta"]
                    or existing["source"] != str(payload["source"])):
                raise service.ServiceError("duplicate sample has different value/kind/source")
            response = {"status": "SAMPLE_DUPLICATE", "replayed": False}
        else:
            con.execute("INSERT INTO v2_usage_sample(run_id,sample_seq,dimension,value,is_delta,source,at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (payload["run_id"], payload["sample_seq"], payload["dimension"], value,
                         1 if payload["is_delta"] else 0, str(payload["source"]), service._now()))
            response = {"status": "SAMPLE_RECORDED", "replayed": False}
        service._record(con, project_id, principal, env, request_sha, response)
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def budget_close(state, actor, version, context, envelope):
    env = _controller_env(envelope, "budget.close")
    payload = env["payload"]
    allowed = {"run_id", "assignment_id", "generation", "state", "usage",
               "authoritative", "reconcile", "price_table_id", "expected_revision"}
    if set(payload) - allowed:
        raise service.ServiceError("budget.close has unknown fields")
    if payload.get("state") not in ("actual", "partial", "unknown"):
        raise service.ServiceError("close state must be actual, partial, or unknown")
    authoritative = payload.get("authoritative", payload["state"] == "actual")
    reconcile = payload.get("reconcile", False)
    if type(authoritative) is not bool or type(reconcile) is not bool:
        raise service.ServiceError("authoritative and reconcile must be booleans")
    if payload["state"] == "actual" and not authoritative:
        raise service.ServiceError("an actual close must be authoritative")
    if type(payload.get("generation")) is not int or payload["generation"] < 1:
        raise service.ServiceError("generation must be a positive integer")
    usage = payload.get("usage", {})
    if not isinstance(usage, dict):
        raise service.ServiceError("usage must be an object")
    for key, item in usage.items():
        if not isinstance(key, str) or not DIMENSION.fullmatch(key):
            raise service.ServiceError("invalid usage dimension: " + str(key))
        if not isinstance(item, dict) or item.get("state") not in ("actual", "estimated", "unknown"):
            raise service.ServiceError("usage entries need state actual, estimated, or unknown")
        if not isinstance(item.get("source"), str) or not item["source"].strip():
            raise service.ServiceError("usage needs a provenance source for " + key)
        if item["state"] in ("actual", "estimated"):
            _number(item.get("value"), key)
        elif not item.get("reason"):
            raise service.ServiceError("unknown usage needs a reason for " + key)
    if payload["state"] == "actual" and (not usage or any(
            item["state"] != "actual" for item in usage.values())):
        raise service.ServiceError("an actual close needs actual dimensions; use partial for mixed usage")
    if payload["state"] == "unknown" and any(item["state"] != "unknown" for item in usage.values()):
        raise service.ServiceError("an unknown close cannot contain measured dimensions; use partial")
    con = service._registry_open(state)
    try:
        state_row = service._registry_controller(con, actor, version, context, env)
        project_id = state_row["project_id"]
        principal = str(service._binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = service._request_sha(env)
        replay = service._replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        link = con.execute("SELECT * FROM v2_run_link WHERE run_id=?", (payload["run_id"],)).fetchone()
        if not link or (link["assignment_id"], link["generation"]) != (payload["assignment_id"], payload["generation"]):
            raise service.ServiceError("run is not linked to this assignment/generation")
        existing = con.execute("SELECT * FROM v2_usage_close WHERE run_id=?", (payload["run_id"],)).fetchone()
        if reconcile and existing is None:
            raise service.ServiceError("reconcile requires an existing close revision")
        # Canonicalize stream-only closes before comparing duplicate payloads.
        if not authoritative and not usage:
            totals = {}
            for row in con.execute("SELECT dimension,value,is_delta FROM v2_usage_sample "
                                   "WHERE run_id=? ORDER BY sample_seq", (payload["run_id"],)):
                if row["is_delta"]:
                    totals[row["dimension"]] = totals.get(row["dimension"], 0.0) + float(row["value"])
                else:
                    totals[row["dimension"]] = float(row["value"])
            usage = {key: {"state": "partial", "value": value, "source": "stream_exposure"}
                     for key, value in totals.items()}
        authority = sorted(key for key, item in usage.items()
                           if authoritative and item["state"] == "actual")
        if existing and not reconcile:
            if (existing["usage_json"] == json.dumps(usage, sort_keys=True)
                    and existing["state"] == payload["state"]
                    and existing["assignment_id"] == payload["assignment_id"]
                    and existing["generation"] == payload["generation"]
                    and _settled_dimensions(existing, _close_usage(existing)) == set(authority)
                    and existing["price_table_id"] == payload.get("price_table_id")):
                response = {"status": "CLOSE_DUPLICATE", "replayed": False}
                service._record(con, project_id, principal, env, request_sha, response)
                con.commit()
                return response
            raise service.ServiceError("run already closed; explicit reconcile is required")
        if existing and reconcile:
            if not authoritative:
                raise service.ServiceError("only an authoritative close may reconcile an earlier close")
            if (existing["assignment_id"], existing["generation"]) != (payload["assignment_id"], payload["generation"]):
                raise service.ServiceError("a run cannot be rebound to another assignment/generation")
            if not service._strict_int(payload.get("expected_revision")) or payload["expected_revision"] != existing["revision"]:
                raise service.ServiceError("stale close revision")
            # An uncertain correction cannot erase an earlier measurement or
            # lower its exposure. A lower actual final value may replace it.
            for key, old in _close_usage(existing).items():
                if isinstance(old, dict) and old.get("state") in ("actual", "estimated", "partial"):
                    new = usage.get(key, {})
                    if key not in authority and (new.get("state") not in ("actual", "estimated")
                            or _number(new.get("value"), key) < _number(old.get("value"), key)):
                        raise service.ServiceError("reconcile cannot discard or reduce unresolved usage for " + key)
        usage_json = json.dumps(usage, sort_keys=True)
        authority_json = json.dumps(authority)
        revision = 1 if not existing else existing["revision"] + 1
        con.execute("INSERT INTO v2_usage_close(run_id,assignment_id,generation,state,usage_json,"
                    "authority_json,price_table_id,closed_at,revision) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET assignment_id=excluded.assignment_id,"
                    "generation=excluded.generation,state=excluded.state,usage_json=excluded.usage_json,"
                    "authority_json=excluded.authority_json,"
                    "price_table_id=excluded.price_table_id,closed_at=excluded.closed_at,"
                    "revision=excluded.revision",
                    (payload["run_id"], payload["assignment_id"], payload["generation"],
                     payload["state"], usage_json, authority_json, payload.get("price_table_id"),
                     service._now(), revision))
        # Settle or retain reservations for this assignment/generation.
        for row in con.execute("SELECT * FROM v2_budget_reservation WHERE assignment_id=? AND generation=?",
                               (payload["assignment_id"], payload["generation"])).fetchall():
            item = usage.get(row["dimension"]) if isinstance(usage, dict) else None
            if row["dimension"] in authority:
                settled = _number(item.get("value", 0), row["dimension"])
                con.execute("UPDATE v2_budget_reservation SET state='settled',settled_amount=?,"
                            "updated_at=? WHERE reservation_id=?",
                            (settled, service._now(), row["reservation_id"]))
                budget = con.execute("SELECT * FROM v2_budget WHERE budget_id=?",
                                     (row["budget_id"],)).fetchone()
                limit = _limits(budget).get(row["dimension"])
                if limit is not None:
                    exposed = _exposure(con, budget).get(row["dimension"], 0.0)
                    if exposed > float(limit):
                        con.execute("UPDATE v2_budget SET state='exceeded',version=version+1,updated_at=? "
                                    "WHERE budget_id=?", (service._now(), row["budget_id"]))
            else:
                # Authority of the report is not settlement of an uncertain
                # dimension. Non-authoritative actual is also only observed.
                con.execute("UPDATE v2_budget_reservation SET state='unknown',settled_amount=NULL,updated_at=? "
                            "WHERE reservation_id=?", (service._now(), row["reservation_id"]))
        response = {"status": "CLOSED", "run_id": payload["run_id"],
                    "state": payload["state"], "revision": revision, "replayed": False}
        service._record(con, project_id, principal, env, request_sha, response)
        service._event(con, project_id, "run", payload["run_id"], "budget.closed", actor, "v2", None,
                       {"state": payload["state"], "authoritative": authoritative,
                        "revision": revision, "usage": usage, "settled_dimensions": authority,
                        "previous_close": dict(existing) if existing else None})
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def budget_limit(state, actor, version, context, envelope):
    env = _controller_env(envelope, "budget.limit")
    payload = env["payload"]
    allowed = {"scope_type", "scope_id", "limits", "policy", "expected_revision"}
    if set(payload) - allowed:
        raise service.ServiceError("budget.limit has unknown fields")
    scope_type, scope_id = _scope(payload.get("scope_type"), payload.get("scope_id"))
    limits = _dimensions(payload.get("limits", {}), "limits", allow_empty=True)
    policy = payload.get("policy", {})
    if not isinstance(policy, dict) or set(policy) - {"on_unknown", "warning_ratio"}:
        raise service.ServiceError("invalid policy")
    if policy.get("on_unknown", "block") not in ("block", "warn"):
        raise service.ServiceError("on_unknown must be block or warn")
    if "warning_ratio" in policy:
        ratio = _number(policy["warning_ratio"], "warning_ratio")
        if ratio > 1:
            raise service.ServiceError("warning_ratio must be at most 1")
        policy["warning_ratio"] = ratio
    con = service._registry_open(state)
    try:
        state_row = service._registry_controller(con, actor, version, context, env)
        project_id = state_row["project_id"]
        principal = str(service._binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = service._request_sha(env)
        replay = service._replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        budget = _budget(con, project_id, scope_type, scope_id, create=True)
        if not service._strict_int(payload.get("expected_revision")) or payload["expected_revision"] != budget["version"]:
            raise service.ServiceError("stale revision")
        merged_policy = {**_policy(budget), **policy}
        con.execute("UPDATE v2_budget SET limits_json=?,policy_json=?,version=version+1,updated_at=? "
                    "WHERE budget_id=?",
                    (json.dumps(limits, sort_keys=True), json.dumps(merged_policy, sort_keys=True),
                     service._now(), budget["budget_id"]))
        response = {"status": "LIMITS_SET", "budget_id": budget["budget_id"],
                    "limits": limits, "version": budget["version"] + 1, "replayed": False}
        service._record(con, project_id, principal, env, request_sha, response)
        service._event(con, project_id, "budget", budget["budget_id"], "budget.limit", actor, "v2", None,
                       {"limits": limits})
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def budget_action(state, actor, version, context, envelope):
    env = _controller_env(envelope, "budget.action")
    payload = env["payload"]
    allowed = {"scope_type", "scope_id", "action", "reason", "expected_revision"}
    if set(payload) - allowed:
        raise service.ServiceError("budget.action has unknown fields")
    scope_type, scope_id = _scope(payload.get("scope_type"), payload.get("scope_id"))
    action = payload.get("action")
    mapping = {"warning": "warning", "pause_requested": "pause_requested",
               "approval_required": "approval_required", "cancel_requested": "cancel_requested"}
    if action not in mapping:
        raise service.ServiceError("action must be warning, pause_requested, approval_required, or cancel_requested")
    con = service._registry_open(state)
    try:
        state_row = service._registry_controller(con, actor, version, context, env)
        project_id = state_row["project_id"]
        principal = str(service._binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = service._request_sha(env)
        replay = service._replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        budget = _budget(con, project_id, scope_type, scope_id, create=True)
        if not service._strict_int(payload.get("expected_revision")) or payload["expected_revision"] != budget["version"]:
            raise service.ServiceError("stale revision")
        con.execute("UPDATE v2_budget SET state=?,version=version+1,updated_at=? WHERE budget_id=?",
                    (mapping[action], service._now(), budget["budget_id"]))
        response = {"status": "ACTION_RECORDED", "budget_id": budget["budget_id"],
                    "state": mapping[action], "version": budget["version"] + 1,
                    "note": "Ledger state only; it does not prove a worker paused or terminated.",
                    "replayed": False}
        service._record(con, project_id, principal, env, request_sha, response)
        service._event(con, project_id, "budget", budget["budget_id"], "budget.action", actor, "v2", None,
                       {"action": action, "reason": payload.get("reason")})
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def budget_approve(state, actor, version, context, envelope):
    env = _controller_env(envelope, "budget.approve")
    payload = env["payload"]
    allowed = {"scope_type", "scope_id", "expected_revision", "reason"}
    if set(payload) - allowed:
        raise service.ServiceError("budget.approve has unknown fields")
    scope_type, scope_id = _scope(payload.get("scope_type"), payload.get("scope_id"))
    con = service._registry_open(state)
    try:
        state_row = service._registry_controller(con, actor, version, context, env)
        project_id = state_row["project_id"]
        principal = str(service._binding(con, actor, version, context)["role"]) + ":" + actor
        request_sha = service._request_sha(env)
        replay = service._replay(con, project_id, principal, env, request_sha)
        if replay is not None:
            con.rollback()
            return replay
        budget = _budget(con, project_id, scope_type, scope_id, create=True)
        if not service._strict_int(payload.get("expected_revision")) or payload["expected_revision"] != budget["version"]:
            raise service.ServiceError("stale revision")
        con.execute("UPDATE v2_budget SET state='ok',version=version+1,updated_at=? WHERE budget_id=?",
                    (service._now(), budget["budget_id"]))
        response = {"status": "APPROVED", "budget_id": budget["budget_id"],
                    "state": "ok", "version": budget["version"] + 1, "replayed": False}
        service._record(con, project_id, principal, env, request_sha, response)
        service._event(con, project_id, "budget", budget["budget_id"], "budget.approved", actor, "v2", None,
                       {"reason": payload.get("reason")})
        con.commit()
        return response
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
