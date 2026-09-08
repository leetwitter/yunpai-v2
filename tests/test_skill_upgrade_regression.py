"""Skill 版本升级回归集（任务书 §6.2/§5.3）。

目的：Skill 版本升级后，旧样例（fixture 快照）行为保持兼容；新样例不依赖人工
逐文件映射。通过锁定 SkillSpec.version 断言：若未来版本变更，必须先更新本回归集，
防止 catalog/证据/路由契约被无意破坏。
"""

from __future__ import annotations

from yunpai_orchestrator.skills import build_default_skill_registry

# 已验收的 Skill 契约基线：名称 -> 版本 + 关键声明工具。
# 版本升级时必须显式修改此处并重跑全量回归（不允许静默漂移）。
SKILL_VERSION_BASELINE = {
    "business-data-identification": {"version": "1.0.0", "has_tools": False},
    "yunpai-m0-data-foundation": {"version": "1.0.0", "tools": ("data_import_run", "data_import_preview", "data_import_resolve", "data_import_commit")},
    "yunpai-m1-document-parser": {"version": "1.0.0", "tools": ("ingest_document", "submit_m1_review", "generate_m1_report")},
    "yunpai-m2-bom-sop": {"version": "1.0.0", "has_tools": True},
    "yunpai-m3-material-planning": {"version": "1.0.0", "has_tools": True},
    "yunpai-m4-procurement": {"version": "1.0.0", "has_tools": True},
    "yunpai-m5-pmc": {"version": "1.0.0", "has_tools": True},
    "yunpai-m5-pmc-lifecycle": {"version": "1.0.0", "tools": ("get_m5_schedule", "list_m5_schedules", "replan_m5_schedule", "dispatch_m5_schedule", "get_m5_execution_summary", "get_m5_pmc_progress")},
}


def test_skill_versions_stay_on_baseline():
    registry = build_default_skill_registry()
    catalog = {item["name"]: item for item in registry.catalog()}
    assert set(catalog) == set(SKILL_VERSION_BASELINE)
    for name, baseline in SKILL_VERSION_BASELINE.items():
        assert catalog[name]["version"] == baseline["version"], f"Skill {name} 版本漂移: {catalog[name]['version']} != {baseline['version']}"


def test_registered_tools_stay_superset_of_baseline():
    registry = build_default_skill_registry()
    for name, baseline in SKILL_VERSION_BASELINE.items():
        declared = set(registry.specs[name].tools)
        if "tools" in baseline:
            assert set(baseline["tools"]) <= declared, f"Skill {name} 丢失声明工具: {set(baseline['tools']) - declared}"
        elif baseline.get("has_tools"):
            assert declared, f"Skill {name} 不应变为无工具"


def test_evidence_shape_survives_skill_call_regression():
    """同一快照重复执行 Skill：证据含 skill 版本/evidence_ref，可复现。"""
    import asyncio

    from yunpai_orchestrator.contracts import ToolSpec
    from yunpai_orchestrator.registry import ToolRegistry

    calls = []

    async def fake_schedule(payload, context):
        calls.append(dict(payload))
        return {"plan_version": "pv-v2-regression"}

    registry = ToolRegistry()
    registry.register(ToolSpec("get_m5_schedule", "m5", "查询排程", {"type": "object"}, {"type": "object"}), fake_schedule)
    skill_registry = build_default_skill_registry()
    results = []
    for _ in range(2):
        result = asyncio.run(skill_registry.call(
            "yunpai-m5-pmc-lifecycle",
            {"operation": "schedule", "tool_payload": {"plan_version": "pv-v2-regression"}},
            {"task_id": "TASK-REGRESSION", "_tool_registry": registry},
        ))
        results.append(result)
    assert results[0] == results[1]
    assert results[0]["skill_version"] == SKILL_VERSION_BASELINE["yunpai-m5-pmc-lifecycle"]["version"]
    assert any(item.get("evidence_ref", "").startswith("skill:yunpai-m5-pmc-lifecycle@") for item in results[0].get("evidence", []))


def test_deterministic_planner_result_is_reproducible():
    from yunpai_orchestrator.agents import PlannerAgent
    from yunpai_orchestrator.llm import QwenConfig, QwenRouter
    from yunpai_orchestrator.registry import build_default_registry

    planner = PlannerAgent(QwenRouter(QwenConfig(enabled=False)), build_default_skill_registry())
    request = {"message": "请识别并落库这些业务资料", "documents": [{"filename": "库存.xlsx", "content_b64": "AA=="}]}
    first = planner.plan(request, build_default_registry())
    second = planner.plan(request, build_default_registry())
    assert first == second
    assert first["steps"][0]["tool"] == "business-data-identification"
