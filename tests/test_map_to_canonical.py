"""map_to_canonical agent 契约（多模态）解析 + prompt + 调用。"""

from __future__ import annotations

import json

import pytest

from yunpai_orchestrator.llm import QwenConfig, QwenRouter


def test_parse_canonical_accepts_valid_decision():
    decision = QwenRouter._parse_canonical(json.dumps({
        "entity_type": "material",
        "records": [{"material_code": "YA.001", "material_name": "铜箔", "_source": {"row": 3}}],
        "confidence": 0.9,
        "needs_review": False,
        "reason": "物料表",
    }, ensure_ascii=False))
    assert decision["entity_type"] == "material"
    assert decision["records"][0]["material_code"] == "YA.001"
    assert decision["confidence"] == 0.9


def test_parse_canonical_rejects_unknown_entity_type():
    with pytest.raises(ValueError, match="invalid entity_type"):
        QwenRouter._parse_canonical(json.dumps({"entity_type": "bogus", "records": [{"a": 1}]}))


def test_parse_canonical_rejects_empty_records():
    with pytest.raises(ValueError, match="non-empty object array"):
        QwenRouter._parse_canonical(json.dumps({"entity_type": "material", "records": []}))


def test_parse_canonical_forces_needs_review_below_threshold():
    decision = QwenRouter._parse_canonical(json.dumps({
        "entity_type": "material", "records": [{"material_code": "M", "material_name": "x"}],
        "confidence": 0.4,
    }))
    assert decision["needs_review"] is True


def test_canonical_system_prompt_lists_entities_and_fields():
    prompt = QwenRouter._canonical_system_prompt()
    for entity_type in ("material", "inventory", "equipment", "route"):
        assert entity_type in prompt
    assert "material_code" in prompt and "quantity" in prompt


def test_canonical_prompt_includes_image_parts():
    prompt = QwenRouter._canonical_prompt({
        "filename": "订单.png",
        "sniff": {"detected_format": "png"},
        "headers": [],
        "sample_rows": [],
        "images": ["AAAA"],
    })
    assert prompt[0]["type"] == "text"
    assert prompt[1]["type"] == "image_url"
    assert prompt[1]["image_url"]["url"].startswith("data:image/jpeg;base64,AAAA")


def test_canonical_prompt_no_images_is_text_only():
    prompt = QwenRouter._canonical_prompt({
        "filename": "物料.csv",
        "sniff": {"detected_format": "csv"},
        "headers": ["物料编码"],
        "sample_rows": [{"物料编码": "M-1"}],
        "images": [],
    })
    assert len(prompt) == 1
    assert prompt[0]["type"] == "text"


@pytest.mark.asyncio
async def test_map_to_canonical_sends_vision_request(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "entity_type": "inventory",
                "records": [{"material_code": "M-1", "available_qty": 100}],
                "confidence": 0.88, "needs_review": False, "reason": "库存表",
            }, ensure_ascii=False)}}]}

    class Client:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured["body"] = kwargs["json"]
            return Response()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = await QwenRouter(QwenConfig(api_key="test-key")).map_to_canonical({
        "filename": "库存.png", "sniff": {"detected_format": "png"},
        "headers": [], "sample_rows": [], "images": ["IMG"],
    })
    assert result["ok"] is True
    assert result["decision"]["entity_type"] == "inventory"
    # 消息里带 image_url 部分（多模态）
    user_content = captured["body"]["messages"][1]["content"]
    assert any(part.get("type") == "image_url" for part in user_content)
    assert captured["body"]["chat_template_kwargs"]["enable_thinking"] is False
