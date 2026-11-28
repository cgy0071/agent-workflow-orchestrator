"""Read-only local CLI discovery and named execution lanes for AWO.

Configuration never grants dispatch or execution authorization. Lanes only
describe how to invoke a local CLI; explicit confirmation and independent
review remain mandatory.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

SUPPORTED_CLIS = ("codex", "claude", "pi", "omp", "agy")
VALID_ROLES = ("executor", "reviewer")
SCHEMA_VERSION = 1

CLI_SPECS = {
    "codex": {"headless_args": ["exec"], "version_args": ["--version"], "supports_resume": True},
    "claude": {"headless_args": ["--print"], "version_args": ["--version"], "supports_resume": True},
    "pi": {"headless_args": ["--print"], "version_args": ["--version"], "supports_resume": False},
    "omp": {"headless_args": ["--print"], "version_args": ["--version"], "supports_resume": False},
    "agy": {"headless_args": ["--print"], "version_args": ["--version"], "supports_resume": True},
}

AUTHORIZATION_NOTE = (
    "Configuration does not authorize dispatch or execution; "
    "explicit confirmation and independent review are required."
)


class LaneError(ValueError):
    """Raised when a lane configuration or lane selection is invalid."""


class LaneArguments(argparse.ArgumentParser):
    def error(self, message):
        raise LaneError(message)


def _is_ps1(path_str):
    return str(path_str).lower().endswith(".ps1")


def _common_windows_dirs():
    dirs = []
    local_app = os.environ.get("LOCALAPPDATA")
    app_data = os.environ.get("APPDATA")
    profile = os.environ.get("USERPROFILE")
    if local_app:
        dirs.append(Path(local_app) / "Programs")
        dirs.append(Path(local_app) / "Microsoft" / "WinGet" / "Links")
    if app_data:
        dirs.append(Path(app_data) / "npm")
    if profile:
        dirs.append(Path(profile) / ".local" / "bin")
        dirs.append(Path(profile) / ".cargo" / "bin")
    existing = []
    for d in dirs:
        try:
            if d.is_dir():
                existing.append(str(d))
        except OSError:
            continue
    return existing


def _unwrap_windows_shim(name, shim_path):
    shim = Path(shim_path)
    if name == "claude":
        candidate = shim.parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        if candidate.is_file():
            return str(candidate.resolve())
    if name == "codex":
        arch = {
            "amd64": ("x64", "x86_64"),
            "x86_64": ("x64", "x86_64"),
            "arm64": ("arm64", "aarch64"),
            "aarch64": ("arm64", "aarch64"),
        }.get(platform.machine().lower())
        if arch:
            package, target = arch
            modules = shim.parent / "node_modules"
            codex = modules / "@openai/codex"
            platform_pkg = "@openai/codex-win32-" + package
            for root in (codex / "node_modules" / platform_pkg, modules / platform_pkg, codex):
                vendor = root / "vendor" / (target + "-pc-windows-msvc")
                for candidate in (vendor / "bin/codex.exe", vendor / "codex/codex.exe"):
                    if candidate.is_file():
                        return str(candidate.resolve())
    return None


def _search_path(path_env=None, extra_dirs=None):
    base = os.environ.get("PATH", "") if path_env is None else path_env
    parts = [p for p in base.split(os.pathsep) if p]
    if extra_dirs is None and path_env is None and os.name == "nt":
        extra_dirs = _common_windows_dirs()
    for d in (extra_dirs or ()):
        s = str(d)
        if s and s not in parts:
            parts.append(s)
    return os.pathsep.join(parts)


def find_executable(name, path_env=None, extra_dirs=None):
    """Locate a native executable for `name`, avoiding .ps1 wrappers."""
    search = _search_path(path_env=path_env, extra_dirs=extra_dirs)
    if os.name == "nt":
        found = shutil.which(name + ".exe", path=search)
        if found and not _is_ps1(found):
            return str(Path(found).resolve())
        for ext in (".cmd", ".bat"):
            shim = shutil.which(name + ext, path=search)
            if shim:
                native = _unwrap_windows_shim(name, shim)
                if native:
                    return native
        return None
    found = shutil.which(name, path=search)
    if found and not _is_ps1(found):
        return str(Path(found).resolve())
    return None


def _minimal_env():
    keys = (
        "SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "HOME", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PATHEXT", "COMSPEC",
    )
    return {k: os.environ[k] for k in keys if k in os.environ}


def _probe(executable, args, timeout=5):
    if not executable or _is_ps1(executable):
        return None
    try:
        proc = subprocess.run(
            [executable, *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_minimal_env(),
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode or not output:
        return None
    return output


def probe_version(executable, version_args=None, timeout=5):
    output = _probe(executable, version_args or ["--version"], timeout)
    if not output:
        return None
    return output.splitlines()[0].strip()[:120]


def discover_cli(name, path_env=None, extra_dirs=None, check_version=True):
    if name not in CLI_SPECS:
        raise LaneError("Unsupported CLI: " + str(name))
    spec = CLI_SPECS[name]
    exe = find_executable(name, path_env=path_env, extra_dirs=extra_dirs)
    version = probe_version(exe, spec["version_args"]) if (exe and check_version) else None
    help_text = _probe(exe, ["--help"]) if exe and check_version else None
    tokens = help_text.replace(",", " ").split() if help_text else []
    return {
        "cli": name,
        "available": exe is not None,
        "executable": exe,
        "version": version,
        "headless_supported": spec["headless_args"][0] in tokens if help_text else None,
        "headless_args": list(spec["headless_args"]),
        "supports_resume": any(flag in tokens for flag in ("resume", "--resume")),
    }


def discover_all(clis=None, path_env=None, extra_dirs=None, check_version=True):
    targets = list(SUPPORTED_CLIS if clis is None else clis)
    return [
        discover_cli(name, path_env=path_env, extra_dirs=extra_dirs, check_version=check_version)
        for name in targets
    ]


def build_default_lanes(discovered):
    """Generate a named lanes config from discovery results."""
    lanes = {}
    available = [item for item in discovered if item.get("available") and item.get("executable")
                 and item.get("headless_supported") is not False]
    by_cli = {item["cli"]: item for item in available}

    if "codex" in by_cli:
        item = by_cli["codex"]
        lanes["codex-default"] = {
            "cli": "codex",
            "executable": item["executable"],
            "default_args": list(item.get("headless_args") or ["exec"]),
            "allowed_roles": ["executor"],
            "supports_resume": bool(item.get("supports_resume", True)),
        }
    if "claude" in by_cli:
        item = by_cli["claude"]
        lanes["claude-flash"] = {
            "cli": "claude",
            "executable": item["executable"],
            "default_args": list(item.get("headless_args") or ["--print"]),
            "allowed_roles": ["executor"],
            "supports_resume": bool(item.get("supports_resume", True)),
        }
    for item in available:
        cli = item["cli"]
        if cli in ("codex", "claude"):
            continue
        lanes[cli + "-default"] = {
            "cli": cli,
            "executable": item["executable"],
            "default_args": list(item.get("headless_args") or CLI_SPECS[cli]["headless_args"]),
            "allowed_roles": ["executor"],
            "supports_resume": bool(item.get("supports_resume", False)),
        }

    if available:
        reviewer_item = by_cli.get("claude") if "codex" in by_cli and "claude" in by_cli else available[-1]
        lanes["reviewer-independent"] = {
            "cli": reviewer_item["cli"],
            "executable": reviewer_item["executable"],
            "default_args": list(reviewer_item.get("headless_args") or CLI_SPECS[reviewer_item["cli"]]["headless_args"]),
            "allowed_roles": ["reviewer"],
            "supports_resume": bool(reviewer_item.get("supports_resume", False)),
        }

    config = {
        "schema_version": SCHEMA_VERSION,
        "authorization_note": AUTHORIZATION_NOTE,
        "lanes": lanes,
    }
    validate_lanes_config(config)
    return config


def validate_lanes_config(config):
    if not isinstance(config, dict):
        raise LaneError("Lanes config must be a JSON object.")
    version = config.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise LaneError("Unsupported lanes schema_version: " + str(version))
    lanes = config.get("lanes")
    if not isinstance(lanes, dict):
        raise LaneError("Lanes config must contain a 'lanes' object.")
    for name, entry in lanes.items():
        if not isinstance(name, str) or not name.strip():
            raise LaneError("Lane name must be a non-empty string.")
        if not isinstance(entry, dict):
            raise LaneError("Lane '%s' must be an object." % name)
        cli = entry.get("cli")
        if not isinstance(cli, str) or cli not in CLI_SPECS:
            raise LaneError("Lane '%s' has unsupported cli: %s" % (name, cli))
        exe = entry.get("executable")
        if not isinstance(exe, str) or not exe.strip():
            raise LaneError("Lane '%s' requires a non-empty executable path." % name)
        if _is_ps1(exe):
            raise LaneError("Lane '%s' must not use a .ps1 wrapper: %s" % (name, exe))
        default_args = entry.get("default_args", [])
        if not isinstance(default_args, list) or not all(isinstance(a, str) for a in default_args):
            raise LaneError("Lane '%s' default_args must be a list of strings." % name)
        roles = entry.get("allowed_roles")
        if (
            not isinstance(roles, list)
            or not roles
            or not all(isinstance(r, str) and r in VALID_ROLES for r in roles)
        ):
            raise LaneError("Lane '%s' allowed_roles must be a non-empty subset of %s." % (name, VALID_ROLES))
    return config


def load_lanes(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_lanes_config(data)


def save_lanes(path, config, overwrite=False):
    validate_lanes_config(config)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if overwrite else "x"
    with target.open(mode, encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=True)
        handle.write("\n")
    return target.resolve()


def resolve_lane(config, lane_name, role="executor", peer_lane=None):
    """Resolve a named lane for `role`, enforcing config != authorization and executor != reviewer."""
    validate_lanes_config(config)
    if role not in VALID_ROLES:
        raise LaneError("Invalid role: " + str(role))
    lanes = config["lanes"]
    if lane_name not in lanes:
        raise LaneError("Unknown lane: " + str(lane_name))
    if peer_lane is not None and peer_lane == lane_name:
        raise LaneError("Executor and reviewer lanes must be distinct; self-review is forbidden.")
    entry = lanes[lane_name]
    if role not in entry["allowed_roles"]:
        raise LaneError("Lane '%s' is not allowed for role '%s'." % (lane_name, role))
    return {
        "lane": lane_name,
        "role": role,
        "cli": entry["cli"],
        "executable": entry["executable"],
        "default_args": list(entry.get("default_args", [])),
        "allowed_roles": list(entry["allowed_roles"]),
        "supports_resume": bool(entry.get("supports_resume", False)),
        "authorized": False,
    }


def native_bridge(config, name, role, peer=None):
    from native_events import CLAUDE_DS_MODEL
    lane = resolve_lane(config, name, role, peer)
    cli = lane["cli"]
    if cli not in ("codex", "claude"):
        raise LaneError("Verified project dispatch supports Codex and Claude")
    parser = LaneArguments(prog="lane " + name, allow_abbrev=False, add_help=False)
    args = lane["default_args"][:]
    prefix = CLI_SPECS[cli]["headless_args"]
    if args[:1] == prefix:
        args = args[1:]
    bridge = {"harness": cli, "executable": lane["executable"]}
    if cli == "claude":
        parser.add_argument("--model", default=CLAUDE_DS_MODEL)
        parser.add_argument("--max-turns", type=int, dest="max_tool_turns", default=argparse.SUPPRESS)
    elif role == "executor":
        parser.add_argument("--session-mode", choices=("one-shot", "session"), default=argparse.SUPPRESS)
    bridge.update(vars(parser.parse_args(args)))
    return bridge


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="awo_project.py discover-cli",
        description="Read-only local CLI discovery and named lanes management.",
    )
    parser.add_argument(
        "--clis",
        help="Comma-separated CLIs to scan (default: %s)" % ",".join(SUPPORTED_CLIS),
    )
    parser.add_argument(
        "--no-version",
        action="store_true",
        help="Skip invoking --version on discovered executables.",
    )
    parser.add_argument(
        "--write-lanes",
        type=Path,
        help="Write generated lanes.json to this path.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow overwriting an existing file with --write-lanes.",
    )
    parser.add_argument(
        "--validate-lanes",
        type=Path,
        help="Validate an existing lanes.json file without scanning.",
    )
    args = parser.parse_args(argv)
    try:
        if args.validate_lanes:
            config = load_lanes(args.validate_lanes)
            print(json.dumps({"valid": True, "lanes": sorted(config["lanes"]), "authorized": False}, indent=2))
            return 0
        clis = [c.strip() for c in args.clis.split(",") if c.strip()] if args.clis else None
        discovered = discover_all(clis=clis, check_version=not args.no_version)
        config = build_default_lanes(discovered)
        output = {
            "discovered": discovered,
            "lanes_config": config,
            "authorized": False,
        }
        if args.write_lanes:
            written = save_lanes(args.write_lanes, config, overwrite=args.force)
            output["written_path"] = str(written)
        print(json.dumps(output, indent=2, ensure_ascii=True))
        return 0
    except (LaneError, OSError, ValueError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
