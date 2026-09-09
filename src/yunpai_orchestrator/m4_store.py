"""M4 采购本地存储（S3 L-M4A；sqlite 直存）。

- 归属三要素：tenant_id / site_id / tracking_task_id。受控行三列非空；
  legacy 无归属行三列全 NULL（仅 CSV 导入且 YUNPAI_M4_ALLOW_UNSCOPED_IMPORT
  开启时产生），只读可见、受控写不可达、不补造身份。
- revision/checksum：内容性快照 sha256；review 决策不 bump revision（同旧 gate）。
- 命名/风格同 m1_domain / repository：原生 sqlite3、表名 m4_*、时间 utc iso 文本。
- 领域函数直接抛 ValueError（稳定码前缀），handler 层只做 ctx 授权与形状组装；
  存储草案见 docs/records/artifacts/s3m4a-store-interface.md。
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import UTC, date as date_type, datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from typing import Any

SCHEMA_VERSION = "s3m4a-v0.1"

_VALIDATION_STATUSES = ("valid", "invalid", "duplicate")
_PO_REVIEW_DECISIONS = ("pending_review", "request_changes", "rejected", "approved_for_message")

CSV_REQUIRED_FIELDS = ("item_code", "item_name", "quantity", "unit", "required_date")
CSV_EXPECTED_FIELDS = (
    "item_code", "item_name", "quantity", "unit", "supplier_name",
    "required_date", "project_code", "remark",
)
CSV_TRACE_FIELDS = ("order_line_id", "contributing_order_line_ids", "contributing_plan_ids")

ITEM_DIGEST_EXTENSION_FIELDS = (
    "order_line_id", "contributing_order_line_ids", "contributing_plan_ids",
    "material_code", "supplier_id",
)
TOP_DIGEST_EXTENSION_FIELDS = ("procurement_plan_ids",)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS m4_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS m4_import_batch (
  id INTEGER PRIMARY KEY,
  filename TEXT NOT NULL,
  total_rows INTEGER NOT NULL DEFAULT 0,
  valid_rows INTEGER NOT NULL DEFAULT 0,
  invalid_rows INTEGER NOT NULL DEFAULT 0,
  duplicate_rows INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  tenant_id TEXT, site_id TEXT, tracking_task_id TEXT, idempotency_key TEXT,
  source_module TEXT, source_plan_id TEXT, source_plan_ids TEXT,
  source_plan_version TEXT, source_plan_checksum TEXT,
  source_order_id TEXT, source_order_version TEXT,
  project_id TEXT, bom_id TEXT, source_event_id TEXT,
  observed_at TEXT, actor TEXT, data_scope TEXT,
  payload_digest TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_m4_import_batch_task_idem
  ON m4_import_batch(tracking_task_id, idempotency_key)
  WHERE tracking_task_id IS NOT NULL AND idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS m4_suggestion_item (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL,
  row_number INTEGER NOT NULL,
  item_code TEXT, item_name TEXT, quantity REAL, unit TEXT,
  supplier_name TEXT, required_date TEXT,
  project_code TEXT, remark TEXT,
  validation_status TEXT NOT NULL,
  error_message TEXT NOT NULL DEFAULT '',
  order_line_id TEXT,
  contributing_order_line_ids TEXT,
  contributing_plan_ids TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_m4_suggestion_item_batch ON m4_suggestion_item(batch_id);

CREATE TABLE IF NOT EXISTS m4_purchase_order (
  id INTEGER PRIMARY KEY,
  purchase_order_no TEXT NOT NULL UNIQUE,
  tenant_id TEXT, site_id TEXT, supplier_id INTEGER,
  supplier_name TEXT NOT NULL,
  external_order_no TEXT, source_system TEXT NOT NULL DEFAULT 'm4',
  generation_key TEXT,
  source_import_batch_id INTEGER,
  tracking_task_id TEXT,
  current_revision INTEGER, current_checksum TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  order_date TEXT, required_date TEXT, expected_delivery_date TEXT,
  payment_terms TEXT, tax_rate REAL, tax_included INTEGER,
  delivery_address TEXT, summary TEXT,
  reviewed_by TEXT, reviewed_at TEXT, review_comment TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_m4_purchase_order_tenant_site ON m4_purchase_order(tenant_id, site_id);
CREATE INDEX IF NOT EXISTS ix_m4_purchase_order_status ON m4_purchase_order(status);

CREATE TABLE IF NOT EXISTS m4_purchase_order_item (
  id INTEGER PRIMARY KEY,
  purchase_order_id INTEGER NOT NULL,
  suggestion_item_id INTEGER,
  item_code TEXT NOT NULL, internal_material_no TEXT,
  item_name TEXT NOT NULL,
  quantity REAL NOT NULL, unit TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  promised_date TEXT, unit_price REAL, line_amount REAL, tax_rate REAL,
  currency TEXT, remark TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_m4_po_item_suggestion
  ON m4_purchase_order_item(suggestion_item_id)
  WHERE suggestion_item_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS m4_purchase_order_revision (
  id INTEGER PRIMARY KEY,
  purchase_order_id INTEGER NOT NULL,
  tracking_task_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  checksum TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  principal TEXT NOT NULL,
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (purchase_order_id, revision)
);

CREATE TABLE IF NOT EXISTS m4_purchase_order_review (
  id INTEGER PRIMARY KEY,
  purchase_order_id INTEGER NOT NULL,
  tracking_task_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  checksum TEXT NOT NULL,
  decision TEXT NOT NULL,
  principal TEXT NOT NULL,
  comment TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_m4_po_review_order ON m4_purchase_order_review(purchase_order_id, revision);

CREATE TABLE IF NOT EXISTS m4_supplier_message (
  id INTEGER PRIMARY KEY,
  purchase_order_id INTEGER NOT NULL,
  channel TEXT NOT NULL DEFAULT 'email',
  subject TEXT, content TEXT NOT NULL,
  send_status TEXT NOT NULL DEFAULT 'generated',
  recipient TEXT,
  tracking_task_id TEXT,
  purchase_order_revision INTEGER, purchase_order_checksum TEXT,
  current_revision INTEGER, current_checksum TEXT,
  status TEXT,
  recipient_snapshot TEXT,
  attachment_snapshot TEXT,
  model_name TEXT, prompt_version TEXT, generation_language TEXT,
  recorded_by TEXT, recorded_at TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (purchase_order_id, tracking_task_id, purchase_order_revision, purchase_order_checksum)
);

CREATE TABLE IF NOT EXISTS m4_supplier_message_revision (
  id INTEGER PRIMARY KEY,
  supplier_message_id INTEGER NOT NULL,
  tracking_task_id TEXT NOT NULL,
  purchase_order_revision INTEGER NOT NULL,
  purchase_order_checksum TEXT NOT NULL,
  revision INTEGER NOT NULL,
  checksum TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  principal TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (supplier_message_id, revision)
);

CREATE TABLE IF NOT EXISTS m4_outbound_record (
  id INTEGER PRIMARY KEY,
  purchase_order_id INTEGER NOT NULL,
  channel TEXT NOT NULL DEFAULT 'email',
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  note TEXT,
  tenant_id TEXT, site_id TEXT, tracking_task_id TEXT,
  payload TEXT,
  created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date_type)):
        return value.isoformat()
    raise TypeError(f"unsupported canonical value: {value!r}")


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=_json_default)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_checksum(snapshot: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(snapshot))


def _to_number(value: Any) -> float | None:
    """数量归一：Decimal/数字/数字字符串 → float；非法 → None。"""
    if value is None or value == "":
        return None
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _digest_number(value: Any) -> str:
    """数量摘要形态：Decimal 规范化字符串（100 → '100'，100.5 → '100.5'）。

    非法数量（如 'abc'）按行级校验判 invalid，摘要侧不抛错，原样字符串化。
    """
    if value is None or value == "":
        return ""
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError, TypeError):
        return str(value)


def _strip_empty_extension(value: Any, fields: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if not (k in fields and not v)}
    return value


def command_digest(command: dict[str, Any]) -> str:
    """导入命令摘要（对齐旧 csv_import_service._command_digest 语义）。

    顶层剔除空扩展字段；每条建议剔除空扩展字段并把 quantity 类数值规范化为
    Decimal 字符串，保证 int/float/str 同义输入摘要一致。
    """
    payload: dict[str, Any] = {}
    for key, value in sorted(command.items()):
        payload[key] = _strip_empty_extension(value, TOP_DIGEST_EXTENSION_FIELDS)
    items = []
    for item in command.get("suggestions") or []:
        if not isinstance(item, dict):
            continue
        normalized: dict[str, Any] = {}
        for key, value in sorted(item.items()):
            if key == "quantity":
                normalized[key] = _digest_number(value)
            elif key in ("required_date", "observed_at") and value not in (None, ""):
                normalized[key] = str(value)
            else:
                normalized[key] = value
        items.append(_strip_empty_extension(normalized, ITEM_DIGEST_EXTENSION_FIELDS))
    payload["suggestions"] = items
    return sha256_hex(canonical_json(payload))


def batch_snapshot_to_json(row: dict[str, Any]) -> dict[str, Any]:
    """批次读形状（manifest import 输出 + items=SuggestionItemRead 同构）。"""
    def _json_list(value: Any) -> Any:
        if not value:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    return {
        "id": row["id"],
        "filename": row["filename"],
        "total_rows": row["total_rows"],
        "valid_rows": row["valid_rows"],
        "invalid_rows": row["invalid_rows"],
        "duplicate_rows": row["duplicate_rows"],
        "status": row["status"],
        "tenant_id": row.get("tenant_id"),
        "site_id": row.get("site_id"),
        "tracking_task_id": row.get("tracking_task_id"),
        "idempotency_key": row.get("idempotency_key"),
        "source_module": row.get("source_module"),
        "source_plan_id": row.get("source_plan_id"),
        "source_plan_ids": _json_list(row.get("source_plan_ids")),
        "source_plan_version": row.get("source_plan_version"),
        "source_plan_checksum": row.get("source_plan_checksum"),
        "source_order_id": row.get("source_order_id"),
        "source_order_version": row.get("source_order_version"),
        "project_id": row.get("project_id"),
        "bom_id": row.get("bom_id"),
        "source_event_id": row.get("source_event_id"),
        "observed_at": row.get("observed_at"),
        "actor": row.get("actor"),
        "data_scope": row.get("data_scope"),
        "payload_digest": row.get("payload_digest"),
    }


def suggestion_item_to_json(row: dict[str, Any]) -> dict[str, Any]:
    """建议项读形状（SuggestionItemRead 同构）。"""
    def _json_list(value: Any) -> Any:
        if not value:
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    return {
        "id": row["id"],
        "batch_id": row["batch_id"],
        "row_number": row["row_number"],
        "item_code": row["item_code"],
        "item_name": row["item_name"],
        "quantity": row["quantity"],
        "unit": row["unit"],
        "supplier_name": row["supplier_name"],
        "required_date": row["required_date"],
        "project_code": row["project_code"],
        "remark": row["remark"],
        "validation_status": row["validation_status"],
        "error_message": row["error_message"] or "",
        "order_line_id": row.get("order_line_id"),
        "contributing_order_line_ids": _json_list(row.get("contributing_order_line_ids")),
        "contributing_plan_ids": _json_list(row.get("contributing_plan_ids")),
    }


def purchase_order_item_to_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "suggestion_item_id": row.get("suggestion_item_id"),
        "item_code": row["item_code"],
        "internal_material_no": row.get("internal_material_no"),
        "item_name": row["item_name"],
        "quantity": row["quantity"],
        "unit": row["unit"],
        "status": row["status"],
        "remark": row.get("remark"),
    }


def purchase_order_to_json(
    order: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """PO 契约形状（manifest 键 + 旧 API 同款 current_revision/current_checksum/归属键）。

    revision/checksum 是 submit/approve/request_changes 的 CAS 输入来源；归属键供
    M4B 与调用方读取；manifest 未设 additionalProperties:false，无冲突。
    """
    return {
        "id": order["id"],
        "purchase_order_no": order["purchase_order_no"],
        "tenant_id": order.get("tenant_id"),
        "site_id": order.get("site_id"),
        "source_import_batch_id": order.get("source_import_batch_id"),
        "tracking_task_id": order.get("tracking_task_id"),
        "current_revision": order.get("current_revision"),
        "current_checksum": order.get("current_checksum"),
        "supplier_name": order["supplier_name"],
        "status": order["status"],
        "required_date": order.get("required_date"),
        "items": [purchase_order_item_to_json(item) for item in items],
    }


def order_snapshot(order: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """内容性快照（revision 用；不含审计/决策列）。"""
    return {
        "purchase_order_id": order["id"],
        "purchase_order_no": order["purchase_order_no"],
        "tenant_id": order.get("tenant_id"),
        "site_id": order.get("site_id"),
        "tracking_task_id": order.get("tracking_task_id"),
        "supplier_name": order["supplier_name"],
        "source_system": order.get("source_system"),
        "source_import_batch_id": order.get("source_import_batch_id"),
        "order_date": order.get("order_date"),
        "required_date": order.get("required_date"),
        "expected_delivery_date": order.get("expected_delivery_date"),
        "payment_terms": order.get("payment_terms"),
        "tax_rate": order.get("tax_rate"),
        "tax_included": order.get("tax_included"),
        "delivery_address": order.get("delivery_address"),
        "summary": order.get("summary"),
        "items": [
            {
                "id": item["id"],
                "suggestion_item_id": item.get("suggestion_item_id"),
                "item_code": item["item_code"],
                "internal_material_no": item.get("internal_material_no"),
                "item_name": item["item_name"],
                "quantity": item["quantity"],
                "unit": item["unit"],
                "status": item["status"],
                "remark": item.get("remark"),
            }
            for item in sorted(items, key=lambda row: row["id"])
        ],
    }


def message_snapshot(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "supplier_message_id": message["id"],
        "purchase_order_id": message["purchase_order_id"],
        "tracking_task_id": message.get("tracking_task_id"),
        "purchase_order_revision": message.get("purchase_order_revision"),
        "purchase_order_checksum": message.get("purchase_order_checksum"),
        "channel": message["channel"],
        "subject": message.get("subject"),
        "content": message["content"],
        "recipient": message.get("recipient"),
        "recipient_snapshot": json.loads(message["recipient_snapshot"]) if message.get("recipient_snapshot") else {},
        "attachment_snapshot": json.loads(message["attachment_snapshot"]) if message.get("attachment_snapshot") else [],
    }


def parse_suggestions_csv(filename: str, content: bytes) -> dict[str, Any]:
    """CSV 解析（旧 csv_import_service 语义）：文件级失败或行级 valid/invalid/duplicate。

    返回 {totals..., status, items:[raw rows without id/batch_id]}；status 仅
    completed/failed（文件级失败产生 1 条 invalid 占位行）。
    """
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(StringIO(text))
    missing_headers = [f for f in CSV_EXPECTED_FIELDS if f not in (reader.fieldnames or [])]
    if missing_headers:
        return {
            "total_rows": 0, "valid_rows": 0, "invalid_rows": 1, "duplicate_rows": 0,
            "status": "failed",
            "items": [{
                "row_number": 1, "item_code": None, "item_name": None, "quantity": None,
                "unit": None, "supplier_name": None, "required_date": None,
                "project_code": None, "remark": None, "validation_status": "invalid",
                "error_message": f"缺少字段: {', '.join(missing_headers)}",
            }],
        }

    seen_keys: set[tuple[str, str, str, str]] = set()
    items: list[dict[str, Any]] = []
    has_trace_headers = all(f in (reader.fieldnames or []) for f in CSV_TRACE_FIELDS)

    def _parse_date(value: Any) -> str | None:
        value = _clean(value)
        if value is None:
            return None
        from datetime import date as _date
        try:
            return _date.fromisoformat(value).isoformat()
        except ValueError:
            return None

    def _parse_str_list(value: Any) -> str | None:
        value = _clean(value)
        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, list):
            return None
        cleaned = [_clean(str(item)) for item in parsed]
        cleaned = [item for item in cleaned if item]
        if not cleaned:
            return None
        return json.dumps(cleaned, ensure_ascii=False)

    for index, raw_row in enumerate(reader, start=2):
        issues: list[str] = []
        quantity = _to_number(raw_row.get("quantity"))
        for field in CSV_REQUIRED_FIELDS:
            if not _clean(raw_row.get(field)):
                issues.append(f"{field} 为必填")
        if quantity is not None and quantity <= 0:
            issues.append("数量必须大于0")
        if _clean(raw_row.get("quantity")) and quantity is None:
            issues.append("数量格式错误")
        required_date = _parse_date(raw_row.get("required_date"))
        if _clean(raw_row.get("required_date")) and required_date is None:
            issues.append("日期格式必须为 YYYY-MM-DD")

        duplicate = False
        key = (
            _clean(raw_row.get("item_code")) or "",
            _clean(raw_row.get("supplier_name")) or "",
            _clean(raw_row.get("required_date")) or "",
            _clean(raw_row.get("project_code")) or "",
        )
        if not issues and key in seen_keys:
            duplicate = True
        seen_keys.add(key)

        if duplicate:
            status, error_message = "duplicate", "同一导入批次内重复"
        elif issues:
            status, error_message = "invalid", "; ".join(issues)
        else:
            status, error_message = "valid", ""

        items.append({
            "row_number": index,
            "item_code": _clean(raw_row.get("item_code")),
            "item_name": _clean(raw_row.get("item_name")),
            "quantity": quantity,
            "unit": _clean(raw_row.get("unit")),
            "supplier_name": _clean(raw_row.get("supplier_name")),
            "required_date": required_date,
            "project_code": _clean(raw_row.get("project_code")),
            "remark": _clean(raw_row.get("remark")),
            "validation_status": status,
            "error_message": error_message,
            "order_line_id": _clean(raw_row.get("order_line_id")) if has_trace_headers else None,
            "contributing_order_line_ids": _parse_str_list(raw_row.get("contributing_order_line_ids")) if has_trace_headers else None,
            "contributing_plan_ids": _parse_str_list(raw_row.get("contributing_plan_ids")) if has_trace_headers else None,
        })

    valid_rows = sum(1 for item in items if item["validation_status"] == "valid")
    invalid_rows = sum(1 for item in items if item["validation_status"] == "invalid")
    duplicate_rows = sum(1 for item in items if item["validation_status"] == "duplicate")
    return {
        "total_rows": len(items), "valid_rows": valid_rows,
        "invalid_rows": invalid_rows, "duplicate_rows": duplicate_rows,
        "status": "completed", "items": items,
    }


class M4Store:
    """M4 采购 sqlite 存储（每 handler 调用一实例，操作自带事务提交）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._ensure_schema()

    # ---------- 基础设施 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            row = conn.execute("SELECT value FROM m4_store_meta WHERE key='schema_version'").fetchone()
            if row is not None and row["value"] != SCHEMA_VERSION:
                raise ValueError(
                    f"SCHEMA_VERSION_MISMATCH: m4 store schema {row['value']} != 本模块 {SCHEMA_VERSION}；"
                    "需要整合线统一迁移，禁止静默升级"
                )
            conn.execute(
                "INSERT OR IGNORE INTO m4_store_meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()
        finally:
            conn.close()

    def _row(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def _fetch_order(self, purchase_order_id: int) -> dict[str, Any] | None:
        return self._row(
            "SELECT * FROM m4_purchase_order WHERE id=?", (purchase_order_id,)
        )

    def _fetch_order_items(self, purchase_order_id: int) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM m4_purchase_order_item WHERE purchase_order_id=? ORDER BY id",
            (purchase_order_id,),
        )

    def order_items(self, purchase_order_id: int) -> list[dict[str, Any]]:
        """公共只读口：采购单行项（供 handler/M4B 只读面）。"""
        return self._fetch_order_items(purchase_order_id)

    # ---------- 导入批次 ----------

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return self._row("SELECT * FROM m4_import_batch WHERE id=?", (batch_id,))

    def get_batch_by_idempotency(self, tracking_task_id: str, idempotency_key: str) -> dict[str, Any] | None:
        return self._row(
            "SELECT * FROM m4_import_batch WHERE tracking_task_id=? AND idempotency_key=?",
            (tracking_task_id, idempotency_key),
        )

    def get_batch_rows(self, batch_id: int) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM m4_suggestion_item WHERE batch_id=? ORDER BY id", (batch_id,)
        )

    def create_batch_with_rows(
        self,
        *,
        filename: str,
        parsed: dict[str, Any],
        scope: dict[str, Any],
        command: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """插入批次+建议项（单事务）。command 提供持久化身份字段与 payload_digest。"""
        conn = self._connect()
        try:
            now = utc_now()
            if command is None:
                digest = None
                batch_fields = {}
            else:
                digest = command_digest(command)
                batch_fields = {
                    "source_module": _clean(command.get("source_module")),
                    "source_plan_id": _clean(command.get("procurement_plan_id")),
                    "source_plan_ids": _normalize_plan_ids(command.get("procurement_plan_ids")),
                    "source_plan_version": _clean(command.get("procurement_plan_version_id")),
                    "source_plan_checksum": _clean(command.get("source_plan_checksum")),
                    "source_order_id": _clean(command.get("order_id")),
                    "source_order_version": _clean(command.get("order_version")),
                    "project_id": _clean(command.get("project_id")),
                    "bom_id": _clean(command.get("bom_id")),
                    "source_event_id": _clean(command.get("source_event_id")),
                    "observed_at": str(command["observed_at"]) if command.get("observed_at") is not None else None,
                    "actor": _clean(command.get("actor")),
                    "data_scope": _clean(command.get("data_scope")),
                }
            cursor = conn.execute(
                "INSERT INTO m4_import_batch (filename, total_rows, valid_rows, invalid_rows,"
                " duplicate_rows, status, tenant_id, site_id, tracking_task_id, idempotency_key,"
                " source_module, source_plan_id, source_plan_ids, source_plan_version,"
                " source_plan_checksum, source_order_id, source_order_version, project_id,"
                " bom_id, source_event_id, observed_at, actor, data_scope, payload_digest, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    filename, parsed["total_rows"], parsed["valid_rows"], parsed["invalid_rows"],
                    parsed["duplicate_rows"], parsed["status"],
                    scope.get("tenant_id"), scope.get("site_id"),
                    scope.get("tracking_task_id"), scope.get("idempotency_key"),
                    batch_fields.get("source_module"), batch_fields.get("source_plan_id"),
                    batch_fields.get("source_plan_ids"), batch_fields.get("source_plan_version"),
                    batch_fields.get("source_plan_checksum"), batch_fields.get("source_order_id"),
                    batch_fields.get("source_order_version"), batch_fields.get("project_id"),
                    batch_fields.get("bom_id"), batch_fields.get("source_event_id"),
                    batch_fields.get("observed_at"), batch_fields.get("actor"),
                    batch_fields.get("data_scope"), digest, now,
                ),
            )
            batch_id = int(cursor.lastrowid)
            for item in parsed["items"]:
                conn.execute(
                    "INSERT INTO m4_suggestion_item (batch_id, row_number, item_code, item_name,"
                    " quantity, unit, supplier_name, required_date, project_code, remark,"
                    " validation_status, error_message, order_line_id,"
                    " contributing_order_line_ids, contributing_plan_ids, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch_id, item["row_number"], item.get("item_code"), item.get("item_name"),
                        item.get("quantity"), item.get("unit"), item.get("supplier_name"),
                        item.get("required_date"), item.get("project_code"), item.get("remark"),
                        item["validation_status"], item.get("error_message") or "",
                        item.get("order_line_id"), item.get("contributing_order_line_ids"),
                        item.get("contributing_plan_ids"), now,
                    ),
                )
            conn.commit()
            batch = self.get_batch(batch_id)
            assert batch is not None
            return {**batch_snapshot_to_json(batch),
                    "items": [suggestion_item_to_json(row) for row in self.get_batch_rows(batch_id)]}
        finally:
            conn.close()

    # ---------- 建议查询 ----------

    def list_suggestions(
        self,
        *,
        page: int,
        page_size: int,
        batch_id: int | None,
        supplier_name: str | None,
        item_code: str | None,
        validation_status: str | None,
        tenant_id: str,
    ) -> dict[str, Any]:
        where_parts = ["(b.tenant_id = ? OR b.tenant_id IS NULL)"]
        params: list[Any] = [tenant_id]
        if batch_id is not None:
            where_parts.append("s.batch_id=?")
            params.append(batch_id)
        if supplier_name:
            where_parts.append("s.supplier_name LIKE ?")
            params.append(f"%{supplier_name}%")
        if item_code:
            where_parts.append("s.item_code LIKE ?")
            params.append(f"%{item_code}%")
        if validation_status is not None:
            if validation_status not in _VALIDATION_STATUSES:
                raise ValueError(
                    f"VALIDATION_ERROR: validation_status 不合法（allowed={list(_VALIDATION_STATUSES)}）"
                )
            where_parts.append("s.validation_status=?")
            params.append(validation_status)
        sql_where = " AND ".join(where_parts)
        conn = self._connect()
        try:
            count = conn.execute(
                f"SELECT COUNT(*) FROM m4_suggestion_item s JOIN m4_import_batch b ON b.id=s.batch_id"
                f" WHERE {sql_where}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT s.* FROM m4_suggestion_item s JOIN m4_import_batch b ON b.id=s.batch_id"
                f" WHERE {sql_where} ORDER BY s.id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return {
                "items": [suggestion_item_to_json(dict(row)) for row in rows],
                "page": page, "page_size": page_size, "total": int(count),
            }
        finally:
            conn.close()

    # ---------- 采购单 ----------

    def resolve_generation_inputs(
        self, suggestion_item_ids: list[int]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """按 id 取建议项（含批次归属）；缺失/跨批/归属残缺在此裁决。"""
        ids = list(dict.fromkeys(suggestion_item_ids))
        if not ids:
            raise ValueError("VALIDATION_ERROR: suggestion_item_ids 至少一项")
        placeholders = ",".join("?" for _ in ids)
        items = self._rows(
            f"SELECT s.*, b.tenant_id AS batch_tenant_id, b.site_id AS batch_site_id,"
            f" b.tracking_task_id AS batch_tracking_task_id, b.id AS batch_id"
            f" FROM m4_suggestion_item s JOIN m4_import_batch b ON b.id=s.batch_id"
            f" WHERE s.id IN ({placeholders})",
            tuple(ids),
        )
        by_id = {item["id"]: item for item in items}
        missing = [item_id for item_id in ids if item_id not in by_id]
        if missing:
            raise ValueError(f"NOT_FOUND: 原始清单项不存在（ids={missing}）")
        batch_ids = sorted({item["batch_id"] for item in items})
        if len(batch_ids) != 1:
            raise ValueError(
                f"VALIDATION_ERROR: 采购建议项必须来自同一导入批次（batch_ids={batch_ids}）"
            )
        batch = self.get_batch(batch_ids[0])
        assert batch is not None
        batch_tenant = _clean(batch["tenant_id"])
        batch_site = _clean(batch["site_id"])
        if (batch_tenant is None) != (batch_site is None):
            raise ValueError(
                f"IMPORT_BATCH_SCOPE_INCOMPLETE: 导入批次缺少完整 tenant/site 归属，不能生成采购单"
                f"（batch_id={batch['id']}）"
            )
        return items, batch

    def next_purchase_order_no(self) -> str:
        from datetime import date as _date
        today = _date.today().strftime("%Y%m%d")
        prefix = f"PO-{today}-"
        conn = self._connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM m4_purchase_order WHERE purchase_order_no LIKE ?",
                (f"{prefix}%",),
            ).fetchone()[0]
            return f"{prefix}{int(count) + 1:03d}"
        finally:
            conn.close()

    def list_purchase_orders(
        self,
        *,
        page: int,
        page_size: int,
        status: str | None,
        supplier_name: str | None,
        tenant_id: str,
    ) -> dict[str, Any]:
        where = "(tenant_id = ? OR tenant_id IS NULL)"
        params: list[Any] = [tenant_id]
        if status:
            where += " AND status=?"
            params.append(status)
        if supplier_name:
            where += " AND supplier_name LIKE ?"
            params.append(f"%{supplier_name}%")
        conn = self._connect()
        try:
            count = conn.execute(
                f"SELECT COUNT(*) FROM m4_purchase_order WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM m4_purchase_order WHERE {where} ORDER BY id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            result = []
            for row in rows:
                order = dict(row)
                result.append(purchase_order_to_json(
                    order, self._fetch_order_items(order["id"])))
            return {"items": result, "page": page, "page_size": page_size, "total": int(count)}
        finally:
            conn.close()

    def get_purchase_order(self, purchase_order_id: int) -> dict[str, Any] | None:
        return self._fetch_order(purchase_order_id)

    def generate_purchase_orders(
        self,
        *,
        suggestion_ids: list[int],
        scope: tuple[str | None, str | None],
        batch: dict[str, Any],
        ctx_task_id: str | None,
        principal: str,
    ) -> list[dict[str, Any]]:
        """生成（含幂等 replay/冲突裁决）。scope = 已裁决 (tenant, site)。"""
        ids = list(dict.fromkeys(suggestion_ids))
        placeholders = ",".join("?" for _ in ids)
        suggestions = self._rows(
            f"SELECT * FROM m4_suggestion_item WHERE id IN ({placeholders})", tuple(ids)
        )
        by_id = {item["id"]: item for item in suggestions}
        invalid = [
            item["id"] for item in suggestions
            if item["validation_status"] != "valid" or not item["item_code"]
            or not item["item_name"] or item["quantity"] is None or not item["unit"]
        ]
        if invalid:
            raise ValueError(f"VALIDATION_ERROR: 只能从有效清单项生成采购单（invalid_ids={invalid}）")

        replay = self._resolve_generation_replay(
            ids, scope=scope, batch=batch, ctx_task_id=ctx_task_id)
        if replay is not None:
            return replay

        generation_key = self._generation_key(ids)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for suggestion_id in ids:
            item = by_id[suggestion_id]
            grouped.setdefault(item["supplier_name"] or "未指定供应商", []).append(item)

        conn = self._connect()
        try:
            now = utc_now()
            order_ids: list[int] = []
            for supplier_name, supplier_items in grouped.items():
                required_dates = [i["required_date"] for i in supplier_items if i["required_date"]]
                cursor = conn.execute(
                    "INSERT INTO m4_purchase_order (purchase_order_no, tenant_id, site_id,"
                    " supplier_name, source_system, generation_key, source_import_batch_id,"
                    " tracking_task_id, status, required_date, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self._next_purchase_order_no_on(conn), scope[0], scope[1],
                        supplier_name, "m4", generation_key, batch["id"],
                        batch.get("tracking_task_id"), "draft",
                        min(required_dates) if required_dates else None, now, now,
                    ),
                )
                order_id = int(cursor.lastrowid)
                for suggestion in supplier_items:
                    conn.execute(
                        "INSERT INTO m4_purchase_order_item (purchase_order_id, suggestion_item_id,"
                        " item_code, internal_material_no, item_name, quantity, unit, status,"
                        " remark, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            order_id, suggestion["id"], suggestion["item_code"] or "",
                            suggestion["item_code"] or None, suggestion["item_name"] or "",
                            suggestion["quantity"], suggestion["unit"] or "", "pending",
                            suggestion.get("remark"), now,
                        ),
                    )
                order_ids.append(order_id)
                if batch.get("tracking_task_id"):
                    self._append_order_revision_on(
                        conn, order_id,
                        tracking_task_id=batch["tracking_task_id"],
                        principal=principal, event_type="purchase_order.created",
                    )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            replay = self._resolve_generation_replay(
                ids, scope=scope, batch=batch, ctx_task_id=ctx_task_id)
            if replay is not None:
                return replay
            raise ValueError("PURCHASE_ORDER_GENERATION_CONFLICT: 采购单生成唯一性冲突，且重放核对未命中")
        finally:
            conn.close()

        orders = []
        for order_id in order_ids:
            order = self._fetch_order(order_id)
            assert order is not None
            orders.append(purchase_order_to_json(
                order, self._fetch_order_items(order_id)))
        return orders

    @staticmethod
    def _generation_key(suggestion_ids: list[int]) -> str:
        canonical = json.dumps(sorted(set(suggestion_ids)), separators=(",", ":"))
        return sha256_hex(canonical)

    def _next_purchase_order_no_on(self, conn: sqlite3.Connection) -> str:
        from datetime import date as _date
        today = _date.today().strftime("%Y%m%d")
        prefix = f"PO-{today}-"
        count = conn.execute(
            "SELECT COUNT(*) FROM m4_purchase_order WHERE purchase_order_no LIKE ?",
            (f"{prefix}%",),
        ).fetchone()[0]
        return f"{prefix}{int(count) + 1:03d}"

    def _resolve_generation_replay(
        self,
        suggestion_ids: list[int],
        *,
        scope: tuple[str | None, str | None],
        batch: dict[str, Any],
        ctx_task_id: str | None,
    ) -> list[dict[str, Any]] | None:
        requested = set(suggestion_ids)
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT i.suggestion_item_id AS sid, o.generation_key AS gk"
                " FROM m4_purchase_order_item i JOIN m4_purchase_order o ON o.id=i.purchase_order_id"
                f" WHERE i.suggestion_item_id IN ({','.join('?' for _ in requested)})",
                tuple(sorted(requested)),
            ).fetchall()
            if not rows:
                return None
            used_ids = {row["sid"] for row in rows}
            if used_ids != requested:
                self._raise_generation_conflict(requested, used_ids, "mixed_used_and_unused_suggestions")
            generation_keys = {row["gk"] for row in rows}
            expected_key = self._generation_key(list(requested))
            if generation_keys != {expected_key}:
                if None in generation_keys:
                    reason = "legacy_generation_identity_unavailable"
                elif len(generation_keys) == 1:
                    reason = "partial_generation_replay"
                else:
                    reason = "multiple_generation_requests"
                self._raise_generation_conflict(requested, used_ids, reason)
            order_rows = conn.execute(
                "SELECT * FROM m4_purchase_order WHERE generation_key=?", (expected_key,),
            ).fetchall()
            orders = [dict(row) for row in order_rows]
            generated_ids = {
                item_row["suggestion_item_id"]
                for order in orders
                for item_row in conn.execute(
                    "SELECT suggestion_item_id FROM m4_purchase_order_item WHERE purchase_order_id=?",
                    (order["id"],),
                ).fetchall()
                if item_row["suggestion_item_id"] is not None
            }
            if generated_ids != requested:
                self._raise_generation_conflict(requested, generated_ids, "partial_generation_replay")
            if any(
                (_clean(order["tenant_id"]), _clean(order["site_id"])) != (scope[0], scope[1])
                for order in orders
            ):
                self._raise_generation_conflict(requested, generated_ids, "generation_scope_mismatch")
            if any(order["source_import_batch_id"] != batch["id"] for order in orders):
                self._raise_generation_conflict(requested, generated_ids, "source_import_batch_mismatch")
            if any(order["tracking_task_id"] != ctx_task_id for order in orders):
                self._raise_generation_conflict(requested, generated_ids, "tracking_task_id_mismatch")
            return [
                purchase_order_to_json(order, self._fetch_order_items(order["id"]))
                for order in sorted(orders, key=lambda row: row["id"])
            ]
        finally:
            conn.close()

    @staticmethod
    def _raise_generation_conflict(
        requested_ids: set[int], used_ids: set[int], reason: str
    ) -> None:
        raise ValueError(
            f"PURCHASE_ORDER_GENERATION_CONFLICT: 采购建议项已被不同的采购单生成请求使用"
            f"（reason={reason}, requested={sorted(requested_ids)}, used={sorted(used_ids)}）"
        )

    def _append_order_revision_on(
        self,
        conn: sqlite3.Connection,
        purchase_order_id: int,
        *,
        tracking_task_id: str,
        principal: str,
        event_type: str,
    ) -> None:
        order_row = conn.execute(
            "SELECT * FROM m4_purchase_order WHERE id=?", (purchase_order_id,)
        ).fetchone()
        assert order_row is not None
        order = dict(order_row)
        item_rows = conn.execute(
            "SELECT * FROM m4_purchase_order_item WHERE purchase_order_id=? ORDER BY id",
            (purchase_order_id,),
        ).fetchall()
        items = [dict(row) for row in item_rows]
        snapshot = order_snapshot(order, items)
        checksum = canonical_checksum(snapshot)
        revision_number = (order["current_revision"] or 0) + 1
        conn.execute(
            "INSERT INTO m4_purchase_order_revision (purchase_order_id, tracking_task_id, revision,"
            " checksum, snapshot, principal, event_type, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                purchase_order_id, tracking_task_id, revision_number, checksum,
                json.dumps(snapshot, ensure_ascii=False, default=_json_default),
                principal, event_type, utc_now(),
            ),
        )
        conn.execute(
            "UPDATE m4_purchase_order SET current_revision=?, current_checksum=? WHERE id=?",
            (revision_number, checksum, purchase_order_id),
        )

    # ---------- 受控审核流 ----------

    def transition_order_review(
        self,
        *,
        purchase_order_id: int,
        ctx_task_id: str,
        expected_revision: int,
        expected_checksum: str,
        target_status: str,
        principal: str,
        comment: str | None,
    ) -> dict[str, Any]:
        order = self._fetch_order(purchase_order_id)
        if order is None:
            raise ValueError(f"NOT_FOUND: 采购单不存在（id={purchase_order_id}）")
        if not order["tracking_task_id"]:
            raise ValueError(
                "LEGACY_UNTRACKED_REVIEW_BLOCKED: 采购单无 tracking_task_id（历史/legacy），"
                "受控审核流不可达；不补造身份"
            )
        task_id = (ctx_task_id or "").strip()
        if not task_id or task_id != order["tracking_task_id"]:
            raise ValueError(
                f"TASK_ID_MISMATCH: X-Yunpai-Task-ID 与采购单 tracking_task_id 不一致"
                f"（ctx={task_id or '<empty>'}, order={order['tracking_task_id']}）"
            )
        self._assert_order_revision(order, expected_revision, expected_checksum)
        if order["status"] == target_status:
            return purchase_order_to_json(order, self._fetch_order_items(purchase_order_id))

        allowed = {
            ("draft", "pending_review"),
            ("request_changes", "pending_review"),
            ("rejected", "pending_review"),
            ("pending_review", "request_changes"),
            ("pending_review", "rejected"),
            ("pending_review", "approved_for_message"),
        }
        if (order["status"], target_status) not in allowed:
            raise ValueError(
                f"INVALID_STATUS: 不能从 {order['status']} 流转到 {target_status}"
            )
        now = utc_now()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE m4_purchase_order SET status=?, reviewed_by=?, reviewed_at=?,"
                " review_comment=?, updated_at=? WHERE id=?",
                (target_status, principal, now, comment, now, purchase_order_id),
            )
            conn.execute(
                "INSERT INTO m4_purchase_order_review (purchase_order_id, tracking_task_id, revision,"
                " checksum, decision, principal, comment, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    purchase_order_id, order["tracking_task_id"],
                    order["current_revision"] or 0, order["current_checksum"] or "",
                    target_status, principal, comment, now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        updated = self._fetch_order(purchase_order_id)
        assert updated is not None
        return purchase_order_to_json(updated, self._fetch_order_items(purchase_order_id))

    @staticmethod
    def _assert_order_revision(
        order: dict[str, Any], expected_revision: int | None, expected_checksum: str | None
    ) -> None:
        if expected_revision is None or expected_checksum is None:
            raise ValueError("VALIDATION_ERROR: expected_revision 与 expected_checksum 必填")
        if (
            order["current_revision"] != expected_revision
            or order["current_checksum"] != expected_checksum
        ):
            raise ValueError(
                f"REVISION_CONFLICT: 采购单 revision/checksum 已过期"
                f"（expected=({expected_revision},{expected_checksum[:8]}…),"
                f" current=({order['current_revision']},{str(order['current_checksum'] or '')[:8]}…)）"
            )

    def assert_order_revision(
        self, order: dict[str, Any], expected_revision: int | None, expected_checksum: str | None
    ) -> None:
        """公共 CAS 守卫（handler/领域同用）。"""
        self._assert_order_revision(order, expected_revision, expected_checksum)

    # ---------- 供应商消息草稿 ----------

    def latest_supplier_message(self, purchase_order_id: int, tracking_task_id: str | None,
                                po_revision: int | None, po_checksum: str | None) -> dict[str, Any] | None:
        if not tracking_task_id or po_revision is None or po_checksum is None:
            return None
        return self._row(
            "SELECT * FROM m4_supplier_message WHERE purchase_order_id=? AND tracking_task_id=?"
            " AND purchase_order_revision=? AND purchase_order_checksum=? ORDER BY id DESC LIMIT 1",
            (purchase_order_id, tracking_task_id, po_revision, po_checksum),
        )

    def create_supplier_message(
        self,
        *,
        purchase_order_id: int,
        tracking_task_id: str,
        po_revision: int,
        po_checksum: str,
        channel: str,
        subject: str | None,
        content: str,
        recipient: str | None,
        recipient_snapshot: dict[str, Any],
        attachment_snapshot: list[Any],
        principal: str,
        model_name: str | None,
        prompt_version: str | None,
        language: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO m4_supplier_message (purchase_order_id, channel, subject, content,"
                " send_status, recipient, tracking_task_id, purchase_order_revision,"
                " purchase_order_checksum, status, recipient_snapshot, attachment_snapshot,"
                " model_name, prompt_version, generation_language, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    purchase_order_id, channel, subject, content, "generated", recipient,
                    tracking_task_id, po_revision, po_checksum, "draft",
                    json.dumps(recipient_snapshot, ensure_ascii=False),
                    json.dumps(attachment_snapshot, ensure_ascii=False),
                    model_name, prompt_version, language, now,
                ),
            )
            message_id = int(cursor.lastrowid)
            message = self._fetch_message(conn, message_id)
            assert message is not None
            snapshot = message_snapshot(message)
            checksum = canonical_checksum(snapshot)
            conn.execute(
                "UPDATE m4_supplier_message SET current_revision=1, current_checksum=? WHERE id=?",
                (checksum, message_id),
            )
            conn.execute(
                "INSERT INTO m4_supplier_message_revision (supplier_message_id, tracking_task_id,"
                " purchase_order_revision, purchase_order_checksum, revision, checksum, snapshot,"
                " principal, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    message_id, tracking_task_id, po_revision, po_checksum, 1, checksum,
                    json.dumps(snapshot, ensure_ascii=False, default=_json_default),
                    principal, now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        message = self._row("SELECT * FROM m4_supplier_message WHERE id=?", (message_id,))
        assert message is not None
        return message

    @staticmethod
    def _fetch_message(conn: sqlite3.Connection, message_id: int) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM m4_supplier_message WHERE id=?", (message_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    # ---------- legacy 出站记录 ----------

    def record_outbound(
        self,
        *,
        purchase_order_id: int,
        kind: str,
        note: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            now = utc_now()
            cursor = conn.execute(
                "INSERT INTO m4_outbound_record (purchase_order_id, channel, kind, status, note,"
                " tenant_id, site_id, tracking_task_id, payload, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    purchase_order_id, "email", kind, "recorded_only", note,
                    payload.get("tenant_id"), payload.get("site_id"),
                    payload.get("tracking_task_id"),
                    json.dumps(payload, ensure_ascii=False), now,
                ),
            )
            conn.commit()
            return {"id": int(cursor.lastrowid), "status": "recorded_only"}
        finally:
            conn.close()

    def mark_order_and_items_sent(self, purchase_order_id: int) -> None:
        now = utc_now()
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE m4_purchase_order SET status='sent', updated_at=? WHERE id=? AND tenant_id IS NULL",
                (now, purchase_order_id),
            )
            conn.execute(
                "UPDATE m4_purchase_order_item SET status='sent' WHERE purchase_order_id=? AND"
                " status='pending'",
                (purchase_order_id,),
            )
            conn.commit()
        finally:
            conn.close()


def _normalize_plan_ids(value: Any) -> str | None:
    if not value:
        return None
    cleaned = sorted({str(item).strip() for item in value if str(item).strip()})
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else None
