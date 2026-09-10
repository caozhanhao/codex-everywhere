"""Thread IDs stay stable across revert; history pointers identify immutable rollouts."""

import io
import json
import os
import shutil
import sqlite3
import unittest
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from unittest import mock

from codex_everywhere import codex, presentation, reader, safety, service, storage
from codex_everywhere.config import Target
from codex_everywhere.plan import Action
from tests.fixtures import SessionFixture


class RolloutTests(SessionFixture):
    def continuation(self, parent, *, home=None, thread_id=None, through=None, message="continued"):
        lines = parent.read_bytes().splitlines(keepends=True)
        inherited = lines if through is None else lines[:through]
        owner, parent_id = reader.rollout_ids(parent)
        rollout_id = str(uuid.uuid4())
        _, path = self.session(
            home=home,
            thread_id=thread_id or owner,
            rollout_id=rollout_id,
            base={
                "thread_id": parent_id,
                "end_byte_offset": sum(map(len, inherited)),
                "end_ordinal_exclusive": json.loads(inherited[-1])["ordinal"] + 1,
            },
            start=json.loads(inherited[-1])["ordinal"] + 1,
            messages=(message,),
        )
        return rollout_id, path

    def duplicate(self, path):
        copy = path.with_name(path.name.replace("T12-00-00", "T13-00-00"))
        shutil.copyfile(path, copy)
        return copy

    def metadata_db(self, thread_id, path):
        database = self.target / "state_5.sqlite"
        with closing(sqlite3.connect(database)) as db, db:
            db.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, "
                "history_mode TEXT, title TEXT)"
            )
            db.executemany(
                "INSERT INTO threads VALUES (?, ?, 'paginated', ?)",
                [(thread_id, str(path), "Keep this title"), ("unrelated", "untouched", "Other")],
            )
        return database

    def metadata_rows(self, database):
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
            return db.execute("SELECT * FROM threads ORDER BY id").fetchall()

    def test_thread_head_and_exact_rollout_ancestry_are_distinct(self):
        thread_id, base = self.session()
        first, middle = self.continuation(base)
        last, tip = self.continuation(middle)
        # Clock order cannot choose the active history.
        os.utime(base, (2000000000, 2000000000))
        sessions, order = reader.collect(self.source, [thread_id])
        self.assertEqual(order, [thread_id, first, last])
        self.assertEqual([sessions[i].id for i in order], [thread_id] * 3)
        self.assertEqual(reader.paths_by_id(self.source), {thread_id: tip})
        self.assertEqual(reader.session_heads(sessions), {thread_id: sessions[last]})
        scanned = reader.scan(self.source)
        self.assertEqual(scanned["issues"], [])
        self.assertEqual(len(scanned["sessions"]), 1)
        self.assertEqual(scanned["sessions"][0]["id"], thread_id)
        self.assertIsNone(scanned["sessions"][0]["parent_id"])
        self.assertEqual(scanned["sessions"][0]["size"], tip.stat().st_size)

    def test_segmented_transfer_indexes_every_segment_and_finishes_at_the_tip(self):
        thread_id, base = self.session()
        _, middle = self.continuation(base)
        tip_id, tip = self.continuation(middle)
        originals = {p.relative_to(self.source): p.read_bytes() for p in (base, middle, tip)}
        self.export([thread_id])
        with zipfile.ZipFile(self.bundle) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["roots"], [thread_id])
        self.assertEqual(len(manifest["sessions"]), 3)
        with (
            service.prepare_file(self.bundle, Target(self.target)) as prepared,
            mock.patch.object(codex, "check_version"),
            mock.patch.object(codex, "select_rollout"),
            mock.patch.object(codex, "AppServer") as server,
        ):
            server.return_value.call.return_value = {"data": [], "nextCursor": None}
            self.assertEqual(prepared.thread_order, [thread_id])
            self.assertEqual(prepared.heads[thread_id].rollout_id, tip_id)
            self.assertIn(
                "Includes 2 earlier history segment(s) needed by these sessions.",
                presentation.comparison_lines(prepared, thread_id),
            )
            service.apply(prepared)
        resumes = [
            call.args[1]
            for call in server.return_value.call.call_args_list
            if call.args[0] == "thread/resume"
        ]
        self.assertEqual([row["threadId"] for row in resumes], [thread_id] * 3)
        self.assertEqual(
            [row["path"] for row in resumes],
            [str(self.target / p.relative_to(self.source)) for p in (base, middle, tip)],
        )
        for relative, content in originals.items():
            self.assertEqual((self.target / relative).read_bytes(), content)
            self.assertEqual((self.source / relative).read_bytes(), content)

    def test_unrelated_true_duplicates_do_not_block_export_import_or_rebuild(self):
        for home in (self.source, self.target):
            _, path = self.session(home)
            self.duplicate(path)
        thread_id, incoming = self.session(messages=("selected",))
        untouched = {p: p.read_bytes() for p in self.target.rglob("*.jsonl")}
        self.export([thread_id])
        with (
            service.prepare_file(self.bundle, Target(self.target)) as prepared,
            mock.patch.object(codex, "check_version"),
            mock.patch.object(codex, "select_rollout"),
            mock.patch.object(codex, "AppServer") as server,
        ):
            server.return_value.call.return_value = {"data": [], "nextCursor": None}
            service.apply(prepared)
        self.assertEqual(
            reader.paths_by_id(self.target, [thread_id])[thread_id].read_bytes(),
            incoming.read_bytes(),
        )
        self.assertEqual({p: p.read_bytes() for p in untouched}, untouched)
        self.assertEqual(len(reader.scan(self.source)["issues"]), 1)

    def test_unrelated_valid_segments_are_not_even_opened_during_import(self):
        _, base = self.session(self.target)
        self.continuation(base, home=self.target)
        _, incoming = self.session()
        self.export()
        original_open = Path.open

        def checked_open(path, *args, **kwargs):
            if path.is_relative_to(self.target / "sessions") and path.name != incoming.name:
                self.fail(f"Opened unrelated history: {path}")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", checked_open):
            self.importing()

    def test_ambiguous_branches_only_block_the_affected_session(self):
        thread_id, base = self.session()
        self.continuation(base, message="branch one")
        self.continuation(base, message="branch two")
        selected, _ = self.session()
        scanned = reader.scan(self.source)
        self.assertEqual([row["id"] for row in scanned["sessions"]], [selected])
        self.assertIn("Ambiguous history", scanned["issues"][0])
        self.export([selected])
        self.importing()
        with self.assertRaisesRegex(reader.SyncError, "Ambiguous history"):
            reader.export_bundle(self.source, io.BytesIO(), [thread_id])

    def test_real_duplicate_of_a_selected_rollout_still_blocks_writes(self):
        thread_id, _ = self.session()
        _, existing = self.session(self.target, thread_id)
        copy = self.duplicate(existing)
        before = {p: p.read_bytes() for p in (existing, copy)}
        self.export([thread_id])
        with self.assertRaisesRegex(reader.SyncError, "same history segment"):
            self.importing()
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertFalse((self.target / storage.STATE_DIRECTORY).exists())

    def test_fork_uses_the_named_rollout_not_the_parents_current_tip(self):
        parent, base = self.session()
        middle_id, middle = self.continuation(base)
        latest_id, _ = self.continuation(middle)
        child = str(uuid.uuid4())
        child_rollout, _ = self.continuation(middle, thread_id=child)
        sessions, order = reader.collect(self.source, [child])
        self.assertEqual(order, [parent, middle_id, child_rollout])
        self.assertNotIn(latest_id, sessions)
        self.export([child])
        self.importing()
        scanned = {row["id"]: row for row in reader.scan(self.target)["sessions"]}
        self.assertEqual(scanned[child]["parent_id"], parent)

    def test_updating_a_tip_keeps_all_ancestor_bytes_and_backs_up_the_tip(self):
        thread_id, base = self.session()
        tip_id, tip = self.continuation(base)
        self.export([thread_id])
        self.importing()
        old_tip = tip.read_bytes()
        old_base = base.read_bytes()
        next_ordinal = json.loads(old_tip.splitlines()[-1])["ordinal"] + 1
        with tip.open("ab") as stream:
            stream.write(
                json.dumps({"ordinal": next_ordinal, "type": "event_msg", "payload": {}}).encode()
                + b"\n"
            )
        self.export([thread_id])
        with service.prepare_file(self.bundle, Target(self.target)) as prepared:
            self.assertEqual(prepared.action_for(thread_id), Action.UPDATE)
            self.assertEqual(prepared.changes[-1].rollout_id, tip_id)
            report = service.apply(prepared, index=False)
        self.assertEqual((report / "original" / tip.relative_to(self.source)).read_bytes(), old_tip)
        self.assertEqual((self.target / base.relative_to(self.source)).read_bytes(), old_base)

    def test_appending_a_new_segment_does_not_replace_the_previous_file(self):
        thread_id, base = self.session()
        self.export([thread_id])
        self.importing()
        old_base = base.read_bytes()
        _, tip = self.continuation(base)
        self.export([thread_id])
        with service.prepare_file(self.bundle, Target(self.target)) as prepared:
            self.assertEqual(prepared.action_for(thread_id), Action.UPDATE)
            self.assertFalse(prepared.has_conflicts)
            service.apply(prepared, index=False)
        self.assertEqual((self.target / base.relative_to(self.source)).read_bytes(), old_base)
        self.assertEqual(reader.paths_by_id(self.target)[thread_id].name, tip.name)

    def test_revert_cannot_silently_replace_longer_local_history(self):
        thread_id, base = self.session(messages=("keep", "would be discarded"))
        self.export([thread_id])
        self.importing()
        before = base.read_bytes()
        _, reverted = self.continuation(base, through=2, message="replacement branch")
        self.export([thread_id])
        with service.prepare_file(self.bundle, Target(self.target)) as prepared:
            self.assertTrue(prepared.has_conflicts)
            with self.assertRaisesRegex(reader.SyncError, "Histories diverged"):
                service.apply(prepared, index=False)
        self.assertEqual((self.target / base.relative_to(self.source)).read_bytes(), before)
        self.assertFalse((self.target / reverted.relative_to(self.source)).exists())

    def test_older_incoming_tip_keeps_and_reindexes_the_local_tip(self):
        thread_id, base = self.session()
        self.export([thread_id])
        self.importing()
        target_base = self.target / base.relative_to(self.source)
        _, target_tip = self.continuation(target_base, home=self.target)
        with (
            service.prepare_file(self.bundle, Target(self.target)) as prepared,
            mock.patch.object(codex, "check_version"),
            mock.patch.object(codex, "select_rollout"),
            mock.patch.object(codex, "AppServer") as server,
        ):
            server.return_value.call.return_value = {"data": [], "nextCursor": None}
            self.assertEqual(prepared.action_for(thread_id), Action.LOCAL_NEWER)
            service.apply(prepared)
        resumes = [
            call.args[1]
            for call in server.return_value.call.call_args_list
            if call.args[0] == "thread/resume"
        ]
        self.assertEqual([row["path"] for row in resumes], [str(target_base), str(target_tip)])

    def test_new_related_branch_after_preview_aborts_before_installing(self):
        thread_id, source = self.session()
        self.export([thread_id])
        self.importing()
        self.continuation(source)
        self.export([thread_id])
        target_base = self.target / source.relative_to(self.source)
        with service.prepare_file(self.bundle, Target(self.target)) as prepared:
            self.continuation(target_base, home=self.target, message="local branch")
            before = {p: p.read_bytes() for p in self.target.rglob("*.jsonl")}
            with self.assertRaisesRegex(reader.SyncError, "changed since preview"):
                service.apply(prepared, index=False)
        self.assertEqual({p: p.read_bytes() for p in self.target.rglob("*.jsonl")}, before)

    def test_missing_or_invalid_segment_dependencies_block_only_their_thread(self):
        thread_id, base = self.session()
        _, tip = self.continuation(base)
        selected, _ = self.session()
        base_bytes = base.read_bytes()
        base.unlink()
        with self.assertRaisesRegex(reader.SyncError, "Missing session or history rollout"):
            reader.collect(self.source, [thread_id])
        self.export([selected])
        self.importing()
        base.write_bytes(base_bytes)
        data = tip.read_bytes()
        bad = data.replace(str(len(base_bytes)).encode(), str(len(base_bytes) - 1).encode())
        tip.write_bytes(bad)
        with self.assertRaisesRegex(reader.SyncError, "Ancestor boundary"):
            reader.collect(self.source, [thread_id])

    def test_new_source_head_during_export_invalidates_the_snapshot(self):
        thread_id, base = self.session()
        original_open = zipfile.ZipFile.open

        def publish_head(archive, name, *args, **kwargs):
            stream = original_open(archive, name, *args, **kwargs)
            if name.endswith(".jsonl"):
                self.continuation(base)
            return stream

        with mock.patch.object(zipfile.ZipFile, "open", publish_head):
            with self.assertRaisesRegex(reader.SyncError, "selection changed during export"):
                self.export([thread_id])

    def test_segmented_locations_are_keyed_by_thread_and_survive_round_trip(self):
        thread_id, base = self.session()
        self.continuation(base)
        self.export([thread_id])
        target = Target(self.target, cwd=str(self.target))
        with service.prepare_file(self.bundle, target) as prepared:
            self.assertEqual(prepared.directory_updates, 1)
            self.assertEqual(prepared.directories, {thread_id: str(self.target)})
            service.apply(prepared, index=False)
        with self.bundle.open("wb") as output:
            reader.export_bundle(self.target, output, [thread_id])
        with zipfile.ZipFile(self.bundle) as archive:
            locations = json.loads(archive.read("manifest.json"))["locations"]
        self.assertEqual(set(locations), {thread_id})
        with service.prepare_file(self.bundle, target) as prepared:
            self.assertFalse(prepared.needs_sync)

    def test_path_selection_changes_only_one_metadata_field_and_records_the_original(self):
        thread_id, base = self.session(self.target)
        _, tip = self.continuation(base, home=self.target)
        database = self.metadata_db(thread_id, tip)
        before = self.metadata_rows(database)
        item = reader.inspect_session(base, base.relative_to(self.target).as_posix())
        selections = {}
        codex.select_rollout(self.target, None, item, self.root, selections)
        after = self.metadata_rows(database)
        self.assertEqual(
            after,
            [(i, str(base) if i == thread_id else p, mode, title) for i, p, mode, title in before],
        )
        self.assertEqual(selections, {thread_id: {"original": str(tip), "selected": str(base)}})
        self.assertEqual(json.loads((self.root / "index-paths.json").read_text()), selections)

    def test_path_selection_rolls_back_when_its_journal_cannot_be_written(self):
        thread_id, base = self.session(self.target)
        _, tip = self.continuation(base, home=self.target)
        database = self.metadata_db(thread_id, tip)
        before = self.metadata_rows(database)
        item = reader.inspect_session(base, base.relative_to(self.target).as_posix())
        with mock.patch.object(codex, "write_json", side_effect=OSError("fixture disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                codex.select_rollout(self.target, None, item, self.root, {})
        self.assertEqual(self.metadata_rows(database), before)

    def test_path_selection_respects_native_writer_locks_and_does_not_create_databases(self):
        thread_id, base = self.session(self.target)
        item = reader.inspect_session(base, base.relative_to(self.target).as_posix())
        with self.assertRaisesRegex(reader.SyncError, "Cannot select history segment"):
            codex.select_rollout(self.target, None, item, self.root, {})
        self.assertFalse((self.target / "state_5.sqlite").exists())
        database = self.metadata_db(thread_id, base)
        before = self.metadata_rows(database)
        with safety.file_lock(self.target / "thread-writer-locks" / (thread_id + ".lock")):
            with self.assertRaisesRegex(reader.SyncError, "Lock is busy"):
                codex.select_rollout(self.target, None, item, self.root, {})
        self.assertEqual(self.metadata_rows(database), before)

    def test_index_failure_restores_the_tip_and_retains_the_original_error(self):
        thread_id, base = self.session()
        _, middle = self.continuation(base)
        _, tip = self.continuation(middle)
        target_tip = self.target / tip.relative_to(self.source)
        database = self.metadata_db(thread_id, target_tip)
        before = self.metadata_rows(database)
        self.export([thread_id])

        def call(method, params):
            if method == "thread/resume" and Path(params["path"]).name == middle.name:
                raise reader.SyncError("fixture projection failure")
            return {"data": [], "nextCursor": None}

        with (
            service.prepare_file(self.bundle, Target(self.target)) as prepared,
            mock.patch.object(codex, "check_version"),
            mock.patch.object(codex, "AppServer") as server,
        ):
            server.return_value.call.side_effect = call
            with self.assertRaisesRegex(reader.SyncError, "fixture projection failure"):
                service.apply(prepared)
        self.assertEqual(self.metadata_rows(database), before)
        report = next((self.target / storage.STATE_DIRECTORY / "backups").iterdir())
        self.assertEqual(
            json.loads((report / "status.json").read_text())["phase"], "index-incomplete"
        )
        self.assertIn("fixture projection failure", (report / "rebuild.json").read_text())
        for path in (base, middle, tip):
            self.assertEqual(
                (self.target / path.relative_to(self.source)).read_bytes(), path.read_bytes()
            )

    @unittest.skipUnless(os.environ.get("CE_NATIVE_CODEX"), "opt-in native Codex integration")
    def test_native_codex_reads_retained_turns_and_indexes_the_correct_tip(self):
        binary = os.environ["CE_NATIVE_CODEX"]
        thread_id, base = self.session()

        def write_native(path, turn_id):
            row = json.loads(path.read_bytes().splitlines()[0])
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

        write_native(base, "synthetic-first")
        _, middle = self.continuation(base)
        write_native(middle, "synthetic-second")
        _, tip = self.continuation(middle)
        write_native(tip, "synthetic-third")
        originals = {p.relative_to(self.source): p.read_bytes() for p in (base, middle, tip)}
        (self.target / "config.toml").write_text(
            'model = "gpt-5.6-sol"\nmodel_provider = "offline"\n'
            '[model_providers.offline]\nname = "Offline fixture"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            "requires_openai_auth = false\n"
        )
        self.export([thread_id])
        with service.prepare_file(self.bundle, Target(self.target, codex=binary)) as prepared:
            report = service.apply(prepared)
        rebuilt = json.loads((report / "rebuild.json").read_text())
        self.assertEqual([(row["id"], row["turns"]) for row in rebuilt], [(thread_id, 3)])
        # The next handoff must advance an existing DB binding to a new segment.
        _, tip = self.continuation(tip)
        write_native(tip, "synthetic-fourth")
        originals[tip.relative_to(self.source)] = tip.read_bytes()
        self.export([thread_id])
        with service.prepare_file(self.bundle, Target(self.target, codex=binary)) as prepared:
            self.assertEqual(prepared.action_for(thread_id), Action.UPDATE)
            report = service.apply(prepared)
        rebuilt = json.loads((report / "rebuild.json").read_text())
        self.assertEqual([(row["id"], row["turns"]) for row in rebuilt], [(thread_id, 4)])
        with closing(
            sqlite3.connect((self.target / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)
        ) as db:
            rows = db.execute(
                "SELECT id, rollout_path FROM threads WHERE id = ?", (thread_id,)
            ).fetchall()
        self.assertEqual(rows, [(thread_id, str(self.target / tip.relative_to(self.source)))])
        server = codex.AppServer(self.target, binary, self.root / "verify.log")
        try:
            server.call(
                "thread/resume",
                {"threadId": thread_id, "excludeTurns": True, "cwd": str(self.root)},
            )
            page = server.call(
                "thread/turns/list", {"threadId": thread_id, "itemsView": "full", "limit": 100}
            )
            self.assertEqual(
                {turn["id"] for turn in page["data"]},
                {"synthetic-first", "synthetic-second", "synthetic-third", "synthetic-fourth"},
            )
            for turn in page["data"]:
                self.assertEqual(len(turn["items"]), 2)
                self.assertEqual(turn["items"][0]["content"][0]["text"], turn["id"])
                self.assertEqual(turn["items"][1]["text"], "Synthetic response.")
        finally:
            server.close()
        for relative, content in originals.items():
            self.assertEqual((self.target / relative).read_bytes(), content)
