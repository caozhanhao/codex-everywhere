"""Real Codex record shapes, bounded hints and immutable source behavior."""

import io
import json
import os
from unittest import mock

from codex_everywhere import reader
from tests.fixtures import SessionFixture


def line(payload, kind="event_msg", timestamp="2026-09-08T12:00:00Z"):
    return (
        json.dumps(
            {"type": kind, "timestamp": timestamp, "payload": payload}, ensure_ascii=False
        ).encode()
        + b"\n"
    )


class CatalogTests(SessionFixture):
    def test_legacy_and_paginated_user_events_use_the_same_preview(self):
        for event in (
            {
                "type": "user_message",
                "message": "IDE context\n## My request for Codex:\n Fix  session selection",
            },
            {
                "type": "item_completed",
                "item": {
                    "type": "UserMessage",
                    "content": [
                        {"type": "text", "text": "Fix  "},
                        {"type": "text", "text": "session selection"},
                    ],
                },
            },
        ):
            with self.subTest(event=event):
                self.assertEqual(
                    reader.read_catalog_preview(io.BytesIO(line(event))), "Fix session selection"
                )

    def test_raw_response_fallback_skips_context_and_prefers_ui_event(self):
        def response(text):
            return line(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
                "response_item",
            )

        data = (
            response("# AGENTS.md instructions for /work")
            + response("<environment_context>generated</environment_context>")
            + response("raw user request")
        )
        self.assertEqual(reader.read_catalog_preview(io.BytesIO(data)), "raw user request")
        data += line(
            {
                "type": "item_completed",
                "item": {
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": "visible user request"}],
                },
            }
        )
        self.assertEqual(reader.read_catalog_preview(io.BytesIO(data)), "visible user request")

    def test_image_audio_and_malformed_records(self):
        for content, expected in (
            ([{"type": "image", "image_url": "fixture"}], "[Image]"),
            ([{"type": "local_audio", "path": "/fixture"}], "[Audio]"),
        ):
            data = b'[]\n{"payload": []}\n{broken}\n' + line(
                {"type": "item_completed", "item": {"type": "UserMessage", "content": content}}
            )
            self.assertEqual(reader.read_catalog_preview(io.BytesIO(data)), expected)
        self.assertEqual(
            reader.read_catalog_preview(
                io.BytesIO(b"{}\n" + line({"type": "user_message", "message": "partial"})[:-1])
            ),
            "",
        )

    def test_sampling_stops_at_byte_and_record_budgets(self):
        stream = io.BytesIO(
            line({"type": "other", "message": "x" * 2000})
            + line({"type": "user_message", "message": "too late"})
        )
        with mock.patch.object(reader, "CATALOG_HEAD_BYTES", 128):
            self.assertEqual(reader.read_catalog_preview(stream), "")
        self.assertLessEqual(stream.tell(), 129)
        stream = io.BytesIO(b"{}\n" * 4 + line({"type": "user_message", "message": "too late"}))
        with mock.patch.object(reader, "CATALOG_HEAD_RECORDS", 4):
            self.assertEqual(reader.read_catalog_preview(stream), "")
        self.assertEqual(stream.tell(), 12)

    def test_legacy_and_paginated_use_latest_complete_name_index_row(self):
        thread_id, path = self.session()
        index = self.source / "session_index.jsonl"
        index.write_bytes(
            b"".join(
                json.dumps({"id": thread_id, "thread_name": title}).encode() + b"\n"
                for title in ("old", "named conversation")
            )
            + b'{"id":'
        )
        self.assertEqual(reader.scan(self.source)["sessions"][0]["title"], "named conversation")
        data = path.read_bytes().replace(b'"paginated"', b'"legacy"')
        path.write_bytes(data)
        self.assertEqual(reader.scan(self.source)["sessions"][0]["title"], "named conversation")
        index.unlink()
        index.symlink_to(self.source / "auth.json")
        scanned = reader.scan(self.source)
        self.assertEqual(scanned["sessions"][0]["title"], "")
        self.assertIn("symlink", scanned["issues"][0])

    def test_name_changes_and_clears_are_visible_on_refresh_without_touching_history(self):
        thread_id, path = self.session()
        before = path.read_bytes()
        index = self.source / "session_index.jsonl"
        for name in ("original name", "renamed conversation", "", "another name"):
            with index.open("ab") as stream:
                stream.write(json.dumps({"id": thread_id, "thread_name": name}).encode() + b"\n")
            content = index.read_bytes()
            stat = index.stat()
            entry = reader.scan(self.source)["sessions"][0]
            self.assertEqual(entry["title"], name)
            self.assertEqual(index.read_bytes(), content)
            self.assertEqual(index.stat().st_mtime_ns, stat.st_mtime_ns)
            self.assertEqual(path.read_bytes(), before)

    def test_index_tail_is_bounded_and_skips_partial_first_record(self):
        thread_id, _ = self.session()
        row = json.dumps({"id": thread_id, "thread_name": "latest"}).encode() + b"\n"
        index = self.source / "session_index.jsonl"
        index.write_bytes(b"x" * 200 + b"\n" + row)
        with mock.patch.object(reader, "CATALOG_INDEX_BYTES", len(row) + 10):
            issues = []
            self.assertEqual(reader.catalog_names(self.source, issues), {thread_id: "latest"})
            self.assertEqual(len(issues), 1)

    def test_activity_ignores_copy_time_and_incomplete_tail(self):
        _, path = self.session()
        with path.open("ab") as stream:
            stream.write(line({"type": "user_message", "message": "last complete message"}))
            stream.write(
                line(
                    {"type": "user_message", "message": "in progress"},
                    timestamp="2099-01-01T00:00:00Z",
                )[:-1]
            )
        os.utime(path, ns=(999999999999999999, 999999999999999999))
        entry = reader.scan(self.source)["sessions"][0]
        self.assertEqual(entry["activity_ns"], reader.timestamp_ns("2026-09-08T12:00:00Z"))
        self.assertEqual(entry["activity_kind"], "record")
        self.assertNotEqual(entry["activity_ns"], entry["modified_ns"])

    def test_activity_falls_back_to_creation_but_never_mtime(self):
        _, path = self.session()
        entry = reader.scan(self.source)["sessions"][0]
        self.assertEqual(entry["activity_kind"], "created")
        self.assertEqual(entry["activity_ns"], reader.timestamp_ns("2026-09-09T12:00:00Z"))
        path.write_bytes(path.read_bytes().replace(b'"2026-09-09T12:00:00Z"', b'"invalid"'))
        entry = reader.scan(self.source)["sessions"][0]
        self.assertIsNone(entry["activity_ns"])
        self.assertEqual(entry["activity_kind"], "unknown")
        self.assertIsNone(reader.timestamp_ns("2026-09-09T12:00:00"))

    def test_source_classification_does_not_use_title_text(self):
        _, path = self.session(messages=("guardian is a word in this human request",))
        self.assertEqual(reader.scan(self.source)["sessions"][0]["source"], "cli")
        path.write_bytes(
            path.read_bytes().replace(
                b'"source": "cli"', b'"source": {"subagent": {"other": "guardian"}}'
            )
        )
        self.assertEqual(reader.scan(self.source)["sessions"][0]["source"], "subagent")
