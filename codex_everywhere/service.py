"""Prepare -> inspect -> apply: the only orchestration that changes a Codex home.

A prepared transfer owns a private snapshot, not a pointer back into a source
home. Applying rechecks that snapshot and the entire destination plan under
locks, backs up every replaced file, then installs and indexes locally.
"""

import shutil
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import codex, transport
from .bundle import unpack_bundle
from .config import Node, Target, mapped_directory
from .plan import Action, Change, make_plan
from .reader import (
    LOCATIONS_FILE,
    Session,
    SyncError,
    assert_idle,
    collect,
    read_locations,
    saved_cwd,
    validate_locations,
)
from .safety import check_storage, file_lock, require_local, safe_destination, stopped_writers
from .storage import STATE_DIRECTORY, atomic_copy, run_label, write_json

Progress = Callable[[str], None]


def quiet(message: str) -> None:
    pass


@dataclass(frozen=True)
class Prepared:
    target: Target
    bundle: Path
    stage: Path
    sessions: dict[str, Session]
    order: list[str]
    changes: tuple[Change, ...]
    directories: dict[str, str]
    previous_locations: dict[str, dict[str, str]]
    locations: dict[str, dict[str, str]]

    @property
    def directory_updates(self) -> int:
        return sum(self.previous_locations.get(i) != self.locations.get(i) for i in self.order)

    @property
    def has_conflicts(self) -> bool:
        return any(change.action is Action.CONFLICT for change in self.changes)

    @property
    def needs_sync(self) -> bool:
        """Inspect the entire ancestor closure, including conflicts, before opening."""
        return bool(self.directory_updates) or any(
            change.action not in (Action.SAME, Action.LOCAL_NEWER) for change in self.changes
        )

    def summary(self) -> str:
        summary = ", ".join(
            f"{action.value}={sum(c.action is action for c in self.changes)}" for action in Action
        )
        return summary + (
            f", directories={self.directory_updates}" if self.directory_updates else ""
        )


def _prepare(bundle: Path, stage: Path, target: Target) -> Prepared:
    if not target.home.is_dir():
        raise SyncError(f"Target CODEX_HOME must already exist: {target.home}")
    check_storage(target.home, target.sqlite_home)
    sessions, order, source_locations = unpack_bundle(bundle, stage)
    changes = make_plan(target.home, stage, sessions, order)
    previous = read_locations(target.home)
    locations, directories = dict(previous), {}
    for change in changes:
        item = sessions[change.id]
        local = saved_cwd(previous, item.id, item.cwd) if change.action is not Action.ADD else None
        source = saved_cwd(source_locations, item.id, item.cwd) or item.cwd
        # Keep an existing local placement across updates and transfers back.
        # An explicit --cwd is the only override of a remembered directory.
        directory = (
            str(Path(target.cwd).expanduser().resolve())
            if target.cwd
            else local or mapped_directory(source, target.mappings)
        )
        directories[item.id] = directory
        if Path(directory).is_absolute() and directory != item.cwd:
            locations[item.id] = {"original_cwd": item.cwd, "cwd": directory}
        else:
            locations.pop(item.id, None)
    validate_locations(locations)
    return Prepared(
        target, bundle, stage, sessions, order, changes, directories, previous, locations
    )


@contextmanager
def prepare_file(bundle: Path, target: Target) -> Iterator[Prepared]:
    with tempfile.TemporaryDirectory(prefix="codex-everywhere-") as temporary:
        root = Path(temporary)
        snapshot = root / "incoming.zip"
        shutil.copyfile(bundle, snapshot)
        yield _prepare(snapshot, root / "stage", target)


@contextmanager
def prepare_pull(
    node: Node,
    target: Target,
    sessions: tuple[str, ...],
    *,
    timeout: float = 600,
    cancel: threading.Event | None = None,
    progress: Progress = quiet,
) -> Iterator[Prepared]:
    target = target.for_node(node)
    # Fail obvious destination errors before transferring potentially large histories.
    if not target.home.is_dir():
        raise SyncError(f"Target CODEX_HOME must already exist: {target.home}")
    check_storage(target.home, target.sqlite_home)
    with tempfile.TemporaryDirectory(prefix="codex-everywhere-") as temporary:
        root = Path(temporary)
        bundle = root / "incoming.zip"
        progress(f"Reading {node.name}; exporting selected sessions and ancestors…")
        with bundle.open("wb") as output:
            transport.receive(
                node, "export", output, sessions=sessions, timeout=timeout, cancel=cancel
            )
        progress("Validating complete history and comparing with this machine…")
        yield _prepare(bundle, root / "stage", target)


def preflight(prepared: Prepared, *, index: bool = True, progress: Progress = quiet) -> None:
    """Check the executable and project paths before any session is replaced."""
    if not index:
        return
    codex.check_version(prepared.target.codex, progress)
    for change in prepared.changes:
        if change.destination.is_relative_to(prepared.target.home / "archived_sessions"):
            raise SyncError(
                f"Local ancestor/session is archived: {change.id}. Run codex unarchive {change.id} first."
            )
        codex.map_cwd(prepared.directories[change.id], ())


def preserve_conflict(prepared: Prepared) -> Path:
    """Preserve the incoming bundle without modifying any session or database."""
    home = prepared.target.home
    check_storage(home, prepared.target.sqlite_home)
    with file_lock(home / STATE_DIRECTORY / ".sync.lock"):
        dest = home / STATE_DIRECTORY / "conflicts" / (run_label() + ".zip")
        safe_destination(home, dest)
        require_local(dest)
        atomic_copy(prepared.bundle, dest)
        return dest


def apply(prepared: Prepared, *, index: bool = True, progress: Progress = quiet) -> Path:
    target = prepared.target
    home = target.home
    check_storage(home, target.sqlite_home)
    assert_idle(home)
    if prepared.has_conflicts:
        saved = preserve_conflict(prepared)
        raise SyncError(f"Histories diverged. No sessions were modified. Incoming copy: {saved}")
    preflight(prepared, index=index, progress=progress)
    # Staging is private, but verify again in case of accidental changes or disk errors.
    sessions, order = collect(prepared.stage)
    if sessions != prepared.sessions or set(order) != set(prepared.order):
        raise SyncError("Staged history changed since preparation; no sessions modified.")
    with file_lock(home / STATE_DIRECTORY / ".sync.lock"):
        report = home / STATE_DIRECTORY / "backups" / run_label()
        safe_destination(home, report)
        require_local(report)
        with stopped_writers(home):
            if (
                make_plan(home, prepared.stage, prepared.sessions, prepared.order)
                != prepared.changes
                or read_locations(home) != prepared.previous_locations
            ):
                raise SyncError(
                    "Target changed since preview; prepare again. No sessions modified."
                )
            for change in prepared.changes:
                require_local(change.destination)
            locations_path = home / LOCATIONS_FILE
            safe_destination(home, locations_path)
            require_local(locations_path)
            report.mkdir(parents=True, mode=0o700)
            write_json(report / "plan.json", [change.to_dict() for change in prepared.changes])
            if prepared.directory_updates:
                write_json(report / "directories.json", prepared.directories)
            write_json(report / "status.json", {"phase": "backing-up"})
            try:
                for change in prepared.changes:
                    if change.action is Action.UPDATE:
                        atomic_copy(
                            change.destination,
                            report / "original" / change.destination.relative_to(home),
                            change.old_sha,
                        )
                if prepared.directory_updates and locations_path.exists():
                    atomic_copy(locations_path, report / "original" / LOCATIONS_FILE)
                write_json(report / "status.json", {"phase": "installing"})
                for change in prepared.changes:
                    if change.action in (Action.ADD, Action.UPDATE):
                        atomic_copy(
                            prepared.stage / prepared.sessions[change.id].relative,
                            change.destination,
                            change.incoming_sha,
                        )
                if prepared.directory_updates:
                    write_json(locations_path, prepared.locations)
                write_json(report / "status.json", {"phase": "files-installed"})
            except BaseException:
                progress(f"Import interrupted; backups and plan: {report}")
                raise
        progress(f"Session files installed; backup and plan: {report}")
        if index:
            try:
                codex.rebuild(
                    home,
                    prepared.order,
                    target.codex,
                    target.mappings,
                    target.cwd,
                    report,
                    target.sqlite_home,
                    progress,
                )
            except BaseException:
                write_json(report / "status.json", {"phase": "index-incomplete"})
                progress(f"Files retained. Correct the error and run rebuild; report: {report}")
                raise
        write_json(report / "status.json", {"phase": "complete" if index else "index-skipped"})
        return report


def rebuild(target: Target, sessions: list[str] | None, progress: Progress = quiet) -> Path:
    check_storage(target.home, target.sqlite_home)
    assert_idle(target.home)
    codex.check_version(target.codex, progress)
    with file_lock(target.home / STATE_DIRECTORY / ".sync.lock"):
        report = target.home / STATE_DIRECTORY / "rebuilds" / run_label()
        safe_destination(target.home, report)
        require_local(report)
        report.mkdir(parents=True, mode=0o700)
        codex.rebuild(
            target.home,
            sessions,
            target.codex,
            target.mappings,
            target.cwd,
            report,
            target.sqlite_home,
            progress,
        )
        return report
