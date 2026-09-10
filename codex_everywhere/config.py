"""Explicit node and destination configuration; no discovery or writes on import."""

import json
import os
import re
import socket
from dataclasses import dataclass, replace
from pathlib import Path

from .reader import SyncError

_SOURCE_ROOT = Path(__file__).resolve().parent.parent


def _check_remote_home(value: object) -> None:
    if not isinstance(value, str) or not value.startswith(("/", "~/")):
        raise SyncError("Set remote_home globally or per node to an absolute path or a ~/ path.")


@dataclass(frozen=True)
class Node:
    name: str
    host: str
    home: str
    mappings: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if (
            not isinstance(self.name, str)
            or not self.name
            or not isinstance(self.host, str)
            or not re.fullmatch(r"[A-Za-z0-9_@.:%\[\]-]+", self.host)
            or self.host.startswith("-")
        ):
            raise SyncError("Use a hostname, user@hostname, or SSH config alias.")
        _check_remote_home(self.home)


@dataclass(frozen=True)
class Target:
    home: Path
    sqlite_home: Path | None = None
    codex: str = "codex"
    mappings: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None

    def for_node(self, node: Node) -> "Target":
        return replace(self, mappings=merged_mappings(node.mappings, self.mappings))


@dataclass(frozen=True)
class Settings:
    nodes: tuple[Node, ...]
    target: Target
    scan_timeout: float = 25
    transfer_timeout: float = 600
    workers: int = 4


def parse_mappings(values: list[str]) -> tuple[tuple[str, str], ...]:
    result = []
    for value in values:
        old, separator, new = value.partition("=")
        if not separator or not old or not new or "\0" in value:
            raise SyncError("--map needs OLD=NEW with two nonempty paths.")
        result.append((old, new))
    return tuple(result)


def configured_mappings(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or not all(isinstance(v, str) and v and "\0" not in v for v in pair)
        for pair in value
    ):
        raise SyncError("mappings must be a list of [old, new] path pairs.")
    return tuple((old, new) for old, new in value)


def merged_mappings(configured: tuple, overrides: tuple) -> tuple[tuple[str, str], ...]:
    """Command-line rules replace configured rules with the same source prefix."""
    return tuple(dict([*configured, *overrides]).items())


def mapped_directory(value: str, mappings: tuple[tuple[str, str], ...]) -> str:
    """Map complete prefixes lexically; foreign symlinks cannot be resolved here."""
    normalized = value.replace("\\", "/").rstrip("/")
    for old, new in sorted(mappings, key=lambda pair: len(pair[0]), reverse=True):
        old = old.replace("\\", "/").rstrip("/")
        if normalized == old or normalized.startswith(old + "/"):
            normalized = str(Path(new).expanduser() / normalized[len(old) :].lstrip("/"))
            break
    if not normalized and value.startswith("/"):
        normalized = "/"
    return normalized


def _configured_nodes(data: dict, local_name: str) -> tuple[Node, ...]:
    """Normalize SSH hosts and node objects without expanding remote paths."""
    remote_home = data.get("remote_home")
    if remote_home is not None:
        _check_remote_home(remote_home)
    rows = data.get("nodes", [])
    if not isinstance(rows, list):
        raise SyncError("nodes must be a list of SSH host strings or node objects.")
    nodes = []
    for row in rows:
        if isinstance(row, str):
            row = {"host": row}
        if (
            not isinstance(row, dict)
            or "host" not in row
            or row.keys() - {"name", "host", "remote_home", "mappings"}
        ):
            raise SyncError("Each node needs host and optional name, remote_home and mappings.")
        node = Node(
            row.get("name", row["host"]),
            row["host"],
            row.get("remote_home", remote_home),
            configured_mappings(row.get("mappings", [])),
        )
        if node.name != local_name and node.host.split("@")[-1].split(".")[0] != local_name:
            nodes.append(node)
    if len({node.name for node in nodes}) != len(nodes) or any(
        node.name == "local" for node in nodes
    ):
        raise SyncError("Node names must be unique and cannot be 'local'.")
    return tuple(nodes)


def default_path() -> Path:
    """Prefer a private checkout config, then the user's installed-app config.

    Resolve the checkout from this module, never from the working directory of
    the project being browsed. Merely entering a project must not select SSH hosts.
    """
    checkout_config = _SOURCE_ROOT / "config.json"
    if (_SOURCE_ROOT / "config.example.json").is_file() and checkout_config.is_file():
        return checkout_config
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return root / "codex-everywhere/config.json"


def load(
    path: Path | None = None,
    *,
    home: str | None = None,
    sqlite_home: str | None = None,
    codex: str | None = None,
    mappings: tuple = (),
    cwd: str | None = None,
) -> Settings:
    explicit = path is not None
    path = path or default_path()
    data = json.loads(path.read_text()) if explicit or path.exists() else {}
    if isinstance(data, dict) and "mappings" in data:
        raise SyncError("Move top-level mappings into nodes[].mappings.")
    if not isinstance(data, dict) or set(data) - {
        "nodes",
        "remote_home",
        "home",
        "sqlite_home",
        "codex",
        "scan_timeout",
        "transfer_timeout",
        "workers",
    }:
        raise SyncError("Invalid configuration or unknown configuration key.")
    nodes = _configured_nodes(data, socket.gethostname().split(".")[0])
    env_home = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    target_home = Path(home or data.get("home") or env_home).expanduser().resolve()
    env_sqlite = (
        os.environ.get("CODEX_SQLITE_HOME")
        if target_home == Path(env_home).expanduser().resolve()
        else None
    )
    sql = sqlite_home or data.get("sqlite_home") or env_sqlite
    target = Target(
        target_home,
        Path(sql).expanduser().resolve() if sql else None,
        codex or data.get("codex", "codex"),
        mappings,
        cwd,
    )
    settings = Settings(
        nodes,
        target,
        float(data.get("scan_timeout", 25)),
        float(data.get("transfer_timeout", 600)),
        int(data.get("workers", 4)),
    )
    if (
        not 1 <= settings.workers <= 16
        or not 1 <= settings.scan_timeout <= 300
        or not 1 <= settings.transfer_timeout <= 86400
    ):
        raise SyncError("Invalid worker count or timeout.")
    return settings
