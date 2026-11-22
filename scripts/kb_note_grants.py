"""Current task-local grants for exact Obsidian link labels. No file access."""
import json


def note_provider(load_config, *, project, task_id, actor, task, body_sha256, artifact_digest, vault, operation):
    pinned = json.dumps(task, sort_keys=True)

    def current(who, requested):
        if who != actor or json.dumps(requested, sort_keys=True) != pinned:
            raise PermissionError("note scope changed")
        cfg = load_config()
        project_cfg = cfg["projects"][project]
        binding = project_cfg["tasks"][task_id]
        if (cfg.get("enabled") is not True or cfg.get("artifact_digest") != artifact_digest
                or operation not in cfg.get("read_operations", []) + cfg.get("write_operations", [])
                or project_cfg["vault"] != vault or binding.get("body_sha256") != body_sha256):
            raise PermissionError("note binding changed")
        grants = binding.get("note_grants", {})
        if not isinstance(grants, dict):
            raise PermissionError("invalid note grants")
        links = grants.get(actor, [])
        if not isinstance(links, list) or not all(
            isinstance(s, str) and s.startswith("[[") and s.endswith("]]")
            and s[2:-2].strip() and s.isprintable() for s in links
        ):
            raise PermissionError("invalid note grants")
        return links

    return current
