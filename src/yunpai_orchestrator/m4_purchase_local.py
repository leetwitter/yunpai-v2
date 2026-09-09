"""M4 采购本地实现：建议导入（JSON/CSV）、PO 生成与审核流转、询价消息、发送守卫。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m4_purchase_local.py``

- sha256: ``116f20cca105bb2a953ec038769541258b153fabbc2881686b6f14c5dddffe77``
- 规模: 33786 字节 / 762 行
- 归属分片: **M4**（逐工具结论见 ``rows-S5.md``）
- 状态: **骨架（skeleton）** —— 只含出处头、已验证的 import 闭包与待迁清单；
  业务函数由归属分片按 rows 逐工具迁入（M-INFRA 只做基建与裁决，不搬具体业务工具）。

import 闭包核对（相对 V2 main@2d0dfdb）：
- ``.m4_store.*（9 个符号）`` → m4_store.py：本次已由 M-INFRA 完整迁入（M4Store / parse_suggestions_csv / command_digest 等）
- ``_Decimal / StringIO / base64 / csv`` → 标准库：无需处理

迁入须知：
- `_actor(ctx)` 读 `ctx['actor']`（INT :88-89），V2 只给 `actor_user` → 按 §1.3 兼容取键。
- `send_m4_purchase_order` 的本地守卫（INT :498-503）与 `local_only` 契约声明必须一并迁入（rows-S5.md 第 79 行 P0-5）。
- `import_m4_purchase_suggestions_json` 替换 V2 的旧 echo 版（V2 workers.py:469-483），并同步 `binding.py` 的 SANDBOX_TOOLS 名单（V2 binding.py:34）。

本文件的可执行内容 = import 闭包 + PORT_SYMBOLS + PORT_HANDLERS（均为清单，不注册任何工具）。
"""

from __future__ import annotations

import base64
import csv
import json
import os
from io import StringIO
from typing import Any, Awaitable, Callable

from .m4_store import (
    CSV_EXPECTED_FIELDS,
    CSV_TRACE_FIELDS,
    M4Store,
    _clean,
    batch_snapshot_to_json,
    command_digest,
    parse_suggestions_csv,
    purchase_order_to_json,
    suggestion_item_to_json,
)


#: 本模块在 INT 源里注册的工具名 → handler 符号（清单，**不是**注册表；
#: 迁入后由分片改写为真正的 ``LOCAL_HANDLERS`` 并登记进 ``workers.HANDLERS``）。
PORT_HANDLERS: dict[str, str] = {
    "import_m4_purchase_suggestions": "import_m4_purchase_suggestions",
    "import_m4_purchase_suggestions_json": "import_m4_purchase_suggestions_json",
    "list_m4_purchase_suggestions": "list_m4_purchase_suggestions",
    "generate_m4_purchase_orders": "generate_m4_purchase_orders",
    "list_m4_purchase_orders": "list_m4_purchase_orders",
    "get_m4_purchase_order": "get_m4_purchase_order",
    "submit_m4_purchase_order_review": "submit_m4_purchase_order_review",
    "approve_m4_purchase_order": "approve_m4_purchase_order",
    "request_changes_m4_purchase_order": "request_changes_m4_purchase_order",
    "generate_m4_purchase_inquiry_message": "generate_m4_purchase_inquiry_message",
    "send_m4_purchase_order": "send_m4_purchase_order",
}


#: 待迁符号清单：(符号名, 种类, INT 源行号)。迁移完成后删除本常量。
PORT_SYMBOLS: tuple[tuple[str, str, int], ...] = (
    ("Handler", "assign", 38),
    ("_JSON_COMMAND_FIELDS", "assign", 41),
    ("_JSON_ITEM_FIELDS", "assign", 48),
    ("_LEGACY_IMPORT_UNSCOPED_FLAG", "assign", 54),
    ("_EMAIL_CHANNELS", "assign", 56),
    ("_CHANNEL_TONE", "assign", 59),
    ("store_path", "def", 70),
    ("_store", "def", 76),
    ("_tenant", "def", 80),
    ("_task", "def", 84),
    ("_actor", "def", 88),
    ("_unscoped_import_enabled", "def", 92),
    ("_resolve_store_row_visibility", "def", 96),
    ("_json_command", "def", 106),
    ("_json_item_rows", "def", 111),
    ("import_m4_purchase_suggestions_json", "async def", 143),  # tool: import_m4_purchase_suggestions_json
    ("_replay_or_conflict", "def", 224),
    ("list_m4_purchase_suggestions", "async def", 241),  # tool: list_m4_purchase_suggestions
    ("_resolve_generation_scope_and_task", "def", 260),
    ("generate_m4_purchase_orders", "async def", 322),  # tool: generate_m4_purchase_orders
    ("list_m4_purchase_orders", "async def", 341),  # tool: list_m4_purchase_orders
    ("get_m4_purchase_order", "async def", 353),  # tool: get_m4_purchase_order
    ("_submit_or_transition", "async def", 364),
    ("submit_m4_purchase_order_review", "async def", 389),  # tool: submit_m4_purchase_order_review
    ("approve_m4_purchase_order", "async def", 396),  # tool: approve_m4_purchase_order
    ("request_changes_m4_purchase_order", "async def", 403),  # tool: request_changes_m4_purchase_order
    ("_IMPORT_CSV_OUTPUT_KEYS", "assign", 414),
    ("_decode_upload", "def", 420),
    ("import_m4_purchase_suggestions", "async def", 431),  # tool: import_m4_purchase_suggestions
    ("send_m4_purchase_order", "async def", 463),  # tool: send_m4_purchase_order
    ("_inquiry_message_read", "def", 539),
    ("_inquiry_recipient", "def", 552),
    ("_fmt_quantity", "def", 562),
    ("_build_inquiry_content", "def", 573),
    ("generate_m4_purchase_inquiry_message", "async def", 618),  # tool: generate_m4_purchase_inquiry_message
    ("_assert_message_generation_replay", "def", 715),
    ("LOCAL_HANDLERS", "assign", 750),
)
