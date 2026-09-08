from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "yunpai.business-catalog.v2"
IGNORED_NAMES = {".DS_Store"}
IGNORED_PREFIXES = ("._", "~$")
SUPPORTED_EXTENSIONS = {
    ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".json", ".pdf", ".docx",
    ".txt", ".md", ".dwg", ".et", ".zip", ".rar", ".7z", ".py", ".ps1",
    ".png", ".jpg", ".jpeg", ".doc", ".pptx",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_prefix(path: Path, limit: int = 65536) -> bytes:
    with path.open("rb") as stream:
        return stream.read(limit)


def _text_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (dict, list)):
        return "object" if isinstance(value, dict) else "array"
    return "string"


def classify_path(path: Path) -> tuple[str, str, float]:
    text = str(path).lower()
    name = path.name.lower()
    if any(token in text for token in ("订单", "备货", "定制单", "cg20", "po-20", "po_20")):
        return "order", "customer_or_stocking_order", 0.90
    if any(token in text for token in ("sop", "工艺", "作业指导")):
        return "sop", "production_sop", 0.93
    if any(token in text for token in ("bom", "物料清单")):
        return "bom", "engineering_bom", 0.93
    if any(token in text for token in ("承认书", "规格书", "datasheet", "数据手册")):
        return "engineering_document", "supplier_approval_or_spec", 0.86
    # 采购记录路径提示：中文词面匹配；英文 "po" 只作为独立词/前缀（如 po-2026、
    # po_20、路径段 po/…）命中，避免 "positive"/"component" 等单词内子串误判。
    if any(token in text for token in ("采购", "采购单", "采购订单", "purchase")) or re.search(r"(?<![a-z0-9])po(?![a-z0-9])", text):
        return "procurement", "purchase_order_or_record", 0.82
    if any(token in text for token in ("库存", "入库", "出库", "盘点")):
        return "inventory", "inventory_record", 0.82
    if path.suffix.lower() in {".dwg"} or any(token in text for token in ("工程图", "图纸", "cad")):
        return "engineering_drawing", "cad_or_drawing", 0.88
    if path.suffix.lower() in {".xlsx", ".xls", ".csv", ".tsv"}:
        return "tabular", "unclassified_table", 0.45
    if path.suffix.lower() in {".pdf", ".docx", ".txt", ".md"}:
        return "document", "unclassified_document", 0.40
    if path.suffix.lower() in {".zip", ".rar", ".7z"}:
        return "archive", "compressed_archive", 0.80
    return "other", "unclassified", 0.20


# 制造资料分类最低识别字段（任务书 §3.3）：无法满足时进入 needs_review。
REQUIRED_FIELDS_BY_KIND: dict[str, tuple[str, ...]] = {
    "order": ("order_id", "quantity", "due_date"),
    "product": ("product_code", "product_name"),
    "bom": ("material_code", "material_name", "quantity"),
    "route": ("operation_code", "operation_name", "sequence", "standard_minutes"),
    "sop": ("station", "step_name", "worker_count"),
    "equipment": ("equipment_code", "equipment_name"),
    "station": ("station_code", "operation_code"),
    "worker": ("worker_code", "worker_name"),
    "inventory": ("material_code", "warehouse", "available_qty"),
    "supplier": ("supplier_code", "supplier_name"),
    "finance_cost": ("cost_item", "period", "unit_cost"),
    "calendar": ("calendar_date", "shift", "start_time", "end_time"),
    "tooling": ("tooling_code", "tooling_name"),
    "procurement": ("supplier_code", "material_code", "quantity"),
}

# 通用表头别名（非 order/bom 类别字段观察用）：语义字段 -> 可能的中文表头。
_GENERIC_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "equipment_code": ("设备编码", "设备编号", "编号"),
    "equipment_name": ("设备名称", "设备名"),
    "product_code": ("产品编码", "产品编号", "型号", "编码"),
    "product_name": ("产品名称", "物料名称", "品名", "名称"),
    "worker_code": ("工号", "人员编码", "员工编号", "工号/姓名"),
    "worker_name": ("姓名", "员工姓名", "名字"),
    "skill": ("技能", "技能码", "技能名称"),
    "material_code": ("物料编码", "料号", "材料编码", "编码"),
    "material_name": ("材料名称", "物料名称", "品名", "名称"),
    "warehouse": ("仓库", "仓库名称"),
    "lot_no": ("批次", "批号", "批次号"),
    "available_qty": ("现存数量", "可用数量", "数量", "库存量"),
    "supplier_code": ("供应商编码", "供应商编号"),
    "supplier_name": ("供应商名称", "供应商"),
    "operation_code": ("工序编码", "工序编号", "工序号"),
    "operation_name": ("工序名称", "工序"),
    "sequence": ("顺序", "序号", "工序顺序"),
    "standard_minutes": ("标准工时", "标准时间", "工时", "IE秒数", "节拍"),
    "station_code": ("工位编码", "工位编号", "工位号"),
    "station_name": ("工位名称", "工位"),
    # SOP（工站/作业步骤/投入人数）
    "station": ("工站", "工作站", "作业工站"),
    "step_name": ("作业步骤", "操作步骤", "步骤名称", "作业内容", "步骤"),
    "worker_count": ("投入人数", "作业人数", "人员数量", "人数"),
    "calendar_date": ("日期", "工作日", "生产日期"),
    "shift": ("班次", "班组"),
    "start_time": ("开始时间", "上班时间", "开工时间"),
    "end_time": ("结束时间", "下班时间", "完工时间"),
    "quantity": ("数量", "采购数量", "订货数量", "需求量"),
    "tooling_code": ("模具编码", "模具编号", "工装编码", "模编号"),
    "tooling_name": ("模具名称", "工装名称", "模具"),
    "cost_item": ("成本项目", "费用项目", "成本科目"),
    "period": ("期间", "月份", "会计期间"),
    "currency": ("币种", "货币"),
    "unit_cost": ("单位成本", "成本单价"),
}


def _norm_header(value: Any) -> str:
    return str(value or "").replace("\n", " ").replace(" ", "").strip().lower()


def _generic_field_from_header(value: Any) -> str | None:
    text = _norm_header(value)
    if not text:
        return None
    # 先精确匹配；再用足够长的别名做子串匹配，避免泛化词（名称/编码）误抢专属字段。
    for field, aliases in _GENERIC_FIELD_ALIASES.items():
        if any(_norm_header(alias) == text for alias in aliases):
            return field
    for field, aliases in _GENERIC_FIELD_ALIASES.items():
        if any(len(alias) >= 3 and _norm_header(alias) in text for alias in aliases):
            return field
    return None


def _generic_observations_from_sheets(extraction: dict[str, Any], kind: str, parser_version: str = "generic.table.v1") -> list[dict[str, Any]]:
    """从 sample_rows 中找表头行并逐行产出字段观察（raw=normalized）。

    仅对 sample_rows 覆盖的前几行做轻量观察，供候选审核与字段指标测试；
    深层解析仍由 M1/专用 parser 负责。
    """
    observations: list[dict[str, Any]] = []
    required = set(REQUIRED_FIELDS_BY_KIND.get(kind, ()))
    if not required:
        return observations
    for sheet in extraction.get("sheets", []):
        rows = sheet.get("sample_rows") or []
        for header_index, header_row in enumerate(rows):
            mapping: dict[str, int] = {}
            for col_index, cell in enumerate(header_row):
                field = _generic_field_from_header(cell)
                if field and field not in mapping and (field in required or field in {"skill", "lot_no", "operation_code"}):
                    mapping[field] = col_index
            if len(mapping) < 1:
                continue
            for row_index in range(header_index + 1, min(header_index + 5, len(rows))):
                row_values = rows[row_index]
                for field, col in mapping.items():
                    if col >= len(row_values) or row_values[col] in (None, ""):
                        continue
                    raw = row_values[col]
                    observations.append({
                        "field_path": f"$.sheets[{sheet.get('name', '')!r}].rows[{row_index + 1}].{field}",
                        "sheet": sheet.get("name", ""),
                        "row": row_index + 1,
                        "column": col + 1,
                        "raw_value": raw,
                        "normalized_value": raw,
                        "physical_type": _text_type(raw),
                        "semantic_type": field,
                        "parser_version": parser_version,
                    })
            break
    return observations

# 表头/内容样本关键词 -> 分类（用于低置信路径分类的内容修正）。
_CONTENT_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...], float], ...] = (
    ("order", ("订单号", "序号", "型号", "交期", "单价", "采购数量", "金额", "po号", "order"), 0.92),
    ("product", ("产品编码", "产品名称", "型号", "规格", "版本号"), 0.80),
    ("bom", ("物料编码", "料号", "材料名称", "用量", "bom版本", "物料清单"), 0.93),
    ("route", ("工序编码", "工序名称", "标准工时", "前置工序", "作业顺序"), 0.92),
    ("sop", ("工站", "作业步骤", "作业指导", "投入人数", "质量要求"), 0.90),
    ("equipment", ("设备编码", "设备名称", "设备台账", "产线", "保养周期"), 0.92),
    ("tooling", ("模具编码", "模具名称", "工装编码", "模治具", "tooling", "模具清单"), 0.90),
    ("station", ("工位编码", "工位名称", "生产单元", "绑定工位"), 0.90),
    ("worker", ("工号", "姓名", "技能", "资格", "班次", "员工"), 0.85),
    ("inventory", ("物料编码", "仓库", "库位", "批次", "现存数量", "库存"), 0.90),
    # procurement 规则置于 supplier 之前：表头同时含供应商/PO 编号时，若再有
    # 物料/数量/交期等采购行特征，优先判定为采购记录而非供应商主数据。
    ("procurement", ("供应商编码", "po编号", "采购订单", "采购单", "订购数量", "采购数量", "交期", "需求日期", "到货日期", "物料", "数量"), 0.90),
    ("supplier", ("供应商编码", "供应商名称", "采购订单", "po编号"), 0.90),
    ("finance_cost", ("成本项目", "期间", "币种", "单位成本", "含税", "费用"), 0.82),
    ("calendar", ("日期", "班次", "开始时间", "结束时间", "假期"), 0.80),
)


def _sample_tokens_for(path: Path) -> list[str]:
    """读取少量表头/内容样本（大文件截断），返回归一化 token。"""
    suffix = path.suffix.lower()
    try:
        if suffix in {".xlsx", ".xlsm"}:
            from openpyxl import load_workbook

            workbook = load_workbook(path, read_only=True, data_only=True)
            try:
                tokens: list[str] = []
                for sheet in workbook.worksheets[:3]:
                    for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row or 0, 6), values_only=True):
                        for value in row[:30]:
                            if value not in (None, ""):
                                tokens.append(str(value).strip().replace(" ", "")[:30])
                return [token for token in tokens if token]
            finally:
                workbook.close()
        if suffix in {".csv", ".tsv"}:
            import csv

            try:
                with path.open(encoding="utf-8-sig", errors="replace") as stream:
                    rows = list(csv.reader(stream))[:6]
                return [str(value).strip().replace(" ", "") for row in rows for value in row if value not in (None, "")]
            except OSError:
                return []
    except Exception:
        return []
    return []


def classify_with_content(path: Path, *, current_kind: str, current_confidence: float) -> dict[str, Any]:
    """文件名/路径 + 表头/内容样本 的多信号分类器。

    返回 {"kind", "subtype", "confidence", "rules": [{"rule", "score", "evidence"}],
          "content_signals": int}。低置信路径分类（tabular/document/other）在内容
    命中足够关键词时被修正；高置信路径分类（>=0.8）优先保留。
    """
    base = {"kind": current_kind, "subtype": _subtype_for(current_kind), "confidence": current_confidence, "rules": [{"rule": "path", "score": current_confidence}], "content_signals": 0}
    if current_confidence >= 0.8:
        return base
    tokens = _sample_tokens_for(path)
    if not tokens:
        return base
    token_set = "".join(tokens)
    best_kind, best_hits, best_conf = None, 0, 0.0
    for kind, keywords, conf in _CONTENT_KEYWORD_RULES:
        hits = sum(1 for keyword in keywords if keyword.lower() in token_set.lower() or any(keyword in token for token in tokens))
        if hits > best_hits:
            best_kind, best_hits, best_conf = kind, hits, conf
    if best_kind and best_hits >= 2:
        return {
            "kind": best_kind, "subtype": _subtype_for(best_kind),
            "confidence": max(current_confidence, best_conf - 0.08 * (3 - min(best_hits, 3))),
            "rules": [{"rule": "path", "score": current_confidence}, {"rule": f"content:{best_kind}", "score": best_conf, "evidence": f"{best_hits} 个表头关键词命中"}],
            "content_signals": best_hits,
        }
    return base


_SUBTYPE_BY_KIND = {
    "order": "customer_or_stocking_order", "product": "product_master", "bom": "engineering_bom",
    "route": "production_route", "sop": "production_sop", "equipment": "equipment_master",
    "tooling": "tooling_master",
    "station": "station_master", "worker": "worker_master", "calendar": "production_calendar",
    "inventory": "inventory_record", "supplier": "supplier_master", "finance_cost": "cost_record",
    "tabular": "unclassified_table", "document": "unclassified_document", "archive": "compressed_archive",
    "other": "unclassified", "engineering_document": "supplier_approval_or_spec", "engineering_drawing": "cad_or_drawing",
    "procurement": "purchase_order_or_record",
}


def _subtype_for(kind: str) -> str:
    return _SUBTYPE_BY_KIND.get(kind, "unclassified")


# 敏感数据分类（任务书 §3.4：普通/内部/HR/财务）。基于路径与文件名，不做内容推测。
_SENSITIVITY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hr", ("工资", "薪资", "社保", "公积金", "身份证", "银行卡", "人员档案", "入职", "离职", "考勤")),
    ("financial", ("财务", "成本", "报价", "对账单", "发票", "含税", "税额", "银行流水", "审计")),
    ("hr_financial", ("工资表", "个税", "奖金", "报销")),
)


def sensitivity_of(extracted: dict[str, Any], kind: str) -> str:
    """按路径/文件名关键词给敏感度分类（普通/内部/HR/财务）；关键词保留可版本化。"""
    if kind == "worker":
        return "hr"
    if kind == "finance_cost":
        return "financial"
    text = str(extracted.get("relative_path", "")).lower() + " " + str(extracted.get("extraction", {}).get("sniffed_mime", "") if isinstance(extracted.get("extraction"), dict) else "").lower()
    score: dict[str, int] = {}
    for sensitivity, keywords in _SENSITIVITY_KEYWORDS:
        score[sensitivity] = sum(1 for keyword in keywords if keyword in text)
    if score.get("hr", 0):
        return "hr"
    if score.get("hr_financial", 0) and not score.get("financial", 0):
        return "hr"
    if score.get("financial", 0):
        return "financial"
    return "internal"


def _extract_xlsx(path: Path, kind: str) -> dict[str, Any]:
    from openpyxl import load_workbook

    result: dict[str, Any] = {"sheets": [], "active_sheet": None}
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            nonempty = []
            for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row or 0, 30), values_only=True):
                values = [str(value).strip()[:120] if value is not None else None for value in row[:40]]
                if any(value not in (None, "") for value in values):
                    nonempty.append(values)
            result["sheets"].append({
                "name": sheet.title,
                "max_row": sheet.max_row,
                "max_column": sheet.max_column,
                "sample_rows": nonempty[:8],
            })
        result["active_sheet"] = workbook.active.title if workbook.active else None
        if kind == "order":
            # 与 graph fixture / M1 HTTP 补充共用 order_semantics：表头驱动解析
            # 优先、老式坐标模板兜底，任何字段缺口进入 needs_review，不用写死的
            # 单元格坐标把真实订单解析成 0 行。
            try:
                from .order_semantics import parse_order_document

                parsed = parse_order_document(path.name, path.read_bytes())
                order_document = parsed.get("document") or {}
                result["order_document"] = order_document
                result["parser_version"] = parsed.get("parser_version") or order_document.get("parser_version") or "order.parser.v2"
                result["order_parser_revision"] = parsed.get("parser_version")
            except Exception as exc:
                result["order_parse_error"] = str(exc)
    finally:
        workbook.close()
    return result


_BOM_HEADER_ALIASES = {
    "material_code": ("物料编码", "料号", "物料编号", "材料编码"),
    "material_name": ("材料名称", "原材料名称", "包材名称", "物料名称", "线材名称", "品名"),
    "specification": ("规格", "规格型号"),
    "quantity": ("用量", "数量", "用量/装箱数量"),
    "unit": ("单位",),
    "unit_price": ("单价", "含税单价", "不含税单价"),
    "cost": ("成本", "成本价格", "成本总价"),
    "supplier": ("供应商",),
}


def _looks_like_material_code(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return bool(re.match(r"^(?:YA(?:\.[A-Z0-9]+)+|XC\d{3,}|[A-Z]{1,5}[._-][A-Z0-9._-]{2,})$", text, re.I))


def _header_key(value: Any) -> str | None:
    text = str(value or "").replace("\n", "").replace(" ", "").strip()
    if not text:
        return None
    for key, aliases in _BOM_HEADER_ALIASES.items():
        if any(alias.replace(" ", "") in text for alias in aliases):
            return key
    return None


def _extract_bom_xlsx(path: Path) -> dict[str, Any]:
    """Extract every populated BOM row from every sheet, retaining its locator."""
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    sheets: list[dict[str, Any]] = []
    bom_lines: list[dict[str, Any]] = []
    try:
        for sheet in workbook.worksheets:
            rows: list[tuple[int, list[Any]]] = []
            max_nonempty_column = 0
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                values = [value for value in row]
                nonempty_columns = [index for index, value in enumerate(values, start=1) if value not in (None, "")]
                if not nonempty_columns:
                    continue
                max_nonempty_column = max(max_nonempty_column, max(nonempty_columns))
                rows.append((row_number, values))
            rows = [(number, values[:max_nonempty_column]) for number, values in rows]
            header_maps: list[tuple[int, dict[str, int]]] = []
            for number, values in rows[:20]:
                mapping = {key: index for index, value in enumerate(values) if (key := _header_key(value))}
                if "material_code" in mapping and ("material_name" in mapping or "quantity" in mapping):
                    header_maps.append((number, mapping))
            lines: list[dict[str, Any]] = []
            # header_maps 按行号升序；一个 sheet 里可能有多个数据块（材料明细 vs
            # 耗材包材），每行应归入「最近的一个表头块」，而不是第一个能匹配到
            # 物料编码的表头块——否则包材行的「规格」会被误当成「用量」。
            for row_number, values in rows:
                mapping: dict[str, int] | None = None
                for header_row, candidate in header_maps:
                    if header_row >= row_number:
                        break
                    mapping = candidate
                if mapping is None:
                    continue
                code_index = mapping.get("material_code")
                code = values[code_index] if code_index is not None and code_index < len(values) else None
                if not _looks_like_material_code(code):
                    continue
                line = {
                    "sheet_name": sheet.title,
                    "row_number": row_number,
                    "material_code": str(code).strip(),
                    "raw_cells": {str(index + 1): value for index, value in enumerate(values) if value not in (None, "")},
                }
                for key, index in mapping.items():
                    if index < len(values) and values[index] not in (None, ""):
                        line[key] = values[index]
                lines.append(line)
            sheets.append({
                "name": sheet.title,
                "max_row": sheet.max_row,
                "max_column": sheet.max_column,
                "actual_max_column": max_nonempty_column,
                "nonempty_row_count": len(rows),
                "header_rows": [number for number, _ in header_maps],
                "bom_line_count": len(lines),
                "rows": [{"row_number": number, "values": values} for number, values in rows],
            })
            bom_lines.extend(lines)
    finally:
        workbook.close()
    return {"sheet_count": len(sheets), "sheets": sheets, "bom_lines": bom_lines, "bom_line_count": len(bom_lines)}


def _extract_delimited(path: Path) -> dict[str, Any]:
    encoding = "utf-8-sig"
    try:
        text = path.read_text(encoding=encoding, errors="replace")
    except OSError:
        return {"read_error": "unreadable"}
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = "\t" if "\t" in sample else ","
    rows = list(csv.reader(text.splitlines()[:101], delimiter=delimiter))
    return {
        "delimiter": delimiter,
        "row_count_estimate": max(0, text.count("\n")),
        "headers": rows[0][:80] if rows else [],
        "sample_rows": [row[:80] for row in rows[1:6]],
    }


def _extract_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"parse_error": str(exc)}
    if isinstance(value, dict):
        return {"top_level_type": "object", "top_level_keys": list(value)[:100], "record_count": len(value.get("records", [])) if isinstance(value.get("records"), list) else None}
    if isinstance(value, list):
        return {"top_level_type": "array", "record_count": len(value), "first_record_keys": list(value[0])[:100] if value and isinstance(value[0], dict) else []}
    return {"top_level_type": _text_type(value)}


def _extract_text(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"text_chars": len(text), "text_preview": text[:2000]}
    if suffix == ".docx":
        try:
            from docx import Document
            document = Document(path)
            paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
            tables = []
            for table in document.tables[:5]:
                tables.append([[cell.text.strip()[:200] for cell in row.cells[:30]] for row in table.rows[:20]])
            return {"paragraph_count": len(paragraphs), "text_preview": "\n".join(paragraphs)[:4000], "tables": tables}
        except Exception as exc:
            return {"parse_error": str(exc)}
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:5])
            return {"page_count": len(reader.pages), "text_preview": text[:4000], "text_chars_sample": len(text)}
        except Exception as exc:
            return {"parse_error": str(exc)}
    return {"binary_prefix_sha256": hashlib.sha256(_read_prefix(path)).hexdigest()}


def extract_file(path: Path, *, root: Path, deep_limit_bytes: int = 4_000_000, parse_xlsx: bool = False) -> dict[str, Any]:
    path_kind, path_subtype, path_confidence = classify_path(path)
    content_class = classify_with_content(path, current_kind=path_kind, current_confidence=path_confidence)
    kind, subtype = content_class["kind"], content_class["subtype"]
    classification_confidence = float(content_class["confidence"])
    suffix = path.suffix.lower()
    size = path.stat().st_size
    raw = path.read_bytes() if size <= 16 * 1024 * 1024 else b""
    # 订单/库存等表格式样误判修正（任务书 T1）：路径/文件名把一张含完整订单
    # 表头的表格判成库存/未分类时，用与 M1 相同的确定性解析器复核。只有 v2
    # 表头驱动能读到“数量+单价/金额/客户/订单号”等多列订单语义才改判为订单；
    # 仓库/库位/现存数量等库存表头不命中订单别名，保持原分类，绝不把库存表
    # 强行改判成订单。
    if (
        suffix in {".xlsx", ".xlsm"}
        and kind in {"inventory", "tabular", "other", "document"}
        and parse_xlsx
        and size <= deep_limit_bytes
        and raw
    ):
        try:
            from .order_semantics import workbook_looks_like_order

            if workbook_looks_like_order(raw, path.name):
                kind = "order"
                subtype = "customer_or_stocking_order"
                classification_confidence = max(classification_confidence, 0.9)
                rules = list(content_class.get("rules", []))
                rules.append({"rule": "content:order_deterministic", "score": 0.9, "evidence": "表头/行结构命中订单语义（数量+单价/金额/订单号/客户）"})
                content_class = {**content_class, "kind": kind, "subtype": subtype, "confidence": classification_confidence, "rules": rules}
        except Exception:
            # 复核失败保持原分类，不让目录识别因单个文件崩掉。
            pass
    sniffed: dict[str, Any] = {}
    if raw:
        from .file_sniff import sniff_format

        verdict = sniff_format(raw, path.name)
        sniffed = {
            "sniffed_format": verdict.detected_format,
            "sniffed_mime": verdict.mime_type,
            "declared_suffix": verdict.declared_suffix,
            "magic_match": verdict.match,
            "mismatch_reason": verdict.reason,
        }
    extraction: dict[str, Any]
    if suffix in {".zip", ".rar", ".7z"} and raw:
        from .archive_extract import unpack_archive

        unpacked = unpack_archive(raw, filename=path.name)
        extraction = {
            "archive_format": unpacked.get("archive_format"),
            "member_count": unpacked.get("member_count", 0),
            "members": [
                {key: member[key] for key in ("relative_path", "size_bytes", "status", "reason") if key in member}
                for member in unpacked.get("members", [])
            ],
            "unpack_error": unpacked.get("error"),
            "parent_sha256": _sha256(path),
            "size_bytes": size,
        }
        if unpacked.get("error"):
            extraction["extraction_skipped"] = "archive_unsupported_or_corrupt"
    elif suffix == ".xls" and size <= deep_limit_bytes:
        from .xls_reader import extract_xls

        xls_result = extract_xls(path)
        extraction = {
            "parser_version": xls_result.get("parser_version"),
            "sheet_count": xls_result.get("sheet_count", 0),
            "sheets": xls_result.get("sheets", []),
            "xls_error": xls_result.get("error"),
            "size_bytes": size,
        }
        if xls_result.get("error"):
            extraction["extraction_skipped"] = "xls_parse_error"
    elif suffix in {".png", ".jpg", ".jpeg"} and raw:
        extraction = {
            "image_format": sniffed.get("sniffed_format") if sniffed.get("magic_match") else "unsupported",
            "size_bytes": size,
            "image_prefix_sha256": hashlib.sha256(raw[:4096]).hexdigest(),
        }
        if not sniffed.get("magic_match"):
            extraction["extraction_skipped"] = "image_magic_mismatch"
    elif suffix in {".doc", ".pptx"} and raw:
        extraction = {"format": suffix.lstrip("."), "size_bytes": size, "declared_only": True}
    elif suffix == ".xlsx" and (not parse_xlsx or size > deep_limit_bytes):
        extraction = {"extraction_skipped": "xlsx_deferred_to_m1_parser", "size_bytes": size, **sniffed}
    elif size > deep_limit_bytes and suffix in {".xls", ".pdf", ".docx"}:
        extraction = {"extraction_skipped": "large_file", "size_bytes": size, **sniffed}
    elif suffix == ".xlsx":
        extraction = _extract_bom_xlsx(path) if kind == "bom" else _extract_xlsx(path, kind)
        extraction = {**extraction, **sniffed}
    elif suffix in {".csv", ".tsv"}:
        extraction = {**_extract_delimited(path), **sniffed}
    elif suffix == ".json":
        extraction = {**_extract_json(path), **sniffed}
    elif suffix in {".pdf", ".docx", ".txt", ".md"}:
        extraction = {**_extract_text(path), **sniffed}
    else:
        extraction = {"binary_prefix_sha256": hashlib.sha256(_read_prefix(path)).hexdigest(), **sniffed}
    document = extraction.get("order_document") if isinstance(extraction, dict) else None
    order = document if isinstance(document, dict) else {}
    lines = order.get("lines") if isinstance(order.get("lines"), list) else []
    field_observations = []
    if kind != "order" and kind != "bom" and isinstance(extraction, dict) and (extraction.get("sheets") or extraction.get("sample_rows")):
        # 通用表头观察器：equipment/worker/inventory/supplier/route/calendar/tooling 等
        # 非深解析类别也从表头行产出字段级候选观察（raw=normalized + 行列定位）。
        field_observations.extend(_generic_observations_from_sheets(extraction, kind, parser_version=extraction.get("parser_version") or "generic.table.v1"))
    for field in ("order_id", "order_date", "due_date", "supplier_name", "payment_terms", "delivery_address", "product_code", "quantity", "total_amount"):
        value = order.get(field)
        if value not in (None, ""):
            field_observations.append({"field_path": f"$.header.{field}", "raw_value": value, "normalized_value": value, "physical_type": _text_type(value), "semantic_type": field, "confidence": order.get("confidence", classification_confidence), "status": "candidate"})
    v2_evidence = order.get("field_evidence") if isinstance(order.get("field_evidence"), list) else []
    if v2_evidence:
        # 表头驱动解析（order.parser.v2）：字段观察带 sheet/行/列与 parser_version 证据。
        for observation in v2_evidence:
            raw_value = observation.get("raw_value")
            if raw_value in (None, ""):
                continue
            field_observations.append({
                "field_path": observation.get("field_path", ""),
                "raw_value": raw_value,
                "normalized_value": raw_value,
                "physical_type": _text_type(raw_value),
                "semantic_type": observation.get("semantic_type"),
                "confidence": order.get("confidence", classification_confidence),
                "status": "candidate",
                "locator": {
                    "sheet": observation.get("sheet"),
                    "row": observation.get("row"),
                    "column": observation.get("column"),
                    "parser_version": observation.get("parser_version") or extraction.get("parser_version"),
                },
            })
    for line_index, line in enumerate(lines, start=1):
        for field, value in line.items():
            if value not in (None, ""):
                field_observations.append({"field_path": f"$.lines[{line_index - 1}].{field}", "raw_value": value, "normalized_value": value, "physical_type": _text_type(value), "semantic_type": field, "confidence": order.get("confidence", classification_confidence), "status": "candidate"})
    if kind == "bom" and isinstance(extraction, dict):
        document = {
            "schema_version": "m0.bom.v1",
            "document_type": "bom",
            "document_subtype": "engineering_bom",
            "confidence": 0.95 if extraction.get("bom_line_count") else 0.60,
            "review_status": "needs_review" if not extraction.get("bom_line_count") else "candidate",
            "sheet_count": extraction.get("sheet_count", 0),
            "bom_line_count": extraction.get("bom_line_count", 0),
        }
        for line_index, line in enumerate(extraction.get("bom_lines", [])):
            for field in ("material_code", "material_name", "specification", "quantity", "unit", "unit_price", "cost", "supplier"):
                if line.get(field) not in (None, ""):
                    field_observations.append({
                        "field_path": f"$.sheets[{line['sheet_name']!r}].rows[{line['row_number']}].{field}",
                        "raw_value": line[field], "normalized_value": line[field], "physical_type": _text_type(line[field]),
                        "semantic_type": field, "confidence": document["confidence"], "status": "candidate",
                    })
    document_payload = {
        "schema_version": document.get("schema_version") if isinstance(document, dict) and document.get("schema_version") else ("m1.document.v2" if order else None),
        "document_type": document.get("document_type") if isinstance(document, dict) and document.get("document_type") else (order.get("document_type") if order else kind),
        "document_subtype": document.get("document_subtype") if isinstance(document, dict) and document.get("document_subtype") else (order.get("document_subtype") if order else subtype),
        "order_id": order.get("order_id"),
        "product_code": order.get("product_code"),
        "confidence": document.get("confidence") if isinstance(document, dict) and document.get("confidence") is not None else order.get("confidence", classification_confidence),
        "review_status": document.get("review_status") if isinstance(document, dict) and document.get("review_status") else ("needs_review" if order else ("candidate" if kind not in {"other", "tabular", "document", "archive"} else "unclassified")),
        **({"sheet_count": document.get("sheet_count"), "bom_line_count": document.get("bom_line_count")} if kind == "bom" and isinstance(document, dict) else {}),
    }
    # 最低字段门槛（任务书 §3.3）：缺少必需字段进入 needs_review 并返回缺失列表，
    # 不允许把缺字段表格当作普通表格后结束。
    required = REQUIRED_FIELDS_BY_KIND.get(kind)
    present_fields = {observation.get("semantic_type") for observation in field_observations if observation.get("semantic_type")}
    missing_fields: list[dict[str, Any]] = []
    if required:
        missing_fields = [
            {"field": field, "reason": "required_field_missing"}
            for field in required
            if field not in present_fields
            and not any(str(observation.get("field_path", "")).endswith(f".{field}") or f"[{field}]" in str(observation.get("field_path", "")) for observation in field_observations)
        ]
    if required and missing_fields and document_payload.get("review_status") in (None, "unclassified", "candidate"):
        document_payload["review_status"] = "needs_review"
    # 延迟深解析状态贯通（任务书 §5.5）：大表不冒充“已识别完成”。
    if isinstance(extraction, dict) and extraction.get("extraction_skipped") == "xlsx_deferred_to_m1_parser":
        document_payload["review_status"] = "deferred_to_m1"
        document_payload["deferred_to_m1"] = True
    document_payload["missing_fields"] = missing_fields
    document_payload["classification_rules"] = content_class.get("rules", [])
    return {
        "schema_version": SCHEMA_VERSION,
        "relative_path": str(path.relative_to(root)),
        "file_kind": kind,
        "document_subtype": subtype,
        "classification_confidence": classification_confidence,
        "classification_rules": content_class.get("rules", []),
        "extraction": extraction,
        "document": document_payload,
        "field_observations": field_observations,
    }


def iter_source_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in IGNORED_NAMES or any(path.name.startswith(prefix) for prefix in IGNORED_PREFIXES):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        yield path


def init_catalog(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS ingest_batches (
                batch_id TEXT PRIMARY KEY,
                root_path TEXT NOT NULL,
                schema_version TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                file_count INTEGER NOT NULL DEFAULT 0,
                summary_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS source_files (
                file_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES ingest_batches(batch_id),
                absolute_path TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                filename TEXT NOT NULL,
                extension TEXT NOT NULL,
                mime_type TEXT,
                size_bytes INTEGER NOT NULL,
                modified_at TEXT,
                sha256 TEXT NOT NULL,
                file_kind TEXT NOT NULL,
                document_subtype TEXT NOT NULL,
                classification_confidence REAL NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                UNIQUE(absolute_path, sha256)
            );
            CREATE TABLE IF NOT EXISTS document_candidates (
                document_id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL REFERENCES source_files(file_id),
                schema_version TEXT NOT NULL,
                document_type TEXT NOT NULL,
                document_subtype TEXT NOT NULL,
                order_id TEXT,
                product_code TEXT,
                confidence REAL NOT NULL,
                review_status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                validation_issues_json TEXT NOT NULL DEFAULT '[]',
                tenant_id TEXT,
                task_id TEXT,
                batch_id TEXT,
                parser_version TEXT,
                classification_confidence REAL,
                missing_fields_json TEXT NOT NULL DEFAULT '[]',
                sensitivity_classification TEXT NOT NULL DEFAULT 'internal',
                reviewer TEXT,
                reviewed_at TEXT,
                entity_key TEXT,
                entity_version TEXT,
                UNIQUE(file_id)
            );
            CREATE TABLE IF NOT EXISTS field_observations (
                observation_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES document_candidates(document_id),
                field_path TEXT NOT NULL,
                raw_value_json TEXT NOT NULL,
                normalized_value_json TEXT,
                physical_type TEXT NOT NULL,
                semantic_type TEXT,
                unit TEXT,
                confidence REAL,
                status TEXT NOT NULL,
                evidence_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_source_files_kind ON source_files(file_kind);
            CREATE INDEX IF NOT EXISTS idx_document_candidates_order ON document_candidates(order_id);
            CREATE INDEX IF NOT EXISTS idx_field_observations_path ON field_observations(field_path);
            """
        )
        _migrate_catalog(db)


def _migrate_catalog(db: sqlite3.Connection) -> None:
    """轻量列迁移：让 v1 旧库也能补上 v2 新增列（不丢数据）。"""
    existing = {row[1] for row in db.execute("PRAGMA table_info(document_candidates)").fetchall()}
    additions = {
        "tenant_id": "TEXT",
        "task_id": "TEXT",
        "batch_id": "TEXT",
        "parser_version": "TEXT",
        "classification_confidence": "REAL",
        "missing_fields_json": "TEXT NOT NULL DEFAULT '[]'",
        "sensitivity_classification": "TEXT NOT NULL DEFAULT 'internal'",
        "reviewer": "TEXT",
        "reviewed_at": "TEXT",
        "entity_key": "TEXT",
        "entity_version": "TEXT",
    }
    for column, definition in additions.items():
        if column not in existing:
            try:
                db.execute(f"ALTER TABLE document_candidates ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass


def ingest_tree(root: str | Path, db_path: str | Path, *, batch_id: str | None = None, limit: int | None = None, parse_xlsx: bool = False, deep_limit_bytes: int = 4_000_000) -> dict[str, Any]:
    root_path = Path(root).expanduser().resolve()
    if not root_path.is_dir():
        raise ValueError(f"业务数据目录不存在: {root_path}")
    batch_id = batch_id or f"batch-{hashlib.sha256(f'{root_path}:{utc_now()}'.encode()).hexdigest()[:16]}"
    init_catalog(db_path)
    started_at = utc_now()
    paths = list(iter_source_files(root_path))
    if limit is not None:
        paths = paths[: max(0, limit)]
    counts: dict[str, int] = {}
    errors: list[dict[str, str]] = []
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("INSERT OR REPLACE INTO ingest_batches(batch_id, root_path, schema_version, started_at, status, file_count, summary_json) VALUES(?,?,?,?,?,?,?)", (batch_id, str(root_path), SCHEMA_VERSION, started_at, "running", 0, "{}"))
        for index, path in enumerate(paths):
            try:
                stat = path.stat()
                sha256 = _sha256(path)
                existing = db.execute("SELECT file_id, file_kind FROM source_files WHERE absolute_path=? AND sha256=?", (str(path), sha256)).fetchone()
                if existing:
                    counts[existing[1]] = counts.get(existing[1], 0) + 1
                    continue
                extracted = extract_file(path, root=root_path, parse_xlsx=parse_xlsx, deep_limit_bytes=deep_limit_bytes)
                source_key = hashlib.sha256(f"{path}\0{sha256}".encode()).hexdigest()
                file_id = f"file-{source_key[:24]}"
                document_id = f"doc-{source_key[:24]}"
                kind = extracted["file_kind"]
                counts[kind] = counts.get(kind, 0) + 1
                document = extracted["document"]
                source_status = "deferred_to_m1" if document.get("review_status") == "deferred_to_m1" else "identified"
                db.execute(
                    """INSERT INTO source_files(file_id,batch_id,absolute_path,relative_path,filename,extension,mime_type,size_bytes,modified_at,sha256,file_kind,document_subtype,classification_confidence,status,metadata_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(absolute_path,sha256) DO UPDATE SET batch_id=excluded.batch_id, metadata_json=excluded.metadata_json, status=excluded.status""",
                    (file_id, batch_id, str(path), extracted["relative_path"], path.name, path.suffix.lower(), mimetypes.guess_type(path.name)[0], stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(), sha256, kind, extracted["document_subtype"], extracted["classification_confidence"], source_status, json_text({"schema_version": SCHEMA_VERSION, "extraction": extracted["extraction"]})),
                )
                db.execute("DELETE FROM field_observations WHERE document_id=?", (document_id,))
                sensitivity = sensitivity_of(extracted, kind)
                issues = list(document.get("validation_issues") or [])
                missing = list(document.get("missing_fields") or [])
                db.execute(
                    """INSERT INTO document_candidates(document_id,file_id,schema_version,document_type,document_subtype,order_id,product_code,confidence,review_status,payload_json,validation_issues_json,tenant_id,task_id,batch_id,parser_version,classification_confidence,missing_fields_json,sensitivity_classification,entity_key,entity_version)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(file_id) DO UPDATE SET document_id=excluded.document_id, payload_json=excluded.payload_json, order_id=excluded.order_id, product_code=excluded.product_code, confidence=excluded.confidence, review_status=excluded.review_status, classification_confidence=excluded.classification_confidence, missing_fields_json=excluded.missing_fields_json, sensitivity_classification=excluded.sensitivity_classification, parser_version=excluded.parser_version""",
                    (document_id, file_id, document.get("schema_version") or SCHEMA_VERSION, document.get("document_type") or kind, document.get("document_subtype") or extracted["document_subtype"], document.get("order_id"), document.get("product_code"), float(document.get("confidence") or 0), document.get("review_status") or "unclassified", json_text({"document": document, "extraction": extracted["extraction"]}), json_text(issues), None, None, batch_id, extracted.get("parser_version") or (document.get("parser_version") if isinstance(document, dict) else None) or "unknown", extracted["classification_confidence"], json_text(missing), sensitivity, document.get("entity_key"), document.get("entity_version")),
                )
                for obs_index, observation in enumerate(extracted["field_observations"]):
                    obs_id = f"obs-{source_key[:16]}-{obs_index}"
                    db.execute(
                        "INSERT INTO field_observations(observation_id,document_id,field_path,raw_value_json,normalized_value_json,physical_type,semantic_type,unit,confidence,status,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (obs_id, document_id, observation["field_path"], json_text(observation["raw_value"]), json_text(observation["normalized_value"]), observation["physical_type"], observation.get("semantic_type"), observation.get("unit"), observation.get("confidence"), observation.get("status", "candidate"), json_text({"source_file": str(path), "sha256": sha256, "locator": observation["field_path"]})),
                    )
            except Exception as exc:
                errors.append({"path": str(path), "error": str(exc)})
                db.execute("INSERT OR REPLACE INTO source_files(file_id,batch_id,absolute_path,relative_path,filename,extension,mime_type,size_bytes,modified_at,sha256,file_kind,document_subtype,classification_confidence,status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"error-{hashlib.sha256(str(path).encode()).hexdigest()[:24]}", batch_id, str(path), str(path.relative_to(root_path)), path.name, path.suffix.lower(), mimetypes.guess_type(path.name)[0], path.stat().st_size, None, "", "other", "unreadable", 0.0, "error", json_text({"error": str(exc)})))
            if (index + 1) % 25 == 0:
                db.commit()
        summary = {"counts_by_kind": counts, "errors": errors[:100], "error_count": len(errors), "root_path": str(root_path)}
        db.execute("UPDATE ingest_batches SET finished_at=?, status=?, file_count=?, summary_json=? WHERE batch_id=?", (utc_now(), "completed_with_errors" if errors else "completed", len(paths), json_text(summary), batch_id))
        db.commit()
    return {"batch_id": batch_id, "root_path": str(root_path), "file_count": len(paths), "counts_by_kind": counts, "error_count": len(errors), "errors": errors[:20], "db_path": str(Path(db_path).resolve())}


def catalog_summary(db_path: str | Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as db:
        return {
            "source_files": db.execute("SELECT count(*) FROM source_files").fetchone()[0],
            "documents": db.execute("SELECT count(*) FROM document_candidates").fetchone()[0],
            "field_observations": db.execute("SELECT count(*) FROM field_observations").fetchone()[0],
            "by_kind": db.execute("SELECT file_kind, count(*) FROM source_files GROUP BY file_kind ORDER BY file_kind").fetchall(),
            "by_review_status": db.execute("SELECT review_status, count(*) FROM document_candidates GROUP BY review_status ORDER BY review_status").fetchall(),
        }


# 候选审核状态机（任务书 §3.4：identified -> candidate -> needs_review -> approved/rejected）。
REVIEW_STATE_MACHINE = {
    "identified": {"candidate", "needs_review", "rejected"},
    "candidate": {"needs_review", "approved", "rejected"},
    "needs_review": {"approved", "rejected", "candidate"},
    "approved": {"rejected"},
    "rejected": {"candidate"},
}


def transition_candidate_review(db_path: str | Path, document_id: str, *, decision: str, actor: str = "operator", entity_key: str = "", entity_version: str = "") -> dict[str, Any]:
    """迁移单个候选的审核状态并记录审核人/时间/entity 标识。

    decision: approve | reject | back_to_candidate | mark_needs_review
    返回 {document_id, from_status, to_status, reviewer, reviewed_at}；
    非法迁移或未知候选抛 ValueError。
    """
    transition_map = {
        "approve": "approved",
        "reject": "rejected",
        "back_to_candidate": "candidate",
        "mark_needs_review": "needs_review",
    }
    target = transition_map.get(decision)
    if target is None:
        raise ValueError(f"不支持的审核决策: {decision}")
    init_catalog(db_path)
    with sqlite3.connect(db_path) as db:
        row = db.execute("SELECT review_status FROM document_candidates WHERE document_id=?", (document_id,)).fetchone()
        if row is None:
            raise ValueError(f"候选不存在: {document_id}")
        current = row[0]
        allowed = REVIEW_STATE_MACHINE.get(current, set())
        if target not in allowed:
            raise ValueError(f"非法候选状态迁移: {current} -> {target}")
        reviewed_at = utc_now()
        db.execute(
            "UPDATE document_candidates SET review_status=?, reviewer=?, reviewed_at=?, entity_key=COALESCE(?, entity_key), entity_version=COALESCE(?, entity_version) WHERE document_id=?",
            (target, actor, reviewed_at, entity_key or None, entity_version or None, document_id),
        )
        return {
            "document_id": document_id,
            "from_status": current,
            "to_status": target,
            "decision": decision,
            "reviewer": actor,
            "reviewed_at": reviewed_at,
        }


def list_candidates(db_path: str | Path, *, review_status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """列出候选（供审核队列回读），绝不下钻 canonical 语义。"""
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        sql = """SELECT document_id, file_id, document_type, document_subtype, confidence, review_status,
                        parser_version, classification_confidence, sensitivity_classification,
                        reviewer, reviewed_at, entity_key, entity_version, missing_fields_json
                 FROM document_candidates"""
        params: list[Any] = []
        if review_status:
            sql += " WHERE review_status=?"
            params.append(review_status)
        sql += " ORDER BY document_id LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        rows = db.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def canonical_records_from_batch(
    db_path: str | Path,
    *,
    batch_id: str,
    tenant_id: str,
    product_code: str,
    product_name: str = "",
    reviewed_by: str = "operator",
) -> list[dict[str, Any]]:
    """Build review-approved M0 records from a reviewed upload batch.

    This is intentionally based on extracted structure stored in the catalog,
    never on a filename or a tenant-specific branch.  A product code is an
    explicit identity supplied by the workflow/request; ambiguous files are
    omitted instead of being guessed into canonical data.
    """
    if not product_code.strip():
        return []
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT c.document_id, c.document_type, c.document_subtype,
                      c.payload_json, f.filename, f.sha256, f.absolute_path
                 FROM document_candidates c
                 JOIN source_files f ON f.file_id = c.file_id
                WHERE c.batch_id=? AND c.review_status IN ('candidate','needs_review','approved')
                ORDER BY c.document_id""",
            (batch_id,),
        ).fetchall()

    def evidence(filename: str, digest: str) -> list[dict[str, Any]]:
        return [{"key": "source", "locator": {"filename": filename, "sha256": digest}, "excerpt": filename}]

    def base(entity_type: str, business_key: str, version_id: str, filename: str, digest: str) -> dict[str, Any]:
        external_id = f"{filename}::{entity_type}::{business_key}::{version_id or 'current'}"
        return {
            "schema_version": "m0.ingest.v1",
            "tenant_id": tenant_id,
            "idempotency_key": f"upload:{batch_id}:{entity_type}:{business_key}:{version_id}",
            "source": {"system": "yunpai-business-upload", "external_id": external_id, "sha256": digest},
            "identity": {"business_key": business_key, "version_id": version_id},
            "evidence": evidence(filename, digest),
            "review_status": "approved",
            "reviewed_by": reviewed_by,
            "entity_type": entity_type,
        }

    records: list[dict[str, Any]] = []
    seen_bom = False
    seen_sop = False
    seen_materials: set[str] = set()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        extraction = payload.get("extraction") if isinstance(payload, dict) else {}
        document = payload.get("document") if isinstance(payload, dict) else {}
        if not isinstance(extraction, dict):
            extraction = {}
        if not isinstance(document, dict):
            document = {}
        filename = str(row["filename"] or "upload")
        digest = str(row["sha256"] or "")
        if len(digest) != 64:
            continue
        kind = str(row["document_type"] or "")
        if kind == "bom" and not seen_bom:
            raw_lines = extraction.get("bom_lines") or []
            # A workbook may contain many product sheets. Select the sheet
            # with the strongest token overlap against the explicit product
            # name; a single-sheet workbook remains fully generic.
            if raw_lines:
                sheets = {}
                for line in raw_lines:
                    sheets.setdefault(str(line.get("sheet_name") or ""), []).append(line)
                normalized_name = product_name.lower().replace(" ", "")
                tokens = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9]{2,}", normalized_name))
                # Include distinctive Chinese bigrams so a product such as
                # "灰色菱形" cannot tie with a generic 8K/HDTV sheet.
                tokens.update(normalized_name[index:index + 2] for index in range(len(normalized_name) - 1) if re.fullmatch(r"[\u4e00-\u9fff]{2}", normalized_name[index:index + 2]))
                scored = sorted(((sum(1 for token in tokens if token in name.lower().replace(" ", "")), name, lines_for_sheet) for name, lines_for_sheet in sheets.items()), reverse=True)
                if scored and (scored[0][0] > 0 or len(scored) == 1):
                    raw_lines = scored[0][2]
                numbered = []
                for line in raw_lines:
                    raw_cells = line.get("raw_cells") if isinstance(line, dict) else {}
                    line_no = raw_cells.get("2") if isinstance(raw_cells, dict) else None
                    if isinstance(line_no, (int, float)) and float(line_no).is_integer() and int(line_no) <= 7:
                        numbered.append(line)
                if numbered:
                    raw_lines = numbered
            lines = []
            for index, line in enumerate(raw_lines, start=1):
                if not isinstance(line, dict) or not str(line.get("material_code") or "").strip():
                    continue
                try:
                    quantity = float(line.get("quantity") or line.get("unit_price") or 0)
                except (TypeError, ValueError):
                    quantity = 0
                if quantity <= 0:
                    continue
                lines.append({
                    "line_no": str(line.get("line_number") or line.get("row_number") or index),
                    "material_code": str(line.get("material_code")),
                    "material_name": str(line.get("material_name") or line.get("material_code")),
                    "quantity": quantity,
                    "uom": str(line.get("unit") or "PCS"),
                    "loss_rate": 0,
                })
            if lines:
                record = base("bom", product_code, str(extraction.get("version") or "upload"), filename, digest)
                record["payload"] = {"product_code": product_code, "status": "active", "lines": lines, "attributes": {"source_batch": batch_id}}
                records.append(record)
                seen_bom = True
                for line in lines:
                    code = line["material_code"]
                    if code in seen_materials:
                        continue
                    material = base("material", code, "", filename, digest)
                    material["payload"] = {"name": line["material_name"], "unit": line["uom"], "status": "active", "attributes": {"source_batch": batch_id}}
                    records.append(material)
                    seen_materials.add(code)
        elif kind == "sop" and not seen_sop:
            route_steps: list[dict[str, Any]] = []
            for source_index, sheet in enumerate(extraction.get("sheets") or [], start=1):
                if not isinstance(sheet, dict) or str(sheet.get("name") or "") == "工艺流程图":
                    continue
                rows_sample = sheet.get("sample_rows") or []
                if len(rows_sample) < 6:
                    continue
                station = ""
                sequence = source_index
                for cell in (rows_sample[2] if len(rows_sample) > 2 else []):
                    text = str(cell or "").strip()
                    if "制作工站" in text:
                        station = text.split("：", 1)[-1].split(":", 1)[-1].strip()
                if len(rows_sample) > 2 and len(rows_sample[2]) > 3:
                    try:
                        sequence = int(float(rows_sample[2][3]))
                    except (TypeError, ValueError):
                        pass
                operation_text = str(rows_sample[5][4] if len(rows_sample[5]) > 4 else "").strip()
                equipment_text = str(rows_sample[7][1] if len(rows_sample) > 7 and len(rows_sample[7]) > 1 else "").strip()
                quality_text = str(rows_sample[7][4] if len(rows_sample) > 7 and len(rows_sample[7]) > 4 else "").strip()
                if not station and not operation_text:
                    continue
                route_steps.append({
                    "sequence_no": sequence,
                    "operation_code": f"TX-001-OP-{source_index:02d}",
                    "operation_name": station or str(sheet.get("name") or f"OP-{source_index:02d}"),
                    "standard_time": "",
                    "station_code": station,
                    "equipment_codes": [],
                    "tooling_codes": [],
                    "attributes": {"instructions": operation_text, "equipment_text": equipment_text, "quality_requirements": quality_text},
                })
            if route_steps:
                record = base("document", f"{product_code}-sop", "SOP-TX001-A1", filename, digest)
                record["payload"] = {
                    "role": "sop", "title": Path(filename).stem, "product_codes": [product_code],
                    "content_uri": f"sha256:{digest}", "status": "active",
                    "attributes": {"route_steps": route_steps, "time_source": "source_not_provided", "source_batch": batch_id},
                }
                records.append(record)
                seen_sop = True
    if product_name and not any(item.get("entity_type") == "product" for item in records):
        first = next((item for item in records if item.get("entity_type") in {"bom", "document"}), None)
        if first:
            product = base("product", product_code, "", str(first["source"]["external_id"]), str(first["source"]["sha256"]))
            product["payload"] = {"name": product_name, "model": product_code, "aliases": [product_code], "status": "active", "attributes": {"source_batch": batch_id}}
            records.insert(0, product)
    return records
