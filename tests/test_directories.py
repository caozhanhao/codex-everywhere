"""Choose local working directories without restarting or losing a transfer."""

import curses
import sys
from dataclasses import replace
from pathlib import Path
from unittest import mock

from codex_everywhere import codex, fleet, launcher, reader, service, ui
from codex_everywhere.browser import BrowseOptions
from codex_everywhere.config import Node, Settings, Target
from tests.fixtures import HERE, SessionFixture
from tests.test_browser import FakeScreen, IdleScan


class DirectoryTests(SessionFixture):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(ui, "ScanJob", IdleScan)
        patch.start()
        self.addCleanup(patch.stop)
        self.node = Node("server-a", "server-a", str(self.source))
        self.settings = Settings((self.node,), Target(self.target, codex=sys.executable))
        self.browser = ui.Browser(
            FakeScreen(), self.settings, BrowseOptions(directory=self.root, global_scope=True)
        )
        self.addCleanup(self.browser.close)

    def select(self, thread_id, *, remote=False):
        node, home = (self.node, self.source) if remote else (None, self.target)
        self.browser.model.ingest(
            [
                fleet.NodeResult(
                    node.name if node else "local", node, tuple(reader.scan(home)["sessions"])
                )
            ]
        )
        self.browser.model.selected = next(
            index for index, row in enumerate(self.browser.model.rows) if row.id == thread_id
        )

    def finish(self):
        self.browser.task.result(timeout=5)
        self.browser.poll()

    def prepare(self, thread_id):
        self.select(thread_id, remote=True)

        def receive(node, operation, output, **kwargs):
            reader.export_bundle(Path(node.home), output, kwargs["sessions"])

        with mock.patch.object(service.transport, "receive", side_effect=receive):
            self.browser.key("\n")
            self.finish()

    def test_missing_local_directory_defaults_to_current_and_keeps_captured_session(self):
        thread_id, path = self.session(self.target, cwd="/remote/project")
        original = path.read_bytes()
        self.select(thread_id)
        self.browser.key("\n")
        self.assertEqual(self.browser.phase, "directory")
        self.assertEqual(self.browser.directory_prompt.choices, ("current", "other", "back"))
        self.browser.draw()
        self.assertIn("/remote/project", self.browser.screen.snapshot())
        self.assertNotIn("Retry", self.browser.screen.snapshot())
        self.assertIsNone(self.browser.launch_request)
        self.browser.model.selected = -1
        self.browser.key("\n")
        self.assertEqual(self.browser.launch_request, launcher.LaunchRequest(self.root, thread_id))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.browser.settings, self.settings)
        self.browser.back()
        self.assertIsNone(self.browser.open_target.cwd)

    def test_existing_other_directory_offers_session_and_current_before_opening(self):
        project = self.root / "another project"
        project.mkdir()
        thread_id, _ = self.session(self.target, cwd=project)
        self.select(thread_id)
        for choice, expected in ((0, project), (1, self.root)):
            with self.subTest(choice=choice):
                self.browser.key("\n")
                self.assertEqual(self.browser.phase, "directory")
                self.assertIsNone(self.browser.launch_request)
                if choice:
                    self.browser.key(curses.KEY_DOWN)
                self.browser.key("\n")
                self.assertEqual(self.browser.launch_request.directory, expected)
                self.browser.back()
                self.browser.launch_request = None

    def test_explicit_and_remembered_directories_skip_prompt_until_unavailable(self):
        project = self.root / "chosen"
        project.mkdir()
        thread_id, _ = self.session(self.target, cwd="/remote/project")
        for target in (
            replace(self.settings.target, cwd=str(project)),
            replace(self.settings.target, mappings=(("/remote/project", str(project)),)),
        ):
            with self.subTest(target=target):
                self.browser.settings = replace(self.settings, target=target)
                self.select(thread_id)
                self.browser.key("\n")
                self.assertEqual(self.browser.launch_request.directory, project)
                self.browser.back()
                self.browser.launch_request = None
        self.browser.settings = self.settings
        with mock.patch.object(
            launcher,
            "read_locations",
            return_value={thread_id: {"original_cwd": "/remote/project", "cwd": str(project)}},
        ):
            self.browser.key("\n")
            self.assertEqual(self.browser.launch_request.directory, project)
            self.browser.back()
            self.browser.launch_request = None
            project.rmdir()
            self.browser.key("\n")
            self.assertEqual(self.browser.phase, "directory")
            self.assertEqual(self.browser.directory_prompt.missing, str(project))
            self.browser.key("\n")
            self.assertEqual(self.browser.launch_request.directory, self.root)

    def test_same_directory_through_symlink_opens_directly(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        thread_id, _ = self.session(self.target, cwd=alias)
        self.select(thread_id)
        self.browser.key("\n")
        self.assertEqual(self.browser.launch_request.directory, self.root)

    def test_other_directory_edits_unicode_paths_and_validates_without_leaving_input(self):
        project = self.root / "项目 with spaces; $(literal)"
        project.mkdir()
        thread_id, path = self.session(self.target, cwd="/remote/project")
        original = path.read_bytes()
        self.select(thread_id)
        self.browser.key("\n")
        self.browser.key(curses.KEY_DOWN)
        self.browser.key("\n")
        self.assertEqual(self.browser.phase, "directory_input")
        for invalid in ("", "missing", str(path)):
            self.browser.key("\x15")
            for char in invalid:
                self.browser.key(char)
            self.browser.key("\n")
            self.assertEqual(self.browser.phase, "directory_input")
            self.assertTrue(self.browser.directory_prompt.error)
            self.assertIsNone(self.browser.launch_request)
            self.assertFalse((self.root / "missing").exists())
        self.browser.key("\x15")
        # Relative paths use the launch directory; punctuation remains literal.
        for char in project.name + "x":
            self.browser.key(char)
        self.browser.key(curses.KEY_LEFT)
        self.browser.key(curses.KEY_DC)
        self.browser.key(curses.KEY_HOME)
        self.browser.key("x")
        self.browser.key(curses.KEY_BACKSPACE)
        self.browser.key(curses.KEY_END)
        self.browser.key("\n")
        self.assertEqual(self.browser.launch_request, launcher.LaunchRequest(project, thread_id))
        self.assertEqual(path.read_bytes(), original)

    def test_directory_input_back_preserves_selection_then_returns_without_opening(self):
        thread_id, _ = self.session(self.target, cwd="/remote/project")
        self.select(thread_id)
        for exit_keys in (("\x1b",), (curses.KEY_DOWN, "\n")):
            self.browser.key("\n")
            self.browser.key(curses.KEY_DOWN)
            self.browser.key("\n")
            self.browser.key("\x1b")
            self.assertEqual(self.browser.phase, "directory")
            self.assertEqual(self.browser.directory_prompt.selected, 1)
            for key in exit_keys:
                self.browser.key(key)
            self.assertEqual(self.browser.phase, "browse")
            self.assertIsNone(self.browser.launch_request)
            self.assertEqual(self.browser.model.group.id, thread_id)

    def test_resizing_below_minimum_does_not_accept_an_invisible_directory_choice(self):
        thread_id, _ = self.session(self.target, cwd="/remote/project")
        self.select(thread_id)
        self.browser.key("\n")
        self.browser.screen = FakeScreen(10, 40)
        self.browser.key("\n")
        self.assertEqual(self.browser.phase, "directory")
        self.assertIsNone(self.browser.launch_request)
        self.browser.key("\x1b")
        self.assertEqual(self.browser.phase, "browse")

    def test_remote_directory_choice_reuses_download_and_confirms_ancestor_locations(self):
        parent, path = self.session(cwd="/remote/parent")
        child, child_path = self.session(
            cwd="/remote/project",
            base={
                "thread_id": parent,
                "end_byte_offset": path.stat().st_size,
                "end_ordinal_exclusive": 2,
            },
            start=2,
        )
        originals = path.read_bytes(), child_path.read_bytes()
        self.prepare(child)
        self.assertEqual(self.browser.phase, "directory")
        self.assertTrue(self.browser.directory_prompt.ancestors)
        stage = self.browser.transfer.prepared.stage
        self.assertEqual(list(self.target.iterdir()), [])
        with mock.patch.object(service, "prepare_pull", side_effect=AssertionError("redownload")):
            self.browser.key("\n")
            self.finish()
        self.assertEqual(self.browser.phase, "preview")
        self.assertFalse(stage.exists())
        prepared = self.browser.transfer.prepared
        self.assertEqual(prepared.directories, {parent: str(self.root), child: str(self.root)})
        self.assertEqual(prepared.directory_updates, 2)
        self.assertEqual(list(self.target.iterdir()), [])
        apply = service.apply
        with mock.patch.object(service, "apply", side_effect=lambda p, **kw: apply(p, index=False)):
            self.browser.key("\n")
            self.finish()
        self.assertEqual(self.browser.launch_request, launcher.LaunchRequest(self.root, child))
        self.assertEqual(reader.read_locations(self.target)[child]["cwd"], str(self.root))
        self.assertEqual((path.read_bytes(), child_path.read_bytes()), originals)
        self.assertEqual(reader.paths_by_id(self.target)[child].read_bytes(), originals[1])

    def test_missing_ancestor_directory_is_resolved_before_sync_confirmation(self):
        parent, path = self.session(cwd="/remote/parent")
        child, _ = self.session(
            base={
                "thread_id": parent,
                "end_byte_offset": path.stat().st_size,
                "end_ordinal_exclusive": 2,
            },
            start=2,
        )
        self.prepare(child)
        self.assertEqual(self.browser.phase, "directory")
        self.assertEqual(self.browser.directory_prompt.missing, "/remote/parent")
        self.browser.key("\n")
        self.finish()
        self.assertEqual(self.browser.phase, "preview")
        self.assertEqual(self.browser.transfer.prepared.directories[parent], str(self.root))

    def test_cancel_directory_choice_removes_staging_without_modifying_destination(self):
        thread_id, _ = self.session(cwd="/remote/project")
        self.prepare(thread_id)
        stage = self.browser.transfer.prepared.stage
        self.browser.key("\x1b")
        self.assertEqual(self.browser.phase, "browse")
        self.assertFalse(stage.exists())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_directory_removed_after_preview_returns_to_chooser_and_reconfirms_changes(self):
        project = self.root / "chosen"
        project.mkdir()
        thread_id, _ = self.session(cwd=project)
        self.prepare(thread_id)
        self.browser.key("\n")  # Use session directory.
        self.finish()
        self.assertEqual(self.browser.phase, "preview")
        project.rmdir()
        with mock.patch.object(codex, "check_version"):
            self.browser.key("\n")
            with self.assertRaises(codex.DirectoryUnavailable):
                self.browser.task.result(timeout=5)
            self.browser.poll()
        self.assertEqual(self.browser.phase, "directory")
        self.assertIsNone(self.browser.launch_request)
        self.assertEqual(list(self.target.iterdir()), [])
        self.browser.key("\n")
        self.finish()
        self.assertEqual(self.browser.phase, "preview")
        self.assertEqual(self.browser.transfer.prepared.directories[thread_id], str(self.root))
        self.assertEqual(list(self.target.iterdir()), [])

    def test_conflict_can_be_saved_without_choosing_a_working_directory(self):
        thread_id, _ = self.session(cwd="/remote/project", messages=("remote",))
        self.session(self.target, thread_id, messages=("local",))
        self.prepare(thread_id)
        self.assertEqual(self.browser.phase, "preview")
        self.assertTrue(self.browser.transfer.prepared.has_conflicts)
        self.assertIsNone(self.browser.directory_prompt)

    def test_missing_foreign_path_is_not_resolved_through_local_symlinks_in_error(self):
        directory = "/home/remote-user/unavailable-project"
        with self.assertRaises(codex.DirectoryUnavailable) as failure:
            codex.map_cwd(directory, ())
        self.assertEqual(failure.exception.directory, directory)
        self.assertIn(directory, str(failure.exception))
        self.assertNotIn("/System/Volumes", str(failure.exception))

    def render_samples(self):
        self.browser.model.options.directory = Path("/work/project")
        samples = {}
        for valid in (False, True):
            prompt = ui.DirectoryPrompt(str(self.root), self.root)
            prompt.session = "/work/another-project" if valid else "/home/dev/project"
            prompt.current = Path("/work/project")
            prompt.missing = None if valid else prompt.session
            prompt.choices = (
                ("session", "current", "other", "back") if valid else ("current", "other", "back")
            )
            self.browser.directory_prompt = prompt
            self.browser.phase = "directory"
            for height, width in ((24, 80), (18, 48), (12, 42)):
                self.browser.screen = FakeScreen(height, width)
                self.browser.draw()
                samples[f"directory_{'existing' if valid else 'missing'}_{width}"] = (
                    self.browser.screen.snapshot()
                )
        prompt.ancestors = True
        prompt.missing = "/home/dev/ancestor-project"
        for height, width in ((18, 48), (12, 42)):
            self.browser.screen = FakeScreen(height, width)
            self.browser.draw()
            samples[f"directory_ancestors_{width}"] = self.browser.screen.snapshot()
        prompt.text = "/work/一个很长的目录名称/另一个目录/项目 with spaces"
        prompt.cursor = len(prompt.text)
        prompt.error = "Directory unavailable. Enter an existing local path."
        self.browser.phase = "directory_input"
        for height, width in ((24, 80), (18, 48), (12, 42)):
            self.browser.screen = FakeScreen(height, width)
            self.browser.draw()
            samples[f"directory_input_{width}"] = self.browser.screen.snapshot()
            row, col = self.browser.screen.getyx()
            self.assertEqual(row, 6)
            self.assertLess(col, width - 1)
            self.assertEqual(self.browser.screen.cells[row][col], " ")
        return samples

    def test_reviewed_directory_screens_and_unicode_cursor(self):
        for name, rendered in self.render_samples().items():
            with self.subTest(screen=name):
                self.assertEqual(rendered, (HERE / "tests/snapshots" / (name + ".txt")).read_text())
