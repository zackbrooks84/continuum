# Continuum

**Async task queue + persistent work continuity for AI agents.**

Push tasks to run offline. Checkpoint your reasoning. Resume any session without re-explaining context.

Built for Claude Code + MCP. No cloud. No accounts. Just a local daemon and a SQLite file.

---

## The Problem

AI agents have two fatal flaws in long-running work:

1. **Session amnesia** — every new conversation starts cold. The agent re-derives what you already figured out, repeats dead ends, loses hard-won context.
2. **No detached execution** — closing the window kills the work. You can't push a 2-hour training run and go to sleep.

Continuum fixes both.

---

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│  Your Claude Code session                                │
│                                                         │
│  forge_push("python train.py", project="ml-v2",        │
│             auto_checkpoint=True)                       │
│                                                         │
│  checkpoint("ml-v2", task="Training run queued",        │
│    goal="Beat 94% accuracy on MNIST",                   │
│    findings=["lr=0.001 overfits after epoch 5"],        │
│    dead_ends=["SGD — too slow to converge"],            │
│    next_steps=["Review val_loss curve", "Try AdamW"])   │
│                                                         │
│  → Close the session. Go to sleep.                      │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│  Continuum daemon (running in background)               │
│                                                         │
│  ● Picks up task from queue                             │
│  ● Runs python train.py                                 │
│  ● Saves auto-checkpoint with stdout findings           │
│  ● Logs everything to ~/.continuum/logs/                │
└─────────────────────────────────────────────────────────┘
                         ↓
┌─────────────────────────────────────────────────────────┐
│  New Claude Code session (next day)                     │
│                                                         │
│  handoff("ml-v2")                                       │
│                                                         │
│  → ~600 token briefing:                                 │
│    • Goal: Beat 94% accuracy on MNIST                   │
│    • Status: Training complete                          │
│    • Findings: val_loss plateaued at epoch 8            │
│    • Dead ends: SGD (too slow), lr=0.001 (overfits)    │
│    • First action: Review val_loss curve, try AdamW     │
└─────────────────────────────────────────────────────────┘
```

---

## Install

```bash
pip install git+https://github.com/zackbrooks84/continuum
```

Or with uv:

```bash
uv add git+https://github.com/zackbrooks84/continuum
```

One-command setup:

```bash
continuum setup
```

---

## MCP Setup (Claude Code)

Add to your `.mcp.json`:

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

Or with uv (recommended):

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

---

## MCP Tools

### Task Queue

| Tool | Description |
|------|-------------|
| `forge_push` | Queue a shell command to run offline |
| `forge_status` | Queue stats or specific task status |
| `forge_list` | List tasks by status |
| `forge_result` | Full stdout/stderr of a completed task |
| `forge_cancel` | Cancel a pending task |
| `forge_run_now` | Run a task immediately (no daemon needed) |

### Checkpoints

| Tool | Description |
|------|-------------|
| `checkpoint` | Save work state: goal, findings, decisions, next_steps, dead_ends |
| `handoff` | Generate compact resume briefing (<1k tokens) |
| `status` | Latest checkpoint for a project |
| `history` | List recent checkpoints |
| `projects` | All active projects with status |

### Hybrid

| Tool | Description |
|------|-------------|
| `push_and_checkpoint` | Queue a task AND save a pre-run checkpoint in one call |

### Memory Search

| Tool | Description |
|------|-------------|
| `memory_search(query)` | Full-text search over all checkpoints — returns compact ID list + summaries |
| `memory_timeline(checkpoint_id)` | Chronological context slice around a checkpoint |
| `memory_get(ids)` | Full details for specific checkpoint IDs |
| `remember()` | **Auto-inject** — compact briefing of all active projects for session start |

---

## CLI

```bash
# Setup
continuum setup                          # create dirs, start daemon

# Task queue
continuum push 'pytest tests/'           # queue a task
continuum push 'python train.py' \
  --project ml-run --auto-checkpoint     # queue + auto-checkpoint on completion
continuum push 'npm run build' --run-now # run immediately
continuum list                           # list all tasks
continuum list --status pending          # filter by status
continuum result <task-id>               # show full output
continuum cancel <task-id>               # cancel pending task

# Daemon
continuum daemon start                   # start background runner
continuum daemon stop                    # stop it
continuum daemon status                  # check if running

# Checkpoints
continuum cp myproject \
  --task "Refactoring auth" \
  --goal "Make auth testable" \
  --finding "JWT tightly coupled to User model" \
  --dead-end "Mock patching breaks 12 tests" \
  --next "Extract UserResolver interface"

continuum resume myproject               # full briefing
continuum resume myproject --compact     # executive summary only
continuum log                            # recent checkpoints
continuum log --project myproject        # filter by project

# Dashboard
continuum status                         # daemon + queue + projects overview
```

---

## Workflow Examples

### Push work, close session, resume tomorrow

```python
# In Claude Code — before you go
push_and_checkpoint(
    name="Run full test suite",
    command="pytest tests/ -v --tb=short",
    project="myapp",
    goal="Identify failing tests before the v2 release",
    context="We just merged the auth refactor PR #142",
    next_steps=["Review failures", "Fix auth_test.py first — it's blocking CI"],
    findings=["PR #142 changed the User model — auth tests likely need updating"],
)
# → Task queued. Checkpoint saved. Close the window.
```

```python
# Next session — instant context
handoff("myapp")
# → Executive summary: 3 sentences.
# → What we know, what not to do, first action.
# → ~600 tokens. Costs almost nothing to load.
```

### Research task with auto-checkpoint

```python
forge_push(
    name="Scrape competitor pricing",
    command="python scrape.py --output pricing.json",
    project="competitive-analysis",
    auto_checkpoint=True,   # stdout becomes findings automatically
    priority=3,
)
# → Runs overnight. Findings from stdout saved as checkpoint.
# → Open new session: handoff("competitive-analysis") → instant briefing.
```

### Chain dependent tasks

```python
build = forge_push("npm run build", name="build")
test  = forge_push("npm test", name="test", depends_on=build["task_id"])
deploy = forge_push("./deploy.sh", name="deploy", depends_on=test["task_id"])
# → build → test → deploy, automatic sequencing
```

### Auto-inject context on every new session

`remember()` is the killer feature. Call it once at the start of any session and get a sub-400 token briefing of every project you've touched in the last two weeks — no re-explaining, no re-deriving context.

```python
# First thing in every new session:
remember()

# → Active Projects (3 in last 14d)
#
# **ml-v2** ◉ `in-progress`
#   Goal: Beat 94% accuracy on MNIST
#   Now: Reviewing val_loss curve
#   Next: Try AdamW with lr=3e-4
#   ID: a1b2c3d4  Updated: 2026-03-23
#
# **myapp** ✓ `complete`
#   Goal: Auth refactor before v2 release
#   Now: PR #142 merged
#   ID: e5f6g7h8  Updated: 2026-03-22
#
# ~280 tokens. Costs almost nothing. You never start cold again.
```

Wire it to fire automatically in Claude Code by calling it as the first tool in your session prompt, or configure it in your system prompt template.

### Three-tier memory retrieval

Mimics the exact pattern everyone is hyping for long-context agents — **search first, expand only what you need**:

```python
# 1. Search — cheap, ~50 tokens
results = memory_search("auth JWT overfitting")
# → [{id: "a1b2...", project: "myapp", task: "Extract UserResolver", ...}]

# 2. Timeline — understand how thinking evolved, still compact
memory_timeline("a1b2c3d4", window=3)
# → 7 checkpoints in chronological order, just IDs + task names

# 3. Get — full details only for what you actually need
memory_get(ids=["a1b2c3d4", "e5f6g7h8"])
# → complete checkpoint state: findings, dead ends, decisions, next steps
```

This is 95% token savings vs. loading full context blindly. The agent retrieves exactly what it needs, when it needs it.

---

## Data

Everything lives in `~/.continuum/`:

```
~/.continuum/
├── continuum.db      # SQLite: tasks, results, checkpoints, handoffs
├── daemon.pid        # daemon process ID
├── daemon.log        # daemon stdout
└── logs/
    └── <task-id>.log # full output for each task
```

Override the DB path:
```bash
export CONTINUUM_DB=/path/to/custom.db
```

---

## Features

- **One DB** — tasks + checkpoints in a single SQLite file. Simple to backup, inspect, migrate.
- **Auto-checkpoint** — set `auto_checkpoint=True` when pushing a task and stdout becomes structured findings automatically on completion.
- **<1k token handoffs** — the resume briefing is designed to be loaded cold. Typically 400-800 tokens. Not a log dump.
- **Three-tier memory retrieval** — `memory_search` → `memory_timeline` → `memory_get`. Search first, expand only what you need. 95% token savings.
- **Auto-inject on session start** — `remember()` gives you a sub-400 token briefing of all active projects. Never start cold again.
- **Dead end propagation** — things you explicitly mark as dead ends survive across sessions and show up in every future handoff. Agents don't repeat past mistakes.
- **Resource-aware** — daemon checks available RAM before running each task. Skip if low, retry in 10s.
- **Dependency chains** — `depends_on=<task_id>` for sequencing builds, tests, deploys.
- **No cloud** — your tasks and checkpoints never leave your machine.

---

## Architecture

```
continuum/
├── models.py       # Task, TaskResult, Checkpoint, Decision, Handoff (Pydantic)
├── db.py           # Single SQLite store + FTS5 memory search
├── runner.py       # Task executor + auto-checkpoint integration
├── daemon.py       # Background process manager
├── handoff.py      # Compact briefing generator
├── mcp_server.py   # FastMCP server — all tools in one place
└── cli.py          # Click CLI
```

Merges and supersedes [threadline](https://github.com/zackbrooks84/threadline) (checkpoints) and [forge](https://github.com/zackbrooks84/forge) (task queue) into a single cohesive package.

---

## License

MIT — do whatever you want with it.

---

*Built because agents shouldn't have to start over every session.*
