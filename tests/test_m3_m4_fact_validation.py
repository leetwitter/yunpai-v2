from yunpai_orchestrator.m3_m4_fact_validation import validate_inventory_facts, validate_supplier_facts


def test_inventory_validation_requires_traceable_stock_facts():
    result = validate_inventory_facts(
        [{"material_code": "MAT-1", "available_qty": 2, "warehouse": "WH-1", "lot_no": "LOT-1", "qc_status": "released", "observed_at": "2026-09-05"}],
        ["MAT-1"],
    )
    assert result["status"] == "ready"


def test_inventory_validation_reports_missing_material_and_provenance():
    result = validate_inventory_facts([{"material_code": "MAT-1", "available_qty": 2}], ["MAT-1", "MAT-2"])
    fields = {item["field"] for item in result["missing_fields"]}
    assert result["status"] == "incomplete"
    assert "inventory[1].warehouse" in fields
    assert "inventory[MAT-2]" in fields


def test_supplier_validation_requires_each_shortage_material():
    result = validate_supplier_facts({"MAT-1": "SUP-1"}, ["MAT-1", "MAT-2"])
    assert result["status"] == "incomplete"
    assert result["missing_fields"][0]["field"] == "supplier_by_material[MAT-2]"


def test_supplier_validation_flags_late_eta():
    result = validate_supplier_facts(
        {"MAT-1": {"supplier_code": "SUP-1", "eta": "2026-09-20", "confirmed_due_date": "2026-09-10"}},
        ["MAT-1"],
    )
    assert result["status"] == "incomplete"
    assert result["validation_issues"][0]["code"] == "ETA_AFTER_REQUIRED_DATE"
