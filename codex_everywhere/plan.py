"""Read-only planning. UUID matches identity; bytes decide version ordering."""

import datetime
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath

from .reader import Session, SyncError, inspect_session, paths_by_id
from .safety import safe_destination


class Action(str, Enum):
    ADD = "add"
    UPDATE = "update"
    SAME = "same"
    LOCAL_NEWER = "local-newer"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class Change:
    id: str
    action: Action
    destination: Path
    incoming_sha: str
    old_sha: str | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "action": self.action.value,
            "destination": str(self.destination),
            "incoming_sha": self.incoming_sha,
            "old_sha": self.old_sha,
        }


def prefix(short: Path, long: Path) -> bool:
    with short.open("rb") as a, long.open("rb") as b:
        for chunk in iter(lambda: a.read(1024 * 1024), b""):
            if b.read(len(chunk)) != chunk:
                return False
    return True


def new_destination(home: Path, incoming: Session) -> Path:
    relative = PurePosixPath(incoming.relative)
    if relative.parts[0] == "archived_sessions":
        match = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})", relative.name)
        if not match:
            raise SyncError(f"Cannot derive active rollout path: {relative.name}")
        datetime.date(*(int(part) for part in match.groups()))
        # Codex refuses to resume an archived ancestor. A newly imported archive is
        # restored as active so its full history can be indexed before its children.
        return home.joinpath("sessions", *match.groups(), relative.name)
    return home / incoming.relative


def make_plan(
    home: Path, stage: Path, sessions: dict[str, Session], order: list[str]
) -> tuple[Change, ...]:
    local = paths_by_id(home)
    plan = []
    for thread_id in order:
        incoming = sessions[thread_id]
        source = stage / incoming.relative
        dest = local.get(thread_id, new_destination(home, incoming))
        safe_destination(home, dest)
        action = Action.ADD
        old_sha = None
        if thread_id in local:
            old = inspect_session(dest, dest.relative_to(home).as_posix())
            if old.id != thread_id:
                raise SyncError(f"Local filename and metadata ID disagree: {dest}")
            old_sha = old.sha256
            if old.sha256 == incoming.sha256:
                action = Action.SAME
            elif old.size < incoming.size and prefix(dest, source):
                action = Action.UPDATE
            elif old.size > incoming.size and prefix(source, dest):
                action = Action.LOCAL_NEWER
            else:
                action = Action.CONFLICT
        plan.append(Change(thread_id, action, dest, incoming.sha256, old_sha))
    return tuple(plan)
