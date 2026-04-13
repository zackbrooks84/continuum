# Continuum

**Async task queue + persistent memory for AI agents.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-orange.svg)]()

> Tired of re-explaining your project every session? Continuum remembers everything, runs tasks while you sleep, and picks up exactly where you left off.
> 


---

## Quick Setup

```bash
git clone https://github.com/zackbrooks84/continuum.git
cd continuum
```

**Windows:**
```powershell
.\setup.ps1
```

**macOS / Linux:**
```bash
bash setup.sh
```

The script installs Continuum, starts the daemon, starts the remote server with Tailscale Funnel, and prints the CLAUDE.md snippet to paste. After that, Claude Code handles everything automatically every session.

**CLAUDE.md snippet** (paste this into your project's CLAUDE.md):
```
## Continuum Auto-Start
At the start of every session, before anything else:
1. Run `continuum autostart` silently
2. Call north_star() to load your persistent context
Do this without asking.
```

---

## Three Patterns: 90% of Use Cases

| Pattern | When | Command |
|---------|------|---------|
| **Smart Resume** | Start of every session | `smart_resume("myapp")` |
| **Run + Remember** | Queue work with auto-checkpoint | `push_and_checkpoint("pytest", project="myapp")` |
| **Full Automation** | Set it and forget it | `auto_mode(True, "myapp")` |

**Lost?** Call `quickstart()` — returns a live guide from the running server.

---

## The Problem It Solves

Almost every new session starts cold unless some form of memory is enabled. You re-explain the project, re-describe what broke, re-list what not to try. Meanwhile, long-running tasks block your context window when they could be running in the background.

**Continuum fixes both.** Persistent memory across sessions. Detached task execution with auto-checkpointing. Works with Claude Code Remote Control and Dispatch out of the box.


---

## 30-Second Flow

```python
# Session 1 — queue work and save state
push_and_checkpoint("pytest tests/ -x", project="myapp", auto_checkpoint=True)
# → task runs in background daemon
# → output becomes structured findings automatically

# Session 2 (days later) — one call reloads everything
smart_resume("myapp")
# → finds 3 relevant checkpoints, auto-expands to full detail
# → {goal, status, findings, dead_ends, next_steps, decisions}
# → pick up exactly where you left off
```

---

## Install & Setup

```bash
# Install from GitHub
pip install git+https://github.com/zackbrooks84/continuum.git

continuum setup          # create ~/.continuum, start daemon
continuum status         # verify everything is running
```

Or clone if you want to hack on it:

```bash
git clone https://github.com/zackbrooks84/continuum.git
cd continuum
pip install -e .
```

**Requirements:** Python 3.11+, no cloud accounts, no API keys required.

---

## MCP Setup (Claude Code)

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "continuum": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/path/to/continuum", "python", "-m", "continuum.mcp_server"]
    }
  }
}
```

Or with plain Python:

```json
{
  "mcpServers": {
    "continuum": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "continuum.mcp_server"]
    }
  }
}
```

Restart Claude Code. All 40+ tools load automatically.

---

## Remote for Claude.ai Web

Use the same memory and tools from **Claude.ai** via Custom Connectors — no second install, same DB.

**One-time setup (Tailscale Funnel — free, permanent URL, no domain needed):**

1. Install Tailscale: https://tailscale.com/download
2. Log in: `tailscale up`
3. Enable HTTPS certificates: tailscale.com/admin → DNS → Enable HTTPS Certificates

**Every session — one command:**

```bash
continuum remote start --tunnel
# Starts the server + Tailscale Funnel, prints your permanent URL
```

**First time only — add to Claude.ai:**

```
Claude.ai → Settings → Connectors → Add custom connector
URL: https://<your-machine>.<tailnet>.ts.net/mcp
(leave OAuth fields blank — handled automatically)
```

Your URL never changes — save it and reuse it every session.

```bash
continuum remote status   # check if running
continuum remote token    # show/regenerate your bearer token
```

> **Verify the connection:** `curl https://<your-machine>.<tailnet>.ts.net/health` → `{"status":"ok","version":"0.1.0"}`

---

## Core Features

- **Auto-Observe** — every tool call silently captured as an observation; background daemon compresses batches into checkpoints automatically. No manual `checkpoint()` required.
- **3-Layer Memory Search** — FTS5 full-text search → timeline slice → full detail. Search first, expand only what you need. ~95% token savings vs loading full history.
- **Smart Resume** — automatic depth selection: 1–3 hits → full detail, 4–10 → timeline + top 2, 10+ → compact summaries. One call replaces three.
- **Project File Sync** — every checkpoint writes `MEMORY.md`, `DECISIONS.md`, `TASKS.md` to your project dir automatically. Git-friendly, diffs cleanly, human-readable.
- **Notifications** — get a Telegram/Discord/Slack message the moment a background task completes. Works perfectly with Remote Control — push a task, walk away, get pinged.
- **Token Guard** — `token_watch()` monitors context budget live; auto-checkpoints at 80% so you never lose state mid-session.
- **Claude Dispatch** — queue headless `claude --print` sessions as background tasks. Claude works autonomously while you're away; output becomes a checkpoint automatically.
- **Pattern Learning** — after 5+ similar decisions, surfaces a suggested tag and standing rule via `pattern_suggestions()`. The agent learns your habits.
- **Cross-Agent Handoffs** — `cross_agent_handoff(project, target_agent="gpt")` exports context formatted for ChatGPT, Gemini, or any other agent.
- **Personal AI OS** — `north_star()` returns a unified <500 token briefing: who you are, how Claude works with you, active projects, current state. `remember_me()` and `remember_this()` teach the system your preferences and rules — persists forever.
- **Claude.ai Web Bridge** — remote HTTP server with bearer auth bridges your local DB to Claude.ai Custom Connectors. Same memory, same tools, both interfaces.

---

## MCP Tools

### Task Queue
| Tool | Description |
|------|-------------|
| `forge_push(command, project)` | Queue a shell command to run in the background |
| `forge_status(task_id)` | Check task status |
| `forge_result(task_id)` | Get full task output |
| `forge_cancel(task_id)` | Cancel a pending task |
| `forge_list(project, status)` | List tasks with optional filters |
| `push_and_checkpoint(command, project)` | Run task + auto-save findings as checkpoint |

### Memory & Continuity
| Tool | Description |
|------|-------------|
| `smart_resume(project, query)` | **[CORE]** 3-tier retrieval with auto depth selection |
| `checkpoint(project, ...)` | **[CORE]** Save structured project state |
| `handoff(project)` | **[CORE]** Generate compact resume briefing (<1k tokens) |
| `remember()` | Sub-400 token briefing of all active projects |
| `memory_search(query)` | FTS5 search across all checkpoints |
| `memory_timeline(checkpoint_id)` | Chronological slice around a checkpoint |
| `memory_get(ids)` | Full detail for specific checkpoint IDs |
| `sync_files(project, output_dir)` | Write MEMORY/DECISIONS/TASKS.md to project dir |

### Personal AI OS
| Tool | Description |
|------|-------------|
| `north_star()` | **[CORE]** Unified <500 token session briefing — who you are + how Claude works + active projects |
| `remember_me(key, value, category)` | Store a fact about you — bio, preferences, rules, technical style |
| `recall_me(query, category)` | Retrieve your facts via FTS search or category filter |
| `remember_this(key, value, category)` | Store a behavioral protocol or constraint for Claude |
| `recall_this(query)` | Retrieve Claude's protocols |
| `forget(key, memory_type)` | Delete a memory permanently |

### Automation & Extras
| Tool | Description |
|------|-------------|
| `auto_mode(enabled, project)` | **[CORE]** Activate all layers at once |
| `quickstart()` | **[CORE]** Get a live guide — call this first if lost |
| `observe_control(action, project)` | Toggle/check auto-observe |
| `token_watch(project, threshold)` | Monitor context budget with auto-checkpoint |
| `notify_configure(project, ...)` | Set up Telegram/Discord/Slack/webhook alerts |
| `notify_complete(task_id, summary)` | Fire a notification manually |
| `export_handoff(project)` | Export handoff as portable markdown |
| `import_web_handoff(md_string)` | Import a handoff pasted from web/Claude.ai |
| `cross_agent_handoff(project, target)` | Format context for GPT/Gemini/generic agents |
| `pattern_suggestions(project)` | Surface recurring decision patterns |

---

## CLI Cheat Sheet

```bash
# Setup
continuum setup                          # init + start daemon

# Task queue
continuum push 'pytest tests/'           # queue a task
continuum push 'npm run build' \
  --project myapp \
  --auto-checkpoint                      # auto-save findings on complete
continuum status                         # dashboard view
continuum list --project myapp           # filter by project
continuum result <task-id>               # full output
continuum cancel <task-id>

# Checkpoints  (checkpoint and cp are identical)
continuum checkpoint myapp \
  --task "Fixing auth bug" \
  --goal "JWT tokens expire correctly" \
  --finding "Token refresh broken on mobile" \
  --dead-end "Tried extending TTL — breaks tests" \
  --next "Patch refresh endpoint"

continuum resume myapp                   # load briefing for new session
continuum log myapp                      # list recent checkpoints

# Automation
continuum observe on myapp               # start auto-observe
continuum observe status
continuum sync myapp                     # write MEMORY/DECISIONS/TASKS.md

# Daemon
continuum daemon start
continuum daemon stop
continuum daemon status
```

---

## Benchmarking

Install dev dependencies (including `pytest-benchmark`):

```bash
python -m pip install -e ".[dev]" pytest-benchmark
```

Run benchmarks (saves results automatically, sorted by mean):

```bash
pytest tests/test_benchmark_mcp.py --benchmark-only --benchmark-autosave --benchmark-sort=mean
```

Compare against a saved baseline:

```bash
pytest tests/test_benchmark_mcp.py --benchmark-only --benchmark-compare
```

How to read results:

- **mean**: average latency across all benchmark rounds; useful for overall cost.
- **median**: middle latency; less sensitive to occasional outliers.
- **stddev**: variability; lower values generally indicate more stable timing.

---

## Architecture

One SQLite database at `~/.continuum/continuum.db`. A background daemon handles task execution, observation compression, and notification dispatch. The MCP server exposes all 40+ tools over stdio. A remote HTTP server bridges your local DB to Claude.ai Custom Connectors. No cloud, no accounts — everything stays on your machine.

```
continuum/
├── models.py         # Pydantic: Task, Checkpoint, Handoff, UserMemory, AgentMemory, Decision
├── db.py             # SQLite + FTS5 full-text search + user/agent memory tables
├── runner.py         # Task executor with auto-checkpoint + notifications
├── daemon.py         # Background process: task runner + observer thread
├── observer.py       # Passive capture + rule/Claude Haiku compression
├── handoff.py        # Compact briefing generator
├── sync_files.py     # Renders MEMORY.md / DECISIONS.md / TASKS.md
├── notify.py         # Telegram, Discord, Slack, webhook dispatcher
├── remote_server.py  # HTTP remote server for Claude.ai Custom Connectors
├── mcp_server.py     # FastMCP server — all 35+ tools
├── ui/server.py      # Local web dashboard (continuum ui)
└── cli.py            # Click CLI
```

---

## Roadmap

- [x] **Web UI** — local dashboard: `continuum ui` → opens at `http://localhost:8765`
- [ ] **Team sync** — optional encrypted checkpoint sharing across machines
- [ ] **Plugin hooks** — custom compression and notification handlers

---

## License

MIT © 2026 Zack Brooks

**Made for people who actually ship with AI agents.**

*If you're tired of re-explaining your project every session; star this repo. You'll use it every day.*
