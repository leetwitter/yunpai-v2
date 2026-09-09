"""M5 报工/派工本地实现：报工事件、工人-订单绑定（两个工具在 V2 属 INTENTIONALLY_UNBOUND）。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m5_work_local.py``

- sha256: ``df6064354d38c4d9387a4379af41bb6e79ece358344471c4fc2fe97c55b02cf5``
- 规模: 19194 字节 / 394 行
- 归属分片: **M5**（逐工具结论见 ``rows-S6.md``）
- 状态: **骨架（skeleton）** —— 只含出处头、已验证的 import 闭包与待迁清单；
  业务函数由归属分片按 rows 逐工具迁入（M-INFRA 只做基建与裁决，不搬具体业务工具）。

import 闭包核对（相对 V2 main@2d0dfdb）：
- ``.m0_sandbox.utc_now`` → m0_sandbox.py:26：存在（逐字节相同）
- ``.m5_repository.M5Repository`` → m5_repository.py:226：存在；`get_plan` / `add_execution_event` 在 V2 与 INT 均在（V2 版还多 `save_procurement_proposal`）

迁入须知：
- `report_workload` / `bind_worker_to_order` 在 V2 `binding.py:26` 属 INTENTIONALLY_UNBOUND（永不进路由目录）→ 迁入实现但不要登记进 HANDLERS 的可见面（见 INFRA-DECISIONS §3.3）。

本文件的可执行内容 = import 闭包 + PORT_SYMBOLS + PORT_HANDLERS（均为清单，不注册任何工具）。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from hashlib import sha256
from typing import Any
from uuid import uuid4

from .m0_sandbox import utc_now


#: 本模块在 INT 源里注册的工具名 → handler 符号（清单，**不是**注册表；
#: 迁入后由分片改写为真正的 ``LOCAL_HANDLERS`` 并登记进 ``workers.HANDLERS``）。
PORT_HANDLERS: dict[str, str] = {
    "report_workload": "report_workload",
    "bind_worker_to_order": "bind_worker_to_order",
}


#: 待迁符号清单：(符号名, 种类, INT 源行号)。迁移完成后删除本常量。
PORT_SYMBOLS: tuple[tuple[str, str, int], ...] = (
    ("EVENT_TYPES", "assign", 29),
    ("BIND_DDL", "assign", 31),
    ("_tenant", "def", 50),
    ("_task", "def", 54),
    ("_actor", "def", 58),
    ("_trace", "def", 62),
    ("_evidence", "def", 66),
    ("_repo_path", "def", 71),
    ("_canonical_lookup", "def", 75),
    ("M5WorkStore", "class", 102),
    ("_canonical_worker", "def", 158),
    ("_canonical_order", "def", 169),
    ("_plan_meta", "def", 180),
    ("report_workload", "async def", 196),  # tool: report_workload
    ("bind_worker_to_order", "async def", 333),  # tool: bind_worker_to_order
    ("LOCAL_HANDLERS", "assign", 390),
)
