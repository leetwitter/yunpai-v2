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
    # approved -> durable outbox (actor approval outside LLM scope)
    approved = repo.approve_message(draft_id, actor="zhb")
    assert approved["outbox_id"].startswith("OUTBOX-")
    delivery = asyncio.run(mt.m5_message_delivery({"outbox_id": approved["outbox_id"]}, ctx))
    assert delivery["success"] is True
    assert delivery["data"]["status"] == "pending"


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
