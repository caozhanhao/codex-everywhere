"""Read-only planning. UUID matches identity; bytes decide version ordering."""

import datetime
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePosixPath

from .reader import RolloutIndex, Session, SyncError, inspect_session, rollout_ids, session_heads
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

    @property
    def rollout_id(self) -> str:
        return rollout_ids(self.destination)[1]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "rollout_id": self.rollout_id,
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
    index = RolloutIndex(home)
    heads = session_heads(sessions)
    related = sorted(set(heads) & set(index.threads))
    local, _ = index.collect(related) if related else ({}, [])
    local_heads = session_heads(local)
    plan = []
    for rollout_id in order:
        incoming = sessions[rollout_id]
        source = stage / incoming.relative
        # Exact dependency IDs also collide across owners; never silently overwrite one.
        dest = (
            index.path(rollout_id)
            if rollout_id in index.rollouts
            else new_destination(home, incoming)
        )
        safe_destination(home, dest)
        action = Action.ADD
        old_sha = None
        if rollout_id in index.rollouts:
            old = inspect_session(dest, dest.relative_to(home).as_posix())
            if old.id != incoming.id:
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
        plan.append(Change(incoming.id, action, dest, incoming.sha256, old_sha))
    # Different tips can otherwise look like independent file additions. Require a
    # complete extension of the retained tip; a revert across existing history is
    # a conflict, not permission to silently switch the destination's conversation.
    for position, change in enumerate(plan):
        incoming = heads[change.id]
        old = local_heads.get(change.id)
        if (
            change.rollout_id != incoming.rollout_id
            or old is None
            or old.rollout_id == incoming.rollout_id
        ):
            continue
        if inherited_bytes(incoming, old.rollout_id, sessions) >= old.size:
            continue
        if inherited_bytes(old, incoming.rollout_id, local) >= incoming.size:
            if change.action in (Action.SAME, Action.LOCAL_NEWER):
                plan[position] = replace(change, action=Action.LOCAL_NEWER)
        else:
            plan[position] = replace(change, action=Action.CONFLICT)
    return tuple(plan)


def inherited_bytes(head: Session, rollout_id: str, sessions: dict[str, Session]) -> int:
    """How much of this exact rollout the head retains (zero on another branch)."""
    current = head
    while current.history_base:
        base = current.history_base
        if base["thread_id"] == rollout_id:
            return base["end_byte_offset"]
        current = sessions[base["thread_id"]]
    return 0
