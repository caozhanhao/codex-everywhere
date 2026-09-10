import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from codex_everywhere import reader, service
from codex_everywhere.config import Target

HERE = Path(__file__).resolve().parent.parent


class SessionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        self.bundle = self.root / "sessions.zip"
        # Executable fixtures and SSH workers must use the test runner's Python.
        search_path = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        environment = mock.patch.dict(os.environ, {"PATH": search_path})
        environment.start()
        self.addCleanup(environment.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def session(
        self,
        home=None,
        thread_id=None,
        messages=("first",),
        base=None,
        start=0,
        archive=False,
        rollout_id=None,
    ):
        home = home or self.source
        thread_id = thread_id or str(uuid.uuid4())
        folder = "archived_sessions" if archive else "sessions/2026/09/09"
        identity = thread_id + ("_" + rollout_id if rollout_id and rollout_id != thread_id else "")
        path = home / folder / ("rollout-2026-09-09T12-00-00-" + identity + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": thread_id,
            "cwd": str(self.root),
            "history_mode": "paginated",
            "source": "cli",
            "timestamp": "2026-09-09T12:00:00Z",
        }
        if base:
            meta["history_base"] = base
        rows = [{"ordinal": start, "type": "session_meta", "payload": meta}]
        for index, message in enumerate(messages, start + 1):
            rows.append(
                {
                    "ordinal": index,
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": message},
                }
            )
        path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
        return thread_id, path

    def export(self, selected=None):
        with self.bundle.open("wb") as f:
            reader.export_bundle(self.source, f, selected)

    def importing(self, dry_run=False):
        with service.prepare_file(self.bundle, Target(self.target)) as prepared:
            plan = [change.to_dict() for change in prepared.changes]
            report = None if dry_run else service.apply(prepared, index=False)
            return {"plan": plan, "report": str(report)}

    def native_history(self, path, turn_id):
        row = json.loads(path.read_bytes().splitlines()[0])
        thread_id = row["payload"]["id"]
        row["payload"].update(
            session_id=thread_id,
            originator="codex",
            cli_version="0.154.0",
            model_provider="offline",
            base_instructions={"text": "Synthetic offline history fixture."},
        )
        rows = [row] + [
            {"type": "event_msg", "payload": payload}
            for payload in (
                {"type": "task_started", "turn_id": turn_id, "model_context_window": None},
                {
                    "type": "item_completed",
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "item": {
                        "type": "UserMessage",
                        "id": turn_id + "-user",
                        "content": [{"type": "text", "text": turn_id}],
                    },
                },
                {
                    "type": "item_completed",
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "item": {
                        "type": "AgentMessage",
                        "id": turn_id + "-agent",
                        "content": [{"type": "Text", "text": "Synthetic response."}],
                    },
                },
                {
                    "type": "task_complete",
                    "turn_id": turn_id,
                    "last_agent_message": "Synthetic response.",
                },
            )
        ]
        for ordinal, record in enumerate(rows, row["ordinal"]):
            record.update(ordinal=ordinal, timestamp="2026-09-09T12:00:00Z")
        path.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in rows))
