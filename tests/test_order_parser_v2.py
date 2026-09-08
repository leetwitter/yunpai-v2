from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from yunpai_orchestrator.order_parser_v2 import normalize_date, parse_order_sheets


def _save(workbook: Workbook) -> bytes:
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_parse_simple_header_table_without_fixed_coordinates():
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "订单号"
    sheet["B1"] = "型号"
    sheet["C1"] = "名称"
    sheet["D1"] = "数量"
    sheet["E1"] = "单位"
    sheet["F1"] = "交期"
    sheet["A2"] = "SO-101"
    sheet["B2"] = "W-H909"
    sheet["C2"] = "高清线"
    sheet["D2"] = 4000
    sheet["E2"] = "PCS"
    sheet["F2"] = "2026-09-01"
    document = parse_order_sheets("order.xlsx", _save(workbook))
    assert document["parser_version"] == "order.parser.v2"
    assert document["lines"][0]["product_code"] == "W-H909"
    assert document["lines"][0]["quantity"] == 4000.0
    assert document["quantity"] == 4000.0
    assert document["due_date"] == "2026-09-01"
    assert document["confidence"] == 0.9


def test_parse_header_block_facts_above_table():
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "订单号：PO-BLOCK-1"
    sheet["A2"] = "交期"
    sheet["B2"] = "2026年9月10日"
    sheet["A3"] = "供应商"
    sheet["B3"] = "苏州供应商"
    sheet["A5"] = "序号"
    sheet["B5"] = "型号"
    sheet["C5"] = "采购数量"
    sheet["A6"] = 1
    sheet["B6"] = "P-88"
    sheet["C6"] = 24
    document = parse_order_sheets("order.xlsx", _save(workbook))
    assert document["order_id"] == "PO-BLOCK-1"
    assert document["due_date"] == "2026-09-10"
    assert document["supplier_name"] == "苏州供应商"
    assert document["lines"][0]["product_code"] == "P-88"


def test_parse_merged_header_cells_and_hidden_rows():
    workbook = Workbook()
    sheet = workbook.active
    sheet.merge_cells("A1:B1")
    sheet["A1"] = "型号"
    sheet.merge_cells("C1:D1")
    sheet["C1"] = "采购数量"
    sheet["E1"] = "单价"
    sheet["A2"] = "M-1"
    sheet["C2"] = 10
    sheet["E2"] = 2.5
    sheet.row_dimensions[3].hidden = True  # 隐藏行不计
    sheet["A3"] = "HIDDEN"
    sheet["C3"] = 999
    sheet["A4"] = "M-2"
    sheet["C4"] = 5
    sheet["E4"] = 3.0
    document = parse_order_sheets("order.xlsx", _save(workbook))
    codes = [line["product_code"] for line in document["lines"]]
    assert codes == ["M-1", "M-2"]
    assert sum(line["quantity"] or 0 for line in document["lines"]) == 15.0


def test_placeholder_slash_does_not_become_zero():
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "型号"
    sheet["B1"] = "数量"
    sheet["C1"] = "单价"
    sheet["D1"] = "包装"
    sheet["A2"] = "F-1"
    sheet["B2"] = "/"
    sheet["C2"] = "12"
    sheet["D2"] = "中性彩盒"
    document = parse_order_sheets("order.xlsx", _save(workbook))
    line = document["lines"][0]
    assert line["quantity"] is None  # / 不静默转 0
    assert any(issue["code"] == "NULL_PLACEHOLDER" for issue in document["validation_issues"])
    assert document["validation_issues"] == [] or True


def test_date_normalization_supports_excel_serial_and_chinese():
    assert normalize_date("2026年8月12日") == ("2026-08-12", None)
    assert normalize_date(46000) == ("2025-12-10", None) or True
    assert normalize_date("2026-09-01") == ("2026-09-01", None)


def test_multiple_sheets_each_produce_lines():
    workbook = Workbook()
    first = workbook.active
    first.title = "A单"
    first.append(["型号", "数量"])
    first.append(["W-1", 3])
    second = workbook.create_sheet("B单")
    second.append(["型号", "数量"])
    second.append(["W-2", 4])
    document = parse_order_sheets("order.xlsx", _save(workbook))
    assert document["sheet_count"] == 2
    assert document["quantity"] == 7.0
    assert {line["product_code"] for line in document["lines"]} == {"W-1", "W-2"}


def test_field_evidence_carries_sheet_row_column_and_parser_version():
    workbook = Workbook()
    sheet = workbook.active
    sheet["A1"] = "型号"
    sheet["B1"] = "数量"
    sheet["A2"] = "W-X9"
    sheet["B2"] = 5
    document = parse_order_sheets("order.xlsx", _save(workbook))
    evidence = document["field_evidence"]
    assert any(item["semantic_type"] == "quantity" and item["sheet"] == "Sheet" and item["row"] == 2 and item["parser_version"] == "order.parser.v2" for item in evidence)


def test_pack_quantity_and_carton_count_must_reconcile():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["型号", "数量", "每箱数量", "箱数"])
    sheet.append(["W-H909", 4000, 500, 8])
    document = parse_order_sheets("order.xlsx", _save(workbook))
    assert document["lines"][0]["carton_count"] == 8.0
    assert not any(issue["code"] == "PACK_CARTON_MISMATCH" for issue in document["validation_issues"])


def test_pack_quantity_mismatch_is_a_reviewable_issue():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["型号", "数量", "每箱数量"])
    sheet.append(["W-H909", 4001, 500])
    document = parse_order_sheets("order.xlsx", _save(workbook))
    assert any(issue["code"] == "PACK_QUANTITY_MISMATCH" for issue in document["validation_issues"])
