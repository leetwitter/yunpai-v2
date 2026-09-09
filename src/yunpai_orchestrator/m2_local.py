"""M2 BOM/SOP 本地实现：模板入库、受控 BOM/SOP 生成、历史检索、run 读回。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m2_local.py``

- sha256: ``d4d882da27edeb7fd51d5c3b88650474d9295ee82b32e465a602a1b78f87113d``
- 规模: 34516 字节 / 733 行
- 归属分片: **M2**（逐工具结论见 ``rows-S3.md``）
- 状态: **骨架（skeleton）** —— 只含出处头、已验证的 import 闭包与待迁清单；
  业务函数由归属分片按 rows 逐工具迁入（M-INFRA 只做基建与裁决，不搬具体业务工具）。

import 闭包核对（相对 V2 main@2d0dfdb）：
- ``.m0_sandbox.utc_now`` → m0_sandbox.py:26：存在（INT 与 V2 逐字节相同）
- ``openpyxl（load_workbook）`` → V2 运行环境已具备：已在 .[test] 依赖内
- ``python-docx（docx）`` → V2 已惰性使用：已在 .[test] 依赖内（business_catalog.py 同款）

迁入须知：
- `_canonical_bom_lines()` 直连 `YUNPAI_M0_DB` 读 canonical 表（INT :631-666），在 V2（M0 可走 HTTP）会静默退化为空候选 → M2 必须改走 V2 的 M0 读口（rows-S3.md 第 1 行）。
- `LOCAL_HANDLERS` 在 INT 里是「字典字面量（:541）+ 追加赋值（:732-733）」两种注册方式，迁入时不要只搬字面量。

本文件的可执行内容 = import 闭包 + PORT_SYMBOLS + PORT_HANDLERS（均为清单，不注册任何工具）。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import threading
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .m0_sandbox import utc_now


#: 本模块在 INT 源里注册的工具名 → handler 符号（清单，**不是**注册表；
#: 迁入后由分片改写为真正的 ``LOCAL_HANDLERS`` 并登记进 ``workers.HANDLERS``）。
PORT_HANDLERS: dict[str, str] = {
    "onboard_m2_bom_template": "onboard_bom_template",
    "generate_m2_bom_controlled": "generate_bom_controlled",
    "list_m2_runs": "m2_list_runs",
    "get_m2_run": "m2_get_run",
    "generate_m2_sop": "generate_m2_sop",
    "search_m2_bom_history": "search_m2_bom_history",
}


#: 待迁符号清单：(符号名, 种类, INT 源行号)。迁移完成后删除本常量。
PORT_SYMBOLS: tuple[tuple[str, str, int], ...] = (
    ("_default_db_path", "def", 33),
    ("SCHEMA", "assign", 37),
    ("_ALIASES", "assign", 81),
    ("_text", "def", 90),
    ("_number", "def", 98),
    ("_artifact_path_map", "def", 108),
    ("M2RunStore", "class", 117),
    ("_read_rule_package", "def", 281),
    ("_parse_history_file", "def", 295),
    ("_guess_prefix", "def", 368),
    ("_ctx_store", "def", 377),
    ("onboard_bom_template", "async def", 381),  # tool: onboard_m2_bom_template
    ("generate_bom_controlled", "async def", 428),  # tool: generate_m2_bom_controlled
    ("m2_list_runs", "async def", 518),  # tool: list_m2_runs
    ("m2_get_run", "async def", 529),  # tool: get_m2_run
    ("LOCAL_HANDLERS", "assign", 541),
    ("_docx_cell_text", "def", 550),
    ("generate_m2_sop", "async def", 554),  # tool: generate_m2_sop
    ("_canonical_bom_lines", "def", 631),
    ("search_m2_bom_history", "async def", 669),  # tool: search_m2_bom_history
)
