"""``run_m3_procurement_requirements`` 行为测试（rows-S4 改造项 ④ 的集成落点）。

覆盖 R4-REQ-1（``m2_package`` 契约分支确定性转换）、R4-REQ-2（在途采购量参与缺口计算）、
确定性算法红线（``gross = order_qty × qty_per × (1 + loss_rate)``、
``shortage = max(0, gross - available - open_po)``）、缺参结构化 ``BLOCKED_INPUT``，
以及经 ``registry.call`` 的契约端到端校验。
随迁来源：``_wt/INT/tests/test_s3_m3.py:655-768``（TestR4ContractBranches）。
"""
from __future__ import annotations

import pytest

from yunpai_orchestrator.m3_local import m2_package_to_order_bom, open_po_quantities
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.workers import m3_mrp

CTX = {"task_id": "T-M3-RUN", "tenant_id": "default"}


class TestM2PackageConversion:
    def test_m2_package_converts_to_order_and_bom(self):
        converted = m2_package_to_order_bom({
            "bom_header": {"bom_id": "B-1", "product_name": "产品A", "order_id": "ORD-1",
                           "order_qty": 2, "due_date": "2026-10-01"},
            "bom_lines": [{"item_no": "10", "material_code": "M-1", "name": "物料1",
                           "quantity_per": 1.5, "unit": "PCS"}],
        })
        assert converted["order"] == {
            "project_id": "ORD-1", "order_id": "ORD-1", "bom_id": "B-1",
            "product_name": "产品A", "order_qty": 2.0, "due_date": "2026-10-01",
        }
        line = converted["bom"]["lines"][0]
        assert line["material_code"] == "M-1"
        assert line["qty_per"] == 1.5
        assert line["uom"] == "PCS"

    @pytest.mark.parametrize("package,message", [
        ({}, "bom_lines"),
        ({"bom_header": {"bom_id": "B-1", "order_id": "ORD-1", "order_qty": 1,
                         "due_date": "2026-10-01"},
          "bom_lines": [{"material_code": "M-1", "qty_per": 0}]}, "qty_per"),
        ({"bom_header": {"bom_id": "B-1", "order_id": "ORD-1", "order_qty": 1,
                         "due_date": "2026-10-01"},
          "bom_lines": ["not-a-dict"]}, "无可量化行"),
        ({"bom_header": {"bom_id": "B-1"}, "bom_lines": [{"material_code": "M-1", "qty_per": 1}]},
         "order_id"),
    ])
    def test_m2_package_missing_facts_raise(self, package, message):
        with pytest.raises(ValueError, match=message):
            m2_package_to_order_bom(package)

    def test_open_po_quantities_sum_by_material(self):
        totals = open_po_quantities({"open_purchase_orders": [
            {"material_code": "M-1", "open_po_qty": 3},
            {"material_code": "M-1", "open_po_qty": 2},
            {"material_code": "M-2", "open_po_qty": "1.5"},
            {"material_code": "", "open_po_qty": 9},
            "not-a-dict",
        ]})
        assert totals == {"M-1": 5.0, "M-2": 1.5}
        assert open_po_quantities({}) == {}


class TestM3MrpHandler:
    @pytest.mark.asyncio
    async def test_m3_mrp_accepts_m2_package_branch(self):
        """R4-REQ-1：契约 anyOf[1] 的 m2_package 分支必须真的能算，而不是 BLOCKED_INPUT。"""
        out = await m3_mrp({
            "m2_package": {
                "bom_header": {"bom_id": "B-1", "product_name": "产品A", "order_id": "ORD-1",
                               "order_qty": 10, "due_date": "2026-10-01"},
                "bom_lines": [{"material_code": "M-1", "qty_per": 2, "uom": "PCS"}],
            },
            "inventory_snapshot": [{"material_code": "M-1", "available_qty": 5}],
        }, {"task_id": "T-M2PKG"})
        assert out["success"] is True
        line = out["data"]["lines"][0]
        assert line["gross_required_qty"] == 20.0
        assert line["shortage_qty"] == 15.0

    @pytest.mark.asyncio
    async def test_m3_mrp_reports_incomplete_m2_package_as_blocked_input(self):
        """不完整 m2_package 必须契约化失败，不得静默补默认值。"""
        out = await m3_mrp(
            {"m2_package": {"bom_header": {"bom_id": "B-1", "order_id": "ORD-1",
                                           "order_qty": 1, "due_date": "2026-10-01"}}},
            {"task_id": "T-M2PKG-BAD"})
        assert out["success"] is False
        assert out["code"] == "BLOCKED_INPUT"
        assert out["errors"][0]["code"] == "M2_PACKAGE_INCOMPLETE"
        assert out["errors"][0]["message"]

    @pytest.mark.asyncio
    async def test_m3_mrp_treats_empty_m2_package_as_missing_bom(self):
        """空对象视为「未提供」，仍走缺参路径——不把「没给 BOM」误报成「m2_package 不完整」。"""
        out = await m3_mrp(
            {"order": {"order_id": "ORD-1", "order_qty": 1, "due_date": "2026-10-01"},
             "m2_package": {}},
            {"task_id": "T-M2PKG-EMPTY"})
        assert out["success"] is False
        assert out["code"] == "BLOCKED_INPUT"
        assert out["errors"][0]["code"] == "MISSING_BOM"

    @pytest.mark.asyncio
    async def test_m3_mrp_subtracts_open_po_from_shortage(self):
        """R4-REQ-2：在途采购量必须参与缺口计算并写入 line，而不是恒为 0.0。"""
        payload = {
            "order": {"order_id": "ORD-2", "order_qty": 10, "due_date": "2026-10-01"},
            "bom": {"bom_id": "B-2", "lines": [{"material_code": "M-1", "qty_per": 2}]},
            "inventory_snapshot": [{"material_code": "M-1", "available_qty": 5}],
        }
        without = await m3_mrp(dict(payload), {"task_id": "T-PO-0"})
        assert without["data"]["lines"][0]["open_po_qty"] == 0.0
        assert without["data"]["lines"][0]["shortage_qty"] == 15.0

        with_po = await m3_mrp({**payload, "open_purchase_orders": [
            {"material_code": "M-1", "open_po_qty": 10}]}, {"task_id": "T-PO-1"})
        line = with_po["data"]["lines"][0]
        assert line["open_po_qty"] == 10.0
        assert line["shortage_qty"] == 5.0
        assert line["readiness"] == "shortage"

    @pytest.mark.asyncio
    async def test_m3_mrp_gross_formula_is_deterministic(self):
        """语义自由红线：gross 只由 order_qty × qty_per × (1+loss_rate) 决定，不掺任何推测。"""
        out = await m3_mrp({
            "order": {"order_id": "ORD-3", "order_qty": 3, "due_date": "2026-10-01"},
            "bom": {"bom_id": "B-3", "lines": [
                {"material_code": "M-1", "qty_per": 2, "loss_rate": 0.1},
                {"material_code": "M-2", "qty_per": 1.5, "loss_rate": 0},
            ]},
            "inventory_snapshot": [{"material_code": "M-1", "available_qty": 100}],
        }, {"task_id": "T-DET"})
        lines = {line["material_code"]: line for line in out["data"]["lines"]}
        assert lines["M-1"]["gross_required_qty"] == pytest.approx(6.6)      # 3 × 2 × 1.1
        assert lines["M-2"]["gross_required_qty"] == pytest.approx(4.5)      # 3 × 1.5
        assert lines["M-1"]["shortage_qty"] == 0.0
        assert lines["M-1"]["readiness"] == "ready"
        assert lines["M-2"]["shortage_qty"] == pytest.approx(4.5)
        assert [line["material_code"] for line in out["data"]["shortage_lines"]] == ["M-2"]

    @pytest.mark.asyncio
    async def test_m3_mrp_history_usage_is_not_consumed(self):
        """manifest 已声明：historical_usage 不消费（不推测安全库存）。"""
        base = {
            "order": {"order_id": "ORD-4", "order_qty": 1, "due_date": "2026-10-01"},
            "bom": {"bom_id": "B-4", "lines": [{"material_code": "M-1", "qty_per": 2}]},
        }
        without = await m3_mrp(dict(base), {"task_id": "T-HIST-0"})
        with_history = await m3_mrp(
            {**base, "historical_usage": [{"material_code": "M-1", "days": 30,
                                           "total_usage_qty": 999, "safety_stock_ratio": 0.5}]},
            {"task_id": "T-HIST-1"})
        assert without["data"]["lines"] == with_history["data"]["lines"]


class TestM3MrpRegistryContract:
    @pytest.mark.asyncio
    async def test_registry_call_validates_output_contract(self):
        registry = build_default_registry()
        out = await registry.call("run_m3_procurement_requirements", {
            "order": {"project_id": "P-5", "order_id": "ORD-5", "bom_id": "B-5",
                      "product_name": "产品A", "order_qty": 2, "due_date": "2026-10-01"},
            "bom": {"bom_id": "B-5", "product_name": "产品A", "lines": [
                {"material_code": "M-1", "material_name": "物料1", "qty_per": 1, "uom": "PCS"}]},
            "inventory_snapshot": [{"material_code": "M-1", "material_name": "物料1",
                                    "warehouse": "WH-A", "lot_no": "L1",
                                    "available_qty": 0, "locked_qty": 0,
                                    "qc_status": "released", "received_at": "2026-09-01"}],
        }, CTX)
        assert out["data"]["shortage_lines"][0]["material_code"] == "M-1"
        assert out["source"]["kind"] == "tool:run_m3_procurement_requirements"

    @pytest.mark.asyncio
    async def test_registry_call_rejects_unknown_m2_package_dir_branch(self):
        """契约已删 ``m2_package_dir``（rows 改造项 ⑥）：该分支不再被接受为替代输入。"""
        import json
        from pathlib import Path

        manifest = json.loads(
            (Path(__file__).resolve().parents[1] / "registry-manifests" / "m3.json")
            .read_text(encoding="utf-8"))
        tool = {t["name"]: t for t in manifest["tools"]}["run_m3_procurement_requirements"]
        assert "m2_package_dir" not in tool["input_schema"]["properties"]
        assert all("m2_package_dir" not in branch.get("required", [])
                   for branch in tool["input_schema"]["anyOf"])
