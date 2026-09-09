"""Local M5 PMC tool handlers (Taskbook Tasks 3-5).

These are the local "real handler" implementations for the 17 in-scope M5
tools, backed by :class:`M5Repository`.  Every handler follows the m5.json
manifest contract: ``{success, data, errors, trace_id}`` plus optional
``evidence``.  Read-only evidence tools never fabricate plan facts.

V2 信封口径（迁移改造项 ②）：阻塞结果顶层必须带 ``code``。V2 的
``worker/executor.py:83-90`` 只认顶层 ``code`` 判 ``status="blocked"``，
``reviewer/rules.py:115`` 只认顶层 ``code == "BLOCKED_INPUT"`` 开
data/blocked_input Gate；只有 ``errors[0].code`` 会被判成硬失败（重试耗尽即失败）。
因此本模块统一用 :func:`_blocked_result`：顶层 ``code="BLOCKED_INPUT"``，
具体原因保留在 ``errors[0].code``（与 ``workers.py:428/499/529/606`` 同形）。
两个例外受合同强制：``get_m5_pmc_progress``（output schema 顶层
``additionalProperties:false`` + ``success const true`` + ``errors maxItems 0``）与
``record_m5_knowledge``（``data.required`` 为 11 个业务字段）无法返回错误信封，
保持 INT 的 fail-closed 异常语义。

The repository path is resolved from the context ``m5_db_path`` (tests /
embedding) or the ``YUNPAI_M5_DB`` env var, defaulting to
``runtime/yunpai-m5.sqlite``。V2 的 ``tool_context()`` 不提供 ``m5_db_path``
（INFRA-DECISIONS §1.2 裁定不补），部署与测试统一走 ``YUNPAI_M5_DB``。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .m5_repository import M5Repository, M5RepositoryError
from .pmc_v2_adapter import run_pmc_v2
from .pmc_v2_snapshots import PmcError
from .pmc_wip_projection import project_wip_pmc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo(ctx: dict[str, Any] | None) -> M5Repository:
    path = (ctx or {}).get("m5_db_path") or os.getenv("YUNPAI_M5_DB") or "runtime/yunpai-m5.sqlite"
    return M5Repository(path)


def _trace(ctx: dict[str, Any] | None, suffix: str) -> str:
    task = (ctx or {}).get("task_id") or "task"
    return f"{task}:{suffix}"


def _evidence(module: str, source_ref: str, detail: str) -> dict[str, Any]:
    return {"module": module, "source_ref": source_ref,
            "evidence_ref": f"{module}:{source_ref}", "detail": detail}


def _err(code: str, message: str, details: list[Any] | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details or []}


def _tenant(ctx: dict[str, Any] | None) -> str:
    return str((ctx or {}).get("tenant_id") or "default")


def _task(ctx: dict[str, Any] | None) -> str:
    return str((ctx or {}).get("task_id") or "")


def _blocked_result(message: str, trace_id: str, *, code: str = "BLOCKED_INPUT",
                    details: list[Any] | None = None,
                    data: dict[str, Any] | None = None) -> dict[str, Any]:
    """可恢复阻塞信封：顶层 ``code="BLOCKED_INPUT"`` + ``errors[0].code`` 具体原因。

    见模块头注（改造项 ②）。``data`` 用于满足个别合同的 ``data.required``
    （如 ``get_m5_schedule`` 要求 ``scenario_purpose``/``next_event_sequence``）。
    """
    return {"success": False, "status": "blocked", "code": "BLOCKED_INPUT",
            "data": dict(data or {}),
            "errors": [_err(code, message, details)],
            "trace_id": trace_id, "evidence": [_evidence("m5", code, message)]}


def _block_code(blocks: list[Any] | None) -> str | None:
    """求解内核产出 blocks 时，顶层补 ``BLOCKED_INPUT``（否则会被判硬失败）。"""
    return "BLOCKED_INPUT" if blocks else None


def _purpose_of(payload: dict[str, Any]) -> str:
    purpose = str(payload.get("scenario_purpose") or "production").lower()
    return purpose if purpose in {"production", "pressure_only"} else "production"


# ---------------------------------------------------------------------------
# ingest_m5_planning_snapshot
# ---------------------------------------------------------------------------

async def m5_ingest_snapshot(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    scenario_id = str(payload.get("scenario_id") or "")
    if not scenario_id:
        return _blocked_result("缺少 scenario_id", _trace(ctx, "m5-ingest"),
                               code="MISSING_SCENARIO")
    if payload.get("replace_existing") is False:
        # 本地实现是整包替换（store_snapshots 先删后写）。增量合并尚未实现，
        # 因此 fail-closed：绝不把 false 静默当成整包覆盖（会删掉未提供的实体）。
        return {
            "success": False,
            "status": "blocked",
            "code": "BLOCKED_INPUT",
            "data": {
                "scenario_id": scenario_id,
                "replace_existing": False,
                "missing_fields": ["replace_existing=false（增量合并）"],
                "recovery": "本地实现只支持整包替换：改传 replace_existing=true 重试，"
                            "或先读出当前六类快照、在调用方按业务稳定键合并后整包提交",
            },
            "errors": [{"code": "UNSUPPORTED_INCREMENTAL_MERGE",
                        "message": "本实现不支持 replace_existing=false 的增量合并；"
                                   "已拒绝，未做任何写入",
                        "details": [{"supported": [True]}]}],
            "trace_id": _trace(ctx, "m5-ingest"),
            "evidence": [_evidence("m5", "planning_snapshot",
                                   "incremental merge rejected (fail-closed, no write)")],
        }
    repo = _repo(ctx)
    # production/pressure_only ingestion: build and validate the six-kind
    # bundle (facts only), then persist.  Full solving is NOT run here: a
    # feasible window is a solve-time concern, while snapshot fact integrity
    # is the ingest-time gate.
    from .pmc_v2_adapter import build_bundle
    from .pmc_v2_snapshots import validate_bundle
    try:
        request = dict(payload)
        request["scenario_purpose"] = _purpose_of(payload)
        bundle = build_bundle(request)
        validate_bundle(bundle)
    except (PmcError, M5RepositoryError) as exc:
        return _blocked_result(str(getattr(exc, "message", exc)), _trace(ctx, "m5-ingest"),
                               details=[{"snapshot_kind": "six-class-bundle"}])
    records = repo.store_snapshots(
        scenario_id, bundle,
        tenant_id=_tenant(ctx), task_id=_task(ctx),
    )
    counts: dict[str, int] = {}
    for record in records:
        counts.setdefault(record["kind"], 0)
        counts[record["kind"]] += 1
    data = {
        "scenario_id": scenario_id,
        "source_systems": payload.get("source_systems") or ["manual"],
        "observed_at": payload.get("observed_at"),
        "source_observed_at": payload.get("source_observed_at") or {},
        "replace_existing": bool(payload.get("replace_existing", True)),
        "counts": counts,
        "readiness": {"status": "snapshots_stored",
                      "snapshot_kinds": sorted(counts),
                      "checksummed": True},
    }
    return {"success": True, "data": data, "errors": [],
            "trace_id": _trace(ctx, "m5-ingest"),
            "evidence": [_evidence("m5", "planning_snapshot",
                                   f"six-class snapshot persisted for {scenario_id}")]}


# ---------------------------------------------------------------------------
# get_m5_schedule / list_m5_schedules
# ---------------------------------------------------------------------------

async def m5_get_schedule(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    plan_version = str(payload.get("plan_version") or "")
    # 预览/本地模式：桥接已从 RunState 直接提供 schedule，不读 M5 repository
    # （preview 不落库），返回等价读回结构，不冒充已发布 canonical。
    if payload.get("preview"):
        return {
            "success": True,
            "data": {
                "scenario_purpose": "production",
                "next_event_sequence": 1,
                "plan_version": plan_version,
                "scenario_id": payload.get("scenario_id") or "",
                "lifecycle_status": payload.get("lifecycle_status") or "released",
                "schedule": payload.get("schedule") or {},
                "is_current_head": True,
                "head_revision": 0,
                "lifecycle_events": [],
                "validation_report": {"status": "preview"},
                "preview": True,
            },
            "errors": [],
            "trace_id": _trace(ctx, "m5-get-schedule"),
            "evidence": [_evidence("m5", "plan", f"preview readback for {plan_version}")],
        }
    repo = _repo(ctx)
    plan = repo.get_plan(plan_version) if plan_version else None
    if plan is None:
        return _blocked_result(f"计划 {plan_version} 不存在", _trace(ctx, "m5-get-schedule"),
                               code="PLAN_NOT_FOUND",
                               data={"scenario_purpose": "production", "next_event_sequence": 1})
    schedule = plan.get("schedule") or {}
    head = repo.get_head(plan.get("scenario_id") or "")
    next_seq = len(repo.list_execution_events(plan_version)) + len(repo.lifecycle_events(plan_version)) + 1
    return {
        "success": True,
        "data": {
            "scenario_purpose": plan.get("scenario_purpose", "production"),
            "next_event_sequence": max(1, next_seq),
            "plan_version": plan_version,
            "scenario_id": plan.get("scenario_id"),
            "lifecycle_status": plan.get("lifecycle_status"),
            "parent_plan_version": plan.get("parent_plan_version"),
            "input_hash": plan.get("input_hash"),
            "solver_hash": plan.get("solver_hash"),
            "algorithm_version": plan.get("algorithm_version"),
            "validation_report": plan.get("validation_report"),
            "is_current_head": bool(head and head["head_plan_version"] == plan_version),
            "head_revision": (head or {}).get("revision"),
            "schedule": schedule,
            "lifecycle_events": repo.lifecycle_events(plan_version),
        },
        "errors": [],
        "trace_id": _trace(ctx, "m5-get-schedule"),
        "evidence": [_evidence("m5", "plan", f"read plan {plan_version}")],
    }


async def m5_list_schedules(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    scenario_id = payload.get("scenario_id")
    if scenario_id is not None:
        scenario_id = str(scenario_id)
    limit = int(payload.get("limit") or 50)
    repo = _repo(ctx)
    items = []
    for row in repo.list_plans(scenario_id, limit=limit):
        items.append({
            "plan_version": row["plan_version"],
            "scenario_id": row["scenario_id"],
            "scenario_purpose": row.get("scenario_purpose", "production"),
            "lifecycle_status": row["lifecycle_status"],
            "parent_plan_version": row.get("parent_plan_version"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        })
    return {"success": True, "data": {"items": items, "total": len(items)},
            "errors": [], "trace_id": _trace(ctx, "m5-list-schedules"),
            "evidence": [_evidence("m5", "plans", f"listed {len(items)} plan versions")]}


# ---------------------------------------------------------------------------
# dispatch_m5_schedule
# ---------------------------------------------------------------------------

async def m5_dispatch_schedule(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    plan_version = str(payload.get("plan_version") or "")
    idem = str(payload.get("idempotency_key") or "")
    operation_keys = payload.get("operation_keys")
    if not plan_version or not idem:
        return _blocked_result("dispatch 需要 plan_version 与 idempotency_key",
                               _trace(ctx, "m5-dispatch"), code="MISSING_DISPATCH_ARGS")
    if not isinstance(operation_keys, list) or not operation_keys:
        return _blocked_result("dispatch 需要显式 operation_keys",
                               _trace(ctx, "m5-dispatch"), code="MISSING_OPERATION_KEYS")
    repo = _repo(ctx)
    plan = repo.get_plan(plan_version)
    if plan is None:
        return _blocked_result(f"计划 {plan_version} 不存在", _trace(ctx, "m5-dispatch"),
                               code="PLAN_NOT_FOUND")
    # only released plans may be dispatched; preview/pressure_only must not
    # reach dispatch (P0 lifecycle boundary).
    if plan.get("lifecycle_status") != "released":
        return _blocked_result(f"计划 {plan_version} 未发布（{plan.get('lifecycle_status')}），不能派工",
                               _trace(ctx, "m5-dispatch"), code="NOT_RELEASED")
    try:
        created = repo.create_dispatch(
            dispatch_id=str(uuid4()), plan_version=plan_version,
            tenant_id=_tenant(ctx), task_id=_task(ctx), idempotency_key=idem,
            operation_keys=operation_keys,
            target_system=str(payload.get("target_system") or "mes"),
            payload=payload.get("payload"), requested_by=str(payload.get("requested_by") or ""),
        )
    except M5RepositoryError as exc:
        return _blocked_result(exc.message, _trace(ctx, "m5-dispatch"), code=exc.code)
    return {
        "success": True,
        "data": {
            "id": created["dispatch_id"], "plan_version": plan_version,
            "target_system": created.get("target_system", "mes"),
            "status": created["status"], "requested_by": created.get("requested_by", ""),
            "total_count": len(operation_keys), "acknowledged_count": 0,
            "failed_count": 0, "items": [{"operation_key": k} for k in operation_keys],
            "replayed": created.get("replayed", False),
        },
        "errors": [],
        "trace_id": _trace(ctx, "m5-dispatch"),
        "evidence": [_evidence("m5", "dispatch", f"durable pending dispatch for {plan_version}")],
    }


# ---------------------------------------------------------------------------
# get_m5_execution_summary
# ---------------------------------------------------------------------------

async def m5_execution_summary(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    plan_version = str(payload.get("plan_version") or "")
    repo = _repo(ctx)
    plan = repo.get_plan(plan_version)
    if plan is None:
        return _blocked_result(f"计划 {plan_version} 不存在", _trace(ctx, "m5-exec-summary"),
                               code="PLAN_NOT_FOUND")
    events = repo.list_execution_events(plan_version)
    event_count = len(events)
    latest = None
    deviations: list[int] = []
    late_ops: set[str] = set()
    exceptions = 0
    scrap = 0.0
    planned = {str(op.get("op_code")): op for op in (plan.get("schedule") or {}).get("operations", [])}
    for event in events:
        occurred = event.get("occurred_at") or ""
        if occurred and (latest is None or occurred > latest):
            latest = occurred
        if event.get("event_type") == "exception":
            exceptions += 1
        try:
            scrap += float(event.get("scrap_quantity") or 0)
        except (TypeError, ValueError):
            pass
        op = planned.get(str(event.get("operation_id") or ""))
        if op and event.get("event_type") in {"actual_finish", "quantity_report"}:
            try:
                plan_end = datetime.fromisoformat(op["plan_end"].replace("Z", "+00:00"))
                actual = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
                deviations.append(int((actual - plan_end).total_seconds() // 60))
                if actual > plan_end:
                    late_ops.add(str(event.get("operation_id")))
            except (ValueError, KeyError, TypeError):
                continue
    avg_start = avg_end = None
    max_dev = None
    if deviations:
        avg_end = round(sum(deviations) / len(deviations), 2)
        max_dev = max(abs(d) for d in deviations)
    data = {
        "plan_version": plan_version,
        "event_count": event_count,
        "latest_event_at": latest,
        "average_start_deviation_minutes": avg_start,
        "average_end_deviation_minutes": avg_end,
        "max_abs_end_deviation_minutes": max_dev,
        "late_operation_count": len(late_ops),
        "exception_count": exceptions,
        "scrap_quantity": scrap,
    }
    return {"success": True, "data": data, "errors": [],
            "trace_id": _trace(ctx, "m5-exec-summary"),
            "evidence": [_evidence("m5", "execution_events",
                                   f"summarised {event_count} persisted events")]}


# ---------------------------------------------------------------------------
# get_m5_material_readiness
# ---------------------------------------------------------------------------

async def m5_material_readiness(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    scenario_id = str(payload.get("scenario_id") or "")
    repo = _repo(ctx)
    bundle = repo.load_bundle(scenario_id) if scenario_id else None
    if bundle is None:
        return _blocked_result(f"scenario {scenario_id} 无已持久化快照",
                               _trace(ctx, "m5-readiness"), code="NO_SNAPSHOT")
    supply = bundle.get("supply_snapshot") or {}
    entries = supply.get("entries") or []
    material_evidence = bool(entries) or bool(bundle.get("order_kitting"))
    items = [
        {"check": "order", "status": "pass" if bundle.get("order_snapshots") else "fail",
         "source": "order_snapshots"},
        {"check": "route", "status": "pass" if bundle.get("routes") else "fail",
         "source": "routes"},
        {"check": "resource", "status": "pass" if bundle.get("resource_snapshot") else "fail",
         "source": "resource_snapshot"},
        {"check": "calendar", "status": "pass" if bundle.get("calendar_snapshot") else "fail",
         "source": "calendar_snapshot"},
        {"check": "material-or-kitting", "status": "pass" if material_evidence else "fail",
         "source": "supply_snapshot/order_kitting",
         "note": "只判断是否提供物料可用量或齐套证据；逐料缺口用 generate_m5_material_procurement_plan"},
    ]
    passed = all(item["status"] == "pass" for item in items)
    return {"success": True,
            "data": {"stage": "material_readiness", "passed": passed, "items": items},
            "errors": [],
            "trace_id": _trace(ctx, "m5-readiness"),
            "evidence": [_evidence("m5", "material_readiness",
                                   f"{scenario_id} readiness={passed}")]}


# ---------------------------------------------------------------------------
# get_m5_integration_contracts (read-only interface contract)
# ---------------------------------------------------------------------------

async def m5_integration_contracts(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """M5 侧静态声明的集成契约目录。

    目录内容是本模块写死的声明，不探活对端、不读实时集成状态、也不校验对端是否
    已实现 schema；``source`` 字段显式标注这一点，避免调用方把声明值当成实时状态。
    """
    data = {
        "source": "static_declaration",
        "contracts": {
            "m1": {"direction": "outbound", "readiness": "available",
                   "deliverables": ["order_route_facts", "wip_snapshot"]},
            "m2": {"direction": "outbound", "readiness": "available",
                   "deliverables": ["route_snapshot_v2"]},
            "m3": {"direction": "inbound", "readiness": "available",
                   "deliverables": ["material_demand", "supply_snapshot"]},
            "m4": {"direction": "inbound", "readiness": "available",
                   "deliverables": ["procurement_eta", "purchase_orders"]},
            "m6": {"direction": "outbound", "readiness": "current_none",
                   "deliverables": []},
            "m7": {"direction": "outbound", "readiness": "current_none",
                   "deliverables": []},
            "m8": {"direction": "outbound", "readiness": "current_none",
                   "deliverables": []},
            "mes": {"direction": "outbound", "readiness": "proposed",
                    "deliverables": ["dispatch"], "note": "当前无 MES sender，只保留 pending"},
        },
        "note": "静态声明目录：readiness 为 M5 侧声明值，不探活、不代表对端接口当前可用；"
                "schema 仅为 M5 侧载荷结构，不代表对端接口已可用",
    }
    return {"success": True, "data": data, "errors": [],
            "trace_id": _trace(ctx, "m5-contracts"),
            "evidence": [_evidence("m5", "integration_contracts",
                                   "static declaration catalog (no live probe)")]}


# ---------------------------------------------------------------------------
# get_m5_pmc_progress
# ---------------------------------------------------------------------------

def _num(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _num0(value):
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _event_status(event_type: str) -> str:
    """Map a persisted execution event type to a progress operation status."""
    return {
        "actual_start": "running",
        "quantity_report": "running",
        "actual_finish": "completed",
        "exception": "exception",
        "scrap": "scrapped",
    }.get(str(event_type), "not_started")


def _build_progress(plan, is_current_head):
    schedule = plan.get("schedule") or {}
    ops = schedule.get("operations") or []
    scenario_id = plan.get("scenario_id") or ""
    events = plan.get("execution_events") or []
    by_order = {}
    for op in ops:
        oid = str(op.get("order_id") or op.get("order_line_id") or "UNKNOWN")
        row = by_order.setdefault(oid, {"order_id": oid, "operations": []})
        row["operations"].append(op)
    orders = []
    completed_orders = 0
    for oid, row in by_order.items():
        order_ops = sorted(row["operations"], key=lambda x: int(x.get("sequence_no") or 0))
        ops_out = []
        for op in order_ops:
            op_code = str(op.get("op_code") or "")
            evid = []
            actual_qty = 0.0
            scrap_qty = 0.0
            latest_status = "not_started"
            actual_end = None
            for e in events:
                if str(e.get("operation_id") or "") != op_code:
                    continue
                evid.append({
                    "event_id": str(e.get("event_id") or ""),
                    "event_type": str(e.get("event_type") or ""),
                    "source": str(e.get("source") or "manual"),
                    "external_ref": str(e.get("external_ref") or ""),
                    "occurred_at": str(e.get("occurred_at") or ""),
                    "worker_id": str(e.get("worker_id") or ""),
                    "team_id": str(e.get("team_id") or ""),
                    "station": str(e.get("station") or ""),
                    "reported_quantity": _num0(e.get("reported_quantity")),
                    "scrap_quantity": _num0(e.get("scrap_quantity")),
                    "status": str(e.get("status") or "accepted"),
                    "reason": str(e.get("reason") or ""),
                    "source_kind": "accepted_non_simulation_execution_event",
                })
                actual_qty += _num0(e.get("reported_quantity"))
                scrap_qty += _num0(e.get("scrap_quantity"))
                st = _event_status(e.get("event_type"))
                if st != "not_started":
                    latest_status = st
                if e.get("event_type") == "actual_finish":
                    actual_end = str(e.get("occurred_at") or "")
            first_res = str(op.get("resource_id") or op.get("equipment_code") or "")
            planned_qty = _num(op.get("qty"))
            completion = None
            if planned_qty and planned_qty > 0:
                completion = round(min(100.0, actual_qty / planned_qty * 100), 2)
            ops_out.append({
                "operation_id": op_code,
                "order_id": oid,
                "product_id": op.get("product_code") or "",
                "operation_name": str(op.get("operation_name") or op_code or ""),
                "resource_id": first_res,
                "resource": {
                    "resource_id": first_res,
                    "name": str(op.get("operation_name") or op.get("equipment_code") or first_res or ""),
                    "resource_type": "EQUIPMENT" if op.get("equipment_code") else "STATION",
                    "work_center": str(op.get("station_code") or ""),
                    "machine_code": str(op.get("equipment_code") or ""),
                    "status": "ACTIVE", "capability_tags": [],
                    "is_bottleneck": False, "source_kind": "m5_resource_master",
                },
                "planned_start_time": op.get("plan_start"),
                "planned_end_time": op.get("plan_end"),
                "planned_quantity": planned_qty,
                "unit": op.get("uom"),
                "actual_start_time": op.get("actual_start") or None,
                "actual_end_time": actual_end,
                "actual_qty": round(actual_qty, 4) if evid else None,
                "actual_status": latest_status,
                "completion_rate_percent": completion,
                "execution_evidence": evid,
            })
        first = row["operations"][0]
        order_status = "not_started"
        if ops_out:
            statuses = {op2["actual_status"] for op2 in ops_out}
            if statuses and statuses <= {"completed"}:
                order_status = "completed"
            elif any(st != "not_started" for st in statuses):
                order_status = "wip"
            elif "exception" in statuses:
                order_status = "wip"
        actual_quantity_supported = bool(
            ops_out and any(e.get("event_type") == "quantity_report"
                            for op2 in ops_out for e in op2.get("execution_evidence", []))
        )
        if order_status == "completed":
            completed_orders += 1
        orders.append({
            "order_id": oid,
            "product_id": first.get("product_code") or None,
            "planned_quantity": _num(first.get("qty")),
            "unit": first.get("uom"),
            "actual_qty": sum((op2.get("actual_qty") or 0) for op2 in ops_out) or None,
            "actual_quantity_supported": actual_quantity_supported,
            "terminal_operation_id": str(order_ops[-1].get("op_code") or "") if order_ops else None,
            "completion_rate_percent": ops_out[-1]["completion_rate_percent"] if ops_out else None,
            "due_time": None, "planned_completion_time": None,
            "actual_completion_time": ops_out[-1]["actual_end_time"] if ops_out else None,
            "status": order_status,
            "on_time": None, "personnel_bindings": [], "operations": ops_out,
        })
    wip_count = sum(1 for o in orders if o["status"] == "wip")
    return {
        "plan_version": plan["plan_version"],
        "generated_at": _now_iso(),
        "authority": {
            "scenario_id": scenario_id,
            "scenario_purpose": "production",
            "lifecycle_status": "released",
            "released_at": plan.get("released_at") or _now_iso(),
            "validation_passed": True,
            "is_current_head": is_current_head,
            "formal_display_eligible": True,
            "tracking_task_id": plan.get("task_id") or "",
            "input_hash": plan.get("input_hash"),
            "plan_source": "pmc_calculated_released_plan",
            "resource_source": "m5_resource_master",
            "actual_source": "accepted_non_simulation_execution_events",
            "personnel_source": "manual_team_member_order_bindings",
            "personnel_scope_note": "order_level_binding_not_attendance_or_live_presence",
        },
        "summary": {
            "order_count": len(orders),
            "completed_order_count": completed_orders,
            "wip_order_count": wip_count,
            "late_order_count": 0,
            "on_time_order_count": completed_orders,
            "on_time_rate_percent": round(completed_orders / len(orders) * 100, 2) if orders else None,
        },
        "orders": orders,
    }


async def m5_pmc_progress(payload, ctx):
    """Only a released, production, current-head plan is a valid progress
    context (m5.json output schema: errors maxItems=0 and authority consts).
    Everything else fails closed with an exception.

    V2 信封例外：合同顶层 ``additionalProperties:false`` + ``success const true``
    + ``errors maxItems 0``，无法表达错误信封 → 保持异常（V2 归 ``TOOL_ERROR``
    硬失败，这是合同强制的行为，不得为开 data 门而放宽 schema）。"""
    plan_version = str(payload.get("plan_version") or "")
    repo = _repo(ctx)
    plan = repo.get_plan(plan_version) if plan_version else None
    if plan is None:
        raise M5RepositoryError("PLAN_NOT_FOUND", f"计划 {plan_version} 不存在")
    if plan.get("lifecycle_status") != "released":
        raise M5RepositoryError("NOT_RELEASED",
                                f"计划 {plan_version} 未发布，不能作为生产上下文展示")
    if plan.get("scenario_purpose") != "production":
        raise M5RepositoryError("PURPOSE_NOT_PRODUCTION",
                                "pressure_only/preview 计划不能展示为生产上下文")
    head = repo.get_head(plan["scenario_id"])
    is_head = bool(head and head["head_plan_version"] == plan_version)
    if not is_head:
        raise M5RepositoryError("NOT_CURRENT_HEAD",
                                f"计划 {plan_version} 不是 scenario head")
    plan["execution_events"] = repo.list_execution_events(plan_version)
    # get_m5_pmc_progress output schema is additionalProperties:false at the
    # top level and errors maxItems:0, so evidence must ride inside data-free
    # state or not at all; keep the result exactly schema-shaped.
    return {"success": True, "data": _build_progress(plan, True), "errors": [],
            "trace_id": _trace(ctx, "m5-progress")}


# ---------------------------------------------------------------------------
# replan_m5_schedule
# ---------------------------------------------------------------------------

def _rechecksum(snapshot: dict) -> dict:
    """Recompute the canonical checksum after mutating a stored snapshot."""
    from .pmc_v2_snapshots import checksum_of
    snapshot["checksum"] = checksum_of(snapshot)
    return snapshot


def _replan_apply_event(base_bundle: dict, event: dict, plan_start: str | None) -> dict:
    """Apply one schema-defined replan event onto a copy of the parent bundle.

    Events are the only allowed mutations (Task 3).  Mutated snapshots get
    their canonical checksum recomputed so the new draft validates as an
    independent bundle.
    """
    from copy import deepcopy
    from .pmc_v2_snapshots import build_order_snapshot

    bundle = deepcopy(base_bundle)
    event_type = str(event.get("event_type") or "")
    event_payload = event.get("payload") or {}
    orders = bundle.setdefault("order_snapshots", [])
    routes = bundle.setdefault("routes", {})
    resource = bundle.setdefault("resource_snapshot", {})
    calendar = bundle.setdefault("calendar_snapshot", {})
    supply = bundle.setdefault("supply_snapshot", {})
    entries = supply.setdefault("entries", [])

    def _status_value(value: Any) -> str:
        text = str(value or "").upper()
        return {"ACTIVE": "ACTIVE", "AVAILABLE": "ACTIVE", "MAINTENANCE": "MAINTENANCE",
                "DOWN": "INACTIVE", "INACTIVE": "INACTIVE", "DISABLED": "INACTIVE"}.get(text, "ACTIVE")

    if event_type == "insert_order":
        for raw in event_payload.get("orders", []):
            product = str(raw.get("product_id") or raw.get("product_code") or "")
            if product not in routes:
                raise PmcError("BLOCKED_INPUT", f"MISSING_ROUTE product_code={product} (insert_order)")
            oid = str(raw.get("order_id") or "")
            orders.append(build_order_snapshot({
                "order_id": oid,
                "order_no": str(raw.get("order_no") or oid),
                "lines": [{
                    "order_line_id": raw.get("order_line_id") or f"{oid}::L1",
                    "product_code": product,
                    "qty": raw.get("quantity"),
                    "uom": raw.get("uom") or "PCS",
                    "due_date": raw.get("due_time"),
                    "priority": raw.get("priority"),
                }],
            }, snapshot_id=f"SNAP-ORD-{oid}-{raw.get('order_line_id') or 'L1'}"))
    elif event_type == "order_cancel":
        cancelled = {str(x) for x in event_payload.get("order_ids", [])}
        orders[:] = [o for o in orders if str(o.get("order_id")) not in cancelled]
    elif event_type in {"material_shortage", "material_delay", "material_supply_update"}:
        affected = {str(x) for x in event_payload.get("order_ids", [])}
        availability = {str(a.get("material_id")): a for a in event_payload.get("material_availability", [])
                        if isinstance(a, dict)}
        kitting = {str(k.get("material_code")): k for k in event_payload.get("order_kitting", [])
                   if isinstance(k, dict)}
        # update supply snapshot entries whose order line belongs to event
        updated = False
        for entry in entries:
            line = str(entry.get("order_line_id") or "")
            order_id = str(line).split("::")[0]
            if affected and order_id not in affected:
                continue
            material = str(entry.get("requirement_ref") or entry.get("inventory_snapshot_ref") or "")
            if event_type == "material_supply_update":
                avail = availability.get(material)
                if avail is not None and _num(avail.get("available_inventory", avail.get("available_qty"))) is not None:
                    entry["readiness"] = "READY" if _num(avail.get("available_inventory", avail.get("available_qty"))) > 0 else "NOT_READY"
                    if entry["readiness"] == "READY":
                        entry.pop("earliest_ready_at", None)
                    updated = True
            else:
                entry["readiness"] = "NOT_READY"
                if event_type == "material_delay":
                    entry["earliest_ready_at"] = event.get("occurred_at")
                updated = True
        if not updated and not entries:
            for order_line, kit in kitting.items():
                entries.append({
                    "order_line_id": str(kit.get("order_line_id") or order_line),
                    "op_code": kit.get("op_code"),
                    "readiness": "NOT_READY" if event_type != "material_supply_update" else "READY",
                    "requirement_ref": str(order_line),
                    "inventory_snapshot_ref": "EVENT-INV",
                })
        supply["entries"] = entries
        _rechecksum(supply)
    elif event_type == "equipment_down":
        rid = str(event_payload.get("resource_id") or "")
        duration = int(event_payload.get("duration_minutes") or 0)
        for eq in resource.setdefault("equipment", []):
            if str(eq.get("equipment_code")) == rid:
                eq["status"] = "INACTIVE"
                _rechecksum(resource)
                break
        else:
            raise PmcError("BLOCKED_INPUT", f"MISSING_EQUIPMENT equipment_code={rid} (equipment_down)")
        if duration > 0 and plan_start:
            ua = calendar.setdefault("unavailability", [])
            try:
                from datetime import timedelta
                start_dt = datetime.fromisoformat(str(plan_start).replace("Z", "+00:00"))
            except ValueError:
                start_dt = datetime.now(timezone.utc)
            ua.append({
                "resource_code": rid, "resource_type": "EQUIPMENT",
                "start_at": start_dt.isoformat(),
                "end_at": (start_dt + timedelta(minutes=duration)).isoformat(),
                "reason": "equipment_down",
            })
            _rechecksum(calendar)
    elif event_type == "capacity_change":
        rid = str(event_payload.get("resource_id") or "")
        status = _status_value(event_payload.get("status"))
        for eq in resource.setdefault("equipment", []):
            if str(eq.get("equipment_code")) == rid:
                eq["status"] = status
                _rechecksum(resource)
                break
        else:
            raise PmcError("BLOCKED_INPUT", f"MISSING_EQUIPMENT equipment_code={rid} (capacity_change)")
    elif event_type == "labor_shortage":
        skill = str(event_payload.get("skill_id") or "")
        for person in resource.setdefault("persons", []):
            if skill and skill in (person.get("skill_codes") or []):
                person["status"] = "INACTIVE"
        _rechecksum(resource)
    elif event_type == "manual_lock":
        # the v2 kernel has no lock model; the declared lock is preserved as
        # plan metadata and never silently converted into a schedule change.
        bundle["manual_locks"] = bundle.get("manual_locks") or []
        lock = event_payload.get("lock") or {}
        if isinstance(lock, dict):
            bundle["manual_locks"].append(lock)
    else:
        raise PmcError("BLOCKED_INPUT", f"UNKNOWN_REPLAN_EVENT event_type={event_type}")
    return bundle


async def m5_replan_schedule(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    base = str(payload.get("base_plan_version") or "")
    idem = str(payload.get("idempotency_key") or "")
    event = payload.get("event")
    def _replan_fail(code, message):
        return _blocked_result(
            message, _trace(ctx, "m5-replan"), code=code,
            data={"result": {"schedule": {"scenario_purpose": "production"}},
                  "scenario_purpose": "production", "input_hash": ""})
    if not base or not idem:
        return _replan_fail("MISSING_REPLAN_ARGS", "replan 需要 base_plan_version 与 idempotency_key")
    if not isinstance(event, dict) or not event:
        return _replan_fail("MISSING_EVENT", "replan 需要显式 event")
    repo = _repo(ctx)
    base_plan = repo.get_plan(base)
    if base_plan is None:
        return _replan_fail("PLAN_NOT_FOUND", f"父版本 {base} 不存在")
    expected = payload.get("expected_head_plan_version")
    if expected is not None and expected != base:
        return _replan_fail("HEAD_MISMATCH", "expected_head_plan_version 与 base_plan_version 不一致")
    head = repo.get_head(base_plan["scenario_id"])
    if head is None or head["head_plan_version"] != base:
        return _replan_fail("STALE_HEAD", "replan 只能基于当前 scenario head")
    # restore the original authoritative bundle from the server-stored parent
    base_bundle = base_plan.get("bundle")
    if not base_bundle:
        return _replan_fail("MISSING_BUNDLE", "父版本缺少服务端 bundle，不能重排")
    # apply ONLY the explicit event to a fresh copy of the parent bundle
    try:
        new_bundle = _replan_apply_event(base_bundle, event, base_plan.get("schedule") or base_plan.get("planning_start"))
    except PmcError as exc:
        return _replan_fail("BLOCKED_INPUT", exc.message)
    request = {
        "scenario_id": base_plan["scenario_id"],
        "scenario_purpose": base_plan.get("scenario_purpose", "production"),
        "pmc_v2_bundle": new_bundle,
        "idempotency_key": idem,
        "expected_head_plan_version": base,
        "planning_start": (base_plan.get("schedule") or {}).get("plan_start") or payload.get("planning_start"),
    }
    try:
        result = run_pmc_v2(request)
    except (PmcError, M5RepositoryError) as exc:
        return _replan_fail("BLOCKED_INPUT", str(getattr(exc, "message", exc)))
    digest = result["data"]["input_hash"]
    new_version = f"replan-{base}-{digest[:8]}"
    # replan only applies the explicit event on top of the server-restored
    # parent bundle; it never fabricates input.  Locks and frozen windows are
    # preserved as replan metadata when the client declares them.
    new_schedule = dict(result["data"]["schedule"])
    preserved = {
        "base_plan_version": base,
        "event": event,
        "freeze_policy": payload.get("freeze_policy"),
        "manual_locks": new_bundle.get("manual_locks") or base_plan.get("manual_locks"),
        "frozen_windows": base_plan.get("frozen_windows"),
    }
    new_schedule["replan_meta"] = {
        key: value for key, value in preserved.items() if value is not None
    }
    repo.save_plan(
        plan_version=new_version, scenario_id=base_plan["scenario_id"],
        tenant_id=_tenant(ctx), task_id=_task(ctx), lifecycle_status="draft",
        parent_plan_version=base, input_hash=digest,
        solver_hash=f"solver-{result['data'].get('algorithm_version', 'pmc-v2')}",
        algorithm_version=result["data"].get("algorithm_version", "pmc-v2-frozen-20260902"),
        scenario_purpose=base_plan.get("scenario_purpose", "production"),
        validation_report=result["data"].get("validator") or {},
        bundle=result["data"]["input_package"],
        schedule=new_schedule,
        idempotency_key=idem,
    )
    return {
        "success": not result["data"].get("blocks"),
        "code": _block_code(result["data"].get("blocks")),
        "data": {
            "result": {"schedule": new_schedule, "blocks": result["data"].get("blocks", [])},
            "scenario_purpose": base_plan.get("scenario_purpose", "production"),
            "input_hash": digest,
            "plan_version": new_version,
            "parent_plan_version": base,
            "event": event,
            # 重排只产出待审批候选：显式回传 lifecycle_status，供编排层按
            # "产出 draft 计划" 统一开 Apply Gate（不要靠工具名硬编码判断）。
            "lifecycle_status": "draft",
        },
        "errors": [{"code": "BLOCKED_INPUT", "message": b.get("reason", ""), "details": [b]}
                   for b in result["data"].get("blocks", [])],
        "trace_id": _trace(ctx, "m5-replan"),
        "evidence": [_evidence("m5", "replan", f"replan from {base} -> {new_version}")],
    }


# ---------------------------------------------------------------------------
# knowledge tools
# ---------------------------------------------------------------------------

def _flatten_feature_pairs(value: Any, prefix: str = "") -> dict[str, str]:
    """把嵌套特征字典压成 path -> 归一化字符串 的扁平对，便于确定性比较。"""
    pairs: dict[str, str] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            pairs.update(_flatten_feature_pairs(item, child))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            pairs.update(_flatten_feature_pairs(item, f"{prefix}[{index}]"))
    elif value is not None:
        pairs[prefix] = str(value).strip().lower()
    return pairs


def _feature_match_score(query_features: Any, case_features: Any) -> float:
    """确定性特征重叠度：命中的查询字段对 ÷ 查询字段对总数（0..1）。

    只做等值比较（无模型调用、无相似度猜测）；查询没有可比较字段时返回 0，
    由调用方回退到记录时间排序。
    """
    query = _flatten_feature_pairs(query_features or {})
    if not query:
        return 0.0
    case = _flatten_feature_pairs(case_features or {})
    matched = sum(1 for key, value in query.items() if case.get(key) == value)
    return round(matched / len(query), 4)


async def m5_search_knowledge(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    repo = _repo(ctx)
    outcome = payload.get("outcome_filter")
    tag = payload.get("tag_filter")
    features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
    hits = repo.list_knowledge(outcome_filter=outcome, tag_filter=tag)
    if tag:
        hits = [h for h in hits if tag in (h.get("tags") or [])]
    scored = [
        {**hit, "match_score": _feature_match_score(features, hit.get("features") or {})}
        for hit in hits
    ]
    # 分数优先，同分按记录时间倒序（与无特征时的旧行为一致）
    scored.sort(key=lambda item: (item["match_score"], str(item.get("created_at") or "")), reverse=True)
    cases_with_features = sum(1 for hit in hits if hit.get("features"))
    return {"success": True,
            "data": {"hits": scored[: int(payload.get("top_k") or 5)],
                     "total_cases_scanned": len(hits),
                     "similarity_basis": "feature_overlap" if (features and cases_with_features) else "recency",
                     "cases_with_features": cases_with_features,
                     "generated_at": _now_iso()},
            "errors": [], "trace_id": _trace(ctx, "m5-search-knowledge"),
            "evidence": [_evidence("m5", "knowledge", f"searched {len(hits)} cases")]}


async def m5_record_knowledge(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    plan_version = str(payload.get("plan_version") or "")
    repo = _repo(ctx)
    plan = repo.get_plan(plan_version)
    if plan is None:
        # record_m5_knowledge output schema requires a real 64-char
        # input_sha256 and a persisted plan_version; a missing authoritative
        # plan therefore fails closed (HTTP-level error semantics).
        # V2 信封例外：该合同 data.required 为 11 个业务字段，返回错误信封会
        # 直接过不了 registry 出参校验（registry.py:138），故保留异常语义。
        raise M5RepositoryError("PLAN_NOT_FOUND",
                                f"计划 {plan_version} 不存在，无法沉淀知识")
    schedule = plan.get("schedule") or {}
    metrics = schedule.get("metrics") or {}
    on_time_rate = metrics.get("on_time_rate")
    if on_time_rate is None:
        on_time_rate = 0
    tardiness = metrics.get("total_tardiness_minutes")
    if tardiness is None:
        tardiness = 0
    knowledge_id = f"K-{plan_version}-{uuid4().hex[:8]}"
    recorded = repo.record_knowledge(
        knowledge_id=knowledge_id, scenario_id=plan.get("scenario_id") or "",
        plan_version=plan_version, tenant_id=_tenant(ctx), task_id=_task(ctx),
        plan_facts={
            "input_hash": plan.get("input_hash"), "outcome": "recorded",
            "solver_status": schedule.get("solver_status", ""),
            "on_time_rate": on_time_rate,
            "total_tardiness_minutes": tardiness,
            "validation_passed": (schedule.get("validation_report") or {}).get("status") == "pass",
        },
        tags=payload.get("tags") or [], note=str(payload.get("note") or ""),
        features=payload.get("features") or {},
    )
    return {"success": True,
            "data": {
                "id": recorded["knowledge_id"], "scenario_id": plan.get("scenario_id"),
                "plan_version": plan_version, "features": payload.get("features") or {},
                "outcome": "recorded", "solver_status": schedule.get("solver_status", ""),
                "on_time_rate": on_time_rate,
                "total_tardiness_minutes": tardiness,
                "validation_passed": (schedule.get("validation_report") or {}).get("status") == "pass",
                "input_sha256": plan.get("input_hash"), "created_at": _now_iso(),
            },
            "errors": [], "trace_id": _trace(ctx, "m5-record-knowledge"),
            "evidence": [_evidence("m5", "knowledge", f"recorded {knowledge_id}")]}


# ---------------------------------------------------------------------------
# department messages
# ---------------------------------------------------------------------------

async def m5_prepare_message(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    idem = str(payload.get("idempotency_key") or "")
    if not idem:
        return _blocked_result("prepare message 需要 idempotency_key", _trace(ctx, "m5-message"),
                               code="MISSING_IDEMPOTENCY")
    draft_id = f"DRAFT-{uuid4().hex[:10]}"
    repo = _repo(ctx)
    subject = (payload.get("template_variables") or {}).get("subject") or ""
    body = (payload.get("template_variables") or {}).get("body") or ""
    if not body:
        body = f"{payload.get('message_kind')}: {payload.get('event_summary')}"
    created = repo.create_message(
        draft_id=draft_id, tenant_id=_tenant(ctx), task_id=_task(ctx),
        idempotency_key=idem, department=str(payload.get("department") or ""),
        channel=str(payload.get("channel") or ""),
        recipient_targets=[str(x) for x in payload.get("recipient_targets") or []],
        message_kind=str(payload.get("message_kind") or ""),
        subject=subject, body=body,
        event_summary=str(payload.get("event_summary") or ""),
        required_action=str(payload.get("required_action") or ""),
        evidence=[dict(e) for e in payload.get("evidence") or []],
    )
    return {"success": True,
            "data": {"draft": {
                "draft_id": created["draft_id"], "department": payload.get("department"),
                "channel": payload.get("channel"), "message_kind": payload.get("message_kind"),
                "subject": subject, "body": body, "status": created["status"],
            }, "replayed": created.get("replayed", False)},
            "errors": [], "trace_id": _trace(ctx, "m5-message"),
            "evidence": [_evidence("m5", "message", f"draft {draft_id} pending_approval")]}


async def m5_get_message(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    draft_id = str(payload.get("draft_id") or "")
    repo = _repo(ctx)
    message = repo.get_message(draft_id) if draft_id else None
    if message is None:
        return _blocked_result(f"消息 {draft_id} 不存在", _trace(ctx, "m5-message"),
                               code="MESSAGE_NOT_FOUND")
    return {"success": True, "data": dict(message), "errors": [],
            "trace_id": _trace(ctx, "m5-message"),
            "evidence": [_evidence("m5", "message", f"read draft {draft_id}")]}


async def m5_message_delivery(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    outbox_id = str(payload.get("outbox_id") or "")
    repo = _repo(ctx)
    outbox = repo.get_outbox(outbox_id) if outbox_id else None
    if outbox is None:
        return _blocked_result(f"outbox {outbox_id} 不存在", _trace(ctx, "m5-message"),
                               code="OUTBOX_NOT_FOUND")
    return {"success": True, "data": dict(outbox), "errors": [],
            "trace_id": _trace(ctx, "m5-message"),
            "evidence": [_evidence("m5", "message", f"read outbox {outbox_id}")]}


# ---------------------------------------------------------------------------
# advise_m5_schedule
# ---------------------------------------------------------------------------

def _advice_items(metrics: Any, operations: Any) -> list[dict[str, Any]]:
    """PMC 规则建议（催料/协商交期/保持计划）。

    ``advise_m5_schedule`` 与 ``run_m5_intelligent_schedule(enable_advise=true)``
    共用同一实现，避免同一能力两处规则漂移。
    """
    metrics = metrics if isinstance(metrics, dict) else {}
    operations = operations if isinstance(operations, list) else []
    on_time = metrics.get("on_time_rate")
    tardiness = metrics.get("total_tardiness_minutes") or 0
    late_by_order: dict[str, float] = {}
    for op in operations:
        if not isinstance(op, dict):
            continue
        oid = str(op.get("order_id") or op.get("order_line_id") or "?")
        delta = op.get("tardiness_minutes")
        if delta is not None:
            try:
                late_by_order[oid] = max(late_by_order.get(oid, 0.0), float(delta))
            except (TypeError, ValueError):
                continue
    items: list[dict[str, Any]] = []
    if tardiness and float(tardiness) > 0:
        items.append({"action": "expedite", "kind": "material",
                      "evidence": [f"total_tardiness_minutes={tardiness}"],
                      "note": "存在迟交，建议核对物料供应与加班窗口"})
    for oid, delta in sorted(late_by_order.items(), key=lambda x: -x[1]):
        if delta > 0:
            items.append({"action": "negotiate", "kind": "delivery",
                          "evidence": [f"order {oid} tardiness={delta}"],
                          "note": "建议与客户协商交期或增加该订单资源"})
    if on_time is not None and float(on_time) >= 100:
        items.append({"action": "keep", "kind": "schedule",
                      "evidence": ["on_time_rate=100"], "note": "当前计划满足交期，无需调整"})
    return items


async def m5_advise_schedule(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    items = _advice_items(payload.get("metrics") or {}, payload.get("operations") or [])
    return {"success": True,
            "data": {"plan_version": payload.get("plan_version"), "items": items,
                     "summary": f"{len(items)} 条建议", "source": "rule_based",
                     "generated_at": _now_iso()},
            "errors": [], "trace_id": _trace(ctx, "m5-advise"),
            "evidence": [_evidence("m5", "advise", "rule-based advice with evidence")]}


# ---------------------------------------------------------------------------
# run_m5_intelligent_schedule
# ---------------------------------------------------------------------------

async def m5_intelligent_schedule(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    request = payload.get("schedule_request")
    if not isinstance(request, dict):
        return _blocked_result("缺少 schedule_request", _trace(ctx, "m5-intelligent"),
                               code="MISSING_SCHEDULE_REQUEST")
    request = {**request, "scenario_purpose": _purpose_of(request)}
    try:
        result = run_pmc_v2(request)
    except (PmcError, M5RepositoryError) as exc:
        return _blocked_result(str(getattr(exc, "message", exc)), _trace(ctx, "m5-intelligent"),
                               code="BLOCKED_INPUT")
    schedule = result["data"]["schedule"]
    data = {"schedule": schedule}
    if payload.get("enable_advise"):
        # 与 advise_m5_schedule 共用同一规则实现，避免两处建议逻辑漂移
        data["advice"] = {
            "items": _advice_items(schedule.get("metrics") or {}, schedule.get("operations") or []),
            "source": "rule_based",
        }
    if payload.get("enable_audit"):
        data["audit"] = {"input_hash": result["data"].get("input_hash"),
                         "solver": schedule.get("algorithm_version"),
                         "non_persistent": True}
    data["auto_replanned"] = False
    # knowledge_case is only created after the plan is persisted via
    # record_m5_knowledge; this analysis run never fabricates a case.
    data["knowledge_case"] = {"persisted": False}
    data["messages"] = []
    return {"success": not schedule.get("blocks"),
            "code": _block_code(schedule.get("blocks")),
            "data": data,
            "errors": [{"code": "BLOCKED_INPUT", "message": b.get("reason", ""), "details": [b]}
                       for b in schedule.get("blocks", [])],
            "trace_id": _trace(ctx, "m5-intelligent"),
            "evidence": [_evidence("m5", "intelligent_schedule",
                                   "non-persistent v2 analysis only")]}


# ---------------------------------------------------------------------------
# generate_m5_material_procurement_plan (proposal only)
# ---------------------------------------------------------------------------

async def m5_procurement_plan(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    scenario_id = str(payload.get("scenario_id") or "")
    request = {**payload, "scenario_purpose": _purpose_of(payload)}
    order_kitting = [x for x in (payload.get("order_kitting") or []) if isinstance(x, dict)]
    material_availability = [x for x in (payload.get("material_availability") or []) if isinstance(x, dict)]
    if not order_kitting:
        # 没有权威齐套事实就没有需求基线：按订单行编造占位缺口等于伪造事实。
        return {
            "success": False,
            "status": "blocked",
            "code": "BLOCKED_INPUT",
            "data": {"scenario_id": scenario_id,
                     "missing_fields": ["order_kitting"],
                     "recovery": "请提供权威齐套事实（order_kitting: material_code/required_qty）后重试；"
                                 "本工具不按订单行编造占位缺口"},
            "errors": [{"code": "MISSING_KITTING_FACTS",
                        "message": "缺少 order_kitting 齐套事实，无法计算物料采购需求",
                        "details": []}],
            "trace_id": _trace(ctx, "m5-procurement"),
            "evidence": [_evidence("m5", "procurement_proposal",
                                   "no order_kitting facts: fail-closed (no placeholder)")],
        }
    available = {str(x.get("material_code")): x for x in material_availability}
    kitting = {str(x.get("material_code")): x for x in order_kitting}
    missing_availability = sorted(
        code for code in kitting if code and code not in available
    )
    if missing_availability:
        # 齐套需求有、可用量事实没有：不得用 0 冒充「全缺」，先补齐权威事实。
        return {
            "success": False,
            "status": "blocked",
            "code": "BLOCKED_INPUT",
            "data": {"scenario_id": scenario_id,
                     "missing_fields": [f"material_availability[{code}]" for code in missing_availability],
                     "recovery": "请补齐这些物料的权威可用量事实（material_availability: "
                                 "material_code/available_qty）后重试；本工具不把缺失可用量当成 0"},
            "errors": [{"code": "MISSING_MATERIAL_AVAILABILITY",
                        "message": "缺少齐套物料的可用量事实：" + ", ".join(missing_availability),
                        "details": missing_availability}],
            "trace_id": _trace(ctx, "m5-procurement"),
            "evidence": [_evidence("m5", "procurement_proposal",
                                   "missing availability facts: fail-closed")],
        }
    try:
        result = run_pmc_v2(request)
    except (PmcError, M5RepositoryError) as exc:
        return _blocked_result(str(getattr(exc, "message", exc)), _trace(ctx, "m5-procurement"),
                               code="BLOCKED_INPUT")
    lines = []
    for material_code, kit in kitting.items():
        demand = float(kit.get("required_qty") or 0)
        on_hand = float((available.get(material_code) or {}).get("available_qty") or 0)
        shortage = max(0.0, demand - on_hand)
        lines.append({
            "material_code": material_code,
            "required_qty": demand, "available_qty": on_hand,
            "shortage_qty": shortage,
            "status": "proposal",
            "note": "只生成 M3/M4 proposal，不写入库存或采购事实",
        })
    shortage_count = sum(1 for x in lines if x.get("shortage_qty", 0) > 0)
    proposal = {
        "scenario_id": scenario_id, "generated_at": _now_iso(),
        "planning_start": payload.get("planning_start"),
        "confidence_threshold": 0.0,
        "material_count": len(lines), "shortage_material_count": shortage_count,
        "lines": lines,
        "messages": [] if not lines else [{"level": "info", "text": "建议由 M3/M4 按 proposal 审核"}],
    }
    return {"success": True, "data": proposal, "errors": [],
            "trace_id": _trace(ctx, "m5-procurement"),
            "evidence": [_evidence("m5", "procurement_proposal",
                                   f"{scenario_id}: {len(lines)} material lines (proposal only, "
                                   f"input_hash={result['data'].get('input_hash')})")]}


# ---------------------------------------------------------------------------
# registry binding
# ---------------------------------------------------------------------------

M5_HANDLERS = {
    "get_m5_schedule": m5_get_schedule,
    "list_m5_schedules": m5_list_schedules,
    "get_m5_pmc_progress": m5_pmc_progress,
    "get_m5_material_readiness": m5_material_readiness,
    "get_m5_integration_contracts": m5_integration_contracts,
    "ingest_m5_planning_snapshot": m5_ingest_snapshot,
    "replan_m5_schedule": m5_replan_schedule,
    "dispatch_m5_schedule": m5_dispatch_schedule,
    "get_m5_execution_summary": m5_execution_summary,
    "search_m5_knowledge": m5_search_knowledge,
    "record_m5_knowledge": m5_record_knowledge,
    "prepare_m5_department_message": m5_prepare_message,
    "get_m5_department_message": m5_get_message,
    "get_m5_department_message_delivery": m5_message_delivery,
    "advise_m5_schedule": m5_advise_schedule,
    "run_m5_intelligent_schedule": m5_intelligent_schedule,
    "generate_m5_material_procurement_plan": m5_procurement_plan,
}
