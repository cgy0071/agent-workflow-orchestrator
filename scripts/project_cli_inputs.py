"""Local input checks shared by the project read commands."""
from pathlib import Path

from mcp_gateway import safe_error


class ProjectInputError(ValueError):
    pass


def check_paths(state, root):
    if not Path(root).is_absolute() or not Path(root).is_dir():
        raise ProjectInputError("--project-root must be an absolute path to an existing directory")
    if not (Path(state) / "workflow.sqlite3").is_file():
        raise ProjectInputError("--state must contain an initialized workflow.sqlite3")


def diagnostic(exc):
    if isinstance(exc, ProjectInputError):
        return str(exc)
    error = safe_error(exc)
    return f"{error.code}: {error}"
