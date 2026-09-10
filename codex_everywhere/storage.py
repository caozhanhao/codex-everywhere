"""Small destination write primitives, independent of transport and UI."""

import datetime
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

# All application-owned persistent files under CODEX_HOME live here.
from .reader import STATE_DIRECTORY as STATE_DIRECTORY
from .reader import SyncError


def run_label() -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-")
        + uuid.uuid4().hex[:12]
    )


def atomic_copy(source: Path, dest: Path, expected_sha: str | None = None) -> None:
    """Verify the copied bytes before replacing one file, then fsync its directory."""
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".codex-everywhere-", dir=dest.parent)
    try:
        sha = hashlib.sha256()
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                sha.update(chunk)
                output.write(chunk)
            if expected_sha is not None and sha.hexdigest() != expected_sha:
                raise SyncError(f"File changed while copying: {source}")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, dest)
        sync_directory(dest.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, value: object) -> None:
    """Atomically record the plan/status so interrupted operations remain reviewable."""
    fd, temporary = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, ensure_ascii=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
