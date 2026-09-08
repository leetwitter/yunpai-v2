"""真实订单结构回放：HTTP Adapter 补充候选 + review Gate + 本地图重放。

用例使用 test_order_semantics.real_order_twin_bytes() 的**同构合成工作簿**
复现实测 run 的输入结构（真实样本本身不进仓库）；断言：
1. 外部 M1 返回“订单但 0 行/缺订单号”时，Adapter 附加本地确定性候选且
   保留外部原结果，并强制 needs_review；
2. Reviewer 对该结果打开 type=review Gate；
3. 本地图（m1_m5_document_to_plan）重放同构文件能产生订单头+订单行并停在
   review Gate；缺产品编码/交期不会变成空成功。
"""

from __future__ import annotations

import base64

import pytest

import yunpai_orchestrator.m1_http_adapter as adapter
from m1_fake_http import BASE, Backend, install
from yunpai_orchestrator.agents import ReviewerAgent
from yunpai_orchestrator.graph import YunpaiGraph
from yunpai_orchestrator.models import new_state
from yunpai_orchestrator.registry import build_default_registry

from test_order_semantics import real_order_twin_bytes

CTX = {"task_id": "TASK-REAL-1", "run_id": "RUN-REAL-1", "tenant_id": "TENANT-1"}

EXTERNAL_ORDER_GAP = {
    "task_id": "task-real-external",
    "status": "needs_review",
    "processing_stage": "review",
    "needs_review": True,
    "overall_confidence": 0.35,
    "doc_type": "order",
    "document_subtype": "customer_purchase_order",
    "document_schema_version": "m1.document.v2",
    "document": {
        "schema_version": "m1.document.v2",
        "document_type": "order",
        "document_subtype": "customer_purchase_order",
        "document_kind_label": "customer_purchase_order",
        "source": {"original_filename": "订单-同构-回放.xlsx", "sha256": "ab" * 32},
        "header": {
            "order_number": None, "order_id": None, "doc_date": None,
            "buyer": None, "seller": None, "title": "业务订单",
        },
        "lines": [],
        "totals": {},
        "field_meta": {},
        "validation_issues": [{"code": "LOW_CONFIDENCE", "message": "解析置信度不足"}],
    },
}


def _xlsx_payload() -> dict:
    raw = real_order_twin_bytes()
    return {
        "file": {
            "filename": "订单-同构-回放.xlsx",
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content_b64": base64.b64encode(raw).decode(),
        },
    }


@pytest.mark.asyncio
async def test_adapter_attaches_supplement_and_keeps_external_result(monkeypatch):
    backend = Backend()
    backend.route("POST", "/ingest/sync", EXTERNAL_ORDER_GAP, repeat=True)
    install(monkeypatch, backend)
    registry = build_default_registry()
    adapter.bind_m1_http(registry, urls={"m1": BASE}, overwrite=True)
    result = await registry.call("ingest_document", _xlsx_payload(), CTX)

    # 外部原结果保留：document 仍为外部 0 行文档，未被覆盖。
    assert result["document"]["lines"] == []
    assert result["document"]["header"]["order_number"] is None
    # 本地确定性候选已附加且强制 review。
    assert result["needs_review"] is True
    supplement = result["semantic_supplement"]
    assert supplement["schema_version"] == "m1.semantic-supplement.v1"
    assert supplement["requires_review"] is True
    assert supplement["document"]["header"]["order_number"] == "WX20260905001"
    assert len(supplement["document"]["lines"]) == 2
    assert supplement["source"]["sha256"]
    assert supplement["parser"]["name"] == "order.parser.v2"
    assert any(missing["field"] == "line.product_code" for missing in supplement["missing_fields"])


@pytest.mark.asyncio
async def test_reviewer_opens_review_gate_for_supplemented_external_result():
    reviewer = ReviewerAgent()
    verdict = reviewer.review("ingest_document", "m1", {**EXTERNAL_ORDER_GAP, "semantic_supplement": {"schema_version": "m1.semantic-supplement.v1"}})
    assert verdict["approved"] is False
    assert verdict["gate"]["type"] == "review"
    assert "本地确定性解析候选" in verdict["gate"]["message"]


@pytest.mark.asyncio
async def test_local_graph_replay_produces_order_rows_and_stops_at_review_gate():
    """本地图（m1_m5_document_to_plan）重放同构真实布局：订单头/行生成，
    但缺产品编码与交期 -> needs_review -> review Gate，绝不空成功。"""
    graph = YunpaiGraph()
    request = {
        "workflow": "m1_m5_document_to_plan",
        "message": "请解析并校验这份采购备货订单",
        "attachments": [
            {"kind": "order", "filename": "订单-同构-回放.xlsx", "content_b64": base64.b64encode(real_order_twin_bytes()).decode()},
        ],
    }
    state = await graph.run(new_state(request, tenant_id="replay-test"))
    assert state["status"] == "waiting_human"
    assert state["pending_gate"]["tool"] == "ingest_document"
    assert state["pending_gate"]["type"] == "review"
    ingested = state["outputs"]["ingest_document"]
    document = ingested["document"]
    assert document["schema_version"] == "m1.document.v2"
    assert document["header"]["order_id"] == "WX20260905001"
    assert len(document["lines"]) == 2
    assert document["lines"][0]["quantity"] == 500.0
    assert document["lines"][1]["quantity"] == 500.0
    assert any(issue["code"].startswith("MISSING_") for issue in (document["validation_issues"] or []))
    assert ingested["needs_review"] is True
