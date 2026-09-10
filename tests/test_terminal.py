"""Exercise curses, cancellation and terminal handoff with isolated executables."""

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from codex_everywhere.storage import STATE_DIRECTORY
from tests.fixtures import SessionFixture


class Terminal:
    def __init__(self, args, env):
        child_env = {**os.environ, "TERM": "xterm-256color", **env}
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        self.process = subprocess.Popen(
            [sys.executable, "-B", "-m", "codex_everywhere", *args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={key: value for key, value in child_env.items() if value is not None},
            start_new_session=True,
        )
        os.close(slave)
        self.captured = bytearray()

    def wait_for(self, text, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text.encode() in self.captured:
                return
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                try:
                    self.captured.extend(os.read(self.master, 65536))
                except OSError:
                    break
        raise AssertionError(f"TUI did not show {text!r}; tail: {bytes(self.captured[-3000:])!r}")

    def send(self, keys):
        os.write(self.master, keys)

    def wait_exit(self, timeout=5):
        # macOS terminal restoration can wait for pending output to be consumed.
        # Keep acting as the terminal reader while waiting for the child to exit.
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    output = os.read(self.master, 65536)
                except OSError:
                    break
                if not output:
                    break
                self.captured.extend(output)
        return self.process.wait(timeout=max(0, deadline - time.monotonic()))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        os.close(self.master)


class TerminalTests(SessionFixture):
    def config(self, binary):
        path = self.root / "config.json"
        path.write_text(
            json.dumps(
                {
                    "nodes": [
                        {
                            "name": "fixture-node",
                            "host": "fixture-host",
                            "remote_home": str(self.source),
                        }
                    ],
                    "home": str(self.target),
                    "codex": str(binary),
                }
            )
        )
        return path

    def test_missing_directory_can_open_here_or_in_a_typed_path_without_restarting(self):
        thread_id, path = self.session(
            self.target, cwd="/remote/unavailable-project", messages=("directory terminal fixture",)
        )
        original = path.read_bytes()
        project = self.root / "项目 with spaces; $(literal)"
        project.mkdir()
        capture = self.root / "capture.json"
        native = self.root / "native-codex"
        native.write_text(
            "#!/usr/bin/env python3\nimport json,os,sys,termios\nfrom pathlib import Path\n"
            "flags = termios.tcgetattr(0)[3]\n"
            "Path(os.environ['CE_CAPTURE']).write_text(json.dumps(dict(\n"
            "    argv=sys.argv[1:], canonical=bool(flags & termios.ICANON),\n"
            "    echo=bool(flags & termios.ECHO))))\nprint('NATIVE_DIRECTORY_HANDOFF')\n"
        )
        native.chmod(0o700)
        config = self.root / "local-config.json"
        config.write_text(json.dumps({"nodes": [], "home": str(self.target), "codex": str(native)}))
        for other in (False, True):
            with (
                self.subTest(other=other),
                Terminal(
                    ["--config", str(config), "--global"], {"CE_CAPTURE": str(capture)}
                ) as terminal,
            ):
                terminal.wait_for("directory terminal fixture")
                terminal.send(b"\n")
                terminal.wait_for("Choose working directory")
                terminal.wait_for("Use current directory")
                self.assertFalse(capture.exists())
                if other:
                    terminal.send(b"\x1bOB\n")
                    terminal.wait_for("Enter an existing local directory")
                    terminal.send(b"\x15/not-an-existing-local-directory\n")
                    terminal.wait_for("Directory unavailable. Enter an existing local path.")
                    self.assertFalse(capture.exists())
                    terminal.send(b"\x15" + str(project).encode() + b"\n")
                else:
                    terminal.send(b"\n")
                terminal.wait_for("NATIVE_DIRECTORY_HANDOFF")
                self.assertEqual(terminal.wait_exit(), 0)
                data = json.loads(capture.read_text())
                self.assertTrue(data["canonical"] and data["echo"])
                self.assertEqual(
                    data["argv"][2:],
                    ["--cd", str(project if other else Path.cwd().resolve()), "resume", thread_id],
                )
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse((self.target / STATE_DIRECTORY).exists())
                capture.unlink()

    def test_search_select_preview_and_arrow_cancel_leaves_target_empty(self):
        thread_id, _ = self.session(messages=("terminal fixture",))
        config = self.config(sys.executable)
        binary = self.root / "bin"
        binary.mkdir()
        ssh = binary / "ssh"
        ssh.write_text(
            "#!/usr/bin/env python3\nimport os,sys\nos.execvp('sh',['sh','-c',sys.argv[-1]])\n"
        )
        ssh.chmod(0o700)
        env = {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}
        with Terminal(["--config", str(config), "--global"], env) as terminal:
            terminal.wait_for("terminal fixture")
            terminal.send(b"/terminal\n\n")
            terminal.wait_for("Choose working directory")
            terminal.wait_for("Use session directory")
            terminal.send(b"\n")
            # Curses may redraw only part of a title. Wait for the full controls
            # unique to the confirmation screen before exercising them.
            terminal.wait_for("Sync this session here")
            terminal.wait_for("[ Cancel ]")
            terminal.wait_for("[ Sync & open ]")
            terminal.send(b"d")
            terminal.wait_for(thread_id)
            terminal.captured.clear()
            terminal.send(b"\x1b")
            terminal.wait_for("[ Sync & open ]")
            self.assertEqual(list(self.target.iterdir()), [])
            # Application-mode right arrow moves from Sync & open to Cancel.
            terminal.captured.clear()
            terminal.send(b"\x1bOC\n")
            terminal.wait_for("New session")
            terminal.send(b"\x1b")
            self.assertEqual(terminal.wait_exit(), 0)
            self.assertEqual(list(self.target.iterdir()), [])

    def test_search_escape_is_responsive_and_preserves_split_arrow_sequences(self):
        self.session(
            self.target,
            thread_id="00000000-0000-0000-0000-000000000001",
            messages=("first latency fixture",),
        )
        second_id, _ = self.session(
            self.target,
            thread_id="00000000-0000-0000-0000-000000000002",
            messages=("second latency fixture",),
        )
        config = self.root / "local.json"
        config.write_text(json.dumps({"nodes": [], "home": str(self.target)}))
        with Terminal(["--config", str(config), "--global"], {"ESCDELAY": None}) as terminal:
            terminal.wait_for("second latency fixture")
            # Allow scheduling slack while catching the old one-second Escape wait.
            for _ in range(3):
                terminal.captured.clear()
                terminal.send(b"/")
                terminal.wait_for("Done", timeout=0.5)
                terminal.captured.clear()
                terminal.send(b"\x1b")
                terminal.wait_for("Open", timeout=0.5)

            terminal.captured.clear()
            terminal.send(b"/latency")
            terminal.wait_for("Done")
            # A short gap within a direction key must not turn it into Escape.
            terminal.send(b"\x1b")
            time.sleep(0.005)
            terminal.send(b"OB")
            terminal.send(b"\nd")
            terminal.wait_for(second_id)
            terminal.captured.clear()
            terminal.send(b"q\x1b")  # q is inert; Escape leaves Details without exiting.
            terminal.wait_for("New session")
            self.assertIsNone(terminal.process.poll())
            terminal.send(b"\x1b")
            self.assertEqual(terminal.wait_exit(), 0)

    def test_error_enter_retries_without_reselecting_the_remote_session(self):
        self.session(messages=("retry terminal fixture",))
        config = self.config(sys.executable)
        binary = self.root / "bin"
        binary.mkdir()
        ssh = binary / "ssh"
        marker = self.root / "failed-once"
        ssh.write_text(
            "#!/usr/bin/env python3\nimport os,shlex,sys\nfrom pathlib import Path\n"
            + "marker = Path("
            + repr(str(marker))
            + ")\n"
            + """if 'export' in shlex.split(sys.argv[-1]) and not marker.exists():
    sys.stdin.read()
    marker.touch()
    print('Remote fixture-node: Source history changed during export. Retry.', file=sys.stderr)
    sys.exit(2)
os.execvp('sh', ['sh', '-c', sys.argv[-1]])
"""
        )
        ssh.chmod(0o700)
        env = {"PATH": str(binary) + os.pathsep + os.environ["PATH"]}
        with Terminal(["--config", str(config), "--global"], env) as terminal:
            terminal.wait_for("retry terminal fixture")
            terminal.send(b"\n")
            terminal.wait_for("Unable to open")
            terminal.wait_for("Remote fixture-node")
            terminal.wait_for("[ Retry ]")
            terminal.wait_for("[ Back ]")
            terminal.captured.clear()
            terminal.send(b"\n")
            terminal.wait_for("Choose working directory")
            terminal.wait_for("Use session directory")
            terminal.send(b"\n")
            terminal.wait_for("Sync this session here")
            terminal.wait_for("[ Sync & open ]")
            self.assertEqual(list(self.target.iterdir()), [])
            terminal.captured.clear()
            terminal.send(b"\x1bOC\n")
            terminal.wait_for("New session")
            terminal.send(b"\x1b")
            self.assertEqual(terminal.wait_exit(), 0)

    def test_new_and_local_resume_restore_terminal_and_cancel_pending_ssh_before_exec(self):
        thread_id, path = self.session(self.target, messages=("local terminal fixture",))
        original = path.read_bytes()
        binary = self.root / "bin"
        binary.mkdir()
        ssh = binary / "ssh"
        ssh.write_text(
            "#!/usr/bin/env python3\nimport os,time\n"
            "from pathlib import Path\n"
            "Path(os.environ['CE_SSH_PID']).write_text(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        ssh.chmod(0o700)
        native = binary / "native codex; literal"
        native.write_text(
            "#!/usr/bin/env python3\nimport json,os,sys,termios\n"
            "from pathlib import Path\n"
            "flags = termios.tcgetattr(0)[3]\n"
            "result = dict(argv=sys.argv[1:], pid=os.getpid(), tty=all(os.isatty(n) for n in range(3)), "
            "canonical=bool(flags & termios.ICANON), echo=bool(flags & termios.ECHO), "
            "home=os.environ['CODEX_HOME'], sqlite=os.environ['CODEX_SQLITE_HOME'], "
            "inherited=os.environ['CE_INHERITED'])\n"
            "Path(os.environ['CE_CAPTURE']).write_text(json.dumps(result))\n"
            "print('NATIVE_HANDOFF')\nsys.exit(17)\n"
        )
        native.chmod(0o700)
        config = self.config(native)
        capture, ssh_pid = self.root / "capture.json", self.root / "ssh.pid"
        env = {
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "CE_CAPTURE": str(capture),
            "CE_SSH_PID": str(ssh_pid),
            "CE_INHERITED": "current-machine-value",
        }
        # Explicit override makes the launch directory deterministic in both cases.
        args = ["--config", str(config), "--cwd", str(self.root), "--global"]
        for keys, session_id in ((b"+", None), (b"\n", thread_id)):
            with self.subTest(session_id=session_id), Terminal(args, env) as terminal:
                terminal.wait_for("local terminal fixture")
                deadline = time.monotonic() + 2
                while not ssh_pid.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ssh_pid.exists())
                remote_pid = int(ssh_pid.read_text())
                start = time.monotonic()
                terminal.send(keys)
                if session_id:
                    terminal.wait_for("Open this copy?")
                    terminal.wait_for("[ Continue ]")
                    self.assertFalse(capture.exists())
                    self.assertIsNone(terminal.process.poll())
                    terminal.captured.clear()
                    terminal.send(b"\n")  # Cancel has the initial focus.
                    terminal.wait_for("New session")
                    self.assertFalse(capture.exists())
                    terminal.captured.clear()
                    terminal.send(keys)
                    terminal.wait_for("[ Continue ]")
                    start = time.monotonic()
                    terminal.send(b"\x1bOC\n")
                terminal.wait_for("NATIVE_HANDOFF")
                self.assertLess(time.monotonic() - start, 3)
                self.assertEqual(terminal.wait_exit(), 17)
                data = json.loads(capture.read_text())
                self.assertEqual(data["pid"], terminal.process.pid)
                self.assertTrue(data["tty"] and data["canonical"] and data["echo"])
                self.assertEqual(data["home"], str(self.target))
                self.assertEqual(data["sqlite"], str(self.target))
                self.assertEqual(data["inherited"], "current-machine-value")
                self.assertEqual(data["argv"][2:4], ["--cd", str(self.root)])
                self.assertEqual(data["argv"][4:], ["resume", session_id] if session_id else [])
                with self.assertRaises(ProcessLookupError):
                    os.kill(remote_pid, 0)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse((self.target / STATE_DIRECTORY).exists())
                ssh_pid.unlink()
                capture.unlink()
