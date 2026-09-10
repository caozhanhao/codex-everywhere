"""Local Codex app-server adapter. Never sent to or run on a source host.

History is reconstructed through the native API; no SQLite edits, model turns,
interactive approvals, or project tools are requested by this adapter.
"""

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import mapped_directory
from .reader import SyncError, assert_idle, collect, read_locations, saved_cwd
from .storage import write_json

VALIDATED_VERSION = "0.153.4"


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
        if sqlite_home:
            command += ["-c", "sqlite_home=" + json.dumps(str(sqlite_home))]
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


def map_cwd(value, mappings, override=None):
    if override:
        result = Path(override).expanduser().resolve()
    else:
        result = Path(mapped_directory(value, mappings)).expanduser()
        if not result.is_absolute():
            raise SyncError(f"Source working directory needs --map or --cwd: {value!r}")
        result = result.resolve()
    if not result.is_dir():
        raise SyncError(f"Working directory does not exist: {result}. Use --map OLD=NEW or --cwd.")
    return str(result)


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
    assert_idle(home)
    sessions, order = collect(home, selected)
    locations = read_locations(home)
    results = []
    server = None
    failed = set()
    try:
        for index, thread_id in enumerate(order):
            item = sessions[thread_id]
            parent = item.history_base and item.history_base["thread_id"]
            if parent in failed:
                failed.add(thread_id)
                results.append(
                    {"id": thread_id, "error": "Ancestor could not be rebuilt: " + parent}
                )
                continue
            try:
                local = saved_cwd(locations, thread_id, item.cwd)
                cwd = map_cwd(local or item.cwd, () if local else mappings, override)
                if server is None:
                    server = AppServer(home, binary, report_dir / "app-server.log", sqlite_home)
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
                results.append({"id": thread_id, "turns": turns, "cwd": cwd})
            except (SyncError, KeyError, TypeError) as exc:
                failed.add(thread_id)
                results.append({"id": thread_id, "error": str(exc)})
                if server:
                    server.close()
                    server = None
            if server and (index + 1) % 16 == 0:
                server.close()
                server = None
            if (index + 1) % 10 == 0 or index + 1 == len(order):
                progress(f"Rebuilt {index + 1}/{len(order)} session(s); {len(failed)} failed.")
    finally:
        if server:
            server.close()
        write_json(report_dir / "rebuild.json", results)
    if failed:
        first = next(row["error"] for row in results if "error" in row)
        raise SyncError(
            "{} session(s) need indexing repair. First error: {}. Report: {}".format(
                len(failed), first, report_dir / "rebuild.json"
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
    if version != VALIDATED_VERSION:
        progress(
            f"Codex {version} is unverified; session reconstruction was validated with {VALIDATED_VERSION}."
        )
