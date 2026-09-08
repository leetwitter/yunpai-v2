"""M0 Data Backend（canonical 主数据服务）。

该服务承载上传候选、人工审批、canonical 版本、ledger 和 outbox。
本地默认使用 SQLite 进行可回滚开发；部署到 PostgreSQL 时执行
``migrations/m0_backend_v1.sql``，并把 ``YUNPAI_M0_DB`` 指向专用连接。
它不读取或迁移历史环境（MinerU 等）数据。
"""

import hashlib
import json
import os
import sqlite3
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_record(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    encoded = normalized.get("content_b64")
    if isinstance(encoded, str):
        try:
            decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
            if isinstance(decoded, dict):
                normalized = {**decoded, **normalized}
        except (ValueError, UnicodeDecodeError):
            pass
    return normalized


def _canonical_key_of(data: dict[str, Any], fallback: str) -> str:
    """按实体的稳定身份字段推导 canonical_key，缺省回退 candidate_id。"""
    for field in ("canonical_key", "product_code", "order_id", "material_code",
                  "equipment_code", "station_code", "person_code", "tooling_code",
                  "calendar_ref", "supplier_code"):
        value = data.get(field)
        if value not in (None, ""):
            return str(value)
    return str(fallback)


SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
  batch_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, task_id TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_documents (
  document_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, filename TEXT NOT NULL,
  sha256 TEXT NOT NULL, content_json TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(batch_id) REFERENCES import_batches(batch_id)
);
CREATE TABLE IF NOT EXISTS import_candidates (
  candidate_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, document_id TEXT NOT NULL,
  entity_type TEXT NOT NULL, candidate_json TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, FOREIGN KEY(batch_id) REFERENCES import_batches(batch_id)
);
CREATE TABLE IF NOT EXISTS approval_records (
  approval_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, candidate_id TEXT,
  actor TEXT NOT NULL, decision TEXT NOT NULL, approval_mode TEXT NOT NULL,
  reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_entities (
  entity_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, entity_type TEXT NOT NULL,
  canonical_key TEXT NOT NULL, current_version INTEGER NOT NULL,
  lifecycle_status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, entity_type, canonical_key)
);
CREATE TABLE IF NOT EXISTS canonical_entity_versions (
  entity_id TEXT NOT NULL, version INTEGER NOT NULL, payload_json TEXT NOT NULL,
  checksum TEXT NOT NULL, source_batch_id TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(entity_id, version), FOREIGN KEY(entity_id) REFERENCES canonical_entities(entity_id)
);
CREATE TABLE IF NOT EXISTS canonical_ledger (
  ledger_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, entity_id TEXT NOT NULL,
  version INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
  approval_mode TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_outbox (
  outbox_id TEXT PRIMARY KEY, ledger_id TEXT NOT NULL, event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class M0Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def ingest(self, records: list[dict[str, Any]], *, tenant_id: str, task_id: str) -> dict[str, Any]:
        batch_id = uuid4().hex[:16]
        now = _now()
        with self._connect() as conn:
            conn.execute("INSERT INTO import_batches VALUES (?, ?, ?, ?, ?, ?)",
                         (batch_id, tenant_id, task_id, "awaiting_review", now, now))
            for item in records:
                item = _normalize_record(item)
                filename = str(item.get("filename") or "document.json")
                payload = item.get("records", item)
                raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                document_id = uuid4().hex
                sha256 = hashlib.sha256(raw.encode()).hexdigest()
                candidate_id = uuid4().hex
                conn.execute("INSERT INTO source_documents VALUES (?, ?, ?, ?, ?, ?)",
                             (document_id, batch_id, filename, sha256, raw, now))
                conn.execute("INSERT INTO import_candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (candidate_id, batch_id, document_id, str(item.get("entity_type") or "order"),
                              json.dumps(item, ensure_ascii=False), "candidate", now))
        return {
            "batch_id": batch_id,
            "status": "awaiting_review",
            "candidate_count": len(records),
            "m0_candidate_records": records,
            "candidates": records,
        }

    def publish(self, batch_id: str, *, actor: str, reason: str = "", human_override: bool = False) -> dict[str, Any]:
        with self._connect() as conn:
            batch = conn.execute("SELECT * FROM import_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                return {"status": "not_found", "batch_id": batch_id}
            candidates = conn.execute("SELECT * FROM import_candidates WHERE batch_id=?", (batch_id,)).fetchall()
            published = 0
            for candidate in candidates:
                data = json.loads(candidate["candidate_json"])
                key = _canonical_key_of(data, candidate["candidate_id"])
                entity_type = candidate["entity_type"]
                entity = conn.execute(
                    "SELECT * FROM canonical_entities WHERE tenant_id=? AND entity_type=? AND canonical_key=?",
                    (batch["tenant_id"], entity_type, key),
                ).fetchone()
                entity_id = entity["entity_id"] if entity else uuid4().hex
                version = int(entity["current_version"]) + 1 if entity else 1
                checksum = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if entity:
                    conn.execute("UPDATE canonical_entities SET current_version=?, updated_at=? WHERE entity_id=?",
                                 (version, _now(), entity_id))
                else:
                    conn.execute("INSERT INTO canonical_entities VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (entity_id, batch["tenant_id"], entity_type, key, version, "active", _now(), _now()))
                conn.execute("INSERT INTO canonical_entity_versions VALUES (?, ?, ?, ?, ?, ?)",
                             (entity_id, version, json.dumps(data, ensure_ascii=False), checksum, batch_id, _now()))
                approval_id = uuid4().hex
                mode = "human_override" if human_override else "standard"
                conn.execute("INSERT INTO approval_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (approval_id, batch_id, candidate["candidate_id"], actor, "approve", mode, reason, _now()))
                ledger_id = uuid4().hex
                conn.execute("INSERT INTO canonical_ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (ledger_id, batch_id, entity_id, version, "publish", actor, mode, _now()))
                conn.execute("INSERT INTO canonical_outbox VALUES (?, ?, ?, ?, ?, ?)",
                             (uuid4().hex, ledger_id, "canonical.entity.published",
                              json.dumps({"entity_id": entity_id, "version": version}, ensure_ascii=False),
                              "pending", _now()))
                conn.execute("UPDATE import_candidates SET status='published' WHERE candidate_id=?",
                             (candidate["candidate_id"],))
                published += 1
            conn.execute("UPDATE import_batches SET status='published', updated_at=? WHERE batch_id=?", (_now(), batch_id))
        return self.readback(batch_id) | {"status": "published", "published": published}

    def readback(self, batch_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            entities = conn.execute(
                "SELECT e.entity_id,e.entity_type,e.canonical_key,v.version,v.payload_json,v.checksum "
                "FROM canonical_entities e JOIN canonical_entity_versions v "
                "ON v.entity_id=e.entity_id AND v.version=e.current_version "
                "JOIN canonical_ledger l ON l.entity_id=e.entity_id AND l.version=v.version "
                "WHERE l.batch_id=?", (batch_id,)).fetchall()
            ledger = conn.execute("SELECT COUNT(*) AS n FROM canonical_ledger WHERE batch_id=?", (batch_id,)).fetchone()["n"]
            outbox = conn.execute(
                "SELECT COUNT(*) AS n FROM canonical_outbox WHERE ledger_id IN "
                "(SELECT ledger_id FROM canonical_ledger WHERE batch_id=?)", (batch_id,)).fetchone()["n"]
        return {
            "batch_id": batch_id,
            "canonical_readback_available": True,
            "approved_candidates": len(entities),
            "ledger_count": int(ledger),
            "outbox_count": int(outbox),
            "entities": [dict(row) for row in entities],
        }

    def list_entities(self, entity_type: str, tenant_id: str = "default") -> dict[str, Any]:
        """按 entity_type 读回当前 active 的 canonical 实体（供 M5 资源快照组装）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT e.entity_id,e.entity_type,e.canonical_key,v.version,v.payload_json,v.checksum "
                "FROM canonical_entities e JOIN canonical_entity_versions v "
                "ON v.entity_id=e.entity_id AND v.version=e.current_version "
                "WHERE e.entity_type=? AND e.tenant_id=? AND e.lifecycle_status='active' "
                "ORDER BY e.canonical_key",
                (entity_type, tenant_id),
            ).fetchall()
        entities: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            payload = item.get("payload_json")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            item["payload_json"] = payload
            entities.append(item)
        return {
            "entity_type": entity_type,
            "tenant_id": tenant_id,
            "count": len(entities),
            "entities": entities,
        }


class PostgresM0Store:
    """PostgreSQL implementation using the same M0 table contract."""

    def __init__(self, dsn: str):
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError("PostgreSQL backend requires psycopg2") from exc
        self._psycopg2 = psycopg2
        self.dsn = dsn
        self.path = "postgresql://yunpai_m0@127.0.0.1/yunpai_m0"
        self._init()

    def _connect(self):
        return self._psycopg2.connect(self.dsn)

    def _init(self):
        schema = SCHEMA.replace("TEXT PRIMARY KEY", "TEXT PRIMARY KEY").replace(
            "TEXT NOT NULL", "TEXT NOT NULL"
        ).replace("INTEGER", "INTEGER")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS yunpai_m0")
                cur.execute("SET search_path TO yunpai_m0")
                # Keep the deployed schema text-safe and independent from old M0.
                cur.execute("""CREATE TABLE IF NOT EXISTS import_batches (
                    batch_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS source_documents (
                    document_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
                    filename TEXT NOT NULL, sha256 TEXT NOT NULL, content_json JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS import_candidates (
                    candidate_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
                    document_id TEXT NOT NULL REFERENCES source_documents(document_id),
                    entity_type TEXT NOT NULL, candidate_json JSONB NOT NULL, status TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS approval_records (
                    approval_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
                    candidate_id TEXT, actor TEXT NOT NULL, decision TEXT NOT NULL,
                    approval_mode TEXT NOT NULL, reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS canonical_entities (
                    entity_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, entity_type TEXT NOT NULL,
                    canonical_key TEXT NOT NULL, current_version INTEGER NOT NULL,
                    lifecycle_status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                    UNIQUE(tenant_id, entity_type, canonical_key))""")
                cur.execute("""CREATE TABLE IF NOT EXISTS canonical_entity_versions (
                    entity_id TEXT NOT NULL REFERENCES canonical_entities(entity_id), version INTEGER NOT NULL,
                    payload_json JSONB NOT NULL, checksum TEXT NOT NULL, source_batch_id TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(entity_id, version))""")
                cur.execute("""CREATE TABLE IF NOT EXISTS canonical_ledger (
                    ledger_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
                    approval_mode TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
                cur.execute("""CREATE TABLE IF NOT EXISTS canonical_outbox (
                    outbox_id TEXT PRIMARY KEY, ledger_id TEXT NOT NULL, event_type TEXT NOT NULL,
                    payload_json JSONB NOT NULL, status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")

    def ingest(self, records: list[dict[str, Any]], *, tenant_id: str, task_id: str) -> dict[str, Any]:
        batch_id, now = uuid4().hex[:16], _now()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO yunpai_m0")
                cur.execute("INSERT INTO import_batches VALUES (%s,%s,%s,%s,%s,%s)",
                            (batch_id, tenant_id, task_id, "awaiting_review", now, now))
                for item in records:
                    item = _normalize_record(item)
                    filename = str(item.get("filename") or "document.json")
                    payload = item.get("records", item)
                    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    document_id, candidate_id = uuid4().hex, uuid4().hex
                    sha256 = hashlib.sha256(raw.encode()).hexdigest()
                    cur.execute("INSERT INTO source_documents VALUES (%s,%s,%s,%s,%s::jsonb,%s)",
                                (document_id, batch_id, filename, sha256, raw, now))
                    cur.execute("INSERT INTO import_candidates VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)",
                                (candidate_id, batch_id, document_id, str(item.get("entity_type") or "order"),
                                 json.dumps(item, ensure_ascii=False), "candidate", now))
        return {
            "batch_id": batch_id,
            "status": "awaiting_review",
            "candidate_count": len(records),
            "m0_candidate_records": records,
            "candidates": records,
        }

    def publish(self, batch_id: str, *, actor: str, reason: str = "", human_override: bool = False) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO yunpai_m0")
                cur.execute("SELECT * FROM import_batches WHERE batch_id=%s", (batch_id,))
                batch = cur.fetchone()
                if not batch:
                    return {"status": "not_found", "batch_id": batch_id}
                cur.execute("SELECT candidate_id,entity_type,candidate_json FROM import_candidates WHERE batch_id=%s", (batch_id,))
                candidates = cur.fetchall()
                published = 0
                for candidate_id, entity_type, data in candidates:
                    if isinstance(data, str):
                        data = json.loads(data)
                    key = _canonical_key_of(data, candidate_id)
                    cur.execute("SELECT entity_id,current_version FROM canonical_entities WHERE tenant_id=%s AND entity_type=%s AND canonical_key=%s",
                                (batch[1], entity_type, key))
                    entity = cur.fetchone()
                    entity_id, version = (entity[0], int(entity[1]) + 1) if entity else (uuid4().hex, 1)
                    checksum = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    if entity:
                        cur.execute("UPDATE canonical_entities SET current_version=%s,updated_at=%s WHERE entity_id=%s", (version, _now(), entity_id))
                    else:
                        cur.execute("INSERT INTO canonical_entities VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                    (entity_id, batch[1], entity_type, key, version, "active", _now(), _now()))
                    cur.execute("INSERT INTO canonical_entity_versions VALUES (%s,%s,%s::jsonb,%s,%s,%s)",
                                (entity_id, version, json.dumps(data, ensure_ascii=False), checksum, batch_id, _now()))
                    mode = "human_override" if human_override else "standard"
                    cur.execute("INSERT INTO approval_records VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                (uuid4().hex, batch_id, candidate_id, actor, "approve", mode, reason, _now()))
                    ledger_id = uuid4().hex
                    cur.execute("INSERT INTO canonical_ledger VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                (ledger_id, batch_id, entity_id, version, "publish", actor, mode, _now()))
                    cur.execute("INSERT INTO canonical_outbox VALUES (%s,%s,%s,%s::jsonb,%s,%s)",
                                (uuid4().hex, ledger_id, "canonical.entity.published",
                                 json.dumps({"entity_id": entity_id, "version": version}), "pending", _now()))
                    cur.execute("UPDATE import_candidates SET status='published' WHERE candidate_id=%s", (candidate_id,))
                    published += 1
                cur.execute("UPDATE import_batches SET status='published',updated_at=%s WHERE batch_id=%s", (_now(), batch_id))
        return self.readback(batch_id) | {"status": "published", "published": published}

    def readback(self, batch_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO yunpai_m0")
                cur.execute("""SELECT e.entity_id,e.entity_type,e.canonical_key,v.version,v.payload_json,v.checksum
                    FROM canonical_entities e JOIN canonical_entity_versions v ON v.entity_id=e.entity_id AND v.version=e.current_version
                    JOIN canonical_ledger l ON l.entity_id=e.entity_id AND l.version=v.version WHERE l.batch_id=%s""", (batch_id,))
                rows = cur.fetchall()
                cur.execute("SELECT COUNT(*) FROM canonical_ledger WHERE batch_id=%s", (batch_id,)); ledger = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM canonical_outbox WHERE ledger_id IN (SELECT ledger_id FROM canonical_ledger WHERE batch_id=%s)", (batch_id,)); outbox = cur.fetchone()[0]
        return {"batch_id": batch_id, "canonical_readback_available": True, "approved_candidates": len(rows),
                "ledger_count": int(ledger), "outbox_count": int(outbox),
                "entities": [{"entity_id": r[0], "entity_type": r[1], "canonical_key": r[2], "version": r[3], "payload_json": r[4], "checksum": r[5]} for r in rows]}

    def list_entities(self, entity_type: str, tenant_id: str = "default") -> dict[str, Any]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO yunpai_m0")
                cur.execute("""SELECT e.entity_id,e.entity_type,e.canonical_key,v.version,v.payload_json,v.checksum
                    FROM canonical_entities e JOIN canonical_entity_versions v ON v.entity_id=e.entity_id AND v.version=e.current_version
                    WHERE e.entity_type=%s AND e.tenant_id=%s AND e.lifecycle_status='active' ORDER BY e.canonical_key""",
                    (entity_type, tenant_id))
                rows = cur.fetchall()
        return {"entity_type": entity_type, "tenant_id": tenant_id, "count": len(rows),
                "entities": [{"entity_id": r[0], "entity_type": r[1], "canonical_key": r[2], "version": r[3],
                              "payload_json": r[4] if isinstance(r[4], dict) else json.loads(r[4]) if isinstance(r[4], str) else r[4],
                              "checksum": r[5]} for r in rows]}


def create_app():
    from fastapi import FastAPI, Header, Request

    app = FastAPI(title="Yunpai M0 Data Backend", version="1.0.0")
    postgres_dsn = os.getenv("YUNPAI_M0_POSTGRES_DSN")
    store = PostgresM0Store(postgres_dsn) if postgres_dsn else M0Store(os.getenv("YUNPAI_M0_DB", "runtime/yunpai-m0.sqlite"))

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "yunpai-m0", "database": store.path}

    @app.post("/api/m0/catalog/ingest/validate")
    async def validate(body: dict[str, Any]):
        records = body.get("records") if isinstance(body.get("records"), list) else []
        return {"publishable": bool(records), "candidate_count": len(records)}

    @app.post("/api/m0/import/upload")
    async def import_upload(request: Request):
        raw_body = await request.body()
        form = await request.form()
        print("m0 upload", request.headers.get("content-type"), len(raw_body), len(form), flush=True)
        files = [
            value for _, value in form.multi_items()
            if hasattr(value, "read") and hasattr(value, "filename")
        ]
        records = []
        for item in files:
            raw = await item.read()
            records.append({
                "filename": item.filename or "document.bin",
                "entity_type": "order",
                "content_b64": base64.b64encode(raw).decode("ascii"),
            })
        if not records:
            # Compatibility path for HTTP clients that serialize the upload
            # envelope as JSON instead of multipart.
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                body = {}
            for item in body.get("files", []) if isinstance(body, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("content_b64"), str):
                    records.append({
                        "filename": str(item.get("filename") or "document.bin"),
                        "entity_type": "order",
                        "content_b64": item["content_b64"],
                    })
        tenant_id = request.headers.get("x-yunpai-tenant-id", "default")
        task_id = request.headers.get("x-yunpai-task-id", "default")
        return store.ingest(records, tenant_id=tenant_id, task_id=task_id)

    @app.post("/api/m0/import/batch/{batch_id}/commit")
    async def import_commit(batch_id: str, body: dict[str, Any] | None = None,
                            x_yunpai_principal: str | None = Header(None)):
        body = body or {}
        return store.publish(
            batch_id,
            actor=str(body.get("approved_by") or x_yunpai_principal or "operator"),
            reason=str(body.get("override_reason") or ""),
            human_override=bool(body.get("human_override", False)),
        )

    @app.get("/api/m0/import/batch/{batch_id}")
    async def import_status(batch_id: str):
        return store.readback(batch_id)

    @app.post("/api/m0/catalog/ingest/publish")
    async def publish(body: dict[str, Any], x_yunpai_principal: str | None = Header(None)):
        records = body.get("records") if isinstance(body.get("records"), list) else []
        approval = body.get("approval") if isinstance(body.get("approval"), dict) else {}
        result = store.ingest(records, tenant_id="default", task_id=str(body.get("task_id") or "default"))
        return {"data": store.publish(
            result["batch_id"],
            actor=str(approval.get("approved_by") or x_yunpai_principal or "operator"),
            reason=str(approval.get("reason") or ""),
            human_override=str(approval.get("mode")) == "human_override",
        )}

    @app.get("/api/m0/catalog/readback/{batch_id}")
    async def readback(batch_id: str):
        return {"data": store.readback(batch_id)}

    @app.get("/api/m0/catalog/entities")
    async def list_entities(entity_type: str, tenant_id: str = "default"):
        return {"data": store.list_entities(entity_type, tenant_id)}

    return app
