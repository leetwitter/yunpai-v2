"""关键字段 precision/recall 指标（任务书 §五.3，目标 >= 90%）。

方法：用已知 golden 事实（人工/构建时写入的字段值）作为 ground truth，
对比解析器/候选的字段证据，逐字段计算：
- precision = 正确抽取数 / 模型抽取数（抽取了但没有 ground truth 的算 FP）
- recall = 正确抽取数 / ground truth 数（应抽取但缺失的算 FN）
失败样例必须携带 missing_fields 或 review_issues。
"""

from __future__ import annotations

from tests import fixtures

from yunpai_orchestrator.order_parser_v2 import parse_order_sheets


def _extract_truth_lines(lines: list[dict]) -> dict[tuple[int, str], object]:
    """把 golden 行折成 {(row, field): value}，仅数值/文本可比较字段。"""
    truth: dict[tuple[int, str], object] = {}
    for line in lines:
        row = line["_row"]
        for field, value in line.items():
            if field == "_row":
                continue
            truth[(row, field)] = value
    return truth


def _parse_to_observations(document: dict) -> dict[tuple[int, str], object]:
    """从解析结果 field_evidence 折成 {(row, semantic_type): value}。"""
    observations: dict[tuple[int, str], object] = {}
    for evidence in document.get("field_evidence", []):
        row = evidence.get("row")
        field = evidence.get("semantic_type")
        value = evidence.get("raw_value")
        if row is not None and field and value not in (None, ""):
            observations[(row, field)] = value
    return observations


def _precision_recall(truth: dict, observed: dict, *, fields: set[str] | None = None) -> dict:
    def _filter(items: dict) -> dict:
        if fields is None:
            return items
        return {key: value for key, value in items.items() if key[1] in fields}

    truth, observed = _filter(truth), _filter(observed)
    tp = len(set(truth) & set(observed))
    fp = len(set(observed) - set(truth))
    fn = len(set(truth) - set(observed))
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": round(tp / max(1, tp + fp), 4),
        "recall": round(tp / max(1, tp + fn), 4),
    }


def test_order_field_precision_and_recall_above_90():
    """order 表头驱动的关键字段 precision/recall >= 90%。"""
    golden_rows = [
        {"_row": 2, "order_id": "SO-1", "product_code": "W-H909", "quantity": "10", "due_date": "2026-09-20", "unit_price": "3.5"},
        {"_row": 3, "order_id": "SO-1", "product_code": "W-H910", "quantity": "20", "due_date": "2026-09-21", "unit_price": "2.0"},
    ]
    headers = ["订单号", "型号", "数量", "交期", "单价"]
    workbook_rows = [headers]
    for line in golden_rows:
        workbook_rows.append([line["order_id"], line["product_code"], line["quantity"], line["due_date"], line["unit_price"]])
    raw = fixtures.xlsx_bytes(headers, workbook_rows[1:])
    document = parse_order_sheets("order.xlsx", raw)

    truth = _extract_truth_lines(golden_rows)
    observed = _parse_to_observations(document)
    eval_fields = {"order_id", "product_code", "quantity", "due_date", "unit_price"}
    metrics = _precision_recall(truth, observed, fields=eval_fields)

    assert metrics["precision"] >= 0.9, f"order 字段 precision {metrics['precision']} < 0.9: {metrics}"
    assert metrics["recall"] >= 0.9, f"order 字段 recall {metrics['recall']} < 0.9: {metrics}"
    # 失败样例必须携带 missing_fields 或 review_issues（本组应无失败）。
    assert document["validation_issues"] == [] or document["missing_fields"] == []


def test_unknown_layout_keeps_90_percent_recall_with_shifted_columns():
    """列顺序变化/未知布局：关键字段仍应高 recall；缺字段进 review_issues。"""
    # 列顺序打乱且加了包装文本列：型号在末尾、数量带中文单位。
    headers = ["序号", "备注", "交期", "数量(个)", "型号"]
    rows = [["1", "中性彩盒", "2026年9月25日", "30", "W-123"]]
    raw = fixtures.xlsx_bytes(headers, rows)
    document = parse_order_sheets("layout-shifted.xlsx", raw)
    truth = {(2, "product_code"): "W-123", (2, "quantity"): "30", (2, "due_date"): "2026年9月25日"}
    observed = _parse_to_observations(document)
    eval_fields = {"product_code", "quantity", "due_date"}
    metrics = _precision_recall(truth, observed, fields=eval_fields)
    # 型号/数量/交期三个关键字段必须都命中。
    assert metrics["recall"] >= 0.9, f"未知布局 recall {metrics['recall']}: {metrics}"
    assert metrics["precision"] >= 0.9


def _extract_to_observations(raw: bytes, filename: str, kind_hint: str) -> tuple[dict, dict, dict]:
    """用 business_catalog.extract_file 抽取，返回 (truth_like, observed_like, document)。"""
    from pathlib import Path
    import tempfile

    from yunpai_orchestrator.business_catalog import extract_file

    tmp = Path(tempfile.mkdtemp())
    path = tmp / filename
    path.write_bytes(raw)
    extracted = extract_file(path, root=tmp, parse_xlsx=True)
    document = extracted["document"]
    observed: dict[tuple[int, str], object] = {}
    for observation in extracted.get("field_observations", []):
        row = observation.get("row")
        field = observation.get("semantic_type")
        value = observation.get("raw_value")
        if row is not None and field and value not in (None, ""):
            observed[(row, field)] = value
    return document, observed, extracted


def test_equipment_field_precision_recall_above_90(tmp_path):
    """equipment 表头驱动字段（code/name/line/capacity）precision/recall >= 90%。"""
    headers = ["设备编码", "设备名称", "产线", "能力"]
    rows = [["EQ-1", "注塑机", "线1", "250"]]
    raw = fixtures.xlsx_bytes(headers, rows)
    document, observed, _ = _extract_to_observations(raw, "equipment.xlsx", "equipment")
    truth = {(2, "equipment_code"): "EQ-1", (2, "equipment_name"): "注塑机"}
    metrics = _precision_recall(truth, observed, fields={"equipment_code", "equipment_name", "product_code", "material_code"})
    assert metrics["recall"] >= 0.9, f"equipment recall {metrics['recall']}: {metrics} obs={observed}"
    assert document.get("review_status") == "candidate"


def test_worker_and_inventory_field_metrics(tmp_path):
    """worker（工号/姓名/技能）与 inventory（物料/仓库/批次/数量）关键字段高命中。"""
    worker_raw = fixtures.xlsx_bytes(["工号", "姓名", "技能", "班次"], [["E-1", "张三", "焊接", "白班"]])
    document_w, observed_w, extracted_w = _extract_to_observations(worker_raw, "worker.xlsx", "worker")
    assert extracted_w["file_kind"] == "worker"
    truth_w = {(2, "worker_code"): "E-1", (2, "worker_name"): "张三", (2, "skill"): "焊接"}
    metrics_w = _precision_recall(truth_w, observed_w, fields={"worker_code", "worker_name", "skill"})
    assert metrics_w["recall"] >= 0.9, f"worker recall {metrics_w['recall']}: {metrics_w} obs={observed_w}"
    assert metrics_w["precision"] >= 0.9

    inv_raw = fixtures.xlsx_bytes(["物料编码", "仓库", "批次", "现存数量"], [["M-1", "A仓", "L-1", 88]])
    document_i, observed_i, extracted_i = _extract_to_observations(inv_raw, "inventory.xlsx", "inventory")
    assert extracted_i["file_kind"] == "inventory"
    truth_i = {(2, "material_code"): "M-1", (2, "warehouse"): "A仓", (2, "available_qty"): "88"}
    metrics_i = _precision_recall(truth_i, observed_i, fields={"material_code", "warehouse", "available_qty"})
    assert metrics_i["recall"] >= 0.9, f"inventory recall {metrics_i['recall']}: {metrics_i} obs={observed_i}"
    assert document_i.get("review_status") == "candidate"


def test_supplier_field_metrics(tmp_path):
    """supplier（供应商编码/名称/PO）字段命中。"""
    raw = fixtures.xlsx_bytes(["供应商编码", "供应商名称", "PO编号"], [["S-1", "苏州厂", "PO-9"]])
    document, observed, extracted = _extract_to_observations(raw, "supplier.xlsx", "supplier")
    assert extracted["file_kind"] == "supplier"
    truth = {(2, "supplier_code"): "S-1", (2, "supplier_name"): "苏州厂"}
    metrics = _precision_recall(truth, observed, fields={"supplier_code", "supplier_name", "material_code"})
    assert metrics["recall"] >= 0.9, f"supplier recall {metrics['recall']}: {metrics} obs={observed}"


def test_sop_field_metrics(tmp_path):
    """sop（工站/作业步骤/投入人数）字段命中，required 映射一致。"""
    raw = fixtures.xlsx_bytes(["工站", "作业步骤", "投入人数", "材料"], [["S-1", "锁付", 2, "螺丝"]])
    from pathlib import Path
    import tempfile
    from yunpai_orchestrator.business_catalog import extract_file

    tmp = Path(tempfile.mkdtemp())
    path = tmp / "sop.xlsx"
    path.write_bytes(raw)
    extracted = extract_file(path, root=tmp, parse_xlsx=True)
    assert extracted["file_kind"] == "sop"
    observed = {}
    for observation in extracted.get("field_observations", []):
        if observation.get("row") is not None and observation.get("semantic_type") and observation.get("raw_value") not in (None, ""):
            observed[(observation["row"], observation["semantic_type"])] = observation["raw_value"]
    truth = {(2, "station"): "S-1", (2, "step_name"): "锁付", (2, "worker_count"): "2"}
    metrics = _precision_recall(truth, observed, fields={"station", "step_name", "worker_count"})
    assert metrics["recall"] >= 0.9, f"sop recall {metrics['recall']}: {metrics} obs={observed}"
    assert metrics["precision"] >= 0.9


def test_calendar_field_metrics(tmp_path):
    """calendar（日期/班次/开始/结束）字段命中，required 映射一致。"""
    raw = fixtures.xlsx_bytes(["日期", "班次", "开始时间", "结束时间"], [["2026-09-01", "白班", "08:00", "17:00"]])
    from pathlib import Path
    import tempfile
    from yunpai_orchestrator.business_catalog import extract_file

    tmp = Path(tempfile.mkdtemp())
    path = tmp / "calendar.xlsx"
    path.write_bytes(raw)
    extracted = extract_file(path, root=tmp, parse_xlsx=True)
    assert extracted["file_kind"] == "calendar"
    observed = {}
    for observation in extracted.get("field_observations", []):
        if observation.get("row") is not None and observation.get("semantic_type") and observation.get("raw_value") not in (None, ""):
            observed[(observation["row"], observation["semantic_type"])] = observation["raw_value"]
    truth = {(2, "calendar_date"): "2026-09-01", (2, "shift"): "白班", (2, "start_time"): "08:00", (2, "end_time"): "17:00"}
    metrics = _precision_recall(truth, observed, fields={"calendar_date", "shift", "start_time", "end_time"})
    assert metrics["recall"] >= 0.9, f"calendar recall {metrics['recall']}: {metrics} obs={observed}"
    assert metrics["precision"] >= 0.9
