"""yunpai-m1-document-parser Skill acceptance: 17 tools, unique ops, Gate split."""

from __future__ import annotations

import pytest

from yunpai_orchestrator.agents import ReviewerAgent
from yunpai_orchestrator.contracts import ToolSpec
from yunpai_orchestrator.graph import YunpaiGraph
from yunpai_orchestrator.m1_tooling import (
    M1_READ_ONLY_SKILL_OPERATIONS,
    M1_SKILL_OPERATION_MAP,
    M1_TOOL_NAMES,
    unique_tools,
)
from yunpai_orchestrator.models import new_state
from yunpai_orchestrator.registry import ToolRegistry
from yunpai_orchestrator.skills import build_default_skill_registry

M1_SKILL = "yunpai-m1-document-parser"


def _skill_catalog():
    catalog = {item["name"]: item for item in build_default_skill_registry().catalog()}
    return catalog[M1_SKILL]


def test_skill_declares_all_seventeen_m1_tools():
    skill = _skill_catalog()
    assert set(skill["tools"]) == set(M1_TOOL_NAMES)


def test_operation_map_is_complete_and_unique():
    values = list(M1_SKILL_OPERATION_MAP.values())
    assert set(values) == set(M1_TOOL_NAMES)
    assert len(set(values)) == len(M1_TOOL_NAMES) == 17
    assert set(M1_SKILL_OPERATION_MAP) >= {"default", "parse", "ingest", "archive", "task", "batch", "document", "orders", "export_order", "documents", "tasks", "review_queue", "review", "report", "knowledge_search", "knowledge_entities", "knowledge_entity", "knowledge_graph", "knowledge_stats"}


def test_read_only_operation_split_is_explicit_in_reviewer():
    assert ReviewerAgent._READ_ONLY_SKILL_OPERATIONS[M1_SKILL] == M1_READ_ONLY_SKILL_OPERATIONS
    assert "orders" in M1_READ_ONLY_SKILL_OPERATIONS
    assert "review" not in M1_READ_ONLY_SKILL_OPERATIONS
    assert "archive" not in M1_READ_ONLY_SKILL_OPERATIONS
    assert "report" not in M1_READ_ONLY_SKILL_OPERATIONS
    # Every declared op either maps read-only or is an explicit side effect.
    assert set(M1_SKILL_OPERATION_MAP) == M1_READ_ONLY_SKILL_OPERATIONS | (set(M1_SKILL_OPERATION_MAP) - M1_READ_ONLY_SKILL_OPERATIONS)


@pytest.mark.asyncio
async def test_skill_operation_dispatches_to_the_mapped_tool():
    calls = []

    async def fake_get_task(payload, context):
        calls.append((payload, context))
        return {"task_id": payload["task_id"], "status": "done"}

    registry = ToolRegistry()
    registry.register(ToolSpec("get_m1_task", "m1", "task", {"type": "object"}, {"type": "object"}), fake_get_task)
    skills = build_default_skill_registry()
    result = await skills.call(
        M1_SKILL,
        {"operation": "task", "tool_payload": {"task_id": "task-1"}},
        {"task_id": "ROOT", "_tool_registry": registry},
    )
    assert result["invoked_tool"] == "get_m1_task"
    assert result["skill_operation"] == "task"
    assert calls == [({"task_id": "task-1"}, {"task_id": "ROOT"})]


@pytest.mark.asyncio
async def test_unknown_skill_operation_rejected():
    skills = build_default_skill_registry()
    registry = ToolRegistry()
    for tool in unique_tools(M1_SKILL_OPERATION_MAP):
        registry.register(ToolSpec(tool, "m1", tool, {"type": "object"}, {"type": "object"}))
    with pytest.raises(ValueError, match="not allowed"):
        await skills.call(M1_SKILL, {"operation": "bogus", "tool_payload": {}}, {"task_id": "T", "_tool_registry": registry})


@pytest.mark.asyncio
async def test_skill_explicit_tool_cannot_escape_whitelist():
    skills = build_default_skill_registry()
    registry = ToolRegistry()
    for tool in unique_tools(M1_SKILL_OPERATION_MAP) + ("data_import_commit",):
        registry.register(ToolSpec(tool, "m0" if tool == "data_import_commit" else "m1", tool, {"type": "object"}, {"type": "object"}))
    with pytest.raises(ValueError, match="not allowed"):
        await skills.call(M1_SKILL, {"operation": "task", "tool": "data_import_commit", "tool_payload": {}}, {"task_id": "T", "_tool_registry": registry})


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["task", "orders", "knowledge_search", "documents", "review_queue"])
async def test_read_only_m1_skill_operations_run_without_authorization_gate(operation):
    graph = YunpaiGraph()
    result_map = {
        "task": {"task_id": "task-1", "status": "done"},
        "orders": [],
        "knowledge_search": {"tenant_id": "t", "query": "q", "include_candidate": False, "hits": []},
        "documents": [],
        "review_queue": [],
    }
    # Each tool payload must satisfy the manifest input schema (required fields).
    payload_map = {
        "task": {"task_id": "task-1"},
        "orders": {},
        "knowledge_search": {"q": "q"},
        "documents": {},
        "review_queue": {},
    }
    tool = M1_SKILL_OPERATION_MAP[operation]

    async def fake(payload, context):
        return result_map[operation]

    graph.registry.handlers[tool] = fake
    state = await graph.run(new_state({
        "skill": M1_SKILL,
        "skill_payload": {"operation": operation, "tool_payload": payload_map[operation]},
    }))
    assert state["status"] == "completed"
    assert state["outputs"][M1_SKILL]["invoked_tool"] == tool


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["archive", "review", "report", "parse"])
async def test_side_effect_m1_skill_operations_require_authorization_gate(operation):
    graph = YunpaiGraph()
    state = await graph.run(new_state({
        "skill": M1_SKILL,
        "skill_payload": {"operation": operation, "tool_payload": {}},
    }))
    assert state["status"] == "waiting_human"
    assert state["pending_gate"]["type"] == "authorization"
    assert state["pending_gate"]["pre_execution"] is True
    assert state["outputs"] == {}
