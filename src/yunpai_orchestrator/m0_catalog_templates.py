"""M0 文件模板适配器引擎（Batch D-2）：版本化 CSV/Excel → m0.ingest.v1。
> 迁入自 `_wt/INT/src/yunpai_langgraph/m0_catalog_templates.py`（LF 归一 sha256=c68c7ad80890307fc17ebb9e2d3a7854415f043ab7b4376d91b464e041a73782，34321B / 721 行）。
> M0 分片 C1 随迁（`_migration/rows-S1.md`）；本文件改动：无（逐行迁入）。

等价基准：`docs/records/artifacts/template-columns-notes.md`（旧 adapters.py
L33-1112 只读提取；本模块为表驱动重建，非整包复制）。与旧实现登记差异：
- 列契约检查（MISSING/UNEXPECTED_TEMPLATE_COLUMNS）按 sheet 表头一次，
  旧实现按数据行重复报（行数多时会重复膨胀报告）；
- canonical 记录级数值非法（quantity/loss_rate/日期）在适配期即报
  CANONICAL_CONTRACT_ERROR 并丢弃该记录（等价旧 finalize 丢弃语义）；
- 无旧实现的 pydantic strict payload 逐字段校验：本模块按笔记 §3 映射表做
  等价约束，未覆盖字段以宽松处理并在 R 记录注记。

组记录：order 按 order_no、bom 按 (bom_code, revision)、route 按
(route_code, revision)，跨 sheet 合并（组键保首见序）；组头一致性检查
INCONSISTENT_GROUP_VALUE（取首个非空值继续）。行键去重（DUPLICATE_*）由
canonical 层派生阶段负责（D-4 接入）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = ["TEMPLATES", "adapt_tabular_file", "canonical_json", "TemplateSpec"]

# ---- 模板契约表（列名一律 casefold 后比对）----

@dataclass(frozen=True)
class TemplateSpec:
    version: str
    entity_type: str
    required: tuple[str, ...]
    allowed: tuple[str, ...]           # required ∪ optional
    group_by: tuple[str, ...] = ()     # 组记录列（identity 键），空=单行单记录


TEMPLATES: dict[str, TemplateSpec] = {
    spec.version: spec for spec in [
        TemplateSpec("m0.product.v1", "product",
                     ("product_code", "name"),
                     ("product_code", "name", "model", "status", "aliases",
                      "family_code", "related_product_codes", "attributes_json")),
        TemplateSpec("m0.product-family.v1", "product_family",
                     ("family_code", "name"),
                     ("family_code", "name", "description", "status", "attributes_json")),
        TemplateSpec("m0.order.v1", "order",
                     ("order_no", "line_no", "product_code", "quantity", "uom"),
                     ("order_no", "line_no", "product_code", "quantity", "uom",
                      "customer", "order_date", "due_date", "status",
                      "requested_spec_json", "attributes_json"),
                     group_by=("order_no",)),
        TemplateSpec("m0.bom.v1", "bom",
                     ("bom_code", "revision", "product_code", "line_no",
                      "material_code", "quantity", "uom"),
                     ("bom_code", "revision", "product_code", "line_no",
                      "material_code", "quantity", "uom", "material_name",
                      "loss_rate", "status", "line_attributes_json", "attributes_json"),
                     group_by=("bom_code", "revision")),
        TemplateSpec("m0.document.v1", "document",
                     ("document_no", "revision", "role", "title", "product_codes"),
                     ("document_no", "revision", "role", "title", "product_codes",
                      "content_uri", "status", "attributes_json")),
        TemplateSpec("m0.material.v1", "material",
                     ("material_code", "name"),
                     ("material_code", "name", "unit", "status", "aliases",
                      "spec_json", "attributes_json")),
        TemplateSpec("m0.supplier.v1", "supplier",
                     ("supplier_code", "name"),
                     ("supplier_code", "name", "legal_id", "status", "material_codes",
                      "aliases", "attributes_json")),
        TemplateSpec("m0.equipment.v1", "equipment",
                     ("equipment_code", "name"),
                     ("equipment_code", "name", "equipment_type", "line", "status",
                      "attributes_json")),
        TemplateSpec("m0.route.v1", "process_route",
                     ("route_code", "revision", "product_code", "sequence_no",
                      "operation_code"),
                     ("route_code", "revision", "product_code", "sequence_no",
                      "operation_code", "operation_name", "standard_time", "station_code",
                      "material_codes", "equipment_codes", "tooling_codes", "status",
                      "row_attributes_json", "attributes_json"),
                     group_by=("route_code", "revision")),
        TemplateSpec("m0.operation.v1", "operation",
                     ("operation_code", "name"),
                     ("operation_code", "name", "standard_time", "status", "attributes_json")),
        TemplateSpec("m0.tooling.v1", "tooling",
                     ("tooling_code", "name"),
                     ("tooling_code", "name", "tooling_type", "status", "attributes_json")),
    ]
}

STATUS_LITERALS: dict[str, tuple[str, ...]] = {
    "product": ("active", "inactive", "deprecated"),
    "product_family": ("active", "inactive", "deprecated"),
    "order": ("draft", "active", "completed", "cancelled"),
    "bom": ("candidate", "active", "superseded", "rejected"),
    "document": ("active", "superseded", "withdrawn"),
    "material": ("active", "inactive", "deprecated"),
    "supplier": ("active", "inactive", "blocked"),
    "equipment": ("active", "inactive", "maintenance", "retired"),
    "process_route": ("candidate", "active", "superseded", "rejected"),
    "operation": ("active", "inactive", "deprecated"),
    "tooling": ("active", "inactive", "maintenance", "retired"),
}

DOC_ROLES = ("approval_specification", "sop", "engineering_drawing")
MAX_SOURCE_ROWS = 20_000
MAX_CANONICAL_RECORDS = 5_000
EXCERPT_LIMIT = 4000


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


# ---- 值助手（语义同旧 adapters）----

def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime := __import__("datetime").datetime, __import__("datetime").date)):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _status(v: Any, default: str) -> str:
    s = _text(v).casefold()
    return default if s == "" else s


def _list_value(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        parts = [str(x).strip() for x in v]
    else:
        text = _text(v)
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    parts = [str(x).strip() for x in parsed]
                else:
                    return []
            except ValueError:
                return []
        else:
            parts = text.split("|")
    out: list[str] = []
    for p in parts:
        if p and p not in out:
            out.append(p)
    return out


def _decimal(v: Any) -> Decimal | None:
    text = _text(v).replace(",", "")
    if text == "":
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _json_object(v: Any) -> tuple[dict[str, Any], bool]:
    """解析 JSON 对象；非法返回 ({}, False)（调用方决定是否发 INVALID_JSON_OBJECT）。"""
    if isinstance(v, dict):
        return dict(v), True
    text = _text(v)
    if text == "":
        return {}, True
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}, False
    if isinstance(parsed, dict):
        return dict(parsed), True
    return {}, False


# ---- 文件读取与表矩阵 ----

def _decode_sheets(raw: bytes, filename: str) -> tuple[list[tuple[str, list[list[Any]]]], str | None]:
    """字节 → [(sheet, matrix)]；失败返回 (None, issue_code)。"""
    lower = filename.lower()
    if lower.endswith(".csv"):
        for enc in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                text = None
        if text is None:
            return None, "UNSUPPORTED_CSV_ENCODING"
        reader = csv.reader(io.StringIO(text))
        matrix = [list(row) for row in reader]
        return [("CSV", matrix)], None
    if lower.endswith((".xlsx", ".xlsm")):
        try:
            from openpyxl import load_workbook
            from openpyxl.utils.exceptions import InvalidFileException
        except ImportError:
            return None, "INVALID_EXCEL_FILE"
        try:
            wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except (InvalidFileException, OSError, ValueError) as exc:
            return None, "INVALID_EXCEL_FILE"
        sheets = []
        try:
            for ws in wb.worksheets:
                rows = [[c for c in row] for row in ws.iter_rows(values_only=True)]
                sheets.append((ws.title, rows))
        finally:
            wb.close()
        return sheets, None
    if lower.endswith(".xls"):
        try:
            import xlrd
        except ImportError:
            return None, "INVALID_EXCEL_FILE"
        try:
            book = xlrd.open_workbook(file_contents=raw)
        except Exception:
            return None, "INVALID_EXCEL_FILE"
        sheets = []
        for sh in book.sheets():
            matrix = [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]
            sheets.append((sh.name, matrix))
        return sheets, None
    return None, "UNSUPPORTED_TABULAR_FORMAT"


@dataclass
class SheetRows:
    sheet: str
    header: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)  # {row_number, values{col: raw}}


def _sheet_rows(sheet: str, matrix: list[list[Any]],
                issues: list[dict[str, Any]]) -> SheetRows | None:
    """首个非空行=表头（casefold、去尾空列）；校验表头/行数。"""
    header_idx = None
    for i, row in enumerate(matrix):
        if any(_text(c) for c in row):
            header_idx = i
            break
    if header_idx is None:
        issues.append({"severity": "error", "code": "EMPTY_SHEET",
                       "message": f"sheet {sheet} 为空", "field": "file", "locator": {}})
        return None
    header = [_text(c).casefold() for c in matrix[header_idx]]
    while header and header[-1] == "":
        header.pop()
    if any(h == "" for h in header):
        issues.append({"severity": "error", "code": "INVALID_HEADER",
                       "message": f"sheet {sheet} 表头含空单元格", "field": "file", "locator": {}})
        return None
    if len(set(header)) != len(header):
        issues.append({"severity": "error", "code": "DUPLICATE_HEADER",
                       "message": f"sheet {sheet} 表头重复", "field": "file", "locator": {}})
        return None
    out = SheetRows(sheet=sheet, header=header)
    start = header_idx + 1
    data_rows = matrix[start:]
    if len(data_rows) > MAX_SOURCE_ROWS:
        issues.append({"severity": "error", "code": "SOURCE_ROW_LIMIT_EXCEEDED",
                       "message": f"sheet {sheet} 超过 {MAX_SOURCE_ROWS} 行", "field": "file",
                       "locator": {}})
        return None
    for idx, row in enumerate(data_rows, start=start + 1):
        values = {}
        for col_idx, col in enumerate(header):
            values[col] = row[col_idx] if col_idx < len(row) else ""
        if not any(_text(v) for v in values.values()):
            continue
        out.rows.append({"row_number": idx, "values": values})
    return out


# ---- 记录映射 ----

def _evidence_rows(filename: str, source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = []
    for i, srow in enumerate(source_rows, start=1):
        excerpt = canonical_json(srow["values"])
        if len(excerpt) > EXCERPT_LIMIT:
            excerpt = excerpt[:EXCERPT_LIMIT]
        evidence.append({
            "key": f"source-row-{i:04d}",
            "locator": {"file": filename, "sheet": srow["sheet"], "row": srow["row_number"]},
            "excerpt": excerpt,
        })
    return evidence


def _idempotency_key(template_version: str, source_system: str, source_external_id: str,
                     entity_type: str, business_key: str, version_id: str) -> str:
    raw = ":".join((template_version, source_system, source_external_id,
                    entity_type, business_key, version_id))
    if len(raw) <= 256:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"{template_version}:{entity_type}:{digest}"


def _row_issue(code: str, message: str, field: str, locator: dict[str, Any]) -> dict[str, Any]:
    return {"severity": "error", "code": code, "message": message, "field": field,
            "locator": locator}


def adapt_tabular_file(*, filename: str, data: bytes, template_version: str,
                       tenant_id: str, source_system: str, source_external_id: str,
                       review_status: str, reviewed_by: str = "") -> dict[str, Any]:
    """版本化文件 → m0.ingest.v1 记录集（adaptation 报告，无写）。"""
    issues: list[dict[str, Any]] = []
    spec = TEMPLATES.get(template_version)
    if spec is None:
        issues.append(_row_issue("UNSUPPORTED_TEMPLATE_VERSION",
                                 f"不支持的 template_version: {template_version}",
                                 "template_version", {}))
        return _adaptation_report(template_version, spec, issues, [])
    if not tenant_id.strip():
        issues.append(_row_issue("TENANT_REQUIRED", "tenant_id 必填", "tenant_id", {}))
    if not source_system.strip():
        issues.append(_row_issue("SOURCE_SYSTEM_REQUIRED", "source_system 必填", "source_system", {}))
    if not source_external_id.strip():
        issues.append(_row_issue("SOURCE_EXTERNAL_ID_REQUIRED",
                                 "source_external_id 必填（不从文件名推断）", "source_external_id", {}))
    if review_status not in ("candidate", "approved", "rejected"):
        issues.append(_row_issue("INVALID_REVIEW_STATUS",
                                 f"review_status 必须为 candidate|approved|rejected",
                                 "review_status", {}))
    if review_status == "approved" and not reviewed_by.strip():
        issues.append(_row_issue("REVIEWER_REQUIRED", "approved 需要审核人", "reviewed_by", {}))
    if not data:
        issues.append(_row_issue("EMPTY_FILE", "文件字节为空", "file", {}))
    if issues:
        return _adaptation_report(template_version, spec, issues, [])

    sha256 = hashlib.sha256(data).hexdigest()
    sheets, decode_error = _decode_sheets(data, filename)
    if decode_error:
        issues.append(_row_issue(decode_error, f"文件无法解析: {filename}", "file", {}))
        return _adaptation_report(template_version, spec, issues, [])

    records: list[dict[str, Any]] = []
    group_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for sheet, matrix in sheets:
        srows = _sheet_rows(sheet, matrix, issues)
        if srows is None:
            continue
        missing = [c for c in spec.required if c not in srows.header]
        unknown = [c for c in srows.header if c not in spec.allowed]
        if missing or unknown:
            if missing:
                issues.append(_row_issue("MISSING_TEMPLATE_COLUMNS",
                                         f"缺列: {missing}", "file",
                                         {"sheet": sheet}))
            if unknown:
                issues.append(_row_issue("UNEXPECTED_TEMPLATE_COLUMNS",
                                         f"多余列: {unknown}", "file",
                                         {"sheet": sheet}))
            continue
        for row in srows.rows:
            values = row["values"]
            empties = [c for c in spec.required if _text(values.get(c)) == ""]
            if empties:
                issues.append(_row_issue("MISSING_REQUIRED_VALUE",
                                         f"必填列空值: {empties}", "file",
                                         {"sheet": sheet, "row": row["row_number"]}))
                continue
            row["sheet"] = sheet
            if spec.group_by:
                gkey = tuple(_text(values.get(c)) for c in spec.group_by)
                group_rows.setdefault(gkey, []).append(row)
            else:
                _map_single_row(spec, row, filename, source_system, source_external_id,
                                sha256, tenant_id, review_status, reviewed_by,
                                issues, records)

    # 组记录（跨 sheet 按组键合并，保首见序）
    if spec.group_by:
        for gkey, rows in group_rows.items():
            _map_group_record(spec, gkey, rows, filename, source_system, source_external_id,
                              sha256, tenant_id, review_status, reviewed_by, issues, records)

    if not records:
        issues.append(_row_issue("NO_CANONICAL_RECORDS", "没有产出任何 canonical 记录",
                                 "file", {}))
    return _adaptation_report(template_version, spec, issues, records[:MAX_CANONICAL_RECORDS])


def _map_single_row(spec: TemplateSpec, row: dict[str, Any], filename: str,
                    source_system: str, source_external_id: str, sha256: str,
                    tenant_id: str, review_status: str, reviewed_by: str,
                    issues: list[dict[str, Any]], records: list[dict[str, Any]]) -> None:
    values = row["values"]
    etype = spec.entity_type
    locator = {"sheet": row["sheet"], "row": row["row_number"]}
    single_key = {
        "product": "product_code", "product_family": "family_code",
        "material": "material_code", "supplier": "supplier_code",
        "equipment": "equipment_code", "operation": "operation_code",
        "tooling": "tooling_code", "document": "document_no",
    }
    if etype in single_key:
        bkey = _text(values.get(single_key[etype]))
        payload: dict[str, Any] = {"name": _text(values.get("name"))}
        # 单值可选文本（按模板 allowed 精准映射）
        single_text = {
            "product": ("model",), "product_family": ("description",),
            "material": ("unit",), "supplier": ("legal_id",),
            "equipment": ("equipment_type", "line"), "tooling": ("tooling_type",),
            "operation": ("standard_time",), "document": ("content_uri",),
        }.get(etype, ())
        for f in single_text:
            payload[f] = _text(values.get(f))
        payload["status"] = _status(values.get("status"), "active")
        status_literals = STATUS_LITERALS.get(etype, ())
        if status_literals and payload["status"] not in status_literals:
            issues.append(_row_issue("CANONICAL_CONTRACT_ERROR",
                                     f"status 非法: {payload['status']}", "payload.status", locator))
            return
        if etype == "document":
            role = _status(values.get("role"), "")
            if role not in DOC_ROLES:
                issues.append(_row_issue("CANONICAL_CONTRACT_ERROR",
                                         f"role 非法: {role!r}", "payload.role", locator))
                return
            payload["role"] = role
            payload["title"] = _text(values.get("title"))
            payload["product_codes"] = _list_value(values.get("product_codes"))
            if not payload["product_codes"]:
                issues.append(_row_issue("MISSING_PRODUCT_CODE",
                                         "product_codes 至少 1 个（不猜产品）", "payload.product_codes", locator))
                return
            if not _text(values.get("revision")):
                issues.append(_row_issue("VERSION_REQUIRED", "document revision 必填",
                                         "identity.version_id", locator))
                return
        json_cols = ("attributes_json",)
        for col in json_cols:
            if col in values and _text(values.get(col)) not in ("", "{}"):
                obj, ok = _json_object(values.get(col))
                if not ok:
                    issues.append(_row_issue("INVALID_JSON_OBJECT", f"{col} 必须是 JSON 对象",
                                             f"payload.{col[:-5]}", locator))
                    return
                payload[col[:-5]] = obj
        for col in ("aliases", "product_codes", "material_codes"):
            if col in spec.allowed:
                val = _list_value(values.get(col))
                if val:
                    payload[col] = val
        if etype in ("material", "supplier") and "spec_json" in spec.allowed:
            obj, ok = _json_object(values.get("spec_json"))
            if not ok:
                issues.append(_row_issue("INVALID_JSON_OBJECT", "spec_json 必须是 JSON 对象",
                                         "payload.spec", locator))
                return
            if obj:
                payload["spec"] = obj
        relations = []
        if etype == "product":
            family = _text(values.get("family_code"))
            if family:
                relations.append({"relation_type": "member_of_family",
                                  "target_type": "product_family",
                                  "target": {"business_key": family}})
            for code in _list_value(values.get("related_product_codes")):
                relations.append({"relation_type": "related_product",
                                  "target_type": "product",
                                  "target": {"business_key": code}})
        version_id = _text(values.get("revision")) if etype == "document" else ""
        evidence = _evidence_rows(filename, [row])
        env = _base_record(spec, bkey, version_id, payload, relations, evidence,
                           filename, source_system, source_external_id, sha256,
                           tenant_id, review_status, reviewed_by)
        records.append(env)
        return
    # 其余单行模板同上分支已覆盖全部 8 种单行类型；防御性落错
    raise AssertionError(f"unhandled single-row template: {etype}")


def _map_group_record(spec: TemplateSpec, gkey: tuple[str, ...], rows: list[dict[str, Any]],
                      filename: str, source_system: str, source_external_id: str, sha256: str,
                      tenant_id: str, review_status: str, reviewed_by: str,
                      issues: list[dict[str, Any]], records: list[dict[str, Any]]) -> None:
    etype = spec.entity_type
    bkey = gkey[0]
    version_id = gkey[1] if len(gkey) > 1 else ""
    head_fields = ("product_code", "status", "attributes_json") if etype in ("bom", "process_route") \
        else ("customer", "order_date", "due_date", "status", "attributes_json")

    def consistent(field: str, locator_base: dict[str, Any]) -> str:
        vals: list[str] = []
        for row in rows:
            v = _text(row["values"].get(field))
            if v and v not in vals:
                vals.append(v)
        if len(vals) > 1:
            issues.append(_row_issue("INCONSISTENT_GROUP_VALUE",
                                     f"{field} must be identical on every row of one business record",
                                     f"payload.{field}", locator_base))
        return vals[0] if vals else ""

    payload: dict[str, Any] = {}
    payload["status"] = consistent("status", {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]})
    if etype == "order":
        lines: list[dict[str, Any]] = []
        seen_lines: set[str] = set()
        for row in rows:
            values = row["values"]
            loc = {"sheet": row["sheet"], "row": row["row_number"]}
            line_no = _text(values.get("line_no"))
            if line_no in seen_lines:
                issues.append(_row_issue("DUPLICATE_ORDER_LINE", f"重复 line_no: {line_no}",
                                         "payload.lines", loc))
                continue
            seen_lines.add(line_no)
            qty_raw = _text(values.get("quantity"))
            qty = _decimal(qty_raw)
            if qty is None or qty <= 0:
                issues.append(_row_issue("CANONICAL_CONTRACT_ERROR",
                                         f"quantity 非法: {qty_raw!r}", "payload.lines[].quantity", loc))
                continue
            line: dict[str, Any] = {
                "line_no": line_no,
                "product_code": _text(values.get("product_code")),
                "quantity": str(qty),
                "uom": _text(values.get("uom")),
            }
            if "requested_spec_json" in spec.allowed:
                obj, ok = _json_object(values.get("requested_spec_json"))
                if not ok:
                    issues.append(_row_issue("INVALID_JSON_OBJECT", "requested_spec_json 必须为 JSON 对象",
                                             "payload.lines[].requested_spec", loc))
                    continue
                if obj:
                    line["requested_spec"] = obj
            lines.append(line)
        if not lines:
            issues.append(_row_issue("NO_CANONICAL_RECORDS", f"order {bkey} 无有效行", "payload.lines",
                                     {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]}))
            return
        payload["lines"] = lines
        payload["customer"] = consistent("customer", {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]})
        for f in ("order_date", "due_date"):
            v = consistent(f, {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]})
            if v:
                payload[f] = v
    elif etype == "bom":
        payload["product_code"] = consistent("product_code",
                                             {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]})
        lines = []
        seen_lines = set()
        for row in rows:
            values = row["values"]
            loc = {"sheet": row["sheet"], "row": row["row_number"]}
            line_no = _text(values.get("line_no"))
            if line_no in seen_lines:
                issues.append(_row_issue("DUPLICATE_BOM_LINE", f"重复 line_no: {line_no}",
                                         "payload.lines", loc))
                continue
            seen_lines.add(line_no)
            qty_raw = _text(values.get("quantity"))
            qty = _decimal(qty_raw)
            if qty is None or qty <= 0:
                issues.append(_row_issue("CANONICAL_CONTRACT_ERROR",
                                         f"quantity 非法: {qty_raw!r}", "payload.lines[].quantity", loc))
                continue
            loss_raw = _text(values.get("loss_rate"))
            loss = "0"
            if loss_raw:
                loss_dec = _decimal(loss_raw)
                if loss_dec is None or loss_dec < 0:
                    issues.append(_row_issue("CANONICAL_CONTRACT_ERROR",
                                             f"loss_rate 非法: {loss_raw!r}", "payload.lines[].loss_rate", loc))
                    continue
                loss = str(loss_dec)
            line: dict[str, Any] = {
                "line_no": line_no,
                "material_code": _text(values.get("material_code")),
                "quantity": str(qty),
                "uom": _text(values.get("uom")),
            }
            name = _text(values.get("material_name"))
            if name:
                line["material_name"] = name
            if loss != "0" or loss_raw:
                line["loss_rate"] = loss
            if "line_attributes_json" in spec.allowed:
                obj, ok = _json_object(values.get("line_attributes_json"))
                if not ok:
                    issues.append(_row_issue("INVALID_JSON_OBJECT", "line_attributes_json 必须为 JSON 对象",
                                             "payload.lines[].attributes", loc))
                    continue
                if obj:
                    line["attributes"] = obj
            lines.append(line)
        if not lines:
            issues.append(_row_issue("NO_CANONICAL_RECORDS", f"bom {bkey} 无有效行", "payload.lines",
                                     {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]}))
            return
        payload["lines"] = lines
    else:  # process_route
        payload["product_code"] = consistent("product_code",
                                             {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]})
        operations = []
        for row in rows:
            values = row["values"]
            loc = {"sheet": row["sheet"], "row": row["row_number"]}
            seq_raw = _text(values.get("sequence_no"))
            try:
                seq = int(seq_raw.replace(",", ""))
            except ValueError:
                seq = 0
            if seq < 1:
                issues.append(_row_issue("INVALID_SEQUENCE_NO", f"sequence_no 非法: {seq_raw!r}",
                                         "payload.operations[].sequence_no", loc))
                continue
            op: dict[str, Any] = {
                "sequence_no": seq,
                "operation_code": _text(values.get("operation_code")),
            }
            for f in ("operation_name", "standard_time", "station_code"):
                v = _text(values.get(f))
                if v:
                    op[f] = v
            for col in ("material_codes", "equipment_codes", "tooling_codes"):
                codes = _list_value(values.get(col))
                if codes:
                    op[col] = codes
            if "row_attributes_json" in spec.allowed:
                obj, ok = _json_object(values.get("row_attributes_json"))
                if not ok:
                    issues.append(_row_issue("INVALID_JSON_OBJECT", "row_attributes_json 必须为 JSON 对象",
                                             "payload.operations[].attributes", loc))
                    continue
                if obj:
                    op["attributes"] = obj
            operations.append(op)
        if not operations:
            issues.append(_row_issue("NO_CANONICAL_RECORDS", f"route {bkey} 无有效工序行",
                                     "payload.operations",
                                     {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]}))
            return
        payload["operations"] = operations
    attrs, ok = _json_object(rows[0]["values"].get("attributes_json"))
    if not ok:
        issues.append(_row_issue("INVALID_JSON_OBJECT", "attributes_json 必须为 JSON 对象",
                                 "payload.attributes",
                                 {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]}))
    elif attrs:
        payload["attributes"] = attrs
    if version_id == "" and spec.entity_type in ("bom", "process_route"):
        issues.append(_row_issue("VERSION_REQUIRED", f"{spec.entity_type} revision 必填",
                                 "identity.version_id",
                                 {"sheet": rows[0]["sheet"], "row": rows[0]["row_number"]}))
        return
    evidence = _evidence_rows(filename, rows)
    env = _base_record(spec, bkey, version_id, payload, [], evidence,
                       filename, source_system, source_external_id, sha256,
                       tenant_id, review_status, reviewed_by)
    records.append(env)


def _base_record(spec: TemplateSpec, business_key: str, version_id: str,
                 payload: dict[str, Any], relations: list[dict[str, Any]],
                 evidence: list[dict[str, Any]], filename: str,
                 source_system: str, source_external_id: str, sha256: str,
                 tenant_id: str, review_status: str, reviewed_by: str) -> dict[str, Any]:
    idem = _idempotency_key(spec.version, source_system.strip(), source_external_id.strip(),
                            spec.entity_type, business_key, version_id)
    env: dict[str, Any] = {
        "schema_version": "m0.ingest.v1",
        "tenant_id": tenant_id.strip(),
        "idempotency_key": idem,
        "source": {"system": source_system.strip(), "external_id": source_external_id.strip(),
                   "sha256": sha256},
        "entity_type": spec.entity_type,
        "identity": {"business_key": business_key, "version_id": version_id},
        "payload": payload,
        "evidence": evidence,
        "review_status": review_status,
    }
    if relations:
        env["relations"] = relations
    if review_status in ("approved", "rejected"):
        env["reviewed_by"] = reviewed_by
    return env


def _adaptation_report(template_version: str, spec: TemplateSpec | None,
                       issues: list[dict[str, Any]],
                       records: list[dict[str, Any]]) -> dict[str, Any]:
    entity_type = spec.entity_type if spec else ""
    summary = {"records": len(records), "errors": len(issues)}
    return {
        "schema_version": "m0.ingest.adaptation.v1",
        "adapter": "tabular-file",
        "input_schema_version": template_version,
        "canonical_schema_version": "m0.ingest.v1",
        "valid": not issues and bool(records),
        "summary": summary,
        "issues": issues,
        "records": records,
        "entity_type": entity_type,
    }

