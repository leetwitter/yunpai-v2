from __future__ import annotations

from urllib.parse import urlsplit

import httpx
import pytest

from yunpai_orchestrator.registry import build_default_registry


class _Response:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


class _StatefulM3M4Backend:
    def __init__(self) -> None:
        self.po_status = "draft"
        self.po_revision = 1
        self.po_checksum = "b" * 64
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, url: str, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, path))
        headers = kwargs["headers"]
        assert headers["X-Yunpai-Task-ID"] == "TASK-ROOT"
        assert headers["X-Yunpai-Tenant-ID"] == "TENANT-1"
        assert headers["Idempotency-Key"] == "IDEM-ROOT"

        if path.endswith("/approval-tasks/APR-1:approve_to_send"):
            return _Response({"success": True, "data": {"status": "approved_to_send"}, "trace_id": "trace-m3"})
        if path.endswith("/procurement-plan/PLAN-1/export-suggestions"):
            return _Response({
                "plan_id": "PLAN-1",
                "version_id": "PLAN-1-V1",
                "tenant_id": "TENANT-1",
                "tracking_task_id": "TASK-ROOT",
                "source_plan_checksum": "a" * 64,
                "observed_at": "2026-09-04T10:00:00+08:00",
                "suggestions": [{
                    "item_code": "MAT-1",
                    "item_name": "Copper wire",
                    "quantity": 100,
                    "unit": "M",
                    "supplier_name": "SUP-1",
                    "required_date": "2026-09-10",
                    "project_code": "ORDER-1",
                }],
                "count": 1,
            })
        if path.endswith("/suggestions/import-json"):
            body = kwargs["json"]
            assert body["source_plan_checksum"] == "a" * 64
            return _Response({
                "id": 11,
                "status": "needs_review",
                "items": [{"id": 101, "validation_status": "valid"}],
                "suggestions": body["suggestions"],
            })
        if path.endswith("/purchase-orders/generate"):
            assert kwargs["json"]["suggestion_item_ids"] == [101]
            return _Response([self._po()])
        if path.endswith("/purchase-orders/1/submit-review"):
            self._check_revision(kwargs["json"])
            self.po_status = "pending_review"
            self._advance("c")
            return _Response(self._po())
        if path.endswith("/purchase-orders/1/approve"):
            self._check_revision(kwargs["json"])
            if self.po_status != "pending_review":
                return self._conflict(method, url, "PO_REVIEW_REQUIRED")
            self.po_status = "pending_send"
            self._advance("d")
            return _Response(self._po())
        if path.endswith("/purchase-orders/1/send"):
            if self.po_status != "pending_send":
                return self._conflict(method, url, "PO_APPROVAL_REQUIRED")
            self.po_status = "sent"
            self._advance("e")
            return _Response(self._po())
        if path.endswith("/replies"):
            return _Response({
                "id": 9,
                "purchase_order_id": 1,
                "purchase_order_no": "PO-1",
                "supplier_name": "SUP-1",
                "reply_content": kwargs["json"]["reply_content"],
                "received_at": "2026-09-04T11:00:00+08:00",
            })
        if path.endswith("/replies/9/parse"):
            return _Response({
                "delivery_date": "2026-09-09",
                "unit_price": "1.25",
                "currency": "CNY",
                "tax_included": True,
                "exception_type": None,
                "exception_description": None,
                "confidence": 0.98,
                "need_human_review": False,
            })
        if path.endswith("/replies/9/confirm"):
            value = dict(kwargs["json"])
            value.setdefault("confidence", 0.98)
            value.setdefault("currency", "CNY")
            return _Response(value)
        if path.endswith("/replies/9/supply-facts/confirm"):
            body = kwargs["json"]
            return _Response({
                "schema_version": "m4.supplier-fact-confirmation.v1",
                "id": 19,
                "purchase_order_item_id": body["purchase_order_item_id"],
                "supplier_reply_id": 9,
                "supplier_reply_version": body["supplier_reply_version"],
                "confirmed_by": "buyer-1",
                "confirmed_at": "2026-09-04T11:05:00+08:00",
                "parse_confidence": "0.98",
                "delivery_date": body["result"]["delivery_date"],
                "exception_type": None,
                "exception_description": None,
                "result_payload": body["result"],
                "checksum": "sha256:" + "f" * 64,
            })
        raise AssertionError(f"unexpected request: {method} {url}")

    def _po(self) -> dict:
        return {
            "id": 1,
            "purchase_order_no": "PO-1",
            "supplier_name": "SUP-1",
            "status": self.po_status,
            "revision": self.po_revision,
            "checksum": self.po_checksum,
            "items": [{"id": 21, "item_code": "MAT-1"}],
        }

    def _check_revision(self, body: dict) -> None:
        if body["expected_revision"] != self.po_revision or body["expected_checksum"] != self.po_checksum:
            raise AssertionError("adapter did not preserve revision/checksum")

    def _advance(self, checksum_char: str) -> None:
        self.po_revision += 1
        self.po_checksum = checksum_char * 64

    @staticmethod
    def _conflict(method: str, url: str, code: str):
        request = httpx.Request(method, url)
        response = httpx.Response(409, request=request, json={"code": code})
        raise httpx.HTTPStatusError(code, request=request, response=response)


@pytest.mark.asyncio
async def test_m3_approval_handoff_and_m4_supplier_fact_flow(monkeypatch):
    backend = _StatefulM3M4Backend()

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def request(self, method, url, **kwargs):
            return await backend.request(method, url, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = build_default_registry()
    registry.bind_http({"m3": "http://m3.test", "m4": "http://m4.test"})
    context = {
        "run_id": "RUN-ROOT",
        "task_id": "TASK-ROOT",
        "tenant_id": "TENANT-1",
        "idempotency_key": "IDEM-ROOT",
        "authorization": "Bearer test-token",
    }

    approval = await registry.call(
        "approve_to_send_m3_task",
        {"approval_id": "APR-1", "order_id": "ORDER-1", "actor_user": "buyer-1", "actor_role": "buyer"},
        context,
    )
    assert approval["data"]["status"] == "approved_to_send"

    exported = await registry.call(
        "export_m3_procurement_suggestions",
        {"plan_id": "PLAN-1", "tenant_id": "TENANT-1", "version_id": "PLAN-1-V1", "source_plan_checksum": "a" * 64},
        context,
    )
    imported = await registry.call(
        "import_m4_purchase_suggestions_json",
        {
            "suggestions": exported["suggestions"],
            "tenant_id": "TENANT-1",
            "site_id": "SITE-1",
            "tracking_task_id": "TASK-ROOT",
            "idempotency_key": "IDEM-ROOT",
            "source_module": "m3",
            "procurement_plan_id": "PLAN-1",
            "procurement_plan_version_id": "PLAN-1-V1",
            "source_plan_checksum": "a" * 64,
        },
        context,
    )
    orders = await registry.call(
        "generate_m4_purchase_orders",
        {"suggestion_item_ids": [imported["items"][0]["id"]], "tenant_id": "TENANT-1", "site_id": "SITE-1"},
        context,
    )
    po = orders[0]

    # P0-5：``send_m4_purchase_order`` 不再有远程通道——bind_http 不为其安装 HTTP
    # 适配器，调用落在本地 handler（本地库无该 PO → NOT_FOUND；受控 ctx 另有
    # LEGACY_SEND_DISABLED，见 tests/test_m4_local_tools.py）。关键是**绝不转发**到 /send。
    with pytest.raises(ValueError, match="NOT_FOUND|LEGACY_SEND_DISABLED"):
        await registry.call("send_m4_purchase_order", {"purchase_order_id": po["id"]}, context)

    po = await registry.call(
        "submit_m4_purchase_order_review",
        {"purchase_order_id": 1, "expected_revision": po["revision"], "expected_checksum": po["checksum"]},
        context,
    )
    po = await registry.call(
        "approve_m4_purchase_order",
        {"purchase_order_id": 1, "expected_revision": po["revision"], "expected_checksum": po["checksum"]},
        context,
    )
    # P0-5：``send_m4_purchase_order`` 是 local_only/remote_invocation=forbidden 的 legacy
    # 兼容面——bind_http 不为其安装 HTTP 适配器，受控 ctx（带 task_id）一律拒绝本地出站，
    # 因此绝不可能出现到 /send 的远程调用。
    with pytest.raises(ValueError, match="NOT_FOUND|LEGACY_SEND_DISABLED"):
        await registry.call("send_m4_purchase_order", {"purchase_order_id": 1}, context)

    reply = await registry.call(
        "create_m4_supplier_reply",
        {"purchase_order_id": 1, "purchase_order_no": "PO-1", "supplier_name": "SUP-1", "reply_content": "Delivery confirmed for 2026-09-09"},
        context,
    )
    parsed = await registry.call("parse_m4_supplier_reply", {"reply_id": reply["id"]}, context)
    assert parsed["need_human_review"] is False
    confirmed = await registry.call(
        "confirm_m4_supplier_reply",
        {"reply_id": 9, **parsed, "confirmed_by": "buyer-1"},
        context,
    )
    fact = await registry.call(
        "confirm_m4_supplier_fact",
        {
            "reply_id": 9,
            "schema_version": "m4.supplier-fact-confirmation-request.v1",
            "purchase_order_item_id": 21,
            "supplier_reply_version": 1,
            "result": {"delivery_date": confirmed["delivery_date"]},
        },
        context,
    )
    assert fact["purchase_order_item_id"] == 21
    assert fact["checksum"].startswith("sha256:")
    # P0-5 硬断言：远程 /send 通道必须不存在（本地 handler 才是唯一执行面）。
    assert not [call for call in backend.calls if call[1].endswith("/send")]
    from yunpai_orchestrator.registry import is_remote_adapter

    assert not is_remote_adapter(registry.handlers["send_m4_purchase_order"])
