# Continuum — Claude Code Session Guide

This is the source repo for the `continuum` Python package.
Repo: https://github.com/zackbrooks84/continuum

## Resuming Work

If the Continuum MCP server is running, start with:
```
smart_resume("continuum")
```

If not, run this in the repo directory:
```bash
continuum resume continuum
```

Or install and start fresh:
```bash
pip install -e ".[dev]"
continuum setup
continuum resume continuum
```

## Project State

All features are live on `main`. The package installs and runs on Windows.

### What's built (29 MCP tools)
- Task queue: `forge_push`, `forge_status`, `forge_result`, `forge_cancel`, `forge_list`
- Checkpoints: `checkpoint`, `handoff`, `remember`, `push_and_checkpoint`
- Memory search (FTS5): `memory_search`, `memory_timeline`, `memory_get`, `smart_resume`
- Auto-observe: `observe_control` (merged toggle + status)
- Auto-sync: `sync_files` — zero-config, defaults to `~/.continuum/projects/{project}/`
- Notifications: `notify_configure`, `notify_test`, `notify_complete` (Telegram/Discord/webhook)
- Web bridge: `export_handoff`, `import_web_handoff`
- Cross-agent: `cross_agent_handoff`
- Pattern learning: `pattern_suggestions`
- Token guard: `token_watch`
- Automation: `auto_mode`, `quickstart`

### Known Windows quirk
Rich's legacy Windows console renderer can't handle UTF-8 symbols.
**Fix already applied** in `cli.py`: `legacy_windows=False` + `io.TextIOWrapper` stdout wrapper.
Do NOT revert this. Do NOT try `PYTHONIOENCODING` env var — it doesn't help.

### Pending (from last checkpoint d5fafd0a)
1. Push `cli.py` Windows fix to GitHub
2. Register MCP server in `claude_desktop_config.json`
3. Write X launch thread: "the triple C threat — claude code continuum"

## Architecture

```
continuum/
├── models.py       # Task, Checkpoint, Decision, Handoff, ProjectConfig (Pydantic)
├── db.py           # SQLite + FTS5 + observations + notify_configs tables
├── runner.py       # Task executor + auto-checkpoint + notification firing
├── daemon.py       # Background process manager (task runner + observer thread)
├── observer.py     # Auto-observe: passive capture + rule/Claude compression
├── handoff.py      # Compact briefing generator
├── sync_files.py   # Auto-sync: render MEMORY/DECISIONS/TASKS.md
├── notify.py       # Notification dispatcher (Telegram, Discord, Slack, webhook)
├── mcp_server.py   # FastMCP server — all 29 tools
└── cli.py          # Click CLI
```

## Running the MCP Server

```bash
python -m continuum.mcp_server
# or via Claude Desktop config (see below)
```

## Owner
Zack Brooks — zackbrooks84 on GitHub.
Built with Claude Code (Sonnet 4.6).
