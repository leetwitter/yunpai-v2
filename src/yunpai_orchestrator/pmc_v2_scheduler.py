#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PMC v2 constrained multi-resource scheduler (0902 frozen algorithm).

Working model v1 (documented assumptions):
  * Time is integer minutes over half-open intervals.  Working windows come
    exclusively from the calendar snapshot (calendar_ref per resource); a
    closed minute is never implicitly available.
  * A scheduled operation needs ``processing`` *working* minutes and may span
    multiple calendar windows: the gaps between working windows (lunch,
    overnight, weekend) are non-working time and simply extend the wall span.
    It is BLOCKED_INPUT NO_FEASIBLE_WINDOW only when the calendar truly runs
    out of working time before the feasible horizon.
  * Processing minutes = standard_minutes x (qty / quantity_basis) scaled by
    the assigned equipment's real capacity_per_hour x efficiency_factor
    (reference = 60/h at efficiency 1; a 120/h machine halves the minutes).
  * Equipment setup/changeover occupies the equipment only (before
    processing); changeover minutes follow the constraint setup_matrix for
    family switches, else the operation's declared setup_minutes.
  * Persons are single-task (max_parallel_tasks=1) and must satisfy the
    operation's skill / qualified-operation requirements; equipment, tooling
    pools and stations are bounded by quantity_available / parallel_slots.
  * Machine/person/tooling/station for one operation are reserved as one
    combination at the same wall time (simultaneous allocation).
  * Supply NOT_READY with earliest_ready_at defers the operation; NOT_READY
    without a date blocks the operation (block record + validator FAIL).

Determinism: all choices are tie-broken by fixed input order / codes, so equal
input snapshots + equal idempotency key yield byte-identical candidates.
"""

from decimal import Decimal, ROUND_CEILING
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pmc_v2_snapshots import (PmcError, BLOCKED,
                              parse_dt, to_minutes, from_minutes, to_iso,
                              validate_bundle)


def _dec(x):
    return Decimal(str(x))


def _ceil_dec(d):
    return int(d.to_integral_value(rounding=ROUND_CEILING))


# ---------------------------------------------------------------------------
# interval helpers (integer minutes, half-open [a, b))
# ---------------------------------------------------------------------------

def _merge(segs):
    segs = sorted((a, b) for a, b in segs if b > a)
    out = []
    for a, b in segs:
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _subtract(segs, drops):
    segs = _merge(segs)
    drops = _merge(drops)
    out = []
    di = 0
    for a, b in segs:
        cur = a
        while di < len(drops) and drops[di][1] <= cur:
            di += 1
        j = di
        while j < len(drops) and drops[j][0] < b:
            da, db_ = drops[j]
            if da > cur:
                out.append((cur, min(da, b)))
            cur = max(cur, db_)
            j += 1
        if cur < b:
            out.append((cur, b))
        di = j
    return out


def _intersect_many(lists_of_segs):
    """Intersection of sorted disjoint segment lists (half-open minutes)."""
    if not lists_of_segs:
        return []
    result = _merge(lists_of_segs[0])
    for segs in lists_of_segs[1:]:
        if not result:
            return []
        nxt = []
        i = j = 0
        segs = _merge(segs)
        while i < len(result) and j < len(segs):
            a1, b1 = result[i]
            a2, b2 = segs[j]
            a = max(a1, a2)
            b = min(b1, b2)
            if b > a:
                nxt.append((a, b))
            if b1 <= b2:
                i += 1
            else:
                j += 1
        result = nxt
    return result


def _working_end(segs, start, need):
    """Wall-clock end minute after consuming `need` working minutes from `start`
    across sorted disjoint segments; None when the total working time from
    `start` onward is less than `need`."""
    if need <= 0:
        return start
    acc = 0
    for a, b in segs:
        if b <= start:
            continue
        s = max(a, start)
        if acc + (b - s) >= need:
            return s + (need - acc)
        acc += b - s
    return None


def _working_back(segs, end, need):
    """Wall-clock start minute `need` working minutes before `end` across sorted
    disjoint segments; None when the working time before `end` is < `need`."""
    if need <= 0:
        return end
    acc = 0
    for a, b in reversed(segs):
        if a >= end:
            continue
        e = min(b, end)
        if acc + (e - a) >= need:
            return e - (need - acc)
        acc += e - a
    return None


def _earliest_span(segs, min_start, need):
    """Earliest (start, end) wall interval covering `need` working minutes across
    sorted disjoint segments, with start >= min_start; None when infeasible."""
    if need <= 0:
        return (min_start, min_start)
    for a, b in segs:
        if b <= min_start:
            continue
        start = max(a, min_start)
        end = _working_end(segs, start, need)
        if end is not None:
            return (start, end)
    return None


def _saturated_from_events(events, cap):
    """Minutes where concurrent allocations reach `cap` -> drop segments."""
    if not events:
        return []
    drops = []
    active = 0
    starts = sorted(events.items())
    for idx, (t, delta) in enumerate(starts):
        active += delta
        if active >= cap:
            nxt = starts[idx + 1][0] if idx + 1 < len(starts) else t
            if nxt > t:
                drops.append((t, nxt))
    return _merge(drops)


# ---------------------------------------------------------------------------
# scheduling context
# ---------------------------------------------------------------------------

class _Resource:
    __slots__ = ("kind", "code", "cap", "calendar_ref", "windows", "ua", "allocs")

    def __init__(self, kind, code, cap, calendar_ref):
        self.kind = kind
        self.code = code
        self.cap = cap
        self.calendar_ref = calendar_ref
        self.windows = []
        self.ua = []
        self.allocs = []  # (start_min, end_min, schedule_operation_id)

    def free_segments(self):
        segs = _subtract(self.windows, self.ua)
        if self.cap == 1:
            occ = _merge([(a, b) for a, b, _, _ in self.allocs])
            return _subtract(segs, occ)
        if not self.allocs:
            return segs
        events = {}
        for a, b, _, _ in self.allocs:
            events[a] = events.get(a, 0) + 1
            events[b] = events.get(b, 0) - 1
        return _subtract(segs, _saturated_from_events(events, self.cap))


class RunContext:
    def __init__(self, bundle):
        self.bundle = bundle
        self.calendar = bundle["calendar_snapshot"]
        self.resource = bundle["resource_snapshot"]
        self.supply = bundle["supply_snapshot"]
        self.constraint = bundle["constraint_snapshot"]
        windows_by_ref = {}
        for iv in self.calendar["working_intervals"]:
            seg = (to_minutes(parse_dt(iv["start_at"])), to_minutes(parse_dt(iv["end_at"])))
            windows_by_ref.setdefault(iv["calendar_ref"], []).append(seg)
        self.windows_by_ref = {ref: _merge(segs) for ref, segs in windows_by_ref.items()}
        ua_by = {}
        for item in self.calendar["unavailability"]:
            seg = (to_minutes(parse_dt(item["start_at"])), to_minutes(parse_dt(item["end_at"])))
            ua_by.setdefault((item["resource_type"], item["resource_code"]), []).append(seg)
        self.ua_by = {key: _merge(segs) for key, segs in ua_by.items()}
        self._by_code = {}

    def resource_for(self, kind, code):
        key = (kind, code)
        if key in self._by_code:
            return self._by_code[key]
        entry = self._entry(kind, code)
        if kind == "EQUIPMENT":
            cap = 1
            cref = entry["calendar_ref"]
        elif kind == "PERSON":
            cap = 1
            cref = entry["calendar_ref"]
        elif kind == "TOOLING":
            cap = entry["quantity_available"]
            cref = entry["calendar_ref"]
        else:  # STATION
            cap = entry["parallel_slots"]
            cref = entry["calendar_ref"]
        res = _Resource(kind, code, cap, cref)
        res.windows = list(self.windows_by_ref.get(cref, []))
        res.ua = list(self.ua_by.get(key, []))
        self._by_code[key] = res
        return res

    def _entry(self, kind, code):
        section = {"EQUIPMENT": "equipment", "PERSON": "persons",
                   "TOOLING": "tooling", "STATION": "stations"}[kind]
        for item in self.resource.get(section, []):
            code_key = {"EQUIPMENT": "equipment_code", "PERSON": "person_code",
                        "TOOLING": "tooling_code", "STATION": "station_code"}[kind]
            if item[code_key] == code:
                return item
        raise PmcError(BLOCKED, "MISSING_%s resource_code=%s not in resource_snapshot"
                       % (kind, code))

    def schedule_id(self, order_line_id, op_code, seq):
        return "%s::%s#%d" % (order_line_id, op_code, seq)


# ---------------------------------------------------------------------------
# eligibility / combo / duration / setup
# ---------------------------------------------------------------------------

def _eligible_equipment(ctx, op):
    req_codes = op.get("required_equipment_codes") or []
    req_caps = op.get("required_equipment_capabilities") or []
    if not req_codes and not req_caps:
        return [None]
    cand = []
    for eq in ctx.resource.get("equipment", []):
        if eq["status"] != "ACTIVE":
            continue
        if req_codes and eq["equipment_code"] not in req_codes:
            continue
        if req_caps and not set(req_caps).issubset(set(eq.get("capability_codes") or [])):
            continue
        cand.append(eq)
    if not cand:
        raise PmcError(BLOCKED,
                       "NO_ELIGIBLE_EQUIPMENT op_code=%s required_codes=%s required_capabilities=%s"
                       % (op["op_code"], req_codes, req_caps))
    return cand


def _eligible_persons(ctx, op):
    req_codes = op.get("required_person_codes") or []
    req_skills = op.get("required_skill_codes") or []
    if not req_codes and not req_skills:
        return [None]
    cand = []
    for person in ctx.resource.get("persons", []):
        if person["status"] != "ACTIVE":
            continue
        if person.get("max_parallel_tasks") != 1:
            raise PmcError(BLOCKED, "INVALID_VALUE person %s max_parallel_tasks must be 1"
                           % person["person_code"])
        if req_codes and person["person_code"] not in req_codes:
            continue
        if req_skills and not set(req_skills).issubset(set(person.get("skill_codes") or [])):
            continue
        qualified = person.get("qualified_operation_codes") or []
        if qualified and op["op_code"] not in qualified:
            continue
        cand.append(person)
    if not cand:
        raise PmcError(BLOCKED,
                       "NO_ELIGIBLE_PERSON op_code=%s required_codes=%s required_skills=%s"
                       % (op["op_code"], req_codes, req_skills))
    return cand


def _eligible_stations(ctx, op):
    req_codes = op.get("required_station_codes") or []
    if not req_codes:
        return [None]
    cand = []
    for station in ctx.resource.get("stations", []):
        if station["status"] != "ACTIVE":
            continue
        if station["station_code"] in req_codes:
            cand.append(station)
    if not cand:
        raise PmcError(BLOCKED, "NO_ELIGIBLE_STATION op_code=%s required_codes=%s"
                       % (op["op_code"], req_codes))
    return cand


def _eligible_tooling(ctx, op):
    req_codes = op.get("required_tooling_codes") or []
    pools = []
    for code in req_codes:
        if code in pools:
            continue
        pool = None
        for tl in ctx.resource.get("tooling", []):
            if tl["tooling_code"] == code:
                pool = tl
                break
        if pool is None or pool["status"] != "ACTIVE":
            raise PmcError(BLOCKED, "NO_ELIGIBLE_TOOLING op_code=%s tooling_code=%s"
                           % (op["op_code"], code))
        pools.append(pool)
    return pools


def _processing_minutes(op, qty, equipment):
    basis = _dec(op["quantity_basis"])
    std = _dec(op["standard_minutes"])
    # Yield loss increases the gross quantity that must enter the operation.
    # The route snapshot carries the explicit yield rate; never hide station
    # loss in a post-hoc KPI.
    yield_rate = _dec(op.get("yield_rate") or "1")
    if yield_rate <= 0 or yield_rate > 1:
        raise PmcError(BLOCKED, "INVALID_VALUE yield_rate must be > 0 and <= 1")
    effective_qty = _dec(qty) / yield_rate
    if equipment is not None:
        cap = _dec(equipment["capacity_per_hour"])
        eff = _dec(equipment["efficiency_factor"])
        return _ceil_dec((effective_qty / basis) * std * (_dec("60") / (cap * eff)))
    # An operation without a bound equipment runs on its explicit standard
    # minutes; no implicit 60/h reference capacity or efficiency 1 is assumed
    # (0902 fail-closed contract).
    return _ceil_dec((effective_qty / basis) * std)


def _setup_minutes_for(ctx, op, equipment, prev_op):
    """Applied setup/changeover minutes for one operation on its equipment."""
    declared = op.get("setup_minutes") or 0
    if equipment is None:
        # no changeover concept without equipment; the declared setup applies
        # as operation lead time (no resource occupancy).
        return declared
    if prev_op is None:
        return declared
    same_family = (op.get("setup_family") is not None
                   and op.get("setup_family") == prev_op.get("setup_family"))
    same_product = op.get("product_code") == prev_op.get("product_code")
    if same_family and same_product:
        return 0
    if op.get("setup_family") and prev_op.get("setup_family") \
            and op["setup_family"] != prev_op["setup_family"]:
        matrix = ctx.constraint.get("setup_matrix") or {}
        pair = (matrix.get(prev_op["setup_family"]) or {}).get(op["setup_family"])
        if pair is not None:
            return pair
    return declared


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------

def _place_processing(ctx, combo, anchor_min, setup_min, processing_min):
    """Earliest feasible (start, end) wall minutes for one combination, or None.

    ``start`` is the wall-clock processing start and ``end`` the wall-clock
    processing end.  Processing needs ``processing_min`` *working* minutes and
    may span multiple calendar windows: gaps between working windows (lunch,
    overnight, weekend) are non-working time and simply extend the wall span.
    The equipment (when bound) additionally holds ``setup_min`` working minutes
    immediately before ``start`` for setup/changeover, beginning no earlier than
    ``anchor_min``.  A manual operation treats ``setup_min`` as lead time, so
    its processing starts at or after ``anchor_min + setup_min``.
    """
    eq = combo["equipment"]
    others = []
    if combo["person"] is not None:
        others.append(ctx.resource_for("PERSON", combo["person"]["person_code"]).free_segments())
    if combo["station"] is not None:
        others.append(ctx.resource_for("STATION", combo["station"]["station_code"]).free_segments())
    for pool in combo["tooling"]:
        others.append(ctx.resource_for("TOOLING", pool["tooling_code"]).free_segments())
    common = _intersect_many(others) if others else None

    if eq is not None:
        eq_free = ctx.resource_for("EQUIPMENT", eq["equipment_code"]).free_segments()
        if not eq_free:
            return None
        # Processing must run where the equipment and every co-resource are free
        # at the same wall time.
        proc_segs = _intersect_many([eq_free, common]) if common is not None else list(eq_free)
        for a, b in proc_segs:
            if b <= anchor_min:
                continue
            start = max(a, anchor_min)
            end = _working_end(proc_segs, start, processing_min)
            if end is None:
                # no later start recovers lost working time
                break
            # Setup occupies `setup_min` working minutes on the equipment ending
            # at `start`; its own start must be >= anchor_min.
            setup_start = _working_back(eq_free, start, setup_min)
            if setup_start is not None and setup_start >= anchor_min:
                return (start, end)
            # Not enough room before `start`; the earliest feasible processing
            # start is after running setup from anchor_min on the equipment.
            alt = _working_end(eq_free, anchor_min, setup_min)
            if alt is not None and alt > start:
                alt_end = _working_end(proc_segs, alt, processing_min)
                if alt_end is not None:
                    return (alt, alt_end)
        return None

    # Manual operation: no equipment; setup_minutes is lead time, so processing
    # must start at or after anchor_min + setup_min.
    if not common:
        return None
    return _earliest_span(common, anchor_min + setup_min, processing_min)


def _prev_op_on_equipment(ctx, equipment_code):
    """Chronologically last allocated operation metadata on this equipment."""
    res = ctx.resource_for("EQUIPMENT", equipment_code)
    if not res.allocs:
        return None
    allocs = sorted(res.allocs, key=lambda x: x[0])
    return allocs[-1][3]  # allocation tuple = (start, end, sid, op_meta)


# ---------------------------------------------------------------------------
# supply readiness
# ---------------------------------------------------------------------------

def _supply_ready(supply, order_line_id, op_code):
    """Return (anchor_min, blocker_entry_or_None)."""
    entries = [e for e in supply.get("entries", [])
               if e.get("order_line_id") == order_line_id
               and (not e.get("op_code") or e.get("op_code") == op_code)]
    anchor = None
    blocker = None
    for e in entries:
        if e.get("readiness") == "READY":
            continue
        if e.get("earliest_ready_at") is None:
            blocker = e
            break
        t = to_minutes(parse_dt(e["earliest_ready_at"]))
        anchor = t if anchor is None else max(anchor, t)
    return anchor, blocker


def _streaming_route(route):
    """Return true when a route explicitly requests transfer-batch flow."""
    mode = str(route.get("execution_model") or route.get("flow_mode") or "").upper()
    return mode in {"STREAMING_FLOW", "STREAMING", "TRANSFER_BATCH"}


def _streaming_batch_qty(total, batch_size, index):
    start = Decimal(index * batch_size)
    return min(Decimal(batch_size), total - start)


def _schedule_streaming_operations(bundle):
    """Schedule transfer batches while preserving v2 resource constraints.

    A batch may enter the next operation as soon as the matching upstream
    batch completes.  The previous batch of the same operation remains a
    predecessor so a station/person never processes two batches at once.
    """
    validate_bundle(bundle)
    ctx = RunContext(bundle)
    operations, intervals, blocks = [], [], []
    for order in bundle["order_snapshots"]:
        for line in order["lines"]:
            route = bundle["routes"][line["product_code"]]
            total = Decimal(str(line["qty"]))
            is_streaming = _streaming_route(route)
            route_default = int(route.get("transfer_batch_size") or route.get("batch_size") or 1) if is_streaming else int(total)
            ops = sorted(route["operations"], key=lambda o: o["sequence_no"])
            batch_count = max(1, (int(total) + route_default - 1) // route_default)
            by_key = {}
            previous_by_op = {}
            for batch_index in range(batch_count):
                batch_qty = _streaming_batch_qty(total, route_default, batch_index)
                for op in ops:
                    op_code = op["op_code"]
                    preds = []
                    for pred in op.get("predecessors", []):
                        pred_out = by_key.get((batch_index, pred))
                        if pred_out:
                            preds.append(pred_out)
                    prior = previous_by_op.get(op_code)
                    if prior:
                        preds.append(prior)
                    anchor = 0
                    pred_ops = [next((item for item in operations if item["schedule_operation_id"] == sid), None)
                                for sid in preds]
                    for pred_op in pred_ops:
                        if pred_op:
                            anchor = max(anchor, to_minutes(parse_dt(pred_op["plan_end"])))
                    ready_anchor, blocker = _supply_ready(ctx.supply, line["order_line_id"], op_code)
                    if blocker is not None:
                        blocks.append({"order_line_id": line["order_line_id"], "op_code": op_code,
                                       "reason_code": "SUPPLY_NOT_READY", "reason": "supply NOT_READY without earliest_ready_at"})
                        continue
                    if ready_anchor is not None:
                        anchor = max(anchor, ready_anchor)
                    combos = []
                    for eq in _eligible_equipment(ctx, op):
                        for person in _eligible_persons(ctx, op):
                            for station in _eligible_stations(ctx, op):
                                combos.append({"equipment": eq, "person": person, "station": station,
                                               "tooling": _eligible_tooling(ctx, op)})
                    best = None
                    for combo_index, combo in enumerate(combos):
                        processing = _processing_minutes(op, batch_qty, combo["equipment"])
                        prev_equipment = None
                        if combo["equipment"] is not None:
                            prev_equipment = _prev_op_on_equipment(ctx, combo["equipment"]["equipment_code"])
                        setup = _setup_minutes_for(ctx, {**op, "product_code": line["product_code"]}, combo["equipment"], prev_equipment)
                        placed = _place_processing(ctx, combo, anchor, setup, processing)
                        if placed is None:
                            continue
                        start, end_min = placed
                        candidate = (start, combo_index, end_min, combo, setup, processing)
                        if best is None or candidate[:2] < best[:2]:
                            best = candidate
                    if best is None:
                        raise PmcError(BLOCKED, "NO_FEASIBLE_WINDOW streaming op_code=%s batch=%d" % (op_code, batch_index + 1))
                    p, _, end_min, combo, setup, processing = best
                    eq = combo["equipment"]
                    sid = "%s::%s#%d/B%02d" % (line["order_line_id"], op_code, op["sequence_no"], batch_index + 1)
                    out = {
                        "schedule_operation_id": sid, "order_line_id": line["order_line_id"],
                        "product_code": line["product_code"], "op_code": op_code,
                        "predecessors": preds, "equipment_code": eq["equipment_code"] if eq else "",
                        "person_code": combo["person"]["person_code"] if combo["person"] else "",
                        "tooling_codes": [t["tooling_code"] for t in combo["tooling"]],
                        "station_code": combo["station"]["station_code"] if combo["station"] else "",
                        "plan_start": to_iso(from_minutes(p - setup)), "plan_end": to_iso(from_minutes(end_min)),
                        "qty": str(batch_qty), "uom": line["uom"], "setup_minutes": setup,
                        "processing_minutes": processing, "batch_index": batch_index + 1,
                        "batch_count": batch_count, "transfer_batch_size": route_default,
                        "flow_mode": "STREAMING_FLOW" if is_streaming else "BATCH_FLOW",
                    }
                    operations.append(out)
                    by_key[(batch_index, op_code)] = sid
                    previous_by_op[op_code] = sid
                    cur = {**op, "product_code": line["product_code"]}
                    if eq:
                        _reserve(ctx, "EQUIPMENT", eq["equipment_code"], p - setup, end_min, sid, cur, intervals)
                    if combo["person"]:
                        _reserve(ctx, "PERSON", combo["person"]["person_code"], p, end_min, sid, cur, intervals)
                    if combo["station"]:
                        _reserve(ctx, "STATION", combo["station"]["station_code"], p, end_min, sid, cur, intervals)
                    for tool in combo["tooling"]:
                        _reserve(ctx, "TOOLING", tool["tooling_code"], p, end_min, sid, cur, intervals)
    return operations, intervals, blocks


# ---------------------------------------------------------------------------
# main scheduling entry
# ---------------------------------------------------------------------------

def schedule_operations(bundle):
    """Deterministic constrained scheduling across all order lines.

    Returns (operations, resource_intervals, blocks).  Raises PmcError
    BLOCKED_INPUT when required resources/approval/checksums are missing or no
    feasible window exists; returns a FAIL validator candidate (with blocks)
    when the supply snapshot is NOT_READY without an earliest_ready_at.
    """
    validate_bundle(bundle)
    if any(_streaming_route(bundle["routes"].get(line["product_code"], {}))
           for order in bundle["order_snapshots"] for line in order["lines"]):
        return _schedule_streaming_operations(bundle)
    ctx = RunContext(bundle)

    lines = []
    for order_idx, order in enumerate(bundle["order_snapshots"]):
        for line in order["lines"]:
            route = bundle["routes"][line["product_code"]]
            ops = sorted(route["operations"], key=lambda o: o["sequence_no"])
            lines.append({
                "order": order,
                "line": line,
                "route": route,
                "ops": ops,
                "next": 0,
                "done": {},          # op_code -> scheduled operation dict
                "blocked": False,
            })

    operations = []
    intervals = []
    blocks = []

    def _block_entry(rec, op, reason_code, detail, entry=None):
        item = {
            "order_line_id": rec["line"]["order_line_id"],
            "op_code": op["op_code"],
            "reason_code": reason_code,
            "reason": detail,
            "detail": detail,
        }
        if entry is not None:
            for ref_key in ("requirement_ref", "shortage_ref", "purchase_ref",
                            "inventory_snapshot_ref"):
                if entry.get(ref_key):
                    item[ref_key] = entry[ref_key]
        return item

    def _cascade_block(rec, from_idx, reason_code, detail, entry=None):
        # the blocking operation itself gets the real reason; every successor
        # on the same line is reported as PREDECESSOR_BLOCKED.
        for k in range(from_idx, len(rec["ops"])):
            op = rec["ops"][k]
            if k == from_idx:
                code, note, refs = reason_code, detail, entry
            else:
                code = "PREDECESSOR_BLOCKED"
                note = "predecessor %s of op %s is blocked" % (
                    rec["ops"][k - 1]["op_code"], op["op_code"])
                refs = None
            blocks.append(_block_entry(rec, op, code, note, refs))
        rec["blocked"] = True
        rec["next"] = len(rec["ops"]) + 1

    while True:
        progress = False
        for rec in lines:
            if rec["blocked"] or rec["next"] >= len(rec["ops"]):
                continue
            op = rec["ops"][rec["next"]]
            # predecessor gate (same order line route)
            pending = [p for p in op.get("predecessors", []) if p not in rec["done"]]
            if pending:
                blocked_pred = any(
                    p in (b["op_code"] for b in blocks
                          if b["order_line_id"] == rec["line"]["order_line_id"])
                    for p in pending)
                if blocked_pred:
                    _cascade_block(rec, rec["next"], "PREDECESSOR_BLOCKED",
                                   "predecessor %s of op %s is blocked"
                                   % (sorted(pending)[0], op["op_code"]))
                    progress = True
                continue
            # anchor: predecessors end (plus calendar epoch 0)
            anchor = 0
            for p in op.get("predecessors", []):
                prev_op = rec["done"][p]
                anchor = max(anchor, to_minutes(parse_dt(prev_op["plan_end"])))
            # supply readiness
            ready_anchor, blocker = _supply_ready(ctx.supply, rec["line"]["order_line_id"],
                                                  op["op_code"])
            if blocker is not None:
                _cascade_block(rec, rec["next"], "SUPPLY_NOT_READY",
                               "supply NOT_READY without earliest_ready_at for "
                               "order_line %s op %s"
                               % (rec["line"]["order_line_id"], op["op_code"]),
                               entry=blocker)
                progress = True
                continue
            if ready_anchor is not None:
                anchor = max(anchor, ready_anchor)

            qty = rec["line"]["qty"]
            # route op metadata carries no product_code; enrich a working copy
            # (used for setup/product-changeover decisions and reservations)
            cur_op = dict(op)
            cur_op["product_code"] = rec["line"]["product_code"]
            combos = []
            for eq in _eligible_equipment(ctx, op):
                for person in _eligible_persons(ctx, op):
                    for station in _eligible_stations(ctx, op):
                        combos.append({
                            "equipment": eq,
                            "person": person,
                            "station": station,
                            "tooling": _eligible_tooling(ctx, op),
                        })

            best = None
            best_meta = None
            for combo_index, combo in enumerate(combos):
                eq = combo["equipment"]
                processing = _processing_minutes(op, qty, eq)
                prev_op = None
                if eq is not None:
                    prev_op = _prev_op_on_equipment(ctx, eq["equipment_code"])
                setup = _setup_minutes_for(ctx, cur_op, eq, prev_op)
                p = _place_processing(ctx, combo, anchor, setup, processing)
                if p is None:
                    continue
                start, end_min = p
                meta = (start, combo_index)
                if best is None or meta < best:
                    best = meta
                    best_meta = (combo, start, end_min, setup, processing)
            if best is None:
                raise PmcError(BLOCKED,
                               "NO_FEASIBLE_WINDOW op_code=%s order_line=%s "
                               "(setup+processing exceeds common working windows "
                               "or no calendar remains)" % (op["op_code"],
                                                            rec["line"]["order_line_id"]))
            combo, p, end_min, setup, processing = best_meta
            eq = combo["equipment"]
            sid = ctx.schedule_id(rec["line"]["order_line_id"], op["op_code"],
                                  op["sequence_no"])
            op_out = {
                "schedule_operation_id": sid,
                "order_line_id": rec["line"]["order_line_id"],
                "product_code": rec["line"]["product_code"],
                "op_code": op["op_code"],
                "predecessors": [rec["done"][pcode]["schedule_operation_id"]
                                 for pcode in op.get("predecessors", [])],
                "equipment_code": eq["equipment_code"] if eq is not None else "",
                "person_code": (combo["person"]["person_code"]
                                if combo["person"] is not None else ""),
                "tooling_codes": [t["tooling_code"] for t in combo["tooling"]],
                "station_code": (combo["station"]["station_code"]
                                 if combo["station"] is not None else ""),
                "plan_start": to_iso(from_minutes(p - setup)),
                "plan_end": to_iso(from_minutes(end_min)),
                "qty": str(qty),
                "uom": rec["line"]["uom"],
                "setup_minutes": setup,
                "processing_minutes": processing,
            }
            operations.append(op_out)
            # resource reservations (equipment includes setup span)
            if eq is not None:
                _reserve(ctx, "EQUIPMENT", eq["equipment_code"], p - setup,
                         end_min, sid, cur_op, intervals)
            if combo["person"] is not None:
                _reserve(ctx, "PERSON", combo["person"]["person_code"], p,
                         end_min, sid, cur_op, intervals)
            if combo["station"] is not None:
                _reserve(ctx, "STATION", combo["station"]["station_code"], p,
                         end_min, sid, cur_op, intervals)
            for t in combo["tooling"]:
                _reserve(ctx, "TOOLING", t["tooling_code"], p, end_min,
                         sid, cur_op, intervals)
            rec["done"][op["op_code"]] = op_out
            rec["next"] += 1
            progress = True
        if not progress:
            remaining = [rec for rec in lines
                         if not rec["blocked"] and rec["next"] < len(rec["ops"])]
            if not remaining:
                break
            raise PmcError(BLOCKED,
                           "SCHEDULE_STUCK order_line=%s op=%s"
                           % (remaining[0]["line"]["order_line_id"],
                              remaining[0]["ops"][remaining[0]["next"]]["op_code"]))
    return operations, intervals, blocks


def _reserve(ctx, kind, code, start, end, sid, op_meta, intervals):
    res = ctx.resource_for(kind, code)
    res.allocs.append((start, end, sid, op_meta))
    intervals.append({
        "schedule_operation_id": sid,
        "resource_type": kind,
        "resource_code": code,
        "start_at": to_iso(from_minutes(start)),
        "end_at": to_iso(from_minutes(end)),
    })
