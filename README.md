![Agent Usage — your agent usage, limits and resets, right on your desktop](.github/assets/header.svg)

A local desktop widget for **Codex and Claude Code** on Linux and Windows.
Read native session history, check subscription availability, and inspect activity
by day or project. No API key or external collector. Account sign-in opens your browser.

## Get started

**Portable app:** download the archive for your system from
[Releases](https://github.com/RanulfoMNeto/agent-usage/releases), extract the **whole
folder**, and open `agent-usage` on Linux or `agent-usage.exe` on Windows. Python
and Qt are included. Keep the `_internal` directory beside the executable.

On Windows, the first normal launch adds **Agent Usage** to the Start menu and
Windows Search. This works with the portable app and the source command below.
Subsequent launches from the shortcut need no terminal or administrator access.

**From source:** install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and Git, then run:

```sh
git clone https://github.com/RanulfoMNeto/agent-usage.git
cd agent-usage
uv run --locked --no-dev agent-usage
```

Python 3.12–3.14 is supported; uv can supply it. A desktop session is required.
`uv run` runs the source; it does not build a portable `.exe`. Keep the checkout
and its `.venv` for the source shortcut, or the extracted folder for the portable
app. After moving an installation, launch it once again to refresh the shortcut.
An existing agent installation is needed for real data. To try the widget first:

```sh
uv run --locked --no-dev agent-usage --demo
```

Demo mode creates six months of fictional native sessions in a temporary profile.
It makes no account queries and removes its profile on normal exit. It does not
require Codex or Claude Code. Close it to return to your real profile.

Drag the header to move the widget. Its height automatically fits the visible
content. **Compact** shows limits; **Normal** adds activity. Click a day in
**Weekly** or **History** to inspect its chart and totals.
Hover previews; clicking selects. Settings controls appearance, warnings, projects
and sources. The close button quits the app.

**Settings → Limits** chooses which reported limits to show across agents;
**Indicator** switches between bars (default) and rings. Limits share one card
per agent. These preferences persist locally and do not affect collection.
In **Both → Weekly**, each agent has its own current reset cycle. Clicking a day
selects that agent's activity; **History** combines both, with a breakdown on hover.
Heatmap intensity represents recorded tokens, not subscription percentages.

## See it in action

<p align="center">
  <img src=".github/assets/preview.png" width="400" alt="The complete Agent Usage widget in History view, with both agents and a selected day's line chart">
</p>

![History shows daily intensity, Weekly follows the current reset window, and Day shows prompt-level tokens and totals. Captured from the widget using fictional data.](.github/assets/activity.png)

*Real widget captures with fictional demo data. The demo starts in History;
Compact view shows subscription limits only.*

## Connect your agents

Open **Settings → Agents**. History is discovered locally; subscription limits
are queried through the installed agents every 60 seconds by default. No model
request or open terminal is needed to keep limits current.

| Agent | Connect | Limits |
| --- | --- | --- |
| Codex | Reuses your ChatGPT login or opens the official sign-in page | Native account interface |
| Claude Code | Opens the native terminal for sign-in if needed; you can close it afterward | Native usage control request |

Both agents are detected on `PATH`, in their standard installation, or inside
their official VS Code, VS Code Insiders or Cursor extension. Each agent handles
its own credentials; the widget does not read or store tokens. No login starts
on its own. API-key accounts do not provide subscription quotas. Windows and WSL
are separate environments. Custom executables can be set with
`subscriptions.codex_command` and `subscriptions.claude_command`.

Claude's `get_usage` control request is the **experimental** interface used by its
VS Code extension, verified with Claude Code **2.1.301**. It may change between
versions. Unsupported versions or failed queries show an unavailable state;
old snapshots are never presented as a successful refresh. The widget sends
control requests only, with tools, hooks and session persistence disabled.
[Codex account interface](https://learn.chatgpt.com/docs/app-server) ·
[Claude native CLI](https://code.claude.com/docs/en/cli-reference).

**Disconnect** pauses new history collection and account queries. The choice
persists across restarts; login, open sessions and collected history are kept.
Reconnecting also collects activity recorded while monitoring was paused.

Earlier Agent Usage status-line capture is removed automatically on startup.
A backed-up custom status line is restored when available; other Claude settings
and backups are preserved. The old `claude.json` snapshot is no longer used.

## Understand the data

- **Limits** are reported by the agent's account interface. They apply to the
  entire account, including when a project filter is active. Local tokens cannot
  reconstruct subscription percentages.
- **Weekly** follows the current reported weekly reset window. **Today** and
  **History** group prompts by their local start date. Different accounts may
  have different reset windows.
- **Tokens** include recorded input and output, including cached input. A prompt
  can involve several model calls. Missing measurements stay unknown.
- **Duration** is exact only when the session records a confirmed duration;
  otherwise it is identified as unconfirmed.
- **API cost** is an estimate, **not a subscription charge**. Native recorded costs
  are used when present; otherwise estimates require an exact model entry in
  [defaults.toml](usage/defaults.toml) or your configuration. The bundled price
  table currently covers only `gpt-6-astra`. Other models show no estimate unless
  pricing or native cost is available. Fast modes and external tool charges are
  excluded. Prices are versioned configuration, not a live pricing feed.

The widget reads existing files; it cannot recover sessions that were deleted
before collection. Native log formats can change between agent versions.

## Local data and privacy

| Agent | Default source |
| --- | --- |
| Codex | `~/.codex/sessions/` and `~/.codex/archived_sessions/` |
| Claude Code | `~/.claude/projects/` |

`CODEX_HOME` and `CLAUDE_CONFIG_DIR` are respected. **Settings → Agents → History folder** changes
the directory for each agent. **Project filter** selects a repository using the working
directory recorded in sessions, including its subdirectories. Records without
project metadata appear only in the unfiltered view.

The archive contains usage metadata, short prompt titles, project paths and
source-file bookkeeping in SQLite. It does not retain full conversation bodies.
There is no application analytics service or listening HTTP port. Native account
queries may contact the provider through its installed client; this is not an
offline account-limit service.

| System | History and preferences |
| --- | --- |
| Linux | `$XDG_STATE_HOME/agent-usage`, normally `~/.local/state/agent-usage` |
| Windows | `%LOCALAPPDATA%\AgentUsage` |

**Settings → Export / Import** transfers the collected archive as JSON. Import
merges records without duplicates, up to 64 MiB. Changing sources keeps collected
history. Exports can contain private titles and paths; review them before sharing.
To erase collected data, quit the widget and remove its state directory. Original
agent files remain unchanged and will be collected again if the widget is restarted.

## Compatibility

| Environment | Behavior |
| --- | --- |
| Linux desktop | Persistent Qt window; positioning and pinning depend on the compositor |
| GNOME 50 | Optional integration places the same widget on the desktop across workspaces |
| Windows x64 | Persistent window with **Keep on top**; no Rainmeter required |
| macOS, other GNOME versions | Not currently supported by the desktop integration |

CI runs the tests on Linux and Windows, at both ends of the supported Python
range. Release builds extract the distribution archive and exercise its executable
and renderer in an isolated demo. This does not cover every display driver,
desktop or agent version.
Glass is translucent; it does not supply operating-system wallpaper blur.

<details>
<summary>GNOME desktop integration</summary>

```sh
uv run --locked --no-dev agent-usage --install-gnome
```

Sign out and back in. The widget starts at login, stays behind normal windows
on every workspace, and is absent from Alt-Tab. **Keep on top** brings it forward.
GNOME user extensions must be enabled. Portable builds accept the same flag.

Remove with `agent-usage --uninstall-gnome` using the same installation.
History is retained. Remove this integration before deleting or moving the app.

</details>

## Configuration

Defaults are in [usage/defaults.toml](usage/defaults.toml). Overrides live at
`~/.config/agent-usage/config.toml` on Linux (`XDG_CONFIG_HOME` is respected), or
`%LOCALAPPDATA%\AgentUsage\config.toml` on Windows. `--config PATH` selects another
file. No personal configuration belongs in the repository.

```toml
[subscriptions]
codex_command = ["/absolute/path/to/codex"]
```

To disable Start menu shortcut registration:

```toml
[integrations]
windows_shortcut = false
```

This stops future shortcut updates; an existing shortcut is retained.

To update, replace the portable app or pull the source and rerun the locked
command. State stays outside the installation. To uninstall, remove the GNOME
integration if enabled, and delete the application folder and Windows Start menu
shortcut. Delete the
state directory separately only if you want to erase the archive.

## Development and support

The Python package is `usage`; the application command is `agent-usage`.
Qt WebChannel connects the bundled UI directly to the collector. The same
readers, SQLite archive, calculations and renderer power the live widget, demo
and packaged builds. Python’s standard library handles collection and storage;
Qt provides the desktop window, rendering and native dialogs.

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
node --check usage/static/assets/app.js
uv build
```

To build a portable app, run `uv run --group build python scripts/package.py`.
Then run `node tests/smoke.mjs dist/agent-usage/agent-usage` (use `agent-usage.exe`
on Windows). Build on the target operating system. Node 22+ is used only by
the packaged UI test. Tags matching the package version, such as `v0.1.0`, trigger
checks, native builds, checksums and a GitHub release.

Report reproducible bugs through [Issues](https://github.com/RanulfoMNeto/agent-usage/issues).
Include versions and redacted screenshots, never full transcripts or credentials.
Keep pull requests focused, reuse the existing readers and UI components, and
add regression tests for behavioral changes. Report vulnerabilities privately
through the repository's **Security → Report a vulnerability** option.

[MIT](LICENSE). Third-party assets retain their licenses. Portable builds bundle
Qt/PySide and their runtime dependencies; see the included license notices.
Maintained by [RanulfoMNeto](https://github.com/RanulfoMNeto). Independent of OpenAI
and Anthropic; their names and logos identify the supported agents.
