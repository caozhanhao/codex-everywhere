"""Destination storage checks and cooperation with Codex's writer locks."""

import contextlib
import fcntl
import os
import re
import sys
from pathlib import Path

from .reader import SyncError, assert_idle
from .storage import STATE_DIRECTORY


@contextlib.contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError(f"Lock is busy: {path}") from None
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def stopped_writers(home: Path):
    # Codex 0.153.4 serializes writer-lock creation/removal with this lock.
    root = home / "thread-writer-locks"
    with file_lock(root / ".coordination.lock"), contextlib.ExitStack() as stack:
        for path in sorted(root.glob("*.lock")):
            if path.name != ".coordination.lock":
                stack.enter_context(file_lock(path))
        assert_idle(home)
        yield


def require_local(path: Path) -> None:
    if sys.platform != "linux":
        raise SyncError("Import currently requires Linux and a verified local filesystem.")
    mountinfo = Path("/proc/self/mountinfo")
    if not mountinfo.exists():
        raise SyncError(f"Cannot verify local filesystem for {path}")
    resolved = path.resolve()
    best = (0, "")
    for line in mountinfo.read_text().splitlines():
        left, right = line.split(" - ", 1)
        raw = left.split()[4]
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), raw))
        if resolved == mount or mount in resolved.parents:
            if len(str(mount)) >= best[0]:
                best = (len(str(mount)), right.split()[0])
    if best[1] not in ("ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs", "overlay", "f2fs", "zfs"):
        raise SyncError(f"Import destination must be local, not {best[1]}: {path}")


def safe_destination(home: Path, path: Path) -> None:
    try:
        relative = path.relative_to(home)
    except ValueError:
        raise SyncError(f"Destination escapes CODEX_HOME: {path}") from None
    current = home
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SyncError(f"Destination contains a symlink: {current}")


def check_storage(home: Path, sqlite_home: Path | None) -> None:
    if not home.is_dir():
        raise SyncError(f"Target CODEX_HOME must already exist: {home}")
    if sqlite_home and sqlite_home.exists() and not sqlite_home.is_dir():
        raise SyncError(f"SQLite home is not a directory: {sqlite_home}")
    require_local(home)
    for folder in ("sessions", "archived_sessions", STATE_DIRECTORY, "thread-writer-locks"):
        safe_destination(home, home / folder)
        require_local(home / folder)
    database_home = sqlite_home or home
    require_local(database_home)
    for database in database_home.glob("*.sqlite*"):
        safe_destination(database_home, database)
        require_local(database)
    if sqlite_home:
        require_local(sqlite_home)
    else:
        config = home / "config.toml"
        if config.is_file() and re.search(r"(?m)^\s*sqlite_home\s*=", config.read_text()):
            raise SyncError(
                "config.toml sets sqlite_home. Pass --sqlite-home explicitly so the "
                "script can verify and use the correct local database directory."
            )
