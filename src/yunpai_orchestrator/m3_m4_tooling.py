from __future__ import annotations


M3_ADAPTER_TOOL_NAMES = (
    "run_mrp_procurement_plan",
    "list_m3_orders",
    "get_m3_order",
    "get_m3_procurement_plan",
    "get_persisted_m3_plan",
    "get_material_readiness_snapshot",
    "get_material_readiness",
    "get_pr_po_drafts",
    "get_m3_approval_tasks",
    "approve_m3_task",
    "reject_m3_task",
    "request_change_m3_task",
    "approve_to_send_m3_task",
    "get_m3_m4_handoffs",
    "export_m3_procurement_suggestions",
)

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
    # ``send_m4_purchase_order`` 不在 HTTP 适配器名单（P0-5）：manifest 声明
    # ``local_only=true`` + ``remote_invocation=forbidden``，registry 拒绝远程绑定/调用，
    # 只保留本地 legacy handler（无 TaskID 的旧 PO 记录出站，不实际发送）。
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
    "default": "run_m3_procurement_requirements",
    "requirements": "run_m3_procurement_requirements",
    "mrp": "run_m3_procurement_requirements",
    "legacy_plan": "run_mrp_procurement_plan",
    "orders": "list_m3_orders",
    "order": "get_m3_order",
    "plan": "get_m3_procurement_plan",
    "persisted_plan": "get_persisted_m3_plan",
    "readiness_snapshot": "get_material_readiness_snapshot",
    "readiness": "get_material_readiness",
    "readiness_summary": "get_material_readiness",
    "drafts": "get_pr_po_drafts",
    "approval_tasks": "get_m3_approval_tasks",
    "approve": "approve_m3_task",
    "reject": "reject_m3_task",
    "request_change": "request_change_m3_task",
    "approve_to_send": "approve_to_send_m3_task",
    "handoffs": "get_m3_m4_handoffs",
    "handoff": "get_m3_m4_handoffs",
    "export": "export_m3_procurement_suggestions",
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
    # ``send`` 已摘除（P0-5）：send_m4_purchase_order 是 local_only/remote_invocation=forbidden
    # 的 legacy 兼容面，不得作为 Skill 出口暴露；唯一合法发送出口 = generate_m4_purchase_inquiry_message。
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

M3_READ_ONLY_SKILL_OPERATIONS = frozenset({
    "orders",
    "order",
    "plan",
    "persisted_plan",
    "readiness_snapshot",
    "readiness",
    "readiness_summary",
    "drafts",
    "approval_tasks",
    "handoffs",
    "handoff",
    "export",
})

M4_READ_ONLY_SKILL_OPERATIONS = frozenset({
    "suggestions",
    "orders",
    "order",
    "suppliers",
    "tracking",
    "alerts",
    # ``supply``（query_m4_material_supply_snapshot）已摘除（P0-1）：该「查询」会惰性
    # 持久化供应快照（m4_tracking_local.query_m4_material_supply_snapshot），属写操作，
    # 不得列入只读白名单绕过授权门。
    "supply_events",
    "supply_snapshot",
})


def unique_tools(operation_map: dict[str, str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(operation_map.values()))
