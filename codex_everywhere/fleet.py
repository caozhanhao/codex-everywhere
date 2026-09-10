"""Concurrent read-only inventory and presentation-independent UUID grouping."""

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from . import reader, transport
from .config import Node, Settings


@dataclass(frozen=True)
class NodeResult:
    name: str
    node: Node | None
    sessions: tuple[dict, ...] = ()
    issues: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class Copy:
    node: Node | None
    entry: dict

    @property
    def title(self) -> str:
        return self.entry.get("title") or self.entry["summary"] or "Untitled session"

    @property
    def activity_ns(self) -> int | None:
        return self.entry.get("activity_ns")

    @property
    def name(self) -> str:
        return self.node.name if self.node else "local"


@dataclass(frozen=True)
class Group:
    id: str
    copies: tuple[Copy, ...]

    @property
    def summary(self) -> str:
        return next(
            (copy.entry["title"] for copy in self.copies if copy.entry.get("title")),
            next(
                (copy.entry["summary"] for copy in self.copies if copy.entry["summary"]),
                "Untitled session",
            ),
        )

    @property
    def activity_ns(self) -> int:
        return max((copy.activity_ns or 0 for copy in self.copies), default=0)


def groups(
    results: dict[str, NodeResult], query: str = "", *, local_name: str = "local"
) -> list[Group]:
    by_id: dict[str, list[Copy]] = {}
    for name in sorted(results):
        result = results[name]
        for entry in result.sessions:
            by_id.setdefault(entry["id"], []).append(Copy(result.node, entry))
    output = []
    for thread_id, copies in by_id.items():
        group = Group(thread_id, tuple(copies))
        haystack = " ".join(
            [
                thread_id,
                *(
                    copy.name
                    + " "
                    + (local_name if copy.node is None else "")
                    + " "
                    + copy.entry["cwd"]
                    + " "
                    + copy.title
                    + " "
                    + copy.entry["summary"]
                    for copy in copies
                ),
            ]
        )
        if query.casefold() in haystack.casefold():
            output.append(group)
    return sorted(output, key=lambda group: (-group.activity_ns, group.id))


class ScanJob:
    """Each failed node has its own result; callers consume completed nodes incrementally."""

    def __init__(self, settings: Settings):
        self.cancel = threading.Event()
        self.executor = ThreadPoolExecutor(
            max_workers=settings.workers, thread_name_prefix="inventory"
        )
        self.pending: dict[Future, Node | None] = {
            self.executor.submit(reader.scan, settings.target.home): None
        }
        for node in settings.nodes:
            self.pending[
                self.executor.submit(transport.inventory, node, settings.scan_timeout, self.cancel)
            ] = node

    def poll(self) -> list[NodeResult]:
        completed = []
        for future, node in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[future]
            name = node.name if node else "local"
            try:
                data = future.result()
                completed.append(
                    NodeResult(name, node, tuple(data["sessions"]), tuple(data["issues"]))
                )
            except Exception as exc:
                completed.append(NodeResult(name, node, error=str(exc)))
        return completed

    def close(self) -> None:
        self.cancel.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
