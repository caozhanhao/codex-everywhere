"""Read-only source worker, also sent verbatim to remote Python over SSH.

This module deliberately has no project imports and no filesystem write API.
Only export_bundle's caller-supplied output stream is writable. Keep changes to
this boundary small: it is the entire program executed on a source machine.
"""

import argparse
import dataclasses
import datetime
import hashlib
import json
import os
import re
import stat
import sys
import uuid
import zipfile
from pathlib import Path
from typing import BinaryIO

# The standalone SSH worker does not import the package's version check.
if sys.version_info < (3, 10):  # noqa: UP036 -- Explain unsupported remote interpreters.
    raise SystemExit(
        "codex-everywhere requires Python 3.10+; "
        f"running {'.'.join(map(str, sys.version_info[:3]))} ({sys.executable}). "
        "Ensure python3 on the source machine resolves to Python 3.10+."
    )

VERSION = 1
MAX_FILE = 2 * 1024**3
MAX_BUNDLE = 20 * 1024**3
STATE_DIRECTORY = ".codex-everywhere"
LOCATIONS_FILE = STATE_DIRECTORY + "/locations.json"
MAX_LOCATIONS = 16 * 1024**2
UUID_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
ROLLOUT_RE = re.compile(
    rf"rollout-\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}}-"
    rf"({UUID_RE.pattern})(?:_({UUID_RE.pattern}))?\.jsonl"
)


class SyncError(Exception):
    pass


def canonical_id(value: str) -> str:
    if not isinstance(value, str):
        raise SyncError(f"Invalid session ID: {value!r}")
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise SyncError(f"Invalid session ID: {value!r}") from None


def validate_locations(value: object) -> dict[str, dict[str, str]]:
    """Directory hints are separate from immutable session metadata and history."""
    if not isinstance(value, dict):
        raise SyncError("Invalid session locations: expected an object.")
    for thread_id, row in value.items():
        if (
            canonical_id(thread_id) != thread_id
            or not isinstance(row, dict)
            or set(row) != {"original_cwd", "cwd"}
            or not all(isinstance(v, str) and "\0" not in v for v in row.values())
            or not Path(row["cwd"]).is_absolute()
        ):
            raise SyncError(f"Invalid session location: {thread_id}")
    if len(json.dumps(value, ensure_ascii=True, indent=2).encode()) + 1 > MAX_LOCATIONS:
        raise SyncError("Session locations file is too large.")
    return value


def read_locations(home: Path) -> dict[str, dict[str, str]]:
    path = home / LOCATIONS_FILE
    if path.parent.is_symlink() or path.is_symlink():
        raise SyncError(f"Refusing symlink in session locations path: {path}")
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise SyncError(f"Expected a regular session locations file: {path}")
        with path.open("rb") as source:
            data = source.read(MAX_LOCATIONS + 1)
    except FileNotFoundError:
        return {}
    if len(data) > MAX_LOCATIONS:
        raise SyncError("Session locations file is too large.")
    try:
        return validate_locations(json.loads(data))
    except (UnicodeError, ValueError) as exc:
        raise SyncError(f"Invalid session locations file: {path}: {exc}") from None


def saved_cwd(locations: dict, thread_id: str, original_cwd: str) -> str | None:
    row = locations.get(thread_id)
    return row["cwd"] if row and row["original_cwd"] == original_cwd else None


def signature(path: Path) -> tuple[int, int, int, int, int]:
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


@dataclasses.dataclass(frozen=True)
class Session:
    id: str
    relative: str
    sha256: str
    size: int
    records: int
    history_mode: str
    history_base: dict | None
    first_ordinal: int | None
    cwd: str

    @property
    def rollout_id(self) -> str:
        return rollout_ids(Path(self.relative))[1]


def rollout_ids(path: Path) -> tuple[str, str]:
    """Codex 0.154.0 rollout_file_name.rs: stable thread ID, optional rollout ID."""
    match = ROLLOUT_RE.fullmatch(path.name)
    if not match:
        raise SyncError(f"Unrecognized rollout filename: {path}")
    return match[1], match[2] or match[1]


def history_head(thread_id: str, bases: dict[str, str | None]) -> str:
    """Choose a unique connected tip by references, never by timestamps or size."""
    tips = set(bases) - set(bases.values())
    if len(tips) == 1:
        tip = next(iter(tips))
        seen = set()
        current = tip
        while current in bases and current not in seen:
            seen.add(current)
            current = bases[current]
        if seen == set(bases) and current not in seen:
            return tip
    raise SyncError(
        f"Ambiguous history for session {thread_id}: multiple branches or a cycle. "
        "Cannot choose a current rollout from history references. Other sessions can still transfer."
    )


def session_heads(sessions: dict[str, Session]) -> dict[str, Session]:
    groups = {}
    for rollout_id, item in sessions.items():
        groups.setdefault(item.id, {})[rollout_id] = (
            canonical_id(item.history_base.get("thread_id")) if item.history_base else None
        )
    return {
        thread_id: sessions[history_head(thread_id, bases)] for thread_id, bases in groups.items()
    }


def inspect_session(path: Path, relative: str) -> Session:
    before = signature(path)
    if before[2] > MAX_FILE:
        raise SyncError(f"Session exceeds 2 GiB: {path}")
    sha = hashlib.sha256()
    meta = None
    count = 0
    first = previous = None
    try:
        with path.open("rb") as f:
            for line_no, line in enumerate(f, 1):
                sha.update(line)
                if not line.endswith(b"\n"):
                    raise SyncError(f"Incomplete final record: {path}:{line_no}")
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise SyncError(f"Expected JSON object: {path}:{line_no}")
                if meta is None:
                    if record.get("type") != "session_meta":
                        raise SyncError(f"Missing session_meta: {path}")
                    meta = record.get("payload")
                    if not isinstance(meta, dict):
                        raise SyncError(f"Invalid session metadata: {path}")
                    canonical_id(meta.get("id"))
                    if meta.get("history_mode", "legacy") not in ("legacy", "paginated"):
                        raise SyncError(f"Unsupported history mode: {path}")
                if meta.get("history_mode") == "paginated":
                    ordinal = record.get("ordinal")
                    if type(ordinal) is not int or ordinal < 0:
                        raise SyncError(f"Invalid ordinal: {path}:{line_no}")
                    if previous is not None and ordinal != previous + 1:
                        raise SyncError(f"Discontinuous ordinal: {path}:{line_no}")
                    if first is None:
                        first = ordinal
                    previous = ordinal
                count += 1
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SyncError(f"Invalid JSONL {path}: {exc}") from None
    if meta is None:
        raise SyncError(f"Empty session: {path}")
    if meta.get("history_base") is not None and not isinstance(meta["history_base"], dict):
        raise SyncError(f"Invalid history_base: {path}")
    if not isinstance(meta.get("cwd", ""), str):
        raise SyncError(f"Invalid working directory: {path}")
    if signature(path) != before:
        raise SyncError(f"Session changed while reading; retry the transfer: {path}")
    return Session(
        canonical_id(meta["id"]),
        relative,
        sha.hexdigest(),
        before[2],
        count,
        meta.get("history_mode", "legacy"),
        meta.get("history_base"),
        first,
        meta.get("cwd", ""),
    )


class RolloutIndex:
    """Index filenames globally; resolve and validate only requested histories.

    One thread can own multiple immutable rollouts after revert. A history_base
    names an exact rollout, whereas a user selection names a thread's current tip.
    Source discovery stays independent of SQLite and never opens unrelated history.
    """

    def __init__(self, home: Path):
        self.home = home
        self.threads: dict[str, set[str]] = {}
        self.rollouts: dict[str, list[Path]] = {}
        self.issues: list[str] = []
        self._metadata: dict[str, dict] = {}
        for folder in ("sessions", "archived_sessions"):
            root = home / folder
            if root.is_symlink():
                self.issues.append(f"Session directory is a symlink: {root}")
                continue
            for directory, subdirs, files in os.walk(root, followlinks=False):
                subdirs[:] = sorted(
                    name for name in subdirs if not (Path(directory) / name).is_symlink()
                )
                for name in sorted(files):
                    if not name.endswith(".jsonl"):
                        continue
                    path = Path(directory) / name
                    try:
                        thread_id, rollout_id = rollout_ids(path)
                        self.threads.setdefault(thread_id, set()).add(rollout_id)
                        self.rollouts.setdefault(rollout_id, []).append(path)
                    except SyncError as exc:
                        self.issues.append(str(exc))

    def path(self, rollout_id: str) -> Path:
        candidates = self.rollouts.get(rollout_id, [])
        if not candidates:
            raise SyncError(f"Missing session or history rollout: {rollout_id}")
        if len(candidates) != 1:
            raise SyncError(
                f"Duplicate rollout ID {rollout_id}: multiple files identify the same history "
                "segment: " + ", ".join(str(path) for path in sorted(candidates))
            )
        path = candidates[0]
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise SyncError(f"Unsupported session file (not a regular file or a symlink): {path}")
        return path

    def metadata(self, rollout_id: str) -> dict:
        if rollout_id not in self._metadata:
            path = self.path(rollout_id)
            with path.open("rb") as stream:
                first = stream.readline(2 * 1024**2)
            try:
                if not first.endswith(b"\n"):
                    raise SyncError(f"Incomplete or oversized session metadata: {path}")
                row = json.loads(first)
                if row.get("type") != "session_meta" or not isinstance(row.get("payload"), dict):
                    raise SyncError(f"Missing session metadata: {path}")
                meta = row["payload"]
                thread_id = canonical_id(meta.get("id"))
                if thread_id != rollout_ids(path)[0]:
                    raise SyncError(f"Local filename and metadata ID disagree: {path}")
                base = meta.get("history_base")
                if base is not None:
                    if not isinstance(base, dict):
                        raise SyncError(f"Invalid history_base: {path}")
                    canonical_id(base.get("thread_id"))
                if rollout_id != thread_id and meta.get("history_mode") != "paginated":
                    raise SyncError(f"Separate rollout IDs require paginated history: {path}")
                self._metadata[rollout_id] = meta
            except (ValueError, AttributeError) as exc:
                raise SyncError(f"Invalid session metadata {path}: {exc}") from None
        return self._metadata[rollout_id]

    def head(self, thread_id: str) -> str:
        thread_id = canonical_id(thread_id)
        if thread_id not in self.threads:
            raise SyncError(f"Missing session or history ancestor: {thread_id}")
        bases = {}
        for rollout_id in sorted(self.threads[thread_id]):
            base = self.metadata(rollout_id).get("history_base")
            bases[rollout_id] = canonical_id(base.get("thread_id")) if base else None
        return history_head(thread_id, bases)

    def collect(self, selected: list[str] | tuple[str, ...] | None = None):
        if not selected and self.issues:
            raise SyncError(self.issues[0])
        sessions, order, visiting = {}, [], set()

        def visit(rollout_id):
            rollout_id = canonical_id(rollout_id)
            if rollout_id in visiting:
                raise SyncError(f"Cyclic history_base at rollout {rollout_id}")
            if rollout_id in sessions:
                return
            visiting.add(rollout_id)
            path = self.path(rollout_id)
            self.metadata(rollout_id)
            item = inspect_session(path, path.relative_to(self.home).as_posix())
            if item.id != rollout_ids(path)[0]:
                raise SyncError(f"Filename and metadata ID disagree: {path}")
            if item.history_base is not None:
                parent_id = canonical_id(item.history_base.get("thread_id"))
                visit(parent_id)
                validate_edge(item, sessions[parent_id], self.path(parent_id))
            visiting.remove(rollout_id)
            sessions[rollout_id] = item
            order.append(rollout_id)

        for thread_id in selected or sorted(self.threads):
            visit(self.head(thread_id))
        return sessions, order


def paths_by_id(home: Path, selected: list[str] | None = None) -> dict[str, Path]:
    index = RolloutIndex(home)
    if selected is None and index.issues:
        raise SyncError(index.issues[0])
    return {
        thread_id: index.path(index.head(thread_id))
        for thread_id in (sorted(index.threads) if selected is None else selected)
    }


def collect(
    home: Path, selected: list[str] | tuple[str, ...] | None = None
) -> tuple[dict[str, Session], list[str]]:
    """Collect complete files by rollout ID, with exact ancestors before their children."""
    return RolloutIndex(home).collect(selected)


def validate_edge(child: Session, parent: Session, parent_path: Path) -> None:
    base = child.history_base
    end = base.get("end_byte_offset")
    ordinal_end = base.get("end_ordinal_exclusive")
    if type(end) is not int or not 0 < end <= parent.size:
        raise SyncError(f"Invalid ancestor byte boundary: {child.id}")
    if type(ordinal_end) is not int or ordinal_end != child.first_ordinal:
        raise SyncError(f"Invalid ancestor ordinal boundary: {child.id}")
    offset = 0
    last = None
    with parent_path.open("rb") as f:
        for line in f:
            offset += len(line)
            if offset > end:
                break
            last = json.loads(line).get("ordinal")
            if offset == end:
                if type(last) is int and last + 1 == ordinal_end:
                    return
                break
    raise SyncError(f"Ancestor boundary is not a matching complete record: {child.id}")


def export_bundle(
    home: Path,
    output: BinaryIO,
    selected: list[str] | tuple[str, ...] | None = None,
    *,
    location: str = "Local machine",
) -> None:
    """Read a validated snapshot without requiring source writers to exit."""
    try:
        _export_snapshot(home, output, selected)
    except (SyncError, OSError) as exc:
        raise SyncError(f"{location}: {exc}") from None


def _export_snapshot(home: Path, output: BinaryIO, selected) -> None:
    if not home.is_dir():
        raise SyncError(f"Source CODEX_HOME does not exist: {home}")
    index = RolloutIndex(home)
    sessions, order = index.collect(selected)
    heads = session_heads(sessions)
    locations = read_locations(home)
    if sum(item.size for item in sessions.values()) > MAX_BUNDLE:
        raise SyncError("Bundle exceeds 20 GiB.")
    manifest = {
        "format": VERSION,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "roots": list(selected or heads),
        "sessions": [dataclasses.asdict(sessions[i]) for i in order],
        "locations": {
            i: locations[i]
            for i, item in heads.items()
            if saved_cwd(locations, i, item.cwd) is not None
        },
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for rollout_id in order:
            item = sessions[rollout_id]
            path = home / item.relative
            before = signature(path)
            sha = hashlib.sha256()
            with path.open("rb") as source, z.open(item.relative, "w", force_zip64=True) as dest:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    sha.update(chunk)
                    dest.write(chunk)
            if signature(path) != before or sha.hexdigest() != item.sha256:
                raise SyncError(f"Source changed during export: {path}")
        # Revert can publish a new head without changing any already copied file.
        current = RolloutIndex(home)
        roots = selected or sorted(index.threads)
        if any(current.head(i) != index.head(i) for i in roots) or any(
            current.path(i) != home / sessions[i].relative for i in order
        ):
            raise SyncError("Source history selection changed during export; retry the transfer.")
        current_locations = read_locations(home)
        if any(current_locations.get(i) != locations.get(i) for i in heads):
            raise SyncError("Session locations changed during export; retry the transfer.")
        # Publish the manifest only after all selected files and dependencies validate.
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))


# Catalog hints never authorize a transfer. Limits apply even to live or malformed
# files; export still validates the complete history and ancestry after selection.
CATALOG_HEAD_BYTES = 512 * 1024
CATALOG_HEAD_RECORDS = 210
CATALOG_TAIL_BYTES = 128 * 1024
CATALOG_INDEX_BYTES = 4 * 1024**2


def timestamp_ns(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        epoch = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
        delta = stamp - epoch
        result = ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds) * 1000
        return result if result >= 0 else None
    except (ValueError, OverflowError):
        return None


def catalog_rows(data: bytes):
    """Ignore incomplete, malformed or non-object records in a bounded sample."""
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                yield row
        except (ValueError, UnicodeError):
            continue


def message_preview(message: dict) -> str:
    text = message.get("message", "")
    content = message.get("content", [])
    if not isinstance(content, list):
        content = []
    if not isinstance(text, str) or not text:
        text = "".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") in ("text", "input_text")
            and isinstance(part.get("text"), str)
        )
    # Codex's user_message_preview removes this IDE context prefix too.
    marker = "## My request for Codex:"
    if marker in text:
        text = text.partition(marker)[2]
    text = " ".join(text.split())
    if text:
        return text[:240]
    kinds = {
        part.get("type")
        for part in content
        if isinstance(part, dict) and isinstance(part.get("type"), str)
    }
    if (
        message.get("images")
        or message.get("local_images")
        or kinds & {"image", "input_image", "local_image"}
    ):
        return "[Image]"
    if (
        message.get("audio")
        or message.get("local_audio")
        or kinds & {"audio", "input_audio", "local_audio"}
    ):
        return "[Audio]"
    return ""


def read_catalog_preview(stream: BinaryIO) -> str:
    """Prefer UI user events; raw response messages are a compatibility fallback."""
    remaining = CATALOG_HEAD_BYTES
    fallback = ""
    for _ in range(CATALOG_HEAD_RECORDS):
        line = stream.readline(remaining + 1)
        remaining -= len(line)
        if not line or remaining < 0:
            break
        for row in catalog_rows(line):
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if row.get("type") == "event_msg":
                message = None
                if payload.get("type") == "user_message":
                    message = payload
                elif payload.get("type") == "item_completed":
                    item = payload.get("item")
                    if isinstance(item, dict) and item.get("type") == "UserMessage":
                        message = item
                if message is not None and (preview := message_preview(message)):
                    return preview
            elif (
                not fallback
                and row.get("type") == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                preview = message_preview(payload)
                # Model-context records are not necessarily human messages. UI
                # events above take precedence; don't use known injected wrappers.
                if not preview.startswith(
                    (
                        "# AGENTS.md instructions for ",
                        "<environment_context>",
                        "<permissions instructions>",
                        "<user_instructions>",
                    )
                ):
                    fallback = preview
        if remaining == 0:
            break
    return fallback


def read_catalog_activity(stream: BinaryIO, size: int) -> int | None:
    start = max(0, size - CATALOG_TAIL_BYTES)
    stream.seek(start)
    data = stream.read(min(size, CATALOG_TAIL_BYTES))
    if start:
        data = data.partition(b"\n")[2]  # The first sampled line may be partial.
    for row in reversed(list(catalog_rows(data))):
        if row.get("type") in ("event_msg", "response_item", "compacted"):
            stamp = timestamp_ns(row.get("timestamp"))
            if stamp is not None:
                return stamp
    return None


def catalog_names(home: Path, issues: list[str]) -> dict[str, str]:
    """Native renames append hints for both legacy and paginated conversations."""
    path = home / "session_index.jsonl"
    try:
        if path.is_symlink():
            raise SyncError("Refusing symlink")
        size = path.stat().st_size
        if not stat.S_ISREG(path.stat().st_mode):
            raise SyncError("Expected a regular file")
        with path.open("rb") as stream:
            start = max(0, size - CATALOG_INDEX_BYTES)
            stream.seek(start)
            data = stream.read(min(size, CATALOG_INDEX_BYTES))
        if start:
            data = data.partition(b"\n")[2]
            issues.append("session_index.jsonl: only the last 4 MiB were sampled")
        names = {}
        for row in catalog_rows(data):
            name = row.get("thread_name")
            if not isinstance(name, str):
                continue
            try:
                # An explicit clear must not revive an earlier name. File order,
                # rather than clocks on different machines, decides the last hint.
                names[canonical_id(row.get("id"))] = " ".join(name.split())[:240]
            except SyncError:
                continue
        return names
    except FileNotFoundError:
        return {}
    except (OSError, SyncError) as exc:
        issues.append(f"session_index.jsonl: {exc}")
        return {}


def scan(home: Path) -> dict:
    """Read bounded display hints without SQLite, launching Codex or idle writers."""
    if not home.is_dir():
        raise SyncError(f"Source CODEX_HOME does not exist: {home}")
    entries, issues = [], []
    names = catalog_names(home, issues)
    locations = read_locations(home)
    index = RolloutIndex(home)
    issues.extend(index.issues)
    for thread_id in sorted(index.threads):
        try:
            rollout_id = index.head(thread_id)
            path = index.path(rollout_id)
            meta = index.metadata(rollout_id)
            before = signature(path)
            with path.open("rb") as source:
                row = json.loads(source.readline(2 * 1024**2))
                summary = read_catalog_preview(source)
                activity = read_catalog_activity(source, before[2])
            kind = "record" if activity is not None else "created"
            if activity is None:
                activity = timestamp_ns(meta.get("timestamp")) or timestamp_ns(row.get("timestamp"))
            base = meta.get("history_base")
            # A physical continuation is not a child conversation. Only cross-thread
            # ancestry participates in browser filtering and session grouping.
            parent = meta.get("forked_from_id") or meta.get("parent_thread_id")
            if parent is None and isinstance(base, dict):
                base_id = canonical_id(base["thread_id"])
                if base_id in index.rollouts:
                    parent = rollout_ids(index.path(base_id))[0]
                else:
                    parent = base_id
            if parent == thread_id:
                parent = None
            origin = meta.get("source", "unknown")
            if isinstance(origin, dict):
                origin = "subagent" if "subagent" in origin else "unknown"
            if not isinstance(origin, str):
                origin = "unknown"
            original_cwd = str(meta.get("cwd", ""))
            local_cwd = saved_cwd(locations, thread_id, original_cwd)
            entries.append(
                {
                    "id": thread_id,
                    "summary": summary,
                    "title": names.get(thread_id, ""),
                    "cwd": local_cwd if local_cwd is not None else original_cwd,
                    **({"original_cwd": original_cwd} if local_cwd is not None else {}),
                    "source": origin,
                    "parent_id": parent,
                    "size": before[2],
                    "modified_ns": before[3],
                    "activity_ns": activity,
                    "activity_kind": kind if activity is not None else "unknown",
                    "archived": path.is_relative_to(home / "archived_sessions"),
                    "changing": signature(path) != before,
                }
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError, SyncError) as exc:
            issues.append(f"Session {thread_id}: {exc}")
    return {"format": VERSION, "sessions": entries, "issues": issues}


def main() -> int:
    """The remote protocol exposes read operations only; output always uses stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote-name")
    parser.add_argument("operation", choices=("scan", "export"))
    parser.add_argument("home")
    parser.add_argument("sessions", nargs="*", type=canonical_id)
    args = parser.parse_args()
    try:
        home = Path(args.home).expanduser().resolve()
        if args.operation == "scan":
            print(json.dumps(scan(home), ensure_ascii=True))
        else:
            location = f"Remote {args.remote_name}" if args.remote_name else "Local machine"
            export_bundle(home, sys.stdout.buffer, args.sessions or None, location=location)
        return 0
    except (OSError, ValueError, KeyError, TypeError, SyncError) as exc:
        print(f"Source read failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
