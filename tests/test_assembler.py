"""书二 §5.1：唯一装配器——free/workflow 共用 bridge；缺口=结构化 BLOCKED_INPUT。"""
import pytest

from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.state import new_state_v2
from yunpai_orchestrator.worker.assembler import Assembler, blocked_envelope


@pytest.fixture(scope="module")
def assembler():
    return Assembler(build_default_registry())


def _state_with_order_attachment():
    return new_state_v2({
        "message": "全链",
        "attachments": [{
            "filename": "PO-1.xlsx", "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content_b64": "QUJD", "kind": "order",
        }],
    })


@pytest.mark.asyncio
async def test_ingest_document_from_order_attachment(assembler):
    state = _state_with_order_attachment()
    asm = await assembler.assemble("ingest_document", state)
    assert not asm.blocked
    assert asm.payload["file"]["filename"] == "PO-1.xlsx"


@pytest.mark.asyncio
async def test_missing_attachment_blocks_with_structure(assembler):
    state = new_state_v2({"message": "全链", "attachments": []})
    asm = await assembler.assemble("ingest_document", state)
    assert asm.blocked
    assert asm.payload["code"] == "BLOCKED_INPUT"
    assert asm.missing and asm.missing[0]["field"]


@pytest.mark.asyncio
async def test_data_import_commit_needs_batch_id(assembler):
    state = new_state_v2({"message": "m"})
    asm = await assembler.assemble("data_import_commit", state)
    assert asm.blocked and asm.payload["code"] == "BLOCKED_INPUT"


@pytest.mark.asyncio
async def test_explicit_params_override_bridge(assembler):
    state = _state_with_order_attachment()
    state["request"]["ingest_document"] = {"extra_flag": "1"}
    asm = await assembler.assemble("ingest_document", state)
    assert asm.payload["extra_flag"] == "1"
    assert asm.payload["file"]["filename"] == "PO-1.xlsx"


def test_blocked_envelope_shape():
    env = blocked_envelope("t", [{"field": "x", "expected_from": "上游", "hint": ""}])
    assert env["code"] == "BLOCKED_INPUT"
    assert env["data"]["missing"][0]["field"] == "x"
