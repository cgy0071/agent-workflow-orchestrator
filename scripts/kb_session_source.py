"""Completed, opt-in MCP stdio sessions in the existing authority event stream."""
from contextlib import closing
import json
from pathlib import Path
import uuid

import awo_service as service
import service_access as access

SOURCE = "mcp-stdio-v1"


def enabled(state, project):
    path = Path(state) / "kb-host.json"
    if not path.exists():
        return False
    config = json.loads(path.read_bytes())
    return (config.get("enabled") is True and
            config.get("projects", {}).get(project, {}).get("candidate_session_source") == SOURCE)


def completed(backend, token):
    if not (Path(backend.state) / "kb-host.json").is_file():
        return
    with closing(service._connect(service._db_path(backend.state))) as con, con:
        con.execute("BEGIN IMMEDIATE")
        actor = access.authenticate(con, backend.project_root, token)
        project = actor["project_id"]
        if enabled(backend.state, project):
            service._event(con, project, "session", uuid.uuid4().hex, "kb.stdio.completed",
                           actor["principal_id"], SOURCE, None, {})


def snapshot(con, project, candidate, vault):
    origins = con.execute("SELECT global_seq FROM v2_event WHERE project_id=? AND entity_type='candidate' "
                          "AND entity_id=? AND source='kb-host' AND event_type='kb.candidate.created' "
                          "AND payload_json=?", (project, candidate, json.dumps({"vault": vault}, sort_keys=True))).fetchall()
    if len(origins) != 1:
        return None
    start = origins[0][0]
    end, count = con.execute("SELECT MAX(global_seq), COUNT(*) FROM v2_event WHERE global_seq>=?",
                             (start,)).fetchone()
    if end is None or end - start + 1 != count:
        return None
    rows = con.execute("SELECT global_seq,entity_id FROM v2_event WHERE global_seq>? AND global_seq<=? "
                       "AND project_id=? AND entity_type='session' AND source=? "
                       "AND event_type='kb.stdio.completed' ORDER BY global_seq", (start, end, project, SOURCE))
    return {"complete": True, "project": project, "candidate_id": candidate, "proposed_sequence": start,
            "through_sequence": end, "completed_sessions": [
                {"project": project, "candidate_id": candidate, "status": "completed", "sequence": seq,
                 "session_id": identity} for seq, identity in rows]}
