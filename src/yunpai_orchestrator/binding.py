"""绑定状态表（书二 §7.2）——路由目录只含已绑定工具（治痛点 5/7）。

四态：BOUND_LOCAL（本地 handler，生产可用）/ BOUND_HTTP（HTTP 适配器已绑）/
SANDBOX（本地假实现，仅联调，生产不可见）/ UNBOUND（不得进路由目录）。
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Any

from .registry import ToolRegistry


class BindingStatus(str, Enum):
    BOUND_LOCAL = "bound_local"
    BOUND_HTTP = "bound_http"
    SANDBOX = "sandbox"
    UNBOUND = "unbound"


#: 合同名保留但禁止路由（legacy 名曾误导 LLM 提案，书二 §7.3 M3 表）。
DEPRECATED_TOOLS: frozenset[str] = frozenset({"run_mrp_procurement_plan"})

#: 故意不绑定（需真实 M5 服务才能语义正确，旧 workers.py:792 注释沿用）。
INTENTIONALLY_UNBOUND: frozenset[str] = frozenset({"report_workload", "bind_worker_to_order"})

#: M5→M3/M4 反向反馈面：编排内部使用，不对外注册进路由目录。
ORCHESTRATION_INTERNAL: frozenset[str] = frozenset({
    "receive_m3_material_demand", "receive_m4_schedule_impact_proposal",
})

#: 本地假实现明示（联调可用，生产环境从目录剔除）。
SANDBOX_TOOLS: frozenset[str] = frozenset({"import_m4_purchase_suggestions_json"})

#: 默认不进路由目录（书二 §7.3 ○：审批类写动作只由人工/编排显式调用，不得被 LLM 提案）。
#: 注意：这些工具**仍是已绑定**（绑定态照实报 BOUND_HTTP），只是不出现在路由目录里。
DEFAULT_ROUTER_HIDDEN: frozenset[str] = frozenset({
    "approve_m3_task",
    "reject_m3_task",
    "request_change_m3_task",
    "approve_to_send_m3_task",
})


def _local_fixture_names() -> set[str]:
    from .workers import HANDLERS
    return set(HANDLERS)


def compute_bindings(registry: ToolRegistry) -> dict[str, BindingStatus]:
    local_fixtures = _local_fixture_names()
    production = os.getenv("YUNPAI_ENV", "sandbox").lower() == "production"
    bindings: dict[str, BindingStatus] = {}
    for name in registry.specs:
        if name in DEPRECATED_TOOLS or name in INTENTIONALLY_UNBOUND or name in ORCHESTRATION_INTERNAL:
            bindings[name] = BindingStatus.UNBOUND
        elif name in local_fixtures:
            bindings[name] = (
                BindingStatus.SANDBOX if name in SANDBOX_TOOLS else BindingStatus.BOUND_LOCAL
            )
        elif registry.handlers.get(name) is not None:
            bindings[name] = BindingStatus.BOUND_HTTP
        else:
            bindings[name] = BindingStatus.UNBOUND
    # SANDBOX 在生产环境整体降级为 UNBOUND（不可见）。
    if production:
        bindings = {
            name: (BindingStatus.UNBOUND if status == BindingStatus.SANDBOX else status)
            for name, status in bindings.items()
        }
    return bindings


def visible_tool_names(registry: ToolRegistry) -> list[str]:
    bindings = compute_bindings(registry)
    return [
        name for name, status in bindings.items()
        if status in (BindingStatus.BOUND_LOCAL, BindingStatus.BOUND_HTTP, BindingStatus.SANDBOX)
        and name not in DEFAULT_ROUTER_HIDDEN
    ]


class CatalogView:
    """给 LLM 路由器看的"瘦身注册表"：只暴露可见工具（书二 §4.1）。

    QwenRouter.classify 遍历 ``registry.specs.values()``——用本视图替换后，
    LLM 提案里不可能出现 UNBOUND/DEPRECATED 工具名。
    """

    def __init__(self, registry: ToolRegistry, visible: list[str] | None = None):
        visible = visible if visible is not None else visible_tool_names(registry)
        self.specs: dict[str, Any] = {name: registry.specs[name] for name in visible if name in registry.specs}


def describe(registry: ToolRegistry) -> dict[str, list[str]]:
    bindings = compute_bindings(registry)
    out: dict[str, list[str]] = {status.value: [] for status in BindingStatus}
    for name, status in bindings.items():
        out[status.value].append(name)
    return {k: sorted(v) for k, v in out.items()}
