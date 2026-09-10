"""Isolated source-read audit, invoked by test_safety in a subprocess."""

import io
import os
import sys
from pathlib import Path

from codex_everywhere import reader


def main():
    home = Path(sys.argv[1]).resolve()

    def audit(event, args):
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(home):
                assert not args[2] & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
                ), (event, args)
                assert path.suffix == ".jsonl" or path == home / reader.LOCATIONS_FILE, path
        if event in ("os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.utime"):
            for arg in args[:2]:
                if isinstance(arg, str):
                    assert not Path(arg).resolve().is_relative_to(home), (event, args)

    sys.addaudithook(audit)
    assert len(reader.scan(home)["sessions"]) == 1
    reader.export_bundle(home, io.BytesIO())


if __name__ == "__main__":
    main()
