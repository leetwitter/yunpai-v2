from hashlib import sha256
import json
from pathlib import Path

import pytest

from yunpai_orchestrator.contracts import ToolSpec
from yunpai_orchestrator.m1_tooling import M1_ADAPTER_TOOL_NAMES, M1_HTTP_ADAPTER_TOOL_NAMES, M1_TOOL_NAMES
from yunpai_orchestrator.m3_m4_tooling import M3_ADAPTER_TOOL_NAMES, M4_ADAPTER_TOOL_NAMES
from yunpai_orchestrator.registry import ToolRegistry, build_default_registry, build_runtime_registry


EXPECTED = {"m0": 32, "m1": 17, "m2": 7, "m3": 17, "m4": 26, "m5": 20}


def test_registry_loads_all_original_m0_m5_contracts():
    registry = build_default_registry()
    assert len(registry.specs) == 119
    assert {module: len(registry.tools_for(module)) for module in EXPECTED} == EXPECTED
    # 合并 main(M3/M4 adapter) + pmctooldev(M5 PMC v2) + M1 专用 adapter 后真实绑定：
    # m0 9 + m1 17 + m2 1 + m3 16 + m4 24 + m5 18 = 85；m3/m4 两个 receive_* 排除。
    # m0 新增 4 个本地识别工具（sample_file/ingest_recognized/query_recognized_table/ingest_canonical）。
    assert len(registry.handlers) == 85
    assert {"data_import_run", "data_import_status", "data_import_preview", "data_import_resolve", "data_import_commit"} <= set(registry.handlers)
    assert all(name in registry.handlers for name in M3_ADAPTER_TOOL_NAMES)
    assert all(name in registry.handlers for name in M4_ADAPTER_TOOL_NAMES)
    # All 17 M1 tools are bound in the default registry: ingest_document stays
    # on the local fixture handler, the remaining 16 use the M1 HTTP adapter.
    assert all(name in registry.handlers for name in M1_TOOL_NAMES)
    assert all(name in registry.handlers for name in M1_ADAPTER_TOOL_NAMES)
    assert "receive_m3_material_demand" not in registry.handlers
    assert "receive_m4_schedule_impact_proposal" not in registry.handlers
    assert "import_m4_purchase_suggestions" not in registry.handlers


def test_packaged_and_documented_manifests_are_identical():
    for module in EXPECTED:
        packaged = Path(f"src/yunpai_orchestrator/manifests/{module}.json").read_bytes()
        documented = Path(f"registry/tool-manifests/{module}.json").read_bytes()
        assert sha256(packaged).digest() == sha256(documented).digest()


def test_extracted_manifests_match_recorded_source_hashes():
    provenance = json.loads(Path("registry/SOURCE_PROVENANCE.json").read_text())
    for module, expected in provenance["manifests"].items():
        content = Path(f"registry/tool-manifests/{module}.json").read_bytes().replace(b"\r\n", b"\n")
        assert sha256(content).hexdigest() == expected


def test_duplicate_tool_fails_fast():
    registry = ToolRegistry()
    spec = ToolSpec("x", "m0", "x", {"type": "object"}, {"type": "object"})
    registry.register(spec)
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(spec)


def test_invalid_output_schema_fails_fast():
    registry = ToolRegistry()
    spec = ToolSpec("x", "m0", "x", {"type": "object"}, {"type": "not-a-json-schema-type"})
    with pytest.raises(ValueError):
        registry.register(spec)


def test_manifest_contract_has_complete_http_metadata():
    registry = ToolRegistry()
    registry.load_manifests("registry/tool-manifests")
    spec = registry.specs["solve_scheduling"]
    assert spec.path == "/api/v1/schedule-candidates"
    assert spec.method == "POST"
    assert spec.timeout_s == 120
    assert spec.input_schema["required"][:3] == ["idempotency_key", "scenario_id", "planning_start"]


def test_catalog_reports_bound_state():
    registry = build_default_registry()
    catalog = {item["name"]: item for item in registry.catalog()}
    assert catalog["solve_scheduling"]["bound"] is True
    assert catalog["list_m4_tracking"]["bound"] is True
    assert catalog["receive_m4_schedule_impact_proposal"]["bound"] is False
    assert catalog["query_m4_material_supply_snapshot"]["http"]["required_headers"] == [
        "Authorization",
        "X-Yunpai-Task-ID",
        "Idempotency-Key",
    ]


def test_full_http_runtime_keeps_missing_receivers_unbound(monkeypatch):
    monkeypatch.setenv("YUNPAI_TOOL_TRANSPORT", "http")
    registry = build_runtime_registry()
    assert len(registry.handlers) == 117
    assert "receive_m3_material_demand" not in registry.handlers
    assert "receive_m4_schedule_impact_proposal" not in registry.handlers


def test_http_module_selection_restores_local_handlers_for_unselected_modules(monkeypatch):
    monkeypatch.setenv("YUNPAI_TOOL_TRANSPORT", "http")
    monkeypatch.setenv("YUNPAI_HTTP_MODULES", "m0,m1,m4,m5")
    registry = build_runtime_registry()
    # M2/M3 are intentionally local for the order replay.
    assert registry.handlers["run_bom_sop_workflow"].__module__.endswith("workers")
    assert registry.handlers["run_m3_procurement_requirements"].__module__.endswith("workers")
    assert registry.handlers["import_m4_purchase_suggestions_json"].__module__.endswith("registry")


def test_http_runtime_can_keep_m4_business_tools_local(monkeypatch):
    monkeypatch.setenv("YUNPAI_TOOL_TRANSPORT", "http")
    monkeypatch.setenv("YUNPAI_HTTP_MODULES", "m0,m1,m2,m3,m4,m5")
    monkeypatch.setenv("YUNPAI_LOCAL_M4", "true")
    registry = build_runtime_registry()
    handler = registry.handlers["import_m4_purchase_suggestions_json"]
    # M4 本地实现已由 workers.m4_purchase（旧 echo）替换为 m4_purchase_local
    # （rows-S5.md:67 改造后搬）；断言口径 = 「仍是本地 handler，未被 HTTP 覆盖」。
    assert handler.__module__.endswith(("workers", "m4_purchase_local"))
    assert registry.environment["local_modules"] == ["m4"]
