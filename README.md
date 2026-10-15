# Agent Usage

A persistent desktop widget for Codex and Claude Code on Linux and Windows.
Subscription limits, recorded tokens, prompt duration and API cost estimates.
No browser, HTTP server, API key or external collector.

## Run

Python 3.11–3.15 and a desktop session are required. From a fresh clone:

```sh
uv run --locked --no-dev agent-usage
```

Alternatively, install into a Python virtual environment with `python -m pip
install .`, then run `agent-usage`. The package includes its UI and assets; it
works independently of the checkout. Qt supplies the window and embedded renderer.

The widget opens immediately. Drag its header to move it. Normal view keeps its
frame when selecting days or opening Settings. The whole panel scales only when
needed to fit the available screen; there is no vertical page scroll. **Keep on top** pins it above other windows. The close button
quits. Windows uses this independent persistent window, without Rainmeter.
Other Linux desktops use the same window; positioning and pinning depend on
the compositor.

**GNOME 50:** install the included desktop integration once:

```sh
uv run --locked --no-dev agent-usage --install-gnome
```

Sign out and back in. The widget starts at login, stays behind normal windows
on every workspace and is absent from Alt-Tab. Dragging saves its position;
**Keep on top** temporarily brings it above applications. The small extension
uses [Mutter's window API](https://gnome.pages.gitlab.gnome.org/mutter/meta/method.Window.set_type.html)
and the session bus to manage the existing Qt window, including on Wayland.
It does not duplicate the UI or collector. Other GNOME versions use the regular
window until explicitly supported. Remove with `agent-usage --uninstall-gnome`;
history is retained. GNOME user extensions must be enabled.

**Settings:** view, style, project, window pinning and usage warning.
**Compact** shows subscription usage only; **Normal** includes activity and
charts. Compact uses a narrower window and a single row of controls; Settings
expands it vertically, keeping the header aligned. **Default** is Minimalism. Glassmorphism, Neo-Brutalism,
Neumorphism and Swiss Design share the same layout and card surfaces, with
different borders, corners and subtle depth. Claude stays orange and Codex teal
in every style.
The header switches dark/light. Inter and Baloo 2 (Neo-Brutalism headings) are
bundled with their licenses; no font download at runtime.
Glass uses a translucent native window and layered surfaces. It does not provide
OS-level wallpaper blur; transparency requires a compositing desktop.
Today shows prompts, Weekly follows the current subscription cycle, and History
shows retained days. Click a day to see its line chart and totals together,
while keeping the heatmap visible. The same chart renders all three views.
Hover only previews; clicking selects. Empty days and missing measurements
are explicit. Weekly days include only the portion inside the current cycle;
History days include all retained records, grouped by local prompt start time.

## Local data

| Agent | Discovered session files |
| --- | --- |
| Codex | `~/.codex/sessions/` and `~/.codex/archived_sessions/` |
| Claude Code | `~/.claude/projects/` |

`~` means the current user's home. `CODEX_HOME` and `CLAUDE_CONFIG_DIR` are
respected. CLI and IDE sessions work when they write these native files.
The widget reads session records and retains usage metadata and short prompt
titles, not full conversation bodies. No home directory is bundled or uploaded.

- **Settings → Sources:** folders containing native session files. Click to
  change; **Reset** restores discovery. A repository folder is not a source
  unless it contains session files. Changing sources preserves collected history.
- **Settings → Project:** choose a repository folder using the native folder
  picker, just like Sources. **Reset** returns to all projects. The filter uses
  the working directory recorded in sessions, including subdirectories. It does
  not infer the project from files edited elsewhere. Records without a directory
  appear only when no project is selected.
  Subscription limits always describe the entire account.
- **Export / Import:** transfer the collected usage archive as JSON. Project
  metadata is included; older backups remain readable. On upgrade, available
  native files are reread once to backfill directories without duplicating usage.

History and settings live outside the repository: Linux uses
`$XDG_STATE_HOME/agent-usage` (default `~/.local/state/agent-usage`); Windows
uses `%LOCALAPPDATA%\AgentUsage`. SQLite retains previously collected records,
including after changing a source. Export contains all collected usage, not
original conversations; import merges records without duplicates (64 MiB max).
Unknown measurements remain unknown. API estimates are not subscription charges.

## Native limits

- **Codex:** uses the installed `codex` executable and its existing login via
  [`account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server).
- **Claude:** uses the native [status line](https://code.claude.com/docs/en/statusline).
  Set its command to `"/absolute/path/to/python" -m usage --capture-claude`
  using the Python environment where this package is installed. Preserve any
  existing status-line command. The VS Code extension alone does not supply
  quota data to this adapter.

Limits are account-reported; local tokens cannot reconstruct a subscription
percentage. Missing quotas remain unavailable, and old snapshots are marked.

Optional overrides: `~/.config/agent-usage/config.toml` on Linux
(`XDG_CONFIG_HOME` supported), `%LOCALAPPDATA%\AgentUsage\config.toml` on Windows,
or `--config PATH`. See [defaults](usage/defaults.toml). For example:

```toml
[subscriptions]
codex_command = ["/absolute/path/to/codex"]
```

## Development

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
node --check usage/static/assets/app.js
node --check usage/gnome/extension.js
uv build
```

CI checks Linux and Windows. The UI can load only bundled assets and communicates
with Python through Qt WebChannel, without a listening port. Native folder/file
pickers handle source changes and backups. Local processes running as your user
remain trusted. Configuration, credentials, databases and exports do not belong
in the repository. Third-party asset licenses are retained; Qt keeps its own
license. This project is independent of OpenAI and Anthropic.
