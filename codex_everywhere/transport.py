"""One SSH connection per operation; remote execution is the read-only worker.

Nonblocking pipes bound output, preserve stderr for diagnostics, and let an
unreachable or cancelled node finish without holding up the fleet browser.
"""

import io
import json
import os
import selectors
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO

from . import reader
from .config import Node
from .reader import MAX_BUNDLE, SyncError

CATALOG_LIMIT = 16 * 1024**2


def command(node: Node, operation: str, sessions: tuple[str, ...] = ()) -> list[str]:
    if operation not in ("scan", "export"):
        raise SyncError("Transport only permits scan and export.")
    ids = [reader.canonical_id(value) for value in sessions]
    remote = ["python3", "-B", "-", f"--remote-name={node.name}", operation, node.home, *ids]
    # Host-key checking and rotation follow the user's SSH configuration.
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "--",
        node.host,
        shlex.join(remote),
    ]


def receive(
    node: Node,
    operation: str,
    output: BinaryIO,
    *,
    sessions: tuple[str, ...] = (),
    timeout: float = 25,
    cancel: threading.Event | None = None,
) -> None:
    payload = memoryview(Path(reader.__file__).read_bytes())
    limit = CATALOG_LIMIT if operation == "scan" else MAX_BUNDLE + CATALOG_LIMIT
    errors = bytearray()
    total = 0
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        command(node, operation, sessions),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as process:
        assert process.stdin and process.stdout and process.stderr
        try:
            with selectors.DefaultSelector() as selector:
                for stream, event in (
                    (process.stdin, selectors.EVENT_WRITE),
                    (process.stdout, selectors.EVENT_READ),
                    (process.stderr, selectors.EVENT_READ),
                ):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, event)
                while selector.get_map():
                    if cancel and cancel.is_set():
                        raise SyncError("Read cancelled.")
                    if time.monotonic() >= deadline:
                        raise SyncError(f"{node.name}: {operation} timed out after {timeout:g}s.")
                    for key, _ in selector.select(timeout=0.2):
                        stream = key.fileobj
                        if stream is process.stdin:
                            try:
                                written = os.write(stream.fileno(), payload[:65536])
                                payload = payload[written:]
                            except BrokenPipeError:
                                payload = memoryview(b"")
                            if not payload:
                                selector.unregister(stream)
                                stream.close()
                            continue
                        chunk = os.read(stream.fileno(), 65536)
                        if not chunk:
                            selector.unregister(stream)
                        elif stream is process.stderr:
                            errors.extend(chunk)
                            del errors[:-8192]
                        else:
                            total += len(chunk)
                            if total > limit:
                                raise SyncError(f"{node.name}: output exceeds the transfer limit.")
                            output.write(chunk)
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            if code:
                detail = errors.decode("utf-8", errors="replace").strip()
                raise SyncError(f"Remote {node.name}: SSH {operation} failed ({code}): {detail}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def inventory(node: Node, timeout: float = 25, cancel: threading.Event | None = None) -> dict:
    output = io.BytesIO()
    receive(node, "scan", output, timeout=timeout, cancel=cancel)
    try:
        data = json.loads(output.getvalue())
        if (
            not isinstance(data, dict)
            or data.get("format") != 1
            or not isinstance(data.get("sessions"), list)
            or not isinstance(data.get("issues"), list)
        ):
            raise ValueError("invalid inventory schema")
        for entry in data["sessions"]:
            if not isinstance(entry, dict):
                raise ValueError("invalid session entry")
            reader.canonical_id(entry["id"])
            if not all(isinstance(entry.get(key), str) for key in ("summary", "cwd")) or any(
                type(entry.get(key)) is not int or entry[key] < 0 for key in ("size", "modified_ns")
            ):
                raise ValueError("invalid session metadata")
            if (
                not all(
                    isinstance(entry.get(key), str) for key in ("title", "source", "activity_kind")
                )
                or not all(type(entry.get(key)) is bool for key in ("archived", "changing"))
                or (
                    entry.get("activity_ns") is not None
                    and (type(entry["activity_ns"]) is not int or entry["activity_ns"] < 0)
                )
            ):
                raise ValueError("invalid display metadata")
        if not all(isinstance(issue, str) for issue in data["issues"]):
            raise ValueError("invalid inventory diagnostics")
        return data
    except (ValueError, TypeError, KeyError) as exc:
        raise SyncError(f"{node.name}: invalid inventory: {exc}") from exc
