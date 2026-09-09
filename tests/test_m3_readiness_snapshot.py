"""M3 正式齐套快照（``get_material_readiness_snapshot``）行为测试。

随迁来源：``_wt/INT/tests/test_s3_m3.py:110-330``（TestM31Snapshot 全集），按 V2 口径
改写为 ``yunpai_orchestrator`` 命名空间 + ``registry-manifests/m3.json`` 契约校验。
覆盖：FIFO 分配 / 质检 hold / 部分齐套 / 空库存 / unknown 可恢复 / 重放同快照 /
租户隔离的输入校验 / 落库可回读 / registry.call 端到端（含输出契约校验）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from yunpai_orchestrator.m0_backend import M0Store
from yunpai_orchestrator.m3_local import LOCAL_HANDLERS
from yunpai_orchestrator.m3_store import M3Store
from yunpai_orchestrator.registry import build_default_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
_M3_MANIFEST = REPO_ROOT / "registry-manifests" / "m3.json"


def _manifest_tools() -> dict:
    return {tool["name"]: tool for tool in json.loads(_M3_MANIFEST.read_text(encoding="utf-8"))["tools"]}


def assert_output_valid(tool: str, result: dict) -> None:
    """Full JSON-schema validation exactly like registry.call does."""
    from jsonschema import Draft202012Validator

    Draft202012Validator(_manifest_tools()[tool]["output_schema"]).validate(result)


def _canonical_rec(entity_type: str, **fields) -> dict:
    return {
        "schema_version": "m0.ingest.v1",
        "tenant_id": "default",
        "idempotency_key": f"seed-{entity_type}-{fields.get('canonical_key') or fields.get('order_id') or fields.get('product_code') or fields.get('material_code')}",
        "source": {"system": "seed", "external_id": "s/seed", "sha256": "d" * 64},
        "entity_type": entity_type,
        "evidence": [],
        "review_status": "approved",
        "reviewed_by": "steward",
        **fields,
    }


def seed_canonical(db_path: str, records: list[dict], tenant: str = "default") -> None:
    """Publish records straight into the canonical store (test seeding)."""
    store = M0Store(db_path)
    batch = store.ingest(records, tenant_id=tenant, task_id="seed")
    store.publish(batch["batch_id"], actor="steward", human_override=True)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated YUNPAI_M0_DB canonical + YUNPAI_M3_DB m3 stores."""
    canonical_db = str(tmp_path / "canonical.sqlite")
    m3_db = str(tmp_path / "m3.sqlite")
    monkeypatch.setenv("YUNPAI_M0_DB", canonical_db)
    monkeypatch.setenv("YUNPAI_M3_DB", m3_db)
    M0Store(canonical_db)  # initialize schema
    return {"canonical_db": canonical_db, "m3_db": m3_db, "tmp_path": tmp_path}


def _order_rec(order_id: str = "ORD-1", product: str = "P-1", qty: float = 2,
               product_name: str = "产品A") -> dict:
    return _canonical_rec(
        "order", order_id=order_id, canonical_key=order_id, product_code=product,
        product_name=product_name, order_qty=qty,
        due_date="2026-10-01", customer="客户A",
    )


def _bom_rec(product: str = "P-1", lines: list | None = None,
             bom_key: str | None = None) -> dict:
    return _canonical_rec(
        "bom", product_code=product, canonical_key=bom_key or f"BOM-{product}",
        status="active",
        lines=lines or [{"line_no": "10", "material_code": "M-1",
                         "material_name": "物料1", "qty_per": 1.5, "uom": "PCS"}],
    )


def _lot_rec(material: str, lot_no: str, available: float, *, qc: str = "released",
             locked: float = 0.0, received_at: str = "2026-09-01", key: str | None = None) -> dict:
    return _canonical_rec(
        "inventory", material_code=material, warehouse="WH-A", lot_no=lot_no,
        available_qty=available, locked_qty=locked, qc_status=qc,
        received_at=received_at, canonical_key=key or f"INV-{material}-{lot_no}",
    )


def _ctx(**overrides) -> dict:
    base = {"task_id": "TASK-M3", "tenant_id": "default", "run_id": "RUN-M3"}
    base.update(overrides)
    return base


class TestM3ReadinessSnapshot:
    async def _snapshot(self, order_id: str = "ORD-1", *, tenant: str = "default", ctx=None):
        handler = LOCAL_HANDLERS["get_material_readiness_snapshot"]
        return await handler({"order_id": order_id, "tenant_id": tenant}, ctx or _ctx())

    async def test_ready_order_full_stock_allocates_fifo(self, env):
        seed_canonical(env["canonical_db"], [
            _order_rec("ORD-1", qty=2),
            _bom_rec("P-1"),
            _lot_rec("M-1", "L1", 3.0, received_at="2026-09-01"),
            _lot_rec("M-1", "L2", 5.0, received_at="2026-09-05"),
        ])
        out = await self._snapshot()
        assert_output_valid("get_material_readiness_snapshot", out)
        assert out["success"] is True
        data = out["data"]
        order = data["orders"][0]
        assert order["status"] == "ready"
        assert order["earliest_kitting_time"] is not None
        material = order["materials"][0]
        assert material["material_code"] == "M-1"
        assert material["required_quantity"] == "3"      # 2 × 1.5
        assert material["allocated_quantity"] == "3"
        assert material["shortage_quantity"] == "0"
        assert material["status"] == "ready"
        # FIFO: first lot only must be consumed
        assert len(material["allocations"]) == 1
        assert material["allocations"][0]["source_type"] == "wms_inventory_lot"
        assert material["allocations"][0]["quantity"] == "3"
        assert material["allocations"][0]["confidence"] == 1.0
        assert data["completeness"]["bom"] is True
        assert data["completeness"]["inventory"] is True
        assert data["completeness"]["uom"] is True
        assert data["completeness"]["in_transit"] is False
        assert data["completeness"]["substitution"] is False
        assert data["input_versions"]["engineering_release_id"] == "ORD-1"
        assert len(data["input_checksum"]) == 64

    async def test_shortage_partial_and_full_shortage(self, env):
        # material 1 fully covered, material 2 not covered at all -> partial
        seed_canonical(env["canonical_db"], [
            _order_rec("ORD-1", qty=2),
            _bom_rec("P-1", lines=[
                {"material_code": "M-1", "material_name": "物料1", "qty_per": 1.0, "uom": "PCS"},
                {"material_code": "M-2", "material_name": "物料2", "qty_per": 2.0, "uom": "PCS"},
            ]),
            _lot_rec("M-1", "L1", 2.0),
        ])
        out = await self._snapshot()
        order = out["data"]["orders"][0]
        assert order["status"] == "partial"           # 缺料但存在已覆盖物料
        by_code = {m["material_code"]: m for m in order["materials"]}
        assert by_code["M-1"]["status"] == "ready"
        assert by_code["M-1"]["allocated_quantity"] == "2"
        assert by_code["M-2"]["status"] == "shortage"
        assert by_code["M-2"]["allocations"] == []
        assert order["earliest_kitting_time"] is None

        out2 = await self._snapshot("ORD-2")          # unknown order
        assert out2["data"]["orders"][0]["status"] == "unknown"

    async def test_no_material_fully_covered_is_shortage(self, env):
        # single material partially covered: no material ready -> shortage
        seed_canonical(env["canonical_db"], [
            _order_rec("ORD-1", qty=2),
            _bom_rec("P-1"),
            _lot_rec("M-1", "L1", 2.0),
        ])
        out = await self._snapshot()
        order = out["data"]["orders"][0]
        assert order["status"] == "shortage"
        assert order["materials"][0]["status"] == "shortage"
        assert order["materials"][0]["allocated_quantity"] == "2"

    async def test_empty_inventory_is_shortage_not_ready(self, env):
        seed_canonical(env["canonical_db"], [_order_rec("ORD-1"), _bom_rec("P-1")])
        out = await self._snapshot()
        order = out["data"]["orders"][0]
        assert order["status"] == "shortage"
        assert order["materials"][0]["allocated_quantity"] == "0"

    async def test_quality_hold_when_only_blocked_lot(self, env):
        seed_canonical(env["canonical_db"], [
            _order_rec("ORD-1"),
            _bom_rec("P-1"),
            _lot_rec("M-1", "L1", 10.0, qc="quarantine"),
        ])
        out = await self._snapshot()
        data = out["data"]
        order = data["orders"][0]
        assert order["status"] == "quality_hold"
        assert order["materials"][0]["status"] == "quality_hold"
        assert data["review_required"] is True
        assert data["reason_code"] == "QUALITY_HOLD"

    async def test_unknown_reason_code_when_order_absent(self, env):
        out = await self._snapshot("ORD-NOPE")
        data = out["data"]
        assert out["success"] is True
        assert data["orders"][0]["status"] == "unknown"
        assert data["reason_code"] == "ORDER_NOT_FOUND"
        assert data["recoverable"] is True
        # unknown snapshot carries the all-false completeness shape
        assert data["completeness"] == {"bom": False, "inventory": False, "in_transit": False,
                                        "substitution": False, "uom": False}

    async def test_unknown_when_canonical_unconfigured(self, env, monkeypatch):
        monkeypatch.delenv("YUNPAI_M0_DB", raising=False)
        out = await self._snapshot()
        data = out["data"]
        assert data["orders"][0]["status"] == "unknown"
        assert data["reason_code"] == "CANONICAL_UNAVAILABLE"
        assert data["recoverable"] is True

    async def test_unknown_when_bom_missing(self, env):
        seed_canonical(env["canonical_db"], [_order_rec("ORD-1")])
        out = await self._snapshot()
        assert out["data"]["reason_code"] == "BOM_MISSING"
        assert out["data"]["orders"][0]["status"] == "unknown"

    async def test_replay_returns_same_snapshot(self, env):
        seed_canonical(env["canonical_db"], [
            _order_rec("ORD-1"), _bom_rec("P-1"), _lot_rec("M-1", "L1", 5.0),
        ])
        first = await self._snapshot()
        second = await self._snapshot()
        assert first["data"]["snapshot_id"] == second["data"]["snapshot_id"]
        assert first["data"]["observed_at"] == second["data"]["observed_at"]
        assert first["data"]["input_checksum"] == second["data"]["input_checksum"]

    async def test_invalid_input_missing_order_id(self, env):
        handler = LOCAL_HANDLERS["get_material_readiness_snapshot"]
        out = await handler({"tenant_id": "default"}, _ctx())
        assert out["success"] is False
        assert out["errors"][0]["code"] == "INVALID_INPUT"
        assert_output_valid("get_material_readiness_snapshot", out)

    async def test_inventory_change_invalidates_checksum(self, env):
        seed_canonical(env["canonical_db"], [
            _order_rec("ORD-1"), _bom_rec("P-1"), _lot_rec("M-1", "L1", 2.0),
        ])
        out1 = await self._snapshot()
        assert out1["data"]["orders"][0]["status"] == "shortage"
        seed_canonical(env["canonical_db"], [_lot_rec("M-1", "L2", 9.0)])
        out2 = await self._snapshot()
        assert out2["data"]["orders"][0]["status"] == "ready"
        assert out2["data"]["input_checksum"] != out1["data"]["input_checksum"]

    async def test_snapshot_persisted_and_latest_readable(self, env):
        seed_canonical(env["canonical_db"], [
            _order_rec("ORD-1"), _bom_rec("P-1"), _lot_rec("M-1", "L1", 5.0),
        ])
        await self._snapshot()
        store = M3Store(env["m3_db"])
        latest = store.latest_snapshot("ORD-1", "default")
        assert latest is not None
        assert latest["order_status"] == "ready"
        assert latest["payload"]["orders"][0]["status"] == "ready"

    async def test_business_catalog_attributes_are_flattened(self, env):
        """父会话裁决（M0 读口一致性）：business_catalog 形态的 payload.attributes 归一化。"""
        seed_canonical(env["canonical_db"], [
            _canonical_rec("order", canonical_key="ORD-ATTR",
                           payload={"attributes": {"order_id": "ORD-ATTR", "product_code": "P-1",
                                                   "order_qty": 2, "product_name": "产品A"}}),
            _bom_rec("P-1"),
            _lot_rec("M-1", "L1", 5.0),
        ])
        out = await self._snapshot("ORD-ATTR")
        assert out["data"]["orders"][0]["status"] == "ready"
        assert out["data"]["orders"][0]["materials"][0]["required_quantity"] == "3"


class TestM3SnapshotRegistryStitching:
    """登记进 workers.HANDLERS 后经 registry.call 跑通（PROMPT-M3 §三.2）。"""

    @pytest.mark.asyncio
    async def test_registry_call_validates_input_and_output_contract(self, env):
        seed_canonical(env["canonical_db"], [
            _order_rec("ORD-1"), _bom_rec("P-1"), _lot_rec("M-1", "L1", 5.0),
        ])
        registry = build_default_registry()
        out = await registry.call(
            "get_material_readiness_snapshot",
            {"order_id": "ORD-1", "tenant_id": "default"},
            _ctx(),
        )
        assert out["data"]["orders"][0]["status"] == "ready"
        # 合同信封归一化（contracts.normalize_contract_result）
        assert out["status"] == "success"
        assert out["source"]["kind"] == "tool:get_material_readiness_snapshot"
        assert out["invoked_tools"] == ["get_material_readiness_snapshot"]

    @pytest.mark.asyncio
    async def test_registry_call_requires_contract_required_fields(self, env):
        registry = build_default_registry()
        with pytest.raises(ValueError, match="tenant_id"):
            await registry.call("get_material_readiness_snapshot", {"order_id": "ORD-1"}, _ctx())
