"""Manual, conservative Codex session transfer between machines."""

import sys

if sys.version_info < (3, 10):  # noqa: UP036 -- Explain unsupported source-checkout interpreters.
    raise SystemExit(
        "codex-everywhere requires Python 3.10+; "
        f"running {'.'.join(map(str, sys.version_info[:3]))} ({sys.executable}). "
        "Run the launcher with Python 3.10+ or activate a virtual environment using it."
    )

__version__ = "0.1.0"
