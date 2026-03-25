from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_mcp_tools_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTINUUM_DB", str(tmp_path / "continuum.db"))

    import continuum.db as db_module
    import continuum.mcp_server as mcp_module

    importlib.reload(db_module)
    mcp = importlib.reload(mcp_module)

    cp = mcp.checkpoint(
        project="auditproj",
        task="audit repo",
        goal="verify all tools",
        findings=["seed"],
    )
    assert cp["checkpoint_id"]

    queued = mcp.forge_push(name="real", command="echo hi", project="auditproj")
    task_id = queued["task_id"]

    assert mcp.quickstart(project="auditproj")
    assert mcp.forge_push(name="dry", command="echo hi", dry_run=True, project="auditproj")["dry_run"]
    assert mcp.forge_status()["queue"]
    assert mcp.forge_status(task_id=task_id)["id"] == task_id
    assert "status" in mcp.forge_result(task_id=task_id)
    assert isinstance(mcp.forge_list(), list)
    assert mcp.forge_cancel(task_id=task_id)["cancelled"] is True
    assert mcp.forge_approve(task_id=task_id)["approved"] is True
    assert mcp.forge_run_now(task_id=task_id)["success"] is True
    assert mcp.safe_mode(level="off")["safe_mode"] == "off"

    assert mcp.handoff(project="auditproj")
    assert mcp.status(project="auditproj")["project"] == "auditproj"

    history = mcp.history(project="auditproj")
    assert history["checkpoints"]
    first = history["checkpoints"][0]
    assert len(first["id"]) > 8
    assert len(first["short_id"]) == 8

    assert mcp.projects()["projects"]
    assert mcp.push_and_checkpoint(
        name="pc",
        command="echo done",
        project="auditproj",
        goal="audit done",
    )

    assert mcp.memory_search(query="audit")["count"] >= 1
    assert mcp.memory_timeline(checkpoint_id=first["id"])["total"] >= 1
    assert mcp.memory_get(ids=[first["id"]])["count"] >= 1
    assert mcp.remember(days=30)["active_projects"] >= 1

    assert mcp.observe_control(action="on", project="auditproj")["auto_observe"] is True
    assert mcp.auto_observe_toggle(enabled=True, project="auditproj")["auto_observe"] is True
    assert mcp.observe_status()["auto_observe"] is True
    assert mcp.auto_mode(enabled=True, project="auditproj")["auto_mode"] is True
    assert mcp.smart_resume(project="auditproj")["project"] == "auditproj"

    assert mcp.notify_configure(project="auditproj", webhook_url="https://example.com")
    assert mcp.notify_when_complete(task_id=task_id, webhook_url="https://example.com")
    assert "sent" in mcp.notify_test(project="auditproj", message="test")

    assert mcp.remember_me(key="audit_user", value="yes")["stored"] is True
    assert mcp.recall_me(query="audit_user")["count"] >= 1
    assert mcp.remember_this(key="audit_rule", value="always audit")["stored"] is True
    assert mcp.recall_this(query="audit_rule")["count"] >= 1
    assert mcp.forget(key="audit_user", memory_type="user")["deleted"] is True
    assert mcp.forget(key="audit_rule", memory_type="agent")["deleted"] is True
    assert "briefing" in mcp.north_star()

    assert mcp.sync_files(project="auditproj")["project"] == "auditproj"
    exported = mcp.export_handoff(project="auditproj")
    assert "markdown" in exported
    imported = mcp.import_web_handoff(md_string=exported["markdown"], save=False)
    assert imported["project"] == "auditproj"

    assert mcp.dispatch_claude(prompt="say hi", project="auditproj")["task_id"]
    watch = mcp.token_watch(used=800, limit=1000, project="auditproj")
    assert watch["status"] in {"ok", "warning", "critical"}
    assert mcp.cross_agent_handoff(project="auditproj", target_agent="gpt")["project"] == "auditproj"
    assert "project" in mcp.pattern_suggestions(project="auditproj")
