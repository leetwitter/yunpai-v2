"""Project a constrained PMC schedule into the WIP PMC workspace contract.

The frozen scheduler places operations and reserves resources.  This module
keeps that result intact and derives the display/analysis facts required by
the WIP PMC package: station state segments, inter-operation WIP buffers, and
worker/station utilization.  It never invents production resources; missing
worker or station bindings are surfaced as explicit production blockers.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

TZ = timezone(timedelta(hours=8))


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=TZ)


def _iso(value: datetime) -> str:
    return value.astimezone(TZ).isoformat(timespec="seconds")


def _minutes(start: datetime | None, end: datetime | None) -> int:
    if not start or not end:
        return 0
    return max(0, int(round((end - start).total_seconds() / 60)))


def _name(item: dict[str, Any], *keys: str, default: str) -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return default


def _calendar_minutes(resource: dict[str, Any], intervals: list[dict[str, Any]],
                      horizon_start: datetime, horizon_end: datetime) -> int:
    ref = resource.get("calendar_ref")
    total = 0
    for interval in intervals:
        if ref and interval.get("calendar_ref") not in (None, ref):
            continue
        start = max(horizon_start, _dt(interval.get("start_at")) or horizon_start)
        end = min(horizon_end, _dt(interval.get("end_at")) or horizon_end)
        total += _minutes(start, end)
    return total


def _project_streaming_pmc(schedule: dict[str, Any], resources: dict[str, Any], *,
                           source_ref: str, production_allowed: bool,
                           require_bindings: bool) -> dict[str, Any]:
    operations = [item for item in schedule.get("operations", []) if isinstance(item, dict)]
    parsed = [(_dt(item.get("plan_start")), _dt(item.get("plan_end")), item) for item in operations]
    valid = [(s, e, item) for s, e, item in parsed if s and e and e > s]
    if not valid:
        return {"production_blocked": True, "production_use_allowed": False, "source_ref": source_ref,
                "state_segments": [], "wip_segments": [], "worker_utilization": [],
                "station_utilization": [], "wip_edges": [], "edge_wip_statistics": [],
                "summary_metrics": {"average_wip_wait_minutes": 0, "selected_worker_count": 0},
                "bottleneck_facts": []}
    horizon_start, horizon_end = min(x[0] for x in valid), max(x[1] for x in valid)
    intervals = []
    calendar = resources.get("calendar_snapshot")
    if isinstance(calendar, dict):
        intervals = [x for x in calendar.get("working_intervals", []) if isinstance(x, dict)]
    intervals = intervals or [x for x in resources.get("working_intervals", []) if isinstance(x, dict)]
    stations = {str(x.get("station_code")): x for x in resources.get("stations", []) if x.get("station_code")}
    persons = {str(x.get("person_code")): x for x in resources.get("persons", []) if x.get("person_code")}
    equipment = {str(x.get("equipment_code")): x for x in resources.get("equipment", []) if x.get("equipment_code")}
    missing_station = missing_worker = 0
    state_segments, station_totals, worker_totals = [], {}, {}
    by_line = defaultdict(list)
    for start, end, item in sorted(valid, key=lambda row: (row[0], str(row[2].get("schedule_operation_id", "")))):
        station_id, worker_id, machine_id = str(item.get("station_code") or ""), str(item.get("person_code") or ""), str(item.get("equipment_code") or "")
        if not station_id:
            missing_station += 1; station_id = f"UNBOUND-STATION-{item.get('op_code', 'UNKNOWN')}"
        if not worker_id:
            missing_worker += 1; worker_id = f"UNBOUND-WORKER-{item.get('op_code', 'UNKNOWN')}"
        station_name = _name(stations.get(station_id, {}), "station_name", "name", default=station_id)
        worker_name = _name(persons.get(worker_id, {}), "person_name", "name", default=worker_id)
        machine_name = _name(equipment.get(machine_id, {}), "equipment_type", "name", default=machine_id or "未绑定机器")
        setup_start = start + timedelta(minutes=int(item.get("setup_minutes") or 0))
        setup_start = min(setup_start, end)
        op_id = str(item.get("op_code") or item.get("schedule_operation_id") or "UNKNOWN")
        batch = item.get("batch_index")
        label = f"{op_id}/B{int(batch):02d}" if batch is not None else op_id
        base = {"operation_id": label, "order_line_id": str(item.get("order_line_id") or "UNKNOWN"),
                "station_id": station_id, "station_name": station_name, "worker_id": worker_id,
                "worker_name": worker_name, "machine_id": machine_id or None, "machine_name": machine_name}
        state_segments.append({**base, "start": _iso(setup_start), "end": _iso(end), "duration_minutes": _minutes(setup_start, end), "state": "active", "reason_code": "streaming_batch"})
        if setup_start > start:
            state_segments.append({**base, "start": _iso(start), "end": _iso(setup_start), "duration_minutes": _minutes(start, setup_start), "state": "idle", "reason_code": "setup"})
        by_line[base["order_line_id"]].append((start, end, item, setup_start))
        station_totals.setdefault(station_id, {"station_id": station_id, "station_name": station_name, "assigned_minutes": 0})["assigned_minutes"] += _minutes(setup_start, end)
        worker_totals.setdefault(worker_id, {"worker_id": worker_id, "worker_name": worker_name, "station_id": station_id, "assigned_minutes": 0})["assigned_minutes"] += _minutes(setup_start, end)

    # Add starvation segments from predecessor batch completion to this batch's processing start.
    by_sid = {str(x.get("schedule_operation_id")): x for x in operations}
    for _, line_ops in by_line.items():
        for start, end, item, processing_start in line_ops:
            pred_ends = [_dt(by_sid[p].get("plan_end")) for p in item.get("predecessors", []) if p in by_sid]
            if pred_ends:
                ready = max(pred_ends)
                if ready < processing_start:
                    station_id = str(item.get("station_code") or f"UNBOUND-STATION-{item.get('op_code', 'UNKNOWN')}")
                    worker_id = str(item.get("person_code") or f"UNBOUND-WORKER-{item.get('op_code', 'UNKNOWN')}")
                    state_segments.append({"operation_id": f"{item.get('op_code')}/B{int(item.get('batch_index', 1)):02d}", "order_line_id": str(item.get("order_line_id")), "station_id": station_id, "worker_id": worker_id, "start": _iso(ready), "end": _iso(processing_start), "duration_minutes": _minutes(ready, processing_start), "state": "starved", "reason_code": "waiting_upstream_batch"})

    wip_segments, wip_edges, edge_stats = [], [], []
    for line_id, line_ops in by_line.items():
        by_seq = defaultdict(list)
        for row in line_ops:
            by_seq[int(row[2].get("sequence_no") or 0)].append(row)
        sequences = sorted(by_seq)
        for seq in sequences[:-1]:
            upstream = sorted(by_seq[seq], key=lambda row: int(row[2].get("batch_index") or 0))
            downstream = {int(row[2].get("batch_index") or 0): row for row in by_seq[seq + 1]}
            if not downstream:
                continue
            edge_id = f"EDGE-{line_id}-{upstream[0][2].get('op_code')}-{next(iter(downstream.values()))[2].get('op_code')}"
            times = {up[3] for up in upstream} | {up[1] for up in upstream}
            for row in downstream.values():
                times |= {row[3], row[1]}
            times = sorted(t for t in times if t)
            quantity = 0.0; segments = []
            for a, b in zip(times, times[1:]):
                minutes = max(1, _minutes(a, b))
                rate_in = rate_out = 0.0
                for _, end, op, processing_start in upstream:
                    if processing_start <= a < end:
                        rate_in += float(op.get("qty") or 0) / max(1, _minutes(processing_start, end))
                for _, end, op, processing_start in downstream.values():
                    if processing_start <= a < end:
                        rate_out += float(op.get("qty") or 0) / max(1, _minutes(processing_start, end))
                delta = rate_in - rate_out
                end_qty = max(0.0, quantity + delta * minutes)
                if abs(delta) > 1e-9 or end_qty > 1e-9 or quantity > 1e-9:
                    segments.append({"edge_id": edge_id, "production_order_id": line_id, "start": _iso(a), "end": _iso(b), "duration_minutes": minutes, "start_quantity": round(quantity, 6), "end_quantity": round(end_qty, 6), "delta_per_minute": round(delta, 6)})
                quantity = end_qty
            wip_segments.extend(segments)
            # Positive buffer inventory while the upstream batch is producing
            # is visible as downstream back-pressure on the upstream station.
            for segment in segments:
                if float(segment.get("end_quantity") or 0) <= 0:
                    continue
                source = next((row for row in upstream if row[3] <= _dt(segment["start"]) < row[1]), None)
                if source is None:
                    source = upstream[0]
                source_item = source[2]
                state_segments.append({"operation_id": f"{source_item.get('op_code')}/B{int(source_item.get('batch_index', 1)):02d}", "order_line_id": line_id, "station_id": source_item.get("station_code") or f"UNBOUND-STATION-{source_item.get('op_code')}", "worker_id": source_item.get("person_code") or f"UNBOUND-WORKER-{source_item.get('op_code')}", "start": segment["start"], "end": segment["end"], "duration_minutes": segment["duration_minutes"], "state": "blocked", "reason_code": "downstream_backpressure"})
            max_qty = max((float(s["end_quantity"]) for s in segments), default=0.0)
            avg_wait = round(sum(s["duration_minutes"] for s in segments if s["end_quantity"] > 0) / max(1, sum(1 for s in segments if s["end_quantity"] > 0)), 6)
            first_up, first_down = upstream[0][2], next(iter(downstream.values()))[2]
            edge = {"edge_id": edge_id, "from_operation_id": first_up.get("op_code"), "to_operation_id": first_down.get("op_code"), "from_station_id": first_up.get("station_code"), "to_station_id": first_down.get("station_code"), "target_wip_minutes": max(1, avg_wait), "current_wip_minutes": avg_wait, "accumulation_allowed": True, "segments": segments, "production_order_id": line_id}
            wip_edges.append(edge)
            edge_stats.append({"edge_id": edge_id, "production_order_id": line_id, "average_wip_time_minutes": avg_wait, "maximum_quantity": round(max_qty, 6), "initial_quantity": 0, "final_quantity": round(quantity, 6)})

    worker_utilization = []
    for worker_id, worker in sorted(worker_totals.items()):
        available = _calendar_minutes(persons.get(worker_id, {}), intervals, horizon_start, horizon_end) or _minutes(horizon_start, horizon_end)
        assigned = int(worker["assigned_minutes"])
        worker_utilization.append({**worker, "available_minutes": available, "productive_minutes": assigned, "allocated_utilization": round(assigned / available, 6) if available else 0, "planned_occupancy_rate": round(assigned / available, 6) if available else 0, "effective_running_rate": 1 if assigned else 0})
    station_utilization = []
    horizon_minutes = max(1, _minutes(horizon_start, horizon_end))
    for station_id, station in sorted(station_totals.items()):
        capacity = max(1, int(stations.get(station_id, {}).get("parallel_slots") or 1))
        station_utilization.append({**station, "available_minutes": horizon_minutes * capacity, "utilization": round(station["assigned_minutes"] / (horizon_minutes * capacity), 6)})
    average_wip = round(sum(x["average_wip_time_minutes"] for x in edge_stats) / len(edge_stats), 6) if edge_stats else 0
    blocked = ((missing_station > 0 or missing_worker > 0) if require_bindings else False) or not production_allowed
    return {"production_blocked": blocked, "production_use_allowed": not blocked, "source_ref": source_ref, "horizon_start": _iso(horizon_start), "horizon_end": _iso(horizon_end), "state_segments": state_segments, "wip_segments": wip_segments, "wip_edges": wip_edges, "edge_wip_statistics": edge_stats, "worker_utilization": worker_utilization, "station_utilization": station_utilization, "bottleneck_facts": sorted(({"station_id": x["station_id"], "station_name": x["station_name"], "utilization": x["utilization"], "fact": "station_utilization"} for x in station_utilization), key=lambda x: (-x["utilization"], x["station_id"]))[:5], "summary_metrics": {"average_wip_wait_minutes": average_wip, "wip_edge_count": len(wip_edges), "selected_worker_count": len(worker_utilization), "selected_worker_allocated_minutes": sum(x["assigned_minutes"] for x in worker_utilization), "selected_worker_available_minutes": sum(x["available_minutes"] for x in worker_utilization), "selected_worker_productive_minutes": sum(x["productive_minutes"] for x in worker_utilization), "selected_worker_average_planned_occupancy_rate": round(sum(x["planned_occupancy_rate"] for x in worker_utilization) / len(worker_utilization), 6) if worker_utilization else 0, "selected_worker_average_effective_running_rate": 1 if worker_utilization else 0, "missing_station_bindings": missing_station, "missing_worker_bindings": missing_worker}}


def project_wip_pmc(schedule: dict[str, Any], resource_snapshot: dict[str, Any] | None = None,
                    *, source_ref: str = "pmc-v2", production_allowed: bool = True,
                    require_bindings: bool = False) -> dict[str, Any]:
    """Return WIP PMC facts derived from a scheduled operation list.

    The function is deterministic and deliberately conservative.  A schedule
    with no explicit station/person binding remains useful for inspection, but
    is marked ``production_blocked`` and carries a machine-as-fallback label
    only for visualization.
    """
    resources = resource_snapshot or {}
    operations = [item for item in schedule.get("operations", []) if isinstance(item, dict)]
    if any(item.get("flow_mode") == "STREAMING_FLOW" or item.get("batch_index") is not None for item in operations):
        return _project_streaming_pmc(schedule, resources, source_ref=source_ref,
                                      production_allowed=production_allowed,
                                      require_bindings=require_bindings)
    parsed = [(_dt(item.get("plan_start")), _dt(item.get("plan_end")), item) for item in operations]
    valid = [(start, end, item) for start, end, item in parsed if start and end and end > start]
    if not valid:
        return {
            "production_blocked": True,
            "production_use_allowed": False,
            "source_ref": source_ref,
            "state_segments": [], "wip_segments": [], "worker_utilization": [],
            "station_utilization": [], "wip_edges": [],
            "summary_metrics": {"average_wip_wait_minutes": 0, "selected_worker_count": 0},
            "bottleneck_facts": [],
        }

    horizon_start = min(start for start, _, _ in valid)
    horizon_end = max(end for _, end, _ in valid)
    intervals = [item for item in (resources.get("calendar_snapshot", {}).get("working_intervals", [])
                                   if isinstance(resources.get("calendar_snapshot"), dict) else [])
                 if isinstance(item, dict)]
    if not intervals:
        intervals = [item for item in (resources.get("working_intervals") or []) if isinstance(item, dict)]

    stations = {str(item.get("station_code")): item for item in resources.get("stations", [])
                if isinstance(item, dict) and item.get("station_code")}
    persons = {str(item.get("person_code")): item for item in resources.get("persons", [])
               if isinstance(item, dict) and item.get("person_code")}
    equipment = {str(item.get("equipment_code")): item for item in resources.get("equipment", [])
                 if isinstance(item, dict) and item.get("equipment_code")}

    missing_station = 0
    missing_worker = 0
    state_segments: list[dict[str, Any]] = []
    by_line: dict[str, list[tuple[datetime, datetime, dict[str, Any]]]] = defaultdict(list)
    station_totals: dict[str, dict[str, Any]] = {}
    worker_totals: dict[str, dict[str, Any]] = {}

    for start, end, item in sorted(valid, key=lambda row: (row[0], str(row[2].get("schedule_operation_id", "")))):
        station_id = str(item.get("station_code") or "")
        worker_id = str(item.get("person_code") or "")
        machine_id = str(item.get("equipment_code") or "")
        if not station_id:
            missing_station += 1
            station_id = f"UNBOUND-STATION-{item.get('op_code', 'UNKNOWN')}"
        if not worker_id:
            missing_worker += 1
            worker_id = f"UNBOUND-WORKER-{item.get('op_code', 'UNKNOWN')}"
        station_name = _name(stations.get(station_id, {}), "station_name", "name", default=station_id)
        worker_name = _name(persons.get(worker_id, {}), "person_name", "name", default=worker_id)
        machine_name = _name(equipment.get(machine_id, {}), "equipment_type", "name", default=machine_id or "未绑定机器")
        processing_start = start + timedelta(minutes=int(item.get("setup_minutes") or 0))
        processing_start = min(processing_start, end)
        duration = _minutes(processing_start, end)
        op_id = str(item.get("op_code") or item.get("schedule_operation_id") or "UNKNOWN")
        order_line_id = str(item.get("order_line_id") or item.get("order_id") or "UNKNOWN")
        state_segments.append({
            "operation_id": op_id, "order_line_id": order_line_id,
            "station_id": station_id, "station_name": station_name,
            "worker_id": worker_id, "worker_name": worker_name,
            "machine_id": machine_id or None, "machine_name": machine_name,
            "start": _iso(processing_start), "end": _iso(end),
            "duration_minutes": duration, "state": "active",
            "reason_code": "scheduled_operation",
        })
        if processing_start > start:
            state_segments.append({
                "operation_id": op_id, "order_line_id": order_line_id,
                "station_id": station_id, "station_name": station_name,
                "worker_id": worker_id, "worker_name": worker_name,
                "machine_id": machine_id or None, "machine_name": machine_name,
                "start": _iso(start), "end": _iso(processing_start),
                "duration_minutes": _minutes(start, processing_start), "state": "idle",
                "reason_code": "setup",
            })
        by_line[order_line_id].append((start, end, item))
        station = station_totals.setdefault(station_id, {"station_id": station_id, "station_name": station_name, "assigned_minutes": 0})
        station["assigned_minutes"] += duration
        worker = worker_totals.setdefault(worker_id, {"worker_id": worker_id, "worker_name": worker_name, "station_id": station_id, "assigned_minutes": 0})
        worker["assigned_minutes"] += duration

    wip_segments: list[dict[str, Any]] = []
    wip_edges: list[dict[str, Any]] = []
    edge_stats: list[dict[str, Any]] = []
    for order_line_id, line_ops in by_line.items():
        line_ops.sort(key=lambda row: (int(row[2].get("sequence_no") or 0), row[0]))
        for index, (prev_start, prev_end, prev) in enumerate(line_ops[:-1]):
            next_start, _, nxt = line_ops[index + 1]
            wait = _minutes(prev_end, next_start)
            from_op = str(prev.get("op_code") or prev.get("schedule_operation_id") or "UNKNOWN")
            to_op = str(nxt.get("op_code") or nxt.get("schedule_operation_id") or "UNKNOWN")
            edge_id = f"EDGE-{order_line_id}-{from_op}-{to_op}"
            segment_state = "low" if wait == 0 else "normal"
            segment = {"edge_id": edge_id, "production_order_id": order_line_id,
                       "start": _iso(prev_end), "end": _iso(next_start),
                       "duration_minutes": wait, "start_quantity": 0,
                       "end_quantity": 0, "state": segment_state}
            wip_segments.append(segment)
            edge = {"edge_id": edge_id, "from_operation_id": from_op,
                    "to_operation_id": to_op, "from_station_id": prev.get("station_code") or prev.get("equipment_code"),
                    "to_station_id": nxt.get("station_code") or nxt.get("equipment_code"),
                    "target_wip_minutes": max(1, wait), "current_wip_minutes": wait,
                    "accumulation_allowed": True, "segments": [segment],
                    "production_order_id": order_line_id}
            wip_edges.append(edge)
            edge_stats.append({"edge_id": edge_id, "production_order_id": order_line_id,
                               "average_wip_time_minutes": wait, "maximum_quantity": 0,
                               "initial_quantity": 0, "final_quantity": 0})

    worker_utilization: list[dict[str, Any]] = []
    for worker_id, worker in sorted(worker_totals.items()):
        resource = persons.get(worker_id, {})
        available = _calendar_minutes(resource, intervals, horizon_start, horizon_end) if resource else 0
        if not available:
            available = _minutes(horizon_start, horizon_end)
        assigned = int(worker["assigned_minutes"])
        worker_utilization.append({**worker, "available_minutes": available,
                                   "productive_minutes": assigned,
                                   "allocated_utilization": round(assigned / available, 6) if available else 0,
                                   "planned_occupancy_rate": round(assigned / available, 6) if available else 0,
                                   "effective_running_rate": 1 if assigned else 0})
    for worker_id, resource in sorted(persons.items()):
        if worker_id in worker_totals:
            continue
        available = _calendar_minutes(resource, intervals, horizon_start, horizon_end) or _minutes(horizon_start, horizon_end)
        worker_utilization.append({"worker_id": worker_id, "worker_name": _name(resource, "person_name", "name", default=worker_id),
                                   "station_id": resource.get("station_code") or resource.get("station_id"),
                                   "assigned_minutes": 0, "available_minutes": available, "productive_minutes": 0,
                                   "allocated_utilization": 0, "planned_occupancy_rate": 0, "effective_running_rate": 0})

    station_utilization = []
    horizon_minutes = max(1, _minutes(horizon_start, horizon_end))
    for station_id, station in sorted(station_totals.items()):
        capacity = max(1, int(stations.get(station_id, {}).get("parallel_slots") or 1))
        available = horizon_minutes * capacity
        station_utilization.append({**station, "available_minutes": available,
                                    "utilization": round(station["assigned_minutes"] / available, 6)})

    average_wip = round(sum(item["average_wip_time_minutes"] for item in edge_stats) / len(edge_stats), 6) if edge_stats else 0
    bottlenecks = sorted(({
        "station_id": item["station_id"], "station_name": item["station_name"],
        "utilization": item["utilization"], "fact": "station_utilization",
    } for item in station_utilization), key=lambda item: (-item["utilization"], item["station_id"]))[:5]
    blocked = ((missing_station > 0 or missing_worker > 0) if require_bindings else False) or not production_allowed
    return {
        "production_blocked": blocked,
        "production_use_allowed": not blocked,
        "source_ref": source_ref,
        "horizon_start": _iso(horizon_start), "horizon_end": _iso(horizon_end),
        "state_segments": state_segments, "wip_segments": wip_segments,
        "wip_edges": wip_edges, "edge_wip_statistics": edge_stats,
        "worker_utilization": worker_utilization, "station_utilization": station_utilization,
        "bottleneck_facts": bottlenecks,
        "summary_metrics": {
            "average_wip_wait_minutes": average_wip,
            "wip_edge_count": len(wip_edges),
            "selected_worker_count": len(worker_utilization),
            "selected_worker_allocated_minutes": sum(item["assigned_minutes"] for item in worker_utilization),
            "selected_worker_available_minutes": sum(item["available_minutes"] for item in worker_utilization),
            "selected_worker_productive_minutes": sum(item["productive_minutes"] for item in worker_utilization),
            "selected_worker_average_planned_occupancy_rate": round(sum(item["planned_occupancy_rate"] for item in worker_utilization) / len(worker_utilization), 6) if worker_utilization else 0,
            "selected_worker_average_effective_running_rate": round(sum(item["effective_running_rate"] for item in worker_utilization) / len(worker_utilization), 6) if worker_utilization else 0,
            "missing_station_bindings": missing_station,
            "missing_worker_bindings": missing_worker,
        },
    }
