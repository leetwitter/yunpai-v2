"""书二 §4.1：路由决策链——显式 > LLM > 最小回退；关键词大表不复活。"""
import json

import pytest

from yunpai_orchestrator.llm import QwenConfig, QwenRouter
from yunpai_orchestrator.orchestrator import router as router_mod
from yunpai_orchestrator.orchestrator.router import Router
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.skills import build_default_skill_registry
from yunpai_orchestrator.workflow_registry import KNOWN_WORKFLOWS


@pytest.fixture(scope="module")
def router():
    reg = build_default_registry()
    skills = build_default_skill_registry()
    return Router(QwenRouter(QwenConfig(enabled=False)), reg, skills)  # LLM 关闭 → 链路可确定


@pytest.mark.asyncio
async def test_explicit_workflow_wins(router):
    d = await router.decide({"request": {"workflow": "canonical_to_m5", "message": "x"}})
    assert (d.route, d.source, d.workflow_id) == ("workflow", "explicit", "canonical_to_m5")


@pytest.mark.asyncio
async def test_explicit_unknown_workflow_rejected(router):
    with pytest.raises(ValueError):
        await router.decide({"request": {"workflow": "m0_m5"}})


@pytest.mark.asyncio
async def test_explicit_unknown_tool_rejected(router):
    with pytest.raises(ValueError):
        await router.decide({"request": {"tools": ["run_mrp_procurement_plan"]}})


@pytest.mark.asyncio
async def test_fallback_attachment_to_document_workflow(router):
    state = {"request": {"message": "处理这个"}, "attachments": [
        {"filename": "order.xlsx", "kind": "order", "content_b64": "QUJD"}]}
    d = await router.decide(state)
    assert (d.route, d.workflow_id, d.source) == (
        "workflow", "m1_m5_document_to_plan", "deterministic_fallback")


@pytest.mark.asyncio
async def test_fallback_canonical_ready(router):
    d = await router.decide({"request": {"message": "重排", "canonical_ready": True}})
    assert (d.route, d.workflow_id) == ("workflow", "canonical_to_m5")


@pytest.mark.asyncio
async def test_fallback_chat_with_suggestions(router):
    d = await router.decide({"request": {"message": "你好"}, "attachments": []})
    assert (d.route, d.source) == ("chat", "deterministic_fallback")
    assert "可用能力" in d.answer


def test_keyword_tables_not_migrated():
    """治痛点 3：旧 INTENT_TO_TOOL/INTENT_TO_SKILL 关键词大表不进 v2。"""
    assert not hasattr(router_mod, "INTENT_TO_TOOL")
    assert not hasattr(router_mod, "INTENT_TO_SKILL")


# ── LLM 提案层（书二 §4.1 第 2 层）：workflow_id 闭环 ──────────


class _FakeLLM:
    """不走网络的 LLM 桩：classify 固定返回给定决策。"""

    def __init__(self, decision: dict):
        self._decision = decision

    async def classify(self, request, registry, skills=None):
        return {"ok": True, "status": "ok", "decision": self._decision}


def _router_with(decision: dict) -> Router:
    return Router(_FakeLLM(decision), build_default_registry(), build_default_skill_registry())


@pytest.mark.asyncio
async def test_llm_workflow_proposal_accepted():
    router = _router_with({"intent": "document_to_plan", "route": "workflow",
                           "workflow_id": "m1_m5_document_to_plan", "tools": [], "skills": [],
                           "confidence": 0.9, "reason": "订单全链"})
    d = await router.decide({"request": {"message": "订单文件做全链处理"}, "attachments": []})
    assert (d.route, d.source, d.workflow_id) == ("workflow", "llm", "m1_m5_document_to_plan")


@pytest.mark.asyncio
async def test_llm_workflow_proposal_unknown_id_rejected_to_fallback():
    router = _router_with({"route": "workflow", "workflow_id": "m0_m5", "tools": [],
                           "confidence": 0.9, "reason": "x"})
    d = await router.decide({"request": {"message": "x"}, "attachments": []})
    assert d.source == "deterministic_fallback"


@pytest.mark.asyncio
async def test_llm_workflow_proposal_missing_id_rejected_to_fallback():
    router = _router_with({"route": "workflow", "tools": [], "confidence": 0.9, "reason": "x"})
    d = await router.decide({"request": {"message": "x"}, "attachments": []})
    assert d.source == "deterministic_fallback"


@pytest.mark.asyncio
async def test_llm_invisible_tool_proposal_rejected_to_fallback():
    router = _router_with({"route": "free", "tools": ["run_mrp_procurement_plan"], "skills": [],
                           "confidence": 0.9, "reason": "x"})
    d = await router.decide({"request": {"message": "x"}, "attachments": []})
    assert d.source == "deterministic_fallback"


@pytest.mark.asyncio
async def test_llm_free_with_empty_tools_and_skills_rejected():
    """free 路由空工具+空技能 → 拒绝回退（否则空转出「0 步完成」）。"""
    router = _router_with({"route": "free", "tools": [], "skills": [], "confidence": 0.9, "reason": "x"})
    d = await router.decide({"request": {"message": "x"}, "attachments": []})
    assert d.source == "deterministic_fallback"


def test_parse_decision_keeps_workflow_id():
    decision = QwenRouter._parse_decision(
        '{"intent":"i","route":"workflow","workflow_id":"canonical_to_m5",'
        '"tools":[],"confidence":0.8,"reason":"r"}')
    assert decision["workflow_id"] == "canonical_to_m5"


def test_parse_decision_blank_or_absent_workflow_id_becomes_none():
    blank = QwenRouter._parse_decision(
        '{"intent":"i","route":"workflow","workflow_id":"  ","tools":[]}')
    absent = QwenRouter._parse_decision(
        '{"intent":"i","route":"chat","tools":[],"answer":"ok"}')
    assert blank["workflow_id"] is None
    assert absent["workflow_id"] is None


def test_llm_prompt_exposes_known_workflows():
    """书二 §4.1：喂给 LLM 的目录含 KNOWN_WORKFLOWS（id+一句话）。"""
    payload = json.loads(QwenRouter._prompt({"message": "x"}, []))
    ids = {w["id"] for w in payload["workflows"]}
    assert set(KNOWN_WORKFLOWS) <= ids
    assert all(w["description"] for w in payload["workflows"])
    assert "workflow_id" in QwenRouter._system_prompt()
