"""M3 local persistence store (SQLite) for the L-M3 localization line.

Ownership boundary (R036/s3m3-notes.md D-2/D-6): the M3 formal surface keeps
its *own* durable facts under the ``YUNPAI_M3_DB`` env var (default
``runtime/yunpai-m3.sqlite``) instead of writing into canonical (M0), M1 or
M5 tables — single-fact-writer per module.

Tables
------
m3_readiness_snapshots        content-addressed order-level readiness snapshots
                              produced by get_material_readiness_snapshot
                              (immutable: same (order, input_checksum) replays
                              the stored row; query reads the latest row)
m3_material_demand_feedback   receive_m3_material_demand records (idempotent
                              on (task_id, event_id); applied flag stays 0 —
                              A-4 application gap is never masked)

Plan/bundle rows for the LEGACY face are intentionally NOT created yet: no
local writer exists today (workers.m3_mrp does not persist; R036 §9.1 defers
that to integration).  LEGACY tools therefore return explicit
unavailable/empty results instead of fabricating old bundles.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any

_DEFAULT_DB = "runtime/yunpai-m3.sqlite"

_DDL = """
CREATE TABLE IF NOT EXISTS m3_readiness_snapshots (
    order_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    input_checksum TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    snapshot_version TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    order_status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (order_id, tenant_id, input_checksum)
);
CREATE INDEX IF NOT EXISTS idx_m3_readiness_order
    ON m3_readiness_snapshots(order_id, tenant_id, created_at);
CREATE TABLE IF NOT EXISTS m3_material_demand_feedback (
    feedback_ref TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    plan_version TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'pending_review',
    applied INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (task_id, event_id)
);
"""


class M3FeedbackConflictError(Exception):
    """Same (task_id, event_id) accepted earlier with a different fingerprint."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def default_db_path() -> str:
    return str(os.getenv("YUNPAI_M3_DB") or _DEFAULT_DB)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class M3Store:
    """Thread-safe single-file SQLite store for M3 formal facts."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = str(path) if path is not None else default_db_path()
        if self.path != ":memory:" and Path(self.path).parent:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as db:
            db.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    # ------------------------------------------------------------------
    # readiness snapshots
    # ------------------------------------------------------------------

    def store_snapshot(
        self,
        *,
        order_id: str,
        tenant_id: str,
        input_checksum: str,
        snapshot_id: str,
        snapshot_version: str,
        calculation_version: str,
        order_status: str,
        observed_at: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Content-addressed persist; identical (order, tenant, checksum)
        replays the stored row (same snapshot_id / observed_at)."""
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        created_at = _now_iso()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO m3_readiness_snapshots
                   (order_id, tenant_id, input_checksum, snapshot_id,
                    snapshot_version, calculation_version, order_status,
                    observed_at, payload_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (order_id, tenant_id, input_checksum, snapshot_id,
                 snapshot_version, calculation_version, order_status,
                 observed_at, encoded, created_at),
            )
            row = db.execute(
                "SELECT * FROM m3_readiness_snapshots WHERE order_id=? AND tenant_id=? AND input_checksum=?",
                (order_id, tenant_id, input_checksum),
            ).fetchone()
        return _snapshot_row(row)

    def latest_snapshot(self, order_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m3_readiness_snapshots WHERE order_id=? AND tenant_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (order_id, tenant_id),
            ).fetchone()
        return _snapshot_row(row)

    # ------------------------------------------------------------------
    # material-demand feedback (receive only; A-4: applied never set here)
    # ------------------------------------------------------------------

    def find_feedback(self, task_id: str, event_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m3_material_demand_feedback WHERE task_id=? AND event_id=?",
                (task_id, event_id),
            ).fetchone()
        return _feedback_row(row)

    def receive_feedback(
        self,
        *,
        task_id: str,
        event_id: str,
        feedback_type: str,
        tenant_id: str,
        plan_version: str,
        fingerprint: str,
        feedback_ref: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Idempotent acceptance. Returns (record, replayed).

        Same identity with a different fingerprint raises
        M3FeedbackConflictError (mirrors legacy 409
        M3_MATERIAL_DEMAND_IDEMPOTENCY_CONFLICT).
        """
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        received_at = _now_iso()
        with self._lock, self._connect() as db:
            existing = db.execute(
                "SELECT * FROM m3_material_demand_feedback WHERE task_id=? AND event_id=?",
                (task_id, event_id),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] == fingerprint:
                    return _feedback_row(existing), True
                raise M3FeedbackConflictError(
                    f"idempotency conflict for task_id={task_id} event_id={event_id}: "
                    "same identity, different payload fingerprint"
                )
            db.execute(
                """INSERT INTO m3_material_demand_feedback
                   (feedback_ref, task_id, event_id, feedback_type, tenant_id,
                    plan_version, review_status, applied, fingerprint,
                    payload_json, received_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (feedback_ref, task_id, event_id, feedback_type, tenant_id,
                 plan_version, "pending_review", 0, fingerprint, encoded, received_at),
            )
            row = db.execute(
                "SELECT * FROM m3_material_demand_feedback WHERE feedback_ref=?",
                (feedback_ref,),
            ).fetchone()
        return _feedback_row(row), False


def _snapshot_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    return item


def _feedback_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    item["applied"] = bool(item["applied"])
    return item
