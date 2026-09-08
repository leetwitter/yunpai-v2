from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

ToolHandler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]

CONTRACT_STATUSES = frozenset({"success", "candidate", "blocked", "conflict", "failed"})


class ContractList(list):
    """List-shaped transport result with non-invasive Goal metadata."""

    def __init__(self, values: list[Any], metadata: dict[str, Any]) -> None:
        super().__init__(values)
        self.contract = metadata

    def get(self, key: str, default: Any = None) -> Any:
        return self.contract.get(key, default)


def normalize_contract_result(
    result: Any,
    *,
    source: str,
    invoked_tools: list[str] | None = None,
    status: str = "success",
) -> dict[str, Any]:
    """Add the Goal-mode result envelope without hiding legacy payload fields."""
    if isinstance(result, list):
        metadata = {
            "status": status,
            "code": None,
            "missing_fields": [],
            "conflicts": [],
            "next_actions": [],
            "evidence": [],
            "source": {"kind": source},
            "gate": {},
            "idempotency": {},
            "execution_mode": "goal",
            "decision_source": "skill_tool_contract",
            "agent_route_mutation": False,
            "invoked_tools": list(invoked_tools or []),
        }
        return ContractList(result, metadata)
    if not isinstance(result, dict):
        result = {"data": result}
    output = dict(result)
    current_status = str(output.get("status") or status)
    if current_status not in CONTRACT_STATUSES:
        current_status = status
    output.setdefault("status", current_status)
    output.setdefault("code", None)
    output.setdefault("data", dict(result))
    output.setdefault("missing_fields", [])
    output.setdefault("conflicts", [])
    output.setdefault("next_actions", [])
    output.setdefault("evidence", [])
    output.setdefault("source", {"kind": source})
    output.setdefault("gate", {})
    output.setdefault("idempotency", {})
    output.setdefault("execution_mode", "goal")
    output.setdefault("decision_source", "skill_tool_contract")
    output.setdefault("agent_route_mutation", False)
    if invoked_tools is not None:
        output.setdefault("invoked_tools", list(invoked_tools))
    return output


@dataclass(frozen=True)
class ToolSpec:
    name: str
    module: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    execution: str = "sync"
    base_url: str = ""
    method: str = "POST"
    path: str = ""
    timeout_s: float = 60.0
    required_headers: tuple[str, ...] = field(default_factory=tuple)
    tool_type: str = "tool"
    agent_endpoints: dict[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = field(default_factory=tuple)
    capability: str = ""
    side_effect: str = "none"
    review_gate: str = ""
    failure_codes: tuple[str, ...] = field(default_factory=tuple)
    recovery_actions: tuple[str, ...] = field(default_factory=tuple)
    downstream_fields: tuple[str, ...] = field(default_factory=tuple)

    def as_mcp_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": f"[{self.module}] {self.description}",
            "inputSchema": self.input_schema,
            "annotations": {
                "module": self.module, "execution": self.execution, "type": self.tool_type,
                "capability": self.capability, "side_effect": self.side_effect,
                "review_gate": self.review_gate,
                "failure_codes": list(self.failure_codes),
                "recovery_actions": list(self.recovery_actions),
                "downstream_fields": list(self.downstream_fields),
            },
        }
