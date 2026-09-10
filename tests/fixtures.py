import json
import tempfile
import unittest
import uuid
from pathlib import Path

from codex_everywhere import reader, service
from codex_everywhere.config import Target

HERE = Path(__file__).resolve().parent.parent


class SessionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        self.bundle = self.root / "sessions.zip"

    def tearDown(self):
        self.temporary.cleanup()

    def session(
        self, home=None, thread_id=None, messages=("first",), base=None, start=0, archive=False
    ):
        home = home or self.source
        thread_id = thread_id or str(uuid.uuid4())
        folder = "archived_sessions" if archive else "sessions/2026/09/09"
        path = home / folder / ("rollout-2026-09-09T12-00-00-" + thread_id + ".jsonl")
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
