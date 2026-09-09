"""M3 物料计划本地实现：齐套快照计算（canonical order/bom/inventory）、MRP 需求、只读读口与 legacy 占位。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m3_local.py``

- sha256: ``e5ae0ac7c97abb9657ea5306966772c3ca9634dc1509545d1ad492851d1e4204``
- 规模: 56886 字节 / 1231 行
- 归属分片: **M3**（逐工具结论见 ``rows-S4.md``）
- 状态: **骨架（skeleton）** —— 只含出处头、已验证的 import 闭包与待迁清单；
  业务函数由归属分片按 rows 逐工具迁入（M-INFRA 只做基建与裁决，不搬具体业务工具）。

import 闭包核对（相对 V2 main@2d0dfdb）：
- ``.m3_store.M3Store / M3FeedbackConflictError`` → m3_store.py:85 / :67：本次已由 M-INFRA 完整迁入
- ``.m0_backend.M0Store`` → m0_backend.py:91：存在；`list_entities()` 返回 `{entities:[...]}`，与 INT `_canonical_entities`（:115-118）期望一致
- ``.m3_m4_tooling.M3_LEGACY_DECISIONS`` → **V2 缺失**：缺口：V2 m3_m4_tooling.py（137 行）是硬编码名单版，INT 版（357 行）才有该符号（INT :266） → 见 INFRA-DECISIONS §2.4，由 M3 补齐或改 import

迁入须知：
- `_store_path()` 读 `ctx['m3_db_path']`（INT :93-95），V2 的 tool_context 不提供该键（回退 `YUNPAI_M3_DB`）→ 按 INFRA-DECISIONS §1.3 的 ctx 兼容取键处理。
- `get_material_readiness_snapshot` 是真实实现（INT :625-671 + `_compute_snapshot` :291-570），V2 无同名 handler → M3 迁入后按裁决 3 模板注册。

本文件的可执行内容 = import 闭包 + PORT_SYMBOLS + PORT_HANDLERS（均为清单，不注册任何工具）。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

# TODO(M3)：V2 的 m3_m4_tooling.py 无该符号（见 INFRA-DECISIONS §2.4），
#           M3 补齐后再恢复下面这行 import。
# from .m3_m4_tooling import M3_LEGACY_DECISIONS
from .m3_store import M3FeedbackConflictError, M3Store


#: 本模块在 INT 源里注册的工具名 → handler 符号（清单，**不是**注册表；
#: 迁入后由分片改写为真正的 ``LOCAL_HANDLERS`` 并登记进 ``workers.HANDLERS``）。
PORT_HANDLERS: dict[str, str] = {
    "get_material_readiness_snapshot": "get_material_readiness_snapshot",
    "receive_m3_material_demand": "receive_m3_material_demand",
    "run_mrp_procurement_plan": "run_mrp_procurement_plan",
    "list_m3_orders": "list_m3_orders",
    "get_m3_order": "get_m3_order",
    "get_m3_procurement_plan": "get_m3_procurement_plan",
    "get_persisted_m3_plan": "get_persisted_m3_plan",
    "get_material_readiness": "get_material_readiness",
    "get_pr_po_drafts": "get_pr_po_drafts",
    "get_m3_approval_tasks": "get_m3_approval_tasks",
    "approve_m3_task": "approve_m3_task",
    "reject_m3_task": "reject_m3_task",
    "request_change_m3_task": "request_change_m3_task",
    "approve_to_send_m3_task": "approve_to_send_m3_task",
    "get_m3_m4_handoffs": "get_m3_m4_handoffs",
    "export_m3_procurement_suggestions": "export_m3_procurement_suggestions",
}


#: 待迁符号清单：(符号名, 种类, INT 源行号)。迁移完成后删除本常量。
PORT_SYMBOLS: tuple[tuple[str, str, int], ...] = (
    ("SCHEMA_VERSION", "assign", 36),
    ("CALCULATION_VERSION", "assign", 37),
    ("_RELEASED_QC", "assign", 42),
    ("_EPS", "assign", 44),
    ("_now_iso", "def", 51),
    ("_trace", "def", 55),
    ("_err", "def", 59),
    ("_num", "def", 63),
    ("_num_strict", "def", 72),
    ("_fmt_qty", "def", 81),
    ("_sha256_text", "def", 85),
    ("_canonical_json", "def", 89),
    ("_store_path", "def", 93),
    ("_canonical_path", "def", 98),
    ("_canonical_entities", "def", 105),
    ("_unpack", "def", 121),
    ("_business_key", "def", 136),
    ("_find_order", "def", 145),
    ("_order_demand_products", "def", 164),
    ("_bom_for_product", "def", 211),
    ("_inventory_lots", "def", 259),
    ("_compute_snapshot", "def", 291),
    ("_unknown_snapshot", "def", 572),
    ("_next_snapshot_version", "def", 611),
    ("get_material_readiness_snapshot", "async def", 625),  # tool: get_material_readiness_snapshot
    ("receive_m3_material_demand", "async def", 675),  # tool: receive_m3_material_demand
    ("_LEGACY_UNAVAILABLE_NOTE", "assign", 761),
    ("_unavailable", "def", 767),
    ("_evidence_m3", "def", 778),
    ("_legacy_block", "def", 784),
    ("_legacy_marked", "def", 807),
    ("_order_demand_view", "def", 812),
    ("run_mrp_procurement_plan", "async def", 856),  # tool: run_mrp_procurement_plan
    ("list_m3_orders", "async def", 896),  # tool: list_m3_orders
    ("get_m3_order", "async def", 914),  # tool: get_m3_order
    ("get_m3_procurement_plan", "async def", 936),  # tool: get_m3_procurement_plan
    ("get_persisted_m3_plan", "async def", 953),  # tool: get_persisted_m3_plan
    ("get_material_readiness", "async def", 965),  # tool: get_material_readiness
    ("get_pr_po_drafts", "async def", 978),  # tool: get_pr_po_drafts
    ("_LEGACY_APPROVER_ROLES", "assign", 991),
    ("get_m3_approval_tasks", "async def", 995),  # tool: get_m3_approval_tasks
    ("_actor_checks", "def", 1012),
    ("_approval_action", "def", 1032),
    ("approve_m3_task", "assign", 1049),  # tool: approve_m3_task
    ("reject_m3_task", "assign", 1052),  # tool: reject_m3_task
    ("request_change_m3_task", "assign", 1053),  # tool: request_change_m3_task
    ("approve_to_send_m3_task", "assign", 1054),  # tool: approve_to_send_m3_task
    ("get_m3_m4_handoffs", "async def", 1059),  # tool: get_m3_m4_handoffs
    ("export_m3_procurement_suggestions", "async def", 1077),  # tool: export_m3_procurement_suggestions
    ("_M2_ORDER_REQUIRED", "assign", 1119),
    ("m2_package_to_order_bom", "def", 1122),
    ("open_po_quantities", "def", 1196),
    ("LOCAL_HANDLERS", "assign", 1214),
)
