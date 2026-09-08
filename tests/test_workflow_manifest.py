from hashlib import sha256
from pathlib import Path

from yunpai_orchestrator.workflow_registry import load_workflow


def test_workflow_is_versioned_ordered_and_packaged():
    workflow = load_workflow("m1_m5_document_to_plan")
    assert workflow["workflow_id"] == "m1_m5_document_to_plan"
    assert workflow["version"] == "1.0.0"
    assert [step["module"] for step in workflow["steps"]] == [
        "m1", "m0", "m0", "m2", "m3", "m4", "m5", "m5", "m5",
    ]
    assert [step["gate"] for step in workflow["steps"] if step["gate"]] == [
        "low_confidence_or_missing_fields", "candidate", "engineering_approval",
        "blocked_input_or_material_match", "missing_supplier_or_eta", "schedule_apply",
    ]
    packaged = Path("src/yunpai_orchestrator/workflows/m1_m5_document_to_plan.json").read_bytes()
    documented = Path("workflows/m1_m5_document_to_plan.json").read_bytes()
    assert sha256(packaged).digest() == sha256(documented).digest()
