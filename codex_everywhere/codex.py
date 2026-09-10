"""Local Codex app-server adapter. Never sent to or run on a source host.

History is reconstructed through the native API. Segmented histories also need
a guarded local metadata-path switch; turn/item indexes stay owned by Codex.
No model turns, interactive approvals, or project tools are requested.
"""

import json
import os
import queue
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import closing, contextmanager
from pathlib import Path

from .config import mapped_directory
from .reader import SyncError, collect, read_locations, saved_cwd, session_heads
from .safety import maintenance_lock, require_local, safe_destination, session_locks
from .storage import write_json

VALIDATED_VERSION = "0.154.0"
VALIDATED_VERSIONS = ("0.153.4", VALIDATED_VERSION)


class AppServer:
    ALLOWED_METHODS = frozenset(
        {
            "initialize",
            "config/read",
            "thread/resume",
            "thread/turns/list",
            "thread/read",
            "thread/unsubscribe",
        }
    )

    def __init__(self, home, binary, log_path, sqlite_home=None, timeout=90):
        env = dict(os.environ)
        env["CODEX_HOME"] = str(home)
        env["CODEX_SQLITE_HOME"] = str(sqlite_home or home)
        env["RUST_LOG"] = "error"
        self.timeout = timeout
        self.home = home
        self.counter = 0
        self.messages = queue.Queue()
        log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        self.log = os.fdopen(log_fd, "ab")
        command = [
            binary,
            "app-server",
            "--stdio",
            "-c",
            "mcp_servers={}",
            "-c",
            "features.apps=false",
            "-c",
            "features.plugins=false",
            "-c",
            "features.hooks=false",
            "-c",
            "features.goals=false",
            "-c",
            "features.memories=false",
            "-c",
            "features.shell_snapshot=false",
        ]
        command += ["-c", "sqlite_home=" + json.dumps(str(sqlite_home or home))]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=home,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.log,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError:
            self.log.close()
            raise

        def read_messages():
            try:
                for line in self.process.stdout:
                    try:
                        message = json.loads(line)
                        self.messages.put(
                            message if isinstance(message, dict) else {"_invalid": True}
                        )
                    except json.JSONDecodeError:
                        self.messages.put({"_invalid": True})
            finally:
                self.messages.put({"_eof": True})

        self.reader = threading.Thread(target=read_messages, daemon=True)
        self.reader.start()
        try:
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "codex_everywhere", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.process.stdin.write('{"method":"initialized"}\n')
            self.process.stdin.flush()
        except BaseException:
            self.close()
            raise

    def call(self, method: str, params: dict) -> dict:
        if method not in self.ALLOWED_METHODS:
            raise SyncError(f"Native adapter does not permit method: {method}")
        if method == "thread/resume":
            # An empty mcp_servers table is merged with, rather than replacing,
            # configured entries in this Codex version. Disable each effective
            # entry explicitly, including entries supplied by project config.
            configuration = self.call(
                "config/read", {"cwd": params.get("cwd", str(self.home)), "includeLayers": False}
            )
            servers = configuration["config"].get("mcp_servers", {})
            params = dict(params)
            overrides = dict(params.get("config") or {})
            overrides["mcp_servers"] = {name: {"enabled": False} for name in servers}
            params["config"] = overrides
        self.counter += 1
        request_id = self.counter
        try:
            self.process.stdin.write(
                json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
            )
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            raise SyncError("app-server exited; see its log.") from None
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SyncError(f"app-server timed out during {method}")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise SyncError(f"app-server timed out during {method}") from None
            if message.get("_eof") or message.get("_invalid"):
                raise SyncError(f"app-server protocol failed during {method}; see log.")
            if "method" not in message and message.get("id") == request_id:
                if "error" in message:
                    raise SyncError("{}: {}".format(method, message["error"]))
                return message.get("result", {})
            if "id" in message and "method" in message:
                # Never approve a tool, supply credentials, or start a model turn while indexing.
                response = {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Session sync does not handle interactive requests",
                    },
                }
                self.process.stdin.write(json.dumps(response) + "\n")
                self.process.stdin.flush()

    def close(self):
        try:
            self.process.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=2)
        self.process.stdout.close()
        self.log.close()


class DirectoryUnavailable(SyncError):
    """A local launch directory needs choosing, not a retry of the same operation."""

    def __init__(self, directory: str):
        self.directory = directory
        super().__init__(
            f"Working directory is unavailable on this machine: {directory}. "
            "Use --map OLD=NEW or --cwd."
        )


def map_cwd(value, mappings, override=None):
    directory = override or mapped_directory(value, mappings)
    result = Path(directory).expanduser()
    if not override and not result.is_absolute():
        raise DirectoryUnavailable(directory)
    if not result.is_dir():
        # Keep the original spelling of foreign paths in diagnostics. In particular,
        # macOS resolves a missing Linux /home path through its local system volume.
        raise DirectoryUnavailable(directory)
    return str(result.resolve())


@contextmanager
def indexing_home(home: Path):
    """Give the native projector its own writer locks while destination locks stay held.

    Rollouts and configuration resolve to their real local paths; databases are
    passed explicitly to AppServer. Sharing the maintenance lock also prevents
    this worker from starting a background compression or migration of them.
    """
    with tempfile.TemporaryDirectory(prefix="codex-everywhere-index-") as temporary:
        root = Path(temporary).resolve()
        for name in (
            "sessions",
            "archived_sessions",
            "config.toml",
            "auth.json",
            "models_cache.json",
            "session_index.jsonl",
        ):
            source = home / name
            if source.exists():
                (root / name).symlink_to(source, target_is_directory=source.is_dir())
        (root / ".tmp").mkdir()
        (root / ".tmp" / "rollout-maintenance.lock").symlink_to(
            home / ".tmp" / "rollout-maintenance.lock"
        )
        yield root


def select_rollout(home, sqlite_home, item, report_dir, selections):
    with maintenance_lock(home), session_locks(home, [item.id]):
        return _select_rollout(home, sqlite_home, item, report_dir, selections)


def _select_rollout(home, sqlite_home, item, report_dir, selections):
    """Bind an already validated segment for native projection (Codex state schema 5).

    Codex refuses a paginated resume whose explicit path differs from this row.
    The caller holds the affected session and maintenance locks throughout
    reconstruction. Codex maintains all other metadata and history indexes.
    """
    database_home = sqlite_home or home
    path = database_home / "state_5.sqlite"
    safe_destination(database_home, path)
    require_local(path)
    try:
        with (
            # Other sessions may briefly write metadata in the same SQLite database.
            closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)) as db,
            db,
        ):
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT rollout_path, history_mode FROM threads WHERE id = ?", (item.id,)
            ).fetchone()
            if row is None or row[1] != "paginated":
                raise SyncError(f"Codex did not index paginated session metadata: {item.id}")
            destination = str(home / item.relative)
            if row[0] == destination:
                return
            entry = selections.setdefault(item.id, {"original": row[0]})
            entry["selected"] = destination
            # Persist the original binding and intended switch before committing it.
            write_json(report_dir / "index-paths.json", selections)
            db.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", (destination, item.id))
    except sqlite3.Error as exc:
        raise SyncError(f"Cannot select history segment for {item.id}: {exc}") from None


def rebuild(
    home,
    selected,
    binary,
    mappings,
    override,
    report_dir,
    sqlite_home=None,
    progress=lambda message: None,
):
    """Reconstruct under the caller's session and maintenance locks."""
    with indexing_home(home) as worker_home:
        return _rebuild(
            home,
            selected,
            binary,
            mappings,
            override,
            report_dir,
            sqlite_home,
            progress,
            worker_home,
        )


def _rebuild(
    home, selected, binary, mappings, override, report_dir, sqlite_home, progress, worker_home
):
    sessions, order = collect(home, selected) if selected else ({}, [])
    heads = session_heads(sessions)
    segmented = {
        item.id for item in sessions.values() if item.rollout_id != heads[item.id].rollout_id
    }
    locations = read_locations(home)
    results = []
    server = None
    failed = {}
    selections = {}
    try:
        for index, rollout_id in enumerate(order):
            item = sessions[rollout_id]
            thread_id = item.id
            is_head = heads[thread_id].rollout_id == rollout_id
            parent = item.history_base and item.history_base["thread_id"]
            try:
                if item.relative.startswith("archived_sessions/"):
                    raise SyncError(
                        f"Local session is archived: {thread_id}. "
                        f"Run codex unarchive {thread_id} first."
                    )
                if parent in failed:
                    raise SyncError(
                        f"Ancestor rollout {parent} could not be rebuilt: {failed[parent]}"
                    )
                local = saved_cwd(locations, thread_id, item.cwd)
                cwd = map_cwd(local or item.cwd, () if local else mappings, override)
                if server is None:
                    server = AppServer(
                        worker_home, binary, report_dir / "app-server.log", sqlite_home or home
                    )
                if thread_id in segmented:
                    # Close this worker's cached writer before selecting a new segment.
                    # The destination's session locks remain held across the whole run.
                    server.call("thread/read", {"threadId": thread_id, "includeTurns": False})
                    server.close()
                    server = None
                    _select_rollout(home, sqlite_home, item, report_dir, selections)
                    server = AppServer(
                        worker_home, binary, report_dir / "app-server.log", sqlite_home or home
                    )
                server.call(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "path": str(home / item.relative),
                        "excludeTurns": True,
                        "cwd": cwd,
                    },
                )
                if item.history_mode == "paginated":
                    cursor = None
                    cursors = set()
                    turns = 0
                    while True:
                        params = {"threadId": thread_id, "itemsView": "notLoaded", "limit": 100}
                        if cursor:
                            params["cursor"] = cursor
                        page = server.call("thread/turns/list", params)
                        turns += len(page["data"])
                        cursor = page.get("nextCursor")
                        if not cursor:
                            break
                        if cursor in cursors:
                            raise SyncError("History pagination repeated a cursor.")
                        cursors.add(cursor)
                else:
                    read = server.call("thread/read", {"threadId": thread_id, "includeTurns": True})
                    turns = len(read["thread"].get("turns", []))
                server.call("thread/unsubscribe", {"threadId": thread_id})
                if is_head:
                    results.append({"id": thread_id, "turns": turns, "cwd": cwd})
            except (SyncError, KeyError, TypeError) as exc:
                failed[rollout_id] = str(exc)
                if is_head:
                    results.append({"id": thread_id, "error": str(exc)})
                if server:
                    server.close()
                    server = None
            # Native resume projects only this physical rollout. Index every segment
            # ancestor-first, leaving the logical thread pointed at its final head.
            # Unsubscribe does not immediately unload it; restart before switching
            # paths for the same thread, otherwise Codex can reuse the old writer.
            if server and (not is_head or (index + 1) % 16 == 0):
                server.close()
                server = None
            if (index + 1) % 10 == 0 or index + 1 == len(order):
                errors = sum("error" in row for row in results)
                progress(f"Rebuilt {len(results)}/{len(heads)} session(s); {errors} failed.")
    finally:
        if server:
            server.close()
        try:
            # A failed ancestor projection must not leave a thread on an older segment.
            for thread_id in selections:
                _select_rollout(home, sqlite_home, heads[thread_id], report_dir, selections)
        finally:
            write_json(report_dir / "rebuild.json", results)
    if failed:
        first = next(row["error"] for row in results if "error" in row)
        raise SyncError(
            "{} session(s) need indexing repair. First error: {}. Report: {}".format(
                sum("error" in row for row in results), first, report_dir / "rebuild.json"
            )
        )
    return results


def check_version(binary: str, progress: Callable[[str], None] = lambda message: None) -> None:
    """Verify the executable runs; report unvalidated versions without rejecting them.

    The version is a validation baseline, not a compatibility test. Protocol
    failures are handled by the adapter and the normal indexing recovery path.
    """
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SyncError(f"Cannot run Codex: {exc}") from exc
    fields = result.stdout.strip().split()
    if not fields:
        raise SyncError("Codex returned an empty version string.")
    version = fields[-1]
    if version not in VALIDATED_VERSIONS:
        progress(
            f"Codex {version} is unverified; session reconstruction was validated with {VALIDATED_VERSION}."
        )
