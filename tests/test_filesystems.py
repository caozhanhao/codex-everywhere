"""Platform filesystem probes must fail closed before any destination writes."""

import contextlib
import ctypes
import errno
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from codex_everywhere import launcher, reader, safety
from codex_everywhere.config import Target
from tests.fixtures import SessionFixture


class FilesystemTests(SessionFixture):
    @contextlib.contextmanager
    def darwin_volume(self, filesystem=b"apfs", flags=0x1000, symbol="statfs"):
        def query(path, output):
            info = ctypes.cast(output, ctypes.POINTER(safety._DarwinStatFS)).contents
            info.f_fstypename = filesystem
            info.f_flags = flags
            return 0

        libc = mock.Mock(spec=[symbol])
        statfs = getattr(libc, symbol)
        statfs.side_effect = query
        with (
            mock.patch.object(safety.sys, "platform", "darwin"),
            mock.patch.object(safety.ctypes, "CDLL", return_value=libc),
        ):
            yield statfs

    def test_macos_accepts_local_apfs_and_hfs_with_either_statfs_symbol(self):
        for filesystem in (b"apfs", b"hfs"):
            for symbol in ("statfs", "statfs$INODE64"):
                with (
                    self.subTest(filesystem=filesystem, symbol=symbol),
                    self.darwin_volume(filesystem, symbol=symbol),
                ):
                    safety.require_local(self.target)

    def test_macos_rejects_network_unknown_and_unsupported_local_filesystems(self):
        for filesystem, flags in (
            (b"nfs", 0),
            (b"smbfs", 0),
            (b"webdav", 0),
            (b"apfs", 0),
            (b"hfs", 0),
            (b"nfs", 0x1000),
            (b"exfat", 0x1000),
            (b"osxfuse", 0x1000),
            (b"", 0x1000),
        ):
            with (
                self.subTest(filesystem=filesystem, flags=flags),
                self.darwin_volume(filesystem, flags),
            ):
                with self.assertRaisesRegex(reader.SyncError, "must be local"):
                    safety.require_local(self.target)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_macos_read_only_apfs_and_hfs_reject_missing_destinations_without_writes(self):
        destination = self.target / "missing" / "sqlite"
        for filesystem in (b"apfs", b"hfs"):
            with (
                self.subTest(filesystem=filesystem),
                self.darwin_volume(filesystem, 0x1001) as statfs,
            ):
                success = statfs.side_effect

                def query(path, output, success=success):
                    if path != os.fsencode(self.target):
                        ctypes.set_errno(errno.ENOENT)
                        return -1
                    return success(path, output)

                statfs.side_effect = query
                with self.assertRaisesRegex(
                    reader.SyncError, "Local destination.*read-only"
                ) as caught:
                    safety.require_local(destination)
                self.assertIn(str(destination), str(caught.exception))
                self.assertEqual(statfs.call_count, 3)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_read_only_sqlite_home_blocks_new_and_resume_before_codex_handoff(self):
        sqlite_home = self.root / "read-only-sqlite"
        target = Target(self.target, sqlite_home, codex=sys.executable)
        thread_id, _ = self.session(self.target)
        for session_id in (None, thread_id):
            with (
                self.subTest(session_id=session_id),
                self.darwin_volume() as statfs,
                mock.patch.object(launcher.os, "execvpe") as execute,
            ):
                success = statfs.side_effect

                def query(path, output, success=success):
                    result = success(path, output)
                    if path == os.fsencode(sqlite_home):
                        info = ctypes.cast(output, ctypes.POINTER(safety._DarwinStatFS)).contents
                        info.f_flags |= 1
                    return result

                statfs.side_effect = query
                with self.assertRaisesRegex(reader.SyncError, "read-only filesystem") as caught:
                    launcher.handoff(target, launcher.LaunchRequest(self.root, session_id))
                self.assertIn(str(sqlite_home), str(caught.exception))
                self.assertIn("--sqlite-home", str(caught.exception))
                execute.assert_not_called()
        self.assertFalse(sqlite_home.exists())
        self.assertEqual(list(self.target.iterdir()), [self.target / "sessions"])

    def test_macos_checks_nearest_existing_parent_without_creating_directories(self):
        destination = self.target / "new 会话" / "rollout.jsonl"
        with self.darwin_volume() as statfs:
            success = statfs.side_effect

            def query(path, output):
                if path != os.fsencode(self.target.resolve()):
                    ctypes.set_errno(errno.ENOENT)
                    return -1
                return success(path, output)

            statfs.side_effect = query
            safety.require_local(destination)
        self.assertEqual(
            [call.args[0] for call in statfs.call_args_list],
            [os.fsencode(p.resolve()) for p in (destination, destination.parent, self.target)],
        )
        self.assertEqual(list(self.target.iterdir()), [])

    def test_macos_missing_path_on_network_volume_is_rejected(self):
        with self.darwin_volume(b"nfs", 0) as statfs:
            success = statfs.side_effect

            def query(path, output):
                if path != os.fsencode(self.target.resolve()):
                    ctypes.set_errno(errno.ENOENT)
                    return -1
                return success(path, output)

            statfs.side_effect = query
            with self.assertRaisesRegex(reader.SyncError, "must be local"):
                safety.require_local(self.target / "missing" / "rollout.jsonl")

    def test_macos_probe_errors_do_not_fall_back_to_a_local_parent(self):
        for error in (errno.EACCES, errno.EIO, errno.ENOTDIR, errno.ELOOP):
            with self.subTest(error=error), self.darwin_volume() as statfs:
                statfs.side_effect = None
                statfs.return_value = -1
                with mock.patch.object(safety.ctypes, "get_errno", return_value=error):
                    with self.assertRaisesRegex(reader.SyncError, "Cannot verify local"):
                        safety.require_local(self.target / "sessions")
                self.assertEqual(statfs.call_count, 1)

    def test_macos_unavailable_root_is_unverified(self):
        with self.darwin_volume() as statfs:
            statfs.side_effect = None
            statfs.return_value = -1
            with mock.patch.object(safety.ctypes, "get_errno", return_value=errno.ENOENT):
                with self.assertRaisesRegex(reader.SyncError, "Cannot verify local"):
                    safety.require_local(Path("/"))
            self.assertEqual(statfs.call_count, 1)

    def test_macos_unavailable_native_api_is_unverified(self):
        with mock.patch.object(safety.sys, "platform", "darwin"):
            for library in (
                {"side_effect": OSError("libSystem unavailable")},
                {"return_value": mock.Mock(spec=[])},
            ):
                with mock.patch.object(safety.ctypes, "CDLL", **library):
                    with self.assertRaisesRegex(reader.SyncError, "Cannot verify local"):
                        safety.require_local(self.target)

    def test_macos_checks_nested_mounts_and_separate_sqlite_home(self):
        for folder in (
            "sessions",
            "archived_sessions",
            ".codex-everywhere",
            "thread-writer-locks",
            "sqlite",
        ):
            with self.subTest(folder=folder), self.darwin_volume() as statfs:
                success = statfs.side_effect
                remote = self.target / folder

                def query(path, output, success=success, remote=remote):
                    result = success(path, output)
                    if path == os.fsencode(remote.resolve()):
                        info = ctypes.cast(output, ctypes.POINTER(safety._DarwinStatFS)).contents
                        info.f_fstypename = b"nfs"
                        info.f_flags = 0
                    return result

                statfs.side_effect = query
                with self.assertRaisesRegex(reader.SyncError, "must be local"):
                    safety.check_storage(self.target, remote if folder == "sqlite" else None)
        self.assertEqual(list(self.target.iterdir()), [])

    @unittest.skipUnless(sys.platform == "darwin", "requires native macOS statfs")
    def test_native_macos_existing_and_missing_paths_and_symlinked_home(self):
        safety.check_storage(self.target, self.target / "new-sqlite")
        database = self.target / "state_5.sqlite"
        database.touch()
        alias = self.root / "alias"
        alias.symlink_to(self.target, target_is_directory=True)
        safety.check_storage(alias, None)
        with self.assertRaisesRegex(reader.SyncError, "Cannot verify local"):
            safety.require_local(database / "child")
        self.assertEqual(list(self.target.iterdir()), [database])

    def test_linux_uses_the_deepest_mount_and_decodes_escaped_paths(self):
        destination = self.target / "nested mount" / "sessions"
        mount = str(destination.parent.resolve()).replace(" ", r"\040")
        for root_type, nested_type, rejected in (("ext4", "nfs4", True), ("nfs4", "ext4", False)):
            mounts = (
                f"1 0 0:1 / / rw - {root_type} root rw\n"
                f"2 1 0:2 / {mount} rw - {nested_type} nested rw\n"
            )
            with (
                self.subTest(root=root_type, nested=nested_type),
                mock.patch.object(safety.sys, "platform", "linux"),
                mock.patch.object(Path, "exists", return_value=True),
                mock.patch.object(Path, "read_text", return_value=mounts),
            ):
                if rejected:
                    with self.assertRaisesRegex(reader.SyncError, "must be local"):
                        safety.require_local(destination)
                else:
                    safety.require_local(destination)

    def test_linux_missing_mountinfo_is_unverified(self):
        with (
            mock.patch.object(safety.sys, "platform", "linux"),
            mock.patch.object(Path, "exists", return_value=False),
        ):
            with self.assertRaisesRegex(reader.SyncError, "Cannot verify local"):
                safety.require_local(self.target)

    def test_linux_checks_read_only_bind_mounts_and_superblocks_at_the_deepest_mount(self):
        destination = self.target / "sqlite"
        for parent_options, mount_options, super_options, rejected in (
            ("rw", "ro,relatime", "rw", True),
            ("rw", "rw,relatime", "ro", True),
            ("ro", "rw,relatime", "rw", False),
            ("rw", "rw,relatime", "rw,errors=remount-ro", False),
        ):
            mounts = (
                f"1 0 0:1 / / {parent_options} - ext4 root {parent_options}\n"
                f"2 1 0:2 / {destination} {mount_options} - ext4 nested {super_options}\n"
            )
            with (
                self.subTest(parent=parent_options, mount=mount_options, superblock=super_options),
                mock.patch.object(safety.sys, "platform", "linux"),
                mock.patch.object(Path, "exists", return_value=True),
                mock.patch.object(Path, "read_text", return_value=mounts),
            ):
                if rejected:
                    with self.assertRaisesRegex(reader.SyncError, "read-only filesystem"):
                        safety.require_local(destination / "missing" / "state.sqlite")
                else:
                    safety.require_local(destination / "missing" / "state.sqlite")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_unsupported_platform_is_rejected(self):
        with mock.patch.object(safety.sys, "platform", "freebsd"):
            with self.assertRaisesRegex(reader.SyncError, "requires Linux or macOS"):
                safety.require_local(self.target)
