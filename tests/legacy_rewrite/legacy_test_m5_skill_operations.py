"""M5 Skill operation dispatch tests (Task 6 + tool/skill matrix)."""
import asyncio

import pytest

from yunpai_orchestrator.skills import build_default_skill_registry
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.agents import ReviewerAgent


SCOPE = {
    "yunpai-m5-pmc": {
        "solve_scheduling", "get_m5_schedule", "get_m5_pmc_progress",
        "get_m5_integration_contracts", "get_m5_material_readiness",
        "search_m5_knowledge", "record_m5_knowledge",
        "prepare_m5_department_message", "get_m5_department_message",
        "get_m5_department_message_delivery", "advise_m5_schedule",
        "run_m5_intelligent_schedule", "generate_m5_material_procurement_plan",
    },
    "yunpai-m5-pmc-lifecycle": {
        "ingest_m5_planning_snapshot", "get_m5_schedule", "list_m5_schedules",
        "replan_m5_schedule", "get_m5_pmc_progress", "dispatch_m5_schedule",
        "get_m5_execution_summary",
    },
}

FORBIDDEN = {"report_workload", "bind_worker_to_order"}

# operation name -> expected default tool (taskbook: unique unambiguous mapping)
OP_MAP_PMC = {
    "solve": "solve_scheduling", "schedule": "get_m5_schedule",
    "progress": "get_m5_pmc_progress", "contracts": "get_m5_integration_contracts",
    "readiness": "get_m5_material_readiness",
    "knowledge_search": "search_m5_knowledge",
    "knowledge_record": "record_m5_knowledge",
    "message_prepare": "prepare_m5_department_message",
    "message_get": "get_m5_department_message",
    "message_delivery": "get_m5_department_message_delivery",
    "advise": "advise_m5_schedule", "intelligent": "run_m5_intelligent_schedule",
    "procurement": "generate_m5_material_procurement_plan",
}
OP_MAP_LIFECYCLE = {
    "snapshot": "ingest_m5_planning_snapshot", "ingest": "ingest_m5_planning_snapshot",
    "schedule": "get_m5_schedule", "versions": "list_m5_schedules",
    "progress": "get_m5_pmc_progress", "replan": "replan_m5_schedule",
    "dispatch": "dispatch_m5_schedule", "execution": "get_m5_execution_summary",
}


def _handler_callable(name):
    from yunpai_orchestrator.workers import HANDLERS
    return HANDLERS.get(name)


def test_skill_tool_whitelists_match_scope_and_exclude_forbidden():
    registry = build_default_registry()
    skills = build_default_skill_registry()
    # every declared tool must exist in the registry
    skills.validate_tools(registry.specs)
    for skill_name, expected in SCOPE.items():
        spec = skills.specs[skill_name]
        assert set(spec.tools) == expected, skill_name
        assert not (set(spec.tools) & FORBIDDEN), skill_name


def test_every_scoped_skill_tool_is_bound():
    registry = build_default_registry()
    for tools in SCOPE.values():
        for name in tools:
            assert name in registry.handlers, f"{name} 应绑定本地 handler"


@pytest.mark.asyncio
async def test_m5_pmc_operations_dispatch_to_intended_tools():
    from yunpai_orchestrator.contracts import ToolSpec
    from yunpai_orchestrator.registry import ToolRegistry

    calls = []
    registry = ToolRegistry()
    for name in set(OP_MAP_PMC.values()) | set(OP_MAP_LIFECYCLE.values()):
        async def fake(payload, context, _name=name):
            calls.append((_name, payload, context))
            return {"success": True, "data": {}, "errors": [], "trace_id": "t", "tool": _name}
        registry.register(ToolSpec(name, "m5", name, {"type": "object"}, {"type": "object"}), fake)
    skills = build_default_skill_registry()
    # yunpai-m5-pmc operations
    for operation, tool in OP_MAP_PMC.items():
        await skills.call("yunpai-m5-pmc",
                          {"operation": operation, "tool_payload": {}},
                          {"task_id": "T", "_tool_registry": registry})
        assert calls[-1][0] == tool, (operation, tool)
    # lifecycle ops
    for operation, tool in OP_MAP_LIFECYCLE.items():
        await skills.call("yunpai-m5-pmc-lifecycle",
                          {"operation": operation, "tool_payload": {}},
                          {"task_id": "T", "_tool_registry": registry})
        assert calls[-1][0] == tool, (operation, tool)


@pytest.mark.asyncio
async def test_lifecycle_skill_rejects_forbidden_and_cross_module_tools():
    from yunpai_orchestrator.contracts import ToolSpec
    from yunpai_orchestrator.registry import ToolRegistry

    async def fake(payload, context):
        return {"success": True, "data": {}, "errors": [], "trace_id": "t"}
    registry = ToolRegistry()
    registry.register(ToolSpec("get_m5_schedule", "m5", "x", {"type": "object"}, {"type": "object"}), fake)
    registry.register(ToolSpec("data_import_commit", "m0", "x", {"type": "object"}, {"type": "object"}), fake)
    skills = build_default_skill_registry()
    for tool in ("report_workload", "data_import_commit"):
        with pytest.raises(ValueError, match="not allowed"):
            await skills.call(
                "yunpai-m5-pmc-lifecycle",
                {"operation": "versions", "tool": tool, "tool_payload": {}},
                {"task_id": "T", "_tool_registry": registry},
            )


def test_reviewer_readonly_operations_do_not_require_authorization():
    reviewer = ReviewerAgent()
    allowed = reviewer._READ_ONLY_SKILL_OPERATIONS["yunpai-m5-pmc"]
    for operation in ("schedule", "progress", "contracts", "readiness", "advise", "intelligent"):
        assert operation in allowed
    # write ops are never silently read-only
    assert "dispatch" not in reviewer._READ_ONLY_SKILL_OPERATIONS["yunpai-m5-pmc-lifecycle"]
    assert "replan" not in reviewer._READ_ONLY_SKILL_OPERATIONS["yunpai-m5-pmc-lifecycle"]
    assert "snapshot" not in reviewer._READ_ONLY_SKILL_OPERATIONS["yunpai-m5-pmc-lifecycle"]
    assert "knowledge_record" not in reviewer._READ_ONLY_SKILL_OPERATIONS["yunpai-m5-pmc"]
    assert "message_prepare" not in reviewer._READ_ONLY_SKILL_OPERATIONS["yunpai-m5-pmc"]
