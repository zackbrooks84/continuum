"""Continuum CLI — unified command surface for task queue + checkpoints."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console

# Ensure UTF-8 output and ANSI color support on Windows
if sys.platform == "win32":
    import io as _io
    import ctypes
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    try:  # Enable ANSI escape processing in CMD and PowerShell
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass
from rich.table import Table
from rich import box
from rich.panel import Panel
from rich.text import Text

console = Console(legacy_windows=False, highlight=False)


def _db():
    from .db import DB
    return DB()


# ===========================================================================
# Root
# ===========================================================================

@click.group()
@click.version_option()
def cli():
    """Continuum — async task queue + work continuity for AI agents.

    Push tasks to run offline. Checkpoint reasoning. Resume without context loss.

    Quick start:
      continuum setup              # create dirs, start daemon
      continuum push 'npm test'    # queue a task
      continuum resume myproject   # load briefing for new session
    """


# ===========================================================================
# Setup
# ===========================================================================

@cli.command()
def setup():
    """Create ~/.continuum dirs and start the daemon."""
    continuum_dir = Path.home() / ".continuum"
    (continuum_dir / "logs").mkdir(parents=True, exist_ok=True)
    console.print(f"[green]✓[/] Created {continuum_dir}")

    db = _db()
    console.print(f"[green]✓[/] Database ready: {db.db_path}")

    from .daemon import daemon_status, start_daemon
    s = daemon_status()
    if s["running"]:
        console.print(f"[yellow]⚡[/] Daemon already running (pid {s['pid']})")
    else:
        console.print("[green]✓[/] Starting daemon...")
        import subprocess, sys
        subprocess.Popen(
            [sys.executable, "-m", "continuum.daemon_entry"],
            stdout=open(continuum_dir / "daemon.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        console.print("[green]✓[/] Daemon started in background")

    console.print("\n[bold]Continuum is ready.[/] Try:")
    console.print("  continuum push 'echo hello'")
    console.print("  continuum status")


# ===========================================================================
# Task Queue
# ===========================================================================

@cli.command()
@click.argument("command")
@click.option("--name", "-n", default=None, help="Human-readable task name")
@click.option("--project", "-p", default=None, help="Project to checkpoint results into")
@click.option("--auto-checkpoint", is_flag=True, help="Auto-save checkpoint when task completes")
@click.option("--auto-observe", is_flag=True, help="Enable auto-observe for this task's project")
@click.option("--priority", default=5, show_default=True, help="1=highest, 10=lowest")
@click.option("--cwd", default=None, help="Working directory")
@click.option("--timeout", default=None, type=int, help="Max seconds to run")
@click.option("--tag", multiple=True, help="Tags (repeatable)")
@click.option("--run-now", is_flag=True, help="Run immediately instead of queuing")
def push(command, name, project, auto_checkpoint, auto_observe, priority, cwd, timeout, tag, run_now):
    """Queue a shell command to run offline.

    \b
    Examples:
      continuum push 'pytest tests/'
      continuum push 'python train.py' --project ml-run --auto-checkpoint
      continuum push 'npm run build' --priority 1 --run-now
    """
    from .models import Task, TaskStatus
    db = _db()

    task_name = name or command[:60]
    task = Task(
        name=task_name,
        command=command,
        cwd=cwd,
        project=project,
        auto_checkpoint=auto_checkpoint,
        priority=priority,
        timeout=timeout,
        tags=list(tag),
    )

    if run_now:
        from .runner import run_task, check_resources
        from datetime import datetime, timezone
        ok, reason = check_resources(task.min_free_ram_mb)
        if not ok:
            console.print(f"[red]✗[/] Insufficient resources: {reason}")
            sys.exit(1)
        console.print(f"[yellow]⚡[/] Running now: {command}")
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        db.push_task(task)
        db.update_task(task)
        from .runner import auto_checkpoint as _auto_cp
        result = run_task(task)
        task.status = TaskStatus.DONE if result.success else TaskStatus.FAILED
        task.finished_at = datetime.now(timezone.utc)
        db.update_task(task)
        db.save_result(result)
        _auto_cp(task, result)
        icon = "[green]✓[/]" if result.success else "[red]✗[/]"
        console.print(f"{icon} Exit {result.exit_code} in {result.duration_seconds:.1f}s")
        if result.stdout:
            console.print(result.stdout[-2000:])
    else:
        db.push_task(task)
        console.print(f"[green]✓[/] Queued task [bold]{task.id}[/]: {task_name}")
        if project and auto_checkpoint:
            console.print(f"  Will checkpoint to project [cyan]{project}[/] on completion")
        if auto_observe and project:
            import os
            os.environ["CONTINUUM_AUTO_OBSERVE"] = "1"
            os.environ["CONTINUUM_OBSERVE_PROJECT"] = project
            console.print(f"  Auto-observe [green]on[/] for project [cyan]{project}[/]")
        console.print("  Start daemon: [dim]continuum daemon start[/]")


@cli.group()
def daemon():
    """Manage the background task runner."""


@daemon.command("start")
@click.option("--workers", default=2, show_default=True)
def daemon_start(workers):
    """Start the background daemon."""
    from .daemon import start_daemon
    start_daemon(workers=workers)


@daemon.command("stop")
def daemon_stop():
    """Stop the background daemon."""
    from .daemon import stop_daemon
    stop_daemon()


@daemon.command("status")
def daemon_status_cmd():
    """Show daemon status."""
    from .daemon import daemon_status
    s = daemon_status()
    if s["running"]:
        console.print(f"[green]● Daemon running[/] (pid {s['pid']})")
    else:
        console.print("[dim]○ Daemon not running[/]")
        console.print("  Start with: continuum daemon start")


@cli.command("list")
@click.option("--status", "-s", default=None, help="Filter by status")
@click.option("--limit", default=20, show_default=True)
def list_tasks(status, limit):
    """List tasks in the queue."""
    db = _db()
    tasks = db.list_tasks(status=status, limit=limit)
    if not tasks:
        console.print("[dim]No tasks found.[/]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", width=8)
    table.add_column("Name")
    table.add_column("Status", width=10)
    table.add_column("Pri", width=3)
    table.add_column("Project", style="dim")
    table.add_column("Created", style="dim")

    status_colors = {
        "pending": "yellow", "running": "green", "done": "dim",
        "failed": "red", "cancelled": "dim", "waiting": "blue",
    }

    for t in tasks:
        color = status_colors.get(t.status.value, "white")
        table.add_row(
            t.id,
            t.name[:40],
            f"[{color}]{t.status.value}[/]",
            str(t.priority),
            t.project or "",
            t.created_at.strftime("%m/%d %H:%M"),
        )
    console.print(table)


@cli.command()
@click.argument("task_id")
def result(task_id):
    """Show the full output of a completed task."""
    db = _db()
    r = db.get_result(task_id)
    if not r:
        task = db.get_task(task_id)
        if not task:
            console.print(f"[red]Task {task_id} not found[/]")
            sys.exit(1)
        console.print(f"[yellow]Task {task_id} is {task.status.value} — no result yet[/]")
        return
    icon = "[green]✓[/]" if r.success else "[red]✗[/]"
    console.print(f"{icon} Exit {r.exit_code} | {r.duration_seconds:.1f}s | {r.finished_at.strftime('%Y-%m-%d %H:%M')}")
    if r.stdout:
        console.print(Panel(r.stdout[-3000:], title="stdout", border_style="dim"))
    if r.stderr:
        console.print(Panel(r.stderr[-1000:], title="stderr", border_style="red"))


@cli.command()
@click.argument("task_id")
def cancel(task_id):
    """Cancel a pending task."""
    db = _db()
    ok = db.cancel_task(task_id)
    if ok:
        console.print(f"[green]✓[/] Cancelled {task_id}")
    else:
        console.print(f"[red]✗[/] Could not cancel {task_id} (not pending?)")


# ===========================================================================
# Checkpoints
# ===========================================================================

@cli.command(name="checkpoint")
@click.argument("project")
@click.option("--task", "-t", required=True, help="What you're doing right now")
@click.option("--goal", "-g", required=True, help="Overall goal")
@click.option("--status", "-s", default="in-progress",
              type=click.Choice(["in-progress", "blocked", "complete", "abandoned"]))
@click.option("--context", "-c", default="", help="Background / constraints")
@click.option("--finding", multiple=True, help="Key findings (repeatable)")
@click.option("--dead-end", multiple=True, help="Dead ends to avoid (repeatable)")
@click.option("--next", multiple=True, help="Next steps (repeatable, ordered)")
@click.option("--question", multiple=True, help="Open questions (repeatable)")
@click.option("--file", "files", multiple=True, help="Files changed (repeatable)")
@click.option("--agent", default=None, help="Agent identifier")
def cp(project, task, goal, status, context, finding, dead_end, next, question, files, agent):
    """Save a checkpoint for a project.

    \b
    Example:
      continuum cp myproject \\
        --task "Refactoring auth module" \\
        --goal "Make auth testable without DB" \\
        --finding "JWT decode is tightly coupled to User model" \\
        --dead-end "Tried mock patching — breaks 12 tests" \\
        --next "Extract UserResolver interface" \\
        --next "Inject resolver in JWTMiddleware"
    """
    from .models import Checkpoint
    db = _db()
    checkpoint = Checkpoint(
        project=project,
        current_task=task,
        goal=goal,
        status=status,
        context=context,
        findings=list(finding),
        dead_ends=list(dead_end),
        next_steps=list(next),
        open_questions=list(question),
        files_changed=list(files),
        agent=agent,
    )
    db.save_checkpoint(checkpoint)
    console.print(f"[green]✓[/] Checkpoint saved: [cyan]{checkpoint.id[:8]}[/] for project [bold]{project}[/]")
    console.print(f"  Resume: [dim]continuum resume {project}[/]")


# Alias: `continuum cp` works the same as `continuum checkpoint`
cli.add_command(cp, name="cp")


@cli.command()
@click.argument("project")
@click.option("--checkpoint-id", default=None, help="Specific checkpoint (default: latest)")
@click.option("--compact", is_flag=True, help="Print executive summary only")
def resume(project, checkpoint_id, compact):
    """Load a handoff briefing to resume work on a project.

    Prints a <1k token context document. Paste it at the start of a new
    agent session to resume without re-explaining anything.

    \b
    Example:
      continuum resume myproject
      continuum resume myproject --compact
    """
    db = _db()
    if checkpoint_id:
        cp_obj = db.get_checkpoint(checkpoint_id)
        if not cp_obj:
            console.print(f"[red]Checkpoint {checkpoint_id} not found[/]")
            sys.exit(1)
    else:
        cp_obj = db.latest_checkpoint(project)
        if not cp_obj:
            console.print(f"[red]No checkpoints for project '{project}'[/]")
            sys.exit(1)

    from .handoff import generate_handoff
    h = generate_handoff(cp_obj)
    db.save_handoff(h)

    if compact:
        console.print(Panel(h.executive_summary, title=f"[bold]{project}[/] — resume briefing", border_style="cyan"))
        console.print(f"\n[bold]First action:[/] {h.immediate_action}")
        if h.watch_out_for:
            console.print("\n[bold]Watch out for:[/]")
            for w in h.watch_out_for:
                console.print(f"  [red]✗[/] {w}")
    else:
        console.print(Panel(
            h.full_context,
            title=f"[bold]{project}[/] — resume briefing (~{h.token_estimate} tokens)",
            border_style="cyan",
        ))


@cli.command()
@click.option("--project", "-p", default=None, help="Filter by project")
@click.option("--limit", default=15, show_default=True)
def log(project, limit):
    """List recent checkpoints."""
    db = _db()
    checkpoints = db.list_checkpoints(project=project, limit=limit)
    if not checkpoints:
        console.print("[dim]No checkpoints found.[/]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("ID", style="cyan", width=8)
    table.add_column("Project", style="bold")
    table.add_column("Status", width=12)
    table.add_column("Task")
    table.add_column("Time", style="dim")

    status_colors = {
        "in-progress": "yellow", "blocked": "red",
        "complete": "green", "abandoned": "dim",
    }
    for cp_obj in checkpoints:
        color = status_colors.get(cp_obj.status, "white")
        table.add_row(
            cp_obj.id[:8],
            cp_obj.project,
            f"[{color}]{cp_obj.status}[/]",
            cp_obj.current_task[:45],
            cp_obj.timestamp.strftime("%m/%d %H:%M"),
        )
    console.print(table)


@cli.command()
def status():
    """Show dashboard: daemon, queue, and active projects."""
    from .daemon import daemon_status as _ds
    db = _db()

    # Daemon
    ds = _ds()
    daemon_str = "[green]● running[/]" if ds["running"] else "[dim]○ stopped[/]"
    if ds.get("pid"):
        daemon_str += f" (pid {ds['pid']})"

    # Queue stats
    stats = db.task_stats()
    total = sum(stats.values())

    # Projects
    project_names = db.list_projects()

    console.print()
    console.print(f"[bold]Continuum[/]  daemon: {daemon_str}  |  tasks: {total}")
    console.print()

    # Queue table
    if total:
        qtable = Table(title="Queue", box=box.SIMPLE, show_header=True, header_style="bold dim")
        for s, n in sorted(stats.items()):
            colors = {"pending": "yellow", "running": "green", "done": "dim", "failed": "red"}
            c = colors.get(s, "white")
            qtable.add_column(s, style=c)
        qtable.add_row(*[str(stats.get(s, 0)) for s in sorted(stats.keys())])
        console.print(qtable)

    # Projects table
    if project_names:
        ptable = Table(title="Projects", box=box.SIMPLE, show_header=True, header_style="bold dim")
        ptable.add_column("Project", style="bold")
        ptable.add_column("Status", width=12)
        ptable.add_column("Latest task")
        ptable.add_column("Updated", style="dim")
        status_colors = {
            "in-progress": "yellow", "blocked": "red",
            "complete": "green", "abandoned": "dim",
        }
        for name in project_names:
            cp_obj = db.latest_checkpoint(name)
            if cp_obj:
                c = status_colors.get(cp_obj.status, "white")
                ptable.add_row(
                    name,
                    f"[{c}]{cp_obj.status}[/]",
                    cp_obj.current_task[:40],
                    cp_obj.timestamp.strftime("%m/%d %H:%M"),
                )
        console.print(ptable)

    if not total and not project_names:
        console.print("[dim]Nothing yet. Try: continuum push 'echo hello'[/]")
    console.print()


# ===========================================================================
# Observe
# ===========================================================================

@cli.group()
def observe():
    """Manage auto-observe (passive tool-call capture)."""


@observe.command("on")
@click.argument("project")
@click.option("--method", default="rule", type=click.Choice(["rule", "claude"]),
              show_default=True, help="Compression method")
@click.option("--compress-every", default=20, show_default=True,
              help="Compress after N observations")
def observe_on(project, method, compress_every):
    """Enable auto-observe for PROJECT.

    Sets CONTINUUM_AUTO_OBSERVE and CONTINUUM_OBSERVE_PROJECT in the current
    shell session. Observations are compressed into checkpoints by the daemon.

    \b
    Example:
      continuum observe on myapp --method claude
    """
    import os
    os.environ["CONTINUUM_AUTO_OBSERVE"] = "1"
    os.environ["CONTINUUM_OBSERVE_PROJECT"] = project
    os.environ["CONTINUUM_OBSERVE_METHOD"] = method
    console.print(f"[green]✓[/] Auto-observe [green]enabled[/] for project [cyan]{project}[/]")
    console.print(f"  Method: {method}  |  Compress every: {compress_every} observations")
    console.print(f"  Tip: export CONTINUUM_AUTO_OBSERVE=1 CONTINUUM_OBSERVE_PROJECT={project}")


@observe.command("off")
def observe_off():
    """Disable auto-observe."""
    import os
    os.environ.pop("CONTINUUM_AUTO_OBSERVE", None)
    os.environ.pop("CONTINUUM_OBSERVE_PROJECT", None)
    console.print("[dim]○ Auto-observe disabled[/]")


@observe.command("status")
@click.option("--project", "-p", default=None)
def observe_status_cmd(project):
    """Show observation counts and pending compression."""
    db = _db()
    all_stats = db.observation_stats()
    console.print()
    console.print(f"[bold]Observations[/]  total: {all_stats['total']}  pending: {all_stats['pending']}")
    if project:
        ps = db.observation_stats(project)
        console.print(f"  [cyan]{project}[/]  total: {ps['total']}  pending: {ps['pending']}")
    console.print()


# ===========================================================================
# Sync
# ===========================================================================

@cli.command()
@click.argument("project")
@click.argument("output_dir", required=False, default=None)
@click.option("--no-save", is_flag=True, help="Don't persist output_dir as project default")
def sync(project, output_dir, no_save):
    """Write MEMORY.md, DECISIONS.md, TASKS.md for PROJECT.

    \b
    Examples:
      continuum sync myapp ./docs
      continuum sync myapp          # uses previously configured dir
    """
    from pathlib import Path as _Path
    from .sync_files import sync_project_files
    from .models import ProjectConfig

    db = _db()
    cp = db.latest_checkpoint(project)
    if not cp:
        console.print(f"[red]✗[/] No checkpoints found for project '{project}'")
        sys.exit(1)

    config = db.get_project_config(project)

    if output_dir:
        out_path = _Path(output_dir).expanduser().resolve()
        if not no_save:
            new_config = ProjectConfig(
                project=project,
                output_dir=str(out_path),
                auto_sync=config.auto_sync if config else True,
            )
            db.save_project_config(new_config)
    elif config and config.output_dir:
        out_path = _Path(config.output_dir)
    else:
        out_path = _Path.home() / ".continuum" / "projects" / project

    history = db.list_checkpoints(project=project, limit=25)
    written = sync_project_files(cp, history, out_path)

    console.print(f"[green]✓[/] Synced [cyan]{project}[/] → {out_path}")
    for filename, path in written.items():
        console.print(f"  [dim]{filename}[/]  {path}")
    if not no_save and output_dir:
        console.print(f"  Saved as default for future auto-sync")


# ===========================================================================
# Web UI
# ===========================================================================

@cli.command()
@click.option("--port", default=8765, show_default=True, help="Port to serve on")
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind to")
@click.option("--no-browser", is_flag=True, help="Don't open browser automatically")
def ui(port, host, no_browser):
    """Start the local web UI (dashboard, tasks, timeline).

    \b
    Opens http://localhost:8765 in your browser automatically.
    Requires: pip install continuum[ui]
    """
    try:
        from .ui.server import serve
    except ImportError:
        console.print("[red]UI dependencies not installed.[/]")
        console.print("Run: [bold]pip install continuum[ui][/]")
        raise SystemExit(1)
    console.print(f"[green]✓[/] Starting Continuum UI on [cyan]http://{host}:{port}[/]")
    console.print("  Press [bold]Ctrl+C[/] to stop.")
    serve(host=host, port=port, open_browser=not no_browser)


# ===========================================================================
# Memory — user identity and agent protocol management
# ===========================================================================

@cli.group()
def memory():
    """Manage persistent user and agent memory."""
    pass


@memory.command("remember")
@click.argument("value")
@click.option("--key", "-k", default=None, help="Unique key slug (auto-generated from value if omitted)")
@click.option("--category", "-c", default="preferences",
              type=click.Choice(["bio", "preferences", "technical", "research", "rules", "relationship"]),
              help="Memory category")
@click.option("--type", "mem_type", default="user",
              type=click.Choice(["user", "agent"]),
              help="user = about you, agent = about Claude's behavior")
@click.option("--rationale", "-r", default=None, help="Why this rule/protocol exists (agent memories)")
def memory_remember(value, key, category, mem_type, rationale):
    """Store a memory.

    \b
    Examples:
      continuum memory remember "Zack, builds AI tools" --category bio
      continuum memory remember "prefer TypeScript" --key ts_pref --category technical
      continuum memory remember "always explain the why" --type agent --category protocol \\
          --rationale "Zack is non-technical but wants to understand decisions"
    """
    from .db import DB
    from .models import UserMemory, AgentMemory, MemoryCategory, AgentMemoryCategory
    db = DB()
    if not key:
        # Auto-generate key from first 30 chars of value, slugified
        key = value[:30].lower().replace(" ", "_").replace(",", "").strip("_")

    if mem_type == "user":
        try:
            cat = MemoryCategory(category)
        except ValueError:
            cat = MemoryCategory.PREFERENCES
        mem = UserMemory(key=key, value=value, category=cat)
        db.remember_user(mem)
        console.print(f"[green]✓[/green] Stored user memory: [bold]{key}[/bold] [{cat.value}]")
    else:
        try:
            cat = AgentMemoryCategory(category)
        except ValueError:
            cat = AgentMemoryCategory.PROTOCOL
        mem = AgentMemory(key=key, value=value, category=cat, rationale=rationale)
        db.remember_agent(mem)
        console.print(f"[green]✓[/green] Stored agent memory: [bold]{key}[/bold] [{cat.value}]")
        if rationale:
            console.print(f"  Rationale: {rationale}")


@memory.command("recall")
@click.argument("query", required=False)
@click.option("--category", "-c", default=None, help="Filter by category")
@click.option("--type", "mem_type", default="user",
              type=click.Choice(["user", "agent"]),
              help="user or agent memory")
@click.option("--limit", "-n", default=20, help="Max results")
def memory_recall(query, category, mem_type, limit):
    """Search memories by keyword or list by category.

    \b
    Examples:
      continuum memory recall "TypeScript"
      continuum memory recall --category rules
      continuum memory recall --type agent --category protocol
    """
    from .db import DB
    db = DB()
    if mem_type == "user":
        results = db.recall_user(query=query, category=category, limit=limit)
        title = "User Memory"
    else:
        results = db.recall_agent(query=query, category=category, limit=limit)
        title = "Agent Memory"

    if not results:
        console.print(f"[dim]No {title.lower()} found.[/dim]")
        return

    table = Table(title=title, box=box.ROUNDED)
    table.add_column("Key", style="bold cyan", no_wrap=True)
    table.add_column("Value")
    table.add_column("Category", style="dim")
    if mem_type == "agent":
        table.add_column("Rationale", style="dim italic")

    for r in results:
        if mem_type == "agent":
            table.add_row(r["key"], r["value"], r["category"], r.get("rationale") or "")
        else:
            table.add_row(r["key"], r["value"], r["category"])

    console.print(table)


@memory.command("forget")
@click.argument("key")
@click.option("--type", "mem_type", default="user",
              type=click.Choice(["user", "agent"]))
def memory_forget(key, mem_type):
    """Delete a memory by key.

    \b
    Example:
      continuum memory forget ts_pref
      continuum memory forget explain_decisions --type agent
    """
    from .db import DB
    db = DB()
    if mem_type == "user":
        deleted = db.delete_user_memory(key)
    else:
        deleted = db.delete_agent_memory(key)

    if deleted:
        console.print(f"[green]✓[/green] Deleted {mem_type} memory: [bold]{key}[/bold]")
    else:
        console.print(f"[yellow]⚠[/yellow] Key not found: [bold]{key}[/bold]")


@memory.command("list")
@click.option("--type", "mem_type", default="user",
              type=click.Choice(["user", "agent"]))
@click.option("--category", "-c", default=None, help="Filter by category")
def memory_list(mem_type, category):
    """List all stored memories.

    \b
    Examples:
      continuum memory list
      continuum memory list --type agent
      continuum memory list --category rules
    """
    from .db import DB
    db = DB()
    if mem_type == "user":
        items = db.all_user_memory(category=category)
        title = "User Memory"
    else:
        items = db.all_agent_memory(category=category)
        title = "Agent Memory"

    if not items:
        console.print(f"[dim]No {title.lower()} stored yet.[/dim]")
        console.print(
            "\n[dim]Add some: continuum memory remember \"your info\" --category bio[/dim]"
        )
        return

    table = Table(title=f"{title} ({len(items)} entries)", box=box.ROUNDED)
    table.add_column("Key", style="bold cyan", no_wrap=True)
    table.add_column("Value", max_width=60)
    table.add_column("Category", style="dim")
    table.add_column("Source", style="dim")

    for m in items:
        table.add_row(m.key, m.value, m.category, m.source)

    console.print(table)


# ===========================================================================
# Remote — HTTP MCP server for Claude.ai Custom Connectors
# ===========================================================================

@cli.group()
def remote():
    """Manage the remote HTTP MCP server for Claude.ai Custom Connectors."""
    pass


@remote.command("start")
@click.option("--port", "-p", default=8766, help="Port to bind (default: 8766)")
@click.option("--host", "-h", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
@click.option("--token", "-t", default=None, help="Bearer token (auto-generated if omitted)")
def remote_start(port, host, token):
    """Start the remote MCP server (foreground).

    \b
    Then expose it publicly:
      ngrok http <port>

    Then add to Claude.ai → Settings → Custom Connectors:
      URL:   https://xxxx.ngrok.io/mcp
      Auth:  Bearer <token shown at startup>
    """
    try:
        from .remote_server import run_remote
    except ImportError:
        console.print("[red]Remote server requires anyio:[/red] pip install 'continuum[remote]'")
        raise SystemExit(1)
    run_remote(host=host, port=port, token=token)


@remote.command("status")
def remote_status_cmd():
    """Check if the remote server is running."""
    try:
        from .remote_server import remote_status, REMOTE_TOKEN_FILE
        st = remote_status()
        if st["running"]:
            console.print(f"[green]● Remote server running[/green]  PID: {st['pid']}")
        else:
            console.print("[dim]○ Remote server not running[/dim]")
        if st["token_saved"]:
            console.print(f"  Token saved at: {REMOTE_TOKEN_FILE}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")


@remote.command("token")
@click.option("--generate", is_flag=True, help="Generate a new token")
def remote_token_cmd(generate):
    """Show or regenerate the remote server bearer token."""
    try:
        from .remote_server import generate_token, load_or_create_token, REMOTE_TOKEN_FILE
        if generate:
            from pathlib import Path
            token = generate_token()
            REMOTE_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            REMOTE_TOKEN_FILE.write_text(token)
            console.print(f"[green]✓[/green] New token generated and saved.")
            console.print(f"  Token: [bold]{token}[/bold]")
        else:
            token = load_or_create_token()
            console.print(f"  Token: [bold]{token}[/bold]")
            console.print(f"  File:  {REMOTE_TOKEN_FILE}")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")


# ===========================================================================
# North Star — session start briefing (also used by SessionStart hook)
# ===========================================================================

@cli.command("north-star")
@click.option("--hook", "hook_mode", is_flag=True, hidden=True,
              help="Output JSON for Claude Code SessionStart hook injection")
@click.option("--project", "-p", default=None, help="Include latest checkpoint for this project")
def north_star_cmd(hook_mode, project):
    """Print your persistent context briefing — who you are, how Claude works with you.

    \b
    Used automatically at session start via the SessionStart hook.
    Run manually to verify your stored memories are correct.

    Examples:
      continuum north-star
      continuum north-star --project myapp
    """
    from .db import DB
    db = DB()

    lines = ["## Continuum North Star — Session Context\n"]

    # User identity
    user_mems = db.all_user_memory()
    if user_mems:
        lines.append("### Who I Am")
        for m in user_mems:
            lines.append(f"- **{m['key']}** ({m['category']}): {m['value']}")
        lines.append("")

    # Agent protocol
    agent_mems = db.all_agent_memory()
    if agent_mems:
        lines.append("### How We Work Together")
        for m in agent_mems:
            lines.append(f"- **{m['key']}** ({m['category']}): {m['value']}")
        lines.append("")

    # Latest project checkpoint
    if project:
        cp = db.latest_checkpoint(project)
        if cp:
            lines.append(f"### Active Project: {project}")
            lines.append(f"- **Goal**: {cp.goal or 'not set'}")
            lines.append(f"- **Current task**: {cp.current_task or 'not set'}")
            lines.append(f"- **Status**: {cp.status or 'unknown'}")
            if cp.next_steps:
                lines.append(f"- **Next steps**: {', '.join(cp.next_steps[:3])}")
            lines.append("")

    # Recent active projects summary
    summaries = db.recent_project_summaries(days=14)
    if summaries and not project:
        lines.append("### Active Projects")
        for s in summaries[:5]:
            lines.append(f"- **{s['project']}**: {s['current_task'] or 'no task'} ({s['status'] or '?'})")
        lines.append("")

    if len(lines) == 1:
        lines.append("_No memories stored yet. Use `remember_me()` or `continuum memory remember` to add context._")

    briefing = "\n".join(lines)

    if hook_mode:
        import json as _json
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": briefing
            }
        }
        click.echo(_json.dumps(payload))
    else:
        console.print(briefing)


# ===========================================================================
# Memory import — bulk load from JSON or markdown
# ===========================================================================

@memory.command("import")
@click.argument("file", type=click.Path(exists=True))
@click.option("--type", "mem_type", default="user",
              type=click.Choice(["user", "agent"]),
              help="Import as user or agent memories")
@click.option("--dry-run", is_flag=True, help="Preview without storing")
def memory_import(file, mem_type, dry_run):
    """Bulk import memories from a JSON or Markdown file.

    \b
    JSON format (array of objects):
      [{"key": "name", "value": "Jane", "category": "bio"}, ...]

    Markdown format (each line: - key: value  or  key | value | category):
      - name: Jane Smith
      - coding_style: minimal dependencies | technical

    Examples:
      continuum memory import memories.json
      continuum memory import notes.md --type agent --dry-run
    """
    import json as _json
    from .db import DB
    from .models import UserMemory, AgentMemory, MemoryCategory, AgentMemoryCategory
    import uuid as _uuid
    from datetime import datetime, timezone

    db = DB()
    path = Path(file)
    text = path.read_text(encoding="utf-8")
    records = []

    if path.suffix == ".json":
        raw = _json.loads(text)
        if isinstance(raw, list):
            records = raw
        elif isinstance(raw, dict):
            records = [{"key": k, "value": v} for k, v in raw.items()]
    else:
        # Markdown: parse lines starting with - or bullet
        for line in text.splitlines():
            line = line.strip().lstrip("-").strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2:
                records.append({"key": parts[0], "value": parts[1],
                                 "category": parts[2] if len(parts) > 2 else "preferences"})
            elif ":" in line:
                key, _, val = line.partition(":")
                records.append({"key": key.strip(), "value": val.strip()})

    if not records:
        console.print("[yellow]No records found in file.[/]")
        return

    console.print(f"[bold]Found {len(records)} record(s)[/]  (type={mem_type})"
                  + (" [dry-run]" if dry_run else ""))

    stored = 0
    for r in records:
        key = r.get("key") or r.get("k", "").strip()
        value = r.get("value") or r.get("v", "").strip()
        category = r.get("category", "preferences")

        if not key or not value:
            continue

        if not dry_run:
            if mem_type == "user":
                try:
                    cat = MemoryCategory(category)
                except ValueError:
                    cat = MemoryCategory.preferences
                mem = UserMemory(
                    id=str(_uuid.uuid4()),
                    key=key, value=value, category=cat,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                db.remember_user(mem)
            else:
                try:
                    cat = AgentMemoryCategory(category)
                except ValueError:
                    cat = AgentMemoryCategory.protocol
                mem = AgentMemory(
                    id=str(_uuid.uuid4()),
                    key=key, value=value, category=cat,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                db.remember_agent(mem)
        console.print(f"  [green]✓[/] {key}: {value[:60]}{'...' if len(value) > 60 else ''}")
        stored += 1

    if not dry_run:
        console.print(f"\n[green]Imported {stored} memories.[/]")
    else:
        console.print(f"\n[dim]Dry run — {stored} would be imported. Remove --dry-run to store.[/]")


if __name__ == "__main__":
    cli()
