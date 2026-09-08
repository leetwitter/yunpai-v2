import pytest

from yunpai_orchestrator.registry import ToolHTTPError, build_default_registry


@pytest.mark.asyncio
async def test_http_binding_propagates_task_and_tenant(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"items": []}

    class Client:
        def __init__(self, **kwargs): captured["timeout"] = kwargs["timeout"]
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def request(self, method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return Response()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = build_default_registry()
    registry.bind_http({"m4": "http://m4.test"})
    result = await registry.call("list_m4_tracking", {}, {"task_id": "TASK-1", "tenant_id": "TENANT-1"})
    assert result["items"] == []
    assert result["status"] == "success"
    assert result["execution_mode"] == "goal"
    assert captured["url"].startswith("http://m4.test/")
    assert captured["headers"]["X-Yunpai-Task-ID"] == "TASK-1"
    assert captured["headers"]["X-Yunpai-Tenant-ID"] == "TENANT-1"
    assert captured["params"] == {}


@pytest.mark.asyncio
async def test_http_binding_converts_base64_files_to_multipart(monkeypatch):
    import base64
    import httpx
    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"status": "awaiting_review"}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def request(self, method, url, **kwargs):
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = build_default_registry()
    registry.bind_http({"m0": "http://m0.test"})
    payload = {"files": [{"filename": "order.json", "content_type": "application/json", "content_b64": base64.b64encode(b"{}").decode()}]}
    await registry.call("data_import_run", payload, {"task_id": "TASK-2"})
    field, file_tuple = captured["files"][0]
    assert field == "files"
    assert file_tuple == ("order.json", b"{}", "application/json")


def test_build_runtime_registry_requires_http_in_production_env(monkeypatch):
    from yunpai_orchestrator.registry import build_runtime_registry

    monkeypatch.setenv("YUNPAI_ENV", "production")
    monkeypatch.setenv("YUNPAI_TOOL_TRANSPORT", "local")
    with pytest.raises(RuntimeError, match="production"):
        build_runtime_registry()
    monkeypatch.delenv("YUNPAI_ENV")
    monkeypatch.delenv("YUNPAI_TOOL_TRANSPORT")


def test_build_runtime_registry_sandbox_marks_local_fixture(monkeypatch):
    from yunpai_orchestrator.registry import build_runtime_registry

    monkeypatch.delenv("YUNPAI_ENV", raising=False)
    monkeypatch.setenv("YUNPAI_TOOL_TRANSPORT", "local")
    registry = build_runtime_registry()
    assert registry.environment["local_fixture"] is True
    assert registry.environment["transport"] == "local"
    monkeypatch.delenv("YUNPAI_TOOL_TRANSPORT")


@pytest.mark.asyncio
async def test_http_binding_substitutes_path_and_routes_m3_tenant_to_query(monkeypatch):
    import httpx

    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"success": True, "data": {"status": "approved"}, "trace_id": "trace-1"}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def request(self, method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = build_default_registry()
    registry.bind_http({"m3": "http://m3.test"})
    result = await registry.call(
        "approve_m3_task",
        {
            "approval_id": "APR/1",
            "order_id": "ORDER-1",
            "actor_user": "buyer-1",
            "actor_role": "buyer",
            "comment": "approved",
        },
        {"run_id": "RUN-1", "task_id": "TASK-1", "tenant_id": "TENANT-1"},
    )
    assert result["data"]["status"] == "approved"
    assert captured["url"] == "http://m3.test/api/v1/m3/approval-tasks/APR%2F1:approve"
    assert captured["params"] == {"tenant_id": "TENANT-1"}
    assert "approval_id" not in captured["json"]
    assert "tenant_id" not in captured["json"]
    assert captured["headers"]["X-Yunpai-Trace-ID"] == "RUN-1"
    assert captured["headers"]["X-Actor-User"] == "buyer-1"
    assert captured["headers"]["X-Actor-Role"] == "buyer"


@pytest.mark.asyncio
async def test_http_binding_merges_m3_tenant_with_get_query(monkeypatch):
    import httpx

    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"success": True, "data": {}, "trace_id": "trace-1"}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def request(self, method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = build_default_registry()
    registry.bind_http({"m3": "http://m3.test"})
    await registry.call(
        "get_pr_po_drafts",
        {"order_id": "ORDER-1", "status": "draft"},
        {"task_id": "TASK-1", "tenant_id": "TENANT-1"},
    )
    assert captured["method"] == "GET"
    assert captured["params"] == {
        "tenant_id": "TENANT-1",
        "order_id": "ORDER-1",
        "status": "draft",
    }


@pytest.mark.asyncio
async def test_http_binding_enforces_manifest_required_headers_before_request(monkeypatch):
    import httpx

    called = False

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def request(self, method, url, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("request should not be sent")

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = build_default_registry()
    with pytest.raises(ToolHTTPError, match="MISSING_REQUIRED_HEADER"):
        await registry.call(
            "query_m4_material_supply_snapshot",
            {
                "schema_version": "m5.material-supply-query.v1",
                "scenario_id": "SC-1",
                "tenant_id": "TENANT-1",
                "site_id": "SITE-1",
                "material_ids": ["MAT-1"],
                "as_of": "2026-09-04T08:00:00+08:00",
                "horizon_end": "2026-09-05T08:00:00+08:00",
            },
            {"task_id": "TASK-1", "tenant_id": "TENANT-1"},
        )
    assert called is False


@pytest.mark.asyncio
async def test_http_binding_maps_status_errors_to_stable_tool_error(monkeypatch):
    import httpx

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def request(self, method, url, **kwargs):
            request = httpx.Request(method, url)
            response = httpx.Response(409, request=request, json={"code": "REVISION_CONFLICT"})
            raise httpx.HTTPStatusError("conflict", request=request, response=response)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = build_default_registry()
    registry.bind_http({"m4": "http://m4.test"})
    with pytest.raises(ToolHTTPError) as error:
        await registry.call(
            "approve_m4_purchase_order",
            {
                "purchase_order_id": 7,
                "expected_revision": 2,
                "expected_checksum": "a" * 64,
            },
            {"task_id": "TASK-1"},
        )
    assert error.value.code == "HTTP_STATUS_ERROR"
    assert error.value.status_code == 409
    assert "REVISION_CONFLICT" in str(error.value)


@pytest.mark.asyncio
async def test_http_binding_maps_timeout_to_stable_tool_error(monkeypatch):
    import httpx

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return None
        async def request(self, method, url, **kwargs):
            raise httpx.ReadTimeout("upstream timeout", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    registry = build_default_registry()
    registry.bind_http({"m4": "http://m4.test"})
    with pytest.raises(ToolHTTPError) as error:
        await registry.call("list_m4_tracking", {}, {"task_id": "TASK-1"})
    assert error.value.code == "HTTP_TIMEOUT"
