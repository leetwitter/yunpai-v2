"""Bridge the current M5 payload to the frozen WIP PMC v2 scheduler.

The adapter deliberately keeps the v2 snapshot boundary visible in the
result.  Legacy callers can continue using the old baseline scheduler, while
payloads carrying approved standard minutes and explicit calendars/resources
use the constrained engine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from .pmc_v2_scheduler import schedule_operations
from .pmc_wip_projection import project_wip_pmc
from .pmc_v2_snapshots import (
    PmcError, build_constraint_snapshot, build_order_snapshot,
    build_supply_snapshot, canonical_bytes, finalize_snapshot, validate_bundle,
)

TZ = timezone(timedelta(hours=8))

# Strict production path: no implicit calendar/capacity/efficiency/approval
# defaults are injected here.  Requests not explicitly marked as a legacy
# preview must carry every hard fact explicitly (0902 PMC v2 contract).
def _strict_production(payload: dict[str, Any]) -> bool:
    purpose = str(payload.get("scenario_purpose") or "production").lower()
    return purpose == "production" and not bool(payload.get("legacy_preview"))


def _blocked(code: str, message: str) -> "PmcError":
    return PmcError("BLOCKED_INPUT", f"{code} {message}")


def _status(value: Any) -> str:
    text = str(value or "ACTIVE").upper()
    return {"AVAILABLE": "ACTIVE", "ACTIVE": "ACTIVE", "MAINTENANCE": "MAINTENANCE", "DOWN": "INACTIVE", "DISABLED": "INACTIVE", "INACTIVE": "INACTIVE"}.get(text, "ACTIVE")


def _iso(value: Any, fallback: datetime) -> str:
    if value:
        return str(value)
    return fallback.isoformat()


def _calendar(payload: dict[str, Any]) -> dict[str, Any]:
    strict = _strict_production(payload)
    windows = payload.get("calendar_windows") or payload.get("calendar") or []
    if isinstance(windows, dict):
        windows = windows.get("working_intervals") or windows.get("windows") or []
    intervals = []
    for item in windows:
        if not isinstance(item, dict):
            continue
        start = item.get("start_at")
        end = item.get("end_at")
        if not start and item.get("date"):
            # Date-only calendar rows would silently synthesize a default
            # 08:00-17:00 shift, which changes plan results (P0-2).  That is
            # only tolerated on the explicitly marked legacy/preview path.
            if strict:
                raise _blocked("MISSING_CALENDAR_TIME",
                               "date-only calendar rows are not allowed in production; provide start_at/end_at")
            start = f"{item['date']}T{item.get('start', '08:00')}:00+08:00"
            end = f"{item['date']}T{item.get('end', '17:00')}:00+08:00"
        if not start or not end:
            raise _blocked("MISSING_CALENDAR", "working intervals require start_at and end_at")
        if strict and not item.get("calendar_ref"):
            raise _blocked("MISSING_CALENDAR_REF", "production calendar windows require an explicit calendar_ref")
        intervals.append({"calendar_ref": str(item.get("calendar_ref") or "CAL-DEFAULT"), "start_at": str(start), "end_at": str(end), "shift_code": str(item.get("shift_code") or item.get("shift") or "DAY")})
    if not intervals:
        raise PmcError("BLOCKED_INPUT", "MISSING_CALENDAR working intervals are required for PMC v2")
    unavailable = payload.get("resource_unavailability") or []
    return finalize_snapshot({"snapshot_id": f"SNAP-CAL-{payload.get('scenario_id', 'M5')}", "revision": 1, "working_intervals": intervals, "unavailability": list(unavailable)})


def _resources(payload: dict[str, Any], calendar: dict[str, Any]) -> dict[str, Any]:
    strict = _strict_production(payload)
    items = payload.get("resources") or []
    explicit = payload.get("resource_snapshot")
    if isinstance(explicit, dict):
        # 显式 snapshot 若未携带 checksum，则补齐（校验器要求 checksum），
        # 与 orchestration_bridge 的 finalize 语义保持一致。
        if not explicit.get("checksum"):
            explicit = finalize_snapshot(explicit)
        return explicit
    cal_ref = str((calendar.get("working_intervals") or [{}])[0].get("calendar_ref") or "CAL-DEFAULT")
    equipment, persons, tooling, stations = [], [], [], []
    for item in items:
        code = str(item.get("resource_id") or item.get("code") or "")
        if not code:
            continue
        kind = str(item.get("resource_type") or item.get("type") or "equipment").upper()
        if kind in {"PERSON", "OPERATOR", "LABOR"}:
            if strict and not item.get("calendar_ref"):
                raise _blocked("MISSING_RESOURCE_BINDING",
                               f"person {code} requires an explicit calendar_ref in production")
            persons.append({"person_code": code, "skill_codes": list(item.get("skills") or item.get("skill_codes") or []), "qualified_operation_codes": list(item.get("qualified_operation_codes") or []), "max_parallel_tasks": int(item.get("max_parallel_tasks") or 1), "status": _status(item.get("status")), "calendar_ref": str(item.get("calendar_ref") or cal_ref)})
        elif kind == "TOOLING":
            if strict and not item.get("calendar_ref"):
                raise _blocked("MISSING_RESOURCE_BINDING",
                               f"tooling {code} requires an explicit calendar_ref in production")
            tooling.append({"tooling_code": code, "tooling_type": str(item.get("tooling_type") or "fixture"), "capability_codes": list(item.get("capability_codes") or []), "compatible_product_codes": list(item.get("compatible_product_codes") or []), "quantity_available": int(item.get("quantity_available") or item.get("capacity") or 1), "status": _status(item.get("status")), "calendar_ref": str(item.get("calendar_ref") or cal_ref)})
        elif kind == "STATION":
            if strict and not item.get("calendar_ref"):
                raise _blocked("MISSING_RESOURCE_BINDING",
                               f"station {code} requires an explicit calendar_ref in production")
            stations.append({"station_code": code, "work_center_code": str(item.get("work_center_code") or code), "parallel_slots": int(item.get("parallel_slots") or 1), "status": _status(item.get("status")), "calendar_ref": str(item.get("calendar_ref") or cal_ref)})
        else:
            capacity = item.get("capacity_per_hour") or item.get("capacity")
            efficiency = item.get("efficiency_factor") or item.get("efficiency")
            equipment_type = item.get("equipment_type") or item.get("name")
            if strict and not item.get("calendar_ref"):
                raise _blocked("MISSING_RESOURCE_BINDING",
                               f"equipment {code} requires an explicit calendar_ref in production")
            if strict and (capacity is None or capacity == ""):
                raise _blocked("MISSING_CAPACITY", f"equipment {code} requires capacity_per_hour in production")
            if strict and (efficiency is None or efficiency == ""):
                raise _blocked("MISSING_EFFICIENCY", f"equipment {code} requires efficiency_factor in production")
            if strict and not equipment_type:
                raise _blocked("MISSING_EQUIPMENT_TYPE", f"equipment {code} requires equipment_type in production")
            equipment.append({"equipment_code": code, "equipment_type": str(equipment_type or "machine"), "capability_codes": list(item.get("capability_codes") or item.get("capabilities") or []), "capacity_per_hour": str(capacity or 60), "efficiency_factor": str(efficiency or 1), "status": _status(item.get("status")), "calendar_ref": str(item.get("calendar_ref") or cal_ref)})
    return finalize_snapshot({"snapshot_id": f"SNAP-RES-{payload.get('scenario_id', 'M5')}", "revision": 1, "equipment": equipment, "tooling": tooling, "persons": persons, "stations": stations})


def _routes(payload: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    strict = _strict_production(payload)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in payload.get("routing_steps") or []:
        product = str(raw.get("product_id") or raw.get("product_code") or "")
        if not product:
            continue
        grouped.setdefault(product, []).append(raw)
    routes: dict[str, Any] = {}
    for product, raw_ops in grouped.items():
        raw_ops = sorted(raw_ops, key=lambda x: int(x.get("sequence") or x.get("sequence_no") or 0))
        ops = []
        for index, raw in enumerate(raw_ops, start=1):
            code = str(raw.get("operation_id") or raw.get("op_code") or f"OP-{index:03d}")
            eligible = raw.get("eligible_resources") or []
            eq_codes = list(raw.get("required_equipment_codes") or [])
            if not eq_codes:
                eq_codes = [str(x.get("resource_id")) for x in eligible if x.get("resource_id") and str(x.get("resource_type") or "equipment").upper() not in {"PERSON", "TOOLING", "STATION"}]
            std = raw.get("standard_minutes")
            if std is None:
                std = raw.get("std_minutes")
            if std is None and eligible:
                std = eligible[0].get("standard_minutes")
            if std is None:
                raise PmcError("BLOCKED_INPUT", f"MISSING_STANDARD_MINUTES op_code={code}")
            loss = raw.get("loss_rate")
            yield_rate = raw.get("yield_rate")
            if yield_rate is None:
                yield_rate = 1 - float(loss or 0)
            predecessors = raw.get("predecessors")
            if predecessors is None:
                predecessor = raw.get("predecessor")
                predecessors = [str(predecessor)] if predecessor else ([ops[-1]["op_code"]] if ops else [])
            groups = {
                "required_equipment_codes": eq_codes,
                "required_equipment_capabilities": list(raw.get("required_equipment_capabilities") or raw.get("capability_codes") or []),
                "required_person_codes": list(raw.get("required_person_codes") or []),
                "required_skill_codes": list(raw.get("required_skill_codes") or raw.get("required_skills") or []),
                "required_tooling_codes": list(raw.get("required_tooling_codes") or []),
                "required_station_codes": list(raw.get("required_station_codes") or []),
            }
            if not any(groups.values()):
                raise PmcError("BLOCKED_INPUT", f"MISSING_RESOURCE_REQUIREMENT op_code={code}")
            approval_ref = raw.get("approval_ref") or payload.get("route_approval_ref")
            if strict and not approval_ref:
                raise PmcError("BLOCKED_INPUT", f"MISSING_APPROVAL_REF op_code={code}")
            op = {"op_code": code, "name": str(raw.get("operation_name") or raw.get("name") or code), "sequence_no": index, "predecessors": predecessors, "standard_minutes": float(std), "quantity_basis": int(raw.get("quantity_basis") or raw.get("batch_size") or 1), "batch_size": int(raw.get("batch_size") or raw.get("quantity_basis") or 1), "setup_minutes": int(raw.get("setup_minutes") or 0), "setup_family": raw.get("setup_family"), "parallel_allowed": bool(raw.get("parallel_allowed", False)), "yield_rate": float(yield_rate), **groups, "approval_ref": str(approval_ref or "APPROVED-ROUTE")}
            if raw.get("transfer_batch_size") is not None:
                op["transfer_batch_size"] = int(raw["transfer_batch_size"])
            ops.append(op)
        flow_mode = str(payload.get("execution_model") or payload.get("flow_mode") or "").upper()
        route_version = payload.get("route_version")
        route_code = payload.get("route_code")
        if strict and not route_version:
            raise PmcError("BLOCKED_INPUT", f"MISSING_ROUTE_VERSION product_code={product}")
        if strict and not route_code:
            raise PmcError("BLOCKED_INPUT", f"MISSING_ROUTE_CODE product_code={product}")
        route = {"snapshot_id": f"SNAP-RT-{product}", "revision": 1, "product_code": product, "route_code": str(route_code or f"ROUTE-{product}"), "route_version": str(route_version or "approved-v2"), "approval_ref": str(approval_ref or "APPROVED-ROUTE"), "operations": ops}
        if flow_mode:
            route["execution_model"] = flow_mode
        if payload.get("transfer_batch_size") is not None:
            route["transfer_batch_size"] = int(payload["transfer_batch_size"])
        routes[product] = finalize_snapshot(route)
    if not routes:
        raise PmcError("BLOCKED_INPUT", "MISSING_ROUTE_SNAPSHOT no routes")
    return routes


def build_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    strict = _strict_production(payload)
    supplied = payload.get("pmc_v2_bundle")
    if isinstance(supplied, dict):
        return supplied
    orders = []
    for raw in payload.get("orders") or []:
        orders.append(build_order_snapshot({"order_id": str(raw.get("order_id") or ""), "order_no": str(raw.get("order_no") or raw.get("order_id") or ""), "lines": [{"order_line_id": raw.get("order_line_id") or f"{raw.get('order_id')}::L1", "product_code": str(raw.get("product_id") or raw.get("product_code") or ""), "qty": raw.get("quantity"), "uom": raw.get("uom") or "PCS", "due_date": raw.get("due_time"), "priority": raw.get("priority")}]}, snapshot_id=f"SNAP-ORD-{raw.get('order_id')}"))
    calendar = _calendar(payload)
    supply_entries = payload.get("supply_entries") or _supply_entries(payload)
    if strict and not supply_entries and not payload.get("order_kitting"):
        raise _blocked("MISSING_SUPPLY", "production 排程必须携带 supply/readiness 或 order_kitting 事实")
    bundle = {"bundle_version": "pmc-input-bundle.v2", "order_snapshots": orders, "routes": _routes(payload, orders), "resource_snapshot": _resources(payload, calendar), "calendar_snapshot": calendar, "supply_snapshot": build_supply_snapshot(supply_entries), "constraint_snapshot": build_constraint_snapshot(payload.get("changeover_rules") or payload.get("setup_matrix") or {}, constraint_version="wip-v2")}
    return bundle


def _supply_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries = []
    for item in payload.get("wip_status") or []:
        if not isinstance(item, dict):
            continue
        entries.append({"order_line_id": str(item.get("order_line_id") or f"{item.get('order_id')}::L1"), "op_code": item.get("op_code") or item.get("next_operation_id"), "readiness": "READY" if str(item.get("status") or item.get("readiness") or "").upper() in {"READY", "COMPLETED"} else "NOT_READY", "earliest_ready_at": item.get("earliest_ready_at") or item.get("ready_at"), "requirement_ref": str(item.get("requirement_ref") or "WIP-SNAPSHOT"), "inventory_snapshot_ref": str(item.get("inventory_snapshot_ref") or "INV-SNAPSHOT")})
    for item in payload.get("material_availability") or []:
        if not isinstance(item, dict):
            continue
        entries.append({"order_line_id": str(item.get("order_line_id") or f"{item.get('order_id')}::L1"), "op_code": item.get("op_code"), "readiness": "READY" if str(item.get("readiness") or "").upper() == "READY" else "NOT_READY", "earliest_ready_at": item.get("earliest_ready_at"), "requirement_ref": str(item.get("requirement_ref") or item.get("material_code") or "MATERIAL"), "inventory_snapshot_ref": str(item.get("inventory_snapshot_ref") or "INV-SNAPSHOT"), "shortage_ref": item.get("shortage_ref"), "purchase_ref": item.get("purchase_ref")})
    return entries


def run_pmc_v2(payload: dict[str, Any]) -> dict[str, Any]:
    bundle = build_bundle(payload)
    validate_bundle(bundle)
    operations, intervals, blocks = schedule_operations(bundle)
    route_meta = {code: {op["op_code"]: op for op in route["operations"]} for code, route in bundle["routes"].items()}
    enriched = []
    for op in operations:
        meta = route_meta.get(op["product_code"], {}).get(op["op_code"], {})
        enriched.append({**op, "operation_name": meta.get("name", op["op_code"]), "sequence_no": meta.get("sequence_no"), "standard_minutes": meta.get("standard_minutes"), "quantity_basis": meta.get("quantity_basis"), "yield_rate": meta.get("yield_rate", 1), "loss_rate": round(1 - float(meta.get("yield_rate", 1)), 6), "setup_family": meta.get("setup_family"), "wip_state": "released"})
    starts = [op["plan_start"] for op in enriched]
    ends = [op["plan_end"] for op in enriched]
    digest = sha256(canonical_bytes(bundle, skip=())).hexdigest()
    processing = sum(float(op["processing_minutes"]) for op in enriched)
    setup = sum(float(op["setup_minutes"]) for op in enriched)
    schedule_start = starts[0] if starts else None
    makespan = max((int((datetime.fromisoformat(x.replace('Z', '+00:00')) - datetime.fromisoformat(schedule_start.replace('Z', '+00:00'))).total_seconds() / 60) for x in ends), default=0) if schedule_start else 0
    flow_modes = sorted({str(route.get("execution_model") or route.get("flow_mode")) for route in bundle["routes"].values() if route.get("execution_model") or route.get("flow_mode")})
    schedule = {"scenario_id": payload.get("scenario_id"), "scenario_purpose": payload.get("scenario_purpose", "production"), "plan_version": f"wip-v2-{digest[:10]}", "algorithm_version": "pmc-v2-streaming-20260903" if "STREAMING_FLOW" in flow_modes else "pmc-v2-frozen-20260902", "execution_model": "STREAMING_FLOW" if "STREAMING_FLOW" in flow_modes else "BATCH_FLOW", "solver_status": "feasible" if not blocks else "blocked", "operations": enriched, "resource_intervals": intervals, "blocks": blocks, "metrics": {"operation_count": len(enriched), "makespan_minutes": makespan, "processing_minutes": round(processing, 2), "setup_minutes": round(setup, 2), "wip_deferred_count": sum(1 for e in bundle["supply_snapshot"].get("entries", []) if e.get("readiness") == "NOT_READY" and e.get("earliest_ready_at")), "batch_operation_count": sum(1 for op in enriched if op.get("batch_index") is not None)}, "validation_report": {"status": "pass" if not blocks else "fail", "errors": blocks}}
    # The scheduler is the source of plan times; the WIP projector adds the
    # station/worker/buffer facts required by the standalone WIP PMC package.
    # A production plan is blocked when those bindings are absent or marked
    # test-only, but the full inspection artifact is still returned.
    wip_requested = bool(payload.get("wip_pmc") or payload.get("wip_pmc_mode") or payload.get("scenario_purpose") == "wip_pmc")
    wip = project_wip_pmc(
        schedule,
        {**bundle["resource_snapshot"], "calendar_snapshot": bundle["calendar_snapshot"]},
        source_ref="wip-pmc-reuse-20260901",
        production_allowed=payload.get("production_use_allowed", True),
        require_bindings=wip_requested,
    )
    wip_blocks = []
    if wip_requested and wip["summary_metrics"].get("missing_station_bindings"):
        wip_blocks.append({"reason_code": "MISSING_STATION_BINDING", "reason": "WIP PMC 需要逐工序真实工位绑定；当前输入缺少工位编码"})
    if wip_requested and wip["summary_metrics"].get("missing_worker_bindings"):
        wip_blocks.append({"reason_code": "MISSING_WORKER_BINDING", "reason": "WIP PMC 需要逐工序人员绑定；当前输入缺少人员编码"})
    blocks.extend(wip_blocks)
    schedule["solver_status"] = "feasible" if not blocks else "blocked"
    schedule["validation_report"] = {"status": "pass" if not blocks else "fail", "errors": blocks}
    schedule.update({
        "wip_version": "wip-pmc-reuse-20260901",
        "production_blocked": bool(blocks) or wip["production_blocked"],
        "state_segments": wip["state_segments"],
        "wip_segments": wip["wip_segments"],
        "wip_edges": wip["wip_edges"],
        "edge_wip_statistics": wip["edge_wip_statistics"],
        "worker_utilization": wip["worker_utilization"],
        "station_utilization": wip["station_utilization"],
        "bottleneck_facts": wip["bottleneck_facts"],
        "summary_metrics": wip["summary_metrics"],
        "wip_source_ref": wip["source_ref"],
    })
    return {"success": not blocks, "data": {"idempotency_key": payload.get("idempotency_key", ""), "schedule": schedule, "scenario_purpose": payload.get("scenario_purpose", "production"), "lifecycle_status": "draft", "input_hash": digest, "algorithm_version": "pmc-v2-frozen-20260902", "input_package": {"order_snapshots": bundle["order_snapshots"], "routes": bundle["routes"], "resource_snapshot": bundle["resource_snapshot"], "calendar_snapshot": bundle["calendar_snapshot"], "supply_snapshot": bundle["supply_snapshot"], "constraint_snapshot": bundle["constraint_snapshot"]}, "validator": schedule["validation_report"], "blocks": blocks, "wip": bundle["supply_snapshot"].get("entries", []), "wip_pmc": wip}, "errors": [{"code": "BLOCKED_INPUT", "message": b.get("reason", "") , "details": [b]} for b in blocks], "trace_id": f"m5:pmc-v2:{digest[:12]}", "evidence": [{"module": "m5", "source_ref": "pmc_v2_frozen", "evidence_ref": "pmc-v2-frozen-20260902", "detail": "WIP/工时/产能/换型/日历约束求解"}, {"module": "m5", "source_ref": "wip-pmc-reuse-20260901", "evidence_ref": "wip-pmc-reuse-20260901", "detail": "工位状态段、工位间 WIP、人工利用率投影"}]}
