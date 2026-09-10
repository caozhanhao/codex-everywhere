"""User-facing screens and confirmation/selection state, without real fleet access."""

import curses
import io
import sys
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from unittest import mock

from codex_everywhere import fleet, launcher, presentation, reader, service, ui
from codex_everywhere.browser import BrowseOptions, BrowserModel, mapped_directory
from codex_everywhere.cli import parser
from codex_everywhere.config import Node, Settings, Target
from codex_everywhere.plan import Action
from codex_everywhere.storage import STATE_DIRECTORY
from tests.fixtures import HERE, SessionFixture


class FakeScreen:
    def __init__(self, height=24, width=80):
        self.height, self.width = height, width
        self.erase()

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, row, col, value, style=0):
        assert col + presentation.columns(value) < self.width
        for char in value:
            size = presentation.columns(char)
            if not size:
                if col:
                    self.cells[row][col - 1] += char
                continue
            self.cells[row][col] = char
            if size == 2:
                self.cells[row][col + 1] = ""
            col += size
        self.cursor = (row, col)

    def move(self, row, col):
        assert 0 <= row < self.height and 0 <= col < self.width
        self.cursor = (row, col)

    def getyx(self):
        return self.cursor

    def erase(self):
        self.cells = [[" "] * self.width for _ in range(self.height)]
        self.cursor = (0, 0)

    def refresh(self):
        pass

    def snapshot(self):
        return "\n".join("".join(row).rstrip() for row in self.cells).rstrip() + "\n"


class IdleScan:
    def __init__(self, settings):
        self.pending = {}

    def poll(self):
        return []

    def close(self):
        pass


class BrowserTests(SessionFixture):
    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(ui, "ScanJob", IdleScan).start()
        mock.patch.object(ui.socket, "gethostname", return_value="workstation.example").start()
        self.node_a = Node("server-a", "server-a", str(self.source))
        self.node_b = Node("server-b", "server-b", str(self.source))
        self.settings = Settings(
            (self.node_a, self.node_b), Target(self.target, codex=sys.executable)
        )
        self.browser = ui.Browser(FakeScreen(), self.settings, BrowseOptions(directory=self.root))
        self.addCleanup(self.browser.close)

    def result(self, node, entries):
        name = node.name if node else "local"
        return fleet.NodeResult(name, node, tuple(entries))

    def entries(self):
        return reader.scan(self.source)["sessions"]

    def test_default_scope_includes_local_and_remote_without_duplicate_rows(self):
        _, path = self.session()
        entry = self.entries()[0]
        other = {
            **entry,
            "id": "00000000-0000-0000-0000-000000000002",
            "cwd": str(self.root / "other"),
        }
        self.browser.model.ingest([self.result(None, [entry, other])])
        self.assertEqual([group.id for group in self.browser.model.rows], [entry["id"]])
        self.assertIsNone(self.browser.model.copy.node)
        self.browser.model.ingest([self.result(self.node_a, [entry, other])])
        self.assertEqual([group.id for group in self.browser.model.rows], [entry["id"]])
        self.browser.key("g")
        self.assertEqual(len(self.browser.model.rows), 2)
        self.browser.key("g")
        self.assertEqual(len(self.browser.model.rows), 1)
        self.assertEqual(
            path.read_bytes(), reader.paths_by_id(self.source)[entry["id"]].read_bytes()
        )

    def test_search_accepts_the_displayed_local_hostname(self):
        thread_id, _ = self.session()
        self.browser.model.ingest([self.result(None, self.entries())])
        self.browser.model.search("workstation")
        self.assertEqual(self.browser.model.group.id, thread_id)

    def test_remote_name_remains_visible_when_local_copy_has_no_name_index(self):
        self.session()
        entry = self.entries()[0]
        named = {**entry, "title": "renamed conversation"}
        model = self.browser.model
        model.ingest([self.result(None, [entry]), self.result(self.node_a, [named])])
        self.assertEqual(model.copy.name, "local")
        self.assertEqual(model.display_title(model.group), named["title"])
        model.search("renamed conversation")
        self.assertEqual(len(model.rows), 1)
        model.ingest([self.result(None, [{**entry, "title": "local custom name"}])])
        self.assertEqual(model.display_title(model.group), "local custom name")

    def test_archived_view_keeps_internal_sessions_hidden(self):
        self.session()
        entry = self.entries()[0]
        archived = {**entry, "id": "00000000-0000-0000-0000-000000000002", "archived": True}
        internal = {**entry, "id": "00000000-0000-0000-0000-000000000003", "source": "subagent"}
        self.browser.model.ingest([self.result(self.node_a, [entry, archived, internal])])
        self.assertEqual(len(self.browser.model.rows), 1)
        self.browser.key("a")
        self.assertEqual(len(self.browser.model.rows), 2)
        self.browser.key("i")
        self.assertEqual(len(self.browser.model.rows), 2)
        self.browser.key("g")
        self.assertEqual(len(self.browser.model.rows), 2)
        self.assertFalse(self.browser.model.options.include_internal)

    def test_cwd_mapping_uses_complete_prefix_and_does_not_change_source(self):
        self.session()
        entry = self.entries()[0]
        entry["cwd"] = "/remote/project/subdir"
        model = BrowserModel(
            BrowseOptions(directory=self.root / "subdir"), (("/remote/project", str(self.root)),)
        )
        model.ingest([self.result(self.node_a, [entry])])
        self.assertEqual(len(model.rows), 1)
        self.assertEqual(entry["cwd"], "/remote/project/subdir")
        self.assertEqual(
            mapped_directory("/remote/projects/subdir", model.mappings), "/remote/projects/subdir"
        )

    def test_same_remote_path_uses_only_its_nodes_mapping(self):
        self.session()
        entry = {**self.entries()[0], "cwd": "/remote/work"}
        node_a = replace(self.node_a, mappings=(("/remote/work", str(self.root)),))
        node_b = replace(self.node_b, mappings=(("/remote/work", str(self.root / "other")),))
        self.browser.model.ingest([self.result(node_a, [entry]), self.result(node_b, [entry])])
        self.assertEqual(
            [c.name for c in self.browser.model.sources(self.browser.model.group)], ["server-a"]
        )
        self.browser.model.options.directory = self.root / "other"
        self.browser.model.rebuild()
        self.assertEqual(
            [c.name for c in self.browser.model.sources(self.browser.model.group)], ["server-b"]
        )

    def test_delayed_node_does_not_change_source_or_selection(self):
        thread_id, _ = self.session()
        entries = self.entries()
        node_c = Node("server-c", "server-c", str(self.source))
        model = self.browser.model
        model.ingest([self.result(self.node_b, entries), self.result(node_c, entries)])
        self.browser.key("\t")
        self.assertEqual(model.copy.name, "server-c")
        model.ingest([self.result(self.node_a, entries)])
        self.assertEqual((model.group.id, model.copy.name), (thread_id, "server-c"))
        model.ingest([fleet.NodeResult("server-c", node_c, error="offline")])
        self.assertIsNone(model.copy)
        with mock.patch.object(service, "prepare_pull") as prepare:
            self.browser.key("\n")
            prepare.assert_not_called()
        self.browser.key("\t")
        self.assertEqual(model.copy.name, "server-a")

    def test_copy_choice_is_remembered_when_moving_between_conversations(self):
        self.session()
        self.session()
        self.browser.model.ingest(
            [self.result(self.node_a, self.entries()), self.result(self.node_b, self.entries())]
        )
        first_id = self.browser.model.group.id
        self.browser.key("\t")
        self.browser.model.move(1)
        self.browser.model.move(-1)
        self.assertEqual(
            (self.browser.model.group.id, self.browser.model.copy.name), (first_id, "server-b")
        )

    def test_scan_status_finishes_and_refresh_cannot_accept_old_results(self):
        self.session()
        old_scan = self.browser.scan
        old_scan.pending = {object(): self.node_a}
        self.assertEqual(
            presentation.catalog_label(self.browser.model.results, self.browser.pending),
            "Updating…",
        )
        self.browser.refresh_catalog()
        old_scan.poll = lambda: [self.result(self.node_a, self.entries())]
        self.browser.poll()
        self.assertEqual(
            presentation.catalog_label(self.browser.model.results, self.browser.pending), "Sessions"
        )
        self.assertEqual(self.browser.model.results, {})

    def test_search_uses_terminal_cursor_after_footer_redraws(self):
        browser = self.browser
        with mock.patch.object(ui.curses, "curs_set") as visibility:
            browser.key("/")
            for query, column in (("", 4), ("中", 6), ("中文ab", 10), ("e\u0301", 5)):
                browser.model.search(query)
                for _ in range(2):
                    browser.draw()
                    self.assertEqual(browser.screen.getyx(), (2, column))
                    visibility.assert_called_with(1)
                    self.assertNotIn("▏", browser.screen.snapshot())
            browser.key("\n")
            browser.draw()
            visibility.assert_called_with(0)
            browser.key("/")
            browser.key("\x1b")
            browser.draw()
            visibility.assert_called_with(0)
            browser.key("/")
            browser.screen = FakeScreen(10, 40)
            browser.draw()
            visibility.assert_called_with(0)

    def test_long_search_keeps_end_visible_without_changing_query_on_resize(self):
        browser = self.browser
        query = "prefix " + "中文" * 60 + "尾声A"
        browser.model.search(query)
        browser.searching = True
        browser.model.options.include_archived = True
        browser.model.options.include_internal = True
        with mock.patch.object(ui.curses, "curs_set"):
            for width in (80, 42, 120):
                browser.screen = FakeScreen(24, width)
                browser.draw()
                row, column = browser.screen.getyx()
                self.assertEqual(row, 2)
                self.assertEqual(browser.screen.cells[row][column - 1], "A")
                self.assertEqual(browser.screen.cells[row][column], " ")
                self.assertIn("尾声A", browser.screen.snapshot().splitlines()[2])
                self.assertIn("+internal", browser.screen.snapshot().splitlines()[2])
                self.assertEqual(browser.model.query, query)

    def test_search_navigation_and_details_are_read_only(self):
        thread_id, _ = self.session(
            thread_id="00000000-0000-0000-0000-000000000001", messages=("修复中文搜索",)
        )
        self.session(thread_id="00000000-0000-0000-0000-000000000002", messages=("中文候选窗",))
        self.browser.model.ingest([self.result(self.node_a, self.entries())])
        with mock.patch.object(service, "apply", side_effect=AssertionError("must not import")):
            self.browser.key("/")
            for char in "中文":
                self.browser.key(char)
            self.assertEqual(self.browser.model.group.id, thread_id)
            for key, selected in (
                (curses.KEY_DOWN, 1),
                (curses.KEY_UP, 0),
                (curses.KEY_NPAGE, 1),
                (curses.KEY_PPAGE, -1),
                (curses.KEY_DOWN, 0),
            ):
                self.browser.key(key)
                self.browser.draw()
                self.assertEqual(self.browser.model.selected, selected)
                self.assertEqual(self.browser.model.query, "中文")
                self.assertTrue(self.browser.searching)
                self.assertEqual(self.browser.screen.getyx(), (2, 8))
                self.assertIsNone(self.browser.launch_request)
            for char in "jk":
                self.browser.key(char)
            self.assertEqual(self.browser.model.query, "中文jk")
            for _ in range(2):
                self.browser.key(curses.KEY_BACKSPACE)
            self.browser.key("\n")
            self.assertEqual(self.browser.phase, "browse")
            for key in ("?", "n", "d"):
                self.browser.key(key)
                self.browser.draw()
                self.browser.key("\x1b")
            self.browser.key("/")
            self.browser.key("\x1b")
            self.assertEqual(self.browser.model.query, "")
        self.assertEqual(list(self.target.iterdir()), [])

    def test_escape_leaves_search_and_panels_then_exits_the_list(self):
        self.assertTrue(self.browser.key("q"))
        self.assertEqual(self.browser.phase, "browse")
        self.browser.key("/")
        self.assertTrue(self.browser.key("q"))
        self.assertEqual(self.browser.model.query, "q")
        self.assertTrue(self.browser.searching)
        self.assertTrue(self.browser.key("\x1b"))
        self.assertFalse(self.browser.searching)
        self.assertEqual(self.browser.model.query, "")
        for key, phase in (("?", "help"), ("n", "nodes"), ("d", "details")):
            self.browser.key(key)
            self.assertTrue(self.browser.key("q"))
            self.assertEqual(self.browser.phase, phase)
            self.assertTrue(self.browser.key("\x1b"))
            self.assertEqual(self.browser.phase, "browse")
        # A finished search is part of the list; Escape exits even with a filter.
        self.browser.key("/")
        self.browser.key("q")
        self.browser.key("\n")
        self.assertFalse(self.browser.key("\x1b"))
        self.assertIsNone(self.browser.launch_request)

    def test_escape_still_returns_and_exits_after_terminal_shrinks(self):
        for key in ("/", "?", "n", "d"):
            with self.subTest(key=key):
                self.browser.screen = FakeScreen()
                self.browser.key(key)
                self.browser.screen = FakeScreen(10, 40)
                self.browser.draw()
                self.assertTrue(self.browser.key("\x1b"))
                self.assertEqual(self.browser.phase, "browse")
                self.assertFalse(self.browser.searching)
                self.browser.draw()
                self.assertIn("Esc Exit", self.browser.screen.snapshot())
                self.assertTrue(self.browser.key("q"))
                self.assertFalse(self.browser.key("\x1b"))

    def test_hidden_ancestor_still_belongs_to_export(self):
        parent, path = self.session(archive=True)
        child, _ = self.session(
            base={
                "thread_id": parent,
                "end_byte_offset": path.stat().st_size,
                "end_ordinal_exclusive": 2,
            },
            start=2,
        )
        self.browser.model.ingest([self.result(self.node_a, self.entries())])
        self.assertEqual([group.id for group in self.browser.model.rows], [child])
        sessions, order = reader.collect(self.source, [child])
        self.assertEqual(set(sessions), {parent, child})
        self.assertEqual(order, [parent, child])
        reader.export_bundle(self.source, io.BytesIO(), [child])

    def transfer(self):
        thread_id, _ = self.session()
        self.export([thread_id])
        manager = service.prepare_file(self.bundle, Target(self.target, codex=sys.executable))
        self.browser.transfer = ui.Transfer(
            manager, manager.__enter__(), thread_id, "server-a", "Fixture conversation"
        )
        self.browser.phase = "preview"
        return thread_id

    def test_details_cannot_apply_and_back_keeps_preview_snapshot(self):
        self.transfer()
        transfer = self.browser.transfer
        with mock.patch.object(service, "apply", side_effect=AssertionError("must not import")):
            self.browser.key("d")
            self.browser.key("\n")
            self.assertEqual(self.browser.phase, "details")
            self.browser.key("\x1b")
            self.assertEqual(self.browser.phase, "preview")
            self.assertTrue(transfer.prepared.stage.exists())
            self.browser.key("\x1b")
            self.assertFalse(transfer.prepared.stage.exists())
        self.assertEqual(list(self.target.iterdir()), [])

    def test_success_opens_codex_after_apply_without_starting_another_scan(self):
        thread_id = self.transfer()
        future = Future()
        future.set_result(self.root / "operation-record")
        self.browser.task, self.browser.task_kind = future, "apply"
        with mock.patch.object(self.browser, "refresh_catalog") as refresh:
            self.browser.poll()
            refresh.assert_not_called()
        self.assertEqual(self.browser.launch_request, launcher.LaunchRequest(self.root, thread_id))
        stage = self.browser.transfer.prepared.stage
        self.browser.close()
        self.assertFalse(stage.exists())

    def test_apply_failure_stays_visible_even_after_escape_and_never_launches(self):
        self.transfer()
        future = Future()
        future.set_exception(reader.SyncError("Files retained; rebuild failed"))
        self.browser.task, self.browser.task_kind = future, "apply"
        self.assertTrue(self.browser.key("\x1b"))
        with mock.patch.object(self.browser, "refresh_catalog") as refresh:
            self.browser.poll()
            refresh.assert_called_once()
        self.assertEqual(self.browser.phase, "error")
        self.assertIsNone(self.browser.launch_request)
        self.browser.key("d")
        self.assertIn("Files retained; rebuild failed", "\n".join(self.browser.detail_lines()))
        self.assertTrue(self.browser.key("\x1b"))
        self.assertEqual(self.browser.phase, "error")
        self.assertTrue(self.browser.key("\x1b"))
        self.assertEqual(self.browser.phase, "browse")
        self.assertFalse(self.browser.back_requested)

    def test_cancel_is_default_and_left_returns_to_cancel(self):
        for keys in (("\n",), (curses.KEY_RIGHT, curses.KEY_LEFT, "\n")):
            with self.subTest(keys=keys):
                self.transfer()
                stage = self.browser.transfer.prepared.stage
                with mock.patch.object(service, "apply") as apply:
                    self.assertTrue(self.browser.key("q"))
                    self.assertEqual(self.browser.phase, "preview")
                    for key in keys:
                        self.browser.key(key)
                    apply.assert_not_called()
                self.assertEqual(self.browser.phase, "browse")
                self.assertIsNone(self.browser.launch_request)
                self.assertFalse(stage.exists())
                self.assertEqual(list(self.target.iterdir()), [])

    def test_escape_waits_for_confirmed_write_then_returns_to_list(self):
        thread_id = self.transfer()
        original = reader.paths_by_id(self.source)[thread_id].read_bytes()
        apply = service.apply
        with mock.patch.object(
            service,
            "apply",
            side_effect=lambda prepared, progress: apply(prepared, index=False, progress=progress),
        ) as invoked:
            self.browser.key(curses.KEY_RIGHT)
            self.browser.key("\n")
            self.assertTrue(self.browser.key("q"))
            self.assertFalse(self.browser.back_requested)
            self.assertTrue(self.browser.key("\x1b"))
            self.assertTrue(self.browser.back_requested)
            self.assertFalse(self.browser.cancel.is_set())
            self.assertEqual(self.browser.phase, "applying")
            self.browser.draw()
            self.assertIn("Returning to list when finished", self.browser.screen.snapshot())
            self.browser.task.result(timeout=5)
            self.browser.poll()
            invoked.assert_called_once()
        self.assertEqual(self.browser.phase, "browse")
        self.assertFalse(self.browser.back_requested)
        self.assertIsNone(self.browser.launch_request)
        self.assertEqual(reader.paths_by_id(self.target)[thread_id].read_bytes(), original)
        self.assertEqual(reader.paths_by_id(self.source)[thread_id].read_bytes(), original)

    def test_escape_cancels_comparison_and_cleans_up_a_late_result(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                self.transfer()
                transfer, self.browser.transfer = self.browser.transfer, None
                self.addCleanup(transfer.close)
                stage = transfer.prepared.stage
                future = Future()
                self.addCleanup(future.cancel)
                self.browser.task, self.browser.task_kind = future, "prepare"
                self.browser.phase = "preparing"
                self.browser.cancel.clear()
                self.assertTrue(self.browser.key("q"))
                self.assertFalse(self.browser.cancel.is_set())
                self.assertTrue(self.browser.key("\x1b"))
                self.assertTrue(self.browser.cancel.is_set())
                self.assertEqual(self.browser.phase, "preparing")
                if failed:
                    transfer.close()  # The read-only worker cleans up before raising.
                    future.set_exception(reader.SyncError("Comparison cancelled"))
                else:
                    future.set_result(transfer)
                self.browser.poll()
                self.assertFalse(stage.exists())
                self.assertEqual(self.browser.phase, "browse")
                self.assertIsNone(self.browser.launch_request)
                self.assertFalse(self.browser.back_requested)
                self.assertFalse(self.browser.key("\x1b"))
        self.assertEqual(list(self.target.iterdir()), [])

    def test_listing_sorts_conversation_time_not_file_time(self):
        self.session()
        entry = self.entries()[0]
        newer = {
            **entry,
            "id": "00000000-0000-0000-0000-000000000002",
            "activity_ns": entry["activity_ns"] + 1000,
            "modified_ns": 1,
        }
        self.browser.model.ingest([self.result(self.node_a, [entry, newer])])
        self.assertEqual(self.browser.model.rows[0].id, newer["id"])

    def test_cli_flags_and_terminal_width(self):
        args = parser().parse_args(["--global", "--include-internal", "browse"])
        self.assertTrue(args.global_scope and args.include_internal)
        self.assertFalse(args.include_archived)
        self.assertNotIn("\x1b", presentation.clean("hello\x1b[2J\x00"))
        self.assertEqual(presentation.clip("中文abc", 5), "中文a")
        self.assertEqual(presentation.clip("e\u0301x", 1), "e\u0301")
        self.assertEqual(presentation.date(10**100), "unknown time")
        self.assertEqual(presentation.relative_time(None), "Unknown time")
        self.assertEqual(presentation.fit("中文标题", 5), "中文…")

    def test_late_activity_updates_candidate_but_does_not_retarget_a_pinned_source(self):
        thread_id, _ = self.session()
        entry = self.entries()[0]
        newer = {**entry, "activity_ns": entry["activity_ns"] + 1000}
        model = self.browser.model
        model.ingest([self.result(None, [entry])])
        self.assertEqual(model.copy.name, "local")
        model.ingest([self.result(self.node_a, [newer])])
        self.assertEqual((model.group.id, model.copy.name), (thread_id, "server-a"))
        model.cycle_source()
        self.assertEqual(model.copy.name, "local")
        model.ingest(
            [self.result(self.node_b, [{**newer, "activity_ns": newer["activity_ns"] + 1}])]
        )
        self.assertEqual(model.copy.name, "local")

    def test_sorting_is_one_timeline_and_does_not_depend_on_source_choice(self):
        self.session()
        entry = self.entries()[0]
        older = {**entry, "activity_ns": entry["activity_ns"] - 1000}
        second = {
            **entry,
            "id": "00000000-0000-0000-0000-000000000002",
            "activity_ns": entry["activity_ns"] - 1,
        }
        third = {
            **second,
            "id": "00000000-0000-0000-0000-000000000003",
            "activity_ns": entry["activity_ns"] - 2,
        }
        model = self.browser.model
        model.ingest([self.result(None, [older, second]), self.result(self.node_a, [entry, third])])
        expected = [entry["id"], second["id"], third["id"]]
        self.assertEqual([group.id for group in model.rows], expected)
        model.cycle_source()
        self.assertEqual(model.copy.name, "local")
        self.assertEqual([group.id for group in model.rows], expected)
        self.assertEqual(model.activity_ns(model.group), entry["activity_ns"])

    def test_local_resume_after_loading_and_new_session_do_not_need_confirmation_or_transfer(self):
        thread_id, _ = self.session(self.target)
        model = self.browser.model
        model.ingest([self.result(None, reader.scan(self.target)["sessions"])])
        with mock.patch.object(service, "prepare_pull") as prepare:
            self.browser.key("\n")
            self.assertEqual(
                self.browser.launch_request, launcher.LaunchRequest(self.root, thread_id)
            )
            self.browser.launch_request = None
            self.browser.scan.pending = {object(): self.node_a}
            self.browser.key(curses.KEY_UP)
            self.assertIsNone(model.group)
            model.ingest([self.result(self.node_a, reader.scan(self.target)["sessions"])])
            self.assertEqual(model.selected, -1)
            self.browser.key("\n")
            self.assertEqual(self.browser.launch_request, launcher.LaunchRequest(self.root))
            prepare.assert_not_called()

    def test_loading_confirmation_cancels_without_launching_or_preparing(self):
        _, path = self.session()
        original = path.read_bytes()
        entries = self.entries()
        self.browser.scan.pending = {object(): self.node_b}
        for node in (None, self.node_a):
            self.browser.model.results.clear()
            self.browser.model.ingest([self.result(node, entries)])
            self.browser.key(curses.KEY_UP)
            self.browser.key(curses.KEY_DOWN)
            self.assertEqual(self.browser.phase, "browse")
            for keys in (
                ("\n",),
                (curses.KEY_RIGHT, curses.KEY_LEFT, "\n"),
                ("\t", "\t", "\n"),
                (curses.KEY_RIGHT, "\x1b"),
            ):
                with (
                    self.subTest(node=node, keys=keys),
                    mock.patch.object(service, "prepare_pull") as prepare,
                    mock.patch.object(launcher, "resume_session") as resume,
                ):
                    self.browser.key("\n")
                    self.assertEqual(self.browser.phase, "confirm_loading")
                    self.assertTrue(self.browser.key("q"))
                    self.assertEqual(self.browser.phase, "confirm_loading")
                    self.assertEqual(self.browser.confirm_choice, 0)
                    self.assertIsNone(self.browser.task)
                    self.assertIsNone(self.browser.launch_request)
                    for key in keys:
                        self.browser.key(key)
                    self.assertEqual(self.browser.phase, "browse")
                    self.assertIsNone(self.browser.pending_open)
                    prepare.assert_not_called()
                    resume.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_loading_confirmation_keeps_local_choice_after_inventory_finishes(self):
        thread_id, path = self.session(self.target)
        original = path.read_bytes()
        entry = reader.scan(self.target)["sessions"][0]
        self.browser.model.ingest([self.result(None, [entry])])
        self.browser.scan.pending = {object(): self.node_a}
        self.browser.key("\n")
        self.browser.key(curses.KEY_RIGHT)
        newer = {**entry, "activity_ns": entry["activity_ns"] + 1000, "title": "Newer remote title"}
        other = {
            **newer,
            "id": "00000000-0000-0000-0000-000000000002",
            "activity_ns": newer["activity_ns"] + 1000,
        }
        with mock.patch.object(service, "prepare_pull") as prepare:
            self.browser.scan.pending = {}
            with mock.patch.object(
                self.browser.scan, "poll", return_value=[self.result(self.node_a, [newer, other])]
            ):
                self.browser.poll()
            self.assertEqual(self.browser.model.copy.name, "server-a")
            self.assertEqual(self.browser.phase, "confirm_loading")
            self.assertEqual(self.browser.confirm_choice, 1)
            self.assertIsNone(self.browser.launch_request)
            self.assertIn("Source: workstation", self.browser.panel()[1])
            self.assertNotIn("Newer remote title", self.browser.panel()[1])
            self.browser.key("\n")
            self.assertEqual(
                self.browser.launch_request, launcher.LaunchRequest(self.root, thread_id)
            )
            self.assertIsNone(self.browser.pending_open)
            prepare.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.target / STATE_DIRECTORY).exists())

    def test_loading_confirmation_keeps_remote_source_and_still_requires_sync_consent(self):
        thread_id, path = self.session()
        original = path.read_bytes()
        self.export([thread_id])
        entry = self.entries()[0]
        self.browser.model.ingest([self.result(self.node_a, [entry])])
        self.browser.scan.pending = {object(): self.node_b}
        manager = service.prepare_file(self.bundle, self.settings.target)
        with (
            mock.patch.object(service, "prepare_pull", return_value=manager) as prepare,
            mock.patch.object(service, "apply") as apply,
        ):
            self.browser.key("\n")
            self.assertEqual(self.browser.phase, "confirm_loading")
            self.assertIsNone(self.browser.task)
            prepare.assert_not_called()
            self.browser.model.ingest(
                [self.result(self.node_b, [{**entry, "activity_ns": entry["activity_ns"] + 1000}])]
            )
            self.assertEqual(self.browser.model.copy.name, "server-b")
            self.browser.key("\t")
            self.browser.key("\n")
            self.browser.task.result(timeout=5)
            self.browser.poll()
            prepare.assert_called_once_with(
                self.node_a,
                self.settings.target,
                (thread_id,),
                timeout=self.settings.transfer_timeout,
                cancel=self.browser.cancel,
                progress=self.browser.events.put,
            )
            self.assertEqual(self.browser.phase, "preview")
            self.assertEqual(self.browser.confirm_choice, 0)
            self.assertEqual(self.browser.transfer.source, "server-a")
            self.assertIsNone(self.browser.launch_request)
            stage = self.browser.transfer.prepared.stage
            self.browser.key("\n")  # Accepting the loading warning does not authorize a sync.
            self.assertEqual(self.browser.phase, "browse")
            self.assertFalse(stage.exists())
            apply.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_equal_or_older_remote_opens_local_without_apply_or_confirmation(self):
        thread_id, source = self.session()
        source_before = source.read_bytes()
        for messages in (("first",), ("first", "local continuation")):
            with self.subTest(messages=messages):
                _, local = self.session(self.target, thread_id, messages=messages)
                before = local.read_bytes()
                self.export([thread_id])
                manager = service.prepare_file(self.bundle, self.settings.target)
                prepared = manager.__enter__()
                stage = prepared.stage
                self.assertFalse(prepared.needs_sync)
                future = Future()
                future.set_result(ui.Transfer(manager, prepared, thread_id, "server-a", "test"))
                self.browser.task, self.browser.task_kind = future, "prepare"
                with mock.patch.object(service, "apply") as apply:
                    self.browser.poll()
                    apply.assert_not_called()
                self.assertEqual(self.browser.launch_request.session_id, thread_id)
                self.assertNotEqual(self.browser.phase, "preview")
                self.browser.back()
                self.browser.launch_request = None
                self.assertFalse(stage.exists())
                self.assertEqual(local.read_bytes(), before)
                self.assertFalse((self.target / STATE_DIRECTORY).exists())
        self.assertEqual(source.read_bytes(), source_before)

    def test_remembering_a_mapped_directory_requires_confirmation_and_supports_local_open(self):
        thread_id, source = self.session()
        _, local = self.session(self.target, thread_id)
        original = source.read_bytes()
        project = self.root / "mapped-project"
        project.mkdir()
        node = replace(self.node_a, mappings=((str(self.root), str(project)),))
        target = self.settings.target.for_node(node)
        self.export([thread_id])
        apply = service.apply
        for confirm in (False, True):
            manager = service.prepare_file(self.bundle, target)
            prepared = manager.__enter__()
            self.assertIs(prepared.changes[0].action, Action.SAME)
            future = Future()
            future.set_result(ui.Transfer(manager, prepared, thread_id, node.name, "test"))
            self.browser.task, self.browser.task_kind = future, "prepare"
            self.browser.poll()
            self.assertEqual(self.browser.phase, "preview")
            self.assertIsNone(self.browser.launch_request)
            if not confirm:
                self.browser.key("\n")
                self.assertEqual(self.browser.phase, "browse")
                self.assertFalse((self.target / reader.LOCATIONS_FILE).exists())
            else:
                with mock.patch.object(
                    service, "apply", side_effect=lambda p, **kw: apply(p, index=False)
                ):
                    self.browser.key("\t")
                    self.browser.key("\n")
                    self.browser.task.result(timeout=5)
                    self.browser.poll()
                self.assertEqual(
                    self.browser.launch_request, launcher.LaunchRequest(project, thread_id)
                )
        self.browser.back()
        self.browser.launch_request = None
        self.browser.model.options.directory = project
        self.browser.model.ingest([self.result(None, reader.scan(self.target)["sessions"])])
        self.browser.key("\n")
        self.assertEqual(self.browser.launch_request, launcher.LaunchRequest(project, thread_id))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(local.read_bytes(), original)

    def test_matching_child_still_requires_confirmation_for_ancestor_update_or_conflict(self):
        parent, source = self.session()
        base = {
            "thread_id": parent,
            "end_byte_offset": source.stat().st_size,
            "end_ordinal_exclusive": 2,
        }
        child, _ = self.session(base=base, start=2)
        self.session(self.target, parent)
        self.session(self.target, child, base=base, start=2)
        self.session(thread_id=parent, messages=("first", "remote continuation"))
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                # Both variants keep the child's referenced prefix intact.
                if conflict:
                    self.session(self.target, parent, messages=("first", "local continuation"))
                self.export([child])
                manager = service.prepare_file(self.bundle, self.settings.target)
                prepared = manager.__enter__()
                self.assertTrue(prepared.needs_sync)
                self.assertEqual(prepared.has_conflicts, conflict)
                self.assertIs(
                    next(c.action for c in prepared.changes if c.id == child), Action.SAME
                )
                future = Future()
                future.set_result(ui.Transfer(manager, prepared, child, "server-a", "test"))
                self.browser.task, self.browser.task_kind = future, "prepare"
                self.browser.poll()
                self.assertEqual(self.browser.phase, "preview")
                self.assertIsNone(self.browser.launch_request)
                self.browser.key("\n")  # Defaults to cancel, including conflicts.
        self.assertFalse((self.target / STATE_DIRECTORY).exists())

    def render_samples(self):
        directory = Path.home() / "project"
        now = reader.timestamp_ns("2026-09-09T12:00:00Z")
        self.browser.model.options.directory = directory
        entries = []
        for suffix, title, age in (
            (1, "Refactor the storage layer", 7200),
            (2, "Investigate a failing integration test", 86400),
        ):
            entries.append(
                {
                    "id": f"00000000-0000-0000-0000-{suffix:012d}",
                    "cwd": str(directory),
                    "source": "cli",
                    "title": "",
                    "summary": title,
                    "activity_ns": now - age * 10**9,
                    "modified_ns": now,
                    "archived": False,
                    "changing": False,
                    "size": 100,
                    "parent_id": None,
                }
            )
        self.browser.model.ingest(
            [
                self.result(self.node_a, entries),
                self.result(None, [entries[1]]),
                fleet.NodeResult("server-b", self.node_b, error="fixture offline"),
            ]
        )
        self.browser.model.move(1)
        samples = {}
        with mock.patch.object(presentation.time, "time_ns", return_value=now):
            for name, height, width in (
                ("cwd_80", 24, 80),
                ("narrow_48", 18, 48),
                ("wide_120", 30, 120),
                ("wide_160", 30, 160),
            ):
                self.browser.screen = FakeScreen(height, width)
                self.browser.draw()
                samples[name] = self.browser.screen.snapshot()
            # A refresh retains earlier rows while a source is still reading.
            self.browser.scan.pending = {object(): self.node_a}
            for name, height, width in (("updating_80", 24, 80), ("updating_48", 18, 48)):
                self.browser.screen = FakeScreen(height, width)
                self.browser.draw()
                samples[name] = self.browser.screen.snapshot()
            self.browser.key("\n")
            for name, height, width in (("open_loading_80", 24, 80), ("open_loading_48", 18, 48)):
                self.browser.screen = FakeScreen(height, width)
                self.browser.draw()
                samples[name] = self.browser.screen.snapshot()
            self.browser.key("\x1b")
            self.browser.scan.pending = {}

            previous_results = self.browser.model.results
            self.browser.model.results = {}
            self.browser.model.rebuild()
            self.browser.scan.pending = {object(): None}
            self.browser.draw()
            samples["loading_empty_48"] = self.browser.screen.snapshot()
            self.browser.scan.pending = {}
            self.browser.model.results = previous_results
            self.browser.model.rebuild()
            self.browser.model.move(1)

            self.browser.screen = FakeScreen()
            self.browser.key("g")
            self.browser.draw()
            samples["global_80"] = self.browser.screen.snapshot()
            self.browser.key("/")
            for char in "no matching session":
                self.browser.key(char)
            self.browser.draw()
            samples["empty_search_80"] = self.browser.screen.snapshot()

            self.browser.searching = False
            self.browser.model.options.global_scope = False
            self.browser.model.search("")
            more = [
                {
                    **entries[0],
                    "id": f"00000000-0000-0000-0000-{index:012d}",
                    "summary": f"Example session {index:02d}",
                    "activity_ns": now - index * 86400 * 10**9,
                }
                for index in range(3, 65)
            ]
            self.browser.model.ingest([self.result(self.node_a, entries + more)])
            self.browser.screen = FakeScreen(48, 120)
            self.browser.draw()
            self.browser.key(curses.KEY_NPAGE)
            self.browser.draw()
            samples["tall_page_120"] = self.browser.screen.snapshot()
            self.browser.screen = FakeScreen(18, 48)
            self.browser.draw()
            samples["resized_48"] = self.browser.screen.snapshot()
            self.browser.screen = FakeScreen()
        self.browser.searching = False
        self.browser.model.options.global_scope = False
        self.browser.model.search("")
        self.transfer()
        self.browser.transfer.title = "Investigate a failing integration test"
        self.browser.draw()
        samples["preview_80"] = self.browser.screen.snapshot()
        self.browser.screen = FakeScreen(18, 48)
        self.browser.transfer.title = (
            "Investigate why the integration command failed; the full title is available in Details. "
            * 3
        )
        self.browser.draw()
        samples["preview_48"] = self.browser.screen.snapshot()
        self.browser.screen = FakeScreen()
        self.browser.transfer.title = "Investigate a failing integration test"
        prepared = self.browser.transfer.prepared
        self.browser.transfer.prepared = replace(
            prepared,
            changes=tuple(replace(change, action=Action.CONFLICT) for change in prepared.changes),
        )
        self.browser.draw()
        samples["conflict_80"] = self.browser.screen.snapshot()
        return samples

    def test_reviewed_screen_snapshots(self):
        for name, rendered in self.render_samples().items():
            with self.subTest(screen=name):
                expected = (HERE / "tests/snapshots" / (name + ".txt")).read_text()
                self.assertEqual(rendered, expected)
