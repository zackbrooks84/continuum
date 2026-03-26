from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from continuum.db import DB
from continuum.models import Checkpoint


def test_checkpoint_writes_always_persist_json_data(tmp_path: Path):
    db = DB(tmp_path / "integrity.db")

    for i in range(25):
        db.save_checkpoint(
            Checkpoint(
                project="integrity",
                current_task=f"task-{i}",
                goal="verify checkpoint payload integrity",
                findings=[f"finding-{i}"],
            )
        )

    row = db._conn.execute(
        "SELECT COUNT(*) AS n FROM checkpoints WHERE data IS NULL OR TRIM(data) = ''"
    ).fetchone()
    assert row["n"] == 0


def test_list_checkpoints_skips_invalid_rows_when_not_strict(tmp_path: Path):
    db_path = tmp_path / "legacy-nullable.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE checkpoints (
            id        TEXT PRIMARY KEY,
            project   TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            data      TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO checkpoints(id, project, timestamp, data) VALUES (?,?,?,?)",
        ("bad-row", "legacy", "2026-03-26T00:00:00+00:00", None),
    )
    conn.commit()
    conn.close()

    db = DB(db_path)
    cps = db.list_checkpoints(project="legacy", strict=False)
    assert cps == []


def test_list_checkpoints_raises_with_context_when_strict(tmp_path: Path):
    db_path = tmp_path / "legacy-nullable-strict.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE checkpoints (
            id        TEXT PRIMARY KEY,
            project   TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            data      TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO checkpoints(id, project, timestamp, data) VALUES (?,?,?,?)",
        ("bad-row", "legacy", "2026-03-26T00:00:00+00:00", None),
    )
    conn.commit()
    conn.close()

    db = DB(db_path)
    with pytest.raises(RuntimeError) as exc:
        db.list_checkpoints(project="legacy", strict=True)

    msg = str(exc.value)
    assert "bad-row" in msg
    assert "legacy" in msg
    assert "2026-03-26T00:00:00+00:00" in msg
    assert "data is NULL" in msg
