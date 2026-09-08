"""Agent 驱动文件识别的确定性安全网（P0）验收。

覆盖三个本地工具：
- sample_file：magic bytes 嗅探 + 表头/前 N 行采样 + sha256；
- ingest_recognized：kind 枚举校验 + PII/敏感列确定性脱敏 + sha256 幂等；
- query_recognized_table：kind 过滤 + 字段等值过滤 + count/sum/avg 聚合。

这些是"事实与安全"层，不做语义分类（语义交给总控 agent/Qwen）。
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from yunpai_orchestrator.registry import build_default_registry


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _ctx(tmp_path, task_id: str = "TASK-RECOG-1") -> dict:
    return {"task_id": task_id, "recognized_db": str(tmp_path / "recognized.sqlite")}


def _wage_csv() -> bytes:
    return "姓名,实发工资,手机号\n张三,7846,13812345678\n李四,5230,13900001111\n".encode("utf-8")


# ---------------------------------------------------------------------------
# sample_file
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sample_file_sniffs_and_samples_csv(tmp_path):
    registry = build_default_registry()
    result = await registry.call(
        "sample_file",
        {"content_b64": _b64(_wage_csv()), "filename": "工资表.csv"},
        _ctx(tmp_path),
    )
    assert result["success"] is True
    data = result["data"]
    assert data["sniff"]["detected_format"] == "csv"
    assert data["sniff"]["match"] is True
    assert data["headers"] == ["姓名", "实发工资", "手机号"]
    assert data["sample_rows"][0]["姓名"] == "张三"
    assert data["content_sampled"] is True
    assert data["sha256"] == hashlib.sha256(_wage_csv()).hexdigest()


@pytest.mark.asyncio
async def test_sample_file_requires_content_b64():
    registry = build_default_registry()
    with pytest.raises(ValueError, match="invalid input"):
        await registry.call("sample_file", {"filename": "x.csv"}, {})


@pytest.mark.asyncio
async def test_sample_file_rejects_invalid_base64(tmp_path):
    registry = build_default_registry()
    result = await registry.call(
        "sample_file", {"content_b64": "!!!not-base64!!!", "filename": "x.csv"}, _ctx(tmp_path)
    )
    assert result["success"] is False
    assert result["code"] == "INVALID_BASE64"


# ---------------------------------------------------------------------------
# ingest_recognized
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_recognized_redacts_pii_and_sensitive_columns(tmp_path):
    registry = build_default_registry()
    sha = hashlib.sha256(b"wage-1").hexdigest()
    result = await registry.call(
        "ingest_recognized",
        {
            "kind": "wage",
            "filename": "工资表.xlsx",
            "sha256": sha,
            "columns": ["姓名", "实发工资", "手机号"],
            "rows": [{"姓名": "张三", "实发工资": 7846, "手机号": "13812345678"}],
            "confidence": 0.95,
        },
        _ctx(tmp_path),
    )
    assert result["success"] is True
    assert result["data"]["inserted_rows"] == 1
    assert result["data"]["duplicate"] is False
    # 读回验证脱敏结果：姓名/工资保留（供跨表计算），手机号（身份类 PII）遮罩。
    rows = await registry.call(
        "query_recognized_table", {"kind": "wage"}, _ctx(tmp_path)
    )
    row = rows["data"]["rows"][0]
    assert row["姓名"] == "张三"
    assert row["实发工资"] == 7846
    assert "13812345678" not in str(row["手机号"]) and "*" in str(row["手机号"])


@pytest.mark.asyncio
async def test_ingest_recognized_redact_can_be_disabled(tmp_path):
    registry = build_default_registry()
    sha = hashlib.sha256(b"wage-no-redact").hexdigest()
    await registry.call(
        "ingest_recognized",
        {
            "kind": "wage",
            "filename": "工资表.xlsx",
            "sha256": sha,
            "columns": ["姓名", "手机号"],
            "rows": [{"姓名": "张三", "手机号": "13812345678"}],
            "redact": False,
        },
        _ctx(tmp_path),
    )
    rows = await registry.call(
        "query_recognized_table", {"kind": "wage"}, _ctx(tmp_path)
    )
    row = rows["data"]["rows"][0]
    assert row["手机号"] == "13812345678"


@pytest.mark.asyncio
async def test_ingest_recognized_is_idempotent_by_sha256(tmp_path):
    registry = build_default_registry()
    sha = hashlib.sha256(b"wage-dup").hexdigest()
    payload = {
        "kind": "wage",
        "filename": "工资表.xlsx",
        "sha256": sha,
        "columns": ["姓名"],
        "rows": [{"姓名": "张三"}],
    }
    first = await registry.call("ingest_recognized", payload, _ctx(tmp_path))
    second = await registry.call("ingest_recognized", payload, _ctx(tmp_path))
    assert first["data"]["inserted_rows"] == 1
    assert second["data"]["duplicate"] is True
    assert second["data"]["inserted_rows"] == 0


@pytest.mark.asyncio
async def test_ingest_recognized_rejects_unknown_kind(tmp_path):
    registry = build_default_registry()
    payload = {
        "kind": "bogus_kind",
        "filename": "x.xlsx",
        "sha256": hashlib.sha256(b"x").hexdigest(),
        "columns": ["a"],
        "rows": [{"a": 1}],
    }
    # schema 层 kind 枚举先行拒绝（agent 契约层面即不可选非法值）。
    with pytest.raises(ValueError, match="invalid input"):
        await registry.call("ingest_recognized", payload, _ctx(tmp_path))
    # 直接调用 handler 兜底（防御性深度）仍返回 INVALID_KIND。
    from yunpai_orchestrator.workers import ingest_recognized as _handler

    direct = await _handler(payload, _ctx(tmp_path))
    assert direct["success"] is False
    assert direct["code"] == "INVALID_KIND"


# ---------------------------------------------------------------------------
# query_recognized_table
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_query_recognized_table_filter_and_aggregate(tmp_path):
    registry = build_default_registry()
    ctx = _ctx(tmp_path)
    await registry.call(
        "ingest_recognized",
        {
            "kind": "production_daily_report",
            "filename": "押出部日报.xlsx",
            "sha256": hashlib.sha256(b"daily-1").hexdigest(),
            "columns": ["日期", "产品型号", "数量"],
            "rows": [
                {"日期": "2026-09-01", "产品型号": "A", "数量": 100},
                {"日期": "2026-09-01", "产品型号": "B", "数量": 200},
            ],
        },
        ctx,
    )
    # 字段等值过滤
    filtered = await registry.call(
        "query_recognized_table", {"kind": "production_daily_report", "filters": {"产品型号": "A"}}, ctx
    )
    assert filtered["data"]["count"] == 1
    assert filtered["data"]["rows"][0]["数量"] == 100
    # sum 聚合
    summed = await registry.call(
        "query_recognized_table",
        {"kind": "production_daily_report", "aggregate": {"op": "sum", "field": "数量"}},
        ctx,
    )
    assert summed["data"]["rows"][0]["sum"] == 300.0


# ---------------------------------------------------------------------------
# 注册与绑定
# ---------------------------------------------------------------------------

def test_recognition_tools_registered_and_bound():
    registry = build_default_registry()
    for name in ("sample_file", "ingest_recognized", "query_recognized_table"):
        assert name in registry.specs, f"{name} 未注册"
        assert name in registry.handlers, f"{name} 未绑定本地 handler"
        assert registry.specs[name].module == "m0"
        assert registry.specs[name].side_effect in {"none", "local_write"}
        assert registry.specs[name].review_gate == "none"
