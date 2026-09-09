"""M4B 本地领域库（L-M4B：供应商主数据/回复 + 追踪/预警/供应快照与事件 + schedule-impact 接收）。

独立模块（禁改共享文件）：本库自持 sqlite（env ``YUNPAI_M4B_DB``；缺省
``runtime/yunpai-m4b.sqlite``，与 m1/m5 领域库同约定）；表前缀 ``m4b_``。

跨线引用纪律（整合者裁决 1）：凡需引用采购单/行项之处只存 manifest 契约字段的
字符串引用（purchase_order_no / purchase_order_id / purchase_order_item_id 原样存储，
无 FK、不假设 M4A 表名）；与 M4A 采购单状态联动属集成期增强。

canonical checksum（RFC 8785 风格）：递归按 UTF-16 code unit 排序对象键、
compact JSON、ensure_ascii=False、拒绝未配对代理项与浮点；digest=``sha256:<hex>``。
语义来源：0831-wh909-bom-sop-39085\\m4\\backend\\app（models/supplier*、supplier_fact_
confirmation、material_supply_*、schedule_impact + alembic）与 m4/contracts/* urn schema。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS m4b_suppliers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  supplier_code TEXT NOT NULL DEFAULT '',
  supplier_name TEXT NOT NULL,
  contact_name TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  default_channel TEXT NOT NULL DEFAULT 'email',
  remark TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  canonical_supplier_code TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (tenant_id, supplier_name)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_m4b_suppliers_tenant_code
  ON m4b_suppliers(tenant_id, supplier_code) WHERE supplier_code != '';
CREATE INDEX IF NOT EXISTS idx_m4b_suppliers_tenant ON m4b_suppliers(tenant_id);

CREATE TABLE IF NOT EXISTS m4b_supplier_replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  purchase_order_id INTEGER NOT NULL,
  purchase_order_no TEXT NOT NULL,
  supplier_name TEXT NOT NULL,
  reply_content TEXT NOT NULL,
  received_at TEXT NOT NULL,
  parsed_result_json TEXT NOT NULL DEFAULT '',
  parsed_result_version INTEGER NOT NULL DEFAULT 0,
  confidence REAL,
  need_human_review INTEGER NOT NULL DEFAULT 0,
  parse_status TEXT NOT NULL DEFAULT 'pending',
  parse_provider TEXT NOT NULL DEFAULT '',
  model_name TEXT NOT NULL DEFAULT '',
  prompt_version TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_m4b_replies_tenant ON m4b_supplier_replies(tenant_id);

CREATE TABLE IF NOT EXISTS m4b_tracking (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  purchase_order_id TEXT NOT NULL,
  purchase_order_no TEXT NOT NULL DEFAULT '',
  purchase_order_item_id TEXT NOT NULL DEFAULT '',
  supplier_name TEXT NOT NULL DEFAULT '',
  promised_date TEXT,
  unit_price TEXT,
  currency TEXT,
  exception_type TEXT,
  exception_description TEXT,
  arrival_status TEXT NOT NULL DEFAULT 'not_received',
  is_overdue INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (tenant_id, purchase_order_id, purchase_order_item_id)
);
CREATE INDEX IF NOT EXISTS idx_m4b_tracking_tenant ON m4b_tracking(tenant_id);

CREATE TABLE IF NOT EXISTS m4b_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  alert_type TEXT NOT NULL,
  purchase_order_id TEXT NOT NULL DEFAULT '',
  purchase_order_item_id TEXT NOT NULL DEFAULT '',
  purchase_order_no TEXT NOT NULL DEFAULT '',
  supplier_name TEXT NOT NULL DEFAULT '',
  item_code TEXT,
  item_name TEXT,
  promised_date TEXT,
  days_overdue INTEGER NOT NULL DEFAULT 0,
  exception_description TEXT,
  urge_message TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_m4b_alerts_open_line_type
  ON m4b_alerts(tenant_id, purchase_order_id, purchase_order_item_id, alert_type)
  WHERE status != 'closed';
CREATE INDEX IF NOT EXISTS idx_m4b_alerts_tenant ON m4b_alerts(tenant_id, status);

CREATE TABLE IF NOT EXISTS m4b_fact_confirmations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  purchase_order_item_id TEXT NOT NULL,
  supplier_reply_id INTEGER NOT NULL,
  supplier_reply_version INTEGER NOT NULL,
  confirmed_by TEXT NOT NULL,
  confirmed_at TEXT NOT NULL,
  parse_confidence TEXT,
  delivery_date TEXT,
  exception_type TEXT,
  exception_description TEXT,
  result_payload_json TEXT NOT NULL,
  checksum TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (tenant_id, purchase_order_item_id, supplier_reply_id, supplier_reply_version)
);
CREATE INDEX IF NOT EXISTS idx_m4b_facts_tenant ON m4b_fact_confirmations(tenant_id);

CREATE TABLE IF NOT EXISTS m4b_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id TEXT NOT NULL,
  snapshot_version INTEGER NOT NULL,
  tenant_id TEXT NOT NULL,
  site_id TEXT NOT NULL,
  tracking_task_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  input_checksum TEXT NOT NULL,
  checksum TEXT NOT NULL,
  generated_at TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  response_json TEXT NOT NULL,
  UNIQUE (snapshot_id, snapshot_version),
  UNIQUE (tracking_task_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_m4b_snapshots_scope ON m4b_snapshots(tenant_id, site_id);

CREATE TABLE IF NOT EXISTS m4b_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  site_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_version INTEGER NOT NULL,
  checksum TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (tenant_id, site_id, event_version),
  UNIQUE (event_id)
);
CREATE INDEX IF NOT EXISTS idx_m4b_events_scope_cursor
  ON m4b_events(tenant_id, site_id, event_version);

CREATE TABLE IF NOT EXISTS m4b_impact_proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  tracking_task_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  plan_version TEXT NOT NULL,
  scenario_id TEXT NOT NULL DEFAULT '',
  material_id TEXT NOT NULL,
  impact_type TEXT NOT NULL DEFAULT '',
  reason_code TEXT NOT NULL DEFAULT '',
  required_quantity TEXT NOT NULL DEFAULT '',
  uom TEXT NOT NULL DEFAULT '',
  previous_required_at TEXT,
  required_at TEXT NOT NULL DEFAULT '',
  affected_orders_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  request_payload_json TEXT NOT NULL,
  input_checksum TEXT NOT NULL,
  application_status TEXT NOT NULL DEFAULT 'pending',
  applied_at TEXT,
  applied_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (tracking_task_id, proposal_id),
  UNIQUE (tracking_task_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_m4b_proposals_tenant ON m4b_impact_proposals(tenant_id, plan_version);
"""


def _default_db_path() -> str:
    return str(os.getenv("YUNPAI_M4B_DB") or "runtime/yunpai-m4b.sqlite")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# ---------------------------------------------------------------------------
# canonical JSON（RFC 8785 风格，用于跨工具 checksum 可重算）
# ---------------------------------------------------------------------------

def _utf16_units(value: str) -> list[int]:
    raw = value.encode("utf-16-le", errors="surrogatepass")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def _lone_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)


def _canonical(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise TypeError("canonical JSON forbids floating-point values")
    if isinstance(value, str):
        if _lone_surrogate(value):
            raise ValueError("canonical JSON string contains unpaired surrogate")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value.keys(), key=lambda k: _utf16_units(k))
        body = ",".join(
            json.dumps(str(k), ensure_ascii=False) + ":" + _canonical(value[k]) for k in keys
        )
        return "{" + body + "}"
    if isinstance(value, Decimal):
        raise TypeError("canonical JSON forbids Decimal; render as fixed string first")
    raise TypeError(f"canonical JSON forbids {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    return _canonical(value).encode("utf-8")


def payload_checksum(payload: dict[str, Any], *, exclude: str | None = None) -> str:
    """sha256:<hex> —— 去掉 exclude 顶层键后对 canonical JSON 摘要。"""
    target = payload
    if exclude:
        target = {k: v for k, v in payload.items() if k != exclude}
    digest = sha256(canonical_json_bytes(target)).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# 数值/时间规范化
# ---------------------------------------------------------------------------

def decimal_string(value: Any) -> str | None:
    """定点十进制字符串（去尾零），None 原样；非法抛 ValueError。"""
    if value is None or value == "":
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"VALIDATION_ERROR: 金额/数量必须为十进制数值: {value!r}") from exc
    rendered = format(dec, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def parse_iso_date(value: Any) -> str | None:
    """YYYY-MM-DD 校验（日期字段）。"""
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"VALIDATION_ERROR: 日期必须为 YYYY-MM-DD: {value!r}") from exc


def _as_utc_timestamp(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"VALIDATION_ERROR: 时间戳必须为 RFC 3339: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def coerce_int(value: Any, field: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"VALIDATION_ERROR: {field} 必须为整数: {value!r}") from exc


# ---------------------------------------------------------------------------
# 领域库
# ---------------------------------------------------------------------------

class M4BStore:
    """M4B 本地 sqlite 领域库（单进程 + RLock，事务内短连接）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.path = str(db_path) if db_path is not None else _default_db_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # ---------- suppliers ----------

    def supplier_get(self, tenant_id: str, supplier_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m4b_suppliers WHERE id=? AND tenant_id=?",
                (supplier_id, tenant_id)).fetchone()
        return dict(row) if row else None

    def supplier_by_name(self, tenant_id: str, name: str, *, exclude_id: int | None = None) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            if exclude_id is None:
                row = db.execute(
                    "SELECT * FROM m4b_suppliers WHERE tenant_id=? AND supplier_name=?",
                    (tenant_id, name)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM m4b_suppliers WHERE tenant_id=? AND supplier_name=? AND id!=?",
                    (tenant_id, name, exclude_id)).fetchone()
        return dict(row) if row else None

    def supplier_by_code(self, tenant_id: str, code: str, *, exclude_id: int | None = None) -> dict[str, Any] | None:
        if not code:
            return None
        with self._lock, self._connect() as db:
            if exclude_id is None:
                row = db.execute(
                    "SELECT * FROM m4b_suppliers WHERE tenant_id=? AND supplier_code=?",
                    (tenant_id, code)).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM m4b_suppliers WHERE tenant_id=? AND supplier_code=? AND id!=?",
                    (tenant_id, code, exclude_id)).fetchone()
        return dict(row) if row else None

    def supplier_create(self, *, tenant_id: str, supplier_name: str, contact_name: str,
                        email: str, phone: str, default_channel: str, remark: str,
                        status: str, supplier_code: str = "",
                        canonical_supplier_code: str = "") -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as db:
            cur = db.execute(
                "INSERT INTO m4b_suppliers (tenant_id, supplier_code, supplier_name, contact_name,"
                " email, phone, default_channel, remark, status, canonical_supplier_code,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, supplier_code, supplier_name, contact_name, email, phone,
                 default_channel, remark, status, canonical_supplier_code, now, now))
            row = db.execute("SELECT * FROM m4b_suppliers WHERE id=?",
                             (cur.lastrowid,)).fetchone()
        return dict(row)

    def supplier_update(self, tenant_id: str, supplier_id: int,
                        fields: dict[str, Any]) -> dict[str, Any] | None:
        sets = ", ".join(f"{key}=?" for key in fields)
        params = list(fields.values()) + [utc_now(), supplier_id, tenant_id]
        with self._lock, self._connect() as db:
            cur = db.execute(
                f"UPDATE m4b_suppliers SET {sets}, updated_at=? WHERE id=? AND tenant_id=?",
                params)
            if cur.rowcount == 0:
                return None
            row = db.execute("SELECT * FROM m4b_suppliers WHERE id=? AND tenant_id=?",
                             (supplier_id, tenant_id)).fetchone()
        return dict(row) if row else None

    def supplier_list(self, tenant_id: str, *, supplier_name: str | None,
                      offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        sql = "SELECT * FROM m4b_suppliers WHERE tenant_id=?"
        count_sql = "SELECT COUNT(*) FROM m4b_suppliers WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if supplier_name:
            like = f"%{supplier_name}%"
            sql += " AND supplier_name LIKE ?"
            count_sql += " AND supplier_name LIKE ?"
            params.append(like)
        with self._lock, self._connect() as db:
            total = db.execute(count_sql, params).fetchone()[0]
            rows = db.execute(sql + " ORDER BY id LIMIT ? OFFSET ?",
                              params + [int(limit), int(offset)]).fetchall()
        return [dict(r) for r in rows], int(total)

    # ---------- supplier replies ----------

    def reply_create(self, *, tenant_id: str, purchase_order_id: int, purchase_order_no: str,
                     supplier_name: str, reply_content: str, received_at: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as db:
            cur = db.execute(
                "INSERT INTO m4b_supplier_replies (tenant_id, purchase_order_id, purchase_order_no,"
                " supplier_name, reply_content, received_at, parse_status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (tenant_id, purchase_order_id, purchase_order_no, supplier_name,
                 reply_content, received_at, "pending", now, now))
            row = db.execute("SELECT * FROM m4b_supplier_replies WHERE id=?",
                             (cur.lastrowid,)).fetchone()
        return dict(row)

    def reply_get(self, tenant_id: str, reply_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m4b_supplier_replies WHERE id=? AND tenant_id=?",
                (reply_id, tenant_id)).fetchone()
        return dict(row) if row else None

    def reply_set_parse(self, tenant_id: str, reply_id: int, *,
                        parsed_result: dict[str, Any] | None,
                        parsed_result_version: int | None = None,
                        confidence: float | None = None,
                        need_human_review: bool = False,
                        parse_status: str = "pending",
                        parse_provider: str = "",
                        model_name: str = "",
                        prompt_version: str = "") -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            fields: list[str] = []
            params: list[Any] = []
            if parsed_result is not None:
                fields.append("parsed_result_json=?")
                params.append(json_text(parsed_result))
            if parsed_result_version is not None:
                fields.append("parsed_result_version=?")
                params.append(int(parsed_result_version))
            if confidence is not None:
                fields.append("confidence=?")
                params.append(float(confidence))
            fields += ["need_human_review=?", "parse_status=?", "parse_provider=?", "model_name=?",
                       "prompt_version=?", "updated_at=?"]
            params += [1 if need_human_review else 0, parse_status, parse_provider,
                       model_name, prompt_version, utc_now()]
            params += [reply_id, tenant_id]
            db.execute(
                f"UPDATE m4b_supplier_replies SET {', '.join(fields)} WHERE id=? AND tenant_id=?",
                params)
            row = db.execute("SELECT * FROM m4b_supplier_replies WHERE id=? AND tenant_id=?",
                             (reply_id, tenant_id)).fetchone()
        return dict(row) if row else None

    # ---------- tracking ----------

    def tracking_upsert(self, *, tenant_id: str, purchase_order_id: str,
                        purchase_order_no: str, purchase_order_item_id: str,
                        supplier_name: str, promised_date: str | None,
                        unit_price: str | None, currency: str | None,
                        exception_type: str | None, exception_description: str | None,
                        arrival_status: str = "not_received",
                        is_overdue: bool = False) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO m4b_tracking (tenant_id, purchase_order_id, purchase_order_no,"
                " purchase_order_item_id, supplier_name, promised_date, unit_price, currency,"
                " exception_type, exception_description, arrival_status, is_overdue,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(tenant_id, purchase_order_id, purchase_order_item_id)"
                " DO UPDATE SET promised_date=excluded.promised_date,"
                " unit_price=excluded.unit_price, currency=excluded.currency,"
                " exception_type=excluded.exception_type,"
                " exception_description=excluded.exception_description,"
                " arrival_status=excluded.arrival_status, is_overdue=excluded.is_overdue,"
                " updated_at=excluded.updated_at",
                (tenant_id, str(purchase_order_id), purchase_order_no, purchase_order_item_id,
                 supplier_name, promised_date, unit_price, currency, exception_type,
                 exception_description, arrival_status, 1 if is_overdue else 0, now, now))
            row = db.execute(
                "SELECT * FROM m4b_tracking WHERE tenant_id=? AND purchase_order_id=?"
                " AND purchase_order_item_id=?",
                (tenant_id, str(purchase_order_id), purchase_order_item_id)).fetchone()
        return dict(row)

    def tracking_list(self, tenant_id: str, *, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        with self._lock, self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM m4b_tracking WHERE tenant_id=?",
                               (tenant_id,)).fetchone()[0]
            rows = db.execute("SELECT * FROM m4b_tracking WHERE tenant_id=? ORDER BY id"
                              " LIMIT ? OFFSET ?", (tenant_id, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows], int(total)

    def tracking_mark_overdue(self, tenant_id: str, tracking_id: int, is_overdue: bool) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE m4b_tracking SET is_overdue=?, updated_at=? WHERE id=? AND tenant_id=?",
                       (1 if is_overdue else 0, utc_now(), tracking_id, tenant_id))

    # ---------- alerts ----------

    def alert_find_open(self, tenant_id: str, purchase_order_id: str,
                        purchase_order_item_id: str, alert_type: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m4b_alerts WHERE tenant_id=? AND purchase_order_id=?"
                " AND purchase_order_item_id=? AND alert_type=? AND status != 'closed'",
                (tenant_id, purchase_order_id, purchase_order_item_id, alert_type)).fetchone()
        return dict(row) if row else None

    def alert_create(self, *, tenant_id: str, alert_type: str, purchase_order_id: str,
                     purchase_order_item_id: str, purchase_order_no: str, supplier_name: str,
                     item_code: str | None, item_name: str | None, promised_date: str | None,
                     days_overdue: int, urge_message: str | None,
                     exception_description: str | None = None) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as db:
            cur = db.execute(
                "INSERT INTO m4b_alerts (tenant_id, alert_type, purchase_order_id,"
                " purchase_order_item_id, purchase_order_no, supplier_name, item_code, item_name,"
                " promised_date, days_overdue, exception_description, urge_message, status,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, alert_type, purchase_order_id, purchase_order_item_id,
                 purchase_order_no, supplier_name, item_code, item_name, promised_date,
                 int(days_overdue), exception_description, urge_message, "open", now, now))
            row = db.execute("SELECT * FROM m4b_alerts WHERE id=?",
                             (cur.lastrowid,)).fetchone()
        return dict(row)

    def alert_get(self, tenant_id: str, alert_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM m4b_alerts WHERE id=? AND tenant_id=?",
                             (alert_id, tenant_id)).fetchone()
        return dict(row) if row else None

    def alert_list(self, tenant_id: str, *, status: str | None, alert_type: str | None,
                   offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        sql = "SELECT * FROM m4b_alerts WHERE tenant_id=?"
        count_sql = "SELECT COUNT(*) FROM m4b_alerts WHERE tenant_id=?"
        params: list[Any] = [tenant_id]
        if status:
            sql += " AND status=?"
            count_sql += " AND status=?"
            params.append(status)
        if alert_type:
            sql += " AND alert_type=?"
            count_sql += " AND alert_type=?"
            params.append(alert_type)
        with self._lock, self._connect() as db:
            total = db.execute(count_sql, params).fetchone()[0]
            rows = db.execute(sql + " ORDER BY id LIMIT ? OFFSET ?",
                              params + [int(limit), int(offset)]).fetchall()
        return [dict(r) for r in rows], int(total)

    def alert_set_urge(self, tenant_id: str, alert_id: int, message: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE m4b_alerts SET urge_message=?, updated_at=? WHERE id=? AND tenant_id=?",
                       (message, utc_now(), alert_id, tenant_id))
            row = db.execute("SELECT * FROM m4b_alerts WHERE id=? AND tenant_id=?",
                             (alert_id, tenant_id)).fetchone()
        return dict(row) if row else None

    # ---------- supplier fact confirmations ----------

    def fact_find(self, tenant_id: str, *, item_id: str, reply_id: int,
                  reply_version: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m4b_fact_confirmations WHERE tenant_id=? AND purchase_order_item_id=?"
                " AND supplier_reply_id=? AND supplier_reply_version=?",
                (tenant_id, item_id, reply_id, reply_version)).fetchone()
        return dict(row) if row else None

    def fact_create(self, *, tenant_id: str, item_id: str, reply_id: int, reply_version: int,
                    confirmed_by: str, confirmed_at: str, parse_confidence: str | None,
                    delivery_date: str | None, exception_type: str | None,
                    exception_description: str | None, result_payload: dict[str, Any],
                    checksum: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as db:
            cur = db.execute(
                "INSERT INTO m4b_fact_confirmations (tenant_id, purchase_order_item_id,"
                " supplier_reply_id, supplier_reply_version, confirmed_by, confirmed_at,"
                " parse_confidence, delivery_date, exception_type, exception_description,"
                " result_payload_json, checksum, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tenant_id, item_id, reply_id, reply_version, confirmed_by, confirmed_at,
                 parse_confidence, delivery_date, exception_type, exception_description,
                 json_text(result_payload), checksum, now, now))
            row = db.execute("SELECT * FROM m4b_fact_confirmations WHERE id=?",
                             (cur.lastrowid,)).fetchone()
        return dict(row)

    # ---------- supply snapshots ----------

    def snapshot_find_by_task_key(self, tracking_task_id: str,
                                  idempotency_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m4b_snapshots WHERE tracking_task_id=? AND idempotency_key=?",
                (tracking_task_id, idempotency_key)).fetchone()
        return dict(row) if row else None

    def snapshot_next_version(self, snapshot_id: str) -> int:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT MAX(snapshot_version) FROM m4b_snapshots WHERE snapshot_id=?",
                             (snapshot_id,)).fetchone()
        return (int(row[0]) if row and row[0] is not None else 0) + 1

    def snapshot_insert(self, *, snapshot_id: str, snapshot_version: int, tenant_id: str,
                        site_id: str, tracking_task_id: str, idempotency_key: str,
                        input_checksum: str, checksum: str, generated_at: str,
                        observed_at: str, response: dict[str, Any]) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO m4b_snapshots (snapshot_id, snapshot_version, tenant_id, site_id,"
                " tracking_task_id, idempotency_key, input_checksum, checksum, generated_at,"
                " observed_at, response_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, snapshot_version, tenant_id, site_id, tracking_task_id,
                 idempotency_key, input_checksum, checksum, generated_at, observed_at,
                 json_text(response)))

    def snapshot_get_exact(self, snapshot_id: str, snapshot_version: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m4b_snapshots WHERE snapshot_id=? AND snapshot_version=?",
                (snapshot_id, snapshot_version)).fetchone()
        return dict(row) if row else None

    # ---------- supply events ----------

    def event_next_version(self, tenant_id: str, site_id: str) -> int:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT MAX(event_version) FROM m4b_events WHERE tenant_id=? AND site_id=?",
                (tenant_id, site_id)).fetchone()
        return (int(row[0]) if row and row[0] is not None else 0) + 1

    def event_insert(self, *, tenant_id: str, site_id: str, event: dict[str, Any]) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO m4b_events (tenant_id, site_id, event_id, event_version, checksum,"
                " payload_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (tenant_id, site_id, event["event_id"], int(event["event_version"]),
                 event["checksum"], json_text(event), utc_now()))

    def event_list(self, tenant_id: str, site_id: str, *, after_version: int,
                   limit: int) -> tuple[list[dict[str, Any]], bool]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM m4b_events WHERE tenant_id=? AND site_id=? AND event_version>?"
                " ORDER BY event_version LIMIT ?",
                (tenant_id, site_id, after_version, limit + 1)).fetchall()
        records = [dict(r) for r in rows]
        has_more = len(records) > limit
        payloads = [json.loads(r["payload_json"]) for r in records[:limit]]
        return payloads, has_more

    # ---------- schedule-impact proposals ----------

    def proposal_find_by_key(self, tracking_task_id: str,
                             idempotency_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m4b_impact_proposals WHERE tracking_task_id=? AND idempotency_key=?",
                (tracking_task_id, idempotency_key)).fetchone()
        return dict(row) if row else None

    def proposal_find_by_task_proposal(self, tracking_task_id: str,
                                       proposal_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM m4b_impact_proposals WHERE tracking_task_id=? AND proposal_id=?",
                (tracking_task_id, proposal_id)).fetchone()
        return dict(row) if row else None

    def proposal_insert(self, *, proposal_id: str, tenant_id: str, tracking_task_id: str,
                        idempotency_key: str, plan_version: str, scenario_id: str,
                        material_id: str, impact_type: str, reason_code: str,
                        required_quantity: str, uom: str,
                        previous_required_at: str | None, required_at: str,
                        affected_orders: list[str], evidence: list[dict[str, Any]],
                        request_payload: dict[str, Any], input_checksum: str) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as db:
            cur = db.execute(
                "INSERT INTO m4b_impact_proposals (proposal_id, tenant_id, tracking_task_id,"
                " idempotency_key, plan_version, scenario_id, material_id, impact_type,"
                " reason_code, required_quantity, uom, previous_required_at, required_at,"
                " affected_orders_json, evidence_json, request_payload_json, input_checksum,"
                " application_status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (proposal_id, tenant_id, tracking_task_id, idempotency_key, plan_version,
                 scenario_id, material_id, impact_type, reason_code, required_quantity, uom,
                 previous_required_at, required_at, json_text(affected_orders),
                 json_text(evidence), json_text(request_payload), input_checksum,
                 "pending", now, now))
            row = db.execute("SELECT * FROM m4b_impact_proposals WHERE id=?",
                             (cur.lastrowid,)).fetchone()
        return dict(row)


def input_checksum_plain(payload: dict[str, Any]) -> str:
    """receive 幂等校验（旧 schedule_impact_service 用 sort_keys canonical JSON）。"""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + sha256(canonical.encode("utf-8")).hexdigest()


def parse_received_at(value: Any) -> str:
    """received_at 宽松解析：RFC 3339 / 无时区 datetime；缺省=now。"""
    if value in (None, ""):
        return utc_now()
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"VALIDATION_ERROR: received_at 必须为 ISO datetime: {text!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
