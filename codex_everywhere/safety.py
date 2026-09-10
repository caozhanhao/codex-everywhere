"""Destination storage checks and cooperation with Codex's writer locks."""

import contextlib
import ctypes
import errno
import fcntl
import os
import re
import sys
from pathlib import Path

from .reader import RolloutIndex, SyncError, canonical_id
from .storage import STATE_DIRECTORY


class _DarwinStatFS(ctypes.Structure):
    # Darwin's 64-bit-inode statfs ABI from <sys/mount.h>, on Intel and Apple Silicon.
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


_MNT_RDONLY = 0x00000001
_MNT_LOCAL = 0x00001000


@contextlib.contextmanager
def file_lock(path: Path, *, busy: str | None = None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError(
                busy or f"Local machine: Lock is busy: {path}. Retry shortly."
            ) from None
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def maintenance_lock(home: Path):
    """Exclude native rollout compression and migration throughout a local write."""
    path = home / ".tmp" / "rollout-maintenance.lock"
    safe_destination(home, path)
    require_local(path)
    with file_lock(
        path,
        busy="Local machine: Codex is maintaining history files. Wait for it to finish, then retry.",
    ):
        yield


def _dependency_threads(home: Path, selected: set[str]) -> set[str]:
    # Discover just metadata before locking; complete validation happens under locks.
    index = RolloutIndex(home)
    pending = [index.head(i) for i in sorted(selected & index.threads.keys())]
    threads, seen = set(selected), set()
    while pending:
        rollout_id = pending.pop()
        if rollout_id in seen:
            continue
        seen.add(rollout_id)
        meta = index.metadata(rollout_id)
        threads.add(canonical_id(meta["id"]))
        if meta.get("history_base"):
            pending.append(canonical_id(meta["history_base"]["thread_id"]))
    return threads


@contextlib.contextmanager
def session_locks(home: Path, selected):
    """Protect selected threads and their local ancestors, allowing unrelated writers.

    Codex coordinates lock-file creation and stale-file cleanup. Release that
    coordination lock immediately after acquisition so other sessions can open.
    Leave unlocked files for Codex's coordinated stale-lock cleanup.
    """
    selected = {canonical_id(i) for i in selected}
    threads = _dependency_threads(home, selected)
    root = home / "thread-writer-locks"
    safe_destination(home, root)
    require_local(root)
    with contextlib.ExitStack() as stack:
        coordination = root / ".coordination.lock"
        safe_destination(home, coordination)
        with file_lock(
            coordination,
            busy="Local machine: Codex is updating session locks. Retry shortly.",
        ):
            for thread_id in sorted(threads):
                path = root / f"{thread_id}.lock"
                safe_destination(home, path)
                require_local(path)
                stack.enter_context(
                    file_lock(
                        path,
                        busy=f"Local machine: Session {thread_id} is in use. "
                        "Close that session in Codex, then retry.",
                    )
                )
        if not _dependency_threads(home, selected) <= threads:
            raise SyncError("Local machine: Session dependencies changed. Retry the transfer.")
        yield


def _darwin_filesystem(path: Path) -> tuple[str, int]:
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        # Intel exports both ABIs; Apple Silicon's unsuffixed symbol is already 64-bit.
        try:
            statfs = getattr(libc, "statfs$INODE64")
        except AttributeError:
            statfs = libc.statfs
        statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_DarwinStatFS)]
        statfs.restype = ctypes.c_int
        info = _DarwinStatFS()
        current = path.resolve()
        while True:
            if statfs(os.fsencode(current), ctypes.byref(info)) == 0:
                return info.f_fstypename.decode("ascii"), info.f_flags
            error = ctypes.get_errno()
            # Sessions, backup directories and an explicit SQLite home may not exist yet.
            # Only ENOENT permits checking the parent; other failures stay unverified.
            if error != errno.ENOENT or current == current.parent:
                raise OSError(error, os.strerror(error))
            current = current.parent
    except (OSError, AttributeError, UnicodeError) as exc:
        raise SyncError(f"Cannot verify local filesystem for {path}: {exc}") from None


def require_local(path: Path) -> None:
    if sys.platform == "darwin":
        filesystem, flags = _darwin_filesystem(path)
        if not flags & _MNT_LOCAL or filesystem not in ("apfs", "hfs"):
            raise SyncError(
                f"Import destination must be local APFS or HFS+, not {filesystem or 'unknown'}: {path}"
            )
        read_only = bool(flags & _MNT_RDONLY)
    elif sys.platform == "linux":
        mountinfo = Path("/proc/self/mountinfo")
        if not mountinfo.exists():
            raise SyncError(f"Cannot verify local filesystem for {path}")
        resolved = path.resolve()
        best = (0, "", False)
        for line in mountinfo.read_text().splitlines():
            left, right = line.split(" - ", 1)
            fields, details = left.split(), right.split()
            mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
            if resolved == mount or mount in resolved.parents:
                if len(str(mount)) >= best[0]:
                    # Bind mounts can be read-only even on a writable filesystem.
                    options = fields[5].split(",") + details[2].split(",")
                    best = (len(str(mount)), details[0], "ro" in options)
        if best[1] not in (
            "ext2",
            "ext3",
            "ext4",
            "xfs",
            "btrfs",
            "tmpfs",
            "overlay",
            "f2fs",
            "zfs",
        ):
            raise SyncError(f"Import destination must be local, not {best[1]}: {path}")
        read_only = best[2]
    else:
        raise SyncError("Import requires Linux or macOS and a verified local filesystem.")
    if read_only:
        raise SyncError(
            f"Local destination is on a read-only filesystem: {path}. "
            "Set home or sqlite_home to a writable local directory (--home / --sqlite-home)."
        )


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
    for folder in ("sessions", "archived_sessions", STATE_DIRECTORY, "thread-writer-locks", ".tmp"):
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
