import fcntl
import json
import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

from codex_everywhere import codex, reader, transport
from codex_everywhere.config import Node
from codex_everywhere.storage import STATE_DIRECTORY
from tests.fixtures import SessionFixture

HERE = Path(__file__).resolve().parent.parent


class SyncTests(SessionFixture):
    def test_round_trip_excludes_credentials_and_databases(self):
        thread_id, source = self.session()
        for name in ("auth.json", "state_5.sqlite", "config.toml"):
            (self.source / name).write_text("do not copy")
        self.export()
        with zipfile.ZipFile(self.bundle) as z:
            self.assertEqual(len(z.namelist()), 2)
        result = self.importing()
        target = Path(result["plan"][0]["destination"])
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertFalse((self.target / "auth.json").exists())
        self.assertFalse((self.target / "state_5.sqlite").exists())

    def test_selected_session_includes_archived_ancestor(self):
        parent, parent_path = self.session(archive=True)
        child, _ = self.session(
            base={
                "thread_id": parent,
                "end_byte_offset": parent_path.stat().st_size,
                "end_ordinal_exclusive": 2,
            },
            start=2,
        )
        self.session()
        self.export([child])
        result = self.importing()
        self.assertEqual([row["id"] for row in result["plan"]], [parent, child])
        self.assertTrue(
            Path(result["plan"][0]["destination"]).is_relative_to(self.target / "sessions")
        )

    def test_fast_forward_keeps_backup(self):
        thread_id, source = self.session()
        _, target = self.session(self.target, thread_id)
        original = target.read_bytes()
        with source.open("ab") as f:
            f.write(
                json.dumps(
                    {
                        "ordinal": 2,
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "more"},
                    }
                ).encode()
                + b"\n"
            )
        self.export()
        result = self.importing()
        self.assertEqual(result["plan"][0]["action"], "update")
        self.assertEqual(target.read_bytes(), source.read_bytes())
        backup = Path(result["report"]) / "original" / target.relative_to(self.target)
        self.assertEqual(backup.read_bytes(), original)

    def test_local_newer_never_downgrades(self):
        thread_id, _ = self.session()
        _, target = self.session(self.target, thread_id, messages=("first", "local"))
        original = target.read_bytes()
        self.export()
        result = self.importing()
        self.assertEqual(result["plan"][0]["action"], "local-newer")
        self.assertEqual(target.read_bytes(), original)

    def test_idempotent_import(self):
        self.session()
        self.export()
        self.importing()
        result = self.importing()
        self.assertEqual(result["plan"][0]["action"], "same")

    def test_conflict_aborts_entire_batch_and_preserves_incoming(self):
        thread_id, _ = self.session(messages=("source branch",))
        _, local = self.session(self.target, thread_id, messages=("local branch",))
        extra, _ = self.session()
        original = local.read_bytes()
        self.export()
        with self.assertRaisesRegex(reader.SyncError, "Histories diverged"):
            self.importing()
        self.assertEqual(local.read_bytes(), original)
        self.assertNotIn(extra, reader.paths_by_id(self.target))
        copies = list((self.target / STATE_DIRECTORY / "conflicts").glob("*.zip"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), self.bundle.read_bytes())

    def test_dry_run_does_not_modify_destination(self):
        self.session()
        self.export()
        result = self.importing(dry_run=True)
        self.assertEqual(result["plan"][0]["action"], "add")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_incomplete_record_is_rejected(self):
        _, path = self.session()
        with path.open("ab") as f:
            f.write(b'{"type":')
        with self.assertRaisesRegex(reader.SyncError, "Incomplete"):
            self.export()

    def test_bad_ordinal_is_rejected(self):
        _, path = self.session()
        with path.open("ab") as f:
            f.write(b'{"ordinal":1,"type":"event_msg","payload":{}}\n')
        with self.assertRaisesRegex(reader.SyncError, "Discontinuous"):
            self.export()

    def test_missing_ancestor_is_rejected(self):
        child, _ = self.session(
            base={"thread_id": str(uuid.uuid4()), "end_byte_offset": 1, "end_ordinal_exclusive": 2},
            start=2,
        )
        with self.assertRaisesRegex(reader.SyncError, "Missing session"):
            self.export([child])

    def test_invalid_ancestor_byte_boundary_is_rejected(self):
        parent, path = self.session()
        child, _ = self.session(
            base={
                "thread_id": parent,
                "end_byte_offset": path.stat().st_size - 1,
                "end_ordinal_exclusive": 2,
            },
            start=2,
        )
        with self.assertRaisesRegex(reader.SyncError, "Ancestor boundary"):
            self.export([child])

    def test_tampered_bundle_is_rejected(self):
        self.session()
        self.export()
        bad = self.root / "bad.zip"
        with zipfile.ZipFile(self.bundle) as source, zipfile.ZipFile(bad, "w") as dest:
            for name in source.namelist():
                data = source.read(name)
                if name.endswith(".jsonl"):
                    data = data.replace(b"first", b"other")
                dest.writestr(name, data)
        self.bundle = bad
        with self.assertRaisesRegex(reader.SyncError, "does not match"):
            self.importing()
        self.assertEqual(list(self.target.iterdir()), [])

    def test_archive_traversal_is_rejected(self):
        with zipfile.ZipFile(self.bundle, "w") as z:
            z.writestr(
                "manifest.json",
                json.dumps(
                    {"format": 1, "sessions": [{"relative": "sessions/../../escaped.jsonl"}]}
                ),
            )
            z.writestr("sessions/../../escaped.jsonl", b"{}\n")
        with self.assertRaisesRegex(reader.SyncError, "Unsafe"):
            self.importing()
        self.assertFalse((self.root / "escaped.jsonl").exists())

    def test_existing_writer_lock_blocks_import(self):
        self.session()
        self.export()
        lock = self.target / "thread-writer-locks" / (str(uuid.uuid4()) + ".lock")
        lock.parent.mkdir()
        with lock.open("wb") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            with self.assertRaisesRegex(reader.SyncError, "Lock is busy"):
                self.importing()
        self.assertFalse((self.target / "sessions").exists())

    def test_mapping_is_prefix_component_aware(self):
        project = self.root / "project"
        project.mkdir()
        self.assertEqual(
            codex.map_cwd("/old/user/repo", [("/old/user", str(self.root))], str(project)),
            str(project),
        )
        self.assertEqual(codex.map_cwd("/old/user", [("/old/user", str(project))]), str(project))
        with self.assertRaises(reader.SyncError):
            codex.map_cwd("/old/username", [("/old/user", str(project))])

    def test_target_directory_symlink_is_rejected(self):
        self.session()
        self.export()
        outside = self.root / "outside"
        outside.mkdir()
        (self.target / "sessions").mkdir()
        (self.target / "sessions/2026").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(reader.SyncError, "symlink"):
            self.importing()
        self.assertEqual(list(outside.iterdir()), [])

    def test_configured_sqlite_home_requires_explicit_directory(self):
        self.session()
        self.export()
        (self.target / "config.toml").write_text('sqlite_home = "/somewhere/else"\n')
        with self.assertRaisesRegex(reader.SyncError, "--sqlite-home"):
            self.importing()

    def test_ssh_host_rejects_shell_syntax(self):
        with self.assertRaises(reader.SyncError):
            Node("bad", "host;touch bad", str(self.source))

    def test_ssh_uses_configured_host_key_policy_without_prompts(self):
        if not shutil.which("ssh"):
            self.skipTest("requires OpenSSH to inspect effective configuration")
        config = self.root / "ssh_config"
        command = transport.command(Node("test", "test-host", str(self.source)), "scan")
        for policy, expected in ((None, "ask"), ("accept-new", "accept-new"), ("yes", "true")):
            with self.subTest(policy=policy):
                config.write_text(
                    "Host test-host\n"
                    "    HostName test.invalid\n"
                    "    BatchMode no\n"
                    "    UpdateHostKeys yes\n"
                    + (f"    StrictHostKeyChecking {policy}\n" if policy else "")
                )
                # -G evaluates this isolated config without connecting to any host.
                result = subprocess.run(
                    [command[0], "-G", "-F", str(config), *command[1:]],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                effective = dict(line.split(maxsplit=1) for line in result.stdout.splitlines())
                self.assertEqual(effective["stricthostkeychecking"], expected)
                self.assertEqual(effective["updatehostkeys"], "true")
                self.assertEqual(effective["batchmode"], "yes")

    def test_ssh_export_script_and_quoting_with_local_transport_stub(self):
        strange = self.root / "source ' $(touch SHOULD_NOT_EXIST)"
        self.source.rename(strange)
        self.source = strange
        thread_id, _ = self.session()
        binary_dir = self.root / "bin"
        binary_dir.mkdir()
        ssh = binary_dir / "ssh"
        ssh.write_text(
            "#!/usr/bin/env python3\nimport os,sys\nos.execvp('sh',['sh','-c',sys.argv[-1]])\n"
        )
        ssh.chmod(0o700)
        old = os.environ.get("PATH", "")
        os.environ["PATH"] = str(binary_dir) + os.pathsep + old
        try:
            with self.bundle.open("wb") as output:
                transport.receive(
                    Node("test", "test-host", str(self.source)),
                    "export",
                    output,
                    sessions=(thread_id,),
                )
        finally:
            os.environ["PATH"] = old
        result = self.importing()
        self.assertEqual(result["plan"][0]["id"], thread_id)
        self.assertFalse((HERE / "SHOULD_NOT_EXIST").exists())
