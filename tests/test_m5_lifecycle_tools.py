"""M5 lifecycle + execution + messages/knowledge tool tests (Tasks 3-5)."""
import asyncio

import pytest
from jsonschema import Draft202012Validator

from yunpai_orchestrator import m5_tools as mt
from yunpai_orchestrator.m5_repository import M5Repository, M5RepositoryError
from yunpai_orchestrator.pmc_v2_adapter import run_pmc_v2
from yunpai_orchestrator.registry import build_default_registry

import json
from pathlib import Path


def _payload():
    return {
        "idempotency_key": "LIFE-1",
        "scenario_id": "SC-LIFE",
        "scenario_purpose": "production",
        "planning_start": "2026-09-03T08:00:00+08:00",
        "calendar_windows": [
            {"calendar_ref": "CAL-A", "shift_code": "DAY",
             "start_at": "2026-09-03T08:00:00+08:00", "end_at": "2026-09-03T17:00:00+08:00"},
        ],
        "route_code": "ROUTE-P1", "route_version": "approved-v1",
        "route_approval_ref": "APPROVED-LIFE-1",
        "orders": [{"order_id": "SO-LIFE-1", "product_id": "P1", "quantity": 2, "uom": "PCS",
                    "due_time": "2026-09-10T17:00:00+08:00"}],
        "resources": [
            {"resource_id": "EQ-A", "name": "设备A", "resource_type": "EQUIPMENT",
             "equipment_type": "press", "capacity_per_hour": 60, "efficiency_factor": 1,
             "calendar_ref": "CAL-A", "status": "available", "capability_codes": ["P"]},
        ],
        "routing_steps": [
            {"product_id": "P1", "operation_id": "OP-10", "sequence": 10,
             "operation_name": "工序10",
             "eligible_resources": [{"resource_id": "EQ-A", "processing_minutes": 5}],
             "standard_minutes": 5, "setup_minutes": 0,
             "required_equipment_codes": ["EQ-A"], "predecessors": [],
             "approval_ref": "APPROVED-LIFE-1"},
            {"product_id": "P1", "operation_id": "OP-20", "sequence": 20,
             "operation_name": "工序20",
             "eligible_resources": [{"resource_id": "EQ-A", "processing_minutes": 5}],
             "standard_minutes": 5, "setup_minutes": 0,
             "required_equipment_codes": ["EQ-A"], "predecessors": ["OP-10"],
             "approval_ref": "APPROVED-LIFE-1"},
        ],
        "supply_entries": [
            {"order_line_id": "SO-LIFE-1::L1", "op_code": "OP-10", "readiness": "READY",
             "requirement_ref": "MAT-1", "inventory_snapshot_ref": "INV-1"},
        ],
    }


@pytest.fixture
def ctx(tmp_path):
    return {"task_id": "TASK-LIFE", "tenant_id": "tenant-life", "m5_db_path": str(tmp_path / "m5.sqlite")}


def _release_plan(repo, ctx, scenario_id="SC-LIFE"):
    """Solve -> persist draft -> approve -> release -> set head (Task 3)."""
    payload = _payload()
    payload["scenario_id"] = scenario_id
    solved = run_pmc_v2(payload)
    digest = solved["data"]["input_hash"]
    version = f"plan-{scenario_id}-{digest[:10]}"
    repo.save_plan(plan_version=version, scenario_id=scenario_id, tenant_id=ctx["tenant_id"],
                   task_id=ctx["task_id"], lifecycle_status="draft", parent_plan_version=None,
                   input_hash=digest, solver_hash="solver-v2-1",
                   algorithm_version="pmc-v2-frozen-20260902", scenario_purpose="production",
                   validation_report=solved["data"]["validator"],
                   bundle=solved["data"]["input_package"], schedule=solved["data"]["schedule"],
                   idempotency_key=f"{scenario_id}:solve")
    repo.transition(version, "approved", gate="review", actor="zhb", task_id=ctx["task_id"])
    repo.transition(version, "released", gate="release", actor="zhb", task_id=ctx["task_id"])
    repo.set_head(scenario_id, version)
    return version


def _schema(name):
    manifest = json.loads(Path("registry-manifests/m5.json").read_text(encoding="utf-8"))
    tool = next(t for t in manifest["tools"] if t["name"] == name)
    return tool["output_schema"]


def test_full_lifecycle_dispatch_and_execution_summary(ctx):
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)
    # progress for released head
    progress = asyncio.run(mt.m5_pmc_progress({"plan_version": version}, ctx))
    assert progress["success"] is True
    Draft202012Validator(_schema("get_m5_pmc_progress")).validate(progress)
    assert progress["data"]["authority"]["is_current_head"] is True
    assert len(progress["data"]["orders"]) == 1

    # dispatch requires released plan
    dispatch = asyncio.run(mt.m5_dispatch_schedule(
        {"plan_version": version, "idempotency_key": "D-1",
         "operation_keys": ["SO-LIFE-1:OP-10", "SO-LIFE-1:OP-20"]}, ctx))
    assert dispatch["success"] is True
    Draft202012Validator(_schema("dispatch_m5_schedule")).validate(dispatch)
    assert dispatch["data"]["status"] == "pending"
    # same idempotency replays
    again = asyncio.run(mt.m5_dispatch_schedule(
        {"plan_version": version, "idempotency_key": "D-1",
         "operation_keys": ["SO-LIFE-1:OP-10"]}, ctx))
    assert again["data"]["replayed"] is True

    # not released plan cannot be dispatched
    repo.save_plan(plan_version="plan-draft-other", scenario_id="SC-LIFE",
                   tenant_id=ctx["tenant_id"], task_id=ctx["task_id"],
                   lifecycle_status="draft", parent_plan_version=None,
                   input_hash="0" * 64, solver_hash="s", algorithm_version="a",
                   scenario_purpose="production", validation_report={},
                   bundle={}, schedule={})
    blocked_dispatch = asyncio.run(mt.m5_dispatch_schedule(
        {"plan_version": "plan-draft-other", "idempotency_key": "D-2",
         "operation_keys": ["SO-LIFE-1:OP-10"]}, ctx))
    assert blocked_dispatch["success"] is False
    assert blocked_dispatch["errors"][0]["code"] == "NOT_RELEASED"

    # execution summary only reflects persisted events
    summary = asyncio.run(mt.m5_execution_summary({"plan_version": version}, ctx))
    assert summary["data"]["event_count"] == 0
    repo.add_execution_event(
        event={"event_id": "EV-1", "event_type": "quantity_report",
               "order_id": "SO-LIFE-1", "operation_id": "OP-10",
               "external_ref": "ref-1", "occurred_at": "2026-09-03T09:00:00+08:00",
               "reported_quantity": 2, "scrap_quantity": 0},
        plan_version=version, tenant_id=ctx["tenant_id"], task_id=ctx["task_id"])
    summary2 = asyncio.run(mt.m5_execution_summary({"plan_version": version}, ctx))
    assert summary2["data"]["event_count"] == 1


def test_progress_rejects_not_released_and_stale_head(ctx):
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)
    # not current head
    repo.save_plan(plan_version="plan-new-draft", scenario_id="SC-LIFE",
                   tenant_id=ctx["tenant_id"], task_id=ctx["task_id"],
                   lifecycle_status="draft", parent_plan_version=version,
                   input_hash="1" * 64, solver_hash="s2", algorithm_version="a",
                   scenario_purpose="production", validation_report={}, bundle={}, schedule={})
    repo.set_head("SC-LIFE", "plan-new-draft")
    with pytest.raises(M5RepositoryError) as exc:
        asyncio.run(mt.m5_pmc_progress({"plan_version": version}, ctx))
    assert exc.value.code == "NOT_CURRENT_HEAD"


def test_replan_from_server_bundle_preserves_parent(ctx):
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)
    result = asyncio.run(mt.m5_replan_schedule({
        "base_plan_version": version,
        "idempotency_key": "REPLAN-1",
        "expected_head_plan_version": version,
        "event": {
            "event_id": "EV-REPLAN-1", "sequence": 1,
            "event_type": "order_cancel",
            "occurred_at": "2026-09-04T10:00:00+08:00", "reason": "客户取消",
            "payload": {"order_ids": ["SO-LIFE-2"]},
        },
    }, ctx))
    assert result["success"] is True
    Draft202012Validator(_schema("replan_m5_schedule")).validate(result)
    assert result["data"]["parent_plan_version"] == version
    plan = repo.get_plan(result["data"]["plan_version"])
    assert plan["lifecycle_status"] == "draft"
    assert (plan["bundle"] or {}).get("order_snapshots")  # bundle restored from server
    # replan on an unknown parent is a recognizable blocked result
    missing = asyncio.run(mt.m5_replan_schedule({
        "base_plan_version": "plan-unknown",
        "idempotency_key": "REPLAN-2",
        "event": {"event_id": "E", "sequence": 1, "event_type": "order_cancel",
                  "occurred_at": "2026-09-04T10:00:00+08:00", "reason": "x",
                  "payload": {"order_ids": []}},
    }, ctx))
    assert missing["success"] is False
    assert missing["errors"][0]["code"] == "PLAN_NOT_FOUND"
    Draft202012Validator(_schema("replan_m5_schedule")).validate(missing)


def test_replan_insert_order_applies_onto_parent_bundle(ctx):
    """insert_order must actually add the order to the restored bundle."""
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)
    base_bundle = repo.get_plan(version)["bundle"]
    before = len((base_bundle or {}).get("order_snapshots") or [])
    result = asyncio.run(mt.m5_replan_schedule({
        "base_plan_version": version,
        "idempotency_key": "REPLAN-INS-1",
        "expected_head_plan_version": version,
        "event": {
            "event_id": "EV-INS-1", "sequence": 1, "event_type": "insert_order",
            "occurred_at": "2026-09-04T10:00:00+08:00", "reason": "加单",
            "payload": {"orders": [
                {"order_id": "SO-LIFE-9", "product_id": "P1", "quantity": 1,
                 "uom": "PCS", "due_time": "2026-09-11T17:00:00+08:00"}
            ]},
        },
    }, ctx))
    assert result["success"] is True
    new_plan = repo.get_plan(result["data"]["plan_version"])
    after = len((new_plan["bundle"] or {}).get("order_snapshots") or [])
    assert after == before + 1
    ids = [o.get("order_id") for o in new_plan["bundle"]["order_snapshots"]]
    assert "SO-LIFE-9" in ids


def test_messages_pending_approval_to_outbox(ctx):
    repo = M5Repository(ctx["m5_db_path"])
    prepared = asyncio.run(mt.m5_prepare_message({
        "department": "PMC", "channel": "wecom", "message_kind": "delay_alert",
        "recipient_targets": ["leader-1"], "event_summary": "OP-20 延后",
        "required_action": "确认加班", "evidence": [{"ref": "plan:SC-LIFE"}],
        "idempotency_key": "MSG-1",
    }, ctx))
    assert prepared["data"]["draft"]["status"] == "pending_approval"
    draft_id = prepared["data"]["draft"]["draft_id"]
    Draft202012Validator(_schema("prepare_m5_department_message")).validate(prepared)
    # get message returns draft status
    fetched = asyncio.run(mt.m5_get_message({"draft_id": draft_id}, ctx))
    assert fetched["success"] is True
    assert fetched["data"]["status"] == "pending_approval"
    # 未审批时 outbox 不存在：可恢复的 OUTBOX_NOT_FOUND，而不是投递失败
    missing = asyncio.run(mt.m5_message_delivery({"outbox_id": f"OUTBOX-{draft_id}"}, ctx))
    assert missing["success"] is False
    assert missing["errors"][0]["code"] == "OUTBOX_NOT_FOUND"
    # approved -> durable outbox (actor approval outside LLM scope)
    approved = repo.approve_message(draft_id, actor="zhb")
    assert approved["outbox_id"].startswith("OUTBOX-")
    # 审批是外部动作：Agent 侧只能读回 approved 状态
    refetched = asyncio.run(mt.m5_get_message({"draft_id": draft_id}, ctx))
    assert refetched["success"] is True
    assert refetched["data"]["status"] == "approved"
    delivery = asyncio.run(mt.m5_message_delivery({"outbox_id": approved["outbox_id"]}, ctx))
    assert delivery["success"] is True
    assert delivery["data"]["status"] == "pending"


def test_department_message_approval_is_not_an_agent_tool():
    """审批只能走人工/外部系统：Agent 工具面不得出现审批工具（不变量回归）。"""
    registry = build_default_registry()
    message_tools = sorted(
        name for name, spec in registry.specs.items()
        if spec.module == "m5" and "message" in name
    )
    assert message_tools == [
        "get_m5_department_message",
        "get_m5_department_message_delivery",
        "prepare_m5_department_message",
    ]
    for name in message_tools:
        assert not any(
            token in name for token in ("approve", "reject", "send", "deliver_now")
        ), f"{name} 不应暴露审批/发送动作"


def test_shared_kernel_tools_declare_distinct_roles():
    """求解内核四兄弟必须在契约里写明定位/禁用场景，避免被误用。"""
    manifest = json.loads(Path("registry-manifests/m5.json").read_text(encoding="utf-8"))
    desc = {t["name"]: t["description"] for t in manifest["tools"]}
    assert "定位边界" in desc["solve_scheduling"]
    assert "定位：" in desc["replan_m5_schedule"]
    assert "禁用场景" in desc["replan_m5_schedule"]
    assert "禁用场景" in desc["run_m5_intelligent_schedule"]
    assert "不落库" in desc["run_m5_intelligent_schedule"]
    assert "禁用场景" in desc["generate_m5_material_procurement_plan"]


def test_intelligent_schedule_compat_fields_are_deprecated_in_contract():
    """兼容字段必须显式 deprecated + 注明替代，避免被当成生效参数。"""
    manifest = json.loads(Path("registry-manifests/m5.json").read_text(encoding="utf-8"))
    tool = next(t for t in manifest["tools"] if t["name"] == "run_m5_intelligent_schedule")
    props = tool["input_schema"]["properties"]
    for field in ("enable_knowledge_record", "auto_replan_on_validation_fail",
                  "knowledge_tags", "knowledge_note"):
        assert props[field]["deprecated"] is True, field
        assert "不生效" in props[field]["description"], field
        assert "record_m5_knowledge" in props[field]["description"] or \
            "replan_m5_schedule" in props[field]["description"], field


def test_integration_contracts_declares_static_source(ctx):
    """集成契约目录是静态声明：显式标注来源，避免被当成实时集成状态。"""
    result = asyncio.run(mt.m5_integration_contracts({}, ctx))
    assert result["success"] is True
    Draft202012Validator(_schema("get_m5_integration_contracts")).validate(result)
    assert result["data"]["source"] == "static_declaration"
    assert "不探活" in result["data"]["note"]
    assert set(result["data"]["contracts"]) == {"m1", "m2", "m3", "m4", "m6", "m7", "m8", "mes"}


def test_knowledge_record_requires_persisted_plan_and_search(ctx):
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)
    rec = asyncio.run(mt.m5_record_knowledge(
        {"plan_version": version, "tags": ["e2e"], "note": "baseline"}, ctx))
    assert rec["success"] is True
    Draft202012Validator(_schema("record_m5_knowledge")).validate(rec)
    search = asyncio.run(mt.m5_search_knowledge(
        {"features": {"product": "P1"}, "top_k": 5, "tag_filter": "e2e"}, ctx))
    assert search["success"] is True
    assert any(item["tags"] == ["e2e"] for item in search["data"]["hits"])
    # missing plan cannot be recorded
    with pytest.raises(M5RepositoryError) as exc:
        asyncio.run(mt.m5_record_knowledge({"plan_version": "missing"}, ctx))
    assert exc.value.code == "PLAN_NOT_FOUND"


def test_material_readiness_and_procurement_proposal_are_readonly(ctx):
    repo = M5Repository(ctx["m5_db_path"])
    payload = _payload()
    asyncio.run(mt.m5_ingest_snapshot(payload, ctx))
    readiness = asyncio.run(mt.m5_material_readiness({"scenario_id": "SC-LIFE"}, ctx))
    assert readiness["success"] is True
    Draft202012Validator(_schema("get_m5_material_readiness")).validate(readiness)
    assert readiness["data"]["passed"] is True
    proposal = asyncio.run(mt.m5_procurement_plan({
        **_payload(), "scenario_id": "SC-PROC", "idempotency_key": "PROC-1",
        "order_kitting": [{"material_code": "MAT-1", "required_qty": 10}],
        "material_availability": [{"material_code": "MAT-1", "available_qty": 4}],
    }, ctx))
    assert proposal["success"] is True
    assert proposal["data"]["shortage_material_count"] >= 1
    assert all(x["status"].startswith("proposal") for x in proposal["data"]["lines"])


def test_advise_and_intelligent_are_non_persistent(ctx):
    advise = asyncio.run(mt.m5_advise_schedule({
        "scenario_id": "SC-ADV", "scenario_purpose": "production",
        "plan_version": "plan-adv", "solver_status": "feasible",
        "operations": [], "metrics": {"total_tardiness_minutes": 120},
        "validation_report": {"status": "pass", "errors": []},
    }, ctx))
    assert advise["success"] is True
    Draft202012Validator(_schema("advise_m5_schedule")).validate(advise)
    assert any(item["action"] == "expedite" for item in advise["data"]["items"])
    intelligent = asyncio.run(mt.m5_intelligent_schedule({
        "schedule_request": _payload(),
        "enable_advise": True, "enable_audit": True,
    }, ctx))
    assert intelligent["success"] is True
    Draft202012Validator(_schema("run_m5_intelligent_schedule")).validate(intelligent)
    assert intelligent["data"]["auto_replanned"] is False


def test_ingest_snapshot_and_get_schedule_read_back(ctx):
    repo = M5Repository(ctx["m5_db_path"])
    payload = _payload()
    ingested = asyncio.run(mt.m5_ingest_snapshot(payload, ctx))
    assert ingested["success"] is True
    Draft202012Validator(_schema("ingest_m5_planning_snapshot")).validate(ingested)
    assert ingested["data"]["counts"]["order_snapshots"] == 1
    # get_m5_schedule of a missing version is a recognizable failure
    missing = asyncio.run(mt.m5_get_schedule({"plan_version": "missing"}, ctx))
    assert missing["success"] is False
    Draft202012Validator(_schema("get_m5_schedule")).validate(missing)
    version = _release_plan(repo, ctx)
    detail = asyncio.run(mt.m5_get_schedule({"plan_version": version}, ctx))
    assert detail["success"] is True
    assert detail["data"]["lifecycle_status"] == "released"
    listed = asyncio.run(mt.m5_list_schedules({"scenario_id": "SC-LIFE"}, ctx))
    assert listed["success"] is True
    assert any(item["plan_version"] == version for item in listed["data"]["items"])


def test_replan_preserves_event_and_freeze_meta(ctx):
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)
    result = asyncio.run(mt.m5_replan_schedule({
        "base_plan_version": version,
        "idempotency_key": "REPLAN-META-1",
        "expected_head_plan_version": version,
        "freeze_policy": {"frozen_until": "2026-09-04T12:00:00+08:00"},
        "event": {"event_id": "EV-META-1", "sequence": 1,
                  "event_type": "order_cancel", "occurred_at": "2026-09-04T10:00:00+08:00",
                  "reason": "客户取消", "payload": {"order_ids": ["SO-NOT-EXIST"]}},
    }, ctx))
    assert result["success"] is True
    new_plan = repo.get_plan(result["data"]["plan_version"])
    meta = (new_plan.get("schedule") or {}).get("replan_meta")
    assert meta is not None
    assert meta["base_plan_version"] == version
    assert meta["freeze_policy"]["frozen_until"].startswith("2026-09-04")
    assert meta["event"]["event_type"] == "order_cancel"
    # parent version preserved and the new version is an independent draft
    assert new_plan["parent_plan_version"] == version
    assert new_plan["lifecycle_status"] == "draft"
    # 契约回传 draft 语义，供编排层按「产出 draft 计划」统一开 Apply Gate
    assert result["data"]["lifecycle_status"] == "draft"
    assert result["data"]["plan_version"] == new_plan["plan_version"]
    Draft202012Validator(_schema("replan_m5_schedule")).validate(result)


def test_progress_attaches_only_persisted_execution_evidence(ctx):
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)
    repo.add_execution_event(
        event={"event_id": "EV-PROG-1", "event_type": "quantity_report",
               "order_id": "SO-LIFE-1", "operation_id": "OP-10",
               "external_ref": "ext-1", "occurred_at": "2026-09-03T10:00:00+08:00",
               "reported_quantity": 2, "scrap_quantity": 0,
               "worker_id": "worker-1", "station": "S-1", "source_kind": "accepted_non_simulation_execution_event"},
        plan_version=version, tenant_id=ctx["tenant_id"], task_id=ctx["task_id"])
    progress = asyncio.run(mt.m5_pmc_progress({"plan_version": version}, ctx))
    ops = progress["data"]["orders"][0]["operations"]
    op10 = next(op for op in ops if op["operation_id"] == "OP-10")
    assert len(op10["execution_evidence"]) == 1
    assert op10["execution_evidence"][0]["event_id"] == "EV-PROG-1"
    Draft202012Validator(_schema("get_m5_pmc_progress")).validate(progress)


# ---------------------------------------------------------------------------
# R6 语义随迁回归（rows-S6 备注/需补项）
# ---------------------------------------------------------------------------

def test_record_knowledge_accepts_declared_features_through_registry(ctx):
    """契约必须允许写 features，否则检索侧永远没有可比较的特征。"""
    registry = build_default_registry()
    version = _release_plan(M5Repository(ctx["m5_db_path"]), ctx)
    result = asyncio.run(registry.call(
        "record_m5_knowledge",
        {"plan_version": version, "tags": ["contract"], "features": {"product": "P1"}},
        ctx,
    ))
    assert result["success"] is True
    assert result["data"]["features"] == {"product": "P1"}


def test_knowledge_search_ranks_by_feature_overlap(ctx):
    """features 必填就必须真的被使用：按字段重叠度打分排序。"""
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)
    asyncio.run(mt.m5_record_knowledge({
        "plan_version": version, "tags": ["sim"], "note": "close",
        "features": {"product": "P1", "order_scale": "small"},
    }, ctx))
    asyncio.run(mt.m5_record_knowledge({
        "plan_version": version, "tags": ["sim"], "note": "far",
        "features": {"product": "P9", "order_scale": "large"},
    }, ctx))
    result = asyncio.run(mt.m5_search_knowledge({
        "features": {"product": "P1", "order_scale": "small"}, "top_k": 5, "tag_filter": "sim",
    }, ctx))
    assert result["success"] is True
    Draft202012Validator(_schema("search_m5_knowledge")).validate(result)
    assert result["data"]["similarity_basis"] == "feature_overlap"
    assert result["data"]["cases_with_features"] == 2
    hits = result["data"]["hits"]
    assert hits[0]["note"] == "close"
    assert hits[0]["match_score"] == 1.0
    assert hits[1]["match_score"] == 0.0
    # 没有可比较特征时回退为时间倒序，不假装有相似度
    fallback = asyncio.run(mt.m5_search_knowledge({"features": {}, "tag_filter": "sim"}, ctx))
    assert fallback["data"]["similarity_basis"] == "recency"
    assert all(hit["match_score"] == 0.0 for hit in fallback["data"]["hits"])


def test_procurement_proposal_fails_closed_without_authoritative_facts(ctx):
    """缺齐套/可用量事实 → fail-closed：不产占位行、不把缺失可用量当 0。"""
    no_kitting = asyncio.run(mt.m5_procurement_plan({
        **_payload(), "scenario_id": "SC-PROC-NOKIT", "idempotency_key": "PROC-2",
    }, ctx))
    assert no_kitting["success"] is False
    Draft202012Validator(_schema("generate_m5_material_procurement_plan")).validate(no_kitting)
    assert no_kitting["errors"][0]["code"] == "MISSING_KITTING_FACTS"
    assert no_kitting["data"]["missing_fields"] == ["order_kitting"]
    assert no_kitting["data"]["recovery"]
    missing_avail = asyncio.run(mt.m5_procurement_plan({
        **_payload(), "scenario_id": "SC-PROC-NOAVAIL", "idempotency_key": "PROC-3",
        "order_kitting": [{"material_code": "MAT-9", "required_qty": 5}],
        "material_availability": [],
    }, ctx))
    assert missing_avail["success"] is False
    assert missing_avail["errors"][0]["code"] == "MISSING_MATERIAL_AVAILABILITY"
    assert missing_avail["data"]["missing_fields"] == ["material_availability[MAT-9]"]


def test_procurement_plan_has_no_placeholder_branch():
    """占位行分支必须彻底移除，防止回归到「按订单行编造缺口」。"""
    source = Path("src/yunpai_orchestrator/m5_tools.py").read_text(encoding="utf-8")
    assert "proposal_placeholder" not in source


def test_intelligent_advice_reuses_the_rule_engine(ctx):
    """enable_advise 必须复用 advise_m5_schedule 的规则实现，而不是另一套固定文案。"""
    intelligent = asyncio.run(mt.m5_intelligent_schedule({
        "schedule_request": _payload(), "enable_advise": True,
    }, ctx))
    assert intelligent["success"] is True
    advice = intelligent["data"]["advice"]
    assert advice["source"] == "rule_based"
    schedule = intelligent["data"]["schedule"]
    direct = asyncio.run(mt.m5_advise_schedule({
        "scenario_id": "SC-LIFE", "scenario_purpose": "production",
        "plan_version": schedule.get("plan_version") or "plan-x",
        "solver_status": "feasible",
        "operations": schedule.get("operations") or [],
        "metrics": schedule.get("metrics") or {},
        "validation_report": {"status": "pass", "errors": []},
    }, ctx))
    assert direct["data"]["items"] == advice["items"]


def test_ingest_snapshot_rejects_incremental_merge_without_writing(ctx):
    """replace_existing=false 未实现：fail-closed，绝不静默整包覆盖。"""
    repo = M5Repository(ctx["m5_db_path"])
    result = asyncio.run(mt.m5_ingest_snapshot({**_payload(), "replace_existing": False}, ctx))
    assert result["success"] is False
    Draft202012Validator(_schema("ingest_m5_planning_snapshot")).validate(result)
    assert result["errors"][0]["code"] == "UNSUPPORTED_INCREMENTAL_MERGE"
    assert result["data"]["missing_fields"] == ["replace_existing=false（增量合并）"]
    assert result["data"]["recovery"]
    assert repo.load_bundle("SC-LIFE") is None  # 未做任何写入
    ok = asyncio.run(mt.m5_ingest_snapshot({**_payload(), "replace_existing": True}, ctx))
    assert ok["success"] is True
    assert ok["data"]["replace_existing"] is True


# ---------------------------------------------------------------------------
# 改造项②：V2 阻塞信封必须带顶层 code（worker/executor.py:83-90 / rules.py:115）
# ---------------------------------------------------------------------------

def _assert_blocked_envelope(envelope: dict, *, inner_code: str, tool: str) -> None:
    """V2 口径：顶层 code=BLOCKED_INPUT → status=blocked → data Gate 可开。"""
    assert envelope["success"] is False, tool
    assert envelope["code"] == "BLOCKED_INPUT", f"{tool} 顶层 code 缺失/错误：{envelope.get('code')}"
    assert envelope["status"] == "blocked", tool
    assert envelope["errors"][0]["code"] == inner_code, tool
    # 顶层 code 必须能被 V2 编排层读到（registry 归一化后仍在顶层）
    from yunpai_orchestrator.contracts import normalize_contract_result
    normalized = normalize_contract_result(envelope, source=f"tool:{tool}", invoked_tools=[tool])
    assert normalized["code"] == "BLOCKED_INPUT", tool


def test_blocked_envelopes_carry_top_level_code(ctx):
    """逐工具断言：阻塞路径的顶层 code 与具体原因分层（改造项②）。"""
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)

    cases = [
        ("ingest_m5_planning_snapshot", mt.m5_ingest_snapshot, {}, "MISSING_SCENARIO"),
        ("dispatch_m5_schedule", mt.m5_dispatch_schedule,
         {"plan_version": version}, "MISSING_DISPATCH_ARGS"),
        ("dispatch_m5_schedule", mt.m5_dispatch_schedule,
         {"plan_version": version, "idempotency_key": "D-X"}, "MISSING_OPERATION_KEYS"),
        ("dispatch_m5_schedule", mt.m5_dispatch_schedule,
         {"plan_version": "plan-nope", "idempotency_key": "D-Y",
          "operation_keys": ["SO-LIFE-1:OP-10"]}, "PLAN_NOT_FOUND"),
        ("get_m5_execution_summary", mt.m5_execution_summary,
         {"plan_version": "plan-nope"}, "PLAN_NOT_FOUND"),
        ("get_m5_schedule", mt.m5_get_schedule, {"plan_version": "plan-nope"}, "PLAN_NOT_FOUND"),
        ("get_m5_material_readiness", mt.m5_material_readiness,
         {"scenario_id": "SC-NO-SNAPSHOT"}, "NO_SNAPSHOT"),
        ("replan_m5_schedule", mt.m5_replan_schedule, {}, "MISSING_REPLAN_ARGS"),
        ("replan_m5_schedule", mt.m5_replan_schedule,
         {"base_plan_version": version, "idempotency_key": "R-X"}, "MISSING_EVENT"),
        ("replan_m5_schedule", mt.m5_replan_schedule,
         {"base_plan_version": "plan-nope", "idempotency_key": "R-Y",
          "event": {"event_id": "E", "sequence": 1, "event_type": "order_cancel",
                    "occurred_at": "2026-09-04T10:00:00+08:00", "reason": "x",
                    "payload": {"order_ids": []}}}, "PLAN_NOT_FOUND"),
        ("prepare_m5_department_message", mt.m5_prepare_message,
         {"department": "PMC"}, "MISSING_IDEMPOTENCY"),
        ("get_m5_department_message", mt.m5_get_message, {"draft_id": "nope"}, "MESSAGE_NOT_FOUND"),
        ("get_m5_department_message_delivery", mt.m5_message_delivery,
         {"outbox_id": "nope"}, "OUTBOX_NOT_FOUND"),
        ("run_m5_intelligent_schedule", mt.m5_intelligent_schedule, {}, "MISSING_SCHEDULE_REQUEST"),
    ]
    for tool, handler, payload, inner_code in cases:
        envelope = asyncio.run(handler(payload, ctx))
        _assert_blocked_envelope(envelope, inner_code=inner_code, tool=tool)
        Draft202012Validator(_schema(tool)).validate(envelope)


def test_blocked_envelope_opens_data_gate_through_reviewer(ctx):
    """顶层 code 的最终用途：V2 审查侧据此开 data/blocked_input Gate。"""
    from yunpai_orchestrator.reviewer import rules as review_rules
    envelope = asyncio.run(mt.m5_material_readiness({"scenario_id": "SC-NONE"}, ctx))
    findings = review_rules.evaluate("get_m5_material_readiness", envelope)
    assert findings and findings[0]["gate"] == "blocked_input"
    # 只有 errors[0].code 的旧形态会被判硬失败（回归护栏）
    legacy = {"success": False, "data": {},
              "errors": [{"code": "NO_SNAPSHOT", "message": "x", "details": []}]}
    assert review_rules.evaluate("get_m5_material_readiness", legacy) == []


def test_pmc_progress_and_record_knowledge_fail_closed_by_contract(ctx):
    """合同强制例外：这两个工具无法返回错误信封（见 m5_tools 头注）。"""
    repo = M5Repository(ctx["m5_db_path"])
    version = _release_plan(repo, ctx)
    # get_m5_pmc_progress: 顶层 additionalProperties:false + success const true + errors maxItems 0
    with pytest.raises(M5RepositoryError) as exc:
        asyncio.run(mt.m5_pmc_progress({"plan_version": "plan-nope"}, ctx))
    assert exc.value.code == "PLAN_NOT_FOUND"
    # record_m5_knowledge: data.required 为 11 个业务字段
    with pytest.raises(M5RepositoryError) as exc2:
        asyncio.run(mt.m5_record_knowledge({"plan_version": "plan-nope"}, ctx))
    assert exc2.value.code == "PLAN_NOT_FOUND"
    progress_schema = _schema("get_m5_pmc_progress")
    assert progress_schema.get("additionalProperties") is False
    assert progress_schema["properties"]["errors"].get("maxItems") == 0
    assert _schema("record_m5_knowledge")["properties"]["data"]["required"]
    assert version  # 已发布计划可用，异常仅来自「查不到/未发布」路径
