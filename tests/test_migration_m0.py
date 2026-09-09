"""M0 分片迁移回归（主依据：`_migration/rows-S0.md` + `rows-S1.md`）。

覆盖范围：
- **C1**：4 个 M0 模块（`m0_facts` / `m0_import_store` / `m0_catalog_ingest` /
  `m0_catalog_templates`）随迁 + `m0_backend.M0Store` 候选裁决三方法增量补；
- **23 个 M0 工具**（rows-S1 在 V2 无 handler 的那批）经 `registry.call` 真跑；
- **C2**：ctx `actor` 映射（由 M-INFRA 提供别名，本分片验证 resolve/rollback 用到）；
- **C3**：`data_import_preview` / `data_import_resolve` 的 batch_id 装配；
- **C4**：bridge `_read_m0_entities` 的 `YUNPAI_M0_DB` 进程内分支；
- **⑦ 门补齐**：RULES 条目 ↔ manifest `side_effect`/`review_gate` 逐条对应。

所有 canonical 库都是 `tmp_path` 下的临时库（`YUNPAI_M0_DB` 注入），不碰 `runtime/`。
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from yunpai_orchestrator import binding
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules

M0_MANIFEST = Path("registry-manifests/m0.json")
CTX = {"task_id": "TASK-M0-MIG", "tenant_id": "default", "actor": "tester"}

#: rows-S1 的 28 个工具 + rows-S0 的 4 个识别工具 = 32 个 M0 工具。
M0_TOOLS_28 = (
    "data_import_run", "data_import_status", "data_import_preview", "data_import_resolve",
    "data_import_commit", "data_import_rollback", "data_import_history", "data_import_quarantine",
    "data_catalog_ingest_validate", "data_catalog_ingest_publish",
    "m0_products_import", "m0_orders_import", "m0_boms_import", "m0_materials_import",
    "m0_suppliers_import", "m0_equipment_import", "m0_routes_import", "m0_operations_import",
    "m0_tooling_import", "data_catalog_file_validate", "data_catalog_file_publish",
    "data_catalog_document_candidate_validate", "data_catalog_document_candidate_publish",
    "get_m0_product_overview", "get_m0_product_graph", "list_m0_documents",
    "list_m0_inventory", "list_m0_entities",
)
M0_TOOLS_4 = ("sample_file", "ingest_recognized", "query_recognized_table", "ingest_canonical")

#: 23 个「V2 原先无 handler」的工具（rows-S1）。
NEWLY_PORTED_23 = (
    "data_import_rollback", "data_import_history", "data_import_quarantine",
    "data_catalog_ingest_validate", "data_catalog_ingest_publish",
    "m0_products_import", "m0_orders_import", "m0_boms_import", "m0_materials_import",
    "m0_suppliers_import", "m0_equipment_import", "m0_routes_import", "m0_operations_import",
    "m0_tooling_import", "data_catalog_file_validate", "data_catalog_file_publish",
    "data_catalog_document_candidate_validate", "data_catalog_document_candidate_publish",
    "get_m0_product_overview", "get_m0_product_graph", "list_m0_documents",
    "list_m0_inventory", "list_m0_entities",
)


@pytest.fixture
def canonical_env(tmp_path, monkeypatch):
    """canonical 库 = 临时 sqlite；不配 M0_URL（避免 ingest_canonical 走 HTTP 发布）。"""
    db = tmp_path / "m0-canonical.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))
    monkeypatch.delenv("M0_URL", raising=False)
    return db


@pytest.fixture
def sandbox_env(tmp_path, monkeypatch):
    """不配 YUNPAI_M0_DB → 走 M0SandboxStore（沙箱语义）。"""
    monkeypatch.delenv("YUNPAI_M0_DB", raising=False)
    monkeypatch.setenv("YUNPAI_M0_SANDBOX_DB", str(tmp_path / "sandbox.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    return tmp_path


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _b64_json(value: object) -> str:
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode()).decode()


def _envelope(entity_type: str, business_key: str, payload: dict, **overrides) -> dict:
    """m0.ingest.v1 信封（字段口径见 m0_catalog_ingest.validate_records:361-420）。"""
    record = {
        "schema_version": "m0.ingest.v1",
        "tenant_id": "default",
        "idempotency_key": f"idem-{entity_type}-{business_key}",
        "source": {"system": "m0-migration-test", "external_id": f"ext-{entity_type}-{business_key}",
                   "sha256": _sha(f"{entity_type}:{business_key}")},
        "entity_type": entity_type,
        "identity": {"business_key": business_key},
        "payload": payload,
        "evidence": [{"key": "ev-1", "ref": f"test:{entity_type}:{business_key}"}],
        "review_status": "approved",
        "reviewed_by": "tester",
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------- C1 随迁

def test_c1_m0_modules_and_backend_candidate_methods():
    """C1：4 个 M0 模块可加载；M0Store 补齐候选裁决/发布三方法（INT 版子集缺口）。"""
    from yunpai_orchestrator import (
        m0_catalog_ingest, m0_catalog_templates, m0_facts, m0_import_store,
    )
    from yunpai_orchestrator.m0_backend import M0Store, PostgresM0Store

    assert m0_facts.list_entities and m0_import_store.CanonicalImportStore
    assert m0_catalog_ingest.CatalogService and m0_catalog_templates.adapt_tabular_file
    for store in (M0Store, PostgresM0Store):
        for method in ("resolve_candidate", "list_candidates", "publish_approved"):
            assert callable(getattr(store, method)), f"{store.__name__} 缺 {method}"


def test_all_32_m0_tools_are_registered_and_bound_local():
    registry = build_default_registry()
    bindings = binding.compute_bindings(registry)
    for tool in M0_TOOLS_28 + M0_TOOLS_4:
        assert tool in registry.specs, f"{tool} 未注册"
        assert tool in registry.handlers, f"{tool} 无本地 handler"
        assert bindings[tool] == binding.BindingStatus.BOUND_LOCAL, f"{tool} 绑定态={bindings[tool]}"
    for tool in NEWLY_PORTED_23:
        assert tool in registry.handlers


def test_m0_facts_merges_attributes_into_top_level(tmp_path, monkeypatch):
    """读口归一化：`payload.attributes` 的键并入顶层、同名以顶层优先（父会话裁决）。"""
    from yunpai_orchestrator.m0_catalog_ingest import CatalogService
    from yunpai_orchestrator.m0_facts import document_rows

    db = tmp_path / "canonical.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(db))
    svc = CatalogService(db)
    record = _envelope("document", "DOC-1", {
        "document_no": "DOC-1", "revision": "A", "role": "sop", "title": "作业指导书",
        "product_codes": ["P-1"], "status": "active",
        "attributes": {"route_steps": [{"seq": 10}], "title": "被顶层覆盖"},
    }, identity={"business_key": "DOC-1", "version_id": "A"})
    svc.publish_records([record], tenant_id="default", task_id="TASK-M0-MIG", actor="tester")

    rows = document_rows(tenant_id="default")
    assert rows and rows[0]["route_steps"] == [{"seq": 10}], rows
    assert rows[0]["title"] == "作业指导书"  # 顶层优先


# ------------------------------------------------------- 23 个工具真跑

@pytest.mark.asyncio
async def test_canonical_import_lifecycle(canonical_env):
    """data_import_run → status → preview → resolve → commit → history → rollback。"""
    registry = build_default_registry()
    record = _envelope("material", "M-1001", {"name": "六角螺栓", "unit": "PCS", "status": "active"})

    run = await registry.call("data_import_run",
                              {"files": [{"filename": "material.json", "content_b64": _b64_json(record)}]},
                              CTX)
    assert run["canonical"] is True and run["environment"] == "local_canonical"
    assert run["status"] == "awaiting_review" and run["candidates"], run
    batch_id = run["batch_id"]

    status = await registry.call("data_import_status", {"batch_id": batch_id}, CTX)
    assert status["batch_id"] == batch_id and status["canonical"] is True

    preview = await registry.call("data_import_preview", {"batch_id": batch_id}, CTX)
    assert preview["documents"][0]["document_kind"] == "material"

    resolved = await registry.call(
        "data_import_resolve", {"batch_id": batch_id, "kind": "entity", "id": 1, "action": "approve"}, CTX)
    assert resolved["status"] == "approved" and resolved["actor"] == "tester"  # C2：ctx["actor"]

    commit = await registry.call("data_import_commit", {"batch_id": batch_id, "require_resolved": True}, CTX)
    assert commit["status"] == "published" and commit["canonical"] is True
    assert commit["readback"]["available"] is True
    assert commit["readback"]["ledger_count"] >= 1 and commit["readback"]["outbox_count"] >= 1

    history = await registry.call("data_import_history", {}, CTX)
    assert any(item["batch_id"] == batch_id for item in history["batches"])

    rollback = await registry.call("data_import_rollback", {"batch_id": batch_id}, CTX)
    assert rollback["status"] == "rolled_back" and rollback["rolled_back"] >= 1
    assert rollback["actor"] == "tester"


@pytest.mark.asyncio
async def test_commit_blocks_on_pending_review(canonical_env):
    """未裁决候选禁止 commit（canonical 语义；W913 死锁的正面断言）。"""
    registry = build_default_registry()
    record = _envelope("material", "M-1002", {"name": "垫片", "unit": "PCS"})
    run = await registry.call("data_import_run",
                              {"files": [{"filename": "m.json", "content_b64": _b64_json(record)}]}, CTX)
    commit = await registry.call("data_import_commit",
                                 {"batch_id": run["batch_id"], "require_resolved": True}, CTX)
    assert commit["code"] == "BLOCKED_INPUT"
    assert commit["errors"][0]["code"] == "PENDING_REVIEW"


@pytest.mark.asyncio
async def test_quarantine_and_history_list_canonical_state(canonical_env):
    registry = build_default_registry()
    junk = base64.b64encode(b"this is not json").decode()
    run = await registry.call("data_import_run",
                              {"files": [{"filename": "notes.txt", "content_b64": junk}]}, CTX)
    assert run["status"] == "failed" and run["quarantined"][0]["filename"] == "notes.txt"
    quarantine = await registry.call("data_import_quarantine",
                                     {"batch_id": run["batch_id"]}, CTX)
    assert quarantine["count"] == 1
    assert quarantine["items"][0]["filename"] == "notes.txt"
    assert quarantine["quarantined"] == quarantine["items"]  # 兼容键同源


@pytest.mark.asyncio
async def test_catalog_ingest_validate_and_publish(canonical_env):
    registry = build_default_registry()
    record = _envelope("material", "M-2001", {"name": "螺母", "unit": "PCS"})
    dry = await registry.call("data_catalog_ingest_validate", {"records": [record]}, CTX)
    assert dry["data"]["valid"] is True and dry["data"]["publishable"] is True

    published = await registry.call("data_catalog_ingest_publish", {"records": [record]}, CTX)
    assert published["success"] is True and published["data"]["published"] == 1

    again = await registry.call("data_catalog_ingest_publish", {"records": [record]}, CTX)
    assert again["data"]["duplicates"] == 1 and again["data"]["published"] == 0

    # 语义非法（schema 合法但 entity_type 不在白名单）→ 校验报告如实报错，不静默发布
    bad = await registry.call("data_catalog_ingest_validate",
                              {"records": [_envelope("unknown_entity", "X-1", {"name": "x"})]}, CTX)
    assert bad["data"]["valid"] is False
    assert bad["data"]["summary"]["errors"] >= 1


@pytest.mark.asyncio
async def test_document_candidate_validate_and_publish(canonical_env):
    registry = build_default_registry()
    product = _envelope("product", "P-1", {"name": "整机", "model": "P-1", "status": "active"})
    await registry.call("m0_products_import", {"records": [product]}, CTX)

    candidate = {
        "schema_version": "m0.document-candidate.v1",
        "tenant_id": "default",
        "source": {"system": "m0-migration-test", "external_id": "doc-cand-1",
                   "sha256": _sha("doc-cand-1")},
        "document_no": "DOC-CAND-1", "revision": "A", "role": "sop", "title": "装配作业指导书",
        "product_codes": ["P-1"],
        "evidence": [{"key": "ev-1", "ref": "test:doc-cand-1"}],
        "review_status": "approved",
        "reviewed_by": "tester",
    }
    dry = await registry.call("data_catalog_document_candidate_validate", {"candidates": [candidate]}, CTX)
    assert dry["data"]["publishable"] is True, dry["data"]

    published = await registry.call("data_catalog_document_candidate_publish",
                                    {"candidates": [candidate]}, CTX)
    assert published["success"] is True
    assert published["data"]["publication"]["published"] == 1

    docs = await registry.call("list_m0_documents", {"product": "P-1"}, CTX)
    assert docs["data"]["count"] == 1
    assert docs["data"]["documents"][0]["role"] == "sop"


@pytest.mark.asyncio
async def test_catalog_file_validate_and_publish(canonical_env):
    registry = build_default_registry()
    csv_text = "material_code,name,unit,status\nM-3001,垫圈,PCS,active\n"
    file_value = {"filename": "materials.csv", "content_b64": base64.b64encode(csv_text.encode()).decode()}
    base = {"file": file_value, "template_version": "m0.material.v1",
            "source_system": "m0-migration-test", "source_external_id": "file-1"}

    dry = await registry.call("data_catalog_file_validate", dict(base), CTX)
    assert dry["data"]["adaptation"]["valid"] is True, dry["data"]

    published = await registry.call("data_catalog_file_publish",
                                    {**base, "review_status": "approved"}, CTX)
    assert published["success"] is True
    assert published["data"]["publication"]["published"] == 1
    assert published["data"]["publication"]["catalog_counts"]["entities"] >= 1

    rows = await registry.call("list_m0_entities", {"entity_type": "material"}, CTX)
    assert any(row["canonical_key"] == "M-3001" for row in rows["data"]["entities"])


@pytest.mark.asyncio
async def test_facade_publishes_and_rejects_mixed_types(canonical_env):
    registry = build_default_registry()
    materials = [_envelope("material", "M-4001", {"name": "弹簧", "unit": "PCS"}),
                 _envelope("material", "M-4002", {"name": "卡簧", "unit": "PCS"})]
    result = await registry.call("m0_materials_import", {"records": materials}, CTX)
    assert result["success"] is True and result["data"]["published"] == 2
    assert result["data"]["catalog_counts"]["entities"] == 2

    mixed = await registry.call("m0_products_import", {"records": materials}, CTX)
    assert mixed["success"] is False and mixed["code"] == "ENTITY_TYPE_MISMATCH"

    # 未审核记录不得发布（facade 只发 approved）
    pending = _envelope("supplier", "S-1", {"name": "某供应商"},
                        review_status="candidate", reviewed_by=None)
    pending.pop("reviewed_by")
    review = await registry.call("m0_suppliers_import", {"records": [pending]}, CTX)
    assert review["success"] is False and review["code"] == "REVIEW_REQUIRED"


@pytest.mark.asyncio
async def test_read_tools_over_canonical_db(canonical_env):
    """5 个读工具：list_m0_entities / inventory / documents / product_overview / graph。"""
    registry = build_default_registry()
    product = _envelope("product", "P-2", {"name": "电机", "model": "P-2", "status": "active"})
    bom = _envelope("bom", "BOM-2", {
        "product_code": "P-2", "status": "active",
        "lines": [{"product_code": "P-2", "material_code": "M-5001", "quantity_per": 2, "unit": "PCS"}],
    }, identity={"business_key": "BOM-2", "version_id": "A"})
    inventory = _envelope("inventory", "M-5001",
                          {"material_code": "M-5001", "available_qty": 12, "warehouse": "WH-1"})
    await registry.call("m0_products_import", {"records": [product]}, CTX)
    await registry.call("m0_boms_import", {"records": [bom]}, CTX)
    # inventory 不在 catalog 白名单（走批次导入链），用 data_import_* 建 canonical 事实
    run = await registry.call(
        "data_import_run", {"files": [{"filename": "inv.json", "content_b64": _b64_json(inventory)}]}, CTX)
    await registry.call("data_import_resolve",
                        {"batch_id": run["batch_id"], "kind": "entity", "id": 1, "action": "approve"}, CTX)
    await registry.call("data_import_commit",
                        {"batch_id": run["batch_id"], "require_resolved": True}, CTX)

    entities = await registry.call("list_m0_entities", {"entity_type": "material"}, CTX)
    assert entities["data"]["count"] == 0  # 没有 material 实体时如实返回 0，不伪造

    stock = await registry.call("list_m0_inventory", {"material_code": "M-5001"}, CTX)
    assert stock["data"]["count"] == 1
    assert stock["data"]["inventory"][0]["available_qty"] == 12

    overview = await registry.call("get_m0_product_overview", {"product_code": "P-2"}, CTX)
    assert overview["data"]["found"] is True
    assert overview["data"]["counts"]["bom"] == 1

    graph = await registry.call("get_m0_product_graph", {"product_code": "P-2", "depth": 2}, CTX)
    assert graph["data"]["depth"] == 2
    assert {"key": "M-5001", "type": "material"} in graph["data"]["nodes"]

    docs = await registry.call("list_m0_documents", {}, CTX)
    assert docs["data"]["count"] == 0


@pytest.mark.asyncio
async def test_m0_canonical_tools_fail_closed_without_db(sandbox_env):
    """未配 YUNPAI_M0_DB：canonical 专属工具必须显式报错，不得伪造成功。"""
    registry = build_default_registry()
    for tool, payload in (
        ("data_import_history", {}),
        ("data_import_quarantine", {}),
        ("data_import_rollback", {"batch_id": "batch-x"}),
        ("data_catalog_ingest_validate", {"records": [_envelope("material", "M-9", {"name": "x"})]}),
        ("m0_materials_import", {"records": [_envelope("material", "M-9", {"name": "x"})]}),
        ("list_m0_entities", {"entity_type": "material"}),
        ("get_m0_product_overview", {"product_code": "P-9"}),
    ):
        with pytest.raises(ValueError, match="YUNPAI_M0_DB"):
            await registry.call(tool, payload, CTX)


# ------------------------------------------------------- ⑦ 门补齐

#: rows-S1 判定「审查需补」的写工具 → (RULES 门, manifest side_effect)。
GATED_WRITE_TOOLS = {
    "data_import_run": ("candidate", "local_write"),
    "data_import_resolve": ("candidate", "local_write"),
    "data_import_rollback": ("candidate", "local_write"),
    "data_catalog_ingest_publish": ("candidate", "external_write"),
    "data_catalog_document_candidate_publish": ("candidate", "external_write"),
    "data_catalog_file_publish": ("candidate", "external_write"),
    "m0_products_import": ("candidate", "external_write"),
    "m0_orders_import": ("candidate", "external_write"),
    "m0_boms_import": ("candidate", "external_write"),
    "m0_materials_import": ("candidate", "external_write"),
    "m0_suppliers_import": ("candidate", "external_write"),
    "m0_equipment_import": ("candidate", "external_write"),
    "m0_routes_import": ("candidate", "external_write"),
    "m0_operations_import": ("candidate", "external_write"),
    "m0_tooling_import": ("candidate", "external_write"),
}


def _manifest_tool(name: str) -> dict:
    data = json.loads(M0_MANIFEST.read_text(encoding="utf-8"))
    return next(tool for tool in data["tools"] if tool["name"] == name)


def test_gate_declarations_match_rules_table():
    """每条门补齐：manifest 声明 side_effect/review_gate ↔ RULES 条目一一对应。"""
    registry = build_default_registry()
    for tool, (gate, side_effect) in GATED_WRITE_TOOLS.items():
        spec = registry.specs[tool]
        assert spec.side_effect == side_effect, f"{tool} side_effect={spec.side_effect}"
        assert spec.review_gate == gate, f"{tool} review_gate={spec.review_gate}"
        # manifest 只被 check_contracts 读；真正开门的是 RULES 表 → 两处都要有
        assert tool in rules.RULES, f"{tool} 缺 RULES 条目（门不会生效）"
        assert rules.gate_type_for(tool, spec) == gate


def test_data_import_run_gate_fires_and_failed_is_terminal():
    """W913 死锁修复：候选登记 → candidate 门；候选为空 → fail（终态）。"""
    findings = rules.evaluate("data_import_run", {"status": "awaiting_review", "candidates": [{"id": 1}]})
    assert [f["gate"] for f in findings if f["gate"]] == ["candidate"]
    empty = rules.evaluate("data_import_run", {"status": "failed", "candidates": []})
    assert [f["action"] for f in empty] == ["fail"]


def test_m0_write_tools_open_candidate_gate_on_success():
    for tool in GATED_WRITE_TOOLS:
        if tool == "data_import_run":  # 该工具的门按 candidates 触发（见下条断言）
            findings = rules.evaluate(tool, {"status": "awaiting_review", "candidates": [{"id": 1}]})
        elif tool == "data_import_resolve":  # 裁决门只在 approve 结果上开
            findings = rules.evaluate(tool, {"status": "approved"})
        elif tool == "data_import_rollback":  # 回滚门只在 rolled_back 结果上开
            findings = rules.evaluate(tool, {"status": "rolled_back"})
        else:
            findings = rules.evaluate(tool, {"success": True, "status": "published"})
        assert any(f.get("gate") == "candidate" for f in findings), f"{tool} 未开 candidate 门"


def test_blocked_input_gate_carries_diagnostics():
    """blocked_input 门必须能回答「缺什么、怎么补」（W913 不可诊断问题的回归）。"""
    from yunpai_orchestrator.reviewer.gates import make_gate

    result = {
        "success": False, "status": "blocked", "code": "BLOCKED_INPUT",
        "errors": [{"code": "PENDING_REVIEW", "message": "batch batch-1 仍有 1 个候选未裁决，禁止 commit"}],
        "data": {"missing_fields": ["data_import_run.batch_id"]},
    }
    finding = rules.evaluate("data_import_commit", result)[0]
    assert finding["gate"] == "blocked_input"
    assert finding["code"] == "PENDING_REVIEW"
    assert "未裁决" in finding["message"]
    assert finding["missing_fields"] == ["data_import_run.batch_id"]

    gate = make_gate(finding["gate"], "data_import_commit", finding["reason"],
                     step_id="m0-3", payload_digest="digest",
                     code=finding["code"], message=finding["message"],
                     missing_fields=finding["missing_fields"])
    assert gate["code"] == "PENDING_REVIEW"
    assert gate["message"].startswith("batch batch-1")
    assert gate["missing_fields"] == ["data_import_run.batch_id"]
    # 不传诊断字段时 Gate 形状与旧版逐字一致（向后兼容）
    legacy = make_gate("candidate", "ingest_canonical", "reason")
    assert set(legacy) == {"type", "tool", "step_id", "reason", "allowed_roles",
                           "payload_digest", "opened_at"}

    # M2 skill 的补数问句（无 errors/missing_fields）也要能变成门上的可诊断信息
    m2_like = {"code": "BLOCKED_INPUT", "status": "human_input_required",
               "data": {"open_customer_questions": [
                   {"field": "product_code_or_bom", "question": "请补充产品编码和已确认 BOM 行"}]}}
    finding2 = rules.evaluate("run_bom_sop_workflow", m2_like)[0]
    assert finding2["gate"] == "blocked_input"
    assert finding2["message"] == "请补充产品编码和已确认 BOM 行"
    assert finding2["missing_fields"] == ["product_code_or_bom"]


def test_data_import_commit_keeps_fail_rule_and_blocked_input_path():
    """rows-S1「审查需决策」二选一的落地：失败=终态 fail；输入缺口=blocked_input 可恢复门。"""
    assert [f["action"] for f in rules.evaluate("data_import_commit", {"success": False})] == ["fail"]
    blocked = rules.evaluate("data_import_commit", {"code": "BLOCKED_INPUT", "errors": []})
    assert blocked and blocked[0]["gate"] == "blocked_input"


# ------------------------------------------------- C3/C4 装配与读口

def _state(**request) -> dict:
    return {"task_id": "TASK-M0-MIG", "tenant_id": "default", "run_id": "RUN-M0",
            "request": request, "outputs": {}}


def test_bridge_assembles_preview_and_resolve_batch_id():
    """C3：preview/resolve 的 batch_id 从上游 data_import_run 产出回填。"""
    from yunpai_orchestrator.orchestration_bridge import bridge_payload

    state = _state(kind="entity", id=1, action="approve")
    state["outputs"] = {"data_import_run": {"batch_id": "batch-77"}}

    preview = bridge_payload(state, "data_import_preview")
    assert preview == {"batch_id": "batch-77"}

    resolve = bridge_payload(state, "data_import_resolve")
    assert resolve == {"batch_id": "batch-77", "kind": "entity", "id": 1, "action": "approve"}

    # 缺批次 → 结构化 BLOCKED_INPUT（不静默空 dict）
    empty = bridge_payload(_state(kind="entity", id=1, action="approve"), "data_import_preview")
    assert empty["code"] == "BLOCKED_INPUT"
    assert empty["data"]["missing_fields"] == ["data_import_run.batch_id"]


def test_bridge_resolve_refuses_fabricated_decision():
    """禁止伪造裁决：kind/action 必须显式且在合同枚举内。"""
    from yunpai_orchestrator.orchestration_bridge import bridge_payload

    state = _state(action="approve", id=1)
    state["outputs"] = {"data_import_run": {"batch_id": "batch-77"}}
    with pytest.raises(ValueError, match="kind"):
        bridge_payload(state, "data_import_resolve")
    state = _state(kind="entity", action="auto")
    state["outputs"] = {"data_import_run": {"batch_id": "batch-77"}}
    with pytest.raises(ValueError, match="action"):
        bridge_payload(state, "data_import_resolve")


def test_bridge_reads_canonical_db_in_process(canonical_env):
    """C4：配 YUNPAI_M0_DB 时进程内读 canonical（不依赖 M0_URL）。"""
    from yunpai_orchestrator.m0_catalog_ingest import CatalogService
    from yunpai_orchestrator.orchestration_bridge import _read_m0_entities

    CatalogService(canonical_env).publish_records(
        [_envelope("equipment", "EQ-1", {"name": "注塑机", "status": "active"})],
        tenant_id="default", task_id="TASK-M0-MIG", actor="tester")
    entities = _read_m0_entities({"tenant_id": "default"}, "equipment")
    assert [item["canonical_key"] for item in entities] == ["EQ-1"]


# ------------------------------------------------- C5 契约对齐（R1 同步）

def test_manifest_carries_r1_contract_fixes():
    """C5：R1 的 8 处 schema/description 修正已进 V2 m0.json。"""
    status = _manifest_tool("data_import_status")
    assert "canonical" in status["output_schema"]["properties"]
    assert status["output_schema"]["properties"]["candidates"]["type"] == "object"

    preview = _manifest_tool("data_import_preview")
    assert set(preview["output_schema"]["properties"]) == {
        "batch_id", "documents", "canonical", "environment"}

    rollback = _manifest_tool("data_import_rollback")
    assert {"rolled_back", "rolled_back_entities"} <= set(rollback["output_schema"]["properties"])

    quarantine = _manifest_tool("data_import_quarantine")
    assert "limit" in quarantine["input_schema"]["properties"]
    assert {"items", "quarantined", "count"} <= set(quarantine["output_schema"]["properties"])

    graph = _manifest_tool("get_m0_product_graph")
    assert graph["input_schema"]["properties"]["depth"]["maximum"] == 2
    assert graph["description"].startswith("deprecated")

    documents = _manifest_tool("list_m0_documents")
    assert "order" not in documents["input_schema"]["properties"]

    inventory = _manifest_tool("list_m0_inventory")
    assert "inventory" in inventory["output_schema"]["properties"]["data"]["properties"]

    for facade in ("m0_products_import", "m0_boms_import", "m0_suppliers_import"):
        assert "入口定位" in _manifest_tool(facade)["description"]


def test_recognition_contract_matches_handlers():
    """rows-S0 四件套契约收敛：字段与 handler 实际读取的键一致。"""
    local = json.loads(Path("registry-manifests/local.json").read_text(encoding="utf-8"))
    tools = {tool["name"]: tool for tool in local["tools"]}

    sample = tools["sample_file"]["input_schema"]["properties"]
    assert "max_rows" in sample and "sample_rows" not in sample
    assert sample["max_rows"]["default"] == 10 and sample["max_rows"]["maximum"] == 50

    recognized = tools["ingest_recognized"]["input_schema"]["properties"]
    assert {"confidence", "redact"} <= set(recognized)
    assert recognized["columns"]["items"] == {"type": "string"}
    assert recognized["rows"]["items"] == {"type": "object"}

    query = tools["query_recognized_table"]["input_schema"]["properties"]
    assert "top_k" not in query and "group_by" not in query
    assert query["aggregate"]["properties"]["group_by"]["type"] == "string"
    assert query["limit"]["default"] == 200

    canonical = tools["ingest_canonical"]["input_schema"]["properties"]
    assert "publish" not in canonical and "tenant_id" not in canonical
    assert {"filename", "sha256", "confidence"} <= set(canonical)
    # 门不降级：candidate 仍由 manifest + RULES 双声明
    assert tools["ingest_canonical"]["review_gate"] == "candidate"
