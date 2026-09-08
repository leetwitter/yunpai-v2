from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

# 候选状态机：observed -> proposed -> active(经 knowledge_items) / rejected；
# active 经 suspend/deprecate 进入治理台处理；纠正 = 新版本。
CANDIDATE_STATUSES = ("observed", "proposed", "active", "suspended", "rejected", "deprecated")
KNOWLEDGE_STATUSES = ("active", "suspended", "deprecated")

# 信号/候选 kind（与设计文稿 §4.2 一致）。
KINDS = (
    "repeated_operation", "error_pattern", "success_mapping",
    "human_correction", "field_meaning", "scheduling_case", "profile_field",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


class EvolutionRepository:
    """演进层唯一持久化仓库（SQLite，一体机单机；PG migration 可后补）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------
    def record_source(self, *, source_id: str, tenant_id: str, source_kind: str,
                      run_id: str = "", task_id: str = "", trace_id: str = "",
                      document_id: str = "", entity_ref: str = "",
                      locator: dict[str, Any] | None = None,
                      content_hash: str = "") -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO ok_sources
                   (source_id, tenant_id, source_kind, run_id, task_id, trace_id,
                    document_id, entity_ref, locator_json, content_hash, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (source_id, tenant_id, source_kind, run_id, task_id, trace_id,
                 document_id, entity_ref, _json(locator or {}), content_hash, _now()),
            )
        return self.get_source(source_id) or {}

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM ok_sources WHERE source_id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # candidates
    # ------------------------------------------------------------------
    def _candidate_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["content"] = _loads(row["content_json"], {})
        item["applicability"] = _loads(row["applicability_json"], {})
        return item

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM ok_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        return self._candidate_row(row) if row else None

    def find_candidate(self, *, tenant_id: str, kind: str, fingerprint: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM ok_candidates WHERE tenant_id=? AND kind=? AND fingerprint=?",
                (tenant_id, kind, fingerprint),
            ).fetchone()
        return self._candidate_row(row) if row else None

    def upsert_candidate(self, *, candidate_id: str, tenant_id: str, kind: str,
                         fingerprint: str, content: dict[str, Any],
                         applicability: dict[str, Any],
                         scope: str = "tenant", last_error: str = "") -> dict[str, Any]:
        """按 (tenant_id, kind, fingerprint) 自然聚合；已存在则复用旧行。"""
        existing = self.find_candidate(tenant_id=tenant_id, kind=kind, fingerprint=fingerprint)
        if existing is not None:
            return existing
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO ok_candidates
                   (candidate_id, tenant_id, kind, fingerprint, content_json,
                    applicability_json, status, independent_count, support_count,
                    contradiction_count, last_error, proposed_at, scope, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,0,0,0,?, '', ?, ?, ?)""",
                (candidate_id, tenant_id, kind, fingerprint, _json(content),
                 _json(applicability), "observed", last_error, scope, now, now),
            )
        return self.get_candidate(candidate_id) or {}

    def add_candidate_source(self, *, candidate_id: str, source_id: str,
                             tenant_id: str, task_id: str) -> dict[str, Any]:
        """登记候选证据来源；独立性 = COUNT(DISTINCT task_id)。幂等。"""
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO ok_candidate_sources (candidate_id, source_id, tenant_id, task_id, created_at) VALUES (?,?,?,?,?)",
                (candidate_id, source_id, tenant_id, task_id, _now()),
            )
            row = db.execute(
                "SELECT COUNT(*) AS support, COUNT(DISTINCT task_id) AS independent FROM ok_candidate_sources WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            support, independent = int(row["support"]), int(row["independent"])
            db.execute(
                "UPDATE ok_candidates SET support_count=?, independent_count=?, updated_at=? WHERE candidate_id=?",
                (support, independent, _now(), candidate_id),
            )
        return {"candidate_id": candidate_id, "independent_count": independent, "support_count": support}

    def set_candidate_status(self, candidate_id: str, status: str, *, last_error: str = "") -> dict[str, Any] | None:
        if status not in CANDIDATE_STATUSES:
            raise ValueError(f"unknown candidate status: {status}")
        now = _now()
        assignments = ["status=?", "updated_at=?"]
        params: list[Any] = [status, now]
        if status == "proposed":
            assignments.append("proposed_at=?")
            params.append(now)
        if last_error:
            assignments.append("last_error=?")
            params.append(last_error)
        params.append(candidate_id)
        sql = "UPDATE ok_candidates SET " + ", ".join(assignments) + " WHERE candidate_id=?"
        with self._lock, self._connect() as db:
            db.execute(sql, params)
        return self.get_candidate(candidate_id)

    def list_candidates(self, *, tenant_id: str = "default", status: str | None = None,
                        kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM ok_candidates WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._candidate_row(row) for row in rows]

    def list_candidate_sources(self, candidate_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM ok_candidate_sources WHERE candidate_id=? ORDER BY created_at",
                (candidate_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_pending(self, *, tenant_id: str = "default", limit: int = 50) -> list[dict[str, Any]]:
        return self.list_candidates(tenant_id=tenant_id, status="proposed", limit=limit)

    # ------------------------------------------------------------------
    # knowledge items + actions
    # ------------------------------------------------------------------
    def create_knowledge(self, *, knowledge_id: str, tenant_id: str, candidate_id: str,
                         version: int, kind: str, content: dict[str, Any],
                         applicability: dict[str, Any], scope: str,
                         approved_by: str, supersedes: str = "") -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO ok_knowledge_items
                   (knowledge_id, tenant_id, candidate_id, version, kind, content_json,
                    applicability_json, scope, status, approved_by, approved_at,
                    supersedes, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (knowledge_id, tenant_id, candidate_id, version, kind, _json(content),
                 _json(applicability), scope, "active", approved_by, _now(), supersedes, _now()),
            )
        return self.get_knowledge(knowledge_id) or {}

    def get_knowledge(self, knowledge_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM ok_knowledge_items WHERE knowledge_id=?", (knowledge_id,)).fetchone()
        return self._knowledge_row(row) if row else None

    @staticmethod
    def _knowledge_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["content"] = _loads(row["content_json"], {})
        item["applicability"] = _loads(row["applicability_json"], {})
        return item

    def next_knowledge_version(self, candidate_id: str) -> int:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(version), 0) AS m FROM ok_knowledge_items WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        return int(row["m"]) + 1

    def knowledge_by_candidate(self, candidate_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM ok_knowledge_items WHERE candidate_id=? ORDER BY version DESC",
                (candidate_id,),
            ).fetchall()
        return [self._knowledge_row(row) for row in rows]

    def list_knowledge(self, *, tenant_id: str = "default", kind: str | None = None,
                       status: str = "active", limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM ok_knowledge_items WHERE tenant_id=? AND status=?"
        params: list[Any] = [tenant_id, status]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._knowledge_row(row) for row in rows]

    def set_knowledge_status(self, knowledge_id: str, status: str) -> dict[str, Any] | None:
        if status not in KNOWLEDGE_STATUSES:
            raise ValueError(f"unknown knowledge status: {status}")
        with self._lock, self._connect() as db:
            db.execute("UPDATE ok_knowledge_items SET status=? WHERE knowledge_id=?", (status, knowledge_id))
        return self.get_knowledge(knowledge_id)

    def record_action(self, *, action_id: str, tenant_id: str, knowledge_id: str,
                      candidate_id: str, action: str, actor: str, reason: str = "",
                      payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO ok_knowledge_actions
                   (action_id, tenant_id, knowledge_id, candidate_id, action, actor, reason, payload_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (action_id, tenant_id, knowledge_id, candidate_id, action, actor,
                 reason, _json(payload or {}), _now()),
            )
        return {"action_id": action_id, "action": action, "actor": actor}

    def list_actions(self, *, candidate_id: str = "", knowledge_id: str = "",
                     limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if candidate_id:
            clauses.append("candidate_id=?")
            params.append(candidate_id)
        if knowledge_id:
            clauses.append("knowledge_id=?")
            params.append(knowledge_id)
        sql = "SELECT * FROM ok_knowledge_actions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # usage
    # ------------------------------------------------------------------
    def record_usage(self, *, usage_id: str, tenant_id: str, knowledge_id: str,
                     run_id: str, task_id: str, outcome: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO ok_knowledge_usage
                   (usage_id, tenant_id, knowledge_id, run_id, task_id, used_at, outcome)
                   VALUES (?,?,?,?,?,?,?)""",
                (usage_id, tenant_id, knowledge_id, run_id, task_id, _now(), outcome),
            )
        # 反例达到阈值（默认 2 次）自动 suspend。
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM ok_knowledge_usage WHERE knowledge_id=? AND outcome='contradicted'",
                (knowledge_id,),
            ).fetchone()
        if int(row["c"]) >= 2:
            self.set_knowledge_status(knowledge_id, "suspended")
        return {"usage_id": usage_id, "outcome": outcome}

    # ------------------------------------------------------------------
    # operation patterns + shortcuts
    # ------------------------------------------------------------------
    def upsert_pattern(self, *, pattern_id: str, tenant_id: str, steps_digest: str,
                       tools: list[str], params_template: dict[str, Any],
                       trigger: dict[str, Any]) -> dict[str, Any]:
        existing = self.find_pattern(tenant_id=tenant_id, steps_digest=steps_digest)
        if existing is not None:
            return existing
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO ok_operation_patterns
                   (pattern_id, tenant_id, steps_digest, tools_json, params_template_json,
                    trigger_json, independent_count, avg_steps_saved, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,0,0,'observed',?,?)""",
                (pattern_id, tenant_id, steps_digest, _json(tools), _json(params_template),
                 _json(trigger), _now(), _now()),
            )
        return self.get_pattern(pattern_id) or {}

    def find_pattern(self, *, tenant_id: str, steps_digest: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM ok_operation_patterns WHERE tenant_id=? AND steps_digest=?",
                (tenant_id, steps_digest),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["tools"] = _loads(row["tools_json"], [])
        item["params_template"] = _loads(row["params_template_json"], {})
        item["trigger"] = _loads(row["trigger_json"], {})
        return item

    def get_pattern(self, pattern_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM ok_operation_patterns WHERE pattern_id=?", (pattern_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["tools"] = _loads(row["tools_json"], [])
        item["params_template"] = _loads(row["params_template_json"], {})
        item["trigger"] = _loads(row["trigger_json"], {})
        return item

    def list_patterns(self, *, tenant_id: str = "default", status: str | None = None,
                      limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM ok_operation_patterns WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["tools"] = _loads(row["tools_json"], [])
            item["params_template"] = _loads(row["params_template_json"], {})
            item["trigger"] = _loads(row["trigger_json"], {})
            out.append(item)
        return out

    def increment_pattern(self, pattern_id: str, *, independent_delta: int = 0) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE ok_operation_patterns SET independent_count=independent_count+?, updated_at=? WHERE pattern_id=?",
                (independent_delta, _now(), pattern_id),
            )

    def create_shortcut(self, *, shortcut_id: str, tenant_id: str, pattern_id: str,
                        title: str, trigger: dict[str, Any], steps: list[dict[str, Any]],
                        pinned_by: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO ok_shortcuts
                   (shortcut_id, tenant_id, pattern_id, title, trigger_json, steps_json,
                    pinned_by, use_count, last_used_at, status, created_at)
                   VALUES (?,?,?,?,?,?,?,0,'','active',?)""",
                (shortcut_id, tenant_id, pattern_id, title, _json(trigger), _json(steps),
                 pinned_by, _now()),
            )
        return self.get_shortcut(shortcut_id) or {}

    def get_shortcut(self, shortcut_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM ok_shortcuts WHERE shortcut_id=?", (shortcut_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["trigger"] = _loads(row["trigger_json"], {})
        item["steps"] = _loads(row["steps_json"], [])
        return item

    def list_shortcuts(self, *, tenant_id: str = "default", status: str = "active",
                       limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM ok_shortcuts WHERE tenant_id=? AND status=? ORDER BY use_count DESC, created_at DESC LIMIT ?",
                (tenant_id, status, max(1, min(limit, 1000))),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["trigger"] = _loads(row["trigger_json"], {})
            item["steps"] = _loads(row["steps_json"], [])
            out.append(item)
        return out

    def record_shortcut_use(self, shortcut_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE ok_shortcuts SET use_count=use_count+1, last_used_at=? WHERE shortcut_id=?",
                (_now(), shortcut_id),
            )

    def disable_shortcut(self, shortcut_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE ok_shortcuts SET status='disabled' WHERE shortcut_id=?", (shortcut_id,))

    # ------------------------------------------------------------------
    # product profiles（产品理解 U2/U4）
    # ------------------------------------------------------------------
    def upsert_profile(self, *, profile_id: str, tenant_id: str, product_code: str,
                       product_name: str | None = None, category: str | None = None,
                       materials: list[dict[str, Any]] | None = None,
                       process: list[dict[str, Any]] | None = None,
                       geometry: dict[str, Any] | None = None,
                       facts: dict[str, Any] | None = None,
                       crosscheck: dict[str, Any] | None = None,
                       confidence: float | None = None, version: int = 0) -> dict[str, Any]:
        # 版本由调用方显式递增；None 字段保留已有值（避免「只更新 process 却把
        # materials 覆盖成空」这类局部更新事故）。
        existing = self.get_profile_by_id(profile_id)
        product_name_v = product_name if product_name is not None else (existing["product_name"] if existing else "")
        category_v = category if category is not None else (existing["category"] if existing else "")
        materials_v = materials if materials is not None else (existing["materials"] if existing else [])
        process_v = process if process is not None else (existing["process"] if existing else [])
        geometry_v = geometry if geometry is not None else (existing["geometry"] if existing else {})
        facts_v = facts if facts is not None else (existing["facts"] if existing else {})
        crosscheck_v = crosscheck if crosscheck is not None else (existing["crosscheck"] if existing else {})
        confidence_v = confidence if confidence is not None else (existing["confidence"] if existing else 0.0)
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO pu_product_profiles
                   (profile_id, tenant_id, product_code, product_name, category,
                    materials_json, process_json, geometry_json, facts_json,
                    crosscheck_json, confidence, version, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(profile_id) DO UPDATE SET
                    product_name=excluded.product_name, category=excluded.category,
                    materials_json=excluded.materials_json, process_json=excluded.process_json,
                    geometry_json=excluded.geometry_json, facts_json=excluded.facts_json,
                    crosscheck_json=excluded.crosscheck_json, confidence=excluded.confidence,
                    version=excluded.version, updated_at=excluded.updated_at""",
                (profile_id, tenant_id, product_code, product_name_v, category_v,
                 _json(materials_v), _json(process_v), _json(geometry_v),
                 _json(facts_v), _json(crosscheck_v), float(confidence_v or 0.0),
                 int(version), _now()),
            )
        return self.get_profile_by_id(profile_id) or {}

    def get_profile(self, *, tenant_id: str, product_code: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM pu_product_profiles WHERE tenant_id=? AND product_code=?",
                (tenant_id, product_code),
            ).fetchone()
        return self._profile_row(row) if row else None

    def get_profile_by_id(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM pu_product_profiles WHERE profile_id=?", (profile_id,)).fetchone()
        return self._profile_row(row) if row else None

    @staticmethod
    def _profile_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["materials"] = _loads(row["materials_json"], [])
        item["process"] = _loads(row["process_json"], [])
        item["geometry"] = _loads(row["geometry_json"], {})
        item["facts"] = _loads(row["facts_json"], {})
        item["crosscheck"] = _loads(row["crosscheck_json"], {})
        return item

    def list_profiles(self, *, tenant_id: str = "default", limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM pu_product_profiles WHERE tenant_id=? ORDER BY updated_at DESC LIMIT ?",
                (tenant_id, max(1, min(limit, 1000))),
            ).fetchall()
        return [self._profile_row(row) for row in rows]

    def add_field_evidence(self, *, evidence_id: str, tenant_id: str, profile_id: str,
                           field_path: str, value: Any, source_kind: str,
                           document_ref: str, locator: dict[str, Any] | None = None,
                           status: str = "candidate") -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO pu_field_evidence
                   (id, tenant_id, profile_id, field_path, value_json, source_kind,
                    document_ref, locator_json, independent_count, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,1,?,?)""",
                (evidence_id, tenant_id, profile_id, field_path, _json(value),
                 source_kind, document_ref, _json(locator or {}), status, _now()),
            )
        return self.get_field_evidence(evidence_id) or {}

    def get_field_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM pu_field_evidence WHERE id=?", (evidence_id,)).fetchone()
        return self._evidence_row(row) if row else None

    @staticmethod
    def _evidence_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["value"] = _loads(row["value_json"], None)
        item["locator"] = _loads(row["locator_json"], {})
        return item

    def list_field_evidence(self, *, profile_id: str, field_path: str | None = None,
                            limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM pu_field_evidence WHERE profile_id=?"
        params: list[Any] = [profile_id]
        if field_path:
            sql += " AND field_path=?"
            params.append(field_path)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._lock, self._connect() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._evidence_row(row) for row in rows]

    def count_field_evidence(self, *, profile_id: str, field_path: str | None = None) -> int:
        """字段证据计数（压测/指标用，不受 list 的 1000 条分页限制）。"""
        sql = "SELECT COUNT(*) FROM pu_field_evidence WHERE profile_id=?"
        params: list[Any] = [profile_id]
        if field_path:
            sql += " AND field_path=?"
            params.append(field_path)
        with self._lock, self._connect() as db:
            return int(db.execute(sql, params).fetchone()[0])

    def set_evidence_status(self, evidence_id: str, status: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE pu_field_evidence SET status=? WHERE id=?", (status, evidence_id))
        return self.get_field_evidence(evidence_id)

    def bump_evidence_independence(self, evidence_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE pu_field_evidence SET independent_count=independent_count+1 WHERE id=?",
                (evidence_id,),
            )

    # ------------------------------------------------------------------
    # metrics（治理台）
    # ------------------------------------------------------------------
    def metrics(self, *, tenant_id: str = "default") -> dict[str, Any]:
        def _scalar(sql: str, params: tuple[Any, ...]) -> int:
            with self._lock, self._connect() as db:
                return int(db.execute(sql, params).fetchone()[0])

        return {
            "candidates": {
                "observed": _scalar("SELECT COUNT(*) FROM ok_candidates WHERE tenant_id=? AND status='observed'", (tenant_id,)),
                "proposed": _scalar("SELECT COUNT(*) FROM ok_candidates WHERE tenant_id=? AND status='proposed'", (tenant_id,)),
                "active": _scalar("SELECT COUNT(*) FROM ok_candidates WHERE tenant_id=? AND status='active'", (tenant_id,)),
                "rejected": _scalar("SELECT COUNT(*) FROM ok_candidates WHERE tenant_id=? AND status='rejected'", (tenant_id,)),
            },
            "knowledge_active": _scalar("SELECT COUNT(*) FROM ok_knowledge_items WHERE tenant_id=? AND status='active'", (tenant_id,)),
            "knowledge_suspended": _scalar("SELECT COUNT(*) FROM ok_knowledge_items WHERE tenant_id=? AND status='suspended'", (tenant_id,)),
            "shortcuts": _scalar("SELECT COUNT(*) FROM ok_shortcuts WHERE tenant_id=? AND status='active'", (tenant_id,)),
            "patterns": _scalar("SELECT COUNT(*) FROM ok_operation_patterns WHERE tenant_id=?", (tenant_id,)),
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ok_sources (
    source_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    source_kind TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    trace_id TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL DEFAULT '',
    entity_ref TEXT NOT NULL DEFAULT '',
    locator_json TEXT NOT NULL DEFAULT '{}',
    content_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ok_sources_run ON ok_sources(run_id);
CREATE INDEX IF NOT EXISTS idx_ok_sources_hash ON ok_sources(content_hash);

CREATE TABLE IF NOT EXISTS ok_candidates (
    candidate_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    content_json TEXT NOT NULL DEFAULT '{}',
    applicability_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'observed',
    independent_count INTEGER NOT NULL DEFAULT 0,
    support_count INTEGER NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    proposed_at TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT 'tenant',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, kind, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_ok_candidates_status ON ok_candidates(tenant_id, status, kind);

CREATE TABLE IF NOT EXISTS ok_candidate_sources (
    candidate_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    task_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(candidate_id, source_id)
);

CREATE TABLE IF NOT EXISTS ok_knowledge_items (
    knowledge_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    candidate_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content_json TEXT NOT NULL DEFAULT '{}',
    applicability_json TEXT NOT NULL DEFAULT '{}',
    scope TEXT NOT NULL DEFAULT 'tenant',
    status TEXT NOT NULL DEFAULT 'active',
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    supersedes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ok_items_lookup ON ok_knowledge_items(tenant_id, kind, status);

CREATE TABLE IF NOT EXISTS ok_knowledge_actions (
    action_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    knowledge_id TEXT NOT NULL DEFAULT '',
    candidate_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ok_knowledge_usage (
    usage_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    knowledge_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    used_at TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ok_operation_patterns (
    pattern_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    steps_digest TEXT NOT NULL,
    tools_json TEXT NOT NULL DEFAULT '[]',
    params_template_json TEXT NOT NULL DEFAULT '{}',
    trigger_json TEXT NOT NULL DEFAULT '{}',
    independent_count INTEGER NOT NULL DEFAULT 0,
    avg_steps_saved INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'observed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, steps_digest)
);

CREATE TABLE IF NOT EXISTS ok_shortcuts (
    shortcut_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    pattern_id TEXT NOT NULL,
    title TEXT NOT NULL,
    trigger_json TEXT NOT NULL DEFAULT '{}',
    steps_json TEXT NOT NULL DEFAULT '[]',
    pinned_by TEXT NOT NULL,
    use_count INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pu_product_profiles (
    profile_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    product_code TEXT NOT NULL,
    product_name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    materials_json TEXT NOT NULL DEFAULT '[]',
    process_json TEXT NOT NULL DEFAULT '[]',
    geometry_json TEXT NOT NULL DEFAULT '{}',
    facts_json TEXT NOT NULL DEFAULT '{}',
    crosscheck_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, product_code)
);

CREATE TABLE IF NOT EXISTS pu_field_evidence (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    profile_id TEXT NOT NULL,
    field_path TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    document_ref TEXT NOT NULL,
    locator_json TEXT NOT NULL DEFAULT '{}',
    independent_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pu_evidence_field ON pu_field_evidence(tenant_id, profile_id, field_path);
"""
