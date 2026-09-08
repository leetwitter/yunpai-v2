from __future__ import annotations

"""Dedicated M1 HTTP adapter speaking the standalone M1 service contract.

Transport contract implemented here (mirrors the T8 M1 FastAPI service):
- ``X-Tenant-ID`` (T8) is derived from trusted context/payload/env; a call with
  no tenant fails closed with ``MISSING_TENANT`` instead of silently falling
  into the service ``default`` tenant.  ``X-Yunpai-Tenant-ID`` is also sent for
  orchestrator-wide correlation.
- Actor identity/roles use the T8 header names ``X-Actor-ID`` and
  ``X-Actor-Roles``.  Roles are only taken from trusted context or environment
  (``M1_ACTOR_ROLES``), never from a client payload, so candidate/under_review
  knowledge rows can never be unlocked by self-reported roles.
- Auth uses the T8 ``X-API-Key`` header (``M1_API_KEY`` env or configured
  headers); a context ``Authorization`` is passed through as a fallback.
- Uploads are sent as multipart.  ``POST /ingest/sync`` may answer HTTP 202
  (``M1_INGEST_ACCEPTED`` with ``poll_url``): the adapter bounded-polls the
  polling endpoint until a terminal status (done/failed/needs_review) or the
  poll budget expires, in which case it returns an explicit, recognizable
  ``pending`` result instead of letting a 202 body masquerade as completion.
- HTTP errors are mapped to stable ``ToolHTTPError`` codes without leaking
  service internals, credentials or arbitrary absolute paths.
- ``search_m1_orders`` manifest-only attribute filters
  (interface/length/color/connector/conductor/od) are applied client-side over
  the returned full ``line`` JSON until the M1 service exposes them as query
  parameters (regression-covered, documented as a service-forward-compat hook).
"""

import asyncio
import json
import os
from typing import Any
from urllib.parse import quote, urljoin

from .registry import ToolHTTPError

POLL_BUDGET_S = float(os.getenv("M1_POLL_BUDGET_S", "90"))
POLL_INTERVAL_S = float(os.getenv("M1_POLL_INTERVAL_S", "1.0"))
TERMINAL_STATUSES = frozenset({"done", "failed", "needs_review"})


def _loop_now() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:  # pragma: no cover - non-async helper callers
        import time

        return time.monotonic()

#: Tools whose manifest input declares the forward-compat order attribute
#: filters that the current M1 service does not yet expose as query params.
#: (interface -> name_attributes.interface, length -> cable_length, color,
#: connector -> plug, conductor, od).  They are applied over the returned
#: full ``line`` JSON by ``_filter_order_rows``.
_ORDER_ATTRIBUTE_PARAMS = frozenset({"interface", "length", "color", "connector", "conductor", "od"})
_ORDER_ATTRIBUTE_KEY = {
    "interface": "interface",
    "length": "cable_length",
    "color": "color",
    "connector": "plug",
    "conductor": "conductor",
    "od": "od",
}


def module_m1_auth_headers() -> dict[str, str]:
    """Auth headers for the standalone M1 service (X-API-Key form)."""
    api_key = os.getenv("M1_API_KEY", "").strip()
    if api_key:
        return {"X-API-Key": api_key}
    authorization = os.getenv("M1_AUTHORIZATION", "").strip()
    if authorization:
        return {"Authorization": authorization}
    return {}


def _tenant_id(payload: dict[str, Any], context: dict[str, Any]) -> str | None:
    value = context.get("tenant_id") or payload.get("tenant_id") or os.getenv("M1_TENANT_ID", "").strip() or None
    return str(value) if value else None


def bind_m1_http(
    registry,
    *,
    urls: dict[str, str] | None = None,
    headers_by_module: dict[str, dict[str, str]] | None = None,
    tool_names: set[str] | frozenset[str] | None = None,
    overwrite: bool = True,
) -> int:
    """Bind M1 specs to the dedicated M1 HTTP adapter.

    ``urls``/``headers_by_module`` use the same shape as ``registry.bind_http``;
    if absent, the module base URL falls back to the manifest base URL and the
    auth headers to ``module_m1_auth_headers()``.
    """
    import httpx  # noqa: F401  (import guard parity with generic adapter)

    base_url = None
    if urls:
        base_url = urls.get("m1")
    if not base_url:
        base_url = os.getenv("M1_URL", "").strip()
    if not base_url and registry is not None:
        tools = registry.tools_for("m1")
        base_url = tools[0].base_url if tools else ""
    if not base_url:
        return 0
    configured = dict((headers_by_module or {}).get("m1", {}))
    configured.update({k: v for k, v in module_m1_auth_headers().items() if v})
    allowed = set(tool_names) if tool_names is not None else None
    installed = 0
    for name, spec in registry.specs.items():
        if spec.module != "m1" or (allowed is not None and name not in allowed):
            continue
        if not overwrite and name in registry.handlers:
            continue
        registry.handlers[name] = _make_m1_handler(spec, base_url, configured)
        installed += 1
    return installed


def _make_m1_handler(spec, base_url: str, configured: dict[str, str]):
    async def handler(payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("M1 HTTP adapter requires httpx") from exc
        name = spec.name
        body = dict(payload)
        path = spec.path
        for key in list(body):
            marker = "{" + key + "}"
            if marker in path:
                path = path.replace(marker, quote(str(body.pop(key)), safe=""))
        headers = _m1_headers(name, body, context, configured)
        files, request_kwargs = _m1_request_kwargs(spec, body)
        # ingest_document 需要保留上传字节，用于外部 M1 缺订单事实时附加本地
        # 确定性候选（不覆盖外部结果，只补充并强制 review）。
        upload_filename, upload_raw = _payload_file_bytes(payload) if name == "ingest_document" else (None, None)
        try:
            async with httpx.AsyncClient(timeout=spec.timeout_s or 60.0) as client:
                response = await client.request(spec.method, base_url.rstrip("/") + path, headers=headers, **request_kwargs)
                if response.status_code == 202:
                    value = await _handle_accepted(client, name, response, headers, base_url)
                else:
                    if response.status_code >= 400:
                        response.raise_for_status()
                    value = _response_json(name, response)
                if isinstance(value, list) and name == "search_m1_orders":
                    value = _filter_order_rows(value, payload)
                if isinstance(value, dict) and name == "ingest_document":
                    value = _attach_semantic_supplement(value, upload_filename, upload_raw)
                return value
        except ToolHTTPError:
            raise
        except httpx.TimeoutException as exc:
            raise ToolHTTPError(name, "HTTP_TIMEOUT", str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise _map_status_error(name, exc) from exc
        except httpx.RequestError as exc:
            raise ToolHTTPError(name, "HTTP_UNAVAILABLE", str(exc)) from exc
        except ValueError as exc:
            raise ToolHTTPError(name, "INVALID_JSON_RESPONSE", str(exc)) from exc
    return handler


def _payload_file_bytes(payload: dict[str, Any]) -> tuple[str | None, bytes | None]:
    """从 ingest_document payload 还原上传文件（string base64 或 file object）。"""
    import base64 as _b64

    file_value = payload.get("file") if isinstance(payload, dict) else None
    if file_value is None:
        return None, None
    if isinstance(file_value, dict):
        filename = str(file_value.get("filename") or "document.bin")
        encoded = file_value.get("content_b64")
        if not isinstance(encoded, str):
            return filename, None
        try:
            return filename, _b64.b64decode(encoded)
        except Exception:
            return filename, None
    if isinstance(file_value, str):
        try:
            return "document.bin", _b64.b64decode(file_value)
        except Exception:
            return "document.bin", None
    return None, None


def _attach_semantic_supplement(value: dict[str, Any], filename: str | None, raw: bytes | None) -> dict[str, Any]:
    """外部 M1 声明订单但缺订单头/行时，附加本地确定性候选并强制 review。

    - 原样保留外部 ``document``/任务结果，仅新增 ``semantic_supplement``；
    - 是否尝试本地补充只按字节结构判断（XLSX magic/OOXML 路径），不依赖文件
      名、路径或租户；无法解析出订单事实时保持原结果不动，绝不伪造补充或把
      0 行订单当成成功；
    - 只要启用了补充，就强制 ``needs_review=True``（打开 review Gate），由
      人工在“外部结果 vs 本地候选”之间复核后再放行下游。
    """
    if raw is None:
        return value
    try:
        from .order_semantics import (
            build_semantic_supplement,
            result_has_order_gap,
            sniff_xlsx_bytes,
        )

        if not sniff_xlsx_bytes(raw) or not result_has_order_gap(value):
            return value
        supplement = build_semantic_supplement(str(filename or "workbook.xlsx"), raw, external=value)
    except Exception:
        # 语义补充失败绝不能让一次真实 M1 调用失败：保持外部原结果。
        return value
    if not isinstance(supplement, dict):
        return value
    out = dict(value)
    out["semantic_supplement"] = supplement
    out["needs_review"] = True
    if not out.get("processing_stage"):
        out["processing_stage"] = "review"
    return out


def _m1_headers(tool: str, payload: dict[str, Any], context: dict[str, Any], configured: dict[str, str]) -> dict[str, str]:
    headers = {key: str(value) for key, value in configured.items() if value}
    task_id = context.get("task_id")
    if task_id:
        headers["X-Yunpai-Task-ID"] = str(task_id)
    tenant = _tenant_id(payload, context)
    if not tenant:
        raise ToolHTTPError(
            tool,
            "MISSING_TENANT",
            "M1 HTTP 调用缺少租户上下文（X-Tenant-ID），已失败关闭，不会静默落入 default 租户；请提供 tenant_id",
        )
    # T8 M1 uses X-Tenant-ID; the orchestrator-wide header is kept for
    # correlation.  Explicit dual-header adaptation is regression-tested so a
    # tenant mismatch can never silently default.
    headers["X-Tenant-ID"] = tenant
    headers["X-Yunpai-Tenant-ID"] = tenant
    idempotency_key = context.get("idempotency_key") or payload.get("idempotency_key")
    if idempotency_key:
        headers["Idempotency-Key"] = str(idempotency_key)
    authorization = context.get("authorization") or context.get("Authorization")
    if authorization and "X-API-Key" not in headers:
        headers["Authorization"] = str(authorization)
    trace_id = context.get("trace_id") or context.get("run_id")
    if trace_id:
        headers["X-Yunpai-Trace-ID"] = str(trace_id)
    # Actor propagation (T8 names).  Identity may come from trusted context;
    # roles NEVER come from a client payload (no self-reported privilege).
    actor_id = context.get("actor_id") or context.get("actor_user")
    if actor_id:
        headers["X-Actor-ID"] = str(actor_id)
    roles = context.get("actor_roles") or _env_roles()
    if roles:
        if isinstance(roles, str):
            roles = [part.strip() for part in roles.split(",") if part.strip()]
        if isinstance(roles, (list, tuple)) and roles:
            headers["X-Actor-Roles"] = ",".join(str(role) for role in roles)
    return headers


def _env_roles() -> list[str] | None:
    value = os.getenv("M1_ACTOR_ROLES", "").strip()
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _m1_request_kwargs(spec, body: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """Build httpx kwargs; uploads become multipart with remaining fields as form data."""
    from .registry import _extract_uploads

    files = _extract_uploads(body)
    request_kwargs: dict[str, Any] = {}
    if files:
        request_kwargs["files"] = files
        request_kwargs["data"] = {
            key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for key, value in body.items()
            if key not in {field for field, _ in files}
        }
    elif spec.method.upper() in {"GET", "DELETE"}:
        request_kwargs["params"] = body
    else:
        request_kwargs["json"] = body
    return files, request_kwargs


def _response_json(tool: str, response: Any) -> Any:
    try:
        value = response.json()
    except ValueError as exc:
        text = getattr(response, "text", "")[:2000] or f"HTTP {getattr(response, 'status_code', 'error')}"
        raise ToolHTTPError(tool, "INVALID_JSON_RESPONSE", f"非 JSON 响应: {text}") from exc
    if isinstance(value, dict) and set(value) == {"result"}:
        return value["result"]
    return value


def _map_status_error(tool: str, exc) -> ToolHTTPError:
    response = exc.response
    status_code = getattr(response, "status_code", 0)
    try:
        detail = response.json()
    except Exception:
        detail = getattr(response, "text", "")[:2000]
    message = _detail_text(detail) or f"HTTP {status_code}"
    # 404 is deliberately rendered as "resource does not exist" by the M1
    # service to avoid disclosing cross-tenant existence; keep it stable.
    return ToolHTTPError(tool, "HTTP_STATUS_ERROR", f"HTTP {status_code}: {message}", status_code=status_code)


def _detail_text(detail: Any) -> str:
    if isinstance(detail, str):
        return detail[:2000]
    if isinstance(detail, dict):
        code = detail.get("code") or detail.get("detail_code")
        message = detail.get("message") or detail.get("detail")
        if isinstance(detail.get("detail"), dict):
            message = detail["detail"].get("message") or json.dumps(detail["detail"], ensure_ascii=False)
        return " ".join(str(part) for part in (code, message) if part)[:2000]
    return str(detail)[:2000]


async def _handle_accepted(client, tool: str, response: Any, headers: dict[str, str], base_url: str) -> dict[str, Any]:
    """Handle HTTP 202: bounded polling by poll_url, or an explicit pending result.

    A raw 202 body must never pass the tool's output schema as if it were a
    completed result; if polling does not reach a terminal status inside the
    budget the adapter returns a recognizable ``pending`` payload carrying the
    task id so the caller can resume with get_m1_task/get_m1_batch.
    """
    body = _response_json(tool, response)
    if not isinstance(body, dict):
        raise ToolHTTPError(tool, "INVALID_202_RESPONSE", "M1 202 accepted 响应不是 JSON 对象")
    task_id = str(body.get("task_id") or "")
    poll_url = body.get("poll_url") or body.get("pollUrl")
    stage = str(body.get("processing_stage") or body.get("status") or "processing")
    deadline = _loop_now() + POLL_BUDGET_S
    last: dict[str, Any] | None = None
    if isinstance(poll_url, str) and poll_url:
        from urllib.parse import urlsplit

        target = poll_url if urlsplit(poll_url).netloc else urljoin(base_url, poll_url)
        last = await _bounded_poll(client, target, headers, deadline)
        if last is not None:
            return last
    return _pending_result(tool, task_id, stage)


async def _bounded_poll(client, poll_url: str, headers: dict[str, str], deadline: float) -> dict[str, Any] | None:
    """GET ``poll_url`` until terminal or the deadline; returns terminal JSON or None."""
    import httpx

    while True:
        remaining = deadline - _loop_now()
        if remaining <= 0:
            return None
        try:
            response = await client.get(poll_url, headers=headers)
        except httpx.TimeoutException as exc:
            raise ToolHTTPError("m1-poll", "HTTP_TIMEOUT", str(exc)) from exc
        except httpx.RequestError as exc:
            raise ToolHTTPError("m1-poll", "HTTP_UNAVAILABLE", str(exc)) from exc
        if response.status_code >= 400:
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise _map_status_error("m1-poll", exc) from exc
        value = _response_json("m1-poll", response)
        if isinstance(value, dict) and str(value.get("status") or "").lower() in TERMINAL_STATUSES:
            return value
        await asyncio.sleep(min(POLL_INTERVAL_S, max(0.5, remaining)))
    return None


def _pending_result(tool: str, task_id: str, stage: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "pending",
        "processing_stage": stage,
        "document_schema_version": None,
        "schema_version": None,
        "document": None,
        "needs_review": False,
        "overall_confidence": None,
        "poll": True,
        "message": "M1 任务仍在处理中（未达终态）。请使用 get_m1_task/get_m1_batch 轮询该 task_id，不要将其视为已完成。",
    }


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _filter_order_rows(rows: list[dict[str, Any]], payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply manifest forward-compat attribute filters over returned order lines.

    The current M1 service exposes no query parameters for
    interface/length/color/connector/conductor/od; the orchestrator manifests
    declare them.  Until the service grows those filters this adapter applies
    them over the returned full ``line`` JSON (kept in the ``line`` field).
    """
    criteria = {
        param: _normalize_text(payload.get(param))
        for param in _ORDER_ATTRIBUTE_PARAMS
        if payload.get(param) not in (None, "")
    }
    if not criteria:
        return rows

    def matches(row: dict[str, Any]) -> bool:
        line = row.get("line")
        if not isinstance(line, dict):
            return False
        name_attributes = line.get("name_attributes")
        attributes = name_attributes if isinstance(name_attributes, dict) else {}
        top_color = line.get("color")
        for param, expected in criteria.items():
            key = _ORDER_ATTRIBUTE_KEY[param]
            actual = attributes.get(key)
            if actual in (None, "") and key == "color":
                actual = top_color
            if expected not in _normalize_text(actual):
                return False
        return True

    return [row for row in rows if matches(row)]
