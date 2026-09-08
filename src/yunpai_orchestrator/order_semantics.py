from __future__ import annotations

"""M1 订单语义补充层（确定性、可版本化、证据可回放）。

设计约束（对应任务书 T1）：
- 外部 M1 服务对真实 XLSX 可能只返回 ``m1.document.v2`` 的“订单类文档但 0 行/
  缺订单号”结果（实测）。本模块提供**不冒充外部 M1**的
  确定性本地解析：表头/块事实/行级证据全部可复现，产出带来源坐标与 SHA 的
  候选文档，并把“外部原始结果”与“本地补充候选”同时保留。
- 任何无法确认的字段（订单号缺失、行缺产品编码、交期缺失……）都以
  ``review_issues``/``missing_fields`` 显式标记并强制进入 review Gate；
  本模块绝不静默把 0 行订单伪装成成功，也绝不用本地结果覆盖外部结果。
- 同一解析入口同时服务 business_catalog（订单/库存误判修正）与本地 fixture
  重放，避免“坐标写死解析”在多模板之间漂移。
"""

from hashlib import sha256
from typing import Any

SUPPLEMENT_SCHEMA_VERSION = "m1.semantic-supplement.v1"
SEMANTIC_REVISION = "order.semantics.2026.09.05"

#: 外部 M1 结果中表示“订单”的关键路径值（常见于 T8 m1 服务返回）。
_ORDER_MARKERS = ("order", "customer_purchase_order", "customer_or_stocking_order", "stocking_order", "采购订单", "销售订单", "备货订单")


def _cell_text(value: Any) -> str:
    return str(value or "").strip()


def sniff_xlsx_bytes(raw: bytes | None) -> bool:
    """按文件结构（magic bytes + OOXML 内部路径）判断字节是否为 XLSX/XLSM。

    只依据结构证据，不依据文件名/路径/租户：ZIP magic (PK\\x03\\x04) 且前
    4 KiB 内出现 ``xl/`` 或 OOXML 内容类型声明时视为可尝试解析的表格字节。
    真正的解析仍由 openpyxl 执行，解析失败（伪装 zip/损坏文件）时调用方保持
    原结果不变，绝不伪造解析成功。
    """
    if not isinstance(raw, bytes) or not raw:
        return False
    if raw[:4] != b"PK\x03\x04" and raw[:2] != b"PK":
        return False
    head = raw[:4096]
    return b"xl/" in head or b"[Content_Types].xml" in head or b"xl/workbook" in head


def parsed_has_order_shape(document: dict[str, Any]) -> bool:
    """判断确定性解析结果是否具备“订单表格”结构证据。

    有订单行、或块事实/表头事实（订单号/日期/客户/供应商/编码）、或至少一个
    Sheet 找到订单表头行即视为订单形状；否则（纯库存/设备/无表头表格）不算，
    避免把任意表格冒充成订单文档。
    """
    if not isinstance(document, dict):
        return False
    lines = document.get("lines")
    if isinstance(lines, list) and lines:
        return True
    for key in ("order_id", "order_date", "due_date", "customer_name", "supplier_name", "product_code"):
        if _cell_text(document.get(key)):
            return True
    sheet_docs = document.get("sheet_docs")
    if isinstance(sheet_docs, list) and any(
        isinstance(item, dict) and item.get("status") == "header_found" for item in sheet_docs
    ):
        return True
    return False


def workbook_parse_candidate(filename: str, raw: bytes) -> dict[str, Any] | None:
    """只在结构证据支持“订单”时返回确定性解析结果，否则返回 None。

    证据（全部只读自文件本身，不依赖文件名/路径/租户）：
    - 字节结构是 XLSX/XLSM（magic + OOXML 路径）；
    - 前几行无仓库/库位/现存数量/盘点等库存专属表头（库存表否决）；
    - 表头块事实（订单号/客户/日期/交期/供应商）**或**明细行含单价/金额
      （价格信号）——二者至少其一，数量+名称/纯编码表（料号清单、设备台账
      等）不会被当作订单候选。

    None 表示无法确认是订单：调用方必须保持原分类/失败关闭，绝不空成功。
    """
    if not sniff_xlsx_bytes(raw) or _workbook_has_inventory_headers(raw):
        return None
    try:
        parsed = parse_order_document(filename, raw)
    except Exception:
        return None
    document = parsed.get("document") or {}
    if not parsed_has_order_shape(document):
        return None
    block_facts = any(
        _cell_text(document.get(key))
        for key in ("order_id", "order_date", "due_date", "customer_name", "supplier_name")
    )
    if block_facts:
        return parsed
    lines = [line for line in (document.get("lines") or []) if isinstance(line, dict)]
    price_signal = any(
        line.get("unit_price") is not None or line.get("amount") is not None for line in lines
    )
    if price_signal:
        return parsed
    return None


def _looks_order_marker(value: Any) -> bool:
    text = _cell_text(value).lower().replace("_", "").replace("-", "").replace(" ", "")
    return any(marker.replace("_", "").replace("-", "") in text for marker in _ORDER_MARKERS)


def result_declares_order(result: dict[str, Any]) -> bool:
    """判断外部 M1 ingest_document 结果是否把文档归类为订单。"""
    if not isinstance(result, dict):
        return False
    doc = result.get("document")
    if not isinstance(doc, dict):
        doc = {}
    markers = (
        result.get("document_type"),
        result.get("document_subtype"),
        result.get("doc_type"),
        doc.get("document_type"),
        doc.get("document_subtype"),
        doc.get("document_kind_label"),
        result.get("order_type"),
    )
    return any(_looks_order_marker(marker) for marker in markers if marker not in (None, ""))


def _order_id_candidates(result: dict[str, Any]) -> list[str]:
    """收集外部结果里可能承载订单号的字段值（不信任空值）。"""
    doc = result.get("document") if isinstance(result.get("document"), dict) else {}
    header = doc.get("header") if isinstance(doc.get("header"), dict) else {}
    candidates: list[str] = []
    for container in (result, doc, header):
        for key in ("order_id", "order_number", "order_no", "po_number", "doc_number", "document_no", "order_num"):
            value = container.get(key) if isinstance(container, dict) else None
            if _cell_text(value):
                candidates.append(str(value))
    return candidates


def result_has_order_gap(result: dict[str, Any]) -> bool:
    """外部 M1 结果声明了订单，但缺少关键订单事实（需要本地补充候选）。

    只有当文档确实被归类为订单时才可能触发补充；非订单文档(库存/图纸/资料)
    保持原样，绝不把非订单文件改写成订单候选。订单行已经存在但顶层产品编码
    缺失时也必须补充，否则下游 M2 无法把订单型号提升为权威产品事实。
    """
    if not isinstance(result, dict) or not result_declares_order(result):
        return False
    doc = result.get("document") if isinstance(result.get("document"), dict) else {}
    lines = doc.get("lines")
    line_count = len(lines) if isinstance(lines, list) else 0
    if line_count == 0:
        return True
    if not _order_id_candidates(result):
        return True
    doc_header = doc.get("header") if isinstance(doc.get("header"), dict) else {}
    top_product_code = (
        result.get("product_code")
        or doc.get("product_code")
        or doc_header.get("product_code")
        or doc_header.get("model")
    )
    if _cell_text(top_product_code):
        return False
    # Only trigger for a useful, deterministic repair: an order line that already
    # carries a model/product code.  A line without one remains an ordinary
    # missing-code review case and must not be guessed.
    return any(
        isinstance(line, dict) and _cell_text(line.get("product_code") or line.get("model"))
        for line in lines
    )


def _pick_best_document(filename: str, raw: bytes) -> dict[str, Any]:
    """v2（表头驱动）优先；老式裸值模板由坐标解析兜底。

    选择规则与 business_catalog 的历史语义保持一致：当 v2 缺少订单号但坐标
    解析能给出订单号时采用坐标结果，避免老模板回归。
    """
    from .order_parser_v2 import parse_order_sheets
    from .order_workbook import parse_order_workbook

    v2 = parse_order_sheets(filename, raw)
    legacy: dict[str, Any] = {}
    try:
        legacy = parse_order_workbook(filename, raw)
    except Exception:
        legacy = {}
    v2_lines = v2.get("lines") or []
    legacy_lines = legacy.get("lines") or []
    v2_has_facts = bool(v2_lines) or any(v2.get(key) for key in ("order_id", "order_date", "due_date", "supplier_name"))
    legacy_has_facts = bool(legacy.get("order_id") or legacy_lines)
    # Some legacy stocking-order workbooks place labels such as “结款方式：”
    # in cells that the header-driven parser mistakes for an order number.
    # Prefer the coordinate parser when it has actual order lines and v2 has
    # no lines; a non-empty label is not stronger evidence than extracted rows.
    if legacy_lines and not v2_lines:
        return {"parser_version": "order.workbook.coordinates.v1", "document": legacy, "parser_name": "order.workbook.coordinates.v1"}
    if v2_has_facts and not (legacy_has_facts and legacy.get("order_id") and not v2.get("order_id")):
        return {"parser_version": v2.get("parser_version") or "order.parser.v2", "document": v2, "parser_name": "order.parser.v2"}
    if legacy_has_facts:
        return {"parser_version": "order.workbook.coordinates.v1", "document": legacy, "parser_name": "order.workbook.coordinates.v1"}
    return {"parser_version": v2.get("parser_version") or "order.parser.v2", "document": v2, "parser_name": "order.parser.v2"}


def parse_order_document(filename: str, raw: bytes) -> dict[str, Any]:
    """可复用订单文档解析入口（v2 优先 + 老模板坐标兜底）。

    返回 {"parser_name", "parser_version", "document", "source": {...}}；
    不能解析时 document 为空壳且 error 字段存在。
    """
    try:
        picked = _pick_best_document(filename, raw)
    except Exception as exc:  # pragma: no cover - 防御损坏文件
        return {
            "parser_name": "order_semantics",
            "parser_version": SEMANTIC_REVISION,
            "document": {},
            "error": f"order document parse failed: {exc}",
            "source": {"original_filename": filename, "sha256": sha256(raw).hexdigest()},
        }
    document = picked.get("document") or {}
    return {
        "parser_name": picked.get("parser_name", "order.parser.v2"),
        "parser_version": picked.get("parser_version", "order.parser.v2"),
        "document": document,
        "source": {"original_filename": filename, "sha256": sha256(raw).hexdigest()},
    }


def build_semantic_supplement(filename: str, raw: bytes, *, external: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """为“外部 M1 声明订单但缺事实”的结果构建本地确定性候选补充。

    只有字节结构 + 表头/价格证据确认是订单工作簿才可能给出补充
    （``workbook_parse_candidate``）；库存/设备等表格返回 ``None``，调用方
    不得伪造补充。
    """
    parsed = workbook_parse_candidate(filename, raw)
    if parsed is None:
        return None
    document = parsed.get("document") or {}
    lines = document.get("lines") if isinstance(document.get("lines"), list) else []
    order_id = _cell_text(document.get("order_id"))
    product_code = _cell_text(document.get("product_code"))
    if not product_code:
        for line in lines:
            if isinstance(line, dict):
                product_code = _cell_text(line.get("product_code") or line.get("model"))
                if product_code:
                    break
    # Some real M1 templates return reliable order lines but omit the product
    # code at the document header. Reuse that already-observed line model as a
    # candidate fact; it is still review-gated and never overwrites M1 output.
    external_doc = external.get("document") if isinstance(external, dict) and isinstance(external.get("document"), dict) else {}
    external_lines = external_doc.get("lines") if isinstance(external_doc.get("lines"), list) else []
    if not product_code:
        for line in external_lines:
            if isinstance(line, dict):
                product_code = _cell_text(line.get("product_code") or line.get("model"))
                if product_code:
                    break
    header_block = {
        "order_number": order_id,
        "order_date": _cell_text(document.get("order_date")),
        "due_date": _cell_text(document.get("due_date")),
        "customer_name": _cell_text(document.get("customer_name")),
        "supplier_name": _cell_text(document.get("supplier_name")),
        "product_code": product_code,
    }
    missing: list[dict[str, Any]] = []
    if not order_id:
        missing.append({"field": "order_number", "level": "header", "reason": "missing_required_field"})
    if not header_block["due_date"]:
        missing.append({"field": "due_date", "level": "header", "reason": "missing_required_field"})
    if not header_block["product_code"]:
        missing.append({"field": "header.product_code", "level": "header", "reason": "missing_required_field"})
    line_missing_codes = 0
    for line in lines:
        if not _cell_text(line.get("product_code")):
            line_missing_codes += 1
    if line_missing_codes:
        missing.append({"field": "line.product_code", "level": "line", "reason": "missing_required_field", "line_count": line_missing_codes})

    review_issues = [
        item for item in (document.get("review_issues") or []) if isinstance(item, dict)
    ]
    if not order_id:
        review_issues.append({"code": "MISSING_ORDER_NUMBER", "severity": "high", "message": "表头未识别到订单号"})
    conflicts: list[dict[str, Any]] = []
    if isinstance(external, dict):
        external_doc = external.get("document") if isinstance(external.get("document"), dict) else {}
        external_lines = external_doc.get("lines")
        external_count = len(external_lines) if isinstance(external_lines, list) else 0
        if external_count != len(lines):
            conflicts.append({"field": "lines", "external": external_count, "supplement": len(lines), "note": "外部 M1 与本地确定性解析的行数不一致，以人工复核结论为准"})
        if not _order_id_candidates(external) and order_id:
            conflicts.append({"field": "order_number", "external": None, "supplement": order_id, "note": "外部 M1 未识别订单号，本地解析按表头块事实补充"})
    supplement: dict[str, Any] = {
        "schema_version": SUPPLEMENT_SCHEMA_VERSION,
        "revision": SEMANTIC_REVISION,
        "provider": "local_deterministic",
        "parser": {
            "name": parsed.get("parser_name", "order.parser.v2"),
            "version": parsed.get("parser_version", "order.parser.v2"),
            "revision": SEMANTIC_REVISION,
        },
        "source": parsed.get("source", {"original_filename": filename, "sha256": sha256(raw).hexdigest()}),
        "document": {
            "schema_version": "m1.document.v2",
            "document_type": "order",
            "document_subtype": _cell_text(document.get("document_subtype")) or "customer_or_stocking_order",
            "header": header_block,
            "lines": lines,
            "totals": {"quantity": document.get("total_quantity") if document.get("total_quantity") is not None else document.get("quantity"), "total_amount": document.get("total_amount")},
            "field_evidence": document.get("field_evidence") or [],
            "confidence": float(document.get("confidence") or 0.5),
            "validation_issues": document.get("validation_issues") or [],
        },
        "missing_fields": missing,
        "review_issues": [item for item in review_issues if item],
        "conflicts": conflicts,
        "requires_review": True,
        "note": "本地确定性解析补充（不覆盖外部 M1 结果）；订单头/行与编码映射需人工复核后才可作为权威输入",
    }
    return supplement


#: 库存/盘点类表单的专属表头（出现即否决“订单”判读，避免把库存表误判成订单）。
_INVENTORY_ONLY_HEADER_TOKENS = (
    "仓库", "库位", "库区", "现存数量", "库存量", "现存量", "盘点", "批次", "结存", "账面", "库存状态",
)


def _workbook_has_inventory_headers(raw: bytes) -> bool:
    """浅扫前几张 Sheet 的表头/前几行是否含库存专属词。"""
    try:
        from io import BytesIO

        from openpyxl import load_workbook

        workbook = load_workbook(BytesIO(raw), read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets[:3]:
                for row in sheet.iter_rows(min_row=1, max_row=12, values_only=True):
                    for value in row:
                        if value is None:
                            continue
                        text = str(value).strip()
                        if any(token in text for token in _INVENTORY_ONLY_HEADER_TOKENS):
                            return True
        finally:
            workbook.close()
    except Exception:
        return False
    return False


def workbook_looks_like_order(raw: bytes, filename: str = "workbook.xlsx") -> bool:
    """业务资料目录里用同一解析器判断表格是否更像订单（修正订单/库存误判）。

    委托给 ``workbook_parse_candidate``：只有字节结构 + 表头块事实或价格信号
    的订单证据才返回 True；库存/设备等表单（仓库/库位/现存数量表头或仅有
    数量+名称/编码）不会命中，保持原分类。
    """
    return workbook_parse_candidate(filename, raw) is not None
