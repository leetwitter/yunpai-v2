from __future__ import annotations

import re
from pathlib import Path

import pytest

from yunpai_orchestrator.agents import SKILL_USAGE_ORDER
from yunpai_orchestrator.llm import QwenConfig, QwenRouter
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.skills import SkillRegistry, build_default_skill_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"


def _frontmatter_name(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^---\n(.*?)\n---", text, flags=re.DOTALL)
    assert match, f"SKILL.md 缺少 frontmatter: {path}"
    name = re.search(r"^name:\s*(.+)$", match.group(1), flags=re.MULTILINE)
    assert name, f"SKILL.md frontmatter 缺少 name: {path}"
    return name.group(1).strip()


def _op_map_tools(skill_name: str, skills: SkillRegistry) -> set[str]:
    """M5-style skills dispatch through operation_map; every mapped tool must
    be visible to validate_tools/catalog so there is no silent whitelist."""
    module_skill_ops = {
        "yunpai-m0-data-foundation": {"default": "data_import_run", "ingest": "data_import_run", "preview": "data_import_preview", "resolve": "data_import_resolve", "commit": "data_import_commit"},
        "yunpai-m1-document-parser": {"default": "ingest_document", "parse": "ingest_document", "review": "submit_m1_review", "report": "generate_m1_report"},
        "yunpai-m2-bom-sop": {"default": "run_bom_sop_workflow", "generate": "run_bom_sop_workflow", "history": "search_m2_bom_history", "bom": "generate_m2_bom_controlled", "sop": "generate_m2_sop", "run": "get_m2_run"},
        "yunpai-m3-material-planning": {"default": "run_m3_procurement_requirements", "mrp": "run_m3_procurement_requirements", "readiness": "get_material_readiness_snapshot", "readiness_summary": "get_material_readiness", "plan": "get_m3_procurement_plan", "handoff": "export_m3_procurement_suggestions"},
        "yunpai-m4-procurement": {"default": "import_m4_purchase_suggestions_json", "import": "import_m4_purchase_suggestions_json", "orders": "list_m4_purchase_orders", "tracking": "list_m4_tracking", "alerts": "list_m4_purchase_alerts", "supply": "query_m4_material_supply_snapshot", "supplier_reply": "parse_m4_supplier_reply"},
        "yunpai-m5-pmc": {"default": "solve_scheduling", "solve": "solve_scheduling", "schedule": "get_m5_schedule", "progress": "get_m5_pmc_progress", "contracts": "get_m5_integration_contracts", "readiness": "get_m5_material_readiness", "knowledge_search": "search_m5_knowledge", "knowledge_record": "record_m5_knowledge", "message_prepare": "prepare_m5_department_message", "message_get": "get_m5_department_message", "message_delivery": "get_m5_department_message_delivery", "advise": "advise_m5_schedule", "intelligent": "run_m5_intelligent_schedule", "procurement": "generate_m5_material_procurement_plan"},
        "yunpai-m5-pmc-lifecycle": {"default": "get_m5_schedule", "snapshot": "ingest_m5_planning_snapshot", "ingest": "ingest_m5_planning_snapshot", "schedule": "get_m5_schedule", "versions": "list_m5_schedules", "progress": "get_m5_pmc_progress", "replan": "replan_m5_schedule", "dispatch": "dispatch_m5_schedule", "execution": "get_m5_execution_summary"},
    }
    return set(module_skill_ops.get(skill_name, {}).values())


def test_skill_docs_frontmatter_matches_registry_names():
    registry = build_default_skill_registry()
    registered = set(registry.specs)
    for path in sorted(SKILLS_DIR.rglob("SKILL.md")):
        name = _frontmatter_name(path)
        # orchestrator is documentation-only and intentionally not registered.
        if name == "yunpai-orchestrator":
            continue
        assert name in registered, f"SKILL.md name 未注册: {path.name} -> {name}"


def test_every_registered_skill_has_doc_dir():
    registry = build_default_skill_registry()
    registered = set(registry.specs)
    docs = {_frontmatter_name(path) for path in SKILLS_DIR.rglob("SKILL.md")}
    assert registered <= docs | {"yunpai-orchestrator"}


def test_skill_tools_declared_match_operation_map():
    registry = build_default_skill_registry()
    tool_registry = build_default_registry()
    for name, spec in registry.specs.items():
        declared = set(spec.tools)
        op_map = _op_map_tools(name, registry)
        if op_map:
            # Every operation-mapped tool must be declared (no hidden whitelist).
            assert op_map <= declared, f"{name} op_map 工具未声明: {op_map - declared}"
        # Declared tools must exist in the ToolRegistry so validate_tools holds.
        assert declared <= set(tool_registry.specs), f"{name} 声明了未注册工具: {declared - set(tool_registry.specs)}"


def test_skill_usage_order_covers_all_ordered_skills():
    registry = build_default_skill_registry()
    assert set(SKILL_USAGE_ORDER) <= set(registry.specs)


@pytest.mark.asyncio
async def test_qwen_prompt_exposes_full_versioned_skill_catalog(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"intent":"x","route":"chat","tools":[],"confidence":0.5,"answer":"ok"}'}}]}

    class Client:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.update(url=url, body=kwargs["json"])
            return Response()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    router = QwenRouter(QwenConfig(api_key="test-key"))
    skills = build_default_skill_registry()
    router.skills = skills
    result = await router.classify({"message": "test"}, build_default_registry())
    assert result["ok"] is True
    prompt = captured["body"]["messages"][1]["content"]
    for spec in skills.specs.values():
        assert f"{spec.name}@{spec.version}" in prompt
    assert '"version"' in prompt


@pytest.mark.asyncio
async def test_planner_rejects_out_of_order_multi_skill_proposal():
    class FakeRouter:
        async def classify(self, request, registry):
            return {
                "ok": True,
                "status": "ok",
                "decision": {"intent": "先排程再识别", "route": "free", "tools": [], "skills": ["yunpai-m5-pmc", "business-data-identification"], "confidence": 0.6, "reason": "order test"},
                "model": {"provider": "qwen", "status": "ok"},
            }

    from yunpai_orchestrator.agents import PlannerAgent

    planner = PlannerAgent(FakeRouter())
    decision = await planner.aplan({"message": "测试顺序"}, build_default_registry())
    assert decision["route_decision"]["source"] == "deterministic_fallback"
    assert "顺序" in decision["route_decision"].get("reject_reason", "")


@pytest.mark.asyncio
async def test_planner_records_rejection_for_unregistered_tool_proposal():
    class FakeRouter:
        async def classify(self, request, registry):
            return {
                "ok": True,
                "status": "ok",
                "decision": {"intent": "幻觉工具", "route": "free", "tools": ["not_a_real_tool_xyz"], "skills": [], "confidence": 0.9, "reason": "hallucination"},
                "model": {"provider": "qwen", "status": "ok"},
            }

    from yunpai_orchestrator.agents import PlannerAgent

    planner = PlannerAgent(FakeRouter())
    decision = await planner.aplan({"message": "测试幻觉工具"}, build_default_registry())
    assert decision["route_decision"]["source"] == "deterministic_fallback"
    assert "not_a_real_tool_xyz" in decision["route_decision"].get("reject_reason", "")
