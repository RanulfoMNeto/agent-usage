# Agent Usage

**Your Codex and Claude Code usage, limits, and resets — on your desktop.**

A local widget for Linux and Windows. Read native session history, check subscription
availability, and inspect activity by day or project. No browser, account with us,
API key, or external collector.

![Agent Usage in History and Compact views, showing fictional Codex and Claude usage](.github/assets/preview.png)

*The preview uses fictional data. Try the same interface with `agent-usage --demo`.*

<details>
<summary>History, Weekly and Day — up close</summary>

**History** — activity across days; select a cell to inspect that day.

<img src=".github/assets/history.png" width="420" alt="History heatmap with recorded daily token activity">

**Weekly** — the current subscription reset window.

<img src=".github/assets/weekly.png" width="420" alt="Weekly activity within the current subscription window">

**Day** — the selected day's prompts, tokens and available cost estimates.

<img src=".github/assets/day.png" width="420" alt="Selected day with its line chart and usage totals">

</details>

## Get started

**Portable app:** download the archive for your system from
[Releases](https://github.com/RanulfoMNeto/agent-usage/releases), extract the **whole
folder**, and open `agent-usage` on Linux or `agent-usage.exe` on Windows. Python
and Qt are included. Keep the `_internal` directory beside the executable.

**From source:** install [uv](https://docs.astral.sh/uv/getting-started/installation/)
and Git, then run:

```sh
git clone https://github.com/RanulfoMNeto/agent-usage.git
cd agent-usage
uv run --locked --no-dev agent-usage
```

Python 3.12–3.14 is supported; uv can supply it. A desktop session is required.
An existing agent installation is needed for real data. To try the widget first:

```sh
uv run --locked --no-dev agent-usage --demo
```

Demo mode creates six months of fictional native sessions in a temporary profile, makes no
account queries, and removes its profile on normal exit. It does not require
Codex or Claude Code. Close it to return to your real profile.

Drag the header to move the widget. **Compact** shows limits; **Normal** adds
activity. Click a day in **Weekly** or **History** to inspect its chart and totals.
Hover previews; clicking selects. Settings controls appearance, warnings, projects
and sources. The close button quits the app.

## Connect your agents

Activity is discovered automatically; subscription limits have separate requirements.

| Data | Codex | Claude Code |
| --- | --- | --- |
| Local activity | Native session files | Native project session files |
| Subscription limits | Installed `codex` CLI with an existing ChatGPT login | Terminal status-line integration below |
| VS Code | Activity when the extension writes native session files; limits use the CLI | Activity when native session files exist; the extension alone does not supply limits |

### Codex

Install and sign in to the [Codex CLI](https://learn.chatgpt.com/docs/codex/cli).
Make sure `codex` is available on the widget's `PATH`. The widget uses the documented
[`account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server) operation.
It does not read or store your login tokens. API-key accounts may not provide
ChatGPT subscription limits.

If a desktop launch cannot find the executable, set its absolute path in the
[configuration](#configuration). On Windows, use the native executable rather than
a shell alias. WSL sessions and credentials belong to a separate environment.

### Claude Code

Generate settings for the installed widget:

```sh
uv run --locked --no-dev agent-usage --claude-config
```

For a portable build, run `./agent-usage --claude-config` on Linux or
`.\agent-usage.exe --claude-config` in Windows PowerShell from its extracted folder.
The command prints JSON with the correct executable path; it changes no files.

Merge the printed `statusLine` field into `~/.claude/settings.json` (`~` is your
home directory; respect `CLAUDE_CONFIG_DIR` when set). Keep your other settings.
If you already use a status-line script, pass the same input JSON to the printed
capture command inside that script, preserving its existing output.

Start a Claude Code terminal session and complete a request. Eligible subscription
accounts supply `rate_limits` after an API response; each window can be absent.
The widget retains only quota fields from this input. A closed or inactive
terminal can leave an old snapshot, which is marked as stale.
[Official status-line documentation](https://code.claude.com/docs/en/statusline).

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

`CODEX_HOME` and `CLAUDE_CONFIG_DIR` are respected. **Settings → Sources** changes
the directories to read. **Project** selects a repository using the working
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

**Sources → Export / Import** transfers the collected archive as JSON. Import
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
range. Release builds also exercise the actual executable and renderer in an
isolated demo. This does not cover every display driver, desktop or agent version.
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

To update, replace the portable app or pull the source and rerun the locked
command. State stays outside the installation. To uninstall, remove the GNOME
integration if enabled, remove the Claude capture command from its settings,
and delete the application folder. Delete the state directory separately only
if you want to erase the archive.

## Development and support

The Python package is `usage`; the application command is `agent-usage`.
Qt WebChannel connects the bundled UI directly to the collector. The same
components power the live widget, demo and packaged builds.

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
