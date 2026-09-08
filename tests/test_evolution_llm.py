"""llm_tasks 单元测试（不联网：解析/回退逻辑；联网功能测试见 scripts/）。"""
from __future__ import annotations

import asyncio
import json

import pytest

from yunpai_orchestrator.evolution.llm_tasks import EvolutionLLM
from yunpai_orchestrator.llm import QwenConfig


def _disabled() -> EvolutionLLM:
    return EvolutionLLM(QwenConfig(enabled=False, api_key=""))


def test_parse_json_strips_fences():
    llm = _disabled()
    assert llm._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._parse_json('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        llm._parse_json("not json")


def test_content_raises_on_empty():
    llm = _disabled()
    with pytest.raises(ValueError):
        llm._content({"choices": [{"message": {"content": ""}}]})
    with pytest.raises(ValueError):
        llm._content({"choices": []})


def test_categorize_bom_lines_fallback_when_disabled():
    lines = [{"name": "光纤线材", "specification": "28AWG"}]
    out = asyncio.run(_disabled().categorize_bom_lines(lines))
    assert out[0]["category"] == ""
    assert out[0]["llm_status"] in {"disabled", "error"}


def test_normalize_names_fallback_when_disabled():
    out = asyncio.run(_disabled().normalize_operation_names(["裁线", "目检"]))
    assert out[0]["canonical_name"] == "裁线"
    assert out[1]["canonical_name"] == "目检"


def test_summarize_and_resolve_fallback_when_disabled():
    assert asyncio.run(_disabled().summarize_drawing("L=152mm")) == {}
    assert asyncio.run(_disabled().resolve_product("x", ["W-H909"]))["same_family"] is False


def test_categorize_bom_lines_with_mocked_http(monkeypatch):
    """验证真实 JSON 响应被正确解析并逐行对齐。"""
    import httpx as _httpx

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(
                {"lines": [{"category": "线材", "spec_normalized": {"线规": "28AWG"}},
                           {"category": "外壳", "spec_normalized": {"材质": "铝合金"}}]},
                ensure_ascii=False)}}]}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, **kw):
            captured["body"] = kw.get("json") or {}
            return FakeResponse()

    monkeypatch.setattr(_httpx, "AsyncClient", FakeClient)
    llm = EvolutionLLM(QwenConfig(enabled=True, base_url="http://127.0.0.1:8088/v1",
                                  model="qwen3.8-27b", api_key="local"))
    lines = [{"name": "光纤线材", "specification": "28AWG"}, {"name": "铝合金壳", "specification": ""}]
    out = asyncio.run(llm.categorize_bom_lines(lines))
    assert out[0]["category"] == "线材"
    assert out[0]["spec_normalized"]["线规"] == "28AWG"
    assert out[1]["category"] == "外壳"
    # 确认请求体带了 enable_thinking=false（本机 27B 是思维链模型，不关会空 content）。
    assert captured["body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert captured["body"]["temperature"] == 0
