"""M3「V2 零引用」7 件的 HTTP 链路与装配断言（rows-S4「测试需补」）。

覆盖：``get_m3_approval_tasks`` / ``get_m3_m4_handoffs`` / ``get_m3_order`` /
``list_m3_orders`` / ``get_persisted_m3_plan`` / ``reject_m3_task`` /
``request_change_m3_task`` —— 这 7 件在 V2 全量源码里零引用（既无本地 handler 也无行为测试），
按 rows 判「保留HTTP」，因此测试落在 **HTTP 适配器行为**（method/path/query/headers/信封）
与 **装配口径**（桥无分支 → EXPLICIT_ONLY，缺必填 → 结构化 BLOCKED_INPUT）上。
"""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest

from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.worker.assembler import Assembler

CTX = {
    "run_id": "RUN-M3",
    "task_id": "TASK-M3",
    "tenant_id": "TENANT-1",
    "idempotency_key": "IDEM-M3",
}

APPROVAL_BODY = {"approval_id": "APR-1", "order_id": "ORD-1",
                 "actor_user": "buyer-1", "actor_role": "buyer"}

#: (tool, payload, method, path, 期望 query, 期望 body 键, data 是否为数组)
CASES = (
    ("get_m3_approval_tasks", {"order_id": "ORD-1"}, "GET", "/api/v1/m3/approval-tasks",
     {"order_id": "ORD-1"}, None, True),
    ("get_m3_m4_handoffs", {"plan_id": "PLAN-1"}, "GET",
     "/api/v1/m3/procurement-plan/PLAN-1/m4-handoffs", {}, None, True),
    ("get_m3_order", {"order_id": "ORD/1"}, "GET", "/api/v1/m3/orders/ORD%2F1", {}, None, False),
    ("list_m3_orders", {}, "GET", "/api/v1/m3/orders", {}, None, True),
    ("get_persisted_m3_plan", {"plan_id": "PLAN-1"}, "GET",
     "/api/v1/m3/persisted/procurement-plans/PLAN-1", {"tenant_id": "TENANT-1"}, None, False),
    ("reject_m3_task", dict(APPROVAL_BODY), "POST",
     "/api/v1/m3/approval-tasks/APR-1:reject", {"tenant_id": "TENANT-1"},
     ("order_id", "actor_user", "actor_role"), False),
    ("request_change_m3_task", dict(APPROVAL_BODY), "POST",
     "/api/v1/m3/approval-tasks/APR-1:request-change", {"tenant_id": "TENANT-1"},
     ("order_id", "actor_user", "actor_role"), False),
)


class _Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


class _Recorder:
    """按 path 形态返回契约合法信封（list 类 data=[]，对象类 data={...}）。"""

    def __init__(self):
        self.calls: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        path = urlsplit(url).path
        is_list = (method == "GET" and path in {"/api/v1/m3/orders", "/api/v1/m3/approval-tasks"}) \
            or path.endswith("/m4-handoffs")
        data = [] if is_list else {"status": "ok"}
        return _Response({"success": True, "data": data, "trace_id": "trace-m3"})


@pytest.fixture()
def http_registry(monkeypatch):
    recorder = _Recorder()

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def request(self, method, url, **kwargs):
            return await recorder.request(method, url, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = build_default_registry()
    registry.bind_http({"m3": "http://m3.test"})
    return registry, recorder


@pytest.mark.parametrize("tool,payload,method,path,query,body_keys,is_list",
                         CASES, ids=[case[0] for case in CASES])
@pytest.mark.asyncio
async def test_zero_reference_tools_route_over_http(http_registry, tool, payload, method,
                                                    path, query, body_keys, is_list):
    registry, recorder = http_registry
    out = await registry.call(tool, payload, CTX)
    call = recorder.calls[-1]
    assert call["method"] == method
    assert urlsplit(call["url"]).path == path
    assert (call.get("params") or {}) == query
    if method == "GET":
        assert "json" not in call
    else:
        body = call["json"]
        assert set(body) == set(body_keys or ())
        assert "approval_id" not in body      # 路径参数不得同时进 body
        assert "tenant_id" not in body        # query 字段不得同时进 body
    headers = call["headers"]
    assert headers["X-Yunpai-Task-ID"] == "TASK-M3"
    assert headers["X-Yunpai-Tenant-ID"] == "TENANT-1"
    assert headers["Idempotency-Key"] == "IDEM-M3"
    # 合同信封归一化（contracts.normalize_contract_result）
    assert out["status"] == "success"
    assert out["source"]["kind"] == f"tool:{tool}"
    assert out["invoked_tools"] == [tool]
    assert out["data"] == ([] if is_list else {"status": "ok"})


@pytest.mark.parametrize("tool,payload,_method,_path,_query,_body_keys,_is_list",
                         CASES, ids=[case[0] for case in CASES])
@pytest.mark.asyncio
async def test_zero_reference_tools_are_explicit_only(tool, payload, _method, _path,
                                                      _query, _body_keys, _is_list):
    """V2 桥无这些工具的分支 → 装配退化为「仅显式参数」（assembler.py 尾部 EXPLICIT_ONLY）。"""
    from yunpai_orchestrator.orchestration_bridge import bridge_payload

    assert bridge_payload({"request": {}}, tool) == {}
    registry = build_default_registry()
    assembled = await Assembler(registry).assemble(tool, {"request": {tool: payload}})
    assert assembled.blocked is False
    assert assembled.payload == payload


@pytest.mark.asyncio
async def test_m3_order_missing_required_field_is_structured_blocked_input():
    """缺必填（order_id）→ 结构化 BLOCKED_INPUT，不再静默空 dict。"""
    registry = build_default_registry()
    assembled = await Assembler(registry).assemble("get_m3_order", {"request": {}})
    assert assembled.blocked is True
    assert assembled.payload["code"] == "BLOCKED_INPUT"
    assert assembled.missing and assembled.missing[0]["field"] == "order_id"
