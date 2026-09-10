"""Hand an already restored terminal to native Codex on this machine.

Requests contain identity and a local directory, never a shell command. The UI
finishes all transfers and closes their resources before calling handoff.
Interactive Codex uses the user's normal configuration and inherited environment;
the restrictions of the offline reconstruction adapter do not apply here.
"""

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .codex import DirectoryUnavailable, map_cwd
from .config import Target, mapped_directory
from .reader import SyncError, canonical_id, read_locations, saved_cwd
from .safety import check_storage


@dataclass(frozen=True)
class LaunchRequest:
    directory: Path
    session_id: str | None = None


def validate(target: Target, request: LaunchRequest) -> None:
    check_storage(target.home, target.sqlite_home)
    if not request.directory.is_absolute() or not request.directory.is_dir():
        raise DirectoryUnavailable(str(request.directory))
    if request.session_id is not None:
        canonical_id(request.session_id)
    if not shutil.which(target.codex):
        raise SyncError(f"Codex executable not found: {target.codex}. Use --codex.")


def new_session(target: Target, directory: Path) -> LaunchRequest:
    directory = Path(map_cwd(str(directory), (), target.cwd))
    request = LaunchRequest(directory)
    validate(target, request)
    return request


def session_directory(target: Target, session_id: str, directory: str) -> str:
    """Return the preferred local path without resolving a foreign filesystem."""
    if target.cwd:
        return str(Path(target.cwd).expanduser().absolute())
    local = saved_cwd(read_locations(target.home), session_id, directory)
    return local or mapped_directory(directory, target.mappings)


def resume_session(
    target: Target, session_id: str, directory: str, *, archived: bool = False
) -> LaunchRequest:
    if archived:
        raise SyncError(f"Local session is archived. Run codex unarchive {session_id} first.")
    directory = Path(map_cwd(session_directory(target, session_id, directory), ()))
    request = LaunchRequest(directory, session_id)
    validate(target, request)
    return request


def arguments(target: Target, request: LaunchRequest) -> list[str]:
    # A TOML setting takes precedence over CODEX_SQLITE_HOME in Codex 0.153.4.
    # Explicitly use the same checked directory as the import/rebuild target.
    args = [
        target.codex,
        "-c",
        "sqlite_home=" + json.dumps(str(target.sqlite_home or target.home)),
        "--cd",
        str(request.directory),
    ]
    if request.session_id is not None:
        args += ["resume", request.session_id]
    return args


def handoff(target: Target, request: LaunchRequest) -> None:
    """Replace the launcher; native Codex owns the TTY, signals and exit status."""
    validate(target, request)
    env = {
        **os.environ,
        "CODEX_HOME": str(target.home),
        "CODEX_SQLITE_HOME": str(target.sqlite_home or target.home),
    }
    os.execvpe(target.codex, arguments(target, request), env)
