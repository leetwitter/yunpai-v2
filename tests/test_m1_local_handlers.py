"""M1 任务/文档链本地 handler 端到端验收（S2 M1 12 个工具「改造后搬」）。

覆盖 ``_migration/rows-S2.md`` 的逐条改造项与「需先修」：

- ``ingest_document`` 真实本地解析 + 任务持久化（替换 V2 fixture/preview 版）；
- ``ingest_m1_archive`` 安全解包 → 父子任务 + ``get_m1_batch`` 计数；
- ``get_m1_task``/``get_m1_batch``/``get_m1_document`` 的**契约平铺形状**
  （旧信封会被 ``registry.call`` 输出校验打红，rows-S2「需先修」R2-3/4/5）；
- ``list_m1_tasks``/``list_m1_review_queue``/``search_m1_orders``/
  ``search_m1_documents`` 的契约数组形状与显式失败；
- ``submit_m1_review`` 的 ctx 键映射（``actor_user``/``actor``）+ 修正真正生效；
- ``export_m1_order`` 的 list-into-cell 崩溃修复 + 导出目录租户隔离；
- ``generate_m1_report`` 的 ``force`` 缓存语义。

所有数值均为虚构合成数据。
"""

from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest

from yunpai_orchestrator.registry import build_default_registry

CTX = {"task_id": "TASK-ROOT", "run_id": "RUN-ROOT", "tenant_id": "TENANT-1",
       "actor_user": "reviewer-1", "actor": "reviewer-1"}

ORDER = {
    "order_id": "SO-M1-1", "product_code": "W-1", "quantity": 5, "due_date": "2026-09-10",
    "lines": [
        {"product_code": "W-1", "model": "HDMI 20M", "quantity": 5, "uom": "PCS",
         "name": "HDMI 2.0 20M", "product_category": "cable"},
    ],
}


def _file_payload(raw: bytes, filename: str, content_type: str = "application/json") -> dict:
    return {"file": {"filename": filename, "content_type": content_type,
                     "content_b64": base64.b64encode(raw).decode()}}


def _json_file(document: dict, filename: str = "order.json") -> dict:
    return _file_payload(json.dumps(document).encode("utf-8"), filename)


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    """每个用例独立的 M1 领域库与导出目录（本地 handler 真实落库/落盘）。"""
    monkeypatch.setenv("YUNPAI_M1_DB", str(tmp_path / "m1.sqlite"))
    monkeypatch.setenv("YUNPAI_M1_EXPORT_DIR", str(tmp_path / "exports"))


@pytest.fixture
def registry():
    return build_default_registry()


# ---------------------------------------------------------------------------
# ingest_document / ingest_m1_archive
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_document_persists_task_document_and_evidence(registry):
    result = await registry.call("ingest_document", _json_file(ORDER), CTX)
    assert result["status"] == "done"
    assert result["provider"] == "local" and result["fixture"] is False
    document = result["document"]
    assert document["schema_version"] == "m1.document.v2"
    assert document["header"]["order_id"] == "SO-M1-1"
    assert document["source"]["sha256"]
    assert result["evidence"]

    task = await registry.call("get_m1_task", {"task_id": result["task_id"]}, CTX)
    assert task["task_id"] == result["task_id"] and task["status"] == "done"
    assert task["document_schema_version"] == "m1.document.v2"
    document_back = await registry.call("get_m1_document", {"task_id": result["task_id"]}, CTX)
    assert document_back["source"]["original_filename"] == "order.json"
    assert document_back["header"]["order_id"] == "SO-M1-1"


@pytest.mark.asyncio
async def test_ingest_document_contract_hints_change_labels_not_facts(registry):
    """``doc_type_hint``/``document_subtype_hint``/``semantic_enrichment`` 生效，
    且只改分类标签与可选语义字段，**不动**源文件事实（rows-S2 盘点第 6 类）。"""
    hinted = await registry.call("ingest_document", {
        **_json_file(ORDER, "hinted.json"),
        "doc_type_hint": "purchase_order", "document_subtype_hint": "blanket",
        "semantic_enrichment": False,
    }, CTX)
    document = hinted["document"]
    assert document["document_type"] == "purchase_order"
    assert document["document_subtype"] == "blanket"
    assert document["header"]["order_id"] == "SO-M1-1"          # 事实未变
    line = document["lines"][0]
    assert line["quantity"] == 5 and line["line_id"]            # 基础事实保留
    for optional in ("name_normalized", "full_product_name", "product_category", "name_attributes"):
        assert optional not in line, f"semantic_enrichment=False 应剥离 {optional}"


@pytest.mark.asyncio
async def test_ingest_m1_archive_creates_parent_and_child_tasks(registry):
    raw = _zip_bytes({
        "批次/order-a.json": json.dumps(ORDER).encode("utf-8"),
        "批次/order-b.json": json.dumps({**ORDER, "order_id": "SO-M1-2"}).encode("utf-8"),
    })
    result = await registry.call("ingest_m1_archive", _file_payload(raw, "批次.zip", "application/zip"), CTX)
    assert result["status"] == "done"
    assert result["child_count"] == 2 and len(result["child_ids"]) == 2
    assert result["done_count"] == 2 and result["failed_count"] == 0

    batch = await registry.call("get_m1_batch", {"parent_id": result["task_id"]}, CTX)
    # 契约 additionalProperties:false → handler 只返回这 7 键（信封由 registry 层补）。
    assert set(batch["data"]) == {"parent", "children", "child_count", "done_count",
                                 "failed_count", "review_count", "pending_count"}
    assert batch["child_count"] == 2 and batch["parent"]["task_id"] == result["task_id"]
    assert {child["task_id"] for child in batch["children"]} == set(result["child_ids"])


@pytest.mark.asyncio
async def test_ingest_m1_archive_fails_closed_on_unsupported_archive(registry):
    result = await registry.call("ingest_m1_archive", _file_payload(b"Rar!\x1a\x07\x00x", "a.rar", "application/vnd.rar"), CTX)
    assert result["status"] == "failed" and result["code"] == "ARCHIVE_UNSUPPORTED"
    assert result["child_count"] == 0 and result["child_ids"] == []
    assert result["provider"] == "local" and result["fixture"] is False


# ---------------------------------------------------------------------------
# 检索 / 列表
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_and_list_tools_return_contract_shapes(registry):
    partial = await registry.call("ingest_document", _json_file(
        {"order_id": "SO-PARTIAL", "lines": [{"product_code": "W-9", "quantity": 3}]}), CTX)
    assert partial["status"] == "needs_review"          # 缺 header.product_code/due_date

    tasks = await registry.call("list_m1_tasks", {"status": "needs_review"}, CTX)
    assert [t["task_id"] for t in tasks] == [partial["task_id"]]
    assert tasks[0]["needs_review"] is True
    # 有 lines → 解析置信度 1.0；进 review 的原因是 validation_issues（缺 header 字段）。
    assert tasks[0]["overall_confidence"] == 1.0

    queue = await registry.call("list_m1_review_queue", {}, CTX)
    assert [t["task_id"] for t in queue] == [partial["task_id"]]

    orders = await registry.call("search_m1_orders", {"order_number": "SO-PARTIAL"}, CTX)
    assert orders and orders[0]["order_number"] == "SO-PARTIAL"
    assert set(orders[0]) == {
        "task_id", "line_id", "line_no", "order_number", "document_date", "model",
        "product_code", "name_raw", "name_normalized", "full_product_name",
        "product_category", "quantity", "unit", "line"}
    # 契约把纯字符串字段声明为 string：缺字段时归一为空串而不是 None（否则输出校验打红）。
    assert orders[0]["product_code"] == "W-9"
    assert orders[0]["unit"] == "" and orders[0]["name_raw"] == ""

    documents = await registry.call("search_m1_documents", {"q": "SO-PARTIAL"}, CTX)
    assert documents[0]["task_id"] == partial["task_id"]
    assert documents[0]["schema_version"] == "m1.document.v2"
    assert documents[0]["order_number"] == "SO-PARTIAL"


@pytest.mark.asyncio
async def test_search_m1_documents_field_path_subset_fails_loudly(registry):
    await registry.call("ingest_document", _json_file(ORDER), CTX)
    hit = await registry.call("search_m1_documents", {"field_path": "$.lines[*].model", "field_value": "hdmi"}, CTX)
    assert hit and hit[0]["order_number"] == "SO-M1-1"
    with pytest.raises(ValueError, match="field_path 暂不支持"):
        await registry.call("search_m1_documents", {"field_path": "$.totals.a.b.c", "field_value": "x"}, CTX)
    with pytest.raises(ValueError, match="field_value 必须与 field_path 配合使用"):
        await registry.call("search_m1_documents", {"field_value": "x"}, CTX)


# ---------------------------------------------------------------------------
# 人工审核 / 报告 / 导出
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submit_m1_review_applies_corrections_and_reads_actor_from_ctx(registry):
    ingested = await registry.call("ingest_document", _json_file({"order_id": "SO-REV", "quantity": 1}), CTX)
    task_id = ingested["task_id"]
    issue_codes = [issue["code"] for issue in ingested["document"]["validation_issues"]]
    assert "MISSING_FIELD" in issue_codes

    reviewed = await registry.call("submit_m1_review", {
        "task_id": task_id, "approve": True, "comment": "补齐编码",
        "header_corrections": {"product_code": "W-9"},
        "issue_resolutions": [{"code": "MISSING_FIELD", "resolution": "accepted", "comment": "人工确认"}],
    }, CTX)
    assert reviewed["status"] == "done"

    document = await registry.call("get_m1_document", {"task_id": task_id}, CTX)
    assert document["header"]["product_code"] == "W-9"
    # ctx 键映射：payload 没给 reviewer 时取 actor_user（V2 的 tool_context 提供）。
    assert document["review"]["reviewer"] == "reviewer-1"
    assert document["review"]["approved"] is True

    # 非 needs_review 的任务再次审核必须 fail-closed（不静默成功）。
    with pytest.raises(ValueError, match="不在待审核队列"):
        await registry.call("submit_m1_review", {"task_id": task_id, "approve": True}, CTX)


@pytest.mark.asyncio
async def test_submit_m1_review_rejects_unknown_line_id(registry):
    ingested = await registry.call("ingest_document", _json_file({"order_id": "SO-REV2"}), CTX)
    with pytest.raises(ValueError, match="不存在的 line_id"):
        await registry.call("submit_m1_review", {
            "task_id": ingested["task_id"], "approve": True,
            "line_corrections": [{"line_id": "nope", "corrections": {"quantity": 9}}],
        }, CTX)


@pytest.mark.asyncio
async def test_generate_m1_report_force_and_cache_semantics(registry):
    ingested = await registry.call("ingest_document", _json_file(ORDER), CTX)
    task_id = ingested["task_id"]
    first = await registry.call("generate_m1_report", {"task_id": task_id}, CTX)
    assert first["report_status"] == "generated" and first["report_kind"] == "markdown"
    assert "M1 识别报告" in first["message"]
    cached = await registry.call("generate_m1_report", {"task_id": task_id}, CTX)
    assert cached["report_status"] == "cached"
    forced = await registry.call("generate_m1_report", {"task_id": task_id, "force": True, "note": "人工复核"}, CTX)
    assert forced["report_status"] == "generated"
    assert "人工复核" in forced["message"]


@pytest.mark.asyncio
async def test_export_m1_order_survives_validation_issues_and_isolates_tenants(registry, tmp_path):
    """rows-S2「行为缺陷需先修」：``validation_issues[*].paths`` 是 list，INT 版
    直接写单元格会 ``ValueError: Cannot convert [...] to Excel``——只要文档有
    校验问题就必现。"""
    ingested = await registry.call("ingest_document", _json_file({"order_id": "SO-EXP"}), CTX)
    assert ingested["document"]["validation_issues"]          # 确有 paths 列表
    exported = await registry.call("export_m1_order", {"task_id": ingested["task_id"]}, CTX)
    assert exported["generated"] is True
    assert exported["filename"] == "SO-EXP.xlsx"
    assert exported["content_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    # 导出目录按租户隔离（INT 版是 runtime/m1-exports/{task_id}.xlsx 单目录）。
    assert f"/{CTX['tenant_id']}/" in exported["download_url"].replace("\\", "/")
    assert exported["download_url"].startswith("file://")

    # 别的租户读不到该任务（tenant 表级隔离）。
    with pytest.raises(ValueError, match="task not found"):
        await registry.call("export_m1_order", {"task_id": ingested["task_id"]},
                            {**CTX, "tenant_id": "TENANT-2"})
