"""Failure injection at the boundaries protecting source and destination data."""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from codex_everywhere import codex, reader, service, storage
from codex_everywhere.config import Target
from tests.fixtures import SessionFixture


class SafetyTests(SessionFixture):
    def test_unavailable_codex_fails_before_replacing_session(self):
        thread_id, source = self.session(messages=("first", "new"))
        _, destination = self.session(self.target, thread_id)
        original = destination.read_bytes()
        incoming = source.read_bytes()
        self.export()
        target = Target(self.target, codex=str(self.root / "missing-codex"))
        with service.prepare_file(self.bundle, target) as prepared:
            with self.assertRaisesRegex(reader.SyncError, "Cannot run Codex"):
                service.apply(prepared)
        self.assertEqual(destination.read_bytes(), original)
        self.assertEqual(source.read_bytes(), incoming)
        self.assertFalse((self.target / storage.STATE_DIRECTORY).exists())

    def test_unverified_version_protocol_failure_retains_history_and_backup(self):
        thread_id, source = self.session(messages=("first", "new"))
        _, destination = self.session(self.target, thread_id)
        original = destination.read_bytes()
        incoming = source.read_bytes()
        self.export()
        notes = []
        version = subprocess.CompletedProcess(
            ["codex", "--version"], 0, stdout="codex-cli 0.155.0\n"
        )
        real_run = subprocess.run

        def run(args, **kwargs):
            return version if args == ["codex", "--version"] else real_run(args, **kwargs)

        with (
            service.prepare_file(self.bundle, Target(self.target)) as prepared,
            mock.patch("subprocess.run", side_effect=run),
            mock.patch.object(
                codex, "AppServer", side_effect=reader.SyncError("fixture protocol mismatch")
            ),
        ):
            with self.assertRaisesRegex(reader.SyncError, "fixture protocol mismatch"):
                service.apply(prepared, progress=notes.append)
        report = next((self.target / storage.STATE_DIRECTORY / "backups").iterdir())
        self.assertEqual(source.read_bytes(), incoming)
        self.assertEqual(destination.read_bytes(), incoming)
        self.assertEqual(
            (report / "original" / destination.relative_to(self.target)).read_bytes(), original
        )
        self.assertEqual(
            json.loads((report / "status.json").read_text())["phase"], "index-incomplete"
        )
        self.assertTrue(any("0.155.0" in note and "unverified" in note for note in notes))
        self.assertTrue(any("run rebuild" in note for note in notes))

    def test_round_trip_a_to_b_to_a(self):
        thread_id, a = self.session()
        self.export()
        result = self.importing()
        b = Path(result["plan"][0]["destination"])
        first_version = a.read_bytes()
        with b.open("ab") as f:
            f.write(
                b'{"ordinal":2,"type":"event_msg","payload":{"type":"user_message","message":"on B"}}\n'
            )
        reverse = self.root / "reverse.zip"
        with reverse.open("wb") as output:
            reader.export_bundle(self.target, output, [thread_id])
        b_before = b.read_bytes()
        with service.prepare_file(reverse, Target(self.source)) as prepared:
            self.assertEqual(prepared.changes[-1].action.value, "update")
            report = service.apply(prepared, index=False)
        self.assertEqual(a.read_bytes(), b_before)
        self.assertEqual(b.read_bytes(), b_before)
        self.assertEqual(
            (report / "original" / a.relative_to(self.source)).read_bytes(), first_version
        )

    def test_target_change_after_preview_is_rejected(self):
        thread_id, _ = self.session(messages=("first", "remote"))
        _, target = self.session(self.target, thread_id)
        self.export()
        with service.prepare_file(self.bundle, Target(self.target)) as prepared:
            with target.open("ab") as f:
                f.write(b'{"ordinal":2,"type":"event_msg","payload":{}}\n')
            changed = target.read_bytes()
            with self.assertRaisesRegex(reader.SyncError, "changed since preview"):
                service.apply(prepared, index=False)
        self.assertEqual(target.read_bytes(), changed)
        self.assertFalse((self.target / storage.STATE_DIRECTORY / "backups").exists())

    def test_staged_file_change_is_rejected(self):
        thread_id, _ = self.session()
        self.export()
        with service.prepare_file(self.bundle, Target(self.target)) as prepared:
            staged = prepared.stage / prepared.sessions[thread_id].relative
            staged.write_bytes(staged.read_bytes().replace(b"first", b"other"))
            with self.assertRaisesRegex(reader.SyncError, "Staged history changed"):
                service.apply(prepared, index=False)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_local_filename_metadata_mismatch_is_rejected(self):
        thread_id, _ = self.session()
        _, target = self.session(self.target, thread_id)
        target.write_bytes(
            target.read_bytes().replace(thread_id.encode(), b"00000000-0000-0000-0000-000000000000")
        )
        self.export()
        with self.assertRaisesRegex(reader.SyncError, "Local filename and metadata"):
            self.importing()

    def test_atomic_copy_verifies_before_replacement(self):
        _, source = self.session()
        target = self.target / "existing"
        target.write_bytes(b"original")
        with self.assertRaisesRegex(reader.SyncError, "changed while copying"):
            storage.atomic_copy(source, target, "0" * 64)
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(list(self.target.iterdir()), [target])

    def test_failed_second_replace_keeps_all_backups_and_can_retry(self):
        for _ in range(2):
            thread_id, _ = self.session(messages=("first", "remote"))
            self.session(self.target, thread_id)
        originals = {
            p.relative_to(self.target): p.read_bytes()
            for p in reader.paths_by_id(self.target).values()
        }
        self.export()
        real_replace = os.replace
        writes = []

        def fail_second(source, dest):
            if Path(dest).is_relative_to(self.target / "sessions"):
                writes.append(dest)
                if len(writes) == 2:
                    raise OSError("simulated disk failure")
            return real_replace(source, dest)

        with mock.patch.object(storage.os, "replace", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.importing()
        report = next((self.target / storage.STATE_DIRECTORY / "backups").iterdir())
        for relative, content in originals.items():
            self.assertEqual((report / "original" / relative).read_bytes(), content)
        self.importing()
        for thread_id, path in reader.paths_by_id(self.target).items():
            self.assertEqual(
                path.read_bytes(), reader.paths_by_id(self.source)[thread_id].read_bytes()
            )

    def test_source_worker_obeys_audit_hook_and_leaves_tree_unchanged(self):
        thread_id, _ = self.session()
        locations = self.source / reader.LOCATIONS_FILE
        locations.parent.mkdir()
        locations.write_text(
            json.dumps({thread_id: {"original_cwd": str(self.root), "cwd": "/mapped/project"}})
        )
        (self.source / "session_index.jsonl").write_text(
            json.dumps(
                {
                    "id": thread_id,
                    "thread_name": "read-only title",
                    "updated_at": "2026-09-09T12:00:00Z",
                }
            )
            + "\n"
        )
        (self.source / "auth.json").write_bytes(b"sentinel secret, must not be opened")
        (self.source / "state_5.sqlite").write_bytes(b"sentinel database, must not be opened")

        def snapshot():
            return {
                str(p.relative_to(self.source)): (
                    p.read_bytes(),
                    p.stat().st_mtime_ns,
                    p.stat().st_mode,
                )
                for p in self.source.rglob("*")
                if p.is_file()
            }

        before = snapshot()
        result = subprocess.run(
            [sys.executable, "-B", "-m", "tests.audit_worker", str(self.source)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, snapshot())

    def test_scan_allows_active_writers_and_reports_damaged_files(self):
        thread_id, path = self.session()
        bad = path.with_name(path.name.replace(thread_id, "00000000-0000-0000-0000-000000000000"))
        bad.write_bytes(b"not json\n")
        with mock.patch.object(
            reader, "assert_idle", side_effect=AssertionError("must not be called")
        ):
            data = reader.scan(self.source)
        self.assertEqual([entry["id"] for entry in data["sessions"]], [thread_id])
        self.assertEqual(len(data["issues"]), 1)

    def test_two_forks_remain_distinct(self):
        parent, path = self.session()
        base = {
            "thread_id": parent,
            "end_byte_offset": path.stat().st_size,
            "end_ordinal_exclusive": 2,
        }
        a, _ = self.session(base=base, start=2, messages=("A",))
        b, _ = self.session(base=base, start=2, messages=("B",))
        self.export([a, b])
        self.importing()
        self.assertEqual(set(reader.paths_by_id(self.target)), {parent, a, b})

    def test_database_symlink_is_rejected_before_import(self):
        self.session()
        self.export()
        outside = self.root / "existing.sqlite"
        outside.write_bytes(b"must not change")
        (self.target / "thread_history_1.sqlite").symlink_to(outside)
        with self.assertRaisesRegex(reader.SyncError, "symlink"):
            self.importing()
        self.assertEqual(outside.read_bytes(), b"must not change")

    def test_network_filesystem_is_rejected(self):
        from codex_everywhere.safety import require_local

        fake = "1 0 0:1 / / rw - nfs4 server:/home rw\n"
        with (
            mock.patch("codex_everywhere.safety.sys.platform", "linux"),
            mock.patch.object(Path, "exists", return_value=True),
            mock.patch.object(Path, "read_text", return_value=fake),
        ):
            with self.assertRaisesRegex(reader.SyncError, "must be local"):
                require_local(self.target)

    def test_macos_idle_check_rejects_active_processes_and_query_failure(self):
        for returncode, stdout, error in (
            (1, "", None),
            (0, "123\n456\n", "active PID\\(s\\): 123, 456"),
            (2, "", "Cannot inspect Codex processes"),
        ):
            with (
                self.subTest(returncode=returncode),
                mock.patch.object(reader.sys, "platform", "darwin"),
                mock.patch.object(Path, "is_dir", return_value=False),
                mock.patch.object(
                    reader.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess("pgrep", returncode, stdout=stdout),
                ) as run,
            ):
                if error:
                    with self.assertRaisesRegex(reader.SyncError, error):
                        reader.assert_idle(self.target)
                else:
                    reader.assert_idle(self.target)
                self.assertEqual(
                    run.call_args.args[0], ["pgrep", "-u", str(os.getuid()), "-x", "codex"]
                )

    def test_legacy_records_are_untouched_by_import(self):
        _, source = self.session()
        incoming = source.read_bytes()
        legacy = self.target / "session-sync"
        backup = legacy / "backups/previous/original.jsonl"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(b"existing backup, must not change")
        (legacy / ".sync.lock").touch()

        def snapshot():
            return {
                path.relative_to(legacy): (
                    path.read_bytes() if path.is_file() else None,
                    path.stat().st_mtime_ns,
                    path.stat().st_mode,
                )
                for path in [legacy, *legacy.rglob("*")]
            }

        before = snapshot()
        self.export()
        result = self.importing()
        self.assertEqual(Path(result["report"]).parent, self.target / ".codex-everywhere/backups")
        self.assertEqual(Path(result["plan"][0]["destination"]).read_bytes(), incoming)
        self.assertEqual(snapshot(), before)
        self.assertEqual(source.read_bytes(), incoming)

    def test_application_lock_serializes_all_destination_operations(self):
        self.session()
        self.export()
        lock = self.target / storage.STATE_DIRECTORY / ".sync.lock"
        lock.parent.mkdir()
        with (
            lock.open("wb") as handle,
            service.prepare_file(self.bundle, Target(self.target)) as prepared,
            mock.patch.object(codex, "check_version"),
            mock.patch.object(codex, "rebuild") as rebuild,
        ):
            fcntl.flock(handle, fcntl.LOCK_EX)
            for name, operation in (
                ("import", lambda: service.apply(prepared, index=False)),
                ("conflict", lambda: service.preserve_conflict(prepared)),
                ("rebuild", lambda: service.rebuild(prepared.target, None)),
            ):
                with self.subTest(operation=name):
                    with self.assertRaisesRegex(reader.SyncError, "Lock is busy"):
                        operation()
            rebuild.assert_not_called()
        self.assertFalse((self.target / "sessions").exists())
        self.assertEqual(list(lock.parent.iterdir()), [lock])

    def test_application_directory_symlink_is_rejected(self):
        self.session()
        self.export()
        outside = self.root / "outside"
        outside.mkdir()
        (self.target / storage.STATE_DIRECTORY).symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(reader.SyncError, "symlink"):
            self.importing()
        self.assertFalse((self.target / "sessions").exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_backup_directory_symlink_is_rejected(self):
        thread_id, _ = self.session(messages=("first", "new"))
        _, target = self.session(self.target, thread_id)
        before = target.read_bytes()
        self.export()
        outside = self.root / "outside"
        outside.mkdir()
        (self.target / storage.STATE_DIRECTORY).mkdir()
        (self.target / storage.STATE_DIRECTORY / "backups").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaisesRegex(reader.SyncError, "symlink"):
            self.importing()
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_conflict_directory_symlink_is_rejected(self):
        thread_id, _ = self.session(messages=("branch A",))
        _, target = self.session(self.target, thread_id, messages=("branch B",))
        before = target.read_bytes()
        self.export()
        outside = self.root / "outside"
        outside.mkdir()
        (self.target / storage.STATE_DIRECTORY).mkdir()
        (self.target / storage.STATE_DIRECTORY / "conflicts").symlink_to(
            outside, target_is_directory=True
        )
        with self.assertRaisesRegex(reader.SyncError, "symlink"):
            self.importing()
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(outside.iterdir()), [])
