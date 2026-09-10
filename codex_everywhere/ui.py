"""Curses controller. Only an explicitly confirmed preview can call service.apply."""

import curses
import os
import queue
import socket
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import codex, launcher, service
from . import presentation as display
from .browser import BrowseOptions, BrowserModel
from .config import Settings, Target
from .fleet import Copy, ScanJob


@dataclass(frozen=True)
class BrowserLayout:
    new_row: int
    first_session_row: int
    footer_row: int
    row_height: int

    @property
    def page_size(self) -> int:
        return max(1, (self.footer_row - self.first_session_row) // self.row_height)


@dataclass
class Transfer:
    manager: AbstractContextManager
    prepared: service.Prepared
    id: str
    source: str
    title: str

    def close(self) -> None:
        self.manager.__exit__(None, None, None)


@dataclass(frozen=True)
class SessionSelection:
    """Keep the chosen session and source stable while inventories arrive."""

    id: str
    copy: Copy
    title: str


@dataclass
class DirectoryPrompt:
    session: str
    current: Path
    missing: str | None = None
    ancestors: bool = False
    selected: int = 0
    text: str = ""
    cursor: int = 0
    error: str = ""
    choices: tuple[str, ...] = field(init=False)

    def __post_init__(self):
        self.choices = tuple(
            name
            for name, path in (("session", Path(self.session)), ("current", self.current))
            if path.is_absolute() and path.is_dir()
        ) + ("other", "back")
        self.text = str(self.current) + os.sep
        self.cursor = len(self.text)


class Browser:
    def __init__(self, screen, settings: Settings, options: BrowseOptions | None = None):
        self.screen = screen
        self.settings = settings
        self.local_name = socket.gethostname().split(".")[0]
        self.model = BrowserModel(
            options or BrowseOptions(), settings.target.mappings, local_name=self.local_name
        )
        self.scan = ScanJob(settings)
        self.searching = False
        self.scroll_top = 0
        self.page_offset = 0
        self.phase = "browse"
        self.return_phase = "browse"
        self.message = ""
        self.report: Path | None = None
        self.transfer: Transfer | None = None
        self.pending_open: SessionSelection | None = None
        self.open_selection: SessionSelection | None = None
        self.open_target = settings.target
        self.directory_prompt: DirectoryPrompt | None = None
        self.rebuild_pending = False
        self.task: Future | None = None
        self.task_kind = ""
        self.back_requested = False
        self.launch_request: launcher.LaunchRequest | None = None
        self.confirm_choice = 0  # The default button is always on the left.
        self.cancel = threading.Event()
        self.events: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.notes: list[str] = []
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transfer")
        self.accent = curses.A_BOLD

    @property
    def pending(self) -> set[str]:
        return {node.name if node else "local" for node in self.scan.pending.values()}

    def put(self, row: int, text: object, style: int = 0, col: int = 2) -> None:
        height, width = self.screen.getmaxyx()
        if 0 <= row < height and col < width - 1:
            try:
                self.screen.addstr(row, col, display.clip(text, width - col - 1), style)
            except curses.error:
                pass

    def collect_events(self) -> None:
        while not self.events.empty():
            self.notes.append(self.events.get())
        self.notes = self.notes[-100:]

    def poll(self) -> None:
        completed = self.scan.poll()
        if completed:
            self.model.ingest(completed)
        self.collect_events()
        if not self.task or not self.task.done():
            return
        task, self.task = self.task, None
        kind = self.task_kind
        self.page_offset = 0
        try:
            result = task.result()
            if kind == "prepare":
                previous = self.transfer
                self.transfer = result
                if previous and previous is not result:
                    previous.close()
                if self.cancel.is_set():
                    self.back()
                elif self.choose_transfer_directory():
                    pass
                elif result.prepared.needs_sync:
                    if (
                        self.rebuild_pending
                        and previous
                        and result.prepared.changes == previous.prepared.changes
                        and result.prepared.locations == previous.prepared.locations
                        and result.prepared.directories == previous.prepared.directories
                    ):
                        # Retry confirms the same previously approved changes. A
                        # different comparison still gets a fresh review below.
                        self.apply_transfer()
                    else:
                        self.phase = "preview"
                        self.confirm_choice = 0
                elif self.rebuild_pending:
                    self.phase = "rebuilding"
                    self.task_kind = "rebuild"
                    self.task = self.worker.submit(
                        service.rebuild,
                        result.prepared.target,
                        [result.id],
                        progress=self.events.put,
                    )
                else:
                    self.resume_transfer()
            else:
                self.report = result
                self.rebuild_pending = False
                self.phase = "result"
                if self.back_requested:
                    self.back()
                else:
                    self.resume_transfer()
        except Exception as exc:
            if kind == "prepare" and self.cancel.is_set():
                self.back()
            else:
                if kind in ("apply", "rebuild") and task.exception() is not None:
                    self.rebuild_pending = True
                self.show_error(exc)
        self.collect_events()
        if kind in ("apply", "rebuild") and self.launch_request is None:
            self.refresh_catalog()

    def refresh_catalog(self) -> None:
        self.scan.close()
        # Retain rows and their identities until each refreshed node replaces its
        # old result. ScanJob owns its futures; cancelled scans cannot leak events.
        self.scan = ScanJob(self.settings)

    def draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 12 or width < 42:
            self.put(0, "Terminal too small (min 42×12).", col=0)
            self.put(1, self.escape_hint(), col=0)
            self.refresh_screen()
            return
        self.put(0, "codex-everywhere", curses.A_BOLD)
        scope = (
            "All projects"
            if self.model.options.global_scope
            else display.directory_label(self.model.options.directory)
        )
        scope = display.fit(scope, max(10, width - 27))
        self.put(0, scope, curses.A_DIM, col=max(23, width - display.columns(scope) - 3))
        cursor = None
        if self.phase == "browse":
            cursor = self.draw_browser(width)
        elif self.phase in ("directory", "directory_input"):
            cursor = self.draw_directory(height, width)
        else:
            self.draw_panel(height, width)
        self.refresh_screen(cursor)

    def refresh_screen(self, cursor: tuple[int, int] | None = None) -> None:
        # IMEs follow the terminal cursor. Restore it after footer/panel drawing.
        if cursor is not None:
            try:
                self.screen.move(*cursor)
            except curses.error:
                pass
        try:
            curses.curs_set(1 if cursor is not None else 0)
        except curses.error:
            pass
        self.screen.refresh()

    def browser_layout(self) -> BrowserLayout:
        height, width = self.screen.getmaxyx()
        # Wide lists use one row in either scope. Narrow screens put metadata
        # on a second line so titles remain readable.
        new_row = 4
        narrow = width < 68
        return BrowserLayout(
            new_row=new_row,
            first_session_row=new_row + 1 + int(self.model.options.global_scope and narrow),
            footer_row=height - 4,
            row_height=2 if narrow else 1,
        )

    def draw_browser(self, width: int) -> tuple[int, int] | None:
        model = self.model
        layout = self.browser_layout()
        time_col, node_col = width - 15, width - 28
        inline_directory = model.options.global_scope and layout.row_height == 1
        # Share the directory column with the new-conversation row, while
        # reserving most of the available width for titles.
        directory_width = min(30, (node_col - 6) // 3)
        directory_col = node_col - directory_width - 2
        labels = []
        if model.options.include_archived:
            labels.append("+archived")
        if model.options.include_internal:
            labels.append("+internal")
        search_width = width - 4
        if labels:
            badges = " · ".join(labels)
            badges_col = width - display.columns(badges) - 3
            self.put(2, badges, curses.A_DIM, col=badges_col)
            search_width = badges_col - 4
        self.put(
            2,
            display.search_text(model.query, search_width, editing=self.searching),
            self.accent if self.searching else curses.A_DIM,
        )
        cursor = self.screen.getyx() if self.searching else None
        new_selected = model.selected < 0 or not model.rows
        new_style = curses.A_REVERSE if new_selected else 0
        if new_selected:
            self.put(layout.new_row, " " * (width - 4), new_style)
        new_title_style = new_style | curses.A_BOLD if new_selected else 0
        self.put(layout.new_row, "›" if new_selected else " ", new_title_style)
        self.put(layout.new_row, "+ New session", new_title_style, col=4)
        if model.options.global_scope:
            directory = (
                Path(self.settings.target.cwd).expanduser()
                if self.settings.target.cwd
                else model.options.directory
            )
            directory_row = layout.new_row if inline_directory else layout.new_row + 1
            if new_selected and not inline_directory:
                self.put(directory_row, " " * (width - 4), new_style)
            self.put(
                directory_row,
                display.fit(
                    display.directory_label(directory),
                    directory_width if inline_directory else width - 7,
                ),
                new_style if new_selected else curses.A_DIM,
                col=directory_col if inline_directory else 4,
            )
        visible = layout.page_size
        self.scroll_top = min(self.scroll_top, max(0, len(model.rows) - visible))
        if model.selected < self.scroll_top:
            self.scroll_top = max(0, model.selected)
        if model.selected >= self.scroll_top + visible:
            self.scroll_top = model.selected - visible + 1
        if not model.rows:
            message, hint = display.empty_catalog_lines(
                query=model.query,
                global_scope=model.options.global_scope,
                results=model.results,
                pending=self.pending,
            )
            self.put(layout.first_session_row, message, curses.A_DIM)
            if layout.first_session_row + 2 < layout.footer_row:
                self.put(layout.first_session_row + 2, hint, curses.A_DIM)
        for index, group in enumerate(
            model.rows[self.scroll_top : self.scroll_top + visible], self.scroll_top
        ):
            selected = index == model.selected
            copy = model.display_copy(group)
            title = model.display_title(group)
            node = self.node_label(copy.name) if copy else "Unavailable"
            age = display.relative_time(model.activity_ns(group))
            row = layout.first_session_row + (index - self.scroll_top) * layout.row_height
            # Selection follows the terminal palette and covers the full content row.
            style = curses.A_REVERSE if selected else 0
            title_style = style | curses.A_BOLD if selected else style
            metadata_style = style if selected else curses.A_DIM
            if selected:
                self.put(row, " " * (width - 4), style)
            self.put(row, "›" if selected else " ", title_style)
            second_line = layout.row_height == 2 and row + 1 < layout.footer_row
            if width >= 68:
                title_end = directory_col if inline_directory else node_col
                self.put(row, display.fit(title, title_end - 6), title_style, col=4)
                if inline_directory and copy:
                    directory = display.directory_label(Path(copy.entry["cwd"]))
                    self.put(
                        row,
                        display.fit(directory, directory_width),
                        metadata_style,
                        col=directory_col,
                    )
                self.put(row, display.fit(node, 11), metadata_style, col=node_col)
                self.put(row, age, metadata_style, col=time_col)
            else:
                self.put(row, display.fit(title, width - 7), title_style, col=4)
                metadata = node + " · " + age
                if model.options.global_scope and copy:
                    metadata += " · " + display.directory_label(Path(copy.entry["cwd"]))
                if second_line:
                    if selected:
                        self.put(row + 1, " " * (width - 4), style)
                    self.put(row + 1, display.fit(metadata, width - 7), metadata_style, col=4)
        if model.group and model.copy is None:
            self.put(3, "Source unavailable. Tab switches copies.", self.accent)
        self.draw_browser_footer(layout.footer_row, width)
        return cursor

    def draw_browser_footer(self, row: int, width: int) -> None:
        model, pending = self.model, self.pending
        self.put(row, "─" * (width - 4), curses.A_DIM)
        position = max(0, model.selected + 1) if model.rows else 0
        progress = (
            f" Session {position}/{len(model.rows)} "
            if position
            else f" {len(model.rows)} sessions "
        )
        progress_col = width - display.columns(progress) - 3
        self.put(row, progress, curses.A_DIM, col=progress_col)
        label = display.catalog_label(model.results, pending)
        self.put(
            row,
            display.fit(f" {label} ", progress_col - 4),
            self.accent if pending else curses.A_DIM,
            col=3,
        )
        col = 2
        for key, label in display.browser_hints(
            width - 4, global_scope=model.options.global_scope, searching=self.searching
        ):
            self.put(row + 1, f"[{key}]", curses.A_BOLD, col=col)
            col += display.columns(key) + 2
            self.put(row + 1, " " + label, curses.A_DIM, col=col)
            col += display.columns(label) + 3
        self.put(row + 2, "Nodes", curses.A_DIM)
        self.put(row + 2, "[n]", curses.A_BOLD, col=8)
        summary = display.node_summary(self.settings, model.results, pending)
        self.put(row + 2, display.fit(summary, width - 16), curses.A_DIM, col=13)

    def node_label(self, name: str) -> str:
        return self.local_name if name == "local" else name

    def escape_hint(self) -> str:
        if self.phase == "preparing":
            return "Cancelling…" if self.cancel.is_set() else "Esc Cancel"
        if self.phase in ("applying", "rebuilding"):
            return (
                "Returning to list when finished…"
                if self.back_requested
                else "Esc Back when finished"
            )
        if self.searching:
            return "Esc Clear & back"
        return "Esc Exit" if self.phase == "browse" else "Esc Back"

    def panel(self) -> tuple[str, list[str], str]:
        back = "Esc Back   ↑/↓ Scroll"
        if self.phase == "nodes":
            return (
                "Node status",
                display.node_lines(self.settings, self.model.results, self.pending),
                back,
            )
        if self.phase == "help":
            return "Help", display.help_lines(), back
        if self.phase == "details":
            return "Details", self.detail_lines(), back
        if self.phase == "confirm_loading" and self.pending_open:
            selection = self.pending_open
            return (
                "Open this copy?",
                [
                    display.fit(selection.title, self.screen.getmaxyx()[1] - 6),
                    f"Source: {self.node_label(selection.copy.name)}",
                    "",
                    "The list was still loading when you selected this copy.",
                    "A newer copy may be available.",
                ],
                "←/→ Choose   Enter Confirm   Esc Cancel",
            )
        if self.phase == "preparing":
            return (
                "Comparing histories…",
                [
                    "Comparing full histories. Transfers need confirmation; matching local copies open directly."
                ],
                self.escape_hint(),
            )
        if self.phase == "applying":
            return (
                "Syncing…",
                [
                    "Checking the destination, backing up files and rebuilding local indexes.",
                    "Please wait until this finishes.",
                ],
                self.escape_hint(),
            )
        if self.phase == "rebuilding":
            return (
                "Rebuilding local indexes…",
                ["The history files are already here. Finishing local indexing before opening."],
                self.escape_hint(),
            )
        if self.phase == "error":
            return (
                "Unable to open",
                [
                    display.fit(
                        self.message.splitlines()[0] if self.message else "Operation failed", 160
                    ),
                    "",
                    "Press d for the full error and activity log.",
                ],
                "←/→ Choose   Enter Confirm   d Details   Esc Back",
            )
        if not self.transfer:
            return "", [], back
        transfer = self.transfer
        if self.phase == "preview":
            lines = [
                display.fit(transfer.title, self.screen.getmaxyx()[1] - 6),
                f"{transfer.source} → {self.local_name}",
                "",
                *display.comparison_lines(transfer.prepared, transfer.id),
            ]
            return "Confirm sync", lines, "←/→ Choose   Enter Confirm   d Details   Esc Cancel"
        return (
            "Sync complete",
            [
                transfer.title,
                "",
                "Session saved locally. Press d to find backups and the activity log.",
            ],
            "d Details   Esc Back",
        )

    def detail_lines(self) -> list[str]:
        if self.transfer:
            lines = [
                self.transfer.title,
                f"{self.transfer.source} → {self.local_name}",
                f"Session: {self.transfer.id}",
                "",
                *display.plan_lines(self.transfer.prepared),
            ]
        else:
            group, copy = self.model.group, self.model.copy
            selection = (
                self.open_selection
                if (
                    self.phase == "error"
                    or (self.phase == "details" and self.return_phase == "error")
                )
                else None
            )
            if selection:
                copy = selection.copy
                group = next((row for row in self.model.rows if row.id == selection.id), None)
            lines = [f"Destination: {self.settings.target.home}"]
            if group:
                lines += [
                    f"Session: {group.id}",
                    "Copies: "
                    + ", ".join(
                        item.name + (" (archived)" if item.entry["archived"] else "")
                        for item in group.copies
                    ),
                ]
            elif selection:
                lines.append(f"Session: {selection.id}")
            if copy:
                entry = copy.entry
                lines += [
                    "",
                    f"Source: {copy.name}",
                    f"Title / preview: {copy.title}",
                    f"Original directory: {entry['cwd']}",
                    f"Type: {entry.get('source', 'unknown')}",
                    f"Activity: {display.date(copy.activity_ns)} ({entry.get('activity_kind', 'unknown')})",
                    f"File modified: {display.date(entry['modified_ns'])}",
                    f"Size: {entry['size']:,} bytes",
                ]
                if entry["changing"]:
                    lines.append(
                        "The file changed during scanning. Close Codex on the source before syncing."
                    )
            lines += [
                "",
                "Times and previews are display hints. Transfers compare full histories.",
                "Names come from available JSONL hints and may differ from Codex titles.",
            ]
        if self.report:
            lines += ["", f"Activity log: {self.report}"]
        if self.message:
            lines += ["", self.message]
        if self.notes:
            lines += ["", "Progress:", *self.notes]
        return lines

    @property
    def cancel_button_index(self) -> int:
        """Put Sync & open and Retry first; other confirmations start with Cancel."""
        if self.phase == "error" or (
            self.phase == "preview" and not self.transfer.prepared.has_conflicts
        ):
            return 1
        return 0

    def draw_panel(self, height: int, width: int) -> None:
        title, raw, keys = self.panel()
        self.put(3, title, self.accent)
        lines = display.wrap(raw, width - 6)
        confirmation = self.phase in ("preview", "confirm_loading", "error")
        visible = max(1, height - (11 if confirmation else 9))
        self.page_offset = min(self.page_offset, max(0, len(lines) - visible))
        for row, line in enumerate(lines[self.page_offset : self.page_offset + visible], 5):
            self.put(row, line)
        if len(lines) > visible:
            self.put(height - (6 if confirmation else 4), "↑/↓ More", curses.A_DIM)
        if confirmation:
            action = "Continue"
            cancel = "Cancel"
            if self.phase == "preview":
                action = (
                    "Save incoming copy" if self.transfer.prepared.has_conflicts else "Sync & open"
                )
            elif self.phase == "error":
                action, cancel = "Retry", "Back"
            col = 2
            buttons = (action, cancel) if self.cancel_button_index == 1 else (cancel, action)
            for index, label in enumerate(buttons):
                text = f"[ {label} ]"
                style = (
                    self.accent | curses.A_REVERSE if index == self.confirm_choice else curses.A_DIM
                )
                self.put(height - 4, text, style, col=col)
                col += display.columns(text) + 4
            if width < 68:
                keys = (
                    "←/→ Choose  Enter  d Details  Esc Back"
                    if self.phase == "error"
                    else "←/→ Choose  Enter Confirm  Esc Cancel"
                )
        self.put(height - 2, keys, curses.A_DIM)

    def draw_directory(self, height: int, width: int) -> tuple[int, int] | None:
        prompt = self.directory_prompt
        if self.phase == "directory_input":
            self.put(2, "Choose another directory", self.accent)
            self.put(4, "Enter an existing local directory.", curses.A_DIM)
            text, cursor = display.input_text(prompt.text, prompt.cursor, width - 6)
            self.put(6, "> ")
            self.put(6, text, col=4)
            for row, line in enumerate(display.wrap([prompt.error], width - 6), 8):
                if row < height - 2:
                    self.put(row, line, self.accent)
            self.put(height - 2, "Enter Continue  Ctrl-U Clear  Esc Back", curses.A_DIM)
            return 6, 4 + cursor

        title = "Choose working directory"
        if prompt.ancestors:
            title = "Choose directory for these sessions"
        self.put(2, title, self.accent)
        self.put(
            4,
            "Directory unavailable on this machine."
            if prompt.missing
            else "Session and current directories differ.",
            curses.A_DIM,
        )
        self.put(5, display.fit(prompt.missing or prompt.session, width - 5), curses.A_DIM)
        compact = height < 18
        if prompt.ancestors and not compact:
            self.put(6, "Applies to this session and its ancestors.", curses.A_DIM)
        labels = {
            "session": "Use session directory",
            "current": "Use current directory",
            "other": "Choose another directory…",
            "back": "Back",
        }
        for index, choice in enumerate(prompt.choices):
            row = (6 + index) if compact else (8 + index * 2)
            selected = index == prompt.selected
            style = self.accent | curses.A_REVERSE if selected else 0
            path = {"session": prompt.session, "current": str(prompt.current)}.get(choice)
            label = labels[choice]
            if compact and path:
                label += f" ({display.directory_label(Path(path))})"
            self.put(row, "›" if selected else " ", style)
            self.put(row, display.fit(label, width - 7), style, col=4)
            if path and not compact:
                self.put(
                    row + 1,
                    display.fit(display.directory_label(Path(path)), width - 7),
                    curses.A_DIM,
                    col=4,
                )
        self.put(height - 2, "↑/↓ Choose   Enter Continue   Esc Back", curses.A_DIM)
        return None

    def offer_directory(
        self, directory: str, *, explicit: bool = False, missing: str | None = None
    ) -> bool:
        current = self.model.options.directory
        try:
            resolved = Path(codex.map_cwd(directory, ()))
        except codex.DirectoryUnavailable:
            missing = directory
        else:
            if not missing and (explicit or resolved == current.resolve()):
                return False
        self.directory_prompt = DirectoryPrompt(
            directory,
            current,
            missing,
            bool(self.transfer and len(self.transfer.prepared.heads) > 1),
        )
        self.phase = "directory"
        return True

    def choose_transfer_directory(self) -> bool:
        prepared = self.transfer.prepared
        if prepared.has_conflicts:
            return False  # Saving a conflicting bundle does not launch a session.
        missing = None
        for directory in prepared.directories.values():
            try:
                codex.map_cwd(directory, ())
            except codex.DirectoryUnavailable:
                missing = directory
                break
        directory = prepared.directories[self.transfer.id]
        return self.offer_directory(
            directory,
            explicit=bool(prepared.target.cwd) or directory != prepared.heads[self.transfer.id].cwd,
            missing=missing,
        )

    def use_directory(self, value: str) -> None:
        prompt = self.directory_prompt
        try:
            if not value.strip():
                raise ValueError("Enter a directory.")
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = prompt.current / path
            directory = codex.map_cwd(str(path), ())
        except (ValueError, RuntimeError, OSError, codex.DirectoryUnavailable):
            if self.phase == "directory":
                prompt.text, prompt.cursor = value, len(value)
            prompt.error = "Directory unavailable. Enter an existing local path."
            self.phase = "directory_input"
            return
        self.directory_prompt = None
        if self.transfer:
            self.reprepare_transfer(replace(self.transfer.prepared.target, cwd=directory))
        else:
            self.open_target = replace(self.open_target, cwd=directory)
            self.open_session(self.open_selection)

    def directory_key(self, key) -> None:
        prompt = self.directory_prompt
        if self.phase == "directory_input":
            if key == "\x1b":
                self.phase = "directory"
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self.use_directory(prompt.text)
            elif key in (curses.KEY_LEFT, curses.KEY_RIGHT):
                prompt.cursor = max(
                    0, min(len(prompt.text), prompt.cursor + (1 if key == curses.KEY_RIGHT else -1))
                )
            elif key in (curses.KEY_HOME, "\x01"):
                prompt.cursor = 0
            elif key in (curses.KEY_END, "\x05"):
                prompt.cursor = len(prompt.text)
            elif key == "\x15":
                prompt.text, prompt.cursor = "", 0
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if prompt.cursor:
                    prompt.text = prompt.text[: prompt.cursor - 1] + prompt.text[prompt.cursor :]
                    prompt.cursor -= 1
            elif key == curses.KEY_DC:
                prompt.text = prompt.text[: prompt.cursor] + prompt.text[prompt.cursor + 1 :]
            elif isinstance(key, str) and key.isprintable() and len(prompt.text) < 4096:
                prompt.text = prompt.text[: prompt.cursor] + key + prompt.text[prompt.cursor :]
                prompt.cursor += len(key)
            return
        if key in (curses.KEY_DOWN, "j", "\t"):
            prompt.selected = (prompt.selected + 1) % len(prompt.choices)
        elif key in (curses.KEY_UP, "k", curses.KEY_BTAB):
            prompt.selected = (prompt.selected - 1) % len(prompt.choices)
        elif key == "\x1b":
            self.back()
        elif key in ("\n", "\r", curses.KEY_ENTER):
            choice = prompt.choices[prompt.selected]
            if choice == "other":
                prompt.error = ""
                self.phase = "directory_input"
            elif choice == "back":
                self.back()
            else:
                self.use_directory(prompt.session if choice == "session" else str(prompt.current))

    def open_selected(self) -> None:
        """Capture one source now; later inventory arrivals cannot retarget this action."""
        group, copy = self.model.group, self.model.copy
        if group is not None and copy is None:
            return
        self.open_target = self.settings.target
        selection = (
            SessionSelection(group.id, copy, self.model.display_title(group)) if group else None
        )
        if selection is not None and self.pending:
            self.pending_open = selection
            self.phase = "confirm_loading"
            self.confirm_choice = 0
            self.page_offset = 0
        else:
            self.open_session(selection)

    def open_session(self, selection: SessionSelection | None) -> None:
        """Open the captured choice; None starts a new conversation."""
        self.open_selection = selection
        self.rebuild_pending = False
        self.message, self.notes, self.report = "", [], None
        try:
            if selection is None:
                self.launch_request = launcher.new_session(
                    self.open_target, self.model.options.directory
                )
            else:
                copy = selection.copy
                if copy.node is not None:
                    self.prepare_session(selection)
                else:
                    original = copy.entry.get("original_cwd", copy.entry["cwd"])
                    directory = launcher.session_directory(self.open_target, selection.id, original)
                    if not copy.entry["archived"] and self.offer_directory(
                        directory,
                        explicit=bool(self.open_target.cwd) or directory != original,
                    ):
                        return
                    self.launch_request = launcher.resume_session(
                        self.open_target,
                        selection.id,
                        original,
                        archived=copy.entry["archived"],
                    )
        except Exception as exc:
            self.show_error(exc)

    def show_error(self, exc: Exception) -> None:
        if isinstance(exc, codex.DirectoryUnavailable):
            self.offer_directory(exc.directory, missing=exc.directory)
            return
        self.phase, self.message = "error", str(exc)
        self.confirm_choice = 0
        self.page_offset = 0
        self.back_requested = False

    def retry_open(self) -> None:
        if self.transfer:
            self.reprepare_transfer(self.transfer.prepared.target)
        else:
            self.open_session(self.open_selection)

    def reprepare_transfer(self, target: Target) -> None:
        previous = self.transfer

        def prepare():
            self.events.put("Rechecking downloaded history against this machine…")
            manager = service.prepare_file(previous.prepared.bundle, target)
            prepared = manager.__enter__()
            return Transfer(manager, prepared, previous.id, previous.source, previous.title)

        # Refresh the plan against the destination; never reuse a failed write's
        # stale comparison or switch to a different copy from a refreshed list.
        self.start_prepare(prepare)

    def resume_transfer(self) -> None:
        transfer = self.transfer
        head = transfer.prepared.heads[transfer.id]
        change = next(
            item for item in transfer.prepared.changes if item.rollout_id == head.rollout_id
        )
        self.launch_request = launcher.resume_session(
            transfer.prepared.target,
            transfer.id,
            head.cwd,
            archived=change.destination.is_relative_to(
                self.settings.target.home / "archived_sessions"
            ),
        )

    def prepare_session(self, selection: SessionSelection) -> None:
        copy = selection.copy
        if copy.node is None:
            return

        def prepare():
            manager = service.prepare_pull(
                copy.node,
                self.open_target,
                (selection.id,),
                timeout=self.settings.transfer_timeout,
                cancel=self.cancel,
                progress=self.events.put,
            )
            prepared = manager.__enter__()
            return Transfer(manager, prepared, selection.id, copy.name, selection.title)

        self.start_prepare(prepare)

    def start_prepare(self, prepare: Callable[[], Transfer]) -> None:
        self.cancel.clear()
        self.back_requested = False
        self.message, self.notes, self.report = "", [], None
        self.phase = "preparing"
        self.page_offset = 0
        self.task_kind = "prepare"
        self.task = self.worker.submit(prepare)

    def apply_transfer(self) -> None:
        self.phase = "applying"
        self.task_kind = "apply"
        self.task = self.worker.submit(
            service.apply, self.transfer.prepared, progress=self.events.put
        )

    def back(self) -> None:
        if self.phase == "details":
            self.phase = self.return_phase
        else:
            if self.transfer:
                self.transfer.close()
                self.transfer = None
            self.phase = "browse"
            self.pending_open = None
            self.open_selection = None
            self.open_target = self.settings.target
            self.directory_prompt = None
            self.rebuild_pending = False
            self.back_requested = False
            self.message, self.notes, self.report = "", [], None
            self.confirm_choice = 0
        self.page_offset = 0

    def key(self, key) -> bool:
        if self.task:
            if key == "\x1b":
                # Cancel read-only work; an approved write must finish before returning.
                self.back_requested = True
                if self.task_kind == "prepare":
                    self.cancel.set()
            return True
        if self.phase in ("directory", "directory_input"):
            height, width = self.screen.getmaxyx()
            if key == "\x1b" or (height >= 12 and width >= 42):
                self.directory_key(key)
            return True
        if self.searching:
            if key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                self.model.move(self.browser_layout().page_size if key == curses.KEY_NPAGE else 1)
            elif key in (curses.KEY_UP, curses.KEY_PPAGE):
                self.model.move(-self.browser_layout().page_size if key == curses.KEY_PPAGE else -1)
            elif key == "\x1b":
                self.model.search("")
                self.searching = False
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self.searching = False
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.model.search(self.model.query[:-1])
            elif isinstance(key, str) and key.isprintable() and len(self.model.query) < 256:
                self.model.search(self.model.query + key)
            return True
        if key == "\x1b":
            if self.phase == "browse":
                return False
            self.back()
            return True
        if min(self.screen.getmaxyx()[0] - 12, self.screen.getmaxyx()[1] - 42) < 0:
            return True
        if self.phase != "browse":
            if key in (curses.KEY_DOWN, "j", curses.KEY_NPAGE):
                self.page_offset += 5 if key == curses.KEY_NPAGE else 1
            elif key in (curses.KEY_UP, "k", curses.KEY_PPAGE):
                self.page_offset = max(0, self.page_offset - (5 if key == curses.KEY_PPAGE else 1))
            elif key == "d" and self.phase in ("preview", "result", "error"):
                self.return_phase, self.phase = self.phase, "details"
                self.page_offset = 0
            elif self.phase in ("preview", "confirm_loading", "error") and key in (
                curses.KEY_LEFT,
                curses.KEY_RIGHT,
                "\t",
            ):
                if key == "\t":
                    self.confirm_choice = 1 - self.confirm_choice
                else:
                    self.confirm_choice = int(key == curses.KEY_RIGHT)
            elif key in ("\n", "\r", curses.KEY_ENTER) and self.phase in (
                "preview",
                "confirm_loading",
                "error",
            ):
                if self.confirm_choice == self.cancel_button_index:
                    self.back()
                elif self.phase == "error":
                    self.retry_open()
                elif self.phase == "confirm_loading" and self.pending_open:
                    selection, self.pending_open = self.pending_open, None
                    self.open_session(selection)
                elif self.phase == "preview" and self.transfer:
                    self.apply_transfer()
            return True
        if key in (curses.KEY_DOWN, "j", curses.KEY_NPAGE):
            self.model.move(self.browser_layout().page_size if key == curses.KEY_NPAGE else 1)
        elif key in (curses.KEY_UP, "k", curses.KEY_PPAGE):
            self.model.move(-self.browser_layout().page_size if key == curses.KEY_PPAGE else -1)
        elif key == "\t":
            self.model.cycle_source()
        elif key == "/":
            self.searching = True
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.open_selected()
        elif key == "+":
            self.model.selected = -1
            self.open_selected()
        elif key in ("n", "?", "d"):
            self.phase = {"n": "nodes", "?": "help", "d": "details"}[key]
            self.return_phase = "browse"
            self.page_offset = 0
        elif key in ("g", "a"):
            name = {"g": "global_scope", "a": "include_archived"}[key]
            setattr(self.model.options, name, not getattr(self.model.options, name))
            self.model.rebuild()
        elif key == "r":
            self.refresh_catalog()
        return True

    def close(self) -> None:
        self.cancel.set()
        self.scan.close()
        # An in-progress apply finishes before its private snapshot is removed.
        self.worker.shutdown(wait=True, cancel_futures=True)
        if self.task and self.task_kind == "prepare" and not self.task.cancelled():
            try:
                self.task.result().close()
            except Exception:
                pass
        if self.transfer:
            self.transfer.close()
            self.transfer = None

    def run(self) -> launcher.LaunchRequest | None:
        # Escape also starts terminal key sequences. Avoid ncurses' one-second
        # default wait, but honor ESCDELAY when a connection needs more time.
        if "ESCDELAY" not in os.environ:
            curses.set_escdelay(25)
        self.screen.timeout(100)
        self.screen.keypad(True)
        try:
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_CYAN, -1)
                self.accent = curses.color_pair(1) | curses.A_BOLD
        except curses.error:
            pass
        try:
            while True:
                self.poll()
                if self.launch_request is not None:
                    break
                self.draw()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    continue
                if not self.key(key):
                    break
        finally:
            self.close()
        return self.launch_request


def run(settings: Settings, options: BrowseOptions | None = None) -> launcher.LaunchRequest | None:
    # curses.wrapper restores the terminal before CLI invokes launcher.handoff.
    return curses.wrapper(lambda screen: Browser(screen, settings, options).run())
