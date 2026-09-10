"""Concurrent native writers must only block imports of their own history."""

import fcntl
import io
import json
import os
import sqlite3
import subprocess
import unittest
import uuid
import zipfile
from contextlib import closing, contextmanager
from unittest import mock

from codex_everywhere import codex, reader, safety, service, storage
from codex_everywhere.config import Target
from tests.fixtures import SessionFixture


class ConcurrencyTests(SessionFixture):
    def test_export_reads_a_locked_source_without_process_probes_or_source_writes(self):
        thread_id, path = self.session()
        before = path.read_bytes()
        output = io.BytesIO()
        with (
            safety.session_locks(self.source, [thread_id]),
            mock.patch.object(subprocess, "run", side_effect=AssertionError("process probe")),
        ):
            reader.export_bundle(self.source, output, [thread_id])
        with zipfile.ZipFile(output) as bundle:
            self.assertEqual(bundle.read(path.relative_to(self.source).as_posix()), before)
        self.assertEqual(path.read_bytes(), before)

    def test_source_change_during_copy_never_publishes_a_valid_manifest(self):
        thread_id, path = self.session()
        original_open = zipfile.ZipFile.open

        def append_during_copy(bundle, name, *args, **kwargs):
            if str(name).endswith(".jsonl"):
                with path.open("ab") as stream:
                    stream.write(b'{"ordinal":2,"type":"event_msg","payload":{}}\n')
            return original_open(bundle, name, *args, **kwargs)

        output = io.BytesIO()
        with mock.patch.object(zipfile.ZipFile, "open", append_during_copy):
            with self.assertRaisesRegex(reader.SyncError, "Remote server-a: Source changed"):
                reader.export_bundle(self.source, output, [thread_id], location="Remote server-a")
        with zipfile.ZipFile(output) as bundle:
            self.assertNotIn("manifest.json", bundle.namelist())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_unrelated_writer_does_not_block_import_and_coordination_lock_is_released(self):
        unrelated, path = self.session(self.target)
        before = path.read_bytes()
        selected, incoming = self.session()
        self.export([selected])
        checked = []

        def check_locks(*args, **kwargs):
            with self.assertRaisesRegex(reader.SyncError, selected):
                with safety.session_locks(self.target, [selected]):
                    self.fail("Selected history was unlocked during reconstruction")
            with self.assertRaisesRegex(reader.SyncError, "maintaining history"):
                with safety.maintenance_lock(self.target):
                    self.fail("Maintenance was unlocked during reconstruction")
            with safety.file_lock(self.target / "thread-writer-locks" / ".coordination.lock"):
                pass
            checked.append(True)

        with (
            safety.session_locks(self.target, [unrelated]),
            service.prepare_file(self.bundle, Target(self.target)) as prepared,
            mock.patch.object(codex, "check_version"),
            mock.patch.object(codex, "rebuild", side_effect=check_locks),
        ):
            service.apply(prepared)
        self.assertEqual(checked, [True])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(
            (self.target / incoming.relative_to(self.source)).read_bytes(), incoming.read_bytes()
        )
        with safety.maintenance_lock(self.target), safety.session_locks(self.target, [selected]):
            pass

    def test_active_ancestor_blocks_only_its_descendants(self):
        parent, path = self.session(self.target)
        child, _ = self.session(
            self.target,
            base={
                "thread_id": parent,
                "end_byte_offset": path.stat().st_size,
                "end_ordinal_exclusive": 2,
            },
            start=2,
        )
        unrelated, _ = self.session(self.target)
        with safety.session_locks(self.target, [parent]):
            with self.assertRaisesRegex(reader.SyncError, parent):
                with safety.session_locks(self.target, [child]):
                    self.fail("Ancestor's active writer was ignored")
            with safety.session_locks(self.target, [unrelated]):
                pass
        with safety.session_locks(self.target, [child]):
            with self.assertRaisesRegex(reader.SyncError, parent):
                with safety.session_locks(self.target, [parent]):
                    self.fail("Ancestor was unlocked while importing a child")

    def test_failed_rebuild_releases_every_lock_and_keeps_installed_history(self):
        selected, path = self.session()
        self.export([selected])
        with (
            service.prepare_file(self.bundle, Target(self.target)) as prepared,
            mock.patch.object(codex, "check_version"),
            mock.patch.object(
                codex, "rebuild", side_effect=reader.SyncError("fixture index failure")
            ),
        ):
            with self.assertRaisesRegex(reader.SyncError, "fixture index failure"):
                service.apply(prepared)
        self.assertEqual(
            (self.target / path.relative_to(self.source)).read_bytes(), path.read_bytes()
        )
        with (
            safety.file_lock(self.target / storage.STATE_DIRECTORY / ".sync.lock"),
            safety.maintenance_lock(self.target),
            safety.session_locks(self.target, [selected]),
        ):
            pass

    def test_busy_maintenance_blocks_import_before_any_history_is_installed(self):
        self.session()
        self.export()
        with safety.maintenance_lock(self.target):
            with self.assertRaisesRegex(reader.SyncError, "Local machine:.*maintaining history"):
                self.importing()
        self.assertFalse((self.target / "sessions").exists())
        self.assertFalse((self.target / storage.STATE_DIRECTORY / "backups").exists())

    def test_changed_dependencies_are_rechecked_after_acquiring_session_locks(self):
        parent, parent_path = self.session(self.target)
        child, child_path = self.session(self.target)
        real_lock = safety.file_lock
        switched = []

        @contextmanager
        def switch_before_lock(path, **kwargs):
            if path.name == f"{child}.lock" and not switched:
                rows = [json.loads(line) for line in child_path.read_bytes().splitlines()]
                rows[0]["payload"]["history_base"] = {
                    "thread_id": parent,
                    "end_byte_offset": parent_path.stat().st_size,
                    "end_ordinal_exclusive": 2,
                }
                for row in rows:
                    row["ordinal"] += 2
                child_path.write_bytes(b"".join(json.dumps(row).encode() + b"\n" for row in rows))
                switched.append(True)
            with real_lock(path, **kwargs):
                yield

        with mock.patch.object(safety, "file_lock", switch_before_lock):
            with self.assertRaisesRegex(reader.SyncError, "dependencies changed"):
                with safety.session_locks(self.target, [child]):
                    self.fail("A newly introduced ancestor was left unlocked")
        with safety.session_locks(self.target, [child]):
            with self.assertRaisesRegex(reader.SyncError, parent):
                with safety.session_locks(self.target, [parent]):
                    self.fail("Retry failed to protect the new ancestor")

    def test_rebuild_all_does_not_include_threads_created_after_lock_selection(self):
        real_rebuild = codex.rebuild
        created = []

        def concurrent_creation(home, selected, *args, **kwargs):
            created.append(self.session(self.target)[0])
            return real_rebuild(home, selected, *args, **kwargs)

        with (
            mock.patch.object(codex, "check_version"),
            mock.patch.object(codex, "rebuild", side_effect=concurrent_creation),
            mock.patch.object(codex, "AppServer") as server,
        ):
            report = service.rebuild(Target(self.target), None)
        self.assertEqual(json.loads((report / "rebuild.json").read_text()), [])
        self.assertEqual(len(created), 1)
        server.assert_not_called()

    def test_index_worker_shares_maintenance_lock_and_cleans_its_private_home(self):
        selected, path = self.session(self.target)
        with safety.maintenance_lock(self.target), safety.session_locks(self.target, [selected]):
            with codex.indexing_home(self.target) as worker:
                self.assertEqual((worker / path.relative_to(self.target)).resolve(), path)
                lock = worker / ".tmp" / "rollout-maintenance.lock"
                # Native Codex follows this symlink when acquiring its maintenance lock.
                with lock.open("rb") as stream:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with safety.file_lock(worker / "thread-writer-locks" / f"{selected}.lock"):
                    pass
        self.assertFalse(worker.exists())

    def test_private_index_worker_cannot_treat_archived_history_as_active(self):
        thread_id, path = self.session(self.target, archive=True)
        before = path.read_bytes()
        with (
            mock.patch.object(codex, "check_version"),
            mock.patch.object(codex, "AppServer") as server,
        ):
            with self.assertRaisesRegex(reader.SyncError, f"codex unarchive {thread_id}"):
                service.rebuild(Target(self.target), [thread_id])
        server.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(os.environ.get("CE_NATIVE_CODEX"), "opt-in native Codex integration")
    def test_native_unrelated_session_stays_open_while_another_history_is_imported(self):
        binary = os.environ["CE_NATIVE_CODEX"]
        (self.target / "config.toml").write_text(
            'model = "gpt-5.6-sol"\nmodel_provider = "offline"\n'
            '[model_providers.offline]\nname = "Offline fixture"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            "requires_openai_auth = false\n"
        )
        unrelated, active_path = self.session(self.target)
        self.native_history(active_path, "still-open")
        selected, incoming = self.session()
        self.native_history(incoming, "imported")
        sqlite_home = self.root / "sqlite"
        sqlite_home.mkdir()
        target = Target(self.target, sqlite_home, codex=binary)
        database = sqlite_home / "state_5.sqlite"

        def metadata(thread_id):
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                return db.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()

        server = codex.AppServer(self.target, binary, self.root / "active.log", sqlite_home)
        try:
            server.call(
                "thread/resume",
                {"threadId": unrelated, "excludeTurns": True, "cwd": str(self.root)},
            )
            before = metadata(unrelated)
            original = active_path.read_bytes()
            _, unindexed = self.session(self.target)
            self.native_history(unindexed, "unopened")
            native_call = codex.AppServer.call
            guarded = []

            def checked_call(worker, method, params):
                if method in ("thread/resume", "thread/turns/list"):
                    with self.assertRaisesRegex(reader.SyncError, selected):
                        with safety.session_locks(self.target, [selected]):
                            self.fail("Native reconstruction released the destination lock")
                    guarded.append(method)
                return native_call(worker, method, params)

            for turns in (1, 2):
                if turns == 2:
                    boundary = json.loads(incoming.read_bytes().splitlines()[-1])["ordinal"] + 1
                    _, incoming = self.session(
                        thread_id=selected,
                        rollout_id=str(uuid.uuid4()),
                        base={
                            "thread_id": selected,
                            "end_byte_offset": incoming.stat().st_size,
                            "end_ordinal_exclusive": boundary,
                        },
                        start=boundary,
                    )
                    self.native_history(incoming, "imported-later")
                self.export([selected])
                with (
                    service.prepare_file(self.bundle, target) as prepared,
                    mock.patch.object(codex.AppServer, "call", checked_call),
                ):
                    report = service.apply(prepared)
                self.assertEqual(
                    json.loads((report / "rebuild.json").read_text())[0]["turns"], turns
                )
            self.assertGreaterEqual(guarded.count("thread/resume"), 3)
            self.assertEqual(metadata(unrelated), before)
            self.assertEqual(active_path.read_bytes(), original)
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                for (path,) in db.execute("SELECT rollout_path FROM threads").fetchall():
                    self.assertTrue(path.startswith(str(self.target / "sessions") + "/"), path)
            with self.assertRaisesRegex(reader.SyncError, unrelated):
                with safety.session_locks(self.target, [unrelated]):
                    self.fail("Existing native writer was disturbed")
            reader.export_bundle(self.target, io.BytesIO(), [unrelated])
            server.call(
                "thread/resume", {"threadId": selected, "excludeTurns": True, "cwd": str(self.root)}
            )
            with service.prepare_file(self.bundle, target) as prepared:
                with self.assertRaisesRegex(reader.SyncError, selected):
                    service.apply(prepared)
            page = server.call(
                "thread/turns/list", {"threadId": unrelated, "itemsView": "full", "limit": 100}
            )
            self.assertEqual([turn["id"] for turn in page["data"]], ["still-open"])
        finally:
            server.close()
