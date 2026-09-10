"""Metadata grouping, untrusted transport, and input behavior without a terminal."""

import io
import json
import os
import threading
import time
from unittest import mock

from codex_everywhere import fleet, reader, transport
from codex_everywhere.config import Node, Settings, Target, load
from tests.fixtures import SessionFixture


class FleetTests(SessionFixture):
    def test_grouping_uses_uuid_and_searches_every_node_path(self):
        thread_id, _ = self.session()
        entries = tuple(reader.scan(self.source)["sessions"])
        results = {
            "local": fleet.NodeResult("local", None, entries),
            "a": fleet.NodeResult("a", Node("a", "host", str(self.source)), entries),
        }
        grouped = fleet.groups(results)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].id, thread_id)
        self.assertEqual(len(grouped[0].copies), 2)
        self.assertEqual(len(fleet.groups(results, str(self.root))), 1)
        self.assertEqual(fleet.groups(results, "absent phrase"), [])

    def test_configured_fleet_excludes_current_host(self):
        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "remote_home": str(self.source),
                    "nodes": [f"node-{i}" for i in range(8)],
                }
            )
        )
        with mock.patch("socket.gethostname", return_value="node-5.example"):
            settings = load(config, home=str(self.target))
        self.assertEqual(
            [node.name for node in settings.nodes], [f"node-{i}" for i in range(8) if i != 5]
        )

    def test_node_failure_does_not_drop_successful_results(self):
        self.session(self.target)
        settings = Settings((Node("bad", "bad", str(self.source)),), Target(self.target))
        with mock.patch.object(transport, "inventory", side_effect=reader.SyncError("offline")):
            job = fleet.ScanJob(settings)
            result = {}
            try:
                deadline = time.monotonic() + 2
                while job.pending and time.monotonic() < deadline:
                    result.update({r.name: r for r in job.poll()})
                    time.sleep(0.01)
            finally:
                job.close()
        self.assertEqual(len(result["local"].sessions), 1)
        self.assertEqual(result["bad"].error, "offline")

    def ssh_stub(self, program: str):
        directory = self.root / "bin"
        directory.mkdir()
        script = directory / "ssh"
        script.write_text("#!/usr/bin/env python3\n" + program)
        script.chmod(0o700)
        return mock.patch.dict(
            os.environ, {"PATH": str(directory) + os.pathsep + os.environ["PATH"]}
        )

    def test_transport_timeout_kills_hung_process(self):
        with self.ssh_stub("import time\ntime.sleep(60)\n"):
            start = time.monotonic()
            with self.assertRaisesRegex(reader.SyncError, "timed out"):
                transport.receive(
                    Node("a", "a", str(self.source)), "scan", io.BytesIO(), timeout=0.1
                )
            self.assertLess(time.monotonic() - start, 2)

    def test_transport_cancellation_does_not_wait_for_timeout(self):
        cancel = threading.Event()
        cancel.set()
        with self.ssh_stub("import time\ntime.sleep(60)\n"):
            with self.assertRaisesRegex(reader.SyncError, "cancelled"):
                transport.receive(
                    Node("a", "a", str(self.source)), "scan", io.BytesIO(), cancel=cancel
                )

    def test_transport_bounds_catalog_output(self):
        with (
            self.ssh_stub("import sys\nsys.stdout.write('x' * 4096)\n"),
            mock.patch.object(transport, "CATALOG_LIMIT", 1024),
        ):
            with self.assertRaisesRegex(reader.SyncError, "transfer limit"):
                transport.receive(Node("a", "a", str(self.source)), "scan", io.BytesIO())

    def test_remote_protocol_has_no_import_operation(self):
        with self.assertRaisesRegex(reader.SyncError, "only permits"):
            transport.command(Node("a", "a", str(self.source)), "import")

    def test_remote_snapshot_errors_identify_the_source_without_process_probes(self):
        _, path = self.session()
        path.write_bytes(path.read_bytes() + b'{"unfinished":')
        program = """import shlex, subprocess, sys
from unittest import mock
payload = sys.stdin.read()
sys.argv = ['worker', *shlex.split(sys.argv[-1])[3:]]
with mock.patch.object(subprocess, 'run', side_effect=AssertionError('process probe')):
    exec(compile(payload, '<remote-worker>', 'exec'), {'__name__': '__main__'})
"""
        output = io.BytesIO()
        with self.ssh_stub(program):
            with self.assertRaises(reader.SyncError) as caught:
                transport.receive(Node("server-a", "ssh-host", str(self.source)), "export", output)
        message = str(caught.exception)
        self.assertIn("Remote server-a:", message)
        self.assertIn("Incomplete final record", message)
        self.assertNotIn("Local machine", message)
        self.assertEqual(output.getvalue(), b"")

    def test_invalid_inventory_is_rejected(self):
        def invalid(node, operation, output, **kwargs):
            output.write(
                json.dumps({"format": 1, "sessions": [{"id": "invalid"}], "issues": []}).encode()
            )

        with mock.patch.object(transport, "receive", side_effect=invalid):
            with self.assertRaises(reader.SyncError):
                transport.inventory(Node("a", "a", str(self.source)))
