"""书二 §7.2：绑定状态表——路由目录只含已绑定工具（治痛点 5/7）。"""
import pytest

from yunpai_orchestrator.binding import (
    DEPRECATED_TOOLS,
    INTENTIONALLY_UNBOUND,
    ORCHESTRATION_INTERNAL,
    BindingStatus,
    CatalogView,
    compute_bindings,
    describe,
    visible_tool_names,
)
from yunpai_orchestrator.registry import build_default_registry


@pytest.fixture(scope="module")
def registry():
    return build_default_registry()


def test_119_specs_loaded(registry):
    assert len(registry.specs) == 119  # 115 manifest + 4 本地识别件套


def test_deprecated_and_unbound_never_visible(registry):
    visible = set(visible_tool_names(registry))
    assert not (DEPRECATED_TOOLS & visible)
    assert not (INTENTIONALLY_UNBOUND & visible)
    assert not (ORCHESTRATION_INTERNAL & visible)
    assert "run_mrp_procurement_plan" not in visible  # legacy 名不得再误导 LLM


def test_local_only_tools_bound_local(registry):
    bindings = compute_bindings(registry)
    for name in ("sample_file", "ingest_recognized", "query_recognized_table", "ingest_canonical"):
        assert registry.specs.get(name) is not None, f"{name} 必须有合同（local.json）"
        assert bindings[name] == BindingStatus.BOUND_LOCAL


def test_sandbox_marked_and_counted(registry):
    bindings = compute_bindings(registry)
    assert bindings["import_m4_purchase_suggestions_json"] == BindingStatus.SANDBOX
    summary = {k: len(v) for k, v in describe(registry).items()}
    assert summary["bound_local"] >= 30  # 31 本地 fixture（含 1 个 sandbox 拆出）


def test_catalog_view_only_exposes_visible(registry):
    visible = set(visible_tool_names(registry))
    view = CatalogView(registry)
    assert set(view.specs) == visible
    assert "report_workload" not in view.specs
