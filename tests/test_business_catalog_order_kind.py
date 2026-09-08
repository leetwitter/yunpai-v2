"""business_catalog 订单/库存误判修正测试（与 M1 同一确定性解析器）。

库存类路径/文件名（“库存登记…”）但表格实际是完整订单结构时，deep 解析
（parse_xlsx=True）应改判 order 并生成订单候选；真实库存登记表必须保持
inventory 分类，不得被强行改判。
"""

from __future__ import annotations

from pathlib import Path

from yunpai_orchestrator.business_catalog import extract_file

from test_order_semantics import inventory_like_bytes, real_order_twin_bytes


def test_inventory_named_order_layout_is_reclassified_to_order(tmp_path):
    path = Path(tmp_path) / "库存登记-2026-09-01.xlsx"
    path.write_bytes(real_order_twin_bytes())
    extracted = extract_file(path, root=tmp_path, parse_xlsx=True, deep_limit_bytes=12_000_000)
    assert extracted["file_kind"] == "order"
    assert extracted["document_subtype"] == "customer_or_stocking_order"
    order_document = extracted["extraction"].get("order_document") or {}
    assert order_document.get("order_id") == "WX20260905001"
    assert len(order_document.get("lines") or []) == 2
    assert extracted["document"]["review_status"] in {"candidate", "needs_review"}


def test_genuine_inventory_table_keeps_inventory_kind(tmp_path):
    path = Path(tmp_path) / "库存登记-2026-09-01.xlsx"
    path.write_bytes(inventory_like_bytes())
    extracted = extract_file(path, root=tmp_path, parse_xlsx=True, deep_limit_bytes=12_000_000)
    assert extracted["file_kind"] == "inventory"
