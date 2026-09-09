"""M3/M4 工具面声明（Skill 层与注册表共用）。

M3 分片（R6 批次）迁移增量：本文件原为 V2 的「硬编码名单版」（137 行，三份 M3 名单
各自手写、互相漂移，且缺 ``M3_LEGACY_DECISIONS``）。按 ``_migration/INFRA-DECISIONS.md``
§2.4 的裁定，**只增量补常量、禁止整文件覆盖**：下面新增 ``M3_TOOL_DECLARATIONS`` 单一
声明源，并把 M3 的三份名单改为从声明派生（与 INT 版
``_wt/INT/src/yunpai_langgraph/m3_m4_tooling.py:44-276`` 同形）；M4 段保持 V2 原样未动。

派生语义（沿用 INT 的既有约定）：

- ``surface="skill"``        → 进入 Skill operation 映射与 HTTP adapter 名单；
- ``surface="orchestrator"`` → 只由编排层/MCP 直接调用，不暴露为 Skill operation；
- ``outcome``                → 本地实现能否产出业务数据（real/unavailable/empty/not_found），
  供 ``M3_LEGACY_DECISIONS``（``m3_local._legacy_block``）与只读名单派生使用。
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# M3 单一声明源（每个 M3 工具一条决策；顺序与 registry-manifests/m3.json 一致）
# ---------------------------------------------------------------------------

M3_TOOL_DECLARATIONS: dict[str, dict[str, Any]] = {
    "run_m3_procurement_requirements": {
        "kind": "formal",
        "surface": "skill",
        "outcome": "real",
        "read_only": False,
        "replacement": "",
        "reason": "正式 M3 需求计算入口（订单 + BOM → 缺口 → M4）",
        "skill_operations": ("default", "requirements", "mrp"),
    },
    "run_mrp_procurement_plan": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "unavailable",
        "failure_code": "LEGACY_UNAVAILABLE",
        "read_only": False,
        "replacement": "run_m3_procurement_requirements",
        "reason": "旧版聚合 MRP bundle 引擎未本地化；新流程必须使用 run_m3_procurement_requirements",
        "skill_operations": ("legacy_plan",),
    },
    "list_m3_orders": {
        "kind": "legacy",
        "legacy_status": "keep",
        "surface": "skill",
        "outcome": "real",
        "read_only": True,
        "replacement": "",
        "reason": "保留兼容：读 canonical order 事实，仍是可用的只读查询",
        "skill_operations": ("orders",),
    },
    "get_m3_order": {
        "kind": "legacy",
        "legacy_status": "keep",
        "surface": "skill",
        "outcome": "real",
        "read_only": True,
        "replacement": "",
        "reason": "保留兼容：读 canonical order 事实，仍是可用的只读查询",
        "skill_operations": ("order",),
    },
    "get_m3_procurement_plan": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "unavailable",
        "failure_code": "LEGACY_UNAVAILABLE",
        "read_only": False,
        "replacement": "run_m3_procurement_requirements",
        "reason": "无 legacy 计划重算引擎/落库写入方；正式需求计算用 run_m3_procurement_requirements",
        "skill_operations": ("plan",),
    },
    "get_persisted_m3_plan": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "unavailable",
        "failure_code": "PERSISTED_PLAN_NOT_FOUND",
        "read_only": False,
        "replacement": "run_m3_procurement_requirements",
        "reason": "run_m3_procurement_requirements 尚未把计划落 m3_store，无持久化计划可读",
        "skill_operations": ("persisted_plan",),
    },
    "get_material_readiness_snapshot": {
        "kind": "formal",
        "surface": "skill",
        "outcome": "real",
        "read_only": True,
        "replacement": "",
        "reason": "正式齐套快照（canonical 事实确定性计算 + 持久化）",
        "skill_operations": ("readiness_snapshot",),
    },
    "get_material_readiness": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "unavailable",
        "failure_code": "LEGACY_UNAVAILABLE",
        "read_only": False,
        "replacement": "get_material_readiness_snapshot",
        "reason": "旧 handoff 段依赖 legacy MRP bundle，本地无写入方；齐套请用 get_material_readiness_snapshot",
        "skill_operations": ("readiness", "readiness_summary"),
    },
    "get_pr_po_drafts": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "unavailable",
        "failure_code": "LEGACY_UNAVAILABLE",
        "read_only": False,
        "replacement": "list_m4_purchase_orders",
        "reason": "PR/PO 草稿段依赖 legacy bundle 审批序；采购执行职责已迁移 M4",
        "skill_operations": ("drafts",),
    },
    "get_m3_approval_tasks": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "empty",
        "read_only": False,
        "replacement": "list_m4_purchase_orders",
        "reason": "正式 M3 不再产生审批任务；本地无审批任务写入方（恒空）",
        "skill_operations": ("approval_tasks",),
    },
    "approve_m3_task": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "not_found",
        "failure_code": "TASK_NOT_FOUND",
        "read_only": False,
        "replacement": "approve_m4_purchase_order",
        "reason": "审批任务仅由 legacy bundle 运行产生，本地无写入方；采购审批走 M4",
        "skill_operations": ("approve",),
    },
    "reject_m3_task": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "not_found",
        "failure_code": "TASK_NOT_FOUND",
        "read_only": False,
        "replacement": "",
        "reason": "旧审批族无本地任务；M4 只有 approve/request_changes，无等价驳回工具",
        "skill_operations": ("reject",),
    },
    "request_change_m3_task": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "not_found",
        "failure_code": "TASK_NOT_FOUND",
        "read_only": False,
        "replacement": "request_changes_m4_purchase_order",
        "reason": "旧审批族无本地任务；修改请求走 M4 采购单评审",
        "skill_operations": ("request_change",),
    },
    "approve_to_send_m3_task": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "not_found",
        "failure_code": "TASK_NOT_FOUND",
        "read_only": False,
        "replacement": "send_m4_purchase_order",
        "reason": "旧审批族无本地任务，且 A-5 发送语义挂起；真实发送走 M4",
        "skill_operations": ("approve_to_send",),
    },
    "get_m3_m4_handoffs": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "empty",
        "read_only": False,
        "replacement": "run_m3_procurement_requirements",
        "reason": "handoff 记录仅由已停用的 legacy 直连端点产生（恒空）；正式交接看 run_m3_procurement_requirements.shortage_lines",
        "skill_operations": ("handoffs", "handoff"),
    },
    "export_m3_procurement_suggestions": {
        "kind": "legacy",
        "legacy_status": "deprecated",
        "surface": "skill",
        "outcome": "unavailable",
        "failure_code": "M3_PLAN_REVISION_UNAVAILABLE",
        "read_only": False,
        "replacement": "run_m3_procurement_requirements",
        "reason": "无匹配的计划快照可导出（run_m3 落库后启用）；正式交接用 run_m3_procurement_requirements.shortage_lines",
        "skill_operations": ("export",),
    },
    "receive_m3_material_demand": {
        "kind": "formal",
        "surface": "orchestrator",
        "outcome": "real",
        "read_only": False,
        "replacement": "",
        "reason": "接收型端点，由编排层/MCP 直接调用；skills/m3/SKILL.md 明确不属于 Skill operation 面",
        "skill_operations": (),
    },
}


def _is_skill_read(name: str) -> bool:
    declaration = M3_TOOL_DECLARATIONS[name]
    return bool(declaration["read_only"]) and declaration["outcome"] == "real"


#: 工具全集（17 个，顺序与 registry-manifests/m3.json 一致；test 锁定两者相等）。
M3_TOOL_NAMES: tuple[str, ...] = tuple(M3_TOOL_DECLARATIONS)

#: 暴露给 Skill operation 面的工具（receive 类除外，与 M4_ADAPTER_TOOL_NAMES 先例一致）。
M3_SKILL_SURFACE_TOOL_NAMES: tuple[str, ...] = tuple(
    name for name, declaration in M3_TOOL_DECLARATIONS.items()
    if declaration["surface"] == "skill"
)

#: HTTP adapter 名单：与 Skill 面保持一致（V2 旧名单缺 run_m3_procurement_requirements）。
M3_ADAPTER_TOOL_NAMES: tuple[str, ...] = M3_SKILL_SURFACE_TOOL_NAMES

M4_ADAPTER_TOOL_NAMES = (
    "import_m4_purchase_suggestions_json",
    "list_m4_purchase_suggestions",
    "generate_m4_purchase_orders",
    "list_m4_purchase_orders",
    "get_m4_purchase_order",
    "submit_m4_purchase_order_review",
    "approve_m4_purchase_order",
    "request_changes_m4_purchase_order",
    "generate_m4_purchase_inquiry_message",
    "send_m4_purchase_order",
    "create_m4_supplier_reply",
    "parse_m4_supplier_reply",
    "confirm_m4_supplier_reply",
    "list_m4_suppliers",
    "create_m4_supplier",
    "update_m4_supplier",
    "list_m4_tracking",
    "scan_m4_purchase_alerts",
    "list_m4_purchase_alerts",
    "generate_m4_urge_message",
    "query_m4_material_supply_snapshot",
    "list_m4_material_supply_events",
    "get_m4_material_supply_snapshot",
    "confirm_m4_supplier_fact",
)

M3_M4_ADAPTER_TOOL_NAMES = frozenset(M3_ADAPTER_TOOL_NAMES + M4_ADAPTER_TOOL_NAMES)
EXCLUDED_M3_M4_TOOL_NAMES = frozenset({
    "receive_m3_material_demand",
    "receive_m4_schedule_impact_proposal",
})


M3_SKILL_OPERATION_MAP = {
    operation: name
    for name, declaration in M3_TOOL_DECLARATIONS.items()
    for operation in declaration["skill_operations"]
}

#: 只读 operation：仅当目标工具既能真实产出数据、又无副作用时才只读
#: （V2 旧名单把恒失败的 readiness/readiness_summary/plan 等也当只读，R4 已收敛）。
M3_READ_ONLY_SKILL_OPERATIONS: frozenset[str] = frozenset(
    operation for operation, name in M3_SKILL_OPERATION_MAP.items() if _is_skill_read(name)
)

M3_LEGACY_TOOL_NAMES: frozenset[str] = frozenset(
    name for name, declaration in M3_TOOL_DECLARATIONS.items()
    if declaration["kind"] == "legacy"
)

#: 本地不产出业务数据的工具（恒失败/恒空/恒 TASK_NOT_FOUND）。
M3_NO_BUSINESS_DATA_TOOLS: frozenset[str] = frozenset(
    name for name, declaration in M3_TOOL_DECLARATIONS.items()
    if declaration["outcome"] != "real"
)

#: LEGACY 工具的逐条决策，供 ``m3_local._legacy_block`` 在结果里附稳定提示。
M3_LEGACY_DECISIONS: dict[str, dict[str, Any]] = {
    name: {
        "status": declaration["legacy_status"],
        "replacement": declaration["replacement"],
        "reason": declaration["reason"],
        "outcome": declaration["outcome"],
        "failure_code": declaration.get("failure_code", ""),
    }
    for name, declaration in M3_TOOL_DECLARATIONS.items()
    if declaration["kind"] == "legacy"
}

M4_SKILL_OPERATION_MAP = {
    "default": "import_m4_purchase_suggestions_json",
    "import": "import_m4_purchase_suggestions_json",
    "suggestions": "list_m4_purchase_suggestions",
    "generate_orders": "generate_m4_purchase_orders",
    "orders": "list_m4_purchase_orders",
    "order": "get_m4_purchase_order",
    "submit_review": "submit_m4_purchase_order_review",
    "approve": "approve_m4_purchase_order",
    "request_changes": "request_changes_m4_purchase_order",
    "inquiry": "generate_m4_purchase_inquiry_message",
    "send": "send_m4_purchase_order",
    "create_reply": "create_m4_supplier_reply",
    "parse_reply": "parse_m4_supplier_reply",
    "supplier_reply": "parse_m4_supplier_reply",
    "confirm_reply": "confirm_m4_supplier_reply",
    "suppliers": "list_m4_suppliers",
    "create_supplier": "create_m4_supplier",
    "update_supplier": "update_m4_supplier",
    "tracking": "list_m4_tracking",
    "scan_alerts": "scan_m4_purchase_alerts",
    "alerts": "list_m4_purchase_alerts",
    "urge": "generate_m4_urge_message",
    "supply": "query_m4_material_supply_snapshot",
    "supply_events": "list_m4_material_supply_events",
    "supply_snapshot": "get_m4_material_supply_snapshot",
    "confirm_fact": "confirm_m4_supplier_fact",
}

M4_READ_ONLY_SKILL_OPERATIONS = frozenset({
    "suggestions",
    "orders",
    "order",
    "suppliers",
    "tracking",
    "alerts",
    "supply",
    "supply_events",
    "supply_snapshot",
})


def unique_tools(operation_map: dict[str, str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(operation_map.values()))
