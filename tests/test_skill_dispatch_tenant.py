"""R16 回归：Skill 派发层补注入合同要求的 ``tenant_id``（仅该键，契约不改）。

背景（父会话 R16）：Skill 派发只取 ``payload``（``_dispatch_registered_tool``），
不经 ``orchestration_bridge`` 装配，而 ``get_material_readiness_snapshot`` /
``export_m3_procurement_suggestions`` / ``query_m4_material_supply_snapshot`` 的
``input_schema.required`` 含 ``tenant_id`` → 经 Skill 调用直接报
``'tenant_id' is a required property``。

集成修复：``skills._dispatch_registered_tool`` 在 ``registry.call`` 之前，
仅当目标工具合同 required 含 ``tenant_id`` 且调用方未给出时，从
``context["tenant_id"]`` 注入该键。
"""
from __future__ import annotations

from types import SimpleNamespace

from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.skills import build_default_skill_registry


class _CapturingRegistry:
    """真 registry 的 specs + 记录调用的假 call（不执行 handler）。"""

    def __init__(self, specs: dict) -> None:
        self.specs = specs
        self.calls: list[tuple[str, dict, dict]] = []

    async def call(self, name: str, payload: dict, context: dict) -> dict:
        self.calls.append((name, dict(payload), dict(context)))
        return {"success": True, "status": "ok", "code": "", "data": {},
                "errors": [], "invoked_tools": [name]}


def _registry() -> tuple[_CapturingRegistry, object]:
    real = build_default_registry()
    return _CapturingRegistry(real.specs), real


def test_three_tools_really_require_tenant_id_in_contract():
    """前提锁定：R16 涉及的工具在合同里确实把 tenant_id 列为必填。"""
    real = build_default_registry()
    required = {
        name for name, spec in real.specs.items()
        if "tenant_id" in ((spec.input_schema or {}).get("required") or [])
    }
    assert required == {
        "get_material_readiness_snapshot",
        "export_m3_procurement_suggestions",
        "query_m4_material_supply_snapshot",
    }


async def test_skill_dispatch_injects_tenant_id_for_contract_required_tool():
    """M3 齐套快照经 Skill 调用：tenant_id 由 ctx 注入，业务入参原样保留。"""
    fake, _ = _registry()
    skills = build_default_skill_registry(fake)
    result = await skills.call(
        "yunpai-m3-material-planning",
        {"operation": "readiness_snapshot", "order_id": "ORD-1"},
        {"task_id": "T-1", "tenant_id": "tenant-main", "actor": "operator"})

    assert fake.calls, result
    tool, payload, _ctx = fake.calls[0]
    assert tool == "get_material_readiness_snapshot"
    assert payload["tenant_id"] == "tenant-main"
    assert payload["order_id"] == "ORD-1"
    assert result["invoked_tool"] == "get_material_readiness_snapshot"


async def test_skill_dispatch_does_not_override_explicit_tenant_id():
    """调用方显式给出的 tenant_id 优先（不覆盖）。"""
    fake, _ = _registry()
    skills = build_default_skill_registry(fake)
    await skills.call(
        "yunpai-m4-procurement",
        {"operation": "supply", "tenant_id": "tenant-explicit", "material_code": "M-1"},
        {"task_id": "T-1", "tenant_id": "tenant-main"})

    tool, payload, _ctx = fake.calls[0]
    assert tool == "query_m4_material_supply_snapshot"
    assert payload["tenant_id"] == "tenant-explicit"


async def test_skill_dispatch_leaves_other_tools_untouched():
    """合同不要求 tenant_id 的工具不得被塞入未声明字段（additionalProperties 风险）。"""
    fake, _ = _registry()
    skills = build_default_skill_registry(fake)
    await skills.call(
        "yunpai-m3-material-planning",
        {"operation": "export", "order_id": "ORD-1", "tenant_id": "tenant-main"},
        {"task_id": "T-1", "tenant_id": "tenant-main"})
    export_tool, export_payload, _ = fake.calls[0]
    assert export_tool == "export_m3_procurement_suggestions"
    assert export_payload["tenant_id"] == "tenant-main"

    fake2, _ = _registry()
    skills2 = build_default_skill_registry(fake2)
    await skills2.call(
        "yunpai-m2-bom-sop",
        {"operation": "run", "run_id": "m2-1"},
        {"task_id": "T-1", "tenant_id": "tenant-main"})
    tool, payload, _ctx = fake2.calls[0]
    assert tool == "get_m2_run"
    assert "tenant_id" not in payload, "未声明 tenant_id 的工具不得被注入该键"


async def test_skill_dispatch_without_tenant_id_fails_closed():
    """ctx 无 tenant_id 时不伪造：让合同校验如实报缺参。"""
    fake, _ = _registry()
    skills = build_default_skill_registry(fake)
    await skills.call(
        "yunpai-m3-material-planning",
        {"operation": "readiness_snapshot", "order_id": "ORD-1"},
        {"task_id": "T-1"})

    _tool, payload, _ctx = fake.calls[0]
    assert "tenant_id" not in payload
