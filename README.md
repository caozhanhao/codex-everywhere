# codex-everywhere

A lightweight Codex CLI launcher with session sync -- pick up right where you left off across machines.

> [!WARNING]
> An unofficial project built entirely by Codex. Use at your own risk.

```text
  codex-everywhere                                                  ~/project

  / Search sessions

    + New session
    Refactor the storage layer                      server-a     2h ago
  › Investigate a failing integration test          workstation  Yesterday

  ─ Sessions ─────────────────────────────────────────────────── Session 2/2 ─
  [Enter] Open  [/] Search  [g] All projects  [?] Help  [Esc] Exit
  Nodes [n]  2 ready
```

## Why this exists

Switching machines and resuming an ongoing Codex session seamlessly was the goal. However:

- **Without a shared filesystem**, there is no convenient way to access or move conversations across machines.
- **With NFS**, sharing a Codex home can also share live SQLite databases between machines. That setup has [locking and corruption risks](https://www.sqlite.org/howtocorrupt.html); SQLite's [WAL mode also requires all processes using a database to run on the same host](https://www.sqlite.org/wal.html).

codex-everywhere solves this by keeping all data local to each machine. It scans conversations on remote hosts via SSH and pulls over history on demand. Codex then rebuilds its indexes locally, allowing you to resume your workflow with commands executing natively on your current machine.

## Get started

The receiving machine needs **Linux or macOS, Python 3.10+, Codex CLI**, and an existing Codex home on a supported local filesystem. Source machines need Linux or macOS, Python 3.10+ and SSH access; they do not need codex-everywhere installed.

Session sync and reconstruction have been validated with **Codex CLI 0.153.4 and 0.154.0**, including segmented histories on 0.154.0. Other versions are allowed but haven't been verified.

From this checkout:

```bash
cp config.example.json config.json
chmod 600 config.json
# Edit config.json with your SSH hosts and Codex data directories.
./codex-everywhere
```

There are no third-party Python runtime dependencies. To use the launcher from a project directory, put this checkout on your `PATH` or invoke its executable by absolute path:

```bash
cd /path/to/project
/path/to/codex-everywhere/codex-everywhere
```

Set up Codex and authenticate separately on each machine where you plan to run it. If your home directory is on NFS, ensure `CODEX_HOME` points to a directory on a local disk, and configure `home` in the launcher accordingly. Any separate SQLite directory must also reside locally; if your `config.toml` specifies `sqlite_home`, set it explicitly in the launcher config as well.

SSH uses your existing aliases, keys and agent without interactive prompts.

## Configuration

Start with [config.example.json](config.example.json) and keep your machine settings in `config.json`, which is ignored by Git.

Configuration is loaded from the first applicable location:

1. `--config /path/to/config.json`.
2. `config.json` beside this checkout's launcher, regardless of the directory you launch from.
3. `$XDG_CONFIG_HOME/codex-everywhere/config.json`, or `~/.config/codex-everywhere/config.json`.

Files are not merged. With no configuration, the launcher uses the local `CODEX_HOME` (default `~/.codex`) and connects to no remote machines.

```json
{
  "remote_home": "~/.codex",
  "nodes": ["server-a", "server-b"]
}
```

Replace these examples with your own SSH aliases and existing data directories. On a receiving machine, `home` and `sqlite_home` must be writable local paths; update both when moving the launcher configuration to another machine. Each source's `remote_home` can point to a different directory.

For a different display name or data directory, use an object such as `{"host": "server-b", "name": "gpu", "remote_home": "/var/local/codex"}` in the same list.

| Setting | Meaning |
| --- | --- |
| `home` | Local Codex home. Defaults to `CODEX_HOME`, then `~/.codex`. |
| `sqlite_home` | Local SQLite directory. Defaults to `CODEX_SQLITE_HOME` when using the same `CODEX_HOME`, otherwise `home`. |
| `remote_home` | Default source Codex home; set it globally or per node when using remote sources. |
| `nodes` | SSH hosts as strings, or objects with `host` and optional `name`, `remote_home`, `mappings`. Names default to hosts and must be unique; `local` is reserved. |
| `codex` | Local Codex executable. Default: `codex`. |
| `scan_timeout` | Seconds allowed for a node's session list. Default: `25`. |
| `transfer_timeout` | Seconds allowed for a transfer. Default: `600`. |
| `workers` | Concurrent session-list readers. Default: `4`. |

Command-line overrides take precedence over file settings, then environment defaults. Remote paths use `nodes[].remote_home`, then the top-level `remote_home`; `~/` expands on the remote machine. A node matching the current hostname is omitted from remote scans. The tool does not discover or guess other hosts.

If project locations differ, configure mappings for each source on the receiving machine:

```json
{
  "remote_home": "~/.codex",
  "nodes": [
    {"host": "server-a", "mappings": [["/srv/projects", "/home/dev/projects"]]},
    {"host": "server-b", "mappings": [["/work", "/home/dev/projects"]]},
    "server-c"
  ]
}
```

Each pair maps a source prefix to a local prefix. They match complete path prefixes, longest first, and apply when filtering the browser, importing and opening a session.

For a one-off transfer or a bundle without a configured source:

```bash
codex-everywhere --map /remote/work=/local/work
codex-everywhere --cwd /path/to/existing/project --global
```

The interactive browser asks where to open a session when its directory differs from the current one. Choose the session directory, the current directory, or enter another local path. If the session directory is unavailable on this machine, the current directory is selected by default. Valid explicit overrides and remembered local directories skip this prompt.

For remote sessions, this choice also applies to required ancestors and appears in the sync preview before anything is installed. Downloaded history is reused when choosing a different directory. Back cancels the open; invalid paths can be corrected directly in the input screen.

`--map` can be repeated; it overrides a node rule with the same source prefix for that invocation. `--cwd` overrides the directory for launching and for all imported ancestors; it does not change the browser's initial directory filter.

After a confirmed transfer, the launcher remembers mapped directories in `.codex-everywhere/locations.json`, so local browsing and resume still work when the source is offline.

## Using the launcher

The browser starts with conversations from your current project. Press `g` to see all projects. Local and remote copies appear together, sorted by recorded activity; the node indicator shows which copy will open. Use `Tab` to choose a different copy of the same session.

Names set with Codex's `/rename` are shown when found in a node's sampled `session_index.jsonl`. These names are used for browsing and are not included in session transfers.

| Key | Action |
| --- | --- |
| `↑` / `↓`, `j` / `k` | Select a session |
| `Enter` | Open the selection |
| `+` | Start a new session in the launch directory |
| `/` | Search titles, previews, IDs, nodes and paths |
| `g` | Toggle this project / all projects |
| `Tab` | Choose another copy of the selected session |
| `a` | Include archived sessions |
| `d` | Show details |
| `n` / `r` | Node status / refresh |
| `?` | Help |
| `Esc` | Back from a page or dialog; exit from the session list. While editing search, clear it and return to the list. |

During comparison, `Esc` cancels and returns to the list. If syncing has already started writing, it finishes the write and returns to the list without opening Codex.

On an error page, `Retry` is selected by default; press `Enter` to retry the same session and source. Select `Back` with the arrow keys or `Tab`, or press `Esc` to return. Errors distinguish local contention from changes to the remote snapshot.

Other Codex sessions can stay open on both machines. A local import locks only the selected sessions and their required ancestors; if one is in use, close that session and retry. Background history maintenance can briefly block imports too. Remote exports are read-only snapshots: if the selected history changes during export, wait for it to settle and retry.

Continue a given conversation on one machine at a time to avoid diverging histories. When you switch back, open the launcher there and select the updated copy.

`Updating…` means results are still arriving and the list may change. Opening a session during this time asks you to confirm that the selected copy may not be the latest. Cancel to review the list, or continue with that session and source. New sessions can start immediately. `Incomplete list` means a node failed; `Read warnings` means some files or name hints were skipped or only partly read. Press `n` for details. This is a snapshot of reachable machines, not a guarantee that you have seen every newer copy. Refresh with `r` when needed.

## Manual transfers

Global options go before the subcommand. CLI `pull` and `import` **apply immediately**; use `--dry-run` to compare without modifying the destination. A dry run still stages and validates the history in temporary storage; for `pull`, that includes downloading it.

```bash
# Read all configured nodes, without browser filters.
codex-everywhere list --json

# Preview a session transfer, including its ancestors.
codex-everywhere pull server-a --session SESSION_UUID --dry-run

# Apply it and rebuild local indexes.
codex-everywhere pull server-a --session SESSION_UUID

# Export, carry the bundle to another machine, then import there.
# Export requires a new filename outside the source Codex home.
codex-everywhere export /tmp/session.zip --session SESSION_UUID
codex-everywhere import /tmp/session.zip

# Retry indexing after correcting a failed reconstruction.
codex-everywhere rebuild --session SESSION_UUID
```

For a machine absent from your configuration, use `pull user@host --source-home /path/to/codex --session SESSION_UUID`. To pull every session on a node, including archived and internal sessions, use `pull NODE --all`. `--no-rebuild` defers indexing; run `rebuild` before resuming those sessions. To sync back, run the launcher on the previous machine and select the updated copy.

## Compared with NFS

A shared directory is convenient when all your machines can mount it. The tradeoff is deciding which parts of Codex can be shared and how to hand a session from one process to another.

| Approach | Experience | Tradeoff |
| --- | --- | --- |
| Entire Codex home on NFS | One shared directory on every machine | Live SQLite databases are shared too; concurrent access across machines risks corruption |
| Session files on NFS, SQLite local | History files are shared, databases stay separate | You still need to coordinate writers and keep each machine's indexes and metadata in step |
| codex-everywhere | Browse conversations across machines and sync when opening one | Each machine keeps its own data; switching requires a transfer and a working local environment |

Sharing only session files can be a reasonable starting point if you already have NFS and can manage those handoffs. codex-everywhere puts browsing, transfer and launch in one place, including for machines that have SSH access but no shared filesystem.

## Data handling and limits

codex-everywhere keeps its locks, directory hints, backups and operation records under `<CODEX_HOME>/.codex-everywhere/`.

- **Sources are read-only.** The SSH worker reads JSONL and streams a bundle. It does not start source Codex, edit session files, or open source SQLite databases.
- **Transfers contain history and directory hints.** Bundles contain active/archived JSONL plus a checksum manifest with any remembered directories for the selected sessions and their ancestors. They exclude databases, authentication, configuration, plugins and source code. Session text itself may contain sensitive information.
- **Updates require matching history.** Session UUIDs identify copies. Identical content is skipped; a strict extension can update a shorter copy. An older incoming copy cannot replace a longer local history. Divergence stops the entire batch; there is no automatic merge.
- **Changed files are backed up.** Replaced files and operation records live under `.codex-everywhere/backups/`. Conflicting incoming bundles can be saved under `.codex-everywhere/conflicts/`; standalone index rebuilds are recorded under `.codex-everywhere/rebuilds/`. Installation is atomic per file, not across the entire batch. After interruption, inspect the record before retrying.
- **No live handoff or environment migration.** Prepare the destination's working tree, dependencies, environment variables and credentials yourself. Running processes do not move.
- **This is not a complete Codex-home replica.** Deletions, pins, custom names, database titles, goals and queues are not synchronized. Imported archived sessions become active for reconstruction; already-local archived sessions need `codex unarchive` first.

For development and isolated tests, see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).
