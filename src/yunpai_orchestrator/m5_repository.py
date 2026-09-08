"""M5 plan/snapshot repository (SQLite).

Taskbook Task 2: persist the six snapshot kinds and authoritative plan
versions with idempotent replay, input/solver hashes, scenario head CAS and
published-version protection.  This module is the local persistence boundary
that the M5 tool handlers use; it never fabricates plan facts.

Tables
------
m5_snapshots      immutable snapshot payload per (scenario_id, kind, snapshot_id)
m5_plans          authoritative plan versions (one row per plan_version)
m5_heads          scenario head pointers with a revision counter for CAS
m5_idempotency    idempotency_key -> stored plan/hash for replay & conflicts
m5_lifecycle      append-only lifecycle transition log
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

from .pmc_v2_snapshots import canonical_bytes, checksum_of

# lifecycle order used to reject illegal transitions (Task 3)
LIFECYCLE_ORDER = {
    "draft": 0,
    "approved": 1,
    "released": 2,
    "dispatched": 3,
    "execution": 4,
}
PROTECTED_STATUSES = {"released", "dispatched", "execution"}

SNAPSHOT_KINDS = (
    ("order_snapshots", "order_snapshots"),
    ("routes", "routes"),
    ("resource_snapshot", "resource_snapshot"),
    ("calendar_snapshot", "calendar_snapshot"),
    ("supply_snapshot", "supply_snapshot"),
    ("constraint_snapshot", "constraint_snapshot"),
)

_DDL = """
CREATE TABLE IF NOT EXISTS m5_snapshots (
    scenario_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    task_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scenario_id, kind, snapshot_id)
);
CREATE TABLE IF NOT EXISTS m5_plans (
    plan_version TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    task_id TEXT NOT NULL DEFAULT '',
    lifecycle_status TEXT NOT NULL DEFAULT 'draft',
    parent_plan_version TEXT,
    input_hash TEXT NOT NULL,
    solver_hash TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    scenario_purpose TEXT NOT NULL DEFAULT 'production',
    validation_report_json TEXT NOT NULL DEFAULT '{"status":"unknown","errors":[]}',
    bundle_json TEXT NOT NULL,
    schedule_json TEXT NOT NULL,
    released_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_heads (
    scenario_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    head_plan_version TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_idempotency (
    scenario_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    plan_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scenario_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS m5_lifecycle (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_version TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    gate TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    revision TEXT NOT NULL DEFAULT '',
    transitioned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_m5_plans_scenario ON m5_plans(scenario_id);
CREATE INDEX IF NOT EXISTS idx_m5_lifecycle_plan ON m5_lifecycle(plan_version);
CREATE TABLE IF NOT EXISTS m5_dispatch (
    dispatch_id TEXT PRIMARY KEY,
    plan_version TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    task_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    operation_keys_json TEXT NOT NULL,
    target_system TEXT NOT NULL DEFAULT 'mes',
    status TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT NOT NULL DEFAULT '{}',
    requested_by TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (plan_version, idempotency_key)
);
CREATE TABLE IF NOT EXISTS m5_execution_events (
    event_id TEXT PRIMARY KEY,
    plan_version TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    task_id TEXT NOT NULL DEFAULT '',
    order_id TEXT NOT NULL DEFAULT '',
    operation_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    external_ref TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    worker_id TEXT NOT NULL DEFAULT '',
    team_id TEXT NOT NULL DEFAULT '',
    station TEXT NOT NULL DEFAULT '',
    reported_quantity TEXT NOT NULL DEFAULT '',
    scrap_quantity TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'accepted_execution_event',
    event_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (plan_version, event_type, order_id, operation_id, external_ref)
);
CREATE INDEX IF NOT EXISTS idx_m5_execution_plan ON m5_execution_events(plan_version);
CREATE TABLE IF NOT EXISTS m5_messages (
    draft_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    task_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    department TEXT NOT NULL,
    channel TEXT NOT NULL,
    recipient_targets_json TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    event_summary TEXT NOT NULL DEFAULT '',
    required_action TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS m5_outbox (
    outbox_id TEXT PRIMARY KEY,
    draft_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    status TEXT NOT NULL DEFAULT 'pending',
    provider_message_id TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_knowledge (
    knowledge_id TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    plan_version TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    task_id TEXT NOT NULL DEFAULT '',
    input_sha256 TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    solver_status TEXT NOT NULL DEFAULT '',
    on_time_rate TEXT NOT NULL DEFAULT '',
    total_tardiness_minutes TEXT NOT NULL DEFAULT '',
    validation_passed INTEGER NOT NULL DEFAULT 0,
    tags_json TEXT NOT NULL DEFAULT '[]',
    features_json TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS m5_procurement_proposals (
    proposal_id TEXT PRIMARY KEY,
    scenario_id TEXT NOT NULL,
    plan_version TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT 'default',
    task_id TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposal',
    items_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (scenario_id, idempotency_key)
);
"""


class M5RepositoryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(text: str | None) -> Any:
    return json.loads(text) if text else None


class M5Repository:
    """Single-file SQLite store for M5 snapshot/plan state (thread-safe)."""

    def __init__(self, path: str | Path = "runtime/yunpai-m5.sqlite") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as db:
            db.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        return db

    # ------------------------------------------------------------------
    # snapshot persistence (Task 2)
    # ------------------------------------------------------------------

    def store_snapshots(self, scenario_id: str, bundle: dict[str, Any], *,
                        tenant_id: str = "default", task_id: str = "") -> list[dict[str, Any]]:
        """Persist the six snapshot kinds from a validated v2 input bundle.

        Returns a list of stored {kind, snapshot_id, revision, checksum} facts.
        Raises M5RepositoryError MISSING_SNAPSHOT when a required kind is
        absent from the bundle.
        """
        records = []
        with self._lock, self._connect() as db:
            # snapshots are the authoritative current facts for a scenario;
            # storing replaces the previous immutable fact set for that scenario
            db.execute("DELETE FROM m5_snapshots WHERE scenario_id=?", (scenario_id,))
            for kind, key in SNAPSHOT_KINDS:
                value = bundle.get(key)
                if value is None:
                    raise M5RepositoryError("MISSING_SNAPSHOT", f"bundle 缺少 {key}")
                if isinstance(value, list):
                    entries = []
                    for item in value:
                        if isinstance(item, dict):
                            entries.append(item)
                elif isinstance(value, dict):
                    # routes{product_code} or a singleton snapshot document
                    if any(isinstance(v, dict) for v in value.values()) and not value.get("snapshot_id"):
                        entries = list(value.values())
                    else:
                        entries = [value]
                else:
                    raise M5RepositoryError("MISSING_SNAPSHOT", f"bundle 缺少 {key}")
                if not entries:
                    raise M5RepositoryError("MISSING_SNAPSHOT", f"bundle 缺少 {key}")
                for snapshot in entries:
                    if not isinstance(snapshot, dict):
                        continue
                    snapshot_id = str(snapshot.get("snapshot_id") or f"{kind}-{len(records)}")
                    payload_json = _json(snapshot)
                    checksum = checksum_of(snapshot)
                    db.execute(
                        """INSERT INTO m5_snapshots
                           (scenario_id, kind, snapshot_id, tenant_id, task_id, payload_json, created_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (scenario_id, kind, snapshot_id, tenant_id, task_id, payload_json, _now()),
                    )
                    records.append({
                        "kind": kind,
                        "snapshot_id": snapshot_id,
                        "revision": int(snapshot.get("revision", 1)),
                        "checksum": checksum,
                        "scenario_id": scenario_id,
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                    })
        return records

    def load_bundle(self, scenario_id: str) -> dict[str, Any] | None:
        """Reconstruct the six-kind bundle from the latest stored snapshots."""
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT kind, payload_json, rowid FROM m5_snapshots
                   WHERE scenario_id=? ORDER BY kind, rowid""",
                (scenario_id,),
            ).fetchall()
        if not rows:
            return None
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_kind.setdefault(row["kind"], []).append(_loads(row["payload_json"]))
        bundle: dict[str, Any] = {}
        for kind, key in SNAPSHOT_KINDS:
            docs = by_kind.get(kind) or []
            if kind == "order_snapshots":
                # a scenario may carry several immutable order snapshots
                bundle[key] = [doc for doc in docs if isinstance(doc, dict) and doc.get("order_id")]
            elif kind == "routes":
                routes = {}
                for doc in docs:
                    product = str(doc.get("product_code") or "")
                    if product:
                        routes[product] = doc
                bundle[key] = routes
            else:
                bundle[key] = docs[-1] if docs else None
        if not bundle.get("order_snapshots") or not bundle.get("routes"):
            return None
        return bundle

    def list_snapshots(self, scenario_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT kind, snapshot_id, tenant_id, task_id, payload_json
                   FROM m5_snapshots WHERE scenario_id=? ORDER BY kind, snapshot_id""",
                (scenario_id,),
            ).fetchall()
        return [
            {
                "kind": row["kind"], "snapshot_id": row["snapshot_id"],
                "tenant_id": row["tenant_id"], "task_id": row["task_id"],
                "payload": _loads(row["payload_json"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # plan persistence / idempotency (Task 2)
    # ------------------------------------------------------------------

    def find_by_idempotency(self, scenario_id: str, idempotency_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT plan_version, input_hash FROM m5_idempotency
                   WHERE scenario_id=? AND idempotency_key=?""",
                (scenario_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        return {"plan_version": row["plan_version"], "input_hash": row["input_hash"]}

    def save_plan(self, *, plan_version: str, scenario_id: str, tenant_id: str,
                  task_id: str, lifecycle_status: str, parent_plan_version: str | None,
                  input_hash: str, solver_hash: str, algorithm_version: str,
                  scenario_purpose: str, validation_report: dict[str, Any],
                  bundle: dict[str, Any], schedule: dict[str, Any],
                  idempotency_key: str | None = None) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as db:
            existing = db.execute("SELECT lifecycle_status FROM m5_plans WHERE plan_version=?",
                                  (plan_version,)).fetchone()
            if existing is not None and existing["lifecycle_status"] in PROTECTED_STATUSES:
                # released/dispatched/execution plans are immutable; they are
                # reached only via transition() and may never be overwritten.
                raise M5RepositoryError("PLAN_PROTECTED",
                                        f"已发布计划 {plan_version} 不可覆盖")
            if lifecycle_status in PROTECTED_STATUSES:
                raise M5RepositoryError("PLAN_PROTECTED",
                                        f"已发布计划 {plan_version} 不可覆盖")
            db.execute(
                """INSERT INTO m5_plans
                   (plan_version, scenario_id, tenant_id, task_id, lifecycle_status,
                    parent_plan_version, input_hash, solver_hash, algorithm_version,
                    scenario_purpose, validation_report_json, bundle_json, schedule_json,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(plan_version) DO UPDATE SET
                     lifecycle_status=excluded.lifecycle_status,
                     parent_plan_version=excluded.parent_plan_version,
                     algorithm_version=excluded.algorithm_version,
                     validation_report_json=excluded.validation_report_json,
                     schedule_json=excluded.schedule_json,
                     updated_at=excluded.updated_at""",
                (plan_version, scenario_id, tenant_id, task_id, lifecycle_status,
                 parent_plan_version, input_hash, solver_hash, algorithm_version,
                 scenario_purpose, _json(validation_report), _json(bundle), _json(schedule),
                 now, now),
            )
            if idempotency_key:
                db.execute(
                    """INSERT OR IGNORE INTO m5_idempotency
                       (scenario_id, idempotency_key, plan_version, input_hash, created_at)
                       VALUES (?,?,?,?,?)""",
                    (scenario_id, idempotency_key, plan_version, input_hash, now),
                )
        return {
            "plan_version": plan_version, "scenario_id": scenario_id,
            "tenant_id": tenant_id, "task_id": task_id,
            "lifecycle_status": lifecycle_status,
            "parent_plan_version": parent_plan_version,
            "input_hash": input_hash, "solver_hash": solver_hash,
            "algorithm_version": algorithm_version,
            "scenario_purpose": scenario_purpose,
            "validation_report": validation_report,
        }

    def get_plan(self, plan_version: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m5_plans WHERE plan_version=?", (plan_version,)).fetchone()
        if row is None:
            return None
        plan = dict(row)
        plan["validation_report"] = _loads(row["validation_report_json"])
        plan["bundle"] = _loads(row["bundle_json"])
        plan["schedule"] = _loads(row["schedule_json"])
        return plan

    def list_plans(self, scenario_id: str | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT plan_version, scenario_id, lifecycle_status, parent_plan_version, input_hash, solver_hash, algorithm_version, scenario_purpose, created_at, updated_at FROM m5_plans"
        params: list[Any] = []
        if scenario_id:
            sql += " WHERE scenario_id=?"
            params.append(scenario_id)
        sql += " ORDER BY updated_at DESC, plan_version DESC LIMIT ?"
        params.append(int(limit))
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # scenario head CAS (Task 2 / 3)
    # ------------------------------------------------------------------

    def set_head(self, scenario_id: str, plan_version: str, *,
                 tenant_id: str = "default", expected_revision: int | None = None) -> int:
        """Advance the scenario head with optimistic CAS.

        Returns the new revision.  Raises M5RepositoryError HEAD_CONFLICT when
        ``expected_revision`` does not match the current head revision.
        """
        now = _now()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT revision FROM m5_heads WHERE scenario_id=?", (scenario_id,)).fetchone()
            current = int(row["revision"]) if row else 0
            if expected_revision is not None and current != expected_revision:
                raise M5RepositoryError("HEAD_CONFLICT",
                                        f"scenario {scenario_id} head 已被更新（当前 revision={current}，期望 {expected_revision}）")
            new_revision = current + 1
            db.execute(
                """INSERT INTO m5_heads (scenario_id, tenant_id, head_plan_version, revision, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(scenario_id) DO UPDATE SET
                     head_plan_version=excluded.head_plan_version,
                     revision=excluded.revision,
                     updated_at=excluded.updated_at""",
                (scenario_id, tenant_id, plan_version, new_revision, now),
            )
        return new_revision

    def get_head(self, scenario_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m5_heads WHERE scenario_id=?", (scenario_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # lifecycle transitions (Task 3)
    # ------------------------------------------------------------------

    def transition(self, plan_version: str, to_status: str, *, gate: str,
                   actor: str = "", task_id: str = "", trace_id: str = "",
                   revision: str = "", expected_from: str | None = None) -> dict[str, Any]:
        """Apply one lifecycle transition with legality + revision checks.

        Taskbook Task 3 guards:
        - pressure_only/preview plans can never be released or dispatched
          (they are diagnostic-only identities).
        - a validation-failed draft can be approved (gate review), but cannot
          be released until the validation report passes.
        """
        if to_status not in LIFECYCLE_ORDER:
            raise M5RepositoryError("INVALID_STATUS", f"未知生命周期状态 {to_status}")
        plan = self.get_plan(plan_version)
        if plan is None:
            raise M5RepositoryError("PLAN_NOT_FOUND", f"计划 {plan_version} 不存在")
        from_status = plan["lifecycle_status"]
        if from_status not in LIFECYCLE_ORDER:
            raise M5RepositoryError("INVALID_STATUS", f"未知生命周期状态 {from_status}")
        if LIFECYCLE_ORDER[to_status] != LIFECYCLE_ORDER[from_status] + 1:
            raise M5RepositoryError("ILLEGAL_TRANSITION",
                                    f"{from_status} -> {to_status} 不是合法相邻迁移")
        if expected_from is not None and from_status != expected_from:
            raise M5RepositoryError("STALE_REVISION",
                                    f"计划 {plan_version} 当前状态为 {from_status}，期望 {expected_from}")
        if plan.get("scenario_purpose") != "production" and to_status in {"released", "dispatched", "execution"}:
            raise M5RepositoryError("PURPOSE_NOT_RELEASABLE",
                                    f"pressure_only/preview 计划 {plan_version} 不能 {to_status}")
        if to_status == "released":
            report = plan.get("validation_report") or {}
            if report.get("status") != "pass":
                raise M5RepositoryError("VALIDATION_FAILED",
                                        f"计划 {plan_version} validation 未通过，不能 release")
        with self._lock, self._connect() as db:
            if to_status == "released":
                db.execute("UPDATE m5_plans SET lifecycle_status=?, released_at=?, updated_at=? WHERE plan_version=?",
                           (to_status, _now(), _now(), plan_version))
            else:
                db.execute("UPDATE m5_plans SET lifecycle_status=?, updated_at=? WHERE plan_version=?",
                           (to_status, _now(), plan_version))
            db.execute(
                """INSERT INTO m5_lifecycle
                   (plan_version, from_status, to_status, gate, actor, task_id, trace_id, revision, transitioned_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (plan_version, from_status, to_status, gate, actor, task_id, trace_id, revision, _now()),
            )
        return {"plan_version": plan_version, "from_status": from_status,
                "to_status": to_status, "gate": gate, "actor": actor,
                "task_id": task_id, "trace_id": trace_id, "revision": revision}

    def lifecycle_events(self, plan_version: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM m5_lifecycle WHERE plan_version=? ORDER BY seq", (plan_version,)
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_release(self, plan_version: str, scenario_id: str, *,
                      tenant_id: str = "default", gate: str = "apply",
                      actor: str = "", task_id: str = "", trace_id: str = "",
                      revision: str = "", expected_from: str | None = None,
                      expected_head_revision: int | None = None) -> dict[str, Any]:
        """人工 Apply Gate 的真实发布落库（任务书 T4）。

        在同一个事务边界内完成：draft -> approved -> released（严格相邻迁移；
        已 approved 的计划只做 approved -> released）并把 scenario head CAS 推进。
        - 只接受已持久化的 production plan；非 production 不可 released。
        - release 前校验 validation report 为 pass。
        - 已发布计划（released/dispatched/execution）不可覆盖（PLAN_PROTECTED）。
        - head 预期 revision 不匹配抛 HEAD_CONFLICT，整笔回滚，不留下
          假 released 或错误 head。
        - lifecycle audit 记录 gate/actor/task_id/trace_id/revision/时间。
        """
        now = _now()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m5_plans WHERE plan_version=?", (plan_version,)
            ).fetchone()
            if row is None:
                raise M5RepositoryError("PLAN_NOT_FOUND", f"计划 {plan_version} 不存在")
            plan = dict(row)
            plan["validation_report"] = _loads(row["validation_report_json"])
            from_status = plan["lifecycle_status"]
            if from_status in PROTECTED_STATUSES:
                raise M5RepositoryError("PLAN_PROTECTED", f"已发布计划 {plan_version} 不可覆盖")
            if expected_from is not None and from_status != expected_from:
                raise M5RepositoryError("STALE_REVISION",
                                        f"计划 {plan_version} 当前状态为 {from_status}，期望 {expected_from}")
            if from_status not in {"draft", "approved"}:
                raise M5RepositoryError("ILLEGAL_TRANSITION",
                                        f"{from_status} -> released 不是合法 Apply Gate 迁移")
            if plan.get("scenario_purpose") != "production":
                raise M5RepositoryError("PURPOSE_NOT_RELEASABLE",
                                        f"非 production 计划 {plan_version} 不能发布")
            report = plan.get("validation_report") or {}
            if report.get("status") != "pass":
                raise M5RepositoryError("VALIDATION_FAILED",
                                        f"计划 {plan_version} validation 未通过，不能 release")
            head_row = db.execute(
                "SELECT revision, head_plan_version FROM m5_heads WHERE scenario_id=?",
                (scenario_id,),
            ).fetchone()
            current_head_rev = int(head_row["revision"]) if head_row else 0
            if expected_head_revision is not None and current_head_rev != expected_head_revision:
                raise M5RepositoryError(
                    "HEAD_CONFLICT",
                    f"scenario {scenario_id} head 已被更新（当前 revision={current_head_rev}，期望 {expected_head_revision}）")
            new_head_rev = current_head_rev + 1
            if from_status == "draft":
                db.execute(
                    "UPDATE m5_plans SET lifecycle_status='approved', updated_at=? WHERE plan_version=?",
                    (now, plan_version),
                )
                db.execute(
                    """INSERT INTO m5_lifecycle
                       (plan_version, from_status, to_status, gate, actor, task_id, trace_id, revision, transitioned_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (plan_version, "draft", "approved", f"{gate}:review", actor, task_id,
                     trace_id, revision, now),
                )
            db.execute(
                "UPDATE m5_plans SET lifecycle_status='released', released_at=?, updated_at=? WHERE plan_version=?",
                (now, now, plan_version),
            )
            db.execute(
                """INSERT INTO m5_lifecycle
                   (plan_version, from_status, to_status, gate, actor, task_id, trace_id, revision, transitioned_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (plan_version, "approved", "released", gate, actor, task_id, trace_id, revision, now),
            )
            db.execute(
                """INSERT INTO m5_heads (scenario_id, tenant_id, head_plan_version, revision, updated_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(scenario_id) DO UPDATE SET
                     head_plan_version=excluded.head_plan_version,
                     revision=excluded.revision,
                     updated_at=excluded.updated_at""",
                (scenario_id, tenant_id, plan_version, new_head_rev, now),
            )
        return {
            "plan_version": plan_version,
            "scenario_id": scenario_id,
            "to_status": "released",
            "gate": gate,
            "actor": actor,
            "task_id": task_id,
            "trace_id": trace_id,
            "revision": revision,
            "head_revision": new_head_rev,
            "transitioned_steps": ("draft", "approved", "released"),
        }

    def readback_after_release(self, plan_version: str, scenario_id: str) -> dict[str, Any]:
        """发布后回读 plan/lifecycle/head（任务书 T4.7 readback）。"""
        plan = self.get_plan(plan_version)
        if plan is None:
            raise M5RepositoryError("PLAN_NOT_FOUND", f"计划 {plan_version} 不存在")
        head = self.get_head(scenario_id)
        return {
            "plan_version": plan_version,
            "scenario_id": scenario_id,
            "lifecycle_status": plan.get("lifecycle_status"),
            "parent_plan_version": plan.get("parent_plan_version"),
            "input_hash": plan.get("input_hash"),
            "validator": plan.get("validation_report"),
            "head_revision": (head or {}).get("revision"),
            "head_plan_version": (head or {}).get("head_plan_version"),
            "lifecycle_events": self.lifecycle_events(plan_version),
        }

    # ------------------------------------------------------------------
    # dispatch (Task 3/5): durable pending dispatch
    # ------------------------------------------------------------------

    def create_dispatch(self, *, dispatch_id: str, plan_version: str, tenant_id: str,
                        task_id: str, idempotency_key: str, operation_keys: list[str],
                        target_system: str = "mes", payload: dict[str, Any] | None = None,
                        requested_by: str = "") -> dict[str, Any]:
        now = _now()
        plan = self.get_plan(plan_version)
        if plan is None:
            raise M5RepositoryError("PLAN_NOT_FOUND", f"计划 {plan_version} 不存在")
        if plan["lifecycle_status"] != "released":
            raise M5RepositoryError("NOT_RELEASED",
                                    f"计划 {plan_version} 未发布（当前 {plan['lifecycle_status']}），不能派工")
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT dispatch_id, status, operation_keys_json FROM m5_dispatch WHERE plan_version=? AND idempotency_key=?",
                (plan_version, idempotency_key),
            ).fetchone()
            if row is not None:
                return {
                    "dispatch_id": row["dispatch_id"], "plan_version": plan_version,
                    "idempotency_key": idempotency_key, "status": row["status"],
                    "operation_keys": _loads(row["operation_keys_json"]) or [],
                    "replayed": True,
                }
            db.execute(
                """INSERT INTO m5_dispatch
                   (dispatch_id, plan_version, tenant_id, task_id, idempotency_key,
                    operation_keys_json, target_system, status, payload_json, requested_by,
                    attempts, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dispatch_id, plan_version, tenant_id, task_id, idempotency_key,
                 _json(list(operation_keys)), target_system, "pending",
                 _json(payload or {}), requested_by, 0, now, now),
            )
        return {
            "dispatch_id": dispatch_id, "plan_version": plan_version,
            "idempotency_key": idempotency_key, "status": "pending",
            "operation_keys": list(operation_keys), "target_system": target_system,
            "replayed": False,
        }

    def get_dispatch(self, dispatch_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m5_dispatch WHERE dispatch_id=?", (dispatch_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["operation_keys"] = _loads(row["operation_keys_json"]) or []
        out["payload"] = _loads(row["payload_json"]) or {}
        return out

    def list_dispatch(self, plan_version: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM m5_dispatch WHERE plan_version=? ORDER BY created_at", (plan_version,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # execution events (Task 5)
    # ------------------------------------------------------------------

    def add_execution_event(self, *, event: dict[str, Any], plan_version: str,
                            tenant_id: str = "default", task_id: str = "") -> dict[str, Any]:
        event_id = str(event.get("event_id") or event.get("external_ref") or "")
        if not event_id:
            raise M5RepositoryError("MISSING_EVENT_ID", "execution event 需要 event_id")
        now = _now()
        existing = self.get_execution_event(event_id)
        if existing is not None:
            return {**existing, "replayed": True}
        keys = ("event_type", "order_id", "operation_id", "external_ref")
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO m5_execution_events
                   (event_id, plan_version, tenant_id, task_id, order_id, operation_id,
                    event_type, source, external_ref, occurred_at, worker_id, team_id,
                    station, reported_quantity, scrap_quantity, status, reason,
                    source_kind, event_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, plan_version, tenant_id, task_id,
                 str(event.get("order_id") or ""), str(event.get("operation_id") or ""),
                 str(event.get("event_type") or ""), str(event.get("source") or "manual"),
                 str(event.get("external_ref") or event_id),
                 str(event.get("occurred_at") or now), str(event.get("worker_id") or ""),
                 str(event.get("team_id") or ""), str(event.get("station") or ""),
                 str(event.get("reported_quantity") or ""), str(event.get("scrap_quantity") or ""),
                 str(event.get("status") or "accepted"), str(event.get("reason") or ""),
                 str(event.get("source_kind") or "accepted_execution_event"),
                 _json({k: v for k, v in event.items() if k not in keys}), now),
            )
        return {**event, "event_id": event_id, "plan_version": plan_version,
                "tenant_id": tenant_id, "task_id": task_id, "replayed": False}

    def get_execution_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m5_execution_events WHERE event_id=?", (event_id,)).fetchone()
        return self._decode_event(row) if row else None

    @staticmethod
    def _decode_event(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        extra = _loads(row["event_json"]) or {}
        out.pop("event_json", None)
        return {**extra, **out}

    def list_execution_events(self, plan_version: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM m5_execution_events WHERE plan_version=? ORDER BY occurred_at, event_id",
                (plan_version,),
            ).fetchall()
        return [self._decode_event(row) for row in rows]

    # ------------------------------------------------------------------
    # department messages / outbox (Task 5)
    # ------------------------------------------------------------------

    def create_message(self, *, draft_id: str, tenant_id: str, task_id: str,
                       idempotency_key: str, department: str, channel: str,
                       recipient_targets: list[str], message_kind: str,
                       subject: str, body: str, event_summary: str,
                       required_action: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT draft_id, status FROM m5_messages WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if row is not None:
                return {"draft_id": row["draft_id"], "status": row["status"], "replayed": True}
            db.execute(
                """INSERT INTO m5_messages
                   (draft_id, tenant_id, task_id, idempotency_key, department, channel,
                    recipient_targets_json, message_kind, subject, body, event_summary,
                    required_action, evidence_json, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (draft_id, tenant_id, task_id, idempotency_key, department, channel,
                 _json(list(recipient_targets)), message_kind, subject, body,
                 event_summary, required_action, _json(list(evidence)),
                 "pending_approval", now, now),
            )
        return {"draft_id": draft_id, "status": "pending_approval", "replayed": False}

    def get_message(self, draft_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m5_messages WHERE draft_id=?", (draft_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["recipient_targets"] = _loads(row["recipient_targets_json"]) or []
        out["evidence"] = _loads(row["evidence_json"]) or []
        return out

    def approve_message(self, draft_id: str, *, actor: str = "") -> dict[str, Any]:
        """pending_approval -> approved and enqueue into the durable outbox."""
        now = _now()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m5_messages WHERE draft_id=?", (draft_id,)).fetchone()
            if row is None:
                raise M5RepositoryError("MESSAGE_NOT_FOUND", f"消息 {draft_id} 不存在")
            if row["status"] != "pending_approval":
                raise M5RepositoryError("ILLEGAL_MESSAGE_STATE",
                                        f"消息 {draft_id} 当前状态 {row['status']}，不能审批")
            db.execute(
                "UPDATE m5_messages SET status='approved', updated_at=? WHERE draft_id=?",
                (now, draft_id),
            )
            outbox_id = f"OUTBOX-{draft_id}"
            db.execute(
                """INSERT OR IGNORE INTO m5_outbox (outbox_id, draft_id, tenant_id, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (outbox_id, draft_id, row["tenant_id"], "pending", now, now),
            )
        return {"draft_id": draft_id, "status": "approved", "outbox_id": outbox_id}

    def get_outbox(self, outbox_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m5_outbox WHERE outbox_id=?", (outbox_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # knowledge (Task 5)
    # ------------------------------------------------------------------

    def record_knowledge(self, *, knowledge_id: str, scenario_id: str, plan_version: str,
                         tenant_id: str = "default", task_id: str = "",
                         plan_facts: dict[str, Any] | None = None,
                         tags: list[str] | None = None, note: str = "",
                         features: dict[str, Any] | None = None) -> dict[str, Any]:
        """Persist a knowledge case derived from an authoritative plan version."""
        now = _now()
        existing = self.get_knowledge(knowledge_id)
        if existing is not None:
            return {**existing, "replayed": True}
        facts = plan_facts or {}
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO m5_knowledge
                   (knowledge_id, scenario_id, plan_version, tenant_id, task_id, input_sha256,
                    outcome, solver_status, on_time_rate, total_tardiness_minutes,
                    validation_passed, tags_json, features_json, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (knowledge_id, scenario_id, plan_version, tenant_id, task_id,
                 str(facts.get("input_hash") or ""), str(facts.get("outcome") or "recorded"),
                 str(facts.get("solver_status") or ""), str(facts.get("on_time_rate") or ""),
                 str(facts.get("total_tardiness_minutes") or ""),
                 1 if facts.get("validation_passed") else 0,
                 _json(list(tags or [])), _json(features or {}), note, now),
            )
        return {
            "knowledge_id": knowledge_id, "scenario_id": scenario_id,
            "plan_version": plan_version, "tenant_id": tenant_id, "task_id": task_id,
            "tags": list(tags or []), "note": note, "replayed": False,
        }

    def get_knowledge(self, knowledge_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m5_knowledge WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["tags"] = _loads(row["tags_json"]) or []
        out["features"] = _loads(row["features_json"]) or {}
        return out

    def list_knowledge(self, *, tag_filter: str | None = None,
                       outcome_filter: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if outcome_filter:
            clauses.append("outcome=?")
            params.append(outcome_filter)
        sql = "SELECT * FROM m5_knowledge"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT 200"
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["tags"] = _loads(row["tags_json"]) or []
            item["features"] = _loads(row["features_json"]) or {}
            out.append(item)
        return out

    # ------------------------------------------------------------------
    # procurement proposals (Task 5)
    # ------------------------------------------------------------------

    def save_procurement_proposal(self, *, proposal_id: str, scenario_id: str,
                                  tenant_id: str = "default", task_id: str = "",
                                  idempotency_key: str = "", plan_version: str = "",
                                  items: list[dict[str, Any]]) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as db:
            if idempotency_key:
                row = db.execute(
                    "SELECT proposal_id, status, items_json FROM m5_procurement_proposals WHERE scenario_id=? AND idempotency_key=?",
                    (scenario_id, idempotency_key),
                ).fetchone()
                if row is not None:
                    return {"proposal_id": row["proposal_id"], "status": row["status"],
                            "items": _loads(row["items_json"]) or [], "replayed": True}
            db.execute(
                """INSERT INTO m5_procurement_proposals
                   (proposal_id, scenario_id, plan_version, tenant_id, task_id, idempotency_key,
                    status, items_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (proposal_id, scenario_id, plan_version, tenant_id, task_id, idempotency_key,
                 "proposal", _json(list(items)), now),
            )
        return {"proposal_id": proposal_id, "status": "proposal", "items": items,
                "replayed": False}

    def close(self) -> None:
        pass
