"""Read workflow evidence and send Owner-only notifications through configured channels."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from monitor_task import validate_argv
import workflow_status
from workflow_scheduler import _read_database

KINDS = ("permission_prompt", "worker_question", "worker_failed", "start_unconfirmed",
         "review_waiting", "over_concurrency")
RETRY_MINUTES = (1, 3, 7, 15, 25, 35, 45, 55)
SCHEMA = """CREATE TABLE IF NOT EXISTS messages (
    channel TEXT NOT NULL, dedupe_key TEXT NOT NULL, kind TEXT NOT NULL,
    task_id TEXT NOT NULL, body TEXT NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    next_at TEXT NOT NULL, last_error TEXT, created_at TEXT NOT NULL,
    first_failed_at TEXT, sending_at TEXT, sent_at TEXT,
    PRIMARY KEY (channel, dedupe_key))"""
MESSAGES = json.loads(Path(__file__).with_name("notify_messages.json").read_text(encoding="utf-8"))


def instant(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def stamp(now):
    return instant(now).isoformat()


def parsed(value):
    return datetime.fromisoformat(value)


def redact(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"https?://[^\s'\"<>]+", "[redacted-url]", text, flags=re.I)
    text = re.sub(r"(?i)\b(?:terminal|session_id|instance_id|conversation_id|job_id|launch_cwd)\s*[:=]\s*\S+",
                  "[redacted-identity]", text)
    return (workflow_status.workflow_text(text) or "")[:300]


def config_of(path, windows=None):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("lang", "en") not in MESSAGES:
        raise ValueError("lang must be en or zh")
    channels = config.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ValueError("channels must be a nonempty object")
    for name, channel in channels.items():
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) or not isinstance(channel, dict):
            raise ValueError("invalid channel name or settings")
        kinds = channel.get("kinds", KINDS)
        if not isinstance(kinds, list) and kinds is not KINDS:
            raise ValueError("kinds must be an array")
        if any(kind not in KINDS for kind in kinds):
            raise ValueError("unknown notification kind")
        kind = channel.get("type")
        if kind == "file":
            if not isinstance(channel.get("path"), str) or not Path(channel["path"]).is_absolute():
                raise ValueError("file channel needs an absolute path")
        elif kind == "command":
            validate_argv(channel.get("argv"))
            if channel["argv"].count("{text}") != 1:
                raise ValueError("command argv needs one standalone {text} argument")
            # On Windows a .bat/.cmd target runs through cmd.exe, which re-parses its arguments. The text includes
            # what a worker wrote, so a batch file would let that text run commands. Require a real program.
            if (os.name == "nt" if windows is None else windows) and                     Path(channel["argv"][0]).suffix.lower() in (".bat", ".cmd"):
                raise ValueError("command channel cannot start a .bat or .cmd file on Windows; call the "
                                 "interpreter directly, for example node or python with the script path")
        elif kind == "webhook":
            if channel.get("format", "generic") not in ("generic", "feishu", "dingtalk", "wecom"):
                raise ValueError("invalid webhook format")
            if not isinstance(channel.get("url_env"), str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", channel["url_env"]):
                raise ValueError("webhook needs url_env")
            if "url" in channel:
                raise ValueError("webhook URLs belong only in environment variables")
        else:
            raise ValueError("channel type must be file, command or webhook")
    return config


def webhook_url(channel, environ):
    value = environ.get(channel["url_env"])
    if not value:
        return None
    url = urlsplit(value)
    if (url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost"))) or \
            not url.netloc or url.username or url.password or url.fragment:
        raise ValueError("webhook URL must be HTTPS or local loopback HTTP")
    return value


def evidence_line(report, nonce):
    worker = report.get("worker_report") or {}
    value = worker.get("question") or report.get("evidence") or worker.get("error") or report.get("anomaly_summary")
    if not isinstance(value, str):
        return ""
    return redact(value, (nonce,))


def discover(state, lang="en", now=None):
    """Derive only current Owner actions. Never write the workflow ledger."""
    summary = workflow_status.summarize(state, now=instant(now))
    details = workflow_status.dashboard_details(state, summary)["task_details"]
    active = {row["task_id"]: row for row in summary["tasks"] if row["phase"] not in ("accepted", "closed")}
    result = []
    def add(kind, task_id, key, report=None, nonce=""):
        desc, next_step = MESSAGES[lang][kind]
        title = redact(details.get(task_id, {}).get("title") or "", (nonce,))
        if not title or re.search(r"\bnonce\b", title, re.I):
            title = "Untitled task" if lang == "en" else "\u672a\u547d\u540d\u4efb\u52a1"
        body = desc + "\n" + ("Task: " if lang == "en" else "\u4efb\u52a1\uff1a") + title + " (" + task_id + ")\n" + \
               ("Next: " if lang == "en" else "\u4e0b\u4e00\u6b65\uff1a") + next_step
        evidence = evidence_line(report or {}, nonce)
        if evidence:
            body += "\n" + ("Evidence: " if lang == "en" else "\u7ebf\u7d22\uff1a") + evidence
        result.append({"kind": kind, "task_id": task_id, "dedupe_key": key, "body": body})
    with _read_database(state) as con:
        for row in con.execute("SELECT e.id,e.task,e.status,e.data,t.nonce FROM events e "
                               "JOIN tasks t ON t.id=e.task ORDER BY e.rowid"):
            task_id = row["task"]
            if task_id not in active or row["status"] not in ("pending", "acknowledged"):
                continue
            report = json.loads(row["data"]).get("report") or {}
            status = report.get("status")
            for kind, match in (("permission_prompt", status == "blocked"),
                                ("worker_question", report.get("cause") == "worker_question"),
                                ("worker_failed", status in ("failed", "exited", "identity_changed"))):
                if match:
                    add(kind, task_id, kind + ":" + task_id + ":" + row["id"], report, row["nonce"])
        for task_id, task in active.items():
            for kind in ("start_unconfirmed", "over_concurrency"):
                if kind in task["warnings"]:
                    add(kind, task_id, kind + ":" + task_id)
            if "review_waiting" in task["warnings"]:
                for row in con.execute("SELECT id,data FROM events WHERE task=? AND status IN ('pending','acknowledged') "
                                       "ORDER BY rowid", (task_id,)):
                    if (json.loads(row["data"]).get("report") or {}).get("status") == "review_ready":
                        add("review_waiting", task_id, "review_waiting:" + task_id + ":" + row["id"])
                        break
    return result


def outbox_path(state):
    return Path(state).resolve() / "owner_notify.sqlite3"


@contextmanager
def connection(path, readonly=False):
    if readonly:
        uri = "file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=10)
    else:
        con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        if not readonly:
            con.commit()
    except BaseException:
        if not readonly:
            con.rollback()
        raise
    finally:
        con.close()


def create_outbox(con):
    with con:
        con.execute(SCHEMA)


def outbox_summary(state):
    """Read an existing outbox without creating a file or contacting a channel."""
    path = outbox_path(state)
    if not path.is_file():
        return None
    try:
        with connection(path, readonly=True) as con:
            rows = con.execute("SELECT status,count(*) AS n FROM messages GROUP BY status").fetchall()
            counts = {row["status"]: row["n"] for row in rows}
            last = con.execute("SELECT MAX(sent_at) FROM messages WHERE status='sent'").fetchone()[0]
        return {"pending": counts.get("pending", 0) + counts.get("sending", 0),
                "given_up": counts.get("given_up", 0), "last_sent": last}
    except sqlite3.Error:
        return None


def send(channel, item, now, environ):
    kind = channel["type"]
    body = item["body"]
    if kind == "file":
        target = Path(channel["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"at": stamp(now), "kind": item["kind"], "task_id": item["task_id"],
                                     "text": body}, ensure_ascii=False) + "\n")
    elif kind == "command":
        argv = [body if part == "{text}" else part for part in channel["argv"]]
        try:
            done = subprocess.run(argv, capture_output=True, timeout=15, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("command timed out") from None
        if done.returncode != 0:
            raise RuntimeError("command exited with code " + str(done.returncode))
    else:
        url = webhook_url(channel, environ)
        fmt = channel.get("format", "generic")
        payload = ({"text": body} if fmt == "generic" else
                   {"msg_type": "text", "content": {"text": body}} if fmt == "feishu" else
                   {"msgtype": "text", "text": {"content": body}})
        request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
        with urlopen(request, timeout=10) as response:
            code = response.status
            data = response.read(65536)
        if not 200 <= code < 300:
            raise RuntimeError("HTTP status " + str(code))
        if fmt != "generic":
            parsed_body = json.loads(data.decode("utf-8")) if data else {}
            field = "code" if fmt == "feishu" else "errcode"
            if (fmt == "feishu" and field in parsed_body and parsed_body[field] != 0) or \
                    (fmt != "feishu" and parsed_body.get(field) != 0):
                raise RuntimeError("webhook returned nonzero " + field)


def claim(con, available, now):
    cutoff = stamp(now - timedelta(minutes=5))
    with con:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE messages SET status='given_up',next_at=? WHERE status IN ('pending','sending') "
                    "AND first_failed_at IS NOT NULL AND first_failed_at<=?",
                    (stamp(now), stamp(now - timedelta(minutes=57))))
        names = tuple(available)
        if not names:
            return None
        sql = "SELECT * FROM messages WHERE channel IN (" + ",".join("?" for _ in names) + ") "
        sql += "AND ((status='pending' AND next_at<=?) OR (status='sending' AND sending_at<=?)) "
        sql += "ORDER BY created_at,channel LIMIT 1"
        row = con.execute(sql, (*names, stamp(now), cutoff)).fetchone()
        if row:
            con.execute("UPDATE messages SET status='sending',attempts=attempts+1,sending_at=? "
                        "WHERE channel=? AND dedupe_key=?", (stamp(now), row["channel"], row["dedupe_key"]))
        return dict(row) if row else None


def conclude(con, item, now, error=None):
    with con:
        con.execute("BEGIN IMMEDIATE")
        if error is None:
            con.execute("UPDATE messages SET status='sent',sent_at=?,last_error=NULL WHERE channel=? AND dedupe_key=?",
                        (stamp(now), item["channel"], item["dedupe_key"]))
            return "sent"
        first = parsed(item["first_failed_at"]) if item["first_failed_at"] else now
        attempt = item["attempts"] + 1
        expired = now >= first + timedelta(minutes=57)
        minute = RETRY_MINUTES[attempt - 1] if attempt <= len(RETRY_MINUTES) else 57
        next_at = first + timedelta(minutes=minute)
        con.execute("UPDATE messages SET status=?,first_failed_at=?,next_at=?,last_error=? "
                    "WHERE channel=? AND dedupe_key=?",
                    ("given_up" if expired else "pending", stamp(first), stamp(next_at), error[:300],
                     item["channel"], item["dedupe_key"]))
        return "given_up" if expired else "pending"


def run_once(state, config, *, now=None, environ=None, dry_run=False, sender=None):
    now = instant(now)
    environ = os.environ if environ is None else environ
    found = discover(state, config.get("lang", "en"), now)
    available, notices = {}, []
    for name, channel in config["channels"].items():
        if channel["type"] == "webhook" and not environ.get(channel["url_env"]):
            notices.append("channel " + name + " skipped: URL environment variable is absent")
            continue
        available[name] = channel
    rows = [(name, item) for name, channel in available.items() for item in found
            if item["kind"] in channel.get("kinds", KINDS)]
    path = outbox_path(state)
    if dry_run:
        old, due = set(), []
        if path.is_file():
            with connection(path, readonly=True) as con:
                for row in con.execute("SELECT * FROM messages"):
                    old.add((row["channel"], row["dedupe_key"]))
                    if row["channel"] in available and ((row["status"] == "pending" and parsed(row["next_at"]) <= now) or
                            (row["status"] == "sending" and parsed(row["sending_at"]) <= now - timedelta(minutes=5))):
                        due.append({key: row[key] for key in ("channel", "kind", "task_id", "dedupe_key", "body")})
        fresh = [dict(item, channel=name) for name, item in rows if (name, item["dedupe_key"]) not in old]
        return {"status": "DRY_RUN", "would_send": due + fresh, "notices": notices}
    if not available:
        return {"status": "NO_CHANNELS", "queued": 0, "sent": 0, "notices": notices}
    queued, outcomes = 0, []
    with connection(path) as con:
        create_outbox(con)
        with con:
            for name, item in rows:
                inserted = con.execute("INSERT OR IGNORE INTO messages "
                    "(channel,dedupe_key,kind,task_id,body,status,next_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (name, item["dedupe_key"], item["kind"], item["task_id"], item["body"],
                     "pending", stamp(now), stamp(now)))
                queued += inserted.rowcount
        while True:
            item = claim(con, available, now)
            if item is None:
                break
            channel = available[item["channel"]]
            try:
                (sender or send)(channel, item, now, environ)
                result = conclude(con, item, now)
            except Exception as exc:
                secrets = [environ.get(ch["url_env"]) for ch in available.values() if ch["type"] == "webhook"]
                result = conclude(con, item, now, redact(exc, secrets))
            outcomes.append({"channel": item["channel"], "kind": item["kind"],
                             "task_id": item["task_id"], "status": result})
    return {"status": "ROUND_DONE", "queued": queued, "sent": sum(x["status"] == "sent" for x in outcomes),
            "results": outcomes, "notices": notices}


def run_loop(state, config, interval, *, sleep=None):
    sleep = sleep or time.sleep
    stop = outbox_path(state).parent / "STOP_NOTIFY"
    while not stop.exists():
        print(json.dumps(run_once(state, config), ensure_ascii=True), flush=True)
        if stop.exists():
            break
        sleep(interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True)
    parser.add_argument("--config", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true")
    group.add_argument("--interval", type=int)
    group.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.interval is not None and not 10 <= args.interval <= 3600:
        parser.error("--interval must be an integer from 10 through 3600")
    try:
        config = config_of(args.config)
        if args.interval is not None:
            run_loop(args.state, config, args.interval)
        else:
            print(json.dumps(run_once(args.state, config, dry_run=args.dry_run), ensure_ascii=True))
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Redact only the configured webhook URLs, not every environment value (short values mangle the text).
        try:
            names = [ch.get("url_env") for ch in json.loads(Path(args.config).read_text(encoding="utf-8"))
                     .get("channels", {}).values() if isinstance(ch, dict)]
        except (OSError, ValueError, AttributeError):
            names = []
        parser.exit(2, "owner_notify: " + redact(exc, tuple(os.environ.get(n) for n in names if n)) + "\n")


if __name__ == "__main__":
    main()
