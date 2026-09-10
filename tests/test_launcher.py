"""Native launch boundaries; no native Codex or real home is needed."""

import os
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

from codex_everywhere import cli, launcher, reader, ui
from codex_everywhere.config import Target
from tests.fixtures import SessionFixture


class LauncherTests(SessionFixture):
    def setUp(self):
        super().setUp()
        self.destination = Target(self.target, codex=sys.executable)

    def test_new_and_resume_use_current_environment_and_explicit_checked_storage(self):
        project = self.root / "project with spaces; $(echo literal)"
        project.mkdir()
        sql = self.root / "sqlite with spaces"
        target = replace(self.destination, sqlite_home=sql)
        thread_id, _ = self.session()
        for request in (
            launcher.new_session(target, project),
            launcher.resume_session(target, thread_id, str(project)),
        ):
            with (
                mock.patch.dict(os.environ, {"CE_LAUNCH_TEST": "kept", "CODEX_HOME": "/wrong"}),
                mock.patch.object(launcher.os, "execvpe") as execute,
            ):
                launcher.handoff(target, request)
                binary, args, env = execute.call_args.args
                self.assertEqual(binary, sys.executable)
                self.assertEqual(
                    args[:5], [binary, "-c", 'sqlite_home="' + str(sql) + '"', "--cd", str(project)]
                )
                self.assertEqual(args[5:], ["resume", thread_id] if request.session_id else [])
                self.assertEqual(env["CODEX_HOME"], str(self.target))
                self.assertEqual(env["CODEX_SQLITE_HOME"], str(sql))
                self.assertEqual(env["CE_LAUNCH_TEST"], "kept")
                self.assertEqual(os.environ["CODEX_HOME"], "/wrong")
                self.assertFalse(any("features." in arg or "sandbox" in arg for arg in args))
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertFalse(sql.exists())

    def test_missing_directory_binary_archive_and_invalid_id_fail_before_handoff(self):
        thread_id, _ = self.session()
        for call in (
            lambda: launcher.resume_session(
                self.destination, thread_id, "/missing-foreign-project"
            ),
            lambda: launcher.resume_session(self.destination, "--last", str(self.root)),
            lambda: launcher.resume_session(
                self.destination, thread_id, str(self.root), archived=True
            ),
            lambda: launcher.new_session(
                replace(self.destination, codex=str(self.root / "absent")), self.root
            ),
        ):
            with self.subTest(call=call), self.assertRaises(reader.SyncError):
                call()
        self.assertEqual(list(self.target.iterdir()), [])

    def test_directory_mapping_and_override_are_explicit_and_do_not_rewrite_history(self):
        thread_id, path = self.session()
        before = path.read_bytes()
        target = replace(self.destination, mappings=(("/foreign/project", str(self.root)),))
        request = launcher.resume_session(target, thread_id, "/foreign/project")
        self.assertEqual(request.directory, self.root)
        with self.assertRaises(reader.SyncError):
            launcher.resume_session(target, thread_id, "/foreign/projects")
        override = replace(target, cwd=str(self.root))
        self.assertEqual(
            launcher.resume_session(override, thread_id, "/missing").directory, self.root
        )
        self.assertEqual(path.read_bytes(), before)

    def test_handoff_rechecks_storage_before_starting_codex(self):
        request = launcher.new_session(self.destination, self.root)
        with (
            mock.patch.object(launcher, "check_storage", side_effect=reader.SyncError("not local")),
            mock.patch.object(launcher.os, "execvpe") as execute,
            self.assertRaisesRegex(reader.SyncError, "not local"),
        ):
            launcher.handoff(self.destination, request)
        execute.assert_not_called()

    def test_cli_hands_off_only_after_browser_returns_and_does_not_launch_on_quit(self):
        config = self.root / "config.json"
        config.write_text('{"nodes": []}')
        args = cli.parser().parse_args(["--config", str(config), "--home", str(self.target)])
        request = launcher.LaunchRequest(Path.cwd())
        for result in (None, request):
            events = []
            with (
                mock.patch.object(cli.sys.stdin, "isatty", return_value=True),
                mock.patch.object(cli.sys.stdout, "isatty", return_value=True),
                mock.patch.object(
                    ui,
                    "run",
                    side_effect=lambda *a, events=events, result=result: (
                        events.append("browser closed") or result
                    ),
                ),
                mock.patch.object(
                    launcher,
                    "handoff",
                    side_effect=lambda *a, events=events: events.append("handoff"),
                ),
            ):
                self.assertEqual(cli.execute(args), 0)
            self.assertEqual(
                events, ["browser closed", "handoff"] if result else ["browser closed"]
            )
