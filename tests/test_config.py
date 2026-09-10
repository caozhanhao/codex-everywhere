"""Configuration chooses explicit sources and keeps local/remote paths separate."""

import json
import os
from pathlib import Path
from unittest import mock

from codex_everywhere import config
from codex_everywhere.config import default_path, load
from codex_everywhere.reader import SyncError
from tests.fixtures import SessionFixture


class ConfigTests(SessionFixture):
    def setUp(self):
        super().setUp()
        self.config = self.root / "explicit.json"
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.checkout_config = self.checkout / "config.json"
        self.user_config = self.root / "xdg/codex-everywhere/config.json"
        self.user_config.parent.mkdir(parents=True)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(config, "_SOURCE_ROOT", self.checkout).start()
        mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "xdg")}).start()

    def test_checkout_config_is_independent_of_browsed_directory_and_overrides_user_config(self):
        (self.checkout / "config.example.json").write_text("{}")
        self.checkout_config.write_text(json.dumps({"home": str(self.source)}))
        self.user_config.write_text(json.dumps({"home": str(self.target)}))
        before = self.checkout_config.read_bytes()
        with mock.patch.object(Path, "cwd", return_value=self.root / "unrelated-project"):
            self.assertEqual(default_path(), self.checkout_config)
            self.assertEqual(load().target.home, self.source)
        self.write({"home": str(self.target)})
        self.assertEqual(load(self.config).target.home, self.target)
        self.assertEqual(self.checkout_config.read_bytes(), before)

    def test_installed_app_uses_user_config_and_ignores_unrelated_config_files(self):
        # A config.json next to an installed package is not a checkout config.
        self.checkout_config.write_text("not a project configuration")
        self.user_config.write_text(json.dumps({"home": str(self.target)}))
        self.assertEqual(default_path(), self.user_config)
        self.assertEqual(load().target.home, self.target)
        # An unconfigured checkout also retains the user's existing settings.
        self.checkout_config.unlink()
        (self.checkout / "config.example.json").write_text("{}")
        self.assertEqual(default_path(), self.user_config)
        self.assertEqual(load().target.home, self.target)

    def test_invalid_checkout_config_does_not_silently_fall_back(self):
        (self.checkout / "config.example.json").write_text("{}")
        self.checkout_config.write_text("not json")
        self.user_config.write_text("{}")
        with self.assertRaises(json.JSONDecodeError):
            load()

    def write(self, data):
        self.config.write_text(json.dumps(data))

    def test_no_configuration_means_local_only_and_does_not_create_a_config(self):
        with mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.root), "CODEX_HOME": str(self.target)}
        ):
            settings = load()
            self.assertEqual(settings.nodes, ())
            self.assertEqual(settings.target.home, self.target)
            self.assertFalse(default_path().exists())

    def test_remote_default_and_node_override_do_not_change_local_destination(self):
        self.write(
            {
                "home": str(self.target),
                "remote_home": "/remote/default/codex",
                "nodes": [
                    {"name": "gpu", "host": "user@gpu-host"},
                    {"name": "laptop", "host": "laptop-host", "remote_home": "~/.codex"},
                ],
            }
        )
        settings = load(self.config)
        self.assertEqual(settings.target.home, self.target)
        self.assertEqual(
            [(n.name, n.host, n.home) for n in settings.nodes],
            [
                ("gpu", "user@gpu-host", "/remote/default/codex"),
                ("laptop", "laptop-host", "~/.codex"),
            ],
        )

    def test_node_override_does_not_require_remote_default(self):
        self.write({"nodes": [{"name": "gpu", "host": "gpu-host", "remote_home": "/source/codex"}]})
        self.assertEqual(load(self.config).nodes[0].home, "/source/codex")

    def test_mappings_belong_to_each_source_and_cli_overrides_matching_prefixes(self):
        self.write(
            {
                "remote_home": "~/.codex",
                "nodes": [
                    {
                        "host": "server-a",
                        "mappings": [["/work", "/local/a"], ["/data", "/local/data"]],
                    },
                    {"host": "server-b", "mappings": [["/work", "/local/b"]]},
                    "server-c",
                ],
            }
        )
        before = self.config.read_bytes()
        settings = load(self.config)
        self.assertEqual(
            [n.mappings for n in settings.nodes],
            [
                (("/work", "/local/a"), ("/data", "/local/data")),
                (("/work", "/local/b"),),
                (),
            ],
        )
        self.assertEqual(settings.target.mappings, ())
        overrides = (("/work", "/cli"),)
        settings = load(self.config, mappings=overrides)
        self.assertEqual(
            [settings.target.for_node(n).mappings for n in settings.nodes],
            [
                (("/work", "/cli"), ("/data", "/local/data")),
                overrides,
                overrides,
            ],
        )
        self.assertEqual(self.config.read_bytes(), before)

    def test_top_level_and_malformed_node_mappings_are_rejected(self):
        self.write({"mappings": []})
        with self.assertRaisesRegex(SyncError, "nodes\\[\\]\\.mappings"):
            load(self.config)
        for value in (None, {}, "path", ["path"], [["a"]], [["", "b"]], [[1, "b"]], [["a", "b\0"]]):
            with self.subTest(value=value):
                self.write(
                    {"remote_home": "~/.codex", "nodes": [{"host": "server-a", "mappings": value}]}
                )
                with self.assertRaisesRegex(SyncError, "mappings"):
                    load(self.config)

    def test_short_and_mixed_nodes_match_explicit_configuration(self):
        data = {
            "home": str(self.target),
            "remote_home": "/remote/default/codex",
            "nodes": [
                "server-a",
                {"host": "user@server-b", "remote_home": "~/.codex"},
                {"name": "gpu", "host": "server-c"},
            ],
        }
        self.write(data)
        before = self.config.read_bytes()
        shorthand = load(self.config)
        self.assertEqual(self.config.read_bytes(), before)
        data["nodes"] = [
            {"name": "server-a", "host": "server-a", "remote_home": "/remote/default/codex"},
            {"name": "user@server-b", "host": "user@server-b", "remote_home": "~/.codex"},
            {"name": "gpu", "host": "server-c", "remote_home": "/remote/default/codex"},
        ]
        self.write(data)
        self.assertEqual(shorthand, load(self.config))

    def test_duplicate_and_reserved_names_in_mixed_forms_are_rejected(self):
        for nodes in (
            ["server-a", "server-a"],
            ["server-a", {"host": "different-host", "name": "server-a"}],
            ["local"],
        ):
            with self.subTest(nodes=nodes):
                self.write({"remote_home": "/remote/codex", "nodes": nodes})
                with mock.patch("socket.gethostname", return_value="workstation"):
                    with self.assertRaisesRegex(SyncError, "unique.*local"):
                        load(self.config)

    def test_unknown_config_keys_and_incomplete_node_paths_are_rejected(self):
        for data in (
            {"remote_hom": "/typo"},
            {"remote_home": "relative/path"},
            {"nodes": {}},
            {"nodes": "server-a"},
            {"nodes": ["server-a"]},
            {"nodes": [{"name": "gpu", "host": "gpu-host"}]},
            {"remote_home": "/valid", "nodes": [{"name": "gpu", "hots": "typo"}]},
            {"remote_home": "/valid", "nodes": [{"host": "gpu-host", "remote_home": ""}]},
            {"remote_home": "/valid", "nodes": [{"host": "gpu-host", "remote_home": None}]},
            {"remote_home": "/valid", "nodes": [{"host": "gpu-host", "home": "/old-key"}]},
            {"remote_home": "/valid", "nodes": [None]},
            {"remote_home": "/valid", "nodes": [""]},
            {"remote_home": "/valid", "nodes": ["-invalid"]},
            {"remote_home": "/valid", "nodes": ["host;touch invalid"]},
            {"remote_home": "/valid", "nodes": [{"host": 42}]},
            {"remote_home": "/valid", "nodes": [{"host": "server-a", "name": None}]},
            {"remote_home": "/valid", "nodes": [{"host": "server-a", "hom": "/typo"}]},
        ):
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(SyncError):
                    load(self.config)

    def test_local_alias_is_excluded_and_explicit_missing_file_is_not_ignored(self):
        self.write(
            {
                "remote_home": "/source/codex",
                "nodes": [
                    {"name": "here", "host": "user@workstation.example"},
                    {"name": "elsewhere", "host": "another-host"},
                ],
            }
        )
        with mock.patch("socket.gethostname", return_value="workstation.example"):
            self.assertEqual([n.name for n in load(self.config).nodes], ["elsewhere"])
        self.config.unlink()
        with self.assertRaises(FileNotFoundError):
            load(self.config)

    def test_local_path_precedence_is_cli_then_config_then_environment(self):
        self.write({"home": str(self.source), "sqlite_home": str(self.source)})
        before = self.config.read_bytes()
        with mock.patch.dict(
            os.environ, {"CODEX_HOME": str(self.target), "CODEX_SQLITE_HOME": str(self.target)}
        ):
            settings = load(self.config)
            self.assertEqual(settings.target.home, self.source)
            self.assertEqual(settings.target.sqlite_home, self.source)
            settings = load(self.config, home=str(self.root), sqlite_home=str(self.root))
            self.assertEqual(settings.target.home, self.root)
            self.assertEqual(settings.target.sqlite_home, self.root)
        self.assertEqual(self.config.read_bytes(), before)
