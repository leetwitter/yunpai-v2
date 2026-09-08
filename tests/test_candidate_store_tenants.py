"""候选层租户隔离（P1.1）：识别落库表与 canonical 落库表补 tenant_id。

- 幂等键从全局 sha256 改为 (tenant_id, sha256)：双租户同名同哈希文件各自成行；
- query 只读本租户，跨租户读取为空；
- 老库（无 tenant_id 列）打开即迁移回填 'default'，不丢数据；
- 工具链（ingest_recognized/query_recognized_table/ingest_canonical）从 ctx
  透传租户，agent 侧无需在 payload 里自带。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from yunpai_orchestrator.canonical_ingest import CanonicalLandingStore
from yunpai_orchestrator.recognized_store import RecognizedTableStore
from yunpai_orchestrator.registry import build_default_registry


def _sha(tag: str) -> str:
    return hashlib.sha256(tag.encode()).hexdigest()


# ---------------------------------------------------------------------------
# RecognizedTableStore
# ---------------------------------------------------------------------------

def test_recognized_same_file_two_tenants_both_insert(tmp_path):
    store = RecognizedTableStore(str(tmp_path / "rec.sqlite"))
    for tenant in ("tenant-a", "tenant-b"):
        result = store.ingest(kind="wage", filename="工资表.xlsx", sha256=_sha("same-file"),
                              columns=["姓名", "实发工资"], rows=[{"姓名": "张三", "实发工资": 5000}],
                              confidence=0.9, tenant_id=tenant)
        assert result["duplicate"] is False
        assert result["inserted_rows"] == 1
        assert result["tenant_id"] == tenant
    # 同租户重传幂等；另一租户不受影响。
    again = store.ingest(kind="wage", filename="工资表.xlsx", sha256=_sha("same-file"),
                         columns=["姓名"], rows=[{"姓名": "李四"}], confidence=0.9, tenant_id="tenant-a")
    assert again["duplicate"] is True
    assert again["inserted_rows"] == 0


def test_recognized_query_scoped_to_tenant(tmp_path):
    store = RecognizedTableStore(str(tmp_path / "rec.sqlite"))
    for tenant, name in (("tenant-a", "甲"), ("tenant-b", "乙")):
        store.ingest(kind="worker", filename="人员.xlsx", sha256=_sha(f"worker-{tenant}"),
                     columns=["worker_code", "worker_name"],
                     rows=[{"worker_code": "W-1", "worker_name": name}],
                     confidence=0.9, tenant_id=tenant)
    a = store.query(kind="worker", tenant_id="tenant-a")
    b = store.query(kind="worker", tenant_id="tenant-b")
    assert [r["worker_name"] for r in a] == ["甲"]
    assert [r["worker_name"] for r in b] == ["乙"]
    assert all(r["tenant_id"] == "tenant-a" for r in a)
    # 跨租户 query 为空，不泄露存在性。
    assert store.query(kind="worker", tenant_id="tenant-c") == []


def test_recognized_legacy_db_migrates_with_default_tenant(tmp_path):
    db_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(str(db_path)) as db:
        db.execute("""CREATE TABLE recognized_tables (
            id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, filename TEXT NOT NULL,
            sha256 TEXT NOT NULL UNIQUE, columns TEXT NOT NULL, rows TEXT NOT NULL,
            confidence REAL NOT NULL, redacted_fields INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO recognized_tables(kind, filename, sha256, columns, rows, confidence, created_at) "
                   "VALUES(?,?,?,?,?,?,?)",
                   ("bom", "bom.xlsx", _sha("legacy"), '["a"]', '[{"a": 1}]', 0.9, "2026-09-01T00:00:00+00:00"))
    store = RecognizedTableStore(str(db_path))
    rows = store.query(kind="bom", tenant_id="default")
    assert len(rows) == 1 and rows[0]["filename"] == "bom.xlsx"
    assert store.query(kind="bom", tenant_id="tenant-a") == []
    # 迁移后再写入新租户，幂等键按 (tenant, sha256) 生效。
    store.ingest(kind="bom", filename="bom.xlsx", sha256=_sha("legacy"),
                 columns=["a"], rows=[{"a": 2}], confidence=0.9, tenant_id="tenant-a")
    assert store.query(kind="bom", tenant_id="tenant-a")


# ---------------------------------------------------------------------------
# CanonicalLandingStore
# ---------------------------------------------------------------------------

def test_canonical_same_file_two_tenants_both_insert(tmp_path):
    store = CanonicalLandingStore(str(tmp_path / "canon.sqlite"))
    records = [{"material_code": "YA.001", "material_name": "铜箔"}]
    for tenant in ("tenant-a", "tenant-b"):
        result = store.ingest(entity_type="material", records=records,
                              filename="物料.xlsx", sha256=_sha("same"), confidence=0.9,
                              tenant_id=tenant)
        assert result["success"] is True
        assert result["data"]["inserted_rows"] == 1
        assert result["data"]["tenant_id"] == tenant
    dup = store.ingest(entity_type="material", records=records,
                       filename="物料.xlsx", sha256=_sha("same"), confidence=0.9,
                       tenant_id="tenant-a")
    assert dup["data"]["duplicate"] is True


def test_canonical_query_scoped_to_tenant(tmp_path):
    store = CanonicalLandingStore(str(tmp_path / "canon.sqlite"))
    for tenant, code in (("tenant-a", "M-A"), ("tenant-b", "M-B")):
        store.ingest(entity_type="material",
                     records=[{"material_code": code, "material_name": "线材"}],
                     filename="物料.xlsx", sha256=_sha(f"mat-{tenant}"), confidence=0.9,
                     tenant_id=tenant)
    a = store.query(entity_type="material", tenant_id="tenant-a")
    b = store.query(entity_type="material", tenant_id="tenant-b")
    assert [r["material_code"] for r in a] == ["M-A"]
    assert [r["material_code"] for r in b] == ["M-B"]
    assert store.query(entity_type="material", tenant_id="tenant-x") == []


def test_canonical_legacy_db_migrates_with_default_tenant(tmp_path):
    db_path = tmp_path / "legacy-canon.sqlite"
    with sqlite3.connect(str(db_path)) as db:
        db.execute("""CREATE TABLE canonical_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, business_key TEXT NOT NULL,
            filename TEXT NOT NULL, sha256 TEXT NOT NULL, payload TEXT NOT NULL,
            confidence REAL NOT NULL, created_at TEXT NOT NULL)""")
        db.execute("INSERT INTO canonical_records(entity_type, business_key, filename, sha256, payload, confidence, created_at) "
                   "VALUES(?,?,?,?,?,?,?)",
                   ("material", "YA.001", "m.xlsx", _sha("legacy"),
                    json.dumps({"material_code": "YA.001", "material_name": "铜箔"}), 0.9, "2026-09-01T00:00:00+00:00"))
    store = CanonicalLandingStore(str(db_path))
    rows = store.query(entity_type="material", tenant_id="default")
    assert len(rows) == 1 and rows[0]["material_code"] == "YA.001"
    assert store.query(entity_type="material", tenant_id="tenant-a") == []
    # 幂等语义按 (tenant_id, sha256)：同哈希在新租户可再落一份。
    result = store.ingest(entity_type="material",
                          records=[{"material_code": "YA.001", "material_name": "铜箔"}],
                          filename="m.xlsx", sha256=_sha("legacy"), confidence=0.9, tenant_id="tenant-a")
    assert result["data"]["duplicate"] is False


# ---------------------------------------------------------------------------
# 工具链：ctx.tenant_id 透传
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_registry_tools_scope_by_ctx_tenant(tmp_path):
    registry = build_default_registry()
    base_ctx = {"task_id": "T-TENANT", "recognized_db": str(tmp_path / "rec.sqlite"),
                "canonical_db": str(tmp_path / "canon.sqlite")}
    for tenant in ("tenant-a", "tenant-b"):
        ctx = {**base_ctx, "tenant_id": tenant}
        ing = await registry.call("ingest_recognized", {
            "kind": "inventory", "filename": "库存.xlsx", "sha256": _sha("inv"),
            "columns": ["material_code", "available_qty"],
            "rows": [{"material_code": f"M-{tenant}", "available_qty": 10}],
            "confidence": 0.9,
        }, ctx)
        assert ing["success"] is True
        result = await registry.call("ingest_canonical", {
            "entity_type": "material",
            "records": [{"material_code": f"M-{tenant}", "material_name": "线材"}],
            "filename": "物料.xlsx", "sha256": _sha(f"mat-{tenant}"), "confidence": 0.9,
        }, ctx)
        assert result["success"] is True
    # 各自租户读回只见本租户数据；陌生租户为空。
    a = await registry.call("query_recognized_table", {"kind": "inventory"},
                            {**base_ctx, "tenant_id": "tenant-a"})
    assert [r["material_code"] for r in a["data"]["rows"]] == ["M-tenant-a"]
    c = await registry.call("query_recognized_table", {"kind": "inventory"},
                            {**base_ctx, "tenant_id": "tenant-c"})
    assert c["data"]["rows"] == []
    store = CanonicalLandingStore(str(tmp_path / "canon.sqlite"))
    assert len(store.query(entity_type="material", tenant_id="tenant-b")) == 1
    assert store.query(entity_type="material", tenant_id="tenant-c") == []
