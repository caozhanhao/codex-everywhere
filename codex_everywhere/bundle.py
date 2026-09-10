"""Unpack untrusted transfers into private staging, then validate every byte."""

import dataclasses
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from .reader import (
    MAX_BUNDLE,
    MAX_FILE,
    VERSION,
    SyncError,
    collect,
    inspect_session,
    validate_locations,
)


def unpack_bundle(bundle: Path, directory: Path):
    with zipfile.ZipFile(bundle) as z:
        names = z.namelist()
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise SyncError("Duplicate ZIP members or missing manifest.")
        if z.getinfo("manifest.json").file_size > 16 * 1024**2:
            raise SyncError("Manifest is too large.")
        manifest = json.loads(z.read("manifest.json"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != VERSION
            or not isinstance(manifest.get("sessions"), list)
        ):
            raise SyncError("Unsupported bundle format.")
        expected = {"manifest.json"}
        total = 0
        for row in manifest["sessions"]:
            if not isinstance(row, dict):
                raise SyncError("Invalid session manifest entry.")
            rel = PurePosixPath(row.get("relative", ""))
            if (
                rel.is_absolute()
                or ".." in rel.parts
                or len(rel.parts) < 2
                or rel.parts[0] not in ("sessions", "archived_sessions")
                or "\\" in str(rel)
                or rel.suffix != ".jsonl"
            ):
                raise SyncError(f"Unsafe archive path: {rel}")
            name = str(rel)
            if name != row.get("relative"):
                raise SyncError(f"Non-canonical archive path: {name}")
            if name in expected:
                raise SyncError(f"Duplicate manifest path: {name}")
            expected.add(name)
            info = z.getinfo(name)
            total += info.file_size
            if info.file_size > MAX_FILE or total > MAX_BUNDLE:
                raise SyncError("Bundle exceeds size limits.")
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as source, path.open("xb") as dest:
                shutil.copyfileobj(source, dest, 1024 * 1024)
            actual = dataclasses.asdict(inspect_session(path, name))
            if actual != row:
                raise SyncError(f"Session content does not match manifest: {name}")
        if set(names) != expected:
            raise SyncError("Unexpected members in bundle.")
    sessions, order = collect(directory)
    if len(sessions) != len(manifest["sessions"]):
        raise SyncError("Duplicate session IDs in manifest.")
    locations = validate_locations(manifest.get("locations", {}))
    for thread_id, row in locations.items():
        if thread_id not in sessions or row["original_cwd"] != sessions[thread_id].cwd:
            raise SyncError(f"Session location does not match bundled metadata: {thread_id}")
    return sessions, order, locations
