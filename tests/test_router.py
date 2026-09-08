"""书二 §4.1：路由决策链——显式 > LLM > 最小回退；关键词大表不复活。"""
import pytest

from yunpai_orchestrator.llm import QwenConfig, QwenRouter
from yunpai_orchestrator.orchestrator import router as router_mod
from yunpai_orchestrator.orchestrator.router import Router
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.skills import build_default_skill_registry


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
