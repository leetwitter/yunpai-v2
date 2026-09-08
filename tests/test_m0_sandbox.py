from __future__ import annotations

import base64
import json
import os
import tempfile

import pytest

from yunpai_orchestrator.workers import m0_commit, m0_import, m0_preview, m0_resolve, m0_status


@pytest.fixture
def sandbox_env():
    prior = os.environ.get("YUNPAI_M0_SANDBOX_DB")
    os.environ["YUNPAI_M0_SANDBOX_DB"] = os.path.join(tempfile.mkdtemp(), "m0-sandbox.sqlite")
    yield
    if prior is None:
        os.environ.pop("YUNPAI_M0_SANDBOX_DB", None)
    else:
        os.environ["YUNPAI_M0_SANDBOX_DB"] = prior


def _files(*payloads):
    return [{"filename": f"doc-{index}.json", "content_b64": base64.b64encode(json.dumps(item).encode()).decode()} for index, item in enumerate(payloads, start=1)]


@pytest.mark.asyncio
async def test_m0_run_status_preview_resolve_commit_chain(sandbox_env):
    ctx = {"task_id": "TASK-M0-CHAIN", "tenant_id": "default"}
    imported = await m0_import({"files": _files({"records": [{"kind": "order"}]})}, ctx)
    batch_id = imported["batch_id"]
    assert imported["status"] == "awaiting_review"
    assert imported["canonical"] is False
    assert imported["environment"] == "sandbox"

    status = await m0_status({"batch_id": batch_id}, ctx)
    assert status["candidates"].get("candidate", 0) >= 1 or status["candidates"].get("needs_review", 0) >= 1

    preview = await m0_preview({"batch_id": batch_id}, ctx)
    assert preview["documents"]
    resolve_id = preview["documents"][0]["id"]

    decided = await m0_resolve({"batch_id": batch_id, "kind": "entity", "id": resolve_id, "action": "approve"}, ctx)
    assert decided["status"] == "approved"

    # 全部批准后 commit（显式 require_resolved）仍只做 fixture 记录，绝不宣称 canonical。
    committed = await m0_commit({"batch_id": batch_id, "require_resolved": True}, ctx)
    assert committed["status"] == "fixture_recorded"
    assert committed["canonical"] is False
    assert committed["readback"]["available"] is False


@pytest.mark.asyncio
async def test_m0_commit_blocks_unresolved_when_required(sandbox_env):
    ctx = {"task_id": "TASK-M0-BLOCK", "tenant_id": "default"}
    imported = await m0_import({"files": _files({"records": [{"kind": "order"}]}, {"records": [{"kind": "bom"}]})}, ctx)
    blocked = await m0_commit({"batch_id": imported["batch_id"], "require_resolved": True}, ctx)
    assert blocked["status"] == "blocked"
    assert blocked["code"] == "BLOCKED_INPUT"
    assert blocked["errors"][0]["code"] == "PENDING_REVIEW"


@pytest.mark.asyncio
async def test_m0_import_idempotent_replay(sandbox_env):
    ctx = {"task_id": "TASK-M0-IDEM", "tenant_id": "default"}
    files = _files({"records": [{"kind": "order"}]})
    first = await m0_import({"files": files}, ctx)
    second = await m0_import({"files": files}, ctx)
    assert first["batch_id"] == second["batch_id"]
    assert first["status"] == second["status"] == "awaiting_review"


@pytest.mark.asyncio
async def test_m0_commit_rejects_unknown_batch(sandbox_env):
    with pytest.raises(ValueError, match="batch not found"):
        await m0_commit({"batch_id": "batch-missing"}, {"task_id": "TASK-M0-MISSING"})


@pytest.mark.asyncio
async def test_m0_resolve_rejects_bad_action_and_missing_id(sandbox_env):
    ctx = {"task_id": "TASK-M0-RESOLVE", "tenant_id": "default"}
    imported = await m0_import({"files": _files({"records": [{"kind": "order"}]})}, ctx)
    with pytest.raises(ValueError, match="action"):
        await m0_resolve({"batch_id": imported["batch_id"], "kind": "entity", "id": 1, "action": "maybe"}, ctx)
    with pytest.raises(ValueError, match="需要显式 id"):
        await m0_resolve({"batch_id": imported["batch_id"], "kind": "entity", "action": "approve"}, ctx)


@pytest.mark.asyncio
async def test_m0_chain_resolve_all_then_commit_tracks_batch_and_evidence(sandbox_env):
    """编排级证据链：注册 -> preview -> 逐个 resolve -> require_resolved commit。

    候选必须带 source(sha)/document_kind/confidence/review_status；commit 只做
    fixture_recorded（无真实 M0 回读），绝不写入 canonical 断言。
    """
    ctx = {"task_id": "TASK-M0-E2E", "tenant_id": "tenant-1"}
    imported = await m0_import(
        {"files": _files({"records": [{"kind": "order"}]}, {"records": [{"kind": "bom"}]}, {"records": [{"kind": "supplier"}]})},
        ctx,
    )
    batch_id = imported["batch_id"]
    assert imported["readback"]["available"] is False

    preview = await m0_preview({"batch_id": batch_id}, ctx)
    assert len(preview["documents"]) == 3
    for doc in preview["documents"]:
        assert doc["sha256"]
        assert doc["document_kind"] in {"order", "bom", "supplier"}
        assert doc["confidence"] > 0
        assert doc["review_status"] in {"candidate", "needs_review"}

    for doc in preview["documents"]:
        decided = await m0_resolve({"batch_id": batch_id, "kind": "entity", "id": doc["id"], "action": "approve"}, ctx)
        assert decided["status"] == "approved"

    status = await m0_status({"batch_id": batch_id}, ctx)
    assert status["candidates"].get("approved", 0) == 3

    committed = await m0_commit({"batch_id": batch_id, "require_resolved": True}, ctx)
    assert committed["status"] == "fixture_recorded"
    assert committed["canonical"] is False
    assert committed["environment"] == "sandbox"
    assert committed["readback"]["available"] is False
    assert any(item.get("module") == "m0" for item in committed.get("evidence", []))


@pytest.mark.asyncio
async def test_m0_readback_report_dry_run_and_production(sandbox_env):
    ctx = {"task_id": "TASK-M0-RB", "tenant_id": "default"}
    imported = await m0_import({"files": _files({"records": [{"kind": "order"}]})}, ctx)
    batch_id = imported["batch_id"]
    store = __import__("yunpai_orchestrator.m0_sandbox", fromlist=["M0SandboxStore"]).M0SandboxStore(os.environ["YUNPAI_M0_SANDBOX_DB"])
    dry = store.readback_report(batch_id, real_m0_available=False)
    assert dry["canonical_readback_available"] is False
    assert dry["dry_run"] is True
    assert dry["ledger"]["available"] is False
    production = store.readback_report(batch_id, real_m0_available=True)
    assert production["canonical_readback_available"] is True
    assert production["environment"] == "production"


@pytest.mark.asyncio
async def test_m0_import_isolates_bad_files_and_retry_is_idempotent(sandbox_env):
    """混合批次：好文件登记、坏文件 quarantine、整体不 failed；重试不重复写入。"""
    ctx = {"task_id": "TASK-M0-ISOLATE", "tenant_id": "default"}
    good = {"filename": "ok.json", "content_b64": base64.b64encode(json.dumps({"records": [{"kind": "order"}]}).encode()).decode()}
    bad_b64 = {"filename": "bad-base64.json", "content_b64": "!!!not-base64!!!"}
    fake_ext = {"filename": "fake.xlsx", "content_b64": base64.b64encode(b"plain text not xlsx").decode()}
    empty = {"filename": "empty.json", "content_b64": ""}
    files = [good, bad_b64, fake_ext, empty]

    first = await m0_import({"files": files}, ctx)
    assert first["status"] == "awaiting_review"  # 坏文件不使整体失败
    batch_id = first["batch_id"]
    quarantined = {item["filename"] for item in first["quarantined"]}
    assert {"bad-base64.json", "fake.xlsx", "empty.json"} <= quarantined
    preview = await m0_preview({"batch_id": batch_id}, ctx)
    assert [doc["filename"] for doc in preview["documents"]] == ["ok.json"]

    # 重试同输入：成功文件不重复登记。
    second = await m0_import({"files": files}, ctx)
    assert second["batch_id"] == batch_id
    preview_again = await m0_preview({"batch_id": batch_id}, ctx)
    assert len(preview_again["documents"]) == 1

    # 重试仅坏文件：此前成功文件不在 payload，重新登记为新文件也幂等（不同 task 快照各自 batch）。
    retry_ctx = {"task_id": "TASK-M0-ISOLATE-R", "tenant_id": "default"}
    retried = await m0_import({"files": [good, bad_b64]}, retry_ctx)
    assert retried["status"] == "awaiting_review"
    retry_preview = await m0_preview({"batch_id": retried["batch_id"]}, retry_ctx)
    assert len(retry_preview["documents"]) == 1  # 只登记 good
