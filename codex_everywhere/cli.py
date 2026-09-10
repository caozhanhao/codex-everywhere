"""CLI entry points; default invocation opens the session launcher."""

import argparse
import dataclasses
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from . import __version__, codex, reader, service
from .config import Node, default_path, load, parse_mappings
from .fleet import ScanJob
from .reader import SyncError
from .storage import STATE_DIRECTORY


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Launch Codex conversations across machines; confirm transfers when needed."
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", type=Path, help=f"Configuration file (default: {default_path()})")
    root.add_argument("--home", help="Existing local CODEX_HOME")
    root.add_argument("--sqlite-home", help="Local SQLite directory")
    root.add_argument(
        "--codex", help=f"Native Codex executable (validated with {codex.VALIDATED_VERSION})"
    )
    root.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Project path override for this invocation; permanent rules use nodes[].mappings",
    )
    root.add_argument("--cwd", help="Working directory for launching and the entire transfer")
    root.add_argument(
        "--global",
        dest="global_scope",
        action="store_true",
        help="Browse sessions from every working directory",
    )
    root.add_argument(
        "--include-archived", action="store_true", help="Include archived sessions in the browser"
    )
    root.add_argument(
        "--include-internal",
        action="store_true",
        help="Include subagents and noninteractive sessions in the browser",
    )
    commands = root.add_subparsers(dest="command")
    commands.add_parser("browse", help="Open the TUI (default)")
    listing = commands.add_parser("list", help="Read all nodes without modifying Codex data")
    listing.add_argument("--json", action="store_true")
    pulling = commands.add_parser("pull", help="Pull selected sessions from one node")
    pulling.add_argument("node")
    pulling.add_argument("--source-home", help="Required for a node outside the configuration")
    selection = pulling.add_mutually_exclusive_group(required=True)
    selection.add_argument("--session", action="append", type=reader.canonical_id)
    selection.add_argument("--all", action="store_true", help="Explicitly pull every session")
    importing = commands.add_parser("import", help="Import a previously exported ZIP")
    importing.add_argument("bundle", type=Path)
    for command in (pulling, importing):
        command.add_argument(
            "--dry-run", action="store_true", help="Preview only, no destination changes"
        )
        command.add_argument(
            "--no-rebuild", action="store_true", help="Leave indexing for a later rebuild"
        )
    exporting = commands.add_parser("export", help="Export local sessions to a new ZIP or stdout")
    exporting.add_argument("output")
    exporting.add_argument("--session", action="append", type=reader.canonical_id)
    rebuilding = commands.add_parser("rebuild", help="Retry only local native indexing")
    rebuilding.add_argument("--session", action="append", type=reader.canonical_id)
    return root


def say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def export(home: Path, output: str, sessions: list[str] | None) -> None:
    if output == "-":
        reader.export_bundle(home, sys.stdout.buffer, sessions)
        return
    path = Path(output).expanduser().resolve()
    if path.exists() or path.is_relative_to(home):
        raise SyncError("Export needs a new filename outside the source CODEX_HOME.")
    fd, temporary = tempfile.mkstemp(prefix=".codex-everywhere-export-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            reader.export_bundle(home, stream, sessions)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)  # Atomic, refuses any existing file.
    finally:
        os.unlink(temporary)
    say(f"Exported to {path}")


def execute(args) -> int:
    settings = load(
        args.config,
        home=args.home,
        sqlite_home=args.sqlite_home,
        codex=args.codex,
        mappings=parse_mappings(args.map),
        cwd=args.cwd,
    )
    if args.command in (None, "browse"):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SyncError(
                "The TUI needs a terminal. Use 'list --json' for noninteractive inventory."
            )
        from . import launcher
        from .browser import BrowseOptions
        from .ui import run

        request = run(
            settings,
            BrowseOptions(
                global_scope=args.global_scope,
                include_archived=args.include_archived,
                include_internal=args.include_internal,
            ),
        )
        if request is not None:
            launcher.handoff(settings.target, request)
    elif args.command == "list":
        job = ScanJob(settings)
        results = {}
        try:
            while job.pending:
                for result in job.poll():
                    results[result.name] = result
                time.sleep(0.05)
        finally:
            job.close()
        if args.json:
            print(
                json.dumps(
                    {name: dataclasses.asdict(result) for name, result in sorted(results.items())},
                    indent=2,
                )
            )
        else:
            from .presentation import clean

            for name, result in sorted(results.items()):
                print(
                    f"{name}: {clean(result.error) if result.error else str(len(result.sessions)) + ' sessions'}"
                )
                for entry in result.sessions:
                    print(f"  {entry['id']}  {clean(entry['summary'])[:100]}")
                for issue in result.issues:
                    print(f"  issue: {clean(issue)}")
        return int(any(result.error for result in results.values()))
    elif args.command == "export":
        export(settings.target.home, args.output, args.session)
    elif args.command == "rebuild":
        say(str(service.rebuild(settings.target, args.session, say)))
    else:
        if args.command == "pull":
            node = next((node for node in settings.nodes if node.name == args.node), None)
            if args.source_home:
                node = (
                    dataclasses.replace(node, home=args.source_home)
                    if node
                    else Node(args.node, args.node, args.source_home)
                )
            if node is None:
                raise SyncError("Unknown node; configure it or pass --source-home.")
            manager = service.prepare_pull(
                node,
                settings.target,
                tuple(args.session or ()),
                timeout=settings.transfer_timeout,
                progress=say,
            )
        else:
            manager = service.prepare_file(args.bundle, settings.target)
        with manager as prepared:
            say(prepared.summary())
            for change in prepared.changes:
                say(f"  {change.action.value:12} {change.id}")
                if prepared.directory_updates:
                    say(f"    Working directory: {prepared.directories[change.id]}")
            if args.dry_run:
                return int(prepared.has_conflicts)
            report = service.apply(prepared, index=not args.no_rebuild, progress=say)
            say(f"Done. Operation record: {report}")
    return 0


def main(argv=None) -> int:
    try:
        return execute(parser().parse_args(argv))
    except (SyncError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        say(f"ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        say(
            "Interrupted. If an import was applying, inspect "
            f"<CODEX_HOME>/{STATE_DIRECTORY}/backups before retrying."
        )
        return 130
