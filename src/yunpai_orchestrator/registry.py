from __future__ import annotations

import json
import base64
from pathlib import Path
import os
from typing import Any
from urllib.parse import quote

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - only used in dependency-free smoke environments
    class _SchemaError(ValueError):
        pass

    class _ValidationError(ValueError):
        def __init__(self, message: str):
            super().__init__(message)
            self.message, self.path = message, []

    class Draft202012Validator:  # type: ignore[no-redef]
        def __init__(self, schema: dict[str, Any]): self.schema = schema
        @staticmethod
        def check_schema(schema: dict[str, Any]) -> None:
            if not isinstance(schema, dict) or schema.get("type") not in (None, "object", "array", "string", "number", "integer", "boolean"):
                raise _SchemaError("unsupported or invalid JSON Schema")
        def iter_errors(self, instance: Any):
            expected = self.schema.get("type")
            if expected == "object" and not isinstance(instance, dict): yield _ValidationError("must be object"); return
            if expected == "array" and not isinstance(instance, list): yield _ValidationError("must be array"); return
            if isinstance(instance, dict):
                for key in self.schema.get("required", []):
                    if key not in instance: yield _ValidationError(f"'{key}' is a required property")
        def validate(self, instance: Any) -> None:
            error = next(self.iter_errors(instance), None)
            if error: raise error

from .contracts import HTTP_QUERY_FIELDS, ToolHandler, ToolSpec, normalize_contract_result


def _contract_defaults(module: str, name: str, item: dict[str, Any]) -> dict[str, Any]:
    """Provide safe contract metadata for older manifests during migration."""
    capability_by_module = {
        "m0": "canonical_write",
        "m1": "document_parse",
        "m2": "bom_sop_validate",
        "m3": "mrp_calculate",
        "m4": "procurement_prepare",
        "m5": "schedule_validate",
    }
    gate_by_module = {
        "m0": "candidate", "m1": "data", "m2": "engineering",
        "m3": "data", "m4": "procurement", "m5": "schedule",
    }
    side_effect = str(item.get("side_effect") or (
        "external_write" if any(token in name for token in ("approve", "commit", "dispatch", "write", "send", "publish"))
        else "none"
    ))
    return {
        "capability": str(item.get("capability") or capability_by_module.get(module, "unknown")),
        "side_effect": side_effect,
        "review_gate": str(item.get("review_gate") or ("none" if side_effect == "none" else gate_by_module.get(module, "data"))),
        "failure_codes": tuple(str(code) for code in item.get("failure_codes", [])),
        "recovery_actions": tuple(str(action) for action in item.get("recovery_actions", [])),
        "downstream_fields": tuple(str(field) for field in item.get("downstream_fields", [])),
    }


class ToolHTTPError(RuntimeError):
    """Stable HTTP adapter error surfaced through ToolRegistry and MCP."""

    def __init__(self, tool: str, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(f"{tool}: {code}: {message}")
        self.tool = tool
        self.code = code
        self.status_code = status_code


#: 契约层「禁止远程调用」的稳定错误码（P0-5）。
REMOTE_INVOCATION_FORBIDDEN = "REMOTE_INVOCATION_FORBIDDEN"

#: 标记通用/专用 HTTP 适配器安装的 handler，供 call() 做防御性判定。
_REMOTE_ADAPTER_FLAG = "__yunpai_remote_adapter__"


def remote_invocation_forbidden(spec: ToolSpec) -> bool:
    """该工具是否被契约禁止任何形式的远程执行（P0-5）。

    ``local_only=true`` 或 ``remote_invocation="forbidden"`` 都表示「只能本地执行」：
    ``bind_http`` 不得为其安装 HTTP 适配器，``call()`` 不得把调用转发到远端。
    """
    return bool(spec.local_only) or spec.remote_invocation == "forbidden"


def mark_remote_adapter(handler: ToolHandler) -> ToolHandler:
    """把 handler 标记为 HTTP 适配器（绑定期与执行期的共同判据）。"""
    setattr(handler, _REMOTE_ADAPTER_FLAG, True)
    return handler


def is_remote_adapter(handler: ToolHandler) -> bool:
    return bool(getattr(handler, _REMOTE_ADAPTER_FLAG, False))


class ToolRegistry:
    """全局唯一工具注册表；可加载 JSON manifest 并绑定本地/HTTP handler。"""

    def __init__(self) -> None:
        self.specs: dict[str, ToolSpec] = {}
        self.handlers: dict[str, ToolHandler] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler | None = None) -> None:
        if spec.name in self.specs:
            raise ValueError(f"duplicate tool: {spec.name}")
        try:
            Draft202012Validator.check_schema(spec.input_schema)
        except Exception as exc:
            raise ValueError(f"invalid input schema for {spec.name}: {exc}") from exc
        try:
            Draft202012Validator.check_schema(spec.output_schema)
        except Exception as exc:
            raise ValueError(f"invalid output schema for {spec.name}: {exc}") from exc
        self.specs[spec.name] = spec
        if handler:
            self.handlers[spec.name] = handler

    def load_manifests(self, root: str | Path) -> None:
        paths = sorted(Path(root).glob("m*.well-known/tool.json"))
        if not paths:
            paths = sorted(Path(root).glob("m*.json"))
        # v2：本地识别四件套合同（registry-manifests/local.json，书二 §7.1）
        paths += sorted(Path(root).glob("local.json"))
        for path in paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("tools", []):
                http = item.get("http", {})
                metadata = _contract_defaults(data["module"], item["name"], item)
                self.register(ToolSpec(
                    name=item["name"], module=data["module"], description=item["description"],
                    input_schema=item.get("input_schema", {"type": "object"}),
                    output_schema=item.get("output_schema", {"type": "object"}),
                    execution=item.get("execution", "sync"), base_url=data.get("base_url", ""),
                    method=http.get("method", "POST"), path=http.get("path", ""),
                    timeout_s=float(http.get("timeout_s", 60)), tool_type=item.get("type", "tool"),
                    required_headers=tuple(http.get("required_headers", [])),
                    query_fields=tuple(str(field) for field in http.get("query_fields", [])),
                    agent_endpoints=item.get("agent_endpoints", {}), tags=tuple(item.get("tags", [])),
                    local_only=bool(item.get("local_only", False)),
                    remote_invocation=str(item.get("remote_invocation", "") or ""),
                    remote_invocation_reason=str(item.get("remote_invocation_reason", "") or ""),
                    **metadata,
                ))

    def tools_for(self, module: str) -> list[ToolSpec]:
        return [s for s in self.specs.values() if s.module == module]

    async def call(self, name: str, payload: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        if name not in self.specs:
            raise KeyError(f"unknown tool: {name}")
        spec = self.specs[name]
        # P0-5 防御性断言：契约禁止远程调用的工具，绝不允许经由任何 HTTP 适配器执行。
        # 放在入参校验之前——策略违规优先于参数形状，且保证不发起网络请求。
        leaked = self.handlers.get(name)
        if leaked is not None and remote_invocation_forbidden(spec) and is_remote_adapter(leaked):
            raise ToolHTTPError(
                name,
                REMOTE_INVOCATION_FORBIDDEN,
                spec.remote_invocation_reason
                or "该工具禁止远程调用，只能由本地 handler 执行",
            )
        errors = sorted(Draft202012Validator(spec.input_schema).iter_errors(payload), key=lambda e: list(e.path))
        if errors:
            raise ValueError(f"invalid input for {name}: {errors[0].message}")
        handler = self.handlers.get(name)
        if handler is None:
            raise RuntimeError(f"tool {name} has no local handler; configure HTTP adapter")
        result = await handler(payload, context)
        Draft202012Validator(spec.output_schema).validate(result)
        return normalize_contract_result(result, source=f"tool:{spec.name}", invoked_tools=[spec.name])

    def bind_http(
        self,
        base_urls: dict[str, str],
        *,
        timeout_s: float = 60.0,
        headers_by_module: dict[str, dict[str, str]] | None = None,
        tool_names: set[str] | frozenset[str] | None = None,
        overwrite: bool = True,
    ) -> None:
        """按模块 base URL 将工具绑定为 HTTP handler，并保留合同校验。"""
        for name, spec in self.specs.items():
            # P0-5：契约禁止远程调用的工具永不安装 HTTP 适配器，保留本地 handler。
            # 守卫放在 tool_names 判定之前，因此同时覆盖 build_default_registry
            # （M3_M4_ADAPTER_TOOL_NAMES）与 build_runtime_registry
            # （set(registry.specs) - EXCLUDED - LOCAL_ONLY）两条绑定路径。
            if remote_invocation_forbidden(spec):
                continue
            if tool_names is not None and name not in tool_names:
                continue
            if not overwrite and name in self.handlers:
                continue
            base_url = base_urls.get(spec.module)
            if not base_url:
                continue

            async def http_handler(payload: dict[str, Any], context: dict[str, Any], *, _spec=spec, _base=base_url):
                try:
                    import httpx
                except ImportError as exc:
                    raise RuntimeError("HTTP adapter requires httpx") from exc
                path, body = _spec.path, dict(payload)
                for key in list(body):
                    marker = "{" + key + "}"
                    if marker in path:
                        path = path.replace(marker, quote(str(body.pop(key)), safe=""))
                configured_headers = (headers_by_module or {}).get(_spec.module, {})
                headers = _http_headers(_spec, body, context, configured_headers)
                missing_headers = [key for key in _spec.required_headers if not headers.get(key)]
                if missing_headers:
                    raise ToolHTTPError(
                        _spec.name,
                        "MISSING_REQUIRED_HEADER",
                        f"missing required HTTP headers: {', '.join(missing_headers)}",
                    )
                # The standalone M2 API consumes uploaded source files as
                # base64 JSON and stages them itself; other modules use the
                # generic multipart adapter.
                files = [] if _spec.module == "m2" else _extract_uploads(body)
                request_kwargs: dict[str, Any] = {"headers": headers}
                query_fields = _http_query_fields(_spec)
                query = {
                    key: body.pop(key)
                    for key in query_fields
                    if key in body and body[key] is not None
                }
                if "tenant_id" in query_fields and context.get("tenant_id"):
                    query.setdefault("tenant_id", context["tenant_id"])
                if files:
                    if query:
                        request_kwargs["params"] = query
                    request_kwargs["files"] = files
                    request_kwargs["data"] = {
                        key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                        for key, value in body.items()
                    }
                elif _spec.method.upper() in {"GET", "DELETE"}:
                    request_kwargs["params"] = {**query, **body}
                else:
                    if query:
                        request_kwargs["params"] = query
                    request_kwargs["json"] = body
                try:
                    async with httpx.AsyncClient(timeout=_spec.timeout_s or timeout_s) as client:
                        response = await client.request(_spec.method, _base.rstrip("/") + path, **request_kwargs)
                        response.raise_for_status()
                        value = response.json()
                except httpx.TimeoutException as exc:
                    raise ToolHTTPError(_spec.name, "HTTP_TIMEOUT", str(exc)) from exc
                except httpx.HTTPStatusError as exc:
                    detail = _http_error_detail(exc.response)
                    raise ToolHTTPError(
                        _spec.name,
                        "HTTP_STATUS_ERROR",
                        detail,
                        status_code=exc.response.status_code,
                    ) from exc
                except httpx.RequestError as exc:
                    raise ToolHTTPError(_spec.name, "HTTP_UNAVAILABLE", str(exc)) from exc
                except ValueError as exc:
                    raise ToolHTTPError(_spec.name, "INVALID_JSON_RESPONSE", str(exc)) from exc
                # The standalone M2 web adapter wraps its workflow result in
                # {"result": ...}; expose the declared tool output directly.
                if isinstance(value, dict) and set(value) == {"result"}:
                    return value["result"]
                return value

            self.handlers[name] = mark_remote_adapter(http_handler)

    def mcp_tools(self) -> list[dict[str, Any]]:
        return [spec.as_mcp_tool() for spec in self.specs.values()]

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name, "module": spec.module, "description": spec.description,
                "execution": spec.execution, "type": spec.tool_type,
                "http": {
                    "method": spec.method,
                    "path": spec.path,
                    "timeout_s": spec.timeout_s,
                    "required_headers": list(spec.required_headers),
                },
                "bound": name in self.handlers,
            }
            for name, spec in self.specs.items()
        ]


def build_default_registry() -> ToolRegistry:
    from .m1_tooling import M1_HTTP_ADAPTER_TOOL_NAMES, bind_m1_http
    from .m1_http_adapter import module_m1_auth_headers
    from .m3_m4_tooling import M3_M4_ADAPTER_TOOL_NAMES
    from .workers import HANDLERS
    registry = ToolRegistry()
    # v2：manifest 唯一目录 = 仓库根 registry-manifests（消灭旧双目录漂移，书二 §7.1）
    manifest_root = Path(os.getenv("YUNPAI_MANIFEST_DIR") or Path(__file__).resolve().parents[2] / "registry-manifests")
    registry.load_manifests(manifest_root)
    for name, handler in HANDLERS.items():
        if name in registry.specs:
            registry.handlers[name] = handler
    # The M1 module uses a dedicated adapter: tenant/actor header mapping
    # (X-Tenant-ID/X-Actor-ID/X-Actor-Roles), 202 bounded polling and stable
    # error mapping.  ingest_document keeps its local fixture handler here
    # (overwrite=False) and is replaced by the real M1 HTTP adapter in
    # production transport.
    bind_m1_http(
        registry,
        urls=_module_urls(registry, {"m1"}),
        headers_by_module={"m1": module_m1_auth_headers()},
        tool_names=M1_HTTP_ADAPTER_TOOL_NAMES,
        overwrite=False,
    )
    registry.bind_http(
        _module_urls(registry, {"m3", "m4"}),
        headers_by_module=_module_auth_headers({"m3", "m4"}),
        tool_names=M3_M4_ADAPTER_TOOL_NAMES,
        overwrite=False,
    )
    return registry


def _http_headers(
    spec: ToolSpec,
    payload: dict[str, Any],
    context: dict[str, Any],
    configured: dict[str, str],
) -> dict[str, str]:
    headers = {key: str(value) for key, value in configured.items() if value is not None and str(value)}
    task_id = context.get("task_id")
    if task_id:
        headers["X-Yunpai-Task-ID"] = str(task_id)
    tenant_id = context.get("tenant_id") or payload.get("tenant_id")
    if tenant_id:
        headers["X-Yunpai-Tenant-ID"] = str(tenant_id)
    idempotency_key = context.get("idempotency_key") or payload.get("idempotency_key")
    if idempotency_key:
        headers["Idempotency-Key"] = str(idempotency_key)
    authorization = context.get("authorization") or context.get("Authorization")
    if authorization:
        headers["Authorization"] = str(authorization)
    elif context.get("auth_token"):
        headers["Authorization"] = f"Bearer {context['auth_token']}"
    trace_id = context.get("trace_id") or context.get("run_id")
    if trace_id:
        headers["X-Yunpai-Trace-ID"] = str(trace_id)
    revision = (
        context.get("revision")
        or payload.get("revision")
        or payload.get("expected_revision")
        or payload.get("expected_po_revision")
    )
    if revision is not None:
        headers["X-Yunpai-Revision"] = str(revision)
    checksum = (
        context.get("checksum")
        or payload.get("checksum")
        or payload.get("expected_checksum")
        or payload.get("expected_po_checksum")
        or payload.get("source_plan_checksum")
    )
    if checksum:
        headers["X-Yunpai-Checksum"] = str(checksum)
    if payload.get("source_module"):
        headers["X-Yunpai-Source"] = str(payload["source_module"])
    actor_user = context.get("actor_user") or payload.get("actor_user")
    actor_role = context.get("actor_role") or payload.get("actor_role")
    if actor_user:
        headers["X-Actor-User"] = str(actor_user)
    if actor_role:
        headers["X-Actor-Role"] = str(actor_role)
    return headers


def _http_query_fields(spec: ToolSpec) -> frozenset[str]:
    """HTTP 适配器显式走 query 的字段（R4-REQ-5）。

    单一来源改为 manifest ``http.query_fields``；未声明时回退到
    ``contracts.HTTP_QUERY_FIELDS``（兼容未升级的 manifest）。
    """
    if spec.query_fields:
        return frozenset(spec.query_fields)
    return HTTP_QUERY_FIELDS.get(spec.name, frozenset())


def _http_error_detail(response: Any) -> str:
    try:
        value = response.json()
    except Exception:
        value = getattr(response, "text", "")
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return text[:2000] or f"HTTP {getattr(response, 'status_code', 'error')}"


def _module_urls(registry: ToolRegistry, modules: set[str]) -> dict[str, str]:
    urls: dict[str, str] = {}
    for module in modules:
        tools = registry.tools_for(module)
        default = tools[0].base_url if tools else ""
        value = os.getenv(f"{module.upper()}_URL", default)
        if value:
            urls[module] = value
    return urls


def _module_auth_headers(modules: set[str]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for module in modules:
        authorization = os.getenv(f"{module.upper()}_AUTHORIZATION", "").strip()
        api_key = os.getenv(f"{module.upper()}_API_KEY", "").strip()
        if module == "m5":
            # M5 v2 服务用 APIKeyHeader(X-M5-API-Key)，而非 Authorization Bearer。
            key = api_key or authorization
            if key:
                result[module] = {"X-M5-API-Key": key}
            continue
        if not authorization and api_key:
            authorization = f"Bearer {api_key}"
        if authorization:
            result[module] = {"Authorization": authorization}
    return result


def _extract_uploads(body: dict[str, Any]) -> list[tuple[str, tuple[str, bytes, str]]]:
    uploads: list[tuple[str, tuple[str, bytes, str]]] = []
    for key in list(body):
        value = body[key]
        items = value if isinstance(value, list) else [value]
        if not items or not all(isinstance(item, dict) and isinstance(item.get("content_b64"), str) for item in items):
            continue
        body.pop(key)
        for index, item in enumerate(items):
            try:
                raw = base64.b64decode(item["content_b64"], validate=True)
            except ValueError as exc:
                raise ValueError(f"invalid base64 upload in {key}[{index}]") from exc
            uploads.append((key, (str(item.get("filename") or f"{key}-{index}.bin"), raw, str(item.get("content_type") or "application/octet-stream"))))
    return uploads


# 本地识别四件套：纯本地确定性工具（无 HTTP 端点），生产 HTTP transport 下也必须
# 保持本地 handler，不得被 M0 远程绑定覆盖。
LOCAL_ONLY_TOOLS = frozenset({"sample_file", "ingest_recognized", "query_recognized_table", "ingest_canonical"})


def build_runtime_registry() -> ToolRegistry:
    registry = build_default_registry()
    transport = os.getenv("YUNPAI_TOOL_TRANSPORT", "local").lower()
    env = os.getenv("YUNPAI_ENV", "sandbox").lower()
    if env == "production" and transport != "http":
        # 生产环境护栏：拒绝用 local fixture 当生产工具面；必须由部署方提供 M0-M5 URL。
        raise RuntimeError("YUNPAI_ENV=production 要求 YUNPAI_TOOL_TRANSPORT=http（本地 fixture 仅限 sandbox/preview）")
    if transport == "http":
        from .m1_tooling import M1_HTTP_ADAPTER_TOOL_NAMES, bind_m1_http
        from .m1_http_adapter import module_m1_auth_headers
        from .m3_m4_tooling import EXCLUDED_M3_M4_TOOL_NAMES
        from .workers import HANDLERS

        # 选择性 HTTP 模块绑定（YUNPAI_HTTP_MODULES 控制哪些模块走远程）。
        selected = {
            item.strip().lower()
            for item in os.getenv("YUNPAI_HTTP_MODULES", "m0,m1,m2,m3,m4,m5").split(",")
            if item.strip()
        }
        # This orchestrator may keep the M4 business capability inside this
        # process.  The transport remains HTTP for the other modules, while M4
        # is served by the registered Tool handler and does not depend on a
        # foreign M4 container or service token.
        local_m4 = os.getenv("YUNPAI_LOCAL_M4", "").strip().lower() in {"1", "true", "yes", "on"}
        if local_m4:
            selected.discard("m4")
        registry.bind_http(
            _module_urls(registry, selected),
            headers_by_module=_module_auth_headers(selected),
            tool_names=set(registry.specs) - set(EXCLUDED_M3_M4_TOOL_NAMES) - set(LOCAL_ONLY_TOOLS),
        )
        if "m1" in selected:
            # Replace the generic HTTP binding for M1 with the dedicated M1
            # adapter so tenant/actor headers, 202 polling and error mapping
            # follow the standalone M1 service contract.
            bind_m1_http(
                registry,
                urls=_module_urls(registry, {"m1"}),
                headers_by_module={"m1": module_m1_auth_headers()},
                tool_names=M1_HTTP_ADAPTER_TOOL_NAMES,
                overwrite=True,
            )
        # build_default_registry installs the dedicated M3/M4 adapters so
        # callers can use them without the full runtime builder.  In the
        # runtime builder, however, YUNPAI_HTTP_MODULES is the source of truth:
        # restore local handlers for explicitly unselected modules instead of
        # silently leaving a pre-bound HTTP adapter in place.
        for name, handler in HANDLERS.items():
            spec = registry.specs.get(name)
            if spec is not None and spec.module not in selected:
                registry.handlers[name] = handler
        registry.environment = {
            "env": env,
            "transport": "http",
            "local_fixture": False,
            "local_modules": ["m4"] if local_m4 else [],
        }  # type: ignore[attr-defined]
    else:
        registry.environment = {"env": env, "transport": "local", "local_fixture": True}  # type: ignore[attr-defined]
    return registry
