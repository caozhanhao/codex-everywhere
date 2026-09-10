"""Opt-in integration test using a stopped backup, never an active Codex home."""

import argparse
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from codex_everywhere import codex, reader, safety, service
from codex_everywhere.config import Target


def rollout_hashes(home):
    sessions, _ = reader.collect(home)
    return {rollout_id: item.sha256 for rollout_id, item in sessions.items()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-home", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--expected-turns", type=int, required=True)
    parser.add_argument("--expected-items", type=int, required=True)
    parser.add_argument("--legacy-session")
    parser.add_argument("--archive-ancestor", action="store_true")
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="Index an earlier complete root prefix before importing its newer version",
    )
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="codex-sync-native-"))
    root.chmod(0o700)
    target = root / "target"
    source = root / "source"
    work = root / "different-device-workspace"
    target.mkdir()
    source.mkdir()
    work.mkdir()
    (target / "config.toml").write_text(
        """model = "gpt-5.6-sol"
model_provider = "offline"
[model_providers.offline]
name = "Offline integration test"
base_url = "http://127.0.0.1:9/v1"
wire_api = "responses"
requires_openai_auth = false
"""
        + '\n[mcp_servers.must_not_start]\ncommand = "/usr/bin/touch"\nargs = ['
        + json.dumps(str(root / "UNEXPECTED_MCP_START"))
        + "]\n"
    )
    selected = [args.session] + ([args.legacy_session] if args.legacy_session else [])
    original_home = Path(args.source_home)
    source_sessions, source_order = reader.collect(original_home, selected)
    for rollout_id in source_order:
        item = source_sessions[rollout_id]
        dest = source / item.relative
        if args.archive_ancestor and rollout_id == source_order[0]:
            dest = source / "archived_sessions" / Path(item.relative).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original_home / item.relative, dest)
    earlier_turns = None
    if args.check_update:
        root_id = source_sessions[source_order[0]].id
        root_copy = reader.paths_by_id(source)[root_id]
        full = root_copy.read_bytes()
        lines = full.splitlines(keepends=True)
        boundary = next(
            i
            for i, line in enumerate(lines, 1)
            if i < len(lines) and json.loads(line).get("payload", {}).get("type") == "task_complete"
        )
        prefix = b"".join(lines[:boundary])
        root_copy.write_bytes(prefix)  # Only the disposable source copy is shortened.
        early = root / "earlier.zip"
        with early.open("wb") as output:
            reader.export_bundle(source, output, [root_id])
        with service.prepare_file(
            early, Target(target, target, args.codex, cwd=str(work))
        ) as prepared:
            first_report = service.apply(prepared)
        earlier_turns = json.loads((first_report / "rebuild.json").read_text())[0]["turns"]
        assert reader.paths_by_id(target)[root_id].read_bytes() == prefix
        root_copy.write_bytes(full)
    bundle = root / "sessions.zip"
    with bundle.open("wb") as output:
        reader.export_bundle(source, output, selected)
    with service.prepare_file(
        bundle, Target(target, target, args.codex, cwd=str(work))
    ) as prepared:
        operation = service.apply(prepared)
    result = json.loads((operation / "rebuild.json").read_text())
    assert all("error" not in row for row in result), result
    assert not (root / "UNEXPECTED_MCP_START").exists(), "Configured MCP was started"
    if args.check_update:
        later_turns = next(row["turns"] for row in result if row["id"] == root_id)
        assert later_turns > earlier_turns, (earlier_turns, later_turns)
    original_hashes = rollout_hashes(target)
    assert original_hashes == rollout_hashes(source)
    server = codex.AppServer(target, args.codex, root / "verify.log", target)
    try:
        server.call(
            "thread/resume", {"threadId": args.session, "excludeTurns": True, "cwd": str(work)}
        )
        page = server.call(
            "thread/turns/list", {"threadId": args.session, "itemsView": "full", "limit": 100}
        )
        assert not page.get("nextCursor")
        turns = len(page["data"])
        items = sum(len(turn.get("items", [])) for turn in page["data"])
        assert turns == args.expected_turns, (turns, args.expected_turns)
        assert items == args.expected_items, (items, args.expected_items)
        try:
            reader.assert_idle(target)
        except reader.SyncError:
            pass
        else:
            raise AssertionError("Active app-server was not detected")
        lock = target / "thread-writer-locks" / (args.session + ".lock")
        try:
            with safety.file_lock(lock):
                raise AssertionError("Native Codex writer lock was not respected")
        except reader.SyncError:
            pass
    finally:
        server.close()
    assert original_hashes == rollout_hashes(target)
    databases = {}
    for path in target.glob("*.sqlite"):
        connection = sqlite3.connect("file:" + str(path) + "?mode=ro", uri=True)
        try:
            checks = connection.execute("PRAGMA integrity_check").fetchall()
            assert checks == [("ok",)], (path.name, checks)
            databases[path.name] = "ok"
        finally:
            connection.close()
    report = {
        "source": "stopped recovery backup",
        "session": args.session,
        "copied_sessions_including_ancestors": len(reader.paths_by_id(target)),
        "copied_rollouts": len(original_hashes),
        "turns": turns,
        "items": items,
        "jsonl_bytes_unchanged": True,
        "active_process_guard": "passed",
        "native_writer_lock": "passed",
        "configured_mcp_not_started": True,
        "archived_ancestor": args.archive_ancestor,
        "legacy_session_included": bool(args.legacy_session),
        "existing_database_update": bool(args.check_update),
        "earlier_root_turns": earlier_turns,
        "database_integrity": databases,
        "result_directory": str(root),
    }
    (root / "RESULT.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return root


if __name__ == "__main__":
    main()
