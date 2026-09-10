"""Per-source paths survive offline resume and transfers without editing history."""

import io
import json
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from codex_everywhere import cli, codex, fleet, launcher, presentation, reader, service, storage
from codex_everywhere.browser import BrowseOptions, BrowserModel
from codex_everywhere.config import Node, Target
from codex_everywhere.plan import Action
from tests.fixtures import SessionFixture


class LocationTests(SessionFixture):
    def setUp(self):
        super().setUp()
        self.project = self.root / "local-project"
        self.project.mkdir()
        self.node = Node(
            "server-a", "server-a", str(self.source), ((str(self.root), str(self.project)),)
        )
        self.destination = Target(self.target, codex=sys.executable)

    @staticmethod
    def receive(node, operation, output, *, sessions, **kwargs):
        assert operation == "export"
        reader.export_bundle(Path(node.home), output, sessions)

    def pull(self, node=None, target=None):
        return service.prepare_pull(node or self.node, target or self.destination, ())

    def test_round_trip_uses_each_sources_current_path_and_preserves_jsonl(self):
        thread_id, source = self.session()
        original = source.read_bytes()
        third = self.root / "third-home"
        third.mkdir()
        third_project = self.root / "third-project"
        third_project.mkdir()
        with mock.patch.object(service.transport, "receive", side_effect=self.receive):
            with self.pull() as prepared:
                self.assertEqual(prepared.directories[thread_id], str(self.project))
                self.assertIn(
                    f"Open in: {self.project}", presentation.comparison_lines(prepared, thread_id)
                )
                self.assertFalse((self.target / reader.LOCATIONS_FILE).exists())
                with (
                    mock.patch.object(codex, "check_version"),
                    mock.patch.object(codex, "AppServer") as server,
                ):
                    server.return_value.call.return_value = {"data": [], "nextCursor": None}
                    service.apply(prepared)
                resumes = [
                    c.args[1]
                    for c in server.return_value.call.call_args_list
                    if c.args[0] == "thread/resume"
                ]
                self.assertEqual([r["cwd"] for r in resumes], [str(self.project)])
            # A new browser/launcher needs neither node configuration nor the source.
            entries = reader.scan(self.target)["sessions"]
            model = BrowserModel(BrowseOptions(directory=self.project), self.node.mappings)
            model.ingest([fleet.NodeResult("local", None, tuple(entries))])
            self.assertEqual(model.group.id, thread_id)
            request = launcher.resume_session(
                self.destination, thread_id, entries[0]["original_cwd"]
            )
            self.assertEqual(request.directory, self.project)
            local_path = reader.paths_by_id(self.target)[thread_id]
            self.assertEqual(local_path.read_bytes(), original)

            node_b = Node(
                "server-b", "server-b", str(self.target), ((str(self.project), str(third_project)),)
            )
            with self.pull(node_b, Target(third)) as prepared:
                self.assertEqual(prepared.directories[thread_id], str(third_project))
                service.apply(prepared, index=False)
            third_path = reader.paths_by_id(third)[thread_id]
            self.assertEqual(third_path.read_bytes(), original)
            with third_path.open("ab") as output:
                output.write(
                    b'{"ordinal":2,"type":"event_msg","payload":{"type":"user_message","message":"continued"}}\n'
                )
            node_c = Node(
                "server-c", "server-c", str(third), ((str(third_project), str(self.root)),)
            )
            with self.pull(node_c, Target(self.source)) as prepared:
                self.assertEqual(prepared.changes[0].action, Action.UPDATE)
                self.assertEqual(prepared.directories[thread_id], str(self.root))
                service.apply(prepared, index=False)
            self.assertEqual(source.read_bytes(), third_path.read_bytes())
            self.assertEqual(local_path.read_bytes(), original)
            self.assertEqual(reader.scan(self.source)["sessions"][0]["cwd"], str(self.root))

    def test_retained_history_still_confirms_a_new_location_then_becomes_idempotent(self):
        thread_id, _ = self.session()
        _, local = self.session(self.target, thread_id, messages=("first", "kept"))
        original = local.read_bytes()
        with mock.patch.object(service.transport, "receive", side_effect=self.receive):
            with self.pull() as prepared:
                self.assertEqual(prepared.changes[0].action, Action.LOCAL_NEWER)
                self.assertTrue(prepared.needs_sync)
                self.assertEqual(prepared.directory_updates, 1)
                service.apply(prepared, index=False)
            with self.pull() as prepared:
                self.assertFalse(prepared.needs_sync)
                self.assertEqual(prepared.directories[thread_id], str(self.project))
        self.assertEqual(local.read_bytes(), original)

    def test_source_home_override_keeps_node_mapping_and_cli_preview_is_read_only(self):
        thread_id, source = self.session()
        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "home": str(self.target),
                    "nodes": [
                        {
                            "host": "server-a",
                            "remote_home": "/unused",
                            "mappings": [[str(self.root), str(self.project)]],
                        }
                    ],
                }
            )
        )
        original = source.read_bytes()
        args = cli.parser().parse_args(
            [
                "--config",
                str(config),
                "pull",
                "server-a",
                "--source-home",
                str(self.source),
                "--session",
                thread_id,
                "--dry-run",
            ]
        )
        with (
            mock.patch.object(service.transport, "receive", side_effect=self.receive),
            mock.patch.object(cli, "say") as say,
        ):
            self.assertEqual(cli.execute(args), 0)
        self.assertIn(
            f"    Working directory: {self.project}", [c.args[0] for c in say.call_args_list]
        )
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(source.read_bytes(), original)

    def test_existing_local_placement_wins_and_explicit_override_is_backed_up(self):
        thread_id, _ = self.session()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        with mock.patch.object(service.transport, "receive", side_effect=self.receive):
            with self.pull() as prepared:
                service.apply(prepared, index=False)
            locations = self.target / reader.LOCATIONS_FILE
            original = locations.read_bytes()
            different = replace(self.node, mappings=((str(self.root), str(elsewhere)),))
            with self.pull(different) as prepared:
                self.assertEqual(prepared.directories[thread_id], str(self.project))
                self.assertFalse(prepared.needs_sync)
            with self.pull(different, replace(self.destination, cwd=str(elsewhere))) as prepared:
                self.assertTrue(prepared.needs_sync)
                report = service.apply(prepared, index=False)
            self.assertEqual((report / "original" / reader.LOCATIONS_FILE).read_bytes(), original)
            self.assertEqual(reader.scan(self.target)["sessions"][0]["cwd"], str(elsewhere))

    def test_location_changed_after_preview_aborts_before_installing_history(self):
        thread_id, source = self.session()
        with mock.patch.object(service.transport, "receive", side_effect=self.receive):
            with self.pull() as prepared:
                service.apply(prepared, index=False)
            local = reader.paths_by_id(self.target)[thread_id]
            original = local.read_bytes()
            with source.open("ab") as output:
                output.write(b'{"ordinal":2,"type":"event_msg","payload":{}}\n')
            with self.pull() as prepared:
                changed = {thread_id: {"original_cwd": str(self.root), "cwd": str(self.target)}}
                storage.write_json(self.target / reader.LOCATIONS_FILE, changed)
                with self.assertRaisesRegex(reader.SyncError, "Target changed since preview"):
                    service.apply(prepared, index=False)
            self.assertEqual(local.read_bytes(), original)
            self.assertEqual(reader.read_locations(self.target), changed)

    def test_export_includes_only_selected_location_hints_and_rejects_tampering(self):
        selected, _ = self.session()
        other, _ = self.session()
        path = self.source / reader.LOCATIONS_FILE
        path.parent.mkdir()
        path.write_text(
            json.dumps(
                {
                    i: {"original_cwd": str(self.root), "cwd": str(self.project)}
                    for i in (selected, other)
                }
            )
        )
        self.export([selected])
        with zipfile.ZipFile(self.bundle) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(members["manifest.json"])
        self.assertEqual(set(manifest["locations"]), {selected})
        for hints in (
            {other: {"original_cwd": str(self.root), "cwd": str(self.project)}},
            {selected: {"original_cwd": "/wrong-original", "cwd": str(self.project)}},
            {selected: {"original_cwd": str(self.root), "cwd": "relative/path"}},
        ):
            with self.subTest(hints=hints):
                manifest["locations"] = hints
                with zipfile.ZipFile(self.bundle, "w") as archive:
                    for name, data in members.items():
                        archive.writestr(
                            name, json.dumps(manifest) if name == "manifest.json" else data
                        )
                with self.assertRaises(reader.SyncError):
                    self.importing()
                self.assertEqual(list(self.target.iterdir()), [])
        # Older bundles have no optional directory metadata.
        manifest.pop("locations")
        with zipfile.ZipFile(self.bundle, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, json.dumps(manifest) if name == "manifest.json" else data)
        with service.prepare_file(self.bundle, self.destination) as prepared:
            self.assertEqual(prepared.directories[selected], str(self.root))

    def test_source_rejects_symlink_oversized_and_invalid_location_files(self):
        self.session()
        path = self.source / reader.LOCATIONS_FILE
        path.parent.mkdir()
        secret = self.root / "unrelated.json"
        secret.write_text("{}")
        path.symlink_to(secret)
        with self.assertRaisesRegex(reader.SyncError, "symlink"):
            reader.scan(self.source)
        path.unlink()
        for data, limit in ((b"{" + b" " * 100, 64), (b"{", 1024), (b"[]", 1024)):
            path.write_bytes(data)
            with (
                mock.patch.object(reader, "MAX_LOCATIONS", limit),
                self.assertRaises(reader.SyncError),
            ):
                reader.export_bundle(self.source, io.BytesIO())
        self.assertEqual(secret.read_text(), "{}")

    def test_stale_hint_is_not_used_and_concurrent_source_location_change_aborts_export(self):
        thread_id, _ = self.session()
        path = self.source / reader.LOCATIONS_FILE
        path.parent.mkdir()
        path.write_text(
            json.dumps(
                {thread_id: {"original_cwd": "/different-original", "cwd": str(self.project)}}
            )
        )
        self.assertEqual(reader.scan(self.source)["sessions"][0]["cwd"], str(self.root))
        with mock.patch.object(reader, "read_locations", side_effect=[{}, {"changed": {}}]):
            with self.assertRaisesRegex(reader.SyncError, "locations changed"):
                reader.export_bundle(self.source, io.BytesIO())
