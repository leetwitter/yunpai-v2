"""M0 canonical 写面 store（Batch B）：与 M0SandboxStore 同接口，落 canonical 表。
> 迁入自 `_wt/INT/src/yunpai_langgraph/m0_import_store.py`（LF 归一 sha256=90955c042e7c1852c55cd0f4cbad3207b19bde43bd739a26d59dedf7ba55fb64，16487B / 303 行）。
> M0 分片 C1 随迁（`_migration/rows-S1.md`）；本文件改动：无（逐行迁入）。

- register_batch：接受结构化 JSON 文件（records/单 envelope/记录列表），映射为
  M0Store.ingest 的 canonical 候选；原始业务文件（无法结构化解析）→ quarantined，
  绝不伪造候选（识别/解析链在 Batch C/M1 面提供）；
- status/preview/resolve/pending_count：基于 canonical 表（import_batches/
  import_candidates/approval_records）聚合，形状与沙箱兼容；
- publish：M0Store.publish_approved（只发布已 approve），返回 readback 计数；
- canonical=True / environment="local_canonical"，杜绝"本地登记冒充 canonical"。
"""
from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .m0_sandbox import _decode_upload, _guess_kind, utc_now

ENVIRONMENT = "local_canonical"


class CanonicalImportStore:
    """进程内 canonical 写面（基于 M0Store DDL 的 sqlite 库）。"""

    canonical = True

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        from .m0_backend import M0Store

        self.store = M0Store(self.path)  # 建表（幂等）
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS m0_quarantines (
                    batch_id TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL, created_at TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT 'default',
                    UNIQUE(batch_id, filename, sha256)
                )
            """)

    # ---------- 内部 helpers ----------
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def _records_from_file(self, item: dict[str, Any], filename: str) -> tuple[list[dict[str, Any]], str | None]:
        """结构化 JSON 文件 → canonical 候选记录列表；无法解析返回 ([] , reason)。"""
        name, raw = _decode_upload(item)
        if raw is None:
            return [], "bad_upload"
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return [], "no_structured_records"
        raw_items: list[Any]
        if isinstance(parsed, dict):
            if isinstance(parsed.get("records"), list) and parsed["records"]:
                raw_items = parsed["records"]
            elif "entity_type" in parsed or "payload" in parsed or isinstance(parsed.get("payload"), dict):
                raw_items = [parsed]
            else:
                raw_items = [parsed]
        elif isinstance(parsed, list):
            raw_items = parsed
        else:
            return [], "no_structured_records"
        records: list[dict[str, Any]] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            record = dict(entry)
            if not str(record.get("entity_type") or ""):
                record["entity_type"] = _guess_kind([record], filename) if record else "document"
            if not str(record.get("filename") or ""):
                record["filename"] = name
            records.append(record)
        if not records:
            return [], "no_structured_records"
        return records, None

    # ---------- sandbox 兼容接口 ----------
    def register_batch(self, *, task_id: str, tenant_id: str,
                       files: list[dict[str, Any]], batch_id: str | None = None) -> dict[str, Any]:
        accepted_records: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        replayed = 0
        descriptors: list[tuple[str, str, str]] = []  # (filename, digest, reason)
        for item in files:
            if not isinstance(item, dict):
                descriptors.append(("upload.bin", "", "bad_upload"))
                continue
            filename = str(item.get("filename") or "upload.bin")
            records, reason = self._records_from_file(item, filename)
            if reason:
                _n, raw = _decode_upload(item)
                digest = sha256(raw).hexdigest() if raw else ""
                descriptors.append((filename, digest, reason))
                continue
            # 幂等：同 (task_id, tenant_id, sha256) 且未发布的历史文件跳过
            _n, raw = _decode_upload(item)
            digest = sha256(raw).hexdigest() if raw else ""
            if digest and self._is_replayed(task_id, tenant_id, digest):
                replayed += 1
                continue
            accepted_records.extend(records)
        if accepted_records:
            ingested = self.store.ingest(accepted_records, tenant_id=tenant_id, task_id=task_id)
            effective = ingested["batch_id"]
        else:
            effective = batch_id or f"batch-{task_id[-12:]}"
        for filename, digest, reason in descriptors:
            with self._connect() as db:
                db.execute("INSERT OR IGNORE INTO m0_quarantines VALUES (?, ?, ?, ?, ?, ?)",
                           (effective, filename, digest, reason, utc_now(), tenant_id))
            quarantined.append({"filename": filename, "reason": reason})
        if not accepted_records:
            return {"batch_id": effective, "status": "awaiting_review",
                    "candidates": [], "quarantined": quarantined, "created_candidates": 0,
                    "replayed": replayed, "canonical": True, "environment": ENVIRONMENT}
        candidates = [
            {"candidate_id": cand["candidate_id"], "filename": cand.get("filename") or "",
             "sha256": cand.get("sha256") or "", "status": "needs_review"}
            for cand in self.list_candidates(effective).get("candidates", [])
        ]
        return {"batch_id": effective, "status": "awaiting_review",
                "candidates": candidates, "quarantined": quarantined,
                "created_candidates": len(candidates), "replayed": replayed,
                "canonical": True, "environment": ENVIRONMENT}

    def history(self, tenant_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        """批次历史（canonical import_batches + 每批候选状态计数）。"""
        sql = ("SELECT b.*, COUNT(c.candidate_id) AS candidate_count "
               "FROM import_batches b LEFT JOIN import_candidates c ON c.batch_id=b.batch_id ")
        params: list[Any] = []
        if tenant_id:
            sql += "WHERE b.tenant_id=? "
            params.append(tenant_id)
        sql += "GROUP BY b.batch_id ORDER BY b.created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as db:
            batches = [dict(row) for row in db.execute(sql, params).fetchall()]
        return {"batches": batches, "count": len(batches),
                "canonical": True, "environment": ENVIRONMENT}

    def list_quarantine(self, tenant_id: str | None = None, limit: int = 100,
                        batch_id: str | None = None) -> dict[str, Any]:
        """隔离区列表；可按 batch_id 过滤（manifest 声明的入参，此前被忽略）。"""
        sql = "SELECT * FROM m0_quarantines "
        params: list[Any] = []
        filters: list[str] = []
        if tenant_id:
            filters.append("tenant_id=?")
            params.append(tenant_id)
        if batch_id:
            filters.append("batch_id=?")
            params.append(batch_id)
        if filters:
            sql += "WHERE " + " AND ".join(filters) + " "
        sql += "ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as db:
            rows = [dict(row) for row in db.execute(sql, params).fetchall()]
        # quarantined = 规范键；items = manifest 历史声明键（保留兼容，勿新增消费方）。
        return {"quarantined": rows, "items": rows, "count": len(rows),
                "canonical": True, "environment": ENVIRONMENT}

    def rollback(self, batch_id: str, *, actor: str) -> dict[str, Any]:
        """按 ledger 回滚已发布批次（保留证据：候选置 rolled_back、outbox 事件、
        ledger 增 rollback 记录；实体版本还原或整实体删除）。"""
        with self._connect() as db:
            batch = db.execute("SELECT status FROM import_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise ValueError(f"batch not found: {batch_id}")
            if batch["status"] != "published":
                raise ValueError(f"only published batch can be rolled back: {batch_id}")
            ledger_rows = db.execute(
                "SELECT ledger_id, entity_id, version FROM canonical_ledger "
                "WHERE batch_id=? AND action='publish'", (batch_id,)).fetchall()
            rolled_back = 0
            for ledger_id, entity_id, version in ledger_rows:
                prev = db.execute(
                    "SELECT MAX(version) AS v FROM canonical_entity_versions "
                    "WHERE entity_id=? AND version<?", (entity_id, version)).fetchone()["v"]
                db.execute("DELETE FROM canonical_entity_versions WHERE entity_id=? AND version=?",
                           (entity_id, version))
                if prev is not None:
                    db.execute("UPDATE canonical_entities SET current_version=?, updated_at=? WHERE entity_id=?",
                               (prev, utc_now(), entity_id))
                else:
                    remaining = db.execute("SELECT COUNT(*) AS c FROM canonical_entity_versions WHERE entity_id=?",
                                           (entity_id,)).fetchone()["c"]
                    if remaining == 0:
                        db.execute("DELETE FROM canonical_entities WHERE entity_id=?", (entity_id,))
                db.execute("UPDATE canonical_outbox SET status='rolled_back' WHERE ledger_id=?", (ledger_id,))
                new_ledger = uuid4().hex
                db.execute("INSERT INTO canonical_ledger VALUES (?,?,?,?,?,?,?,?)",
                           (new_ledger, batch_id, entity_id, version, "rollback", actor, "standard", utc_now()))
                db.execute("INSERT INTO canonical_outbox VALUES (?,?,?,?,?,?)",
                           (uuid4().hex, new_ledger, "canonical.entity.rolled_back",
                            json.dumps({"entity_id": entity_id, "version": version}), "pending", utc_now()))
                rolled_back += 1
            db.execute("UPDATE import_candidates SET status='rolled_back' "
                       "WHERE batch_id=? AND status='published'", (batch_id,))
            db.execute("UPDATE import_batches SET status='rolled_back', updated_at=? WHERE batch_id=?",
                       (utc_now(), batch_id))
        return {"batch_id": batch_id, "status": "rolled_back", "actor": actor,
                # rolled_back = manifest 声明键；rolled_back_entities = 历史实现键。
                "rolled_back": rolled_back, "rolled_back_entities": rolled_back,
                "canonical": True, "environment": ENVIRONMENT}

    def _is_replayed(self, task_id: str, tenant_id: str, digest: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM import_batches b JOIN source_documents d ON d.batch_id=b.batch_id "
                "WHERE b.task_id=? AND b.tenant_id=? AND d.sha256=? AND b.status!='published' LIMIT 1",
                (task_id, tenant_id, digest),
            ).fetchone()
        return row is not None

    def status(self, batch_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            batch = db.execute("SELECT * FROM import_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                return None
            counts = {
                row["status"]: row["c"]
                for row in db.execute("SELECT status, count(*) AS c FROM import_candidates WHERE batch_id=? GROUP BY status",
                                      (batch_id,)).fetchall()
            }
        return {"batch_id": batch_id, "status": batch["status"], "task_id": batch["task_id"],
                "candidates": counts, "canonical": True, "environment": ENVIRONMENT}

    def preview(self, batch_id: str) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.candidate_id, c.entity_type, c.status, c.candidate_json, d.filename, d.sha256 "
                "FROM import_candidates c JOIN source_documents d ON d.document_id=c.document_id "
                "WHERE c.batch_id=? ORDER BY c.rowid", (batch_id,)).fetchall()
        documents = []
        for index, row in enumerate(rows, start=1):
            candidate: dict[str, Any] = {}
            try:
                candidate = json.loads(row["candidate_json"])
            except ValueError:
                pass
            records = candidate.get("records") if isinstance(candidate.get("records"), list) else []
            if not records and isinstance(candidate.get("payload"), dict):
                records = [candidate["payload"]]
            documents.append({
                "id": index, "candidate_id": row["candidate_id"], "filename": row["filename"],
                "sha256": row["sha256"], "document_kind": row["entity_type"],
                "confidence": 0.8 if records else 0.5, "review_status": row["status"],
                "payload_json": records,
            })
        return {"batch_id": batch_id, "documents": documents,
                "canonical": True, "environment": ENVIRONMENT}

    def resolve(self, *, batch_id: str, candidate_id: str | None = None,
                resolve_id: int | None = None, action: str, actor: str = "operator") -> dict[str, Any]:
        if action not in {"approve", "reject"}:
            raise ValueError(f"unsupported resolve action: {action}")
        if not candidate_id and resolve_id is None:
            raise ValueError("resolve 需要 candidate_id 或 resolve_id(序号)")
        if candidate_id is None:
            docs = self.preview(batch_id)["documents"]
            try:
                target = docs[int(resolve_id) - 1]["candidate_id"]
            except (IndexError, ValueError):
                raise ValueError(f"candidate not found: {batch_id}/resolve_id={resolve_id}")
        else:
            target = candidate_id
        decided = self.store.resolve_candidate(batch_id, target, action=action, actor=actor)
        with self._connect() as db:
            db.execute("UPDATE import_batches SET status='decided' WHERE batch_id=?", (batch_id,))
        return {"batch_id": batch_id, "candidate_id": target, "action": action,
                "status": "approved" if decided["decision"] == "approve" else "rejected",
                "actor": actor, "canonical": True, "environment": ENVIRONMENT}

    def pending_count(self, batch_id: str) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT count(*) AS c FROM import_candidates WHERE batch_id=? "
                "AND status NOT IN ('approved','rejected','published')", (batch_id,)).fetchone()
        return int(row["c"]) if row else 0

    def publish(self, batch_id: str, *, actor: str, human_override: bool = False) -> dict[str, Any]:
        """发布已 approve 候选；返回 publish_approved + readback 语义。"""
        result = self.store.publish_approved(batch_id, actor=actor, human_override=human_override)
        if result.get("status") not in {"published", "already_published"}:
            return result
        readback = self.store.readback(batch_id)
        return {**result, "canonical": True, "environment": ENVIRONMENT,
                "readback_available": True,
                "approved_candidates": readback["approved_candidates"],
                "ledger_count": readback["ledger_count"], "outbox_count": readback["outbox_count"]}

    def list_candidates(self, batch_id: str) -> dict[str, Any]:
        return self.store.list_candidates(batch_id)
