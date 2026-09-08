"""T2 回归：Qwen/M2 模型端点配置守卫与 M2 端点故障的 Gate 语义。

- Planner Qwen 默认端点必须是本机 OpenAI-compatible 代理，绝不能落到 127.0.0.1:9；
- 配置模板(.env.example)与启动脚本(start_backend.sh)必须带端点说明/守卫；
- M2 工程工具在模型/服务端点不可达(HTTP_UNAVAILABLE/HTTP_TIMEOUT)时必须由
  编排器转成可恢复 BLOCKED_INPUT 数据 Gate（不能硬失败伪装模型成功）。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest

import yunpai_orchestrator.llm as llm
from yunpai_orchestrator.graph import YunpaiGraph
from yunpai_orchestrator.models import new_state
from yunpai_orchestrator.registry import ToolHTTPError

ROOT = Path(__file__).resolve().parents[1]


def test_qwen_default_endpoint_is_local_proxy_never_port_9():
    config = llm.QwenConfig()
    parts = urlsplit(config.base_url)
    assert parts.port == 8088
    assert parts.port != 9
    assert "qwen" in parts.hostname or parts.hostname in {"127.0.0.1", "localhost"}


def test_qwen_from_env_honors_explicit_endpoint_and_reports_honestly(monkeypatch):
    monkeypatch.setenv("QWEN_BASE_URL", "http://127.0.0.1:18085/v1")
    monkeypatch.setenv("QWEN_MODEL", "qwen3.6-35b-a3b-fp8-gpu0-200k")
    config = llm.QwenConfig.from_env()
    assert urlsplit(config.base_url).port == 18085
    public = config.public()
    assert public["base_url"].endswith(":18085/v1")
    assert "configured" in public
    # 端点错误也必须如实反映：from_env 不校验、不伪造。
    monkeypatch.setenv("QWEN_BASE_URL", "http://127.0.0.1:9/v1")
    broken = llm.QwenConfig.from_env()
    assert urlsplit(broken.base_url).port == 9


def test_env_example_documents_m2_model_endpoint_contract():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "QWEN_BASE_URL=http://127.0.0.1:8088/v1" in text
    assert "M2_MODEL_BASE_URL" in text
    assert ":9" not in text.replace("127.0.0.1:9", "")  # 模板本身不出现 127.0.0.1:9 值


def test_start_backend_script_contains_endpoint_guard():
    text = (ROOT / "ops" / "deploy" / "start_backend.sh").read_text(encoding="utf-8")
    assert "8088" in text
    assert "QWEN_BASE_URL" in text
    assert "127.0.0.1:8081/" in text  # 启动守卫拦截未代理端口


@pytest.mark.asyncio
async def test_m2_endpoint_unavailable_opens_recoverable_data_gate():
    graph = YunpaiGraph()
    original = graph.registry.handlers["run_bom_sop_workflow"]

    async def unavailable(payload, context):
        raise ToolHTTPError("run_bom_sop_workflow", "HTTP_UNAVAILABLE", "M2 backend 127.0.0.1:8765 refused connection")

    graph.registry.handlers["run_bom_sop_workflow"] = unavailable
    try:
        state = await graph.run(new_state({
            "tool": "run_bom_sop_workflow",
            "product": {"product_code": "P-1"},
            "bom_lines": [{"material_code": "M-1", "material_name": "铜线", "quantity_per": 1}],
        }, tenant_id="t2-test"))
    finally:
        graph.registry.handlers["run_bom_sop_workflow"] = original
    assert state["status"] == "waiting_human"
    assert state["pending_gate"]["type"] == "data"
    error = state["current_result"]["errors"][0]
    assert error["code"] == "M2_MODEL_ENDPOINT_UNAVAILABLE"
    assert "端点不可用" in error["message"]
    assert any(item.get("evidence_ref", "").endswith("endpoint-unavailable") for item in state["current_result"].get("evidence", []))
