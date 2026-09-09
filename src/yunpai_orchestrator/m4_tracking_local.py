"""M4 交期跟踪与供应快照：跟踪列表、预警扫描、催货文案、供应快照查询/事件页、M5→M4 提案受理。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m4_tracking_local.py``

- sha256: ``8006fdcf3146908a8766a75935081e9ea83b62ac01abb3c0a91254ce1d279965``
- 规模: 25278 字节 / 526 行
- 归属分片: **M4**（逐工具结论见 ``rows-S5.md``）
- 状态: **骨架（skeleton）** —— 只含出处头、已验证的 import 闭包与待迁清单；
  业务函数由归属分片按 rows 逐工具迁入（M-INFRA 只做基建与裁决，不搬具体业务工具）。

import 闭包核对（相对 V2 main@2d0dfdb）：
- ``.m4b_store.*（5 个符号）`` → m4b_store.py：本次已由 M-INFRA 完整迁入（M4BStore / _as_utc_timestamp / input_checksum_plain 等）

迁入须知：
- `receive_m4_schedule_impact_proposal` 属 V2 `binding.py:29-31` 的 ORCHESTRATION_INTERNAL（不进路由目录）；M4 若要保留实现，按 INFRA-DECISIONS §3.3 的「编排内部 handler」写法登记。
- `query_m4_material_supply_snapshot` 是惰性写快照（INT :389-395）→ Gate 声明需与 rows-S5.md 对齐。

本文件的可执行内容 = import 闭包 + PORT_SYMBOLS + PORT_HANDLERS（均为清单，不注册任何工具）。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from .m4b_store import (
    M4BStore,
    _as_utc_timestamp,
    input_checksum_plain,
    payload_checksum,
    utc_now,
)


#: 本模块在 INT 源里注册的工具名 → handler 符号（清单，**不是**注册表；
#: 迁入后由分片改写为真正的 ``LOCAL_HANDLERS`` 并登记进 ``workers.HANDLERS``）。
PORT_HANDLERS: dict[str, str] = {
    "list_m4_tracking": "list_m4_tracking",
    "scan_m4_purchase_alerts": "scan_m4_purchase_alerts",
    "list_m4_purchase_alerts": "list_m4_purchase_alerts",
    "generate_m4_urge_message": "generate_m4_urge_message",
    "query_m4_material_supply_snapshot": "query_m4_material_supply_snapshot",
    "list_m4_material_supply_events": "list_m4_material_supply_events",
    "get_m4_material_supply_snapshot": "get_m4_material_supply_snapshot",
    "receive_m4_schedule_impact_proposal": "receive_m4_schedule_impact_proposal",
}


#: 待迁符号清单：(符号名, 种类, INT 源行号)。迁移完成后删除本常量。
PORT_SYMBOLS: tuple[tuple[str, str, int], ...] = (
    ("URGE_RULE_PROMPT_VERSION", "assign", 32),
    ("_SCHEMA_QUERY", "assign", 34),
    ("_SCHEMA_SNAPSHOT", "assign", 35),
    ("_SCHEMA_EVENT_PAGE", "assign", 36),
    ("_SCHEMA_PROPOSAL", "assign", 37),
    ("_MISSING_LINE_REGISTRY", "assign", 39),
    ("_RECEIVED_OR_CLOSED", "assign", 40),
    ("_store", "def", 43),
    ("_tenant", "def", 47),
    ("_task", "def", 51),
    ("_page_args", "def", 55),
    ("_json_value", "def", 61),
    ("_tracking_item", "def", 76),
    ("list_m4_tracking", "async def", 94),  # tool: list_m4_tracking
    ("_classify_alert", "def", 107),
    ("scan_m4_purchase_alerts", "async def", 116),  # tool: scan_m4_purchase_alerts
    ("_alert_item", "def", 170),
    ("list_m4_purchase_alerts", "async def", 185),  # tool: list_m4_purchase_alerts
    ("_CHANNEL_LABEL", "assign", 197),
    ("_urge_template", "def", 208),
    ("_urge_message_text", "def", 228),
    ("generate_m4_urge_message", "async def", 250),  # tool: generate_m4_urge_message
    ("_parse_datetime", "def", 283),
    ("_normalize_query", "def", 293),
    ("_current_snapshot_id", "def", 321),
    ("_empty_projection", "def", 331),
    ("query_m4_material_supply_snapshot", "async def", 344),  # tool: query_m4_material_supply_snapshot
    ("_response_of", "def", 399),
    ("get_m4_material_supply_snapshot", "async def", 412),  # tool: get_m4_material_supply_snapshot
    ("list_m4_material_supply_events", "async def", 432),  # tool: list_m4_material_supply_events
    ("receive_m4_schedule_impact_proposal", "async def", 464),  # tool: receive_m4_schedule_impact_proposal
    ("_proposal_replay_or_conflict", "def", 508),
    ("LOCAL_HANDLERS", "assign", 517),
)
