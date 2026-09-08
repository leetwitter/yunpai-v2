"""canonical_ingest：确定性校验 + 落库 + 幂等 + PII。"""

from __future__ import annotations

import hashlib

import pytest

from yunpai_orchestrator.canonical_ingest import CanonicalLandingStore
from yunpai_orchestrator.registry import build_default_registry


def _store(tmp_path) -> CanonicalLandingStore:
    return CanonicalLandingStore(str(tmp_path / "canonical.sqlite"))


def _sha(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


def test_ingest_and_query_material(tmp_path):
    store = _store(tmp_path)
    result = store.ingest(
        entity_type="material",
        records=[{"material_code": "YA.001", "material_name": "铜箔", "unit": "m"},
                 {"material_code": "YA.002", "material_name": "线材"}],
        filename="bom.xlsx", sha256=_sha("m1"), confidence=0.9,
    )
    assert result["success"] is True
    assert result["data"]["inserted_rows"] == 2
    rows = store.query(entity_type="material")
    assert len(rows) == 2
    assert rows[0]["material_code"] == "YA.001"


def test_ingest_idempotent_by_sha256(tmp_path):
    store = _store(tmp_path)
    payload = {"entity_type": "equipment",
               "records": [{"equipment_code": "EQ-1", "equipment_name": "押出机"}],
               "filename": "设备台账.xlsx", "sha256": _sha("eq1"), "confidence": 0.9}
    first = store.ingest(**payload)
    second = store.ingest(**payload)
    assert first["data"]["inserted_rows"] == 1
    assert second["data"]["duplicate"] is True
    assert second["data"]["inserted_rows"] == 0


def test_ingest_rejects_schema_invalid(tmp_path):
    store = _store(tmp_path)
    result = store.ingest(
        entity_type="material",
        records=[{"material_name": "铜箔"}],  # 缺 material_code
        filename="x.xlsx", sha256=_sha("bad"), confidence=0.9,
    )
    assert result["success"] is False
    assert result["code"] == "SCHEMA_INVALID"


def test_ingest_rejects_invalid_sha256(tmp_path):
    store = _store(tmp_path)
    result = store.ingest(entity_type="material", records=[{"material_code": "M", "material_name": "x"}],
                          filename="x.xlsx", sha256="short", confidence=0.9)
    assert result["success"] is False
    assert result["code"] == "INVALID_SHA256"


def test_ingest_masks_identity_pii_value(tmp_path):
    store = _store(tmp_path)
    store.ingest(
        entity_type="supplier",
        records=[{"supplier_code": "S-1", "supplier_name": "某供应商", "legal_id": "13812345678"}],
        filename="supplier.xlsx", sha256=_sha("pii"), confidence=0.9,
    )
    rows = store.query(entity_type="supplier")
    assert "13812345678" not in str(rows[0]["legal_id"])
    assert "*" in str(rows[0]["legal_id"])


@pytest.mark.asyncio
async def test_registry_ingest_canonical_tool(tmp_path):
    registry = build_default_registry()
    assert "ingest_canonical" in registry.specs
    assert "ingest_canonical" in registry.handlers
    result = await registry.call("ingest_canonical", {
        "entity_type": "inventory",
        "records": [{"material_code": "M-1", "available_qty": 100, "warehouse": "WH-1"}],
        "filename": "库存.xlsx",
        "sha256": _sha("inv1"),
        "confidence": 0.9,
    }, {"task_id": "T-1", "canonical_db": str(tmp_path / "canonical.sqlite")})
    assert result["success"] is True
    assert result["data"]["inserted_rows"] == 1
