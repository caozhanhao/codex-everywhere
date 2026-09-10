"""Presentation-independent browsing state. Filters never enter the export plan."""

import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from .config import mapped_directory as mapped_directory
from .config import merged_mappings
from .fleet import Copy, Group, NodeResult, groups


@dataclass
class BrowseOptions:
    directory: Path = field(default_factory=Path.cwd)
    global_scope: bool = False
    include_archived: bool = False
    include_internal: bool = False


class BrowserModel:
    def __init__(
        self,
        options: BrowseOptions,
        mappings: tuple[tuple[str, str], ...] = (),
        *,
        local_name: str = "local",
    ):
        self.options = options
        self.local_name = local_name
        self.mappings = mappings
        self.results: dict[str, NodeResult] = {}
        self.rows: list[Group] = []
        self.query = ""
        self.selected = 0
        # Only explicit source choices are pinned. Automatic choices can follow new
        # inventory results; a transfer captures its source when Enter is pressed.
        self.source_names: dict[str, str] = {}

    @property
    def group(self) -> Group | None:
        return self.rows[self.selected] if 0 <= self.selected < len(self.rows) else None

    @property
    def source_name(self) -> str | None:
        if not self.group:
            return None
        copy = self.display_copy(self.group)
        return copy.name if copy else self.source_names.get(self.group.id)

    @property
    def copy(self) -> Copy | None:
        return self.display_copy(self.group) if self.group else None

    def display_copy(self, group: Group) -> Copy | None:
        copies = self.sources(group)
        if group.id in self.source_names:
            name = self.source_names[group.id]
            return next((copy for copy in copies if copy.name == name), None)
        # Activity picks a browsing candidate, never authorizes an overwrite.
        # Prefer local only on equal timestamps, avoiding needless SSH reads.
        return min(
            copies,
            key=lambda copy: (-(copy.activity_ns or 0), copy.node is not None, copy.name),
            default=None,
        )

    def display_title(self, group: Group) -> str:
        copy = self.display_copy(group)
        if copy and copy.entry.get("title"):
            return copy.entry["title"]
        # A transferred copy may have no local name index. Keep a known name for
        # this UUID visible without changing the chosen history source.
        return next(
            (item.entry["title"] for item in self.sources(group) if item.entry.get("title")),
            copy.title if copy else group.summary,
        )

    def sources(self, group: Group) -> tuple[Copy, ...]:
        return tuple(copy for copy in group.copies if self.visible(copy))

    def activity_ns(self, group: Group) -> int | None:
        return max(
            (copy.activity_ns for copy in self.sources(group) if copy.activity_ns is not None),
            default=None,
        )

    def visible(self, copy: Copy) -> bool:
        entry, options = copy.entry, self.options
        if entry["archived"] and not options.include_archived:
            return False
        if not options.include_internal and entry.get("source") not in ("cli", "vscode"):
            return False
        mappings = self.mappings
        if copy.node is not None:
            mappings = merged_mappings(copy.node.mappings, self.mappings)
        elif "original_cwd" in entry:
            mappings = ()  # A remembered directory is already local.
        return options.global_scope or posixpath.normpath(
            mapped_directory(entry["cwd"], mappings)
        ) == posixpath.normpath(str(options.directory))

    def rebuild(self) -> None:
        previous_id = self.group.id if self.group else None
        new_selected = self.selected < 0
        self.rows = [
            group
            for group in groups(self.results, self.query, local_name=self.local_name)
            if self.sources(group)
        ]
        # One timeline for every visible copy, independent of its location or
        # the source explicitly chosen with Tab. UUID keeps row identity stable.
        self.rows.sort(key=lambda group: (-(self.activity_ns(group) or 0), group.id))
        self.selected = (
            -1
            if new_selected
            else next((i for i, group in enumerate(self.rows) if group.id == previous_id), 0)
        )

    def ingest(self, results: list[NodeResult]) -> None:
        self.results.update((result.name, result) for result in results)
        self.rebuild()

    def move(self, delta: int) -> None:
        self.selected = max(-1, min(len(self.rows) - 1, self.selected + delta))

    def cycle_source(self) -> None:
        if self.group:
            names = [copy.name for copy in self.sources(self.group)]
            index = names.index(self.source_name) if self.source_name in names else -1
            self.source_names[self.group.id] = names[(index + 1) % len(names)]
            self.rebuild()

    def search(self, query: str) -> None:
        self.query = query
        if query and self.selected < 0:
            self.selected = 0
        self.rebuild()
