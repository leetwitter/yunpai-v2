"""M4 供应商主数据与回复解析：供应商 CRUD、回复原文、规则解析、人工确认与事实落库。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m4_supplier_local.py``

- sha256: ``da055ac1de21eb177b6a81bb5cd07bfab97e674907396b6a8686405caa71f806``
- 规模: 26751 字节 / 582 行
- 归属分片: **M4**（逐工具结论见 ``rows-S5.md``）
- 状态: **骨架（skeleton）** —— 只含出处头、已验证的 import 闭包与待迁清单；
  业务函数由归属分片按 rows 逐工具迁入（M-INFRA 只做基建与裁决，不搬具体业务工具）。

import 闭包核对（相对 V2 main@2d0dfdb）：
- ``.m4b_store.*（7 个符号）`` → m4b_store.py：本次已由 M-INFRA 完整迁入（M4BStore / payload_checksum / parse_iso_date 等）

迁入须知：
- `_actor(ctx)` 读 `ctx['actor']`（INT :45）→ 按 §1.3 兼容取键。
- `ctx['canonical_supplier_code']`（INT :114）V2 不提供 → 业务语义键，由 M4 按 rows-S5.md:100 自行决定，不要塞进 V2 ctx。

本文件的可执行内容 = import 闭包 + PORT_SYMBOLS + PORT_HANDLERS（均为清单，不注册任何工具）。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .m4b_store import (
    M4BStore,
    coerce_int,
    decimal_string,
    parse_iso_date,
    parse_received_at,
    payload_checksum,
    utc_now,
)


#: 本模块在 INT 源里注册的工具名 → handler 符号（清单，**不是**注册表；
#: 迁入后由分片改写为真正的 ``LOCAL_HANDLERS`` 并登记进 ``workers.HANDLERS``）。
PORT_HANDLERS: dict[str, str] = {
    "list_m4_suppliers": "list_m4_suppliers",
    "create_m4_supplier": "create_m4_supplier",
    "update_m4_supplier": "update_m4_supplier",
    "create_m4_supplier_reply": "create_m4_supplier_reply",
    "parse_m4_supplier_reply": "parse_m4_supplier_reply",
    "confirm_m4_supplier_reply": "confirm_m4_supplier_reply",
    "confirm_m4_supplier_fact": "confirm_m4_supplier_fact",
}


#: 待迁符号清单：(符号名, 种类, INT 源行号)。迁移完成后删除本常量。
PORT_SYMBOLS: tuple[tuple[str, str, int], ...] = (
    ("RULE_SUBSET_PROVIDER", "assign", 30),
    ("_SUPPLIER_STATUSES", "assign", 32),
    ("_SUPPLIER_CHANNELS", "assign", 33),
    ("_store", "def", 37),
    ("_tenant", "def", 41),
    ("_actor", "def", 45),
    ("_maybe", "def", 49),
    ("_email_ok", "def", 53),
    ("_supplier_read", "def", 59),
    ("list_m4_suppliers", "async def", 73),  # tool: list_m4_suppliers
    ("create_m4_supplier", "async def", 85),  # tool: create_m4_supplier
    ("update_m4_supplier", "async def", 119),  # tool: update_m4_supplier
    ("create_m4_supplier_reply", "async def", 168),  # tool: create_m4_supplier_reply
    ("_DATE_RE", "assign", 197),
    ("_EXCEPTION_RULES", "assign", 198),
    ("_PRICE_PATTERNS", "assign", 204),
    ("rule_parse_reply", "def", 212),
    ("_parse_result_read", "def", 293),
    ("parse_m4_supplier_reply", "async def", 306),  # tool: parse_m4_supplier_reply
    ("_bool_value", "def", 334),
    ("_confirmed_result", "def", 344),
    ("confirm_m4_supplier_reply", "async def", 370),  # tool: confirm_m4_supplier_reply
    ("_parsed_or_empty", "def", 414),
    ("_apply_confirmed_to_tracking", "def", 427),
    ("confirm_m4_supplier_fact", "async def", 461),  # tool: confirm_m4_supplier_fact
    ("_parse_confidence_decimal", "def", 528),
    ("_fact_replay_or_conflict", "def", 534),
    ("_fact_read", "def", 554),
    ("LOCAL_HANDLERS", "assign", 574),
)
