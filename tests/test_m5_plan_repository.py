"""M5 plan repository / idempotency / CAS / lifecycle tests (Task 2/3)."""
import pytest

from yunpai_orchestrator.m5_repository import (
    M5Repository, M5RepositoryError, LIFECYCLE_ORDER, PROTECTED_STATUSES,
)
from yunpai_orchestrator.pmc_v2_adapter import run_pmc_v2


def _strict_payload():
    return {
        "idempotency_key": "REPO-001",
        "scenario_id": "SC-REPO",
        "scenario_purpose": "production",
        "planning_start": "2026-09-03T08:00:00+08:00",
        "calendar_windows": [
            {"calendar_ref": "CAL-A", "shift_code": "DAY",
             "start_at": "2026-09-03T08:00:00+08:00", "end_at": "2026-09-03T17:00:00+08:00"},
        ],
        "route_code": "ROUTE-P1",
        "route_version": "approved-v1",
        "route_approval_ref": "APPROVED-REPO-001",
        "orders": [{"order_id": "SO-REPO-1", "product_id": "P1", "quantity": 2, "uom": "PCS",
                    "due_time": "2026-09-10T17:00:00+08:00"}],
        "resources": [
            {"resource_id": "EQ-A", "resource_type": "EQUIPMENT", "name": "设备A",
             "equipment_type": "press", "capacity_per_hour": 60, "efficiency_factor": 1,
             "calendar_ref": "CAL-A", "status": "available", "capability_codes": ["P"]},
        ],
        "routing_steps": [
            {"product_id": "P1", "operation_id": "OP-10", "sequence": 10,
             "operation_name": "工序10", "standard_minutes": 5, "setup_minutes": 0,
             "required_equipment_codes": ["EQ-A"], "predecessors": [],
             "approval_ref": "APPROVED-REPO-001"},
        ],
        "supply_entries": [
            {"order_line_id": "SO-REPO-1::L1", "op_code": "OP-10", "readiness": "READY",
             "requirement_ref": "MAT-1", "inventory_snapshot_ref": "INV-1"},
        ],
    }


@pytest.fixture
def repo(tmp_path):
    return M5Repository(tmp_path / "m5.sqlite")


def _solved(repo, payload):
    result = run_pmc_v2(payload)
    bundle = result["data"]["input_package"]
    records = repo.store_snapshots(payload["scenario_id"], bundle,
                                   tenant_id="tenant-a", task_id="task-a")
    assert len(records) == 6
    return result, bundle, records


def test_six_snapshot_kinds_persist_and_read_back(repo):
    payload = _strict_payload()
    result, _, _ = _solved(repo, payload)
    bundle = repo.load_bundle(payload["scenario_id"])
    assert set(bundle) == {"order_snapshots", "routes", "resource_snapshot",
                           "calendar_snapshot", "supply_snapshot", "constraint_snapshot"}
    assert bundle["routes"]["P1"]["operations"][0]["op_code"] == "OP-10"
    assert bundle["calendar_snapshot"]["working_intervals"][0]["calendar_ref"] == "CAL-A"
    assert repo.list_snapshots(payload["scenario_id"])[0]["tenant_id"] == "tenant-a"


def test_multi_line_order_snapshots_get_distinct_ids(repo):
    """多行订单（bridge 按行展开、同 order_id）不得生成重复 SNAP-ORD-{order_id}
    导致 store 批内 UNIQUE 冲突（E2E 实测 IntegrityError）。"""
    payload = _strict_payload()
    payload["scenario_id"] = "SC-MULTILINE"
    payload["orders"] = [
        {"order_id": "SO-MULTI", "order_line_id": "SO-MULTI::L1", "product_id": "P1",
         "quantity": 2, "uom": "PCS", "due_time": "2026-09-10T17:00:00+08:00"},
        {"order_id": "SO-MULTI", "order_line_id": "SO-MULTI::L2", "product_id": "P1",
         "quantity": 3, "uom": "PCS", "due_time": "2026-09-10T17:00:00+08:00"},
    ]
    payload["supply_entries"] = [
        {"order_line_id": "SO-MULTI::L1", "op_code": "OP-10", "readiness": "READY",
         "requirement_ref": "MAT-1", "inventory_snapshot_ref": "INV-1"},
        {"order_line_id": "SO-MULTI::L2", "op_code": "OP-10", "readiness": "READY",
         "requirement_ref": "MAT-1", "inventory_snapshot_ref": "INV-1"},
    ]
    result = run_pmc_v2(payload)
    assert result["success"] is True, result.get("errors")
    bundle = result["data"]["input_package"]
    ids = [o["snapshot_id"] for o in bundle["order_snapshots"]]
    assert len(ids) == 2 and len(set(ids)) == 2, f"同行订单快照 id 必须唯一: {ids}"
    records = repo.store_snapshots(payload["scenario_id"], bundle,
                                   tenant_id="tenant-a", task_id="task-multi")
    assert len(records) == 7  # 2 个 order 快照（按行）+ 其余五类各 1


def test_plan_save_read_with_hashes(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    input_hash = result["data"]["input_hash"]
    version = f"plan-{payload['scenario_id']}-{input_hash[:10]}"
    saved = repo.save_plan(
        plan_version=version, scenario_id=payload["scenario_id"], tenant_id="tenant-a",
        task_id="task-a", lifecycle_status="draft", parent_plan_version=None,
        input_hash=input_hash, solver_hash="solver-v2-001",
        algorithm_version="pmc-v2-frozen-20260902",
        scenario_purpose="production",
        validation_report=result["data"]["validator"],
        bundle=bundle, schedule=result["data"]["schedule"],
        idempotency_key=payload["idempotency_key"],
    )
    assert saved["lifecycle_status"] == "draft"
    plan = repo.get_plan(version)
    assert plan["scenario_id"] == "SC-REPO"
    assert plan["schedule"]["metrics"]["operation_count"] == 1
    hit = repo.find_by_idempotency("SC-REPO", "REPO-001")
    assert hit["plan_version"] == version


def test_same_idempotency_same_input_replays_same_plan(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    v1 = f"plan-{payload['scenario_id']}-{result['data']['input_hash'][:10]}"
    repo.save_plan(plan_version=v1, scenario_id=payload["scenario_id"], tenant_id="t",
                   task_id="k", lifecycle_status="draft", parent_plan_version=None,
                   input_hash=result["data"]["input_hash"], solver_hash="s1",
                   algorithm_version="a", scenario_purpose="production",
                   validation_report={"status":"pass","errors":[]}, bundle=bundle, schedule={},
                   idempotency_key="KEY-REPLAY")
    result2 = run_pmc_v2(payload)  # identical canonical input
    v2 = f"plan-{payload['scenario_id']}-{result2['data']['input_hash'][:10]}"
    assert v2 == v1
    repo.save_plan(plan_version=v2, scenario_id=payload["scenario_id"], tenant_id="t",
                   task_id="k", lifecycle_status="draft", parent_plan_version=None,
                   input_hash=result2["data"]["input_hash"], solver_hash="s1",
                   algorithm_version="a", scenario_purpose="production",
                   validation_report={"status":"pass","errors":[]}, bundle=bundle, schedule={},
                   idempotency_key="KEY-REPLAY")
    hit = repo.find_by_idempotency("SC-REPO", "KEY-REPLAY")
    assert hit["plan_version"] == v1


def test_same_idempotency_different_input_conflicts(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    repo.save_plan(plan_version=f"plan-{result['data']['input_hash'][:10]}",
                   scenario_id="SC-REPO", tenant_id="t", task_id="k",
                   lifecycle_status="draft", parent_plan_version=None,
                   input_hash=result["data"]["input_hash"], solver_hash="s1",
                   algorithm_version="a", scenario_purpose="production",
                   validation_report={"status":"pass","errors":[]}, bundle=bundle, schedule={},
                   idempotency_key="KEY-CONFLICT")
    changed = _strict_payload()
    changed["orders"][0]["quantity"] = 5
    changed_result = run_pmc_v2(changed)
    hit = repo.find_by_idempotency("SC-REPO", "KEY-CONFLICT")
    assert hit["input_hash"] != changed_result["data"]["input_hash"]


def test_head_cas_rejects_stale_revision(repo):
    repo.set_head("SC-1", "plan-v1", expected_revision=None)
    with pytest.raises(M5RepositoryError) as exc:
        repo.set_head("SC-1", "plan-v2", expected_revision=0)
    assert exc.value.code == "HEAD_CONFLICT"
    rev = repo.set_head("SC-1", "plan-v2", expected_revision=1)
    assert repo.get_head("SC-1")["head_plan_version"] == "plan-v2"
    assert repo.get_head("SC-1")["revision"] == 2


def test_released_plan_cannot_be_overwritten(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    version = f"plan-{result['data']['input_hash'][:10]}"
    repo.save_plan(plan_version=version, scenario_id="SC-REPO", tenant_id="t",
                   task_id="k", lifecycle_status="draft", parent_plan_version=None,
                   input_hash=result["data"]["input_hash"], solver_hash="s1",
                   algorithm_version="a", scenario_purpose="production",
                   validation_report={"status":"pass","errors":[]}, bundle=bundle, schedule={})
    repo.transition(version, "approved", gate="review", actor="zhb")
    repo.transition(version, "released", gate="release", actor="zhb")
    with pytest.raises(M5RepositoryError) as exc:
        repo.save_plan(plan_version=version, scenario_id="SC-REPO", tenant_id="t",
                       task_id="k", lifecycle_status="draft", parent_plan_version=None,
                       input_hash="different", solver_hash="s2", algorithm_version="a",
                       scenario_purpose="production", validation_report={"status":"pass","errors":[]},
                       bundle=bundle, schedule={})
    assert exc.value.code == "PLAN_PROTECTED"


def test_lifecycle_requires_adjacent_transition(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    version = f"plan-{result['data']['input_hash'][:10]}"
    repo.save_plan(plan_version=version, scenario_id="SC-REPO", tenant_id="t",
                   task_id="k", lifecycle_status="draft", parent_plan_version=None,
                   input_hash=result["data"]["input_hash"], solver_hash="s1",
                   algorithm_version="a", scenario_purpose="production",
                   validation_report={"status":"pass","errors":[]}, bundle=bundle, schedule={})
    repo.transition(version, "approved", gate="review", actor="zhb")
    with pytest.raises(M5RepositoryError) as exc:
        repo.transition(version, "execution", gate="mes", actor="zhb")
    assert exc.value.code == "ILLEGAL_TRANSITION"
    repo.transition(version, "released", gate="release", actor="zhb")
    repo.transition(version, "dispatched", gate="dispatch", actor="zhb")
    repo.transition(version, "execution", gate="mes", actor="zhb")
    assert repo.get_plan(version)["lifecycle_status"] == "execution"
    events = repo.lifecycle_events(version)
    assert [e["to_status"] for e in events] == ["approved", "released", "dispatched", "execution"]
    assert all(e["actor"] == "zhb" for e in events)


def test_pressure_only_plan_cannot_release_or_dispatch(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    version = f"plan-pressure-{result['data']['input_hash'][:10]}"
    repo.save_plan(plan_version=version, scenario_id="SC-REPO", tenant_id="t",
                   task_id="k", lifecycle_status="draft", parent_plan_version=None,
                   input_hash=result["data"]["input_hash"], solver_hash="s1",
                   algorithm_version="a", scenario_purpose="pressure_only",
                   validation_report={"status": "pass", "errors": []},
                   bundle=bundle, schedule={})
    repo.transition(version, "approved", gate="review", actor="zhb")
    with pytest.raises(M5RepositoryError) as exc:
        repo.transition(version, "released", gate="release", actor="zhb")
    assert exc.value.code == "PURPOSE_NOT_RELEASABLE"


def test_validation_failed_plan_cannot_release(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    version = f"plan-fail-{result['data']['input_hash'][:10]}"
    repo.save_plan(plan_version=version, scenario_id="SC-REPO", tenant_id="t",
                   task_id="k", lifecycle_status="draft", parent_plan_version=None,
                   input_hash=result["data"]["input_hash"], solver_hash="s1",
                   algorithm_version="a", scenario_purpose="production",
                   validation_report={"status": "fail", "errors": [{"reason_code": "SUPPLY_NOT_READY"}]},
                   bundle=bundle, schedule={})
    repo.transition(version, "approved", gate="review", actor="zhb")
    with pytest.raises(M5RepositoryError) as exc:
        repo.transition(version, "released", gate="release", actor="zhb")
    assert exc.value.code == "VALIDATION_FAILED"


def test_execution_event_add_is_idempotent(repo):
    repo.add_execution_event(
        event={"event_id": "EV-1", "event_type": "actual_start",
               "order_id": "SO-1", "operation_id": "OP-10", "external_ref": "e-1",
               "occurred_at": "2026-09-03T08:00:00+08:00"},
        plan_version="plan-x", task_id="t")
    again = repo.add_execution_event(
        event={"event_id": "EV-1", "event_type": "actual_start",
               "order_id": "SO-1", "operation_id": "OP-10", "external_ref": "e-1",
               "occurred_at": "2026-09-03T08:00:00+08:00"},
        plan_version="plan-x", task_id="t")
    assert again["replayed"] is True
    assert len(repo.list_execution_events("plan-x")) == 1


def test_message_double_approve_rejected(repo):
    repo.create_message(draft_id="D-1", tenant_id="t", task_id="k",
                        idempotency_key="M-1", department="PMC", channel="wecom",
                        recipient_targets=["x"], message_kind="alert",
                        subject="", body="b", event_summary="e",
                        required_action="a", evidence=[])
    repo.approve_message("D-1", actor="zhb")
    with pytest.raises(M5RepositoryError) as exc:
        repo.approve_message("D-1", actor="zhb")
    assert exc.value.code == "ILLEGAL_MESSAGE_STATE"


# ---------------------------------------------------------------------------
# T4: Apply Gate 原子发布（draft->approved->released + set_head 单事务 + readback）
# ---------------------------------------------------------------------------

def _save_draft(repo, result, bundle, *, version_prefix="plan", scenario_id="SC-REPO",
                report_status=None, purpose="production"):
    version = f"{version_prefix}-{result['data']['input_hash'][:10]}"
    report = {"status": report_status or result["data"]["validator"]["status"], "errors": []} if report_status else result["data"]["validator"]
    repo.save_plan(plan_version=version, scenario_id=scenario_id, tenant_id="t",
                   task_id="k", lifecycle_status="draft", parent_plan_version=None,
                   input_hash=result["data"]["input_hash"], solver_hash="s1",
                   algorithm_version="a", scenario_purpose=purpose,
                   validation_report=report, bundle=bundle,
                   schedule=result["data"]["schedule"])
    return version


def test_apply_release_draft_to_released_sets_head_atomically(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    version = _save_draft(repo, result, bundle)
    # draft 计划直接走人工 Apply Gate：一次事务完成 approved -> released + head CAS
    applied = repo.apply_release(version, "SC-REPO", tenant_id="t", gate="apply",
                                 actor="zhb", task_id="k", trace_id="k:apply",
                                 expected_head_revision=0)
    assert applied["to_status"] == "released"
    assert applied["head_revision"] == 1
    assert applied["transitioned_steps"] == ("draft", "approved", "released")
    readback = repo.readback_after_release(version, "SC-REPO")
    assert readback["lifecycle_status"] == "released"
    assert readback["head_plan_version"] == version
    assert readback["head_revision"] == 1
    statuses = [e["to_status"] for e in readback["lifecycle_events"]]
    assert statuses == ["approved", "released"]
    last = readback["lifecycle_events"][-1]
    assert last["gate"] == "apply"
    assert last["actor"] == "zhb"
    assert last["task_id"] == "k"


def test_apply_release_rejects_stale_head_and_does_not_fake_release(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    version = _save_draft(repo, result, bundle)
    # head 已被其他计划占用：CAS 冲突 -> 整笔回滚，无假 released、head 不变
    repo.set_head("SC-REPO", "other-plan")  # revision 1
    with pytest.raises(M5RepositoryError) as exc:
        repo.apply_release(version, "SC-REPO", gate="apply", actor="zhb",
                           expected_head_revision=0)
    assert exc.value.code == "HEAD_CONFLICT"
    plan = repo.get_plan(version)
    assert plan["lifecycle_status"] == "draft"  # 事务回滚，未假 released
    head = repo.get_head("SC-REPO")
    assert head["head_plan_version"] == "other-plan"
    assert len(repo.lifecycle_events(version)) == 0


def test_apply_release_rejects_non_production_and_validation_failed(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    pressure_version = _save_draft(repo, result, bundle, version_prefix="pressure-plan", purpose="pressure_only")
    with pytest.raises(M5RepositoryError) as exc:
        repo.apply_release(pressure_version, "SC-REPO", gate="apply", actor="zhb")
    assert exc.value.code == "PURPOSE_NOT_RELEASABLE"
    failed_version = _save_draft(repo, result, bundle, version_prefix="fail-plan", report_status="fail")
    with pytest.raises(M5RepositoryError) as exc:
        repo.apply_release(failed_version, "SC-REPO", gate="apply", actor="zhb")
    assert exc.value.code == "VALIDATION_FAILED"


def test_apply_release_protects_released_plan(repo):
    payload = _strict_payload()
    result, bundle, _ = _solved(repo, payload)
    version = _save_draft(repo, result, bundle)
    repo.apply_release(version, "SC-REPO", gate="apply", actor="zhb",
                       expected_head_revision=0)
    with pytest.raises(M5RepositoryError) as exc:
        repo.apply_release(version, "SC-REPO", gate="apply", actor="zhb",
                           expected_head_revision=1)
    assert exc.value.code == "PLAN_PROTECTED"
