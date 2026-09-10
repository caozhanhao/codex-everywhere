"""Plain display text and width handling; no terminal or filesystem mutations."""

import datetime
import time
import unicodedata
from pathlib import Path

from .config import Settings
from .fleet import NodeResult
from .plan import Action
from .service import Prepared


def clean(value: object) -> str:
    return "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in str(value)
    )


def columns(value: str) -> int:
    return sum(
        0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        for char in value
    )


def clip(value: object, width: int) -> str:
    output, used = [], 0
    for char in clean(value):
        size = columns(char)
        if used + size > width:
            break
        output.append(char)
        used += size
    return "".join(output)


def fit(value: object, width: int) -> str:
    text = clean(value)
    return (
        text
        if columns(text) <= width
        else clip(text, max(0, width - 1)) + ("…" if width > 0 else "")
    )


def search_text(query: str, width: int, *, editing: bool) -> str:
    if not editing:
        return fit("/ " + query if query else "/ Search sessions", width)
    # Keep the input end visible, reserving one cell for the terminal cursor.
    text = clean(query)
    available = max(0, width - 3)  # Two cells for the prompt, one for the cursor.
    used, start = 0, len(text)
    while start:
        size = columns(text[start - 1])
        if used + size > available:
            break
        used += size
        start -= 1
    while start < len(text) and unicodedata.combining(text[start]):
        start += 1
    return "/ " + text[start:]


def wrap(lines: list[str], width: int) -> list[str]:
    output = []
    for raw in lines:
        text = clean(raw)
        if not text:
            output.append("")
        while text:
            part = clip(text, max(2, width))
            if len(part) < len(text) and part.rfind(" ") > len(part) // 2:
                part = part[: part.rfind(" ")]
            output.append(part)
            text = text[len(part) :].lstrip()
    return output


def directory_label(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home) :] if text == home or text.startswith(home + "/") else text


def date(nanoseconds: int | None) -> str:
    try:
        return datetime.datetime.fromtimestamp(nanoseconds / 1e9).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown time"


def relative_time(nanoseconds: int | None, now: int | None = None) -> str:
    if nanoseconds is None:
        return "Unknown time"
    seconds = ((time.time_ns() if now is None else now) - nanoseconds) // 10**9
    if seconds < -60:
        return "Future time"
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 2 * 86400:
        return "Yesterday"
    if seconds < 7 * 86400:
        return f"{seconds // 86400}d ago"
    stamp = date(nanoseconds)
    return "Unknown time" if stamp == "unknown time" else stamp[:10]


ACTION_LABELS = {
    Action.ADD: "Add",
    Action.UPDATE: "Update",
    Action.SAME: "Identical",
    Action.LOCAL_NEWER: "Keep local",
    Action.CONFLICT: "Conflict",
}


def comparison_lines(prepared: Prepared, thread_id: str) -> list[str]:
    if prepared.has_conflicts:
        lines = [
            "These histories have diverged. Automatic sync is unavailable.",
            "Save the incoming copy for review. Existing sessions stay intact.",
        ]
    else:
        lines = [
            {
                Action.ADD: "Sync this session here, then open Codex.",
                Action.UPDATE: "Add the missing history here, then open Codex.",
                Action.SAME: "The session files already match.",
                Action.LOCAL_NEWER: "The local copy has more history and will be kept.",
            }[prepared.action_for(thread_id)]
        ]
    counts = [
        (action, sum(prepared.action_for(i) is action for i in prepared.heads)) for action in Action
    ]
    lines += [
        "Keep Codex closed on both machines.",
        "",
        "Changes: "
        + " · ".join(f"{ACTION_LABELS[action]} {count}" for action, count in counts if count),
    ]
    ancestors = len(prepared.heads) - 1
    if ancestors:
        lines.append(f"Includes {ancestors} ancestor session(s) to preserve context.")
    segments = len(prepared.sessions) - len(prepared.heads)
    if segments:
        lines.append(f"Includes {segments} earlier history segment(s) needed by these sessions.")
    restored = sum(
        change.action is Action.ADD
        and change.rollout_id == prepared.heads[change.id].rollout_id
        and prepared.sessions[change.rollout_id].relative.startswith("archived_sessions/")
        for change in prepared.changes
    )
    if restored:
        lines.append(f"{restored} archived session(s) will become active here.")
    if prepared.directory_updates and not prepared.has_conflicts:
        lines += [
            f"Open in: {prepared.directories[thread_id]}",
            f"Remember local directories for {prepared.directory_updates} session(s).",
        ]
    if not prepared.has_conflicts:
        lines += ["", "Existing files are backed up before changes. Local indexes are rebuilt."]
    return lines


def plan_lines(prepared: Prepared) -> list[str]:
    lines = [f"Destination: {prepared.target.home}", ""]
    for change in prepared.changes:
        lines += [f"{ACTION_LABELS[change.action]}  {change.id}", f"  {change.destination}"]
        if prepared.directory_updates:
            lines.append(f"  Working directory: {prepared.directories[change.id]}")
    return lines


def catalog_label(results: dict[str, NodeResult], pending: set[str]) -> str:
    if pending:
        return "Updating…"
    if any(result.error for result in results.values()):
        return "Incomplete list"
    if any(result.issues for result in results.values()):
        return "Read warnings"
    return "Sessions"


def empty_catalog_lines(
    *, query: str, global_scope: bool, results: dict[str, NodeResult], pending: set[str]
) -> tuple[str, str]:
    if pending:
        return "Reading sessions…", "Start a new session while loading."
    if query:
        return "No matching sessions.", "Try a shorter query or a different scope."
    if any(result.error for result in results.values()):
        return "No sessions found.", "Some nodes failed. Press n for details."
    hint = (
        "[n] Node status   [?] Filters and help"
        if global_scope
        else "[g] All projects   [n] Node status"
    )
    return "No sessions here yet.", hint


def node_summary(settings: Settings, results: dict[str, NodeResult], pending: set[str]) -> str:
    completed = [
        results[node.name]
        for node in settings.nodes
        if node.name in results and node.name not in pending
    ]
    failed = sum(bool(result.error) for result in completed)
    ready = len(completed) - failed
    loading = len(pending - {"local"})
    counts = (
        (ready, "ready"),
        (loading, "reading"),
        (failed, "failed"),
    )
    parts = [f"{count} {label}" for count, label in counts if count]
    local = results.get("local")
    if "local" in pending:
        parts.append("Local: reading")
    elif local and local.error:
        parts.append("Local: failed")
    elif not settings.nodes:
        parts.append("Local only")
    if any(result.issues for name, result in results.items() if name not in pending):
        parts.append("read warnings")
    return " · ".join(parts) or "Not read yet"


def browser_hints(width: int, *, global_scope: bool, searching: bool) -> list[tuple[str, str]]:
    if searching:
        hints = [("↑↓", "Browse"), ("Enter", "Done"), ("Esc", "Clear & back")]
        if columns("  ".join(f"[{key}] {label}" for key, label in hints)) > width:
            return [("↑↓", "Browse"), ("↵", "Done"), ("Esc", "Clear")]
        return hints
    hints = [
        ("Enter", "Open"),
        ("/", "Search"),
        ("g", "This project" if global_scope else "All projects"),
        ("?", "Help"),
        ("Esc", "Exit"),
    ]
    extras = [
        ("Tab", "Source"),
        ("r", "Refresh"),
        ("d", "Details"),
        ("+", "New"),
        ("a", "Archived"),
    ]
    insert_before = -2  # Keep Help and Exit at the end.
    used = columns("  ".join(f"[{key}] {label}" for key, label in hints))
    if used > width:
        # Always show Open, Search and Exit, even at the minimum terminal width.
        hints = [("↵", "Open"), ("/", "Find"), ("Esc", "Exit")]
        extras = [("g", "Cwd" if global_scope else "All"), ("?", "Help")]
        insert_before = -1
        used = columns("  ".join(f"[{key}] {label}" for key, label in hints))
    for key, label in extras:
        extra = columns(f"[{key}] {label}") + 2
        if used + extra > width:
            break
        hints.insert(insert_before, (key, label))
        used += extra
    return hints


def node_lines(settings: Settings, results: dict[str, NodeResult], pending: set[str]) -> list[str]:
    lines = []
    for name, home in [
        ("local", str(settings.target.home)),
        *((node.name, node.home) for node in settings.nodes),
    ]:
        label = "Local" if name == "local" else name
        result = results.get(name)
        if name in pending:
            status = "Reading…"
        elif result is None:
            status = "Not read yet"
        elif result.error:
            status = "Failed"
        else:
            status = f"{len(result.sessions)} session files"
        lines += [f"{label} · {status}", f"  {home}"]
        if result and name not in pending:
            if result.error:
                lines.append("  " + result.error)
            if result.issues:
                lines += [
                    f"  {len(result.issues)} read warning(s):",
                    *("  " + issue for issue in result.issues),
                ]
            if not result.sessions and not result.error:
                lines.append("  No usable sessions here. Other data directories were not scanned.")
        lines.append("")
    return lines


def help_lines() -> list[str]:
    return [
        "↑/↓ or j/k   Select a session; arrows and page keys also work during search",
        "Enter        Open the selection; confirm first if a transfer is needed",
        "+            New session in the launch directory (also the first list item)",
        "←/→, Enter   Choose Cancel or Sync & open on the confirmation screen",
        "Tab          Switch copies of the selected session, including the local copy",
        "/            Search titles, previews, IDs, nodes and paths",
        "g            Switch between this project and all projects",
        "a            Show or hide archived sessions",
        "d            Details for the selection or transfer",
        "n            Node status, directories and full errors",
        "r            Refresh session lists",
        "Esc          Go back; exit from the session list; clear and leave search when editing",
        "",
        "Local and remote sessions share one list, scoped to this directory by default.",
        "Directories match by full path after --map. --cwd overrides the launch directory.",
        "Filters affect browsing only. Transfers include all required ancestor sessions.",
        "Esc cancels comparison. During a write, it returns to the list once syncing finishes.",
        "",
        "Sessions are sorted by recorded activity across copies. Local wins a time tie.",
        "Updating means more results may arrive. Incomplete list means a node failed.",
        "Use n for errors and r to refresh. The list does not refresh continuously.",
        "Opening a session before loading finishes asks you to confirm: a newer copy may exist.",
        "The preferred source follows incoming results until you pin a copy with Tab.",
        "Times come from session records, never file-copy timestamps.",
        "Titles and times are display hints. Full content determines safe updates.",
        "Codex runs on this machine with its local environment and configuration.",
        "Sources only provide JSONL files. Their Codex processes and databases are untouched.",
    ]
