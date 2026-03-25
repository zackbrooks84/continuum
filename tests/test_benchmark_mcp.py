from __future__ import annotations

import importlib
import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DATASET_SIZES = {
    "small": 100,
    "medium": 1_000,
    "large": 5_000,
}


def _reload_mcp(monkeypatch: pytest.MonkeyPatch, db_path: Path):
    monkeypatch.setenv("CONTINUUM_DB", str(db_path))

    import continuum.db as db_module
    import continuum.mcp_server as mcp_module

    importlib.reload(db_module)
    return importlib.reload(mcp_module)


def _seed_checkpoints(mcp, *, project: str, count: int) -> list[str]:
    ids: list[str] = []
    for idx in range(count):
        cp = mcp.checkpoint(
            project=project,
            task=f"task-{idx}",
            goal="benchmark memory operations",
            context=f"context line {idx}",
            findings=[f"finding {idx}", "echo hi", "shared benchmark token"],
            next_steps=[f"next-{idx}"],
            dead_ends=[f"dead-end-{idx % 7}"],
        )
        ids.append(cp["checkpoint_id"])
    return ids


@pytest.fixture
def mcp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "continuum-benchmark.db"
    return _reload_mcp(monkeypatch, db_path)


@pytest.fixture
def seeded_mcp(mcp_env):
    checkpoint_ids = _seed_checkpoints(mcp_env, project="benchproj", count=250)
    return mcp_env, checkpoint_ids


@pytest.fixture
def queue_ready_mcp(mcp_env):
    mcp = mcp_env
    queued = mcp.forge_push(name="bench-result", command="echo hi", project="benchproj")
    task_id = queued["task_id"]
    run_now = mcp.forge_run_now(task_id=task_id)
    assert run_now["success"] is True
    return mcp, task_id


@pytest.fixture
def scaled_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    label = request.param
    count = DATASET_SIZES[label]
    mcp = _reload_mcp(monkeypatch, tmp_path / f"continuum-{label}.db")
    checkpoint_ids = _seed_checkpoints(mcp, project="scaleproj", count=count)
    return mcp, checkpoint_ids, label, count


@pytest.mark.benchmark(group="core-memory")
def test_benchmark_checkpoint_insert_latency(mcp_env, benchmark):
    mcp = mcp_env
    counter = itertools.count()

    def run_once():
        i = next(counter)
        return mcp.checkpoint(
            project="benchproj",
            task=f"checkpoint-{i}",
            goal="measure insert latency",
            findings=["bench"],
        )

    result = benchmark.pedantic(run_once, rounds=20, iterations=1)
    assert result["checkpoint_id"]


@pytest.mark.benchmark(group="core-memory")
def test_benchmark_memory_search_latency(seeded_mcp, benchmark):
    mcp, _ = seeded_mcp
    result = benchmark(lambda: mcp.memory_search(query="shared benchmark token", limit=10))
    assert result["count"] >= 1


@pytest.mark.benchmark(group="core-memory")
def test_benchmark_memory_timeline(seeded_mcp, benchmark):
    mcp, checkpoint_ids = seeded_mcp
    pivot = checkpoint_ids[len(checkpoint_ids) // 2]
    result = benchmark(lambda: mcp.memory_timeline(checkpoint_id=pivot, window=5))
    assert result["total"] >= 1


@pytest.mark.benchmark(group="core-memory")
def test_benchmark_memory_get(seeded_mcp, benchmark):
    mcp, checkpoint_ids = seeded_mcp
    ids = checkpoint_ids[:5]
    result = benchmark(lambda: mcp.memory_get(ids=ids))
    assert result["count"] == len(ids)


@pytest.mark.benchmark(group="core-memory")
def test_benchmark_smart_resume(seeded_mcp, benchmark):
    mcp, _ = seeded_mcp
    result = benchmark(lambda: mcp.smart_resume(project="benchproj", query="shared benchmark token"))
    assert result["project"] == "benchproj"


@pytest.mark.benchmark(group="task-queue")
def test_benchmark_forge_push(mcp_env, benchmark):
    mcp = mcp_env
    counter = itertools.count()

    def run_once():
        i = next(counter)
        return mcp.forge_push(name=f"push-{i}", command="echo hi", project="benchproj")

    result = benchmark.pedantic(run_once, rounds=20, iterations=1)
    assert result["task_id"]


@pytest.mark.benchmark(group="task-queue")
def test_benchmark_forge_status(mcp_env, benchmark):
    mcp = mcp_env
    queued = mcp.forge_push(name="status", command="echo hi", project="benchproj")
    task_id = queued["task_id"]
    result = benchmark(lambda: mcp.forge_status(task_id=task_id))
    assert result["id"] == task_id


@pytest.mark.benchmark(group="task-queue")
def test_benchmark_forge_list(mcp_env, benchmark):
    mcp = mcp_env
    for idx in range(10):
        mcp.forge_push(name=f"list-{idx}", command="echo hi", project="benchproj")
    result = benchmark(lambda: mcp.forge_list(limit=20))
    assert isinstance(result, list)
    assert len(result) >= 1


@pytest.mark.benchmark(group="task-queue")
def test_benchmark_forge_result(queue_ready_mcp, benchmark):
    mcp, task_id = queue_ready_mcp
    result = benchmark(lambda: mcp.forge_result(task_id=task_id))
    assert result["task_id"] == task_id
    assert result["exit_code"] == 0


@pytest.mark.benchmark(group="session-context")
def test_benchmark_handoff(seeded_mcp, benchmark):
    mcp, _ = seeded_mcp
    result = benchmark(lambda: mcp.handoff(project="benchproj", save=False))
    assert result["checkpoint_id"]


@pytest.mark.benchmark(group="session-context")
def test_benchmark_status(seeded_mcp, benchmark):
    mcp, _ = seeded_mcp
    result = benchmark(lambda: mcp.status(project="benchproj"))
    assert result["project"] == "benchproj"


@pytest.mark.benchmark(group="session-context")
def test_benchmark_history(seeded_mcp, benchmark):
    mcp, _ = seeded_mcp
    result = benchmark(lambda: mcp.history(project="benchproj", limit=50))
    assert result["checkpoints"]


@pytest.mark.benchmark(group="session-context")
def test_benchmark_remember(seeded_mcp, benchmark):
    mcp, _ = seeded_mcp
    result = benchmark(lambda: mcp.remember(days=30))
    assert result["active_projects"] >= 1


@pytest.mark.benchmark(group="session-context")
def test_benchmark_sync_files(seeded_mcp, tmp_path: Path, benchmark):
    mcp, _ = seeded_mcp
    output_dir = tmp_path / "sync-out"
    result = benchmark(lambda: mcp.sync_files(project="benchproj", output_dir=str(output_dir), save_dir=False))
    assert result["project"] == "benchproj"


@pytest.mark.benchmark(group="session-context")
def test_benchmark_export_import_handoff(seeded_mcp, benchmark):
    mcp, _ = seeded_mcp

    def run_once():
        exported = mcp.export_handoff(project="benchproj")
        imported = mcp.import_web_handoff(md_string=exported["markdown"], save=False)
        return exported, imported

    exported, imported = benchmark(run_once)
    assert exported["token_estimate"] >= 1
    assert imported["project"] == "benchproj"


@pytest.mark.parametrize("scaled_mcp", ["small", "medium", "large"], indirect=True)
@pytest.mark.benchmark(group="scaling-memory-search")
def test_benchmark_memory_search_scaling(scaled_mcp, benchmark):
    mcp, _, label, count = scaled_mcp
    result = benchmark(lambda: mcp.memory_search(query="shared benchmark token", limit=25))
    assert result["count"] >= min(count, 25)
    assert label in DATASET_SIZES


@pytest.mark.parametrize("scaled_mcp", ["small", "medium", "large"], indirect=True)
@pytest.mark.benchmark(group="scaling-smart-resume")
def test_benchmark_smart_resume_scaling(scaled_mcp, benchmark):
    mcp, _, label, _ = scaled_mcp
    result = benchmark(lambda: mcp.smart_resume(project="scaleproj", query="shared benchmark token"))
    assert result["project"] == "scaleproj"
    assert label in DATASET_SIZES


@pytest.mark.parametrize("scaled_mcp", ["small", "medium", "large"], indirect=True)
@pytest.mark.benchmark(group="scaling-history")
def test_benchmark_history_scaling(scaled_mcp, benchmark):
    mcp, _, label, _ = scaled_mcp
    result = benchmark(lambda: mcp.history(project="scaleproj", limit=100))
    assert len(result["checkpoints"]) >= 1
    assert label in DATASET_SIZES
