import json

import pytest

from yunpai_orchestrator.agents import PlannerAgent
from yunpai_orchestrator.llm import QwenConfig, QwenRouter
from yunpai_orchestrator.registry import build_default_registry


@pytest.mark.asyncio
async def test_qwen_router_parses_structured_route(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "intent": "解析订单并排程", "route": "workflow", "tools": [],
                "confidence": 0.93, "reason": "跨越订单、物料和排程",
            }, ensure_ascii=False)}}]}

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
    result = await router.classify({"message": "请解析订单并排程"}, build_default_registry())
    assert result["ok"] is True
    assert result["decision"]["route"] == "workflow"
    assert captured["body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert captured["options"]["trust_env"] is False


@pytest.mark.asyncio
async def test_qwen_router_preserves_chat_answer(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "intent": "能力咨询", "route": "chat", "tools": [], "confidence": 0.9,
                "reason": "用户在询问能力", "answer": "可以处理订单和排程。",
            }, ensure_ascii=False)}}]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def post(self, url, **kwargs): return Response()
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = await QwenRouter(QwenConfig(api_key="test-key")).classify({"message": "你能做什么"}, build_default_registry())
    assert result["decision"]["answer"] == "可以处理订单和排程。"


@pytest.mark.asyncio
async def test_planner_records_qwen_intent_and_validated_route(monkeypatch):
    class FakeRouter:
        async def classify(self, request, registry):
            return {
                "ok": True,
                "status": "ok",
                "decision": {"intent": "订单解析", "route": "free", "tools": ["ingest_document"], "confidence": 0.88, "reason": "需要解析文件"},
                "model": {"provider": "qwen", "model": "test-qwen", "status": "ok"},
            }

    planner = PlannerAgent(FakeRouter())
    decision = await planner.aplan({"message": "请解析订单"}, build_default_registry())
    assert decision["route"] == "free"
    assert decision["steps"][0]["tool"] == "ingest_document"
    assert decision["intent"] == {"name": "订单解析", "confidence": 0.88, "source": "qwen"}
    assert decision["route_decision"]["source"] == "qwen"


@pytest.mark.asyncio
async def test_qwen_disabled_is_explicit_fallback():
    router = QwenRouter(QwenConfig(enabled=False, api_key="test-key"))
    result = await router.classify({"message": "hello"}, build_default_registry())
    assert result["status"] == "disabled"


@pytest.mark.asyncio
async def test_planner_falls_back_from_preview_chain_for_unparsed_order_attachment():
    class FakeRouter:
        async def classify(self, request, registry):
            return {
                "ok": True,
                "status": "ok",
                "decision": {
                    "intent": "解析订单数据", "route": "free",
                    "tools": ["data_import_preview", "data_import_resolve", "data_import_run"],
                    "confidence": 0.9, "reason": "preview first",
                },
                "model": {"provider": "qwen", "status": "ok"},
            }

    planner = PlannerAgent(FakeRouter())
    decision = await planner.aplan({
        "message": "请解析并校验这份订单",
        "attachments": [{"kind": "order", "filename": "order.xlsx", "content_b64": "AA=="}],
    }, build_default_registry())
    assert decision["route"] == "free"
    assert [step["tool"] for step in decision["steps"]] == ["ingest_document"]
    assert decision["route_decision"]["source"] == "deterministic_fallback"
