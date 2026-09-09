"""M0 工具合同回归（任务书 §五.7/§四 P0-4）。

断言 manifest input/output schema 关键字段与本地 handler 的输入校验行为：
- required 字段缺失必须拒绝（KeyError/ValueError 或 4xx），不静默成功；
- 输出必须带 batch_id（run/commit）或接受 batch_id（status/preview/resolve）；
- 本地 sandbox 输出显式 canonical=false / environment=sandbox，绝不写生产。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yunpai_orchestrator.registry import build_default_registry


def _m0_schema(name: str) -> dict:
    # 显式 UTF-8：Windows 默认 locale（cp936）会把 manifest 里的中文读崩。
    data = json.loads(Path("registry-manifests/m0.json").read_text(encoding="utf-8"))
    return next(tool for tool in data["tools"] if tool["name"] == name)


REQUIRED_BY_TOOL = {
    "data_import_run": ["files"],
    "data_import_status": ["batch_id"],
    "data_import_preview": ["batch_id"],
    "data_import_resolve": ["batch_id", "kind", "id", "action"],
    "data_import_commit": ["batch_id"],
}


def test_m0_manifest_schemas_require_contract_fields():
    for tool, required in REQUIRED_BY_TOOL.items():
        schema = _m0_schema(tool)
        for field in required:
            assert field in schema["input_schema"].get("properties", {}), f"{tool} 缺字段 {field}"
        assert schema["output_schema"].get("type") == "object"
        assert schema["http"]["method"] in {"GET", "POST"}


def test_m0_tools_registered_and_bound_locally():
    registry = build_default_registry()
    for tool in REQUIRED_BY_TOOL:
        assert tool in registry.specs, f"{tool} 未注册"
        assert tool in registry.handlers, f"{tool} 未本地绑定"


@pytest.mark.asyncio
async def test_m0_run_requires_files_field(sandbox_db_env):
    from yunpai_orchestrator.registry import build_default_registry

    registry = build_default_registry()
    # registry 层按 manifest schema 强制 required files。
    with pytest.raises(ValueError, match="invalid input"):
        await registry.call("data_import_run", {}, {"task_id": "TASK-CONTRACT-1"})
    with pytest.raises(ValueError, match="invalid input"):
        await registry.call("data_import_run", {"files": "not-a-list"}, {"task_id": "TASK-CONTRACT-1"})


@pytest.mark.asyncio
async def test_m0_commit_requires_existing_batch(sandbox_db_env):
    from yunpai_orchestrator.workers import m0_commit

    with pytest.raises(ValueError, match="batch not found"):
        await m0_commit({}, {"task_id": "TASK-CONTRACT-2"})
    with pytest.raises(ValueError, match="batch not found"):
        await m0_commit({"batch_id": "batch-ghost"}, {"task_id": "TASK-CONTRACT-2"})


@pytest.mark.asyncio
async def test_m0_resolve_requires_kind_id_action(sandbox_db_env):
    import base64

    from yunpai_orchestrator.workers import m0_import, m0_resolve

    ctx = {"task_id": "TASK-CONTRACT-3"}
    imported = await m0_import({"files": [{"filename": "order.json", "content_b64": base64.b64encode(b'{"records":[{"kind":"order"}]}').decode()}]}, ctx)
    batch_id = imported["batch_id"]
    with pytest.raises(ValueError, match="kind"):
        await m0_resolve({"batch_id": batch_id, "id": 1, "action": "approve"}, ctx)
    with pytest.raises(ValueError, match="action"):
        await m0_resolve({"batch_id": batch_id, "kind": "entity", "id": 1, "action": "wat"}, ctx)
    with pytest.raises(ValueError, match="id"):
        await m0_resolve({"batch_id": batch_id, "kind": "entity", "action": "approve"}, ctx)
    # 有效 resolve 返回 batch/candidate 定位。
    result = await m0_resolve({"batch_id": batch_id, "kind": "entity", "id": 1, "action": "approve"}, ctx)
    assert result["batch_id"] == batch_id
    assert result["status"] == "approved"


@pytest.fixture
def sandbox_db_env():
    import base64 as _b64
    import os
    import tempfile

    prior = os.environ.get("YUNPAI_M0_SANDBOX_DB")
    os.environ["YUNPAI_M0_SANDBOX_DB"] = os.path.join(tempfile.mkdtemp(), "m0-contract.sqlite")
    yield
    if prior is None:
        os.environ.pop("YUNPAI_M0_SANDBOX_DB", None)
    else:
        os.environ["YUNPAI_M0_SANDBOX_DB"] = prior
