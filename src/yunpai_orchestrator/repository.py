from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from .models import RunState


class RunRepository(Protocol):
    def save(self, state: RunState) -> None: ...
    def get(self, run_id: str) -> RunState | None: ...
    def list(self, *, tenant_id: str | None = None, limit: int = 100) -> list[RunState]: ...


class InMemoryRunRepository:
    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}

    def save(self, state: RunState) -> None:
        self._runs[state["run_id"]] = deepcopy(state)

    def get(self, run_id: str) -> RunState | None:
        value = self._runs.get(run_id)
        return deepcopy(value) if value else None

    def list(self, *, tenant_id: str | None = None, limit: int = 100) -> list[RunState]:
        values = list(reversed(self._runs.values()))
        if tenant_id is not None:
            values = [value for value in values if value.get("tenant_id") == tenant_id]
        return deepcopy(values[:limit])


class SQLiteRunRepository:
    """单机可恢复运行库；生产环境可替换为 PostgreSQL repository。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = RLock()
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def save(self, state: RunState) -> None:
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True)
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO runs(run_id, task_id, tenant_id, status, state_json)
                   VALUES(?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                   status=excluded.status, state_json=excluded.state_json,
                   updated_at=CURRENT_TIMESTAMP""",
                (state["run_id"], state["task_id"], state.get("tenant_id", "default"), state["status"], encoded),
            )

    def get(self, run_id: str) -> RunState | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT state_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row["state_json"]) if row else None

    def list(self, *, tenant_id: str | None = None, limit: int = 100) -> list[RunState]:
        sql, params = "SELECT state_json FROM runs", []
        if tenant_id is not None:
            sql += " WHERE tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [json.loads(row["state_json"]) for row in rows]
