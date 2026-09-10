"""The native adapter cannot start turns or accidentally approve server requests."""

import json
import stat
import subprocess
from unittest import mock

from codex_everywhere.codex import VALIDATED_VERSION, AppServer, check_version
from codex_everywhere.reader import SyncError
from tests.fixtures import SessionFixture


class AdapterTests(SessionFixture):
    def test_version_is_a_validation_baseline_not_an_allowlist(self):
        for version in (VALIDATED_VERSION, "0.154.0", "0.152.0", "1.0.0", "0.154.0-alpha.1"):
            with self.subTest(version=version):
                notes = []
                result = subprocess.CompletedProcess(
                    ["codex", "--version"], 0, stdout=f"codex-cli {version}\n"
                )
                with mock.patch("subprocess.run", return_value=result) as run:
                    check_version("codex", notes.append)
                self.assertEqual(run.call_args.args[0], ["codex", "--version"])
                if version == VALIDATED_VERSION:
                    self.assertEqual(notes, [])
                else:
                    self.assertEqual(len(notes), 1)
                    self.assertIn(version, notes[0])
                    self.assertIn(VALIDATED_VERSION, notes[0])

    def test_failed_or_empty_version_probe_is_still_an_error(self):
        failures = (
            OSError("fixture executable unavailable"),
            subprocess.CalledProcessError(1, ["codex", "--version"]),
            subprocess.TimeoutExpired(["codex", "--version"], 15),
        )
        for error in failures:
            with self.subTest(error=error), mock.patch("subprocess.run", side_effect=error):
                with self.assertRaisesRegex(SyncError, "Cannot run Codex"):
                    check_version("codex")
        empty = subprocess.CompletedProcess(["codex", "--version"], 0, stdout="\n")
        with mock.patch("subprocess.run", return_value=empty):
            with self.assertRaisesRegex(SyncError, "empty version"):
                check_version("codex")

    def test_model_turns_are_not_an_allowed_method(self):
        server = object.__new__(AppServer)
        with self.assertRaisesRegex(SyncError, "does not permit"):
            server.call("turn/start", {"threadId": "anything"})

    def test_same_id_server_request_is_denied_and_mcp_is_disabled(self):
        recorded = self.root / "requests.jsonl"
        binary = self.root / "fake-codex"
        program = "#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n"
        program += "recorded = Path(" + repr(str(recorded)) + ")\n"
        program += """
for line in sys.stdin:
    message = json.loads(line)
    with recorded.open('a') as log:
        log.write(json.dumps(message) + '\\n')
    method = message.get('method')
    if method == 'initialize':
        print(json.dumps({'id': message['id'], 'method': 'item/commandExecution/requestApproval', 'params': {}}), flush=True)
    if 'id' not in message or not method:
        continue
    result = {}
    if method == 'config/read':
        result = {'config': {'mcp_servers': {'configured': {'command': 'must-not-run'}}}}
    if method == 'thread/resume':
        result = message['params']
    print(json.dumps({'id': message['id'], 'result': result}), flush=True)
"""
        binary.write_text(program)
        binary.chmod(0o700)
        log = self.root / "adapter.log"
        server = AppServer(self.target, str(binary), log, self.target)
        try:
            result = server.call("thread/resume", {"threadId": "sample", "cwd": str(self.root)})
            self.assertFalse(result["config"]["mcp_servers"]["configured"]["enabled"])
        finally:
            server.close()
        calls = [json.loads(line) for line in recorded.read_text().splitlines()]
        denials = [row for row in calls if "error" in row]
        self.assertEqual(denials[0]["error"]["code"], -32601)
        self.assertFalse(any(row.get("method") == "turn/start" for row in calls))
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
