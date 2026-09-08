"""canonical 落库：确定性校验 + 身份 PII 脱敏 + sha256 幂等 + 自描述 canonical 表。

agent 负责「理解 + 映射」，这里负责「守门」：schema 校验（字段合法/必填齐全/
类型正确）、身份类 PII 遮罩、sha256 幂等（同文件不重复建行）。数值型业务字段
（数量/单价/库存等）不遮罩，供后续模块计算。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .canonical_schema import required_fields, validate_canonical
from .recognized_store import redact_row


def _default_db_path() -> str:
    return os.getenv("YUNPAI_CANONICAL_DB", "runtime/yunpai-canonical.sqlite")


class CanonicalLandingStore:
    """本地 canonical 落库（sandbox/测试）；生产 transport 下由 HTTP M0 facade 接管。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = str(db_path or _default_db_path())
        if os.path.dirname(self.db_path):
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as db:
            # 租户迁移（2026-09-07）：记录表补 tenant_id。SQLite 无法原地改
            # UNIQUE，检出无 tenant_id 的老表时建新表回填 'default' 再删旧表。
            records_ddl = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='canonical_records'"
            ).fetchone()
            legacy_records = records_ddl is not None and "tenant_id" not in str(records_ddl["sql"])
            if legacy_records:
                db.execute("ALTER TABLE canonical_records RENAME TO canonical_records_pre_tenant")
            db.execute(
                """CREATE TABLE IF NOT EXISTS canonical_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    entity_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            if legacy_records:
                db.execute(
                    """INSERT INTO canonical_records(tenant_id, entity_type, business_key, filename, sha256,
                                                     payload, confidence, created_at)
                       SELECT 'default', entity_type, business_key, filename, sha256,
                              payload, confidence, created_at
                       FROM canonical_records_pre_tenant"""
                )
                db.execute("DROP TABLE canonical_records_pre_tenant")
            # 幂等索引扩为 (tenant_id, sha256)：判重语义按租户。
            db.execute("CREATE INDEX IF NOT EXISTS idx_canonical_tenant_sha ON canonical_records(tenant_id, sha256)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_canonical_entity ON canonical_records(entity_type)")

    def ingest(self, *, entity_type: str, records: list[dict[str, Any]], filename: str,
               sha256: str, confidence: float, redact: bool = True,
               tenant_id: str = "default") -> dict[str, Any]:
        """校验 + 脱敏 + 落库；同租户同 sha256 幂等。返回 {success, data, errors}。"""
        tenant = str(tenant_id or "default")
        if not sha256 or len(sha256) != 64:
            return {"success": False, "code": "INVALID_SHA256", "errors": [{"code": "INVALID_SHA256", "message": "sha256 必须是 64 位十六进制"}], "data": {}}
        validated = validate_canonical(entity_type, records)
        if validated["errors"]:
            return {"success": False, "code": "SCHEMA_INVALID", "errors": validated["errors"], "data": {}}
        identity_field = required_fields(entity_type)[0] if required_fields(entity_type) else ""
        stored: list[dict[str, Any]] = []
        redacted_count = 0
        for clean in validated["clean_records"]:
            before = json.dumps(clean, ensure_ascii=False, sort_keys=True)
            out = redact_row(clean) if redact else dict(clean)
            if json.dumps(out, ensure_ascii=False, sort_keys=True) != before:
                redacted_count += 1
            stored.append(out)
        created = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM canonical_records WHERE tenant_id=? AND sha256=?", (tenant, sha256)
            ).fetchone()
            if exists:
                return {"success": True, "data": {"inserted_rows": 0, "duplicate": True, "sha256": sha256, "entity_type": entity_type, "redacted_fields": 0, "tenant_id": tenant}, "errors": []}
            for out in stored:
                business_key = str(out.get(identity_field) or "") if identity_field else ""
                db.execute(
                    "INSERT INTO canonical_records(tenant_id, entity_type, business_key, filename, sha256, payload, confidence, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (tenant, entity_type, business_key, filename, sha256, json.dumps(out, ensure_ascii=False), float(confidence), created),
                )
        return {"success": True, "data": {"inserted_rows": len(stored), "duplicate": False, "sha256": sha256, "entity_type": entity_type, "redacted_fields": redacted_count, "tenant_id": tenant, "clean_records": validated["clean_records"]}, "errors": []}

    def query(self, *, entity_type: str | None = None, limit: int = 200,
              tenant_id: str = "default") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with self._connect() as db:
            sql = ("SELECT tenant_id, entity_type, business_key, filename, sha256, payload, confidence, created_at "
                   "FROM canonical_records WHERE tenant_id=?")
            params: list[Any] = [str(tenant_id or "default")]
            if entity_type:
                sql += " AND entity_type=?"
                params.append(entity_type)
            sql += " ORDER BY id LIMIT ?"
            params.append(int(limit))
            for row in db.execute(sql, params).fetchall():
                payload = json.loads(row["payload"])
                rows.append({"tenant_id": row["tenant_id"], "entity_type": row["entity_type"], "business_key": row["business_key"], "filename": row["filename"], "sha256": row["sha256"], "confidence": row["confidence"], **payload})
        return rows


def to_m0_records(entity_type: str, clean_records: list[dict[str, Any]], *, filename: str,
                  sha256: str, tenant_id: str, actor: str = "operator") -> list[dict[str, Any]]:
    """把校验后的 canonical 记录转成 m0.ingest.v1 记录（供 M0 canonical 发布）。"""
    identity_field = required_fields(entity_type)[0] if required_fields(entity_type) else ""
    records: list[dict[str, Any]] = []
    for record in clean_records:
        business_key = str(record.get(identity_field) or "") if identity_field else ""
        records.append({
            "schema_version": "m0.ingest.v1",
            "tenant_id": tenant_id,
            "idempotency_key": f"upload:{sha256}:{entity_type}:{business_key}",
            "source": {"system": "yunpai-agent-recognition", "external_id": f"{filename}::{entity_type}::{business_key}", "sha256": sha256},
            "identity": {"business_key": business_key, "version_id": ""},
            "evidence": [{"key": "source", "locator": {"filename": filename, "sha256": sha256}, "excerpt": filename}],
            "review_status": "approved",
            "reviewed_by": actor,
            "entity_type": entity_type,
            "payload": record,
        })
    return records
