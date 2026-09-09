"""``fact_gateway`` 单元测试（INT2 收口 1.1）。

覆盖统一形状归一的三个口径 + M0 读口的失败语义：
- attributes 合并 / 顶层优先 / ``attributes`` 原键保留；
- M3 口径 ``drop_attributes=True``（合并后删键）；
- M2 口径 ``M2_ENVELOPE_KEYS``（扁平记录连 attributes 一起剥离）；
- 空库不抛异常且**不创建**空库；行形状非法跳过（不伪造空体）。

父会话裁决 R1/R2：读 canonical 只走 ``M0Store.list_entities``，读取处做一次
attributes 归一化。本模块是该口径的唯一实现点，分片内均为薄适配层。
"""
from pathlib import Path

from yunpai_orchestrator import fact_gateway as fg
from yunpai_orchestrator.m0_backend import M0Store


# ---------- 形状归一 ----------

def test_merge_attributes_top_level_wins_and_keeps_key():
    """attributes 键并入顶层；同名以顶层优先；attributes 原键保留。"""
    body = fg.merge_attributes({"lines": ["top"], "attributes": {"lines": ["attr"], "route_steps": [1]}})
    assert body["lines"] == ["top"], "顶层事实不得被 attributes 覆盖"
    assert body["route_steps"] == [1]
    assert body["attributes"] == {"lines": ["attr"], "route_steps": [1]}


def test_normalize_body_nested_payload_and_flat_record():
    """信封拆分：``payload`` 为 dict 用它；否则按 envelope_keys 剥离信封键。"""
    nested = fg.normalize_body({"canonical_key": "K", "payload": {"product_code": "P-1", "lines": [1]}})
    assert nested == {"product_code": "P-1", "lines": [1]}

    flat = fg.normalize_body({"canonical_key": "K", "tenant_id": "T", "entity_type": "bom",
                              "product_code": "P-2"})
    assert flat == {"product_code": "P-2"}, "信封键不得进业务体"


def test_normalize_body_m3_policy_drops_attributes_key():
    """M3 原口径：合并后删除 ``attributes`` 键。"""
    payload, identity = fg.split_envelope(
        {"identity": {"business_key": "SO-1"},
         "payload": {"order_id": "SO-1", "attributes": {"priority": "high", "order_id": "SHOULD-NOT-WIN"}}},
        drop_attributes=True)
    assert payload == {"priority": "high", "order_id": "SO-1"}
    assert "attributes" not in payload
    assert identity == {"business_key": "SO-1"}


def test_normalize_body_m2_policy_strips_attributes_from_flat_record():
    """M2 原口径 ``M2_ENVELOPE_KEYS``：扁平记录的 attributes 键被剥离（不参与合并）。"""
    flat = fg.normalize_body({"canonical_key": "K", "attributes": {"route_steps": [1]}, "role": "sop"},
                             envelope_keys=fg.M2_ENVELOPE_KEYS)
    assert flat == {"role": "sop"}
    assert fg.M2_ENVELOPE_KEYS == fg.ENVELOPE_KEYS | {"attributes"}


def test_normalize_body_tolerates_non_dict_and_empty():
    """空/非法输入不抛异常、不造数。"""
    assert fg.normalize_body(None) == {}          # type: ignore[arg-type]
    assert fg.normalize_body({}) == {}
    assert fg.merge_attributes(None) == {}        # type: ignore[arg-type]
    assert fg.split_envelope({}) == ({}, {})
    assert fg.split_envelope({"identity": "not-a-dict"}) == ({}, {})


# ---------- M0 读口 ----------

def test_read_entities_missing_db_returns_empty_and_creates_nothing(tmp_path):
    """库不存在 → 空列表，且不得隐式建库（``M0Store.__init__`` 会建文件）。"""
    missing = tmp_path / "nope" / "m0.sqlite"
    assert fg.read_entities("bom", "T-A", str(missing)) == []
    assert not missing.exists(), "读不到时不得创建空库"


def test_read_entities_normalizes_attributes_and_keeps_canonical_key(tmp_path):
    """真实 M0 读口：attributes 并入顶层、顶层优先、canonical_key 保留。"""
    db = tmp_path / "m0.sqlite"
    store = M0Store(db)
    batch = store.ingest([{
        "entity_type": "document", "doc_id": "DOC-1", "product_code": "P-1",
        "attributes": {"route_steps": [{"operation_id": "OP-1", "sequence": 1}],
                       "product_code": "SHOULD-NOT-WIN"},
    }], tenant_id="T-A", task_id="t")
    store.publish(batch["batch_id"], actor="reviewer", reason="审批文档")

    rows = fg.read_entities("document", "T-A", str(db))
    assert len(rows) == 1
    row = rows[0]
    assert row["route_steps"] == [{"operation_id": "OP-1", "sequence": 1}]
    assert row["product_code"] == "P-1", "顶层优先：attributes 不得覆盖顶层事实"
    assert row["canonical_key"] == "P-1"
    # 租户隔离：别的租户读不到
    assert fg.read_entities("document", "T-B", str(db)) == []


def test_read_entities_default_path_from_env(tmp_path, monkeypatch):
    """``db_path=None`` 时按 ``YUNPAI_M0_DB`` 解析（与 M0Store 缺省口径一致）。"""
    db = tmp_path / "env-m0.sqlite"
    store = M0Store(db)
    batch = store.ingest([{"entity_type": "equipment_master", "equipment_code": "EQ-9",
                           "status": "available"}], tenant_id="T-A", task_id="t")
    store.publish(batch["batch_id"], actor="reviewer", reason="审批设备")
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))
    assert fg.default_db_path() == str(db)
    rows = fg.read_entities("equipment_master", "T-A")
    assert [r["equipment_code"] for r in rows] == ["EQ-9"]


def test_read_entities_propagates_read_port_failure(tmp_path):
    """读口异常原样抛出（不静默吞成空），由调用方决定失败关闭。"""
    broken = tmp_path / "broken.sqlite"
    broken.write_text("not a sqlite database", encoding="utf-8")
    try:
        fg.read_entities("bom", "T-A", str(broken))
    except Exception:  # noqa: BLE001 - 只断言「有异常」，不锁定具体类型
        pass
    else:  # pragma: no cover - 读口未抛异常时给出明确失败
        raise AssertionError("损坏库必须抛异常而不是返回空（否则等于静默造空事实）")
    assert Path(broken).exists()
