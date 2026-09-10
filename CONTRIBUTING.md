# Contributing

codex-everywhere uses Python 3.10+ and the standard library at runtime. Keep changes focused, readable and covered by checks appropriate to the behavior being changed.

## Development

Run from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -B -m unittest discover -v
.venv/bin/ruff check codex_everywhere tests codex-everywhere
.venv/bin/ruff format --check codex_everywhere tests codex-everywhere
.venv/bin/pre-commit run --files <changed-files>
```

The default tests use synthetic session homes and executable fixtures. They do not require SSH access to real machines or launch a real model turn. For UI changes, review the English snapshots in `tests/snapshots/` at each terminal size before updating expectations. Keep Unicode input coverage for display widths, search and cursor positioning.

## Change boundaries

The remote worker may read session JSONL, `session_index.jsonl`, and the launcher's optional `.codex-everywhere/locations.json`, and export to stdout. Keep destination writes and the native Codex adapter out of it. Planning stays read-only; application must recheck the destination and back up replaced files, including directory hints. Browser titles and timestamps must never determine whether history can be overwritten. Browser filters must not remove required ancestors from a transfer.

Preserve confirmation and cancellation, terminal restoration, worker cleanup and the current machine's environment when changing the launcher. Changes to history reading, bundle validation or destination writes need the corresponding safety tests.

Native integration is opt-in and uses disposable copies of a stopped backup. It has been validated with Codex 0.153.4. To check another version, pass its executable with `--codex`:

```bash
python3 -B -m tests.native_integration \
  --source-home /path/to/stopped-backup \
  --session UUID --expected-turns N --expected-items N \
  --archive-ancestor --check-update
```

`--check-update` needs an earlier completed task in the selected root session. The runner prints the private temporary directory; inspect it and remove it yourself afterward. Use only synthetic fixtures in routine tests.

## Before sharing

Keep personal machine settings in the ignored `config.json` or your user configuration directory. Never commit transcripts, credentials, exported bundles, databases, operation logs or real machine inventories. Use generic hosts and paths in examples and fixtures.

Describe the problem, the resulting behavior and the checks you ran. Disclose AI assistance and review every changed line before submitting.
