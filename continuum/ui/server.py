"""Continuum local web UI — FastAPI server serving the dashboard, task list, and checkpoint timeline."""
from __future__ import annotations

import json
import webbrowser
from datetime import datetime, timezone
from typing import Optional

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
    _UI_AVAILABLE = True
except ImportError:
    _UI_AVAILABLE = False

from ..db import DB
from ..daemon import daemon_status

app = FastAPI(title="Continuum UI", docs_url=None, redoc_url=None)
_db: Optional[DB] = None


def _get_db() -> DB:
    global _db
    if _db is None:
        _db = DB()
    return _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

STATUS_COLORS = {
    "pending":         ("bg-amber-500/20 text-amber-300 border border-amber-500/30",   "Pending"),
    "pending_confirm": ("bg-blue-500/20 text-blue-300 border border-blue-500/30",      "Awaiting Approval"),
    "waiting":         ("bg-slate-500/20 text-slate-300 border border-slate-500/30",   "Waiting"),
    "running":         ("bg-violet-500/20 text-violet-300 border border-violet-500/30 animate-pulse", "Running"),
    "done":            ("bg-green-500/20 text-green-300 border border-green-500/30",   "Done"),
    "failed":          ("bg-red-500/20 text-red-300 border border-red-500/30",         "Failed"),
    "cancelled":       ("bg-slate-500/20 text-slate-400 border border-slate-500/30",   "Cancelled"),
    "in-progress":     ("bg-violet-500/20 text-violet-300 border border-violet-500/30","In Progress"),
    "blocked":         ("bg-red-500/20 text-red-300 border border-red-500/30",         "Blocked"),
    "complete":        ("bg-green-500/20 text-green-300 border border-green-500/30",   "Complete"),
    "abandoned":       ("bg-slate-500/20 text-slate-400 border border-slate-500/30",   "Abandoned"),
}


def _badge(status: str) -> str:
    cls, label = STATUS_COLORS.get(status, ("bg-slate-500/20 text-slate-300", status))
    return f'<span class="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium {cls}">{label}</span>'


def _rel_time(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    s = int(delta.total_seconds())
    if s < 60:    return f"{s}s ago"
    if s < 3600:  return f"{s//60}m ago"
    if s < 86400: return f"{s//3600}h ago"
    return f"{s//86400}d ago"


def _base(title: str, content: str, active: str = "") -> str:
    nav = [
        ("Dashboard",  "/",         "dashboard", '<path stroke-linecap="round" stroke-linejoin="round" d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"/>'),
        ("Tasks",      "/tasks",    "tasks",     '<path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/>'),
        ("Projects",   "/projects", "projects",  '<path stroke-linecap="round" stroke-linejoin="round" d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z"/>'),
        ("Timeline",   "/timeline", "timeline",  '<path stroke-linecap="round" stroke-linejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>'),
    ]
    nav_html = ""
    for label, href, key, icon_path in nav:
        is_active = active == key
        link_cls = (
            "flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors "
            + ("bg-violet-600 text-white" if is_active else "text-slate-400 hover:text-white hover:bg-slate-700/60")
        )
        nav_html += f'''
        <a href="{href}" class="{link_cls}">
          <svg class="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">{icon_path}</svg>
          {label}
        </a>'''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Continuum</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {{
      theme: {{
        extend: {{
          colors: {{
            slate: {{
              850: '#172033',
            }}
          }}
        }}
      }}
    }}
  </script>
  <style>
    body {{ background-color: #0f172a; color: #f1f5f9; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; }}
    .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 0.75rem; }}
    .truncate-cmd {{ max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    ::-webkit-scrollbar-track {{ background: #1e293b; }}
    ::-webkit-scrollbar-thumb {{ background: #475569; border-radius: 3px; }}
    .timeline-line {{ position: relative; }}
    .timeline-line::before {{ content: ''; position: absolute; left: 7px; top: 24px; bottom: 0; width: 2px; background: #334155; }}
  </style>
</head>
<body class="min-h-screen flex">
  <!-- Sidebar -->
  <aside class="w-56 flex-shrink-0 flex flex-col" style="background:#1e293b; border-right:1px solid #334155;">
    <div class="p-4" style="border-bottom:1px solid #334155;">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm" style="background: linear-gradient(135deg, #7c3aed, #4f46e5);">C</div>
        <div>
          <div class="font-semibold text-white text-sm">Continuum</div>
          <div class="text-xs text-slate-500">v0.1.0</div>
        </div>
      </div>
    </div>
    <nav class="flex-1 p-3 space-y-0.5">
      {nav_html}
    </nav>
    <div class="p-3" style="border-top:1px solid #334155;">
      <div id="daemon-badge" class="flex items-center gap-1.5 text-xs text-slate-500">
        <span class="w-2 h-2 rounded-full bg-slate-600"></span>
        <span>Checking...</span>
      </div>
    </div>
  </aside>

  <!-- Main content -->
  <main class="flex-1 overflow-auto">
    {content}
  </main>

  <script>
    async function checkDaemon() {{
      try {{
        const r = await fetch('/api/status');
        const d = await r.json();
        const el = document.getElementById('daemon-badge');
        if (d.running) {{
          el.innerHTML = '<span class="w-2 h-2 rounded-full bg-green-400 flex-shrink-0"></span><span class="text-green-400">Daemon running</span>';
        }} else {{
          el.innerHTML = '<span class="w-2 h-2 rounded-full bg-red-400 flex-shrink-0"></span><span class="text-red-400">Daemon stopped</span>';
        }}
      }} catch(e) {{
        document.getElementById('daemon-badge').innerHTML = '<span class="w-2 h-2 rounded-full bg-slate-600 flex-shrink-0"></span><span>Offline</span>';
      }}
    }}
    checkDaemon();
    setInterval(checkDaemon, 15000);
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    db = _get_db()
    stats = db.task_stats()
    pending  = stats.get("pending", 0) + stats.get("pending_confirm", 0)
    running  = stats.get("running", 0)
    done     = stats.get("done", 0)
    failed   = stats.get("failed", 0)

    recent_tasks = db.list_tasks(limit=8)
    projects_data = db.recent_project_summaries(days=90)

    def stat_card(label, value, color, sub=""):
        return f'''
        <div class="card p-5">
          <div class="text-xs font-medium text-slate-400 uppercase tracking-wider mb-1">{label}</div>
          <div class="text-3xl font-bold {color}">{value}</div>
          {f'<div class="text-xs text-slate-500 mt-1">{sub}</div>' if sub else ''}
        </div>'''

    task_rows = ""
    for t in recent_tasks:
        cmd = t.command[:60] + ("…" if len(t.command) > 60 else "")
        proj = f'<span class="text-violet-400">{t.project}</span>' if t.project else '<span class="text-slate-600">—</span>'
        task_rows += f'''
        <tr class="border-b border-slate-700/50 hover:bg-slate-700/20 transition-colors">
          <td class="py-3 px-4"><code class="text-xs text-slate-300 font-mono">{t.id}</code></td>
          <td class="py-3 px-4"><span class="text-sm text-slate-200 truncate-cmd" title="{t.command}">{cmd}</span></td>
          <td class="py-3 px-4">{proj}</td>
          <td class="py-3 px-4">{_badge(t.status.value)}</td>
          <td class="py-3 px-4 text-xs text-slate-500">{_rel_time(t.created_at)}</td>
        </tr>'''

    project_cards = ""
    for p in (projects_data or [])[:6]:
        project_cards += f'''
        <a href="/projects/{p['project']}" class="card p-4 block hover:border-violet-500/50 transition-colors">
          <div class="flex items-start justify-between mb-2">
            <div class="font-medium text-white">{p['project']}</div>
            {_badge(p.get('status', 'in-progress'))}
          </div>
          <div class="text-xs text-slate-400 line-clamp-2">{p.get('current_task', '')}</div>
          <div class="text-xs text-slate-600 mt-2">{_rel_time(datetime.fromisoformat(p['latest']) if p.get('latest') else None)}</div>
        </a>'''

    content = f'''
    <div class="p-6 max-w-6xl mx-auto">
      <div class="mb-6">
        <h1 class="text-2xl font-bold text-white">Dashboard</h1>
        <p class="text-slate-400 text-sm mt-1">Overview of tasks, projects, and agent activity</p>
      </div>

      <!-- Stats -->
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {stat_card("Pending", pending, "text-amber-400")}
        {stat_card("Running", running, "text-violet-400")}
        {stat_card("Completed", done, "text-green-400")}
        {stat_card("Failed", failed, "text-red-400")}
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <!-- Recent tasks -->
        <div class="lg:col-span-2">
          <div class="flex items-center justify-between mb-3">
            <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Recent Tasks</h2>
            <a href="/tasks" class="text-xs text-violet-400 hover:text-violet-300">View all →</a>
          </div>
          <div class="card overflow-hidden">
            <table class="w-full text-sm">
              <thead>
                <tr class="border-b border-slate-700" style="background:#162032;">
                  <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">ID</th>
                  <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">Command</th>
                  <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">Project</th>
                  <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">Status</th>
                  <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">When</th>
                </tr>
              </thead>
              <tbody>
                {task_rows or '<tr><td colspan="5" class="py-8 text-center text-slate-500 text-sm">No tasks yet — use forge_push() or continuum push</td></tr>'}
              </tbody>
            </table>
          </div>
        </div>

        <!-- Active projects -->
        <div>
          <div class="flex items-center justify-between mb-3">
            <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider">Active Projects</h2>
            <a href="/projects" class="text-xs text-violet-400 hover:text-violet-300">View all →</a>
          </div>
          <div class="space-y-3">
            {project_cards or '<div class="card p-6 text-center text-slate-500 text-sm">No projects yet — use checkpoint() to start tracking</div>'}
          </div>
        </div>
      </div>
    </div>'''

    return HTMLResponse(_base("Dashboard", content, "dashboard"))


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(project: str = "", status: str = ""):
    db = _get_db()
    all_tasks = db.list_tasks(status=status or None, limit=100)
    if project:
        all_tasks = [t for t in all_tasks if t.project == project]

    # Collect unique projects for filter dropdown
    all_projects = sorted({t.project for t in db.list_tasks(limit=500) if t.project})
    proj_options = '<option value="">All projects</option>' + "".join(
        f'<option value="{p}" {"selected" if p==project else ""}>{p}</option>' for p in all_projects
    )
    status_options = ""
    for s in ["", "pending", "running", "done", "failed", "cancelled", "pending_confirm"]:
        label = s.replace("_", " ").title() if s else "All statuses"
        status_options += f'<option value="{s}" {"selected" if s==status else ""}>{label}</option>'

    rows = ""
    for t in all_tasks:
        cmd = t.command[:80] + ("…" if len(t.command) > 80 else "")
        proj = f'<a href="/projects/{t.project}" class="text-violet-400 hover:text-violet-300">{t.project}</a>' if t.project else '<span class="text-slate-600">—</span>'
        result_link = f'<a href="/tasks/{t.id}" class="text-xs text-slate-400 hover:text-white">view output →</a>' if t.status.value in ("done", "failed") else ""
        approve_btn = f'<a href="/tasks/{t.id}/approve" class="text-xs text-blue-400 hover:text-blue-300">approve →</a>' if t.status.value == "pending_confirm" else ""
        rows += f'''
        <tr class="border-b border-slate-700/50 hover:bg-slate-700/20 transition-colors">
          <td class="py-3 px-4"><code class="text-xs text-slate-300 font-mono">{t.id}</code></td>
          <td class="py-3 px-4 max-w-xs"><span class="text-sm text-slate-200" title="{t.command}">{cmd}</span></td>
          <td class="py-3 px-4">{proj}</td>
          <td class="py-3 px-4">{_badge(t.status.value)}</td>
          <td class="py-3 px-4 text-xs text-slate-500">{_rel_time(t.created_at)}</td>
          <td class="py-3 px-4 text-xs">{result_link}{approve_btn}</td>
        </tr>'''

    content = f'''
    <div class="p-6 max-w-6xl mx-auto">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h1 class="text-2xl font-bold text-white">Tasks</h1>
          <p class="text-slate-400 text-sm mt-1">{len(all_tasks)} task{"s" if len(all_tasks)!=1 else ""} shown</p>
        </div>
      </div>

      <!-- Filters -->
      <form method="get" class="flex gap-3 mb-5">
        <select name="project" onchange="this.form.submit()" class="text-sm rounded-lg px-3 py-2 text-slate-200" style="background:#1e293b; border:1px solid #334155;">
          {proj_options}
        </select>
        <select name="status" onchange="this.form.submit()" class="text-sm rounded-lg px-3 py-2 text-slate-200" style="background:#1e293b; border:1px solid #334155;">
          {status_options}
        </select>
      </form>

      <div class="card overflow-hidden">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-slate-700" style="background:#162032;">
              <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">ID</th>
              <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">Command</th>
              <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">Project</th>
              <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">Status</th>
              <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400">Created</th>
              <th class="text-left py-2.5 px-4 text-xs font-medium text-slate-400"></th>
            </tr>
          </thead>
          <tbody>
            {rows or '<tr><td colspan="6" class="py-12 text-center text-slate-500">No tasks found</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>'''

    return HTMLResponse(_base("Tasks", content, "tasks"))


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(task_id: str):
    db = _get_db()
    task = db.get_task(task_id)
    if not task:
        return HTMLResponse("<h1>Task not found</h1>", status_code=404)
    result = db.get_result(task_id)

    output_html = ""
    if result:
        stdout = result.stdout.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        stderr = result.stderr.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        output_html = f'''
        <div class="mt-6">
          <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-3">Output</h2>
          <div class="card p-4">
            <div class="text-xs text-slate-400 mb-2">stdout · exit code {result.exit_code} · {result.duration_seconds:.1f}s</div>
            <pre class="text-xs text-slate-300 font-mono overflow-auto max-h-96 whitespace-pre-wrap">{stdout or "(empty)"}</pre>
            {"<div class='mt-3 pt-3 border-t border-slate-700'><div class='text-xs text-red-400 mb-2'>stderr</div><pre class='text-xs text-red-300 font-mono overflow-auto max-h-48 whitespace-pre-wrap'>" + stderr + "</pre></div>" if stderr else ""}
          </div>
        </div>'''

    approve_html = ""
    if task.status.value == "pending_confirm":
        approve_html = f'''
        <div class="mt-4 p-4 rounded-lg" style="background:#1a2535; border:1px solid #3b82f6aa;">
          <div class="text-sm text-blue-300 font-medium mb-2">⚠ This task requires approval before it will run</div>
          <a href="/tasks/{task_id}/approve" class="inline-flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-white" style="background:#2563eb;">
            Approve &amp; Run
          </a>
        </div>'''

    content = f'''
    <div class="p-6 max-w-4xl mx-auto">
      <div class="mb-4">
        <a href="/tasks" class="text-sm text-slate-400 hover:text-white">← Tasks</a>
      </div>
      <div class="flex items-start justify-between mb-6">
        <div>
          <h1 class="text-xl font-bold text-white font-mono">{task.id}</h1>
          <p class="text-slate-400 text-sm mt-1">{task.command}</p>
        </div>
        {_badge(task.status.value)}
      </div>
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <div class="card p-3"><div class="text-xs text-slate-400 mb-1">Project</div><div class="text-sm text-violet-400">{task.project or "—"}</div></div>
        <div class="card p-3"><div class="text-xs text-slate-400 mb-1">Priority</div><div class="text-sm text-white">{task.priority}</div></div>
        <div class="card p-3"><div class="text-xs text-slate-400 mb-1">Created</div><div class="text-sm text-white">{_rel_time(task.created_at)}</div></div>
        <div class="card p-3"><div class="text-xs text-slate-400 mb-1">Auto-checkpoint</div><div class="text-sm text-white">{"Yes" if task.auto_checkpoint else "No"}</div></div>
      </div>
      {approve_html}
      {output_html}
    </div>'''

    return HTMLResponse(_base(f"Task {task_id}", content, "tasks"))


@app.get("/tasks/{task_id}/approve", response_class=HTMLResponse)
async def approve_task(task_id: str):
    """Approve a pending_confirm task so the daemon will run it."""
    db = _get_db()
    task = db.get_task(task_id)
    if task and task.status.value == "pending_confirm":
        from ..models import TaskStatus
        task.status = TaskStatus.PENDING
        db.update_task(task)
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@app.get("/projects", response_class=HTMLResponse)
async def projects_page():
    db = _get_db()
    summaries = db.recent_project_summaries(days=365) or []

    cards = ""
    for p in summaries:
        ts = datetime.fromisoformat(p["latest"]) if p.get("latest") else None
        task_text = p.get("current_task", "No checkpoint yet")[:80]
        cards += f'''
        <a href="/projects/{p['project']}" class="card p-5 block hover:border-violet-500/50 transition-colors group">
          <div class="flex items-start justify-between mb-3">
            <div class="font-semibold text-white group-hover:text-violet-300 transition-colors">{p['project']}</div>
            {_badge(p.get('status', 'in-progress'))}
          </div>
          <div class="text-sm text-slate-400 mb-3 line-clamp-2">{task_text}</div>
          <div class="flex items-center justify-between">
            <div class="text-xs text-slate-500">Last checkpoint {_rel_time(ts)}</div>
            <span class="text-xs text-violet-400 opacity-0 group-hover:opacity-100 transition-opacity">View timeline →</span>
          </div>
        </a>'''

    content = f'''
    <div class="p-6 max-w-6xl mx-auto">
      <div class="mb-6">
        <h1 class="text-2xl font-bold text-white">Projects</h1>
        <p class="text-slate-400 text-sm mt-1">{len(summaries)} project{"s" if len(summaries)!=1 else ""} tracked</p>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {cards or '<div class="card p-8 col-span-3 text-center text-slate-500">No projects yet — use checkpoint() to start tracking a project</div>'}
      </div>
    </div>'''

    return HTMLResponse(_base("Projects", content, "projects"))


@app.get("/projects/{project}", response_class=HTMLResponse)
async def project_detail(project: str):
    db = _get_db()
    checkpoints = db.list_checkpoints(project=project, limit=50)

    if not checkpoints:
        content = f'''
        <div class="p-6">
          <a href="/projects" class="text-sm text-slate-400 hover:text-white">← Projects</a>
          <div class="mt-6 card p-8 text-center text-slate-500">No checkpoints for "{project}" yet</div>
        </div>'''
        return HTMLResponse(_base(project, content, "projects"))

    latest = checkpoints[0]

    def _list_items(items: list, color: str = "slate-300") -> str:
        if not items:
            return '<span class="text-slate-600 text-sm">None</span>'
        return "".join(f'<li class="text-sm text-{color}">{i}</li>' for i in items)

    # Latest state panel
    state_html = f'''
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-8">
      <div class="card p-4">
        <div class="text-xs text-slate-400 uppercase tracking-wider mb-2">Goal</div>
        <div class="text-sm text-white">{latest.goal}</div>
        <div class="text-xs text-slate-400 uppercase tracking-wider mt-4 mb-2">Current Task</div>
        <div class="text-sm text-slate-200">{latest.current_task}</div>
        {"<div class='text-xs text-slate-400 uppercase tracking-wider mt-4 mb-2'>Context</div><div class='text-sm text-slate-300'>" + latest.context + "</div>" if latest.context else ""}
      </div>
      <div class="space-y-4">
        <div class="card p-4">
          <div class="text-xs text-green-400 uppercase tracking-wider mb-2">Findings</div>
          <ul class="space-y-1 list-disc list-inside">{_list_items(latest.findings, "slate-300")}</ul>
        </div>
        <div class="card p-4">
          <div class="text-xs text-red-400 uppercase tracking-wider mb-2">Dead Ends — Do Not Repeat</div>
          <ul class="space-y-1 list-disc list-inside">{_list_items(latest.dead_ends, "red-300")}</ul>
        </div>
        <div class="card p-4">
          <div class="text-xs text-violet-400 uppercase tracking-wider mb-2">Next Steps</div>
          <ul class="space-y-1 list-disc list-inside">{_list_items(latest.next_steps, "violet-300")}</ul>
        </div>
      </div>
    </div>'''

    # Timeline
    timeline_items = ""
    for i, cp in enumerate(checkpoints):
        is_latest = i == 0
        dot_color = "bg-violet-500" if is_latest else "bg-slate-600"
        findings_preview = " · ".join(cp.findings[:2])[:80] if cp.findings else "No findings"
        decisions_html = ""
        if cp.decisions:
            decisions_html = f'<div class="mt-2 text-xs text-amber-400">{len(cp.decisions)} decision{"s" if len(cp.decisions)!=1 else ""}: {cp.decisions[0].what[:60]}</div>'

        timeline_items += f'''
        <div class="timeline-line relative pb-6 pl-6">
          <div class="absolute left-0 top-1.5 w-4 h-4 rounded-full {dot_color} border-2 border-slate-900 z-10"></div>
          <div class="card p-4 {"border-violet-500/40" if is_latest else ""}">
            <div class="flex items-start justify-between mb-2">
              <div class="font-medium text-sm {"text-white" if is_latest else "text-slate-300"}">{cp.current_task}</div>
              <div class="flex items-center gap-2 flex-shrink-0 ml-3">
                {_badge(cp.status)}
                <span class="text-xs text-slate-500">{_rel_time(cp.timestamp)}</span>
              </div>
            </div>
            <div class="text-xs text-slate-400">{findings_preview}</div>
            {decisions_html}
            {"<div class='mt-1 text-xs text-violet-400'>← Latest</div>" if is_latest else ""}
          </div>
        </div>'''

    content = f'''
    <div class="p-6 max-w-5xl mx-auto">
      <div class="mb-4">
        <a href="/projects" class="text-sm text-slate-400 hover:text-white">← Projects</a>
      </div>
      <div class="flex items-center justify-between mb-6">
        <div>
          <h1 class="text-2xl font-bold text-white">{project}</h1>
          <p class="text-slate-400 text-sm mt-1">{len(checkpoints)} checkpoint{"s" if len(checkpoints)!=1 else ""} · last updated {_rel_time(latest.timestamp)}</p>
        </div>
        {_badge(latest.status)}
      </div>
      {state_html}
      <h2 class="text-sm font-semibold text-slate-300 uppercase tracking-wider mb-4">Checkpoint Timeline</h2>
      <div>
        {timeline_items}
      </div>
    </div>'''

    return HTMLResponse(_base(project, content, "projects"))


# ---------------------------------------------------------------------------
# Timeline (all projects)
# ---------------------------------------------------------------------------

@app.get("/timeline", response_class=HTMLResponse)
async def timeline_page(project: str = ""):
    db = _get_db()
    checkpoints = db.list_checkpoints(project=project or None, limit=100)
    all_projects = sorted({cp.project for cp in db.list_checkpoints(limit=500)})

    proj_options = '<option value="">All projects</option>' + "".join(
        f'<option value="{p}" {"selected" if p==project else ""}>{p}</option>' for p in all_projects
    )

    items = ""
    for cp in checkpoints:
        findings_preview = " · ".join(cp.findings[:2])[:100] if cp.findings else ""
        items += f'''
        <div class="timeline-line relative pb-5 pl-6">
          <div class="absolute left-0 top-1.5 w-4 h-4 rounded-full bg-slate-600 border-2 border-slate-900 z-10"></div>
          <div class="flex items-start gap-3">
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 mb-1">
                <a href="/projects/{cp.project}" class="text-xs font-medium text-violet-400 hover:text-violet-300">{cp.project}</a>
                <span class="text-xs text-slate-600">·</span>
                <span class="text-xs text-slate-500">{_rel_time(cp.timestamp)}</span>
                {_badge(cp.status)}
              </div>
              <div class="text-sm text-slate-200">{cp.current_task}</div>
              {f'<div class="text-xs text-slate-500 mt-1">{findings_preview}</div>' if findings_preview else ""}
            </div>
          </div>
        </div>'''

    content = f'''
    <div class="p-6 max-w-4xl mx-auto">
      <div class="flex items-center justify-between mb-6">
        <div>
          <h1 class="text-2xl font-bold text-white">Timeline</h1>
          <p class="text-slate-400 text-sm mt-1">All checkpoints across projects</p>
        </div>
      </div>
      <form method="get" class="mb-6">
        <select name="project" onchange="this.form.submit()" class="text-sm rounded-lg px-3 py-2 text-slate-200" style="background:#1e293b; border:1px solid #334155;">
          {proj_options}
        </select>
      </form>
      <div>
        {items or '<div class="card p-8 text-center text-slate-500">No checkpoints yet</div>'}
      </div>
    </div>'''

    return HTMLResponse(_base("Timeline", content, "timeline"))


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def api_status():
    status = daemon_status()
    stats  = _get_db().task_stats()
    return JSONResponse({
        "running":  status.get("running", False),
        "pid":      status.get("pid"),
        "stats":    stats,
    })


@app.get("/api/tasks")
async def api_tasks(project: str = "", status: str = "", limit: int = 50):
    db = _get_db()
    tasks = db.list_tasks(status=status or None, limit=limit)
    if project:
        tasks = [t for t in tasks if t.project == project]
    return JSONResponse([
        {"id": t.id, "command": t.command, "project": t.project,
         "status": t.status.value, "created_at": t.created_at.isoformat()}
        for t in tasks
    ])


@app.get("/api/projects")
async def api_projects():
    db = _get_db()
    summaries = db.recent_project_summaries(days=365) or []
    return JSONResponse(summaries)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Start the Continuum web UI."""
    if not _UI_AVAILABLE:
        raise RuntimeError(
            "UI dependencies missing. Install with: pip install continuum[ui]"
        )
    url = f"http://{host}:{port}"
    print(f"  Continuum UI → {url}")
    if open_browser:
        import threading
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
