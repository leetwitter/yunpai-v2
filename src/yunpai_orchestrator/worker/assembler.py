"""唯一装配器（书二 §5.1）——free 与 workflow 共用同一份装配（治痛点 4）。

实现方式：收编 legacy orchestration_bridge.bridge_payload（W913 验证过、
唯一跑通全链的装配实现，迁移说明见 orchestration_bridge.py 头注）。
本模块在其上叠加三层：
1) 显式参数（request[tool]）覆盖桥接值；
2) 合同 required 字段校验，缺口返回结构化 Assembled(blocked)（不再静默空 dict）；
3) 未知/无桥接规则的工具退化为"仅显式参数"（EXPLICIT_ONLY 语义）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..orchestration_bridge import bridge_payload
from ..registry import ToolRegistry


@dataclass
class Assembled:
    blocked: bool
    payload: dict[str, Any] = field(default_factory=dict)
    missing: list[dict[str, Any]] = field(default_factory=list)


def _is_blocked(result: Any) -> bool:
    return isinstance(result, dict) and (
        str(result.get("code") or "").upper() == "BLOCKED_INPUT"
        or (result.get("success") is False and str((result.get("errors") or [{}])[0].get("code", "")).upper() == "BLOCKED_INPUT")
    )


def _missing_from_envelope(result: dict[str, Any]) -> list[dict[str, Any]]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    fields = data.get("missing_fields") or []
    return [{"field": f, "expected_from": data.get("required_tool") or "上游步骤", "hint": data.get("recovery") or ""} for f in fields]


def blocked_envelope(tool: str, missing: list[dict[str, Any]], source: str = "assembler") -> dict[str, Any]:
    """结构化 BLOCKED_INPUT 信封（审查 Agent 据此开 data/blocked_input Gate）。"""
    return {
        "success": False,
        "status": "blocked",
        "code": "BLOCKED_INPUT",
        "errors": [{"code": "BLOCKED_INPUT",
                    "message": f"装配 {tool} 缺少权威输入", "tool": tool}],
        "data": {"tool": tool, "missing": missing, "source": source},
        "invoked_tools": [],
    }


class Assembler:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    async def assemble(self, tool: str, state: dict[str, Any]) -> Assembled:
        request = state.get("request", {})
        explicit = {k: v for k, v in dict(request.get(tool) or {}).items()
                    if v not in (None, "", [], {})}
        bridged = bridge_payload(state, tool)
        if _is_blocked(bridged):
            return Assembled(blocked=True, payload=dict(bridged), missing=_missing_from_envelope(bridged))
        payload: dict[str, Any] = dict(bridged or {})
        payload.update(explicit)

        spec = self.registry.specs.get(tool)
        if spec is not None:
            required = spec.input_schema.get("required") or []
            missing = [{"field": key, "expected_from": "显式参数或前序步骤产出",
                        "hint": f"{tool} 合同必填 {key}"}
                       for key in required
                       if payload.get(key) in (None, "", [], {})]
            if missing:
                return Assembled(blocked=True,
                                 payload=blocked_envelope(tool, missing),
                                 missing=missing)
        return Assembled(blocked=False, payload=payload, missing=[])
