from yunpai_orchestrator.m2_fact_validation import validate_engineering_facts


def test_m2_fact_validation_reports_field_level_missing_facts():
    result = validate_engineering_facts(
        product_code="W-H909",
        bom_lines=[{"material_code": "MAT-1", "qty_per": 1, "uom": "PCS"}],
        bom_version="BOM-1",
        bom_effective_from="2026-01-01",
        route_steps=[{"operation_id": "OP-1", "standard_minutes": 5, "station": "S-1", "required_equipment_codes": ["EQ-1"]}],
        sop_version="SOP-1",
        sop_effective_from="2026-01-01",
    )
    assert result["status"] == "ready_for_approval"
    assert result["missing_fields"] == []


def test_m2_fact_validation_does_not_invent_standard_minutes_or_resources():
    result = validate_engineering_facts(
        product_code="W-H909",
        bom_lines=[{"material_code": "MAT-1", "qty_per": 1}],
        bom_version="BOM-1",
        route_steps=[{"operation_id": "OP-1"}],
        sop_version="SOP-1",
    )
    fields = {item["field"] for item in result["missing_fields"]}
    assert result["status"] == "incomplete"
    assert "bom_lines[1].uom" in fields
    assert "route_steps[1].standard_minutes" in fields
    assert "route_steps[1].station" in fields
    assert "route_steps[1].required_equipment_codes" in fields


def test_m2_fact_validation_flags_invalid_effective_range():
    result = validate_engineering_facts(
        product_code="P-1",
        bom_lines=[{"material_code": "M-1", "qty_per": 1, "uom": "PCS"}],
        bom_version="B-1",
        bom_effective_from="2026-09-10",
        bom_effective_to="2026-09-01",
        route_steps=[],
    )
    assert any(item["code"] == "INVALID_EFFECTIVE_RANGE" for item in result["validation_issues"])
