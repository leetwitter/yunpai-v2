"""M1 通用表格解析能力回归测试（表头别名 / 列顺序 / 结构干扰 / 跨路径租户）。

要求覆盖（任务书约束）：
1. 同一解析器在“列顺序不同 / 表头别名不同 / 多 Sheet / 合并单元格 / 隐藏行列 /
   空行 / 合计行 / 缺字段”等布局下产出**一致的订单事实**，绝不依赖文件名、
   路径或租户；
2. 库存、设备等非订单反例不得被解析/补充/改判成订单（fail-closed 或保持原分类）；
3. 缺字段/仅合计行样本进入 needs_review，绝不返回空成功；
4. 相同 Tool（ingest_document 本地 handler / M1 HTTP adapter / business_catalog
   分类）在**不同文件名、不同路径、不同租户**下行为一致。

所有工作簿数值与名称均为虚构值（真实样本含客户数据，仅存于仓库外 run
目录，仓库内只用同构合成布局）。
"""

from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook

from yunpai_orchestrator.business_catalog import extract_file
from yunpai_orchestrator.m1_http_adapter import bind_m1_http
from yunpai_orchestrator.order_semantics import (
    build_semantic_supplement,
    parse_order_document,
    sniff_xlsx_bytes,
    workbook_looks_like_order,
    workbook_parse_candidate,
)
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.workers import m1_parse

from m1_fake_http import BASE, Backend, install
from test_order_semantics import inventory_like_bytes, real_order_twin_bytes


@pytest.fixture(autouse=True)
def _isolated_m1_db(tmp_path, monkeypatch):
    """本地 handler 已真实落库（``m1_domain.M1Store``，rows-S2 第 1 行）：测试用
    临时库隔离，避免污染 ``runtime/yunpai-m1.sqlite`` 与跨用例串行污染。"""
    monkeypatch.setenv("YUNPAI_M1_DB", str(tmp_path / "m1.sqlite"))

ORDER_NO = "WX20260905001"  # 虚构值（真实样本订单号不进入仓库）
CUSTOMER = "测试客户A"


def _save(workbook: Workbook) -> bytes:
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _canonical_facts(doc: dict) -> dict:
    return {
        "order_id": doc.get("order_id"),
        "order_date": doc.get("order_date"),
        "customer_name": doc.get("customer_name"),
        "due_date": doc.get("due_date"),
        "line_count": doc.get("line_count"),
        "total_quantity": doc.get("total_quantity"),
        "total_amount": doc.get("total_amount"),
        "lines": [
            (line.get("row"), line.get("specification"), line.get("quantity"),
             line.get("unit_price"), line.get("amount"), line.get("product_code"))
            for line in (doc.get("lines") or [])
        ],
        "missing_codes": doc.get("lines_with_missing_product_code"),
        "issue_codes": sorted({item.get("code") for item in (doc.get("validation_issues") or []) if item.get("code")}),
    }


def variant_reordered_alias_bytes() -> bytes:
    """列顺序打乱 + 别名不同（物料名称/规格型号/订货数量/不含税单价/产品型号）。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet["A1"] = "销售备货单"
    sheet["A2"] = "采购订单号："
    sheet["B2"] = ORDER_NO
    sheet["A3"] = "客户："
    sheet["B3"] = CUSTOMER
    sheet["A4"] = "订购日期："
    sheet["B4"] = datetime(2026, 9, 1)
    sheet["A5"] = "交货日期："
    for cell, text in {
        "A7": "产品名称", "B7": "规格型号", "C7": "订货数量", "D7": "不含税单价",
        "E7": "金额", "F7": "产品型号", "G7": "装箱数量", "H7": "备注",
    }.items():
        sheet[cell] = text
    sheet["A8"] = "测试线材 型号V1 OD5.0"
    sheet["B8"] = "15M"
    sheet["C8"] = 500
    sheet["D8"] = 60
    sheet["E8"] = 30000
    sheet["F8"] = "无"
    sheet["G8"] = 12
    sheet["B9"] = "20M"
    sheet["C9"] = 500
    sheet["D9"] = 62
    sheet["E9"] = 31000
    sheet["F9"] = "无"
    sheet["G9"] = 12
    sheet["A10"] = "合计"
    sheet["E10"] = 61000
    return _save(workbook)


def variant_merged_prefix_bytes() -> bytes:
    """别名再变 + 前缀标题行 + 合并单元格块事实（订单编号/下单日期/客户名称）。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.merge_cells("A1:D1")
    sheet["A1"] = "供货订单"
    sheet["A3"] = "订单编号："
    sheet["B3"] = ORDER_NO
    sheet["A4"] = "下单日期："
    sheet.merge_cells("B4:C4")
    sheet["B4"] = datetime(2026, 9, 1)
    sheet["A5"] = "客户名称："
    sheet.merge_cells("B5:C5")
    sheet["B5"] = CUSTOMER
    sheet["A6"] = "交货日期："
    for cell, text in {
        "A7": "产品型号", "B7": "名称材质要求", "C7": "规格", "D7": "订购数量",
        "E7": "单价", "F7": "总金额", "G7": "每箱数量",
    }.items():
        sheet[cell] = text
    sheet["A8"] = "无"
    sheet["B8"] = "测试线材 型号V1 OD5.0"
    sheet["C8"] = "15M"
    sheet["D8"] = 500
    sheet["E8"] = 60
    sheet["F8"] = 30000
    sheet["G8"] = 12
    sheet["A9"] = "无"
    sheet["C9"] = "20M"
    sheet["D9"] = 500
    sheet["E9"] = 62
    sheet["F9"] = 31000
    sheet["G9"] = 12
    sheet["A10"] = "合计"
    sheet["F10"] = 61000
    return _save(workbook)


def multi_sheet_order_bytes() -> bytes:
    """Sheet1 为封面说明（无订单表头），Sheet2 才是订单明细。"""
    workbook = Workbook()
    cover = workbook.active
    cover.title = "说明"
    cover["A1"] = "资料封面"
    cover["A2"] = "整理日期：2026-09-05"
    cover["A3"] = "说明：本工作簿含订单明细"
    sheet = workbook.create_sheet("订单明细")
    sheet["A1"] = "业务订单"
    sheet["A2"] = "采购订单号："
    sheet["B2"] = ORDER_NO
    sheet["A3"] = "客户："
    sheet["B3"] = CUSTOMER
    sheet["A5"] = "订购日期："
    sheet["B5"] = datetime(2026, 9, 1)
    for cell, text in {
        "A7": "产品图片", "B7": "系统型号", "C7": "名称/材质要求", "D7": "规格",
        "E7": "采购数量", "F7": "销售价", "G7": "装箱数量", "H7": "件数", "I7": "金额",
    }.items():
        sheet[cell] = text
    sheet["B8"] = "无"
    sheet["C8"] = "测试线材 型号V1 OD5.0"
    sheet["D8"] = "15M"
    sheet["E8"] = 500
    sheet["F8"] = 60
    sheet["G8"] = 12
    sheet["I8"] = 30000
    sheet["B9"] = "无"
    sheet["D9"] = "20M"
    sheet["E9"] = 500
    sheet["F9"] = 62
    sheet["G9"] = 12
    sheet["I9"] = 31000
    sheet["H10"] = "合计"
    sheet["I10"] = 61000
    return _save(workbook)


def hidden_and_total_interference_bytes() -> bytes:
    """隐藏行 / 隐藏列 / 空行 / 合并备注文本 / 总计行：都不得变成订单行。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet["A1"] = "采购订单号："
    sheet["B1"] = ORDER_NO
    sheet["A2"] = "客户："
    sheet["B2"] = CUSTOMER
    sheet["A3"] = "订购日期："
    sheet["B3"] = datetime(2026, 9, 1)
    for cell, text in {
        "A5": "产品型号", "B5": "名称", "C5": "规格", "D5": "数量", "E5": "单价", "F5": "金额",
    }.items():
        sheet[cell] = text
    sheet["A6"] = "无"
    sheet["B6"] = "线材A 1.0M"
    sheet["C6"] = "1M"
    sheet["D6"] = 100
    sheet["E6"] = 5
    sheet["F6"] = 500
    sheet["A7"] = "无"
    sheet["B7"] = "线材B 2.0M"
    sheet["C7"] = "2M"
    sheet["D7"] = 200
    sheet["E7"] = 6
    sheet["F7"] = 1200
    sheet["A9"] = "无"
    sheet["B9"] = "线材C 3.0M"
    sheet["C9"] = "3M"
    sheet["D9"] = 300
    sheet["E9"] = 7
    sheet["F9"] = 2100
    sheet["B10"] = "总计"
    sheet["F10"] = 2600
    sheet.merge_cells("A11:C11")
    sheet["A11"] = "付款安排请见合同附件"
    sheet.row_dimensions[7].hidden = True
    sheet.column_dimensions["B"].hidden = True
    return _save(workbook)


def missing_field_total_only_bytes() -> bytes:
    """表头齐全但没有任何明细行（只有合计行与空行）的缺字段样本。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet["A1"] = "采购订单号："
    sheet["B1"] = ORDER_NO
    sheet["A2"] = "客户："
    sheet["B2"] = CUSTOMER
    sheet["A3"] = "订购日期："
    sheet["B3"] = datetime(2026, 9, 1)
    for cell, text in {
        "A5": "产品型号", "B5": "名称", "C5": "数量", "D5": "金额",
    }.items():
        sheet[cell] = text
    sheet["B6"] = "合计"
    sheet["D6"] = 5000
    return _save(workbook)


def device_register_bytes() -> bytes:
    """设备台账：有数量但无订单块事实/无价格，不是订单。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "设备"
    sheet["A1"] = "设备台账"
    sheet.append(["设备编号", "设备名称", "数量", "存放位置", "负责人"])
    sheet.append(["DEV-01", "注塑机", 1, "A区", "张三"])
    sheet.append(["DEV-02", "冲床", 2, "B区", "李四"])
    return _save(workbook)


def coordinate_legacy_bytes() -> bytes:
    """老式坐标模板（P6 订单号 / X7 交期 / 第 10 行起明细）——v2 无表头证据时
    由坐标解析在 Tool 内部兜底，保证旧模板不回归。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "1"
    sheet["P6"] = "PO-LEGACY-001"
    sheet["X7"] = "2026-09-12"
    sheet["E10"], sheet["I10"], sheet["J10"], sheet["R10"] = 1, "W-H909", "高清线", 4000
    sheet["E11"], sheet["I11"], sheet["J11"], sheet["R11"] = 2, "W-H910", "高清线2", 2000
    return _save(workbook)


# ---------------------------------------------------------------------------
# 解析器：内容结构识别（magic bytes / 表头 / 价格信号）
# ---------------------------------------------------------------------------


def test_sniff_xlsx_bytes_is_content_based():
    assert sniff_xlsx_bytes(real_order_twin_bytes()) is True
    assert sniff_xlsx_bytes(inventory_like_bytes()) is True
    assert sniff_xlsx_bytes(b"%PDF-1.4 fake") is False
    assert sniff_xlsx_bytes(b"") is False
    assert sniff_xlsx_bytes(None) is False


def test_inventory_and_device_workbooks_are_not_order_candidates():
    assert workbook_parse_candidate("库存登记.xlsx", inventory_like_bytes()) is None
    assert workbook_parse_candidate("设备台账.xlsx", device_register_bytes()) is None
    assert workbook_looks_like_order(inventory_like_bytes(), "库存登记.xlsx") is False
    assert workbook_looks_like_order(device_register_bytes(), "设备台账.xlsx") is False
    # 真实订单同构布局在任何文件名下都是订单候选（不依赖文件名）。
    assert workbook_parse_candidate("数据文件.bin", real_order_twin_bytes()) is not None
    assert workbook_looks_like_order(real_order_twin_bytes(), "库存登记-2026.xlsx") is True


# ---------------------------------------------------------------------------
# 解析器：列顺序 / 表头别名 / 多 Sheet / 隐藏行列 / 合并 / 合计行
# ---------------------------------------------------------------------------


def test_column_order_and_alias_variants_produce_identical_order_facts():
    expected = _canonical_facts(parse_order_document("订单-同构-回放.xlsx", real_order_twin_bytes())["document"])
    assert expected["order_id"] == ORDER_NO
    assert expected["order_date"] == "2026-09-01"
    assert expected["customer_name"] == CUSTOMER
    assert expected["line_count"] == 2
    assert expected["missing_codes"] == 2
    assert "MISSING_PRODUCT_CODE" in expected["issue_codes"]

    for name, raw in (
        ("订单-列序别名A.xlsx", variant_reordered_alias_bytes()),
        ("订单-列序别名B.xlsx", variant_merged_prefix_bytes()),
    ):
        doc = parse_order_document(name, raw)["document"]
        assert _canonical_facts(doc) == expected, name


def test_multi_sheet_order_table_is_found_and_cover_sheet_ignored():
    doc = parse_order_document("订单-多Sheet.xlsx", multi_sheet_order_bytes())["document"]
    assert doc["sheet_count"] == 2
    assert [item.get("status") for item in doc["sheet_docs"]] == ["no_header", "header_found"]
    assert doc["order_id"] == ORDER_NO
    assert doc["line_count"] == 2
    assert [line.get("sheet") for line in doc["lines"]] == ["订单明细", "订单明细"]
    assert [line.get("row") for line in doc["lines"]] == [8, 9]


def test_hidden_rows_hidden_cols_total_and_note_rows_never_become_lines():
    doc = parse_order_document("订单-结构干扰.xlsx", hidden_and_total_interference_bytes())["document"]
    lines = doc["lines"]
    assert doc["line_count"] == 2
    assert [line.get("row") for line in lines] == [6, 9]  # 隐藏行 7 / 总计行 10 / 备注 11 不入行
    assert [line.get("quantity") for line in lines] == [100.0, 300.0]
    assert [line.get("amount") for line in lines] == [500.0, 2100.0]
    assert doc["total_quantity"] == 400.0
    assert doc["total_amount"] == 2600.0
    assert any(item.get("code") == "MISSING_PRODUCT_CODE" for item in doc["validation_issues"])


def test_missing_field_total_only_sample_is_not_empty_success():
    doc = parse_order_document("订单-缺明细.xlsx", missing_field_total_only_bytes())["document"]
    assert doc["order_id"] == ORDER_NO
    assert doc["line_count"] == 0
    assert (doc["lines"] or []) == []
    assert doc["total_amount"] == 0.0
    # 解析层仍给出结构证据（sheet_docs / 表头），并显式标记缺行，不是空成功。
    assert any(item.get("status") == "header_found" for item in doc["sheet_docs"])


# ---------------------------------------------------------------------------
# ingest_document 本地 Tool（fixture handler）：跨文件名 / 跨租户一致
# ---------------------------------------------------------------------------


def _file_payload(raw: bytes, filename: str) -> dict:
    return {
        "file": {
            "filename": filename,
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content_b64": base64.b64encode(raw).decode(),
        },
    }


@pytest.mark.asyncio
async def test_local_ingest_tool_consistent_across_filenames_and_tenants():
    registry = build_default_registry()
    raw = real_order_twin_bytes()
    observed: list[tuple[str, str, float]] = []
    for filename, tenant in (
        ("订单-华东仓-20260905.xlsx", "TENANT-A"),
        ("采购单.dat", "TENANT-B"),        # 无扩展名/伪装名：按内容结构解析
        ("inventory-mislabel.xls", "TENANT-C"),  # 错误扩展名 .xls：仍按 XLSX 内容解析
    ):
        result = await registry.call(
            "ingest_document", _file_payload(raw, filename), {"task_id": "T-1", "tenant_id": tenant}
        )
        assert result["provider"] == "local"  # 真实本地解析（provider=local，见 rows-S2 第 1 行）
        assert result["status"] == "needs_review"  # 缺编码/交期 -> review，不空成功
        assert result["needs_review"] is True
        document = result["document"]
        assert document["schema_version"] == "m1.document.v2"
        assert document["header"]["order_id"] == ORDER_NO
        assert document["header"]["quantity"] == 1000.0
        assert len(document["lines"]) == 2
        assert result["parser"]["name"] == "order.parser.v2"
        assert any(item.get("code") == "MISSING_FIELD" for item in document["validation_issues"])
        observed.append((document["header"]["order_id"], document["source"]["sha256"], document["header"]["quantity"]))
    assert len({item for item in observed}) == 1  # 完全一致，与文件名/租户无关


@pytest.mark.asyncio
async def test_local_ingest_tool_fails_closed_for_non_order_tables():
    for name, raw in (("库存表.xlsx", inventory_like_bytes()), ("设备台账.xlsx", device_register_bytes())):
        result = await m1_parse(_file_payload(raw, name), {"task_id": "T-1", "tenant_id": "TENANT-1"})
        assert result["status"] == "failed"
        assert result["code"] == "LOCAL_FIXTURE_UNSUPPORTED_FORMAT"
        assert result["document"] is None
        assert result["needs_review"] is False  # 非订单文档不得伪装成成功/进入 review


@pytest.mark.asyncio
async def test_local_ingest_tool_keeps_legacy_coordinate_template_fallback():
    """旧坐标模板（P6/X7/行10+）在 Tool 内部走坐标兜底，不依赖编排层预解析。"""
    result = await m1_parse(
        _file_payload(coordinate_legacy_bytes(), "旧模板-订单.xlsx"),
        {"task_id": "T-1", "tenant_id": "TENANT-1"},
    )
    assert result["status"] == "done"
    assert result["parser"]["name"] == "order.workbook.coordinates.v1"
    assert result["document"]["header"]["order_id"] == "PO-LEGACY-001"
    assert result["document"]["header"]["quantity"] == 6000.0
    assert len(result["document"]["lines"]) == 2


@pytest.mark.asyncio
async def test_local_ingest_tool_missing_field_sample_opens_review_not_fake_success():
    result = await m1_parse(
        _file_payload(missing_field_total_only_bytes(), "订单-缺明细.xlsx"),
        {"task_id": "T-1", "tenant_id": "TENANT-1"},
    )
    assert result["status"] == "needs_review"
    assert result["document"]["lines"] == []
    assert any(item.get("code") == "MISSING_FIELD" for item in result["document"]["validation_issues"])


# ---------------------------------------------------------------------------
# M1 HTTP adapter：补充候选不依赖文件名/租户；非订单表格不伪造补充
# ---------------------------------------------------------------------------

EXTERNAL_ORDER_GAP = {
    "task_id": "task-gap",
    "status": "needs_review",
    "processing_stage": "review",
    "needs_review": True,
    "overall_confidence": 0.3,
    "doc_type": "order",
    "document_subtype": "customer_purchase_order",
    "document_schema_version": "m1.document.v2",
    "document": {
        "schema_version": "m1.document.v2",
        "document_type": "order",
        "document_subtype": "customer_purchase_order",
        "source": {"original_filename": "外部-订单.pdf", "sha256": "ab" * 32},
        "header": {"order_number": None, "order_id": None, "doc_date": None, "title": "业务订单"},
        "lines": [],
        "totals": {},
        "field_meta": {},
        "validation_issues": [{"code": "LOW_CONFIDENCE", "message": "解析置信度不足"}],
    },
}


@pytest.mark.asyncio
async def test_adapter_supplement_independent_of_filename_and_tenant(monkeypatch):
    backend = Backend()
    backend.route("POST", "/ingest/sync", EXTERNAL_ORDER_GAP, repeat=True)
    install(monkeypatch, backend)
    registry = build_default_registry()
    bind_m1_http(registry, urls={"m1": BASE}, overwrite=True)
    raw = real_order_twin_bytes()
    supplements = []
    for filename, tenant in (("attachment.bin", "TENANT-A"), ("库存登记-伪装.xlsx", "TENANT-B")):
        result = await registry.call(
            "ingest_document", _file_payload(raw, filename), {"task_id": "T-1", "tenant_id": tenant}
        )
        assert result["document"]["lines"] == []  # 外部原结果保留
        supplement = result["semantic_supplement"]
        assert result["needs_review"] is True
        assert supplement["document"]["header"]["order_number"] == ORDER_NO
        assert len(supplement["document"]["lines"]) == 2
        assert any(missing["field"] == "line.product_code" for missing in supplement["missing_fields"])
        supplements.append(supplement["document"]["header"]["order_number"])
    assert supplements == [ORDER_NO, ORDER_NO]


@pytest.mark.asyncio
async def test_adapter_never_supplements_inventory_workbook(monkeypatch):
    backend = Backend()
    backend.route("POST", "/ingest/sync", EXTERNAL_ORDER_GAP, repeat=True)
    install(monkeypatch, backend)
    registry = build_default_registry()
    bind_m1_http(registry, urls={"m1": BASE}, overwrite=True)
    result = await registry.call(
        "ingest_document", _file_payload(inventory_like_bytes(), "库存登记.xlsx"),
        {"task_id": "T-1", "tenant_id": "TENANT-1"},
    )
    assert "semantic_supplement" not in result
    assert result["document"]["lines"] == []


# ---------------------------------------------------------------------------
# business_catalog：同一文件在不同根路径/文件名下分类一致；库存反例不被改判
# ---------------------------------------------------------------------------


def test_business_catalog_order_reclassification_consistent_across_paths_and_names(tmp_path):
    raw = real_order_twin_bytes()
    seen: list[tuple[str, str, int]] = []
    for index, name in enumerate(("库存登记-2026-09-01.xlsx", "上传数据-批量3.xlsx", "order-export.xlsx")):
        root = tmp_path / f"root-{index}"
        root.mkdir()
        path = root / name
        path.write_bytes(raw)
        extracted = extract_file(path, root=root, parse_xlsx=True, deep_limit_bytes=12_000_000)
        assert extracted["file_kind"] == "order", name
        order_document = extracted["extraction"].get("order_document") or {}
        assert order_document.get("order_id") == ORDER_NO
        seen.append((extracted["file_kind"], order_document.get("order_id"), len(order_document.get("lines") or [])))
    assert len({item for item in seen}) == 1


def test_business_catalog_never_reclassifies_inventory_or_device_tables(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for name, raw in (("库存登记-2026-09-01.xlsx", inventory_like_bytes()), ("库存登记-2026-09-01.xlsx", device_register_bytes())):
        path = root / name
        path.write_bytes(raw)
        extracted = extract_file(path, root=root, parse_xlsx=True, deep_limit_bytes=12_000_000)
        assert extracted["file_kind"] == "inventory", name


def test_supplement_builder_uses_same_candidate_gate():
    assert build_semantic_supplement("库存登记.xlsx", inventory_like_bytes(), external=EXTERNAL_ORDER_GAP) is None
    supplement = build_semantic_supplement("order.xlsx", real_order_twin_bytes(), external=EXTERNAL_ORDER_GAP)
    assert supplement is not None
    assert supplement["document"]["header"]["order_number"] == ORDER_NO
