"""M0 本地 sandbox 批次登记存储（任务书阶段一：M0-first 可编排面）。

边界声明：
- 本模块只做本地批次/候选登记与人工裁决记录，供 data_import_run/status/
  preview/resolve 在 local transport 下可编排、可测试、可审计；
- 所有输出显式标注 environment=sandbox、canonical=False；
- 绝不把本地登记冒充 M0 canonical：真实发布必须由部署方提供 M0 URL/
  PostgreSQL schema/审核授权/写入回读接口，经 YUNPAI_TOOL_TRANSPORT=http
  bind_http 覆盖同名单工具（registry.bind_http）。
"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path
from typing import Any

ENVIRONMENT = "sandbox"

# 候选审核状态机（本地裁决层；canonical 迁移由真实 M0 承担）。
REVIEW_STATES = ("identified", "candidate", "needs_review", "approved", "rejected")


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class M0SandboxStore:
    """SQLite 登记：批次 + 候选 + 裁决。幂等键按 (task_id, sha256)。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS m0_batches (
                    batch_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'default',
                    status TEXT NOT NULL,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS m0_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    document_kind TEXT,
                    confidence REAL,
                    review_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, sha256)
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def register_batch(self, *, task_id: str, tenant_id: str, files: list[dict[str, Any]], batch_id: str | None = None) -> dict[str, Any]:
        """登记批次与候选；相同 (task_id, sha256) 重放返回既有候选，不重复登记。"""
        now = utc_now()
        effective_batch = batch_id or f"batch-{task_id[-12:]}"
        with self._connect() as db:
            existing_batch = db.execute("SELECT status FROM m0_batches WHERE batch_id=?", (effective_batch,)).fetchone()
            created_candidates = 0
            replayed = 0
            if existing_batch is None:
                db.execute(
                    "INSERT INTO m0_batches(batch_id, task_id, tenant_id, status, file_count, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (effective_batch, task_id, tenant_id, "awaiting_review", len(files), now, now),
                )
            for item in files:
                name, raw = _decode_upload(item)
                if raw is None:
                    continue
                digest = sha256(raw).hexdigest()
                payload = {"filename": name, "size_bytes": len(raw)}
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                    if isinstance(parsed, dict):
                        payload["records"] = parsed.get("records", [])
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                kind = _guess_kind(payload.get("records") or [], name)
                confidence = 0.8 if payload.get("records") else 0.5
                candidate_id = f"cand-{sha256((effective_batch + digest).encode()).hexdigest()[:20]}"
                row = db.execute("SELECT review_status FROM m0_candidates WHERE batch_id=? AND sha256=?", (effective_batch, digest)).fetchone()
                if row:
                    replayed += 1
                    continue
                db.execute(
                    """INSERT INTO m0_candidates(candidate_id, batch_id, sha256, filename, document_kind, confidence, review_status, payload_json, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (candidate_id, effective_batch, digest, name, kind, confidence, "needs_review" if confidence < 0.8 else "candidate", json.dumps(payload, ensure_ascii=False), now),
                )
                created_candidates += 1
            db.execute("UPDATE m0_batches SET updated_at=? WHERE batch_id=?", (now, effective_batch))
        return {
            "batch_id": effective_batch,
            "status": "awaiting_review",
            "environment": ENVIRONMENT,
            "canonical": False,
            "created_candidates": created_candidates,
            "replayed": replayed,
        }

    def status(self, batch_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            batch = db.execute("SELECT * FROM m0_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                return None
            counts = {
                row["review_status"]: row["c"]
                for row in db.execute("SELECT review_status, count(*) AS c FROM m0_candidates WHERE batch_id=? GROUP BY review_status", (batch_id,)).fetchall()
            }
        return {"batch_id": batch_id, "status": batch["status"], "task_id": batch["task_id"], "candidates": counts, "environment": ENVIRONMENT, "canonical": False}

    def preview(self, batch_id: str) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT candidate_id, sha256, filename, document_kind, confidence, review_status, payload_json FROM m0_candidates WHERE batch_id=? ORDER BY created_at",
                (batch_id,),
            ).fetchall()
        return {
            "batch_id": batch_id,
            "documents": [
                {
                    "id": index + 1, "candidate_id": row["candidate_id"], "filename": row["filename"],
                    "sha256": row["sha256"], "document_kind": row["document_kind"],
                    "confidence": row["confidence"], "review_status": row["review_status"],
                }
                for index, row in enumerate(rows)
            ],
            "environment": ENVIRONMENT,
            "canonical": False,
        }

    def resolve(self, *, batch_id: str, candidate_id: str | None = None, resolve_id: int | None = None, action: str, actor: str = "operator") -> dict[str, Any]:
        if action not in {"approve", "reject"}:
            raise ValueError(f"unsupported resolve action: {action}")
        if not candidate_id and resolve_id is None:
            raise ValueError("resolve 需要 candidate_id 或 resolve_id(序号)")
        offset = max(0, int(resolve_id) - 1) if resolve_id is not None else None
        with self._connect() as db:
            if candidate_id:
                row = db.execute("SELECT review_status FROM m0_candidates WHERE batch_id=? AND candidate_id=?", (batch_id, candidate_id)).fetchone()
            elif offset is not None:
                row = db.execute("SELECT review_status FROM m0_candidates WHERE batch_id=? ORDER BY created_at LIMIT 1 OFFSET ?", (batch_id, offset)).fetchone()
            else:
                row = None
            if row is None:
                raise ValueError(f"candidate not found: {batch_id}/resolve_id={resolve_id}")
            if row["review_status"] in {"approved", "rejected"}:
                raise ValueError(f"candidate already decided: {row['review_status']}")
            target = "approved" if action == "approve" else "rejected"
            if candidate_id:
                db.execute("UPDATE m0_candidates SET review_status=? WHERE batch_id=? AND candidate_id=?", (target, batch_id, candidate_id))
            elif offset is not None:
                db.execute(
                    "UPDATE m0_candidates SET review_status=? WHERE batch_id=? AND candidate_id=(SELECT candidate_id FROM m0_candidates WHERE batch_id=? ORDER BY created_at LIMIT 1 OFFSET ?)",
                    (target, batch_id, batch_id, offset),
                )
            db.execute("UPDATE m0_batches SET status='decided', updated_at=? WHERE batch_id=?", (utc_now(), batch_id))
        return {"batch_id": batch_id, "candidate_id": candidate_id or f"resolve_id={resolve_id}", "action": action, "status": target, "actor": actor, "environment": ENVIRONMENT, "canonical": False}

    def pending_count(self, batch_id: str) -> int:
        with self._connect() as db:
            row = db.execute("SELECT count(*) AS c FROM m0_candidates WHERE batch_id=? AND review_status NOT IN ('approved','rejected')", (batch_id,)).fetchone()
        return int(row["c"]) if row else 0

    def readback_report(self, batch_id: str, *, real_m0_available: bool = False) -> dict[str, Any]:
        """M0 canonical 回读报告（dry-run 语义）。

        本地 sandbox 无真实 M0 canonical 表/ledger/outbox；只有当部署方提供
        M0 URL/PostgreSQL/审核授权且本函数收到 real_m0_available=true 时才报告
        可回读。没有回读证据时绝不写“生产完成”。
        """
        with self._connect() as db:
            approved = db.execute("SELECT count(*) AS c FROM m0_candidates WHERE batch_id=? AND review_status='approved'", (batch_id,)).fetchone()["c"]
            pending = self.pending_count(batch_id)
        if real_m0_available:
            return {
                "batch_id": batch_id, "transport": "http", "environment": "production",
                "canonical_readback_available": True, "approved_candidates": int(approved),
                "ledger": {"available": True}, "outbox": {"available": True}, "rollback": {"available": True},
                "detail": "真实 M0 联调回读（需部署方确认 URL/schema/授权）",
            }
        return {
            "batch_id": batch_id, "transport": "local", "environment": "sandbox",
            "canonical_readback_available": False, "approved_candidates": int(approved), "pending_candidates": int(pending),
            "ledger": {"available": False}, "outbox": {"available": False}, "rollback": {"available": False},
            "detail": "local transport 无 M0 canonical 表与回读接口；未写生产完成。真实发布需部署方提供 M0 URL、PostgreSQL schema/权限、审核授权和写入回读接口",
            "dry_run": True,
        }


def _decode_upload(item: Any) -> tuple[str, bytes | None]:
    if not isinstance(item, dict):
        return "upload.bin", None
    name = str(item.get("filename") or "upload.bin")
    encoded = item.get("content_b64")
    if not isinstance(encoded, str) or not encoded.strip():
        return name, None
    try:
        import base64

        return name, base64.b64decode(encoded, validate=True)
    except ValueError:
        return name, None


def _guess_kind(records: list[Any], filename: str) -> str:
    text = f"{filename} {json.dumps(records, ensure_ascii=False)}".lower()
    for token, kind in (
        ("订单", "order"), ("po", "order"), ("order", "order"),
        ("bom", "bom"), ("物料清单", "bom"),
        ("设备", "equipment"), ("equipment", "equipment"),
        ("供应商", "supplier"), ("supplier", "supplier"),
        ("库存", "inventory"), ("inventory", "inventory"),
        ("工位", "station"), ("station", "station"),
        ("人员", "worker"), ("worker", "worker"), ("员工", "worker"),
        ("工序", "operation"), ("operation", "operation"),
        ("route", "route"), ("sop", "sop"),
        ("日历", "calendar"), ("calendar", "calendar"),
        ("成本", "finance_cost"), ("财务", "finance_cost"),
        ("产品", "product"), ("product", "product"),
        ("工装", "tooling"), ("tooling", "tooling"),
    ):
        if token in text:
            return kind
    return "document"
