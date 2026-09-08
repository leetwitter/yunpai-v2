#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PMC v2 snapshots and adapters (0902 frozen PMC v2 input domain).

PMC v2 consumes an immutable input bundle of six snapshot kinds:

    order_snapshots[]      -> OrderSnapshot (multi-order supported)
    routes{product_code}   -> RouteSnapshot  (only m2.sop-route-fact.v2 or a
                               strictly validated RouteSnapshot / approved
                               v1 route adaptation)
    resource_snapshot      -> equipment / tooling / persons / stations
    calendar_snapshot      -> working_intervals + unavailability
    supply_snapshot        -> M3/M4 refs + readiness (read-only)
    constraint_snapshot    -> setup_matrix ...

Every snapshot carries snapshot_id/revision/checksum.  Missing required
fields, approval refs or checksums raise PmcError("BLOCKED_INPUT", ...) and
nothing is scheduled.  No implicit machine / person / shift / capacity /
availability defaults are ever created here.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

BLOCKED = "BLOCKED_INPUT"

# default working timezone for legacy fixture dates (legacy code pinned +08:00)
DEFAULT_TZ_OFFSET = "+08:00"
EPOCH = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8)))

STATUS_ENUM = ("ACTIVE", "MAINTENANCE", "INACTIVE")
RESOURCE_TYPE_ENUM = ("EQUIPMENT", "PERSON", "TOOLING", "STATION")
READINESS_ENUM = ("READY", "NOT_READY")


class PmcError(Exception):
    """PMC v2 fail-closed error; code == BLOCKED_INPUT for input problems."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _tzinfo(offset_text):
    try:
        sign = 1 if offset_text.startswith("+") else -1
        hhmm = offset_text[1:].split(":")
        hours = int(hhmm[0])
        minutes = int(hhmm[1]) if len(hhmm) > 1 else 0
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    except Exception as exc:
        raise PmcError(BLOCKED, "INVALID_TIMEZONE offset=%s" % offset_text) from exc


def parse_dt(text, default_tz=None):
    """Parse an ISO datetime; a missing offset defaults to the legacy +08:00."""
    if isinstance(text, datetime):
        return text
    if not isinstance(text, str) or not text:
        raise PmcError(BLOCKED, "MISSING_TIMESTAMP value=%r" % (text,))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PmcError(BLOCKED, "INVALID_TIMESTAMP value=%s" % text) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz if default_tz is not None else _tzinfo(DEFAULT_TZ_OFFSET))
    return dt


def to_iso(dt):
    return dt.isoformat()


def to_minutes(dt):
    """Aware datetime -> integer minutes since EPOCH (fixed +08:00, DST-free)."""
    return int((dt - EPOCH).total_seconds() // 60)


def from_minutes(m):
    return EPOCH + timedelta(minutes=m)


def _dec(value, field, where):
    if value is None or value == "":
        raise PmcError(BLOCKED, "MISSING_VALUE %s %s" % (where, field))
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PmcError(BLOCKED, "INVALID_VALUE %s %s=%r" % (where, field, value)) from exc
    return d


def _int(value, field, where, minimum=None, exact=None):
    if value is None or value == "":
        raise PmcError(BLOCKED, "MISSING_VALUE %s %s" % (where, field))
    if isinstance(value, bool) or not isinstance(value, int):
        raise PmcError(BLOCKED, "INVALID_VALUE %s %s=%r (must be integer)" % (where, field, value))
    if minimum is not None and value < minimum:
        raise PmcError(BLOCKED, "INVALID_VALUE %s %s=%d (must be >= %d)" % (where, field, value, minimum))
    if exact is not None and value != exact:
        raise PmcError(BLOCKED, "INVALID_VALUE %s %s=%d (must equal %d)" % (where, field, value, exact))
    return value


def _text(value, field, where, allow_empty=False):
    if value is None or (not allow_empty and value == ""):
        raise PmcError(BLOCKED, "MISSING_VALUE %s %s" % (where, field))
    if not isinstance(value, str):
        raise PmcError(BLOCKED, "INVALID_VALUE %s %s=%r (must be string)" % (where, field, value))
    return value


def _list_of_str(value, field, where):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise PmcError(BLOCKED, "INVALID_VALUE %s %s (must be list of strings)" % (where, field))
    return list(value)


def _enum(value, field, where, allowed):
    value = _text(value, field, where)
    if value not in allowed:
        raise PmcError(BLOCKED, "INVALID_VALUE %s %s=%s (allowed %s)" % (where, field, value, sorted(allowed)))
    return value


# ---------------------------------------------------------------------------
# canonical JSON / checksum
# ---------------------------------------------------------------------------

def _default_json(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError("not serialisable: %r" % (obj,))


def canonical_bytes(doc, skip=("checksum",)):
    if skip:
        doc = {k: v for k, v in doc.items() if k not in skip}
    return json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=_default_json).encode("utf-8")


def checksum_of(doc, skip=("checksum",)):
    return hashlib.sha256(canonical_bytes(doc, skip=skip)).hexdigest()


def finalize_snapshot(doc):
    """Attach the canonical checksum (top-level 'checksum' key) if absent."""
    if "checksum" not in doc:
        doc["checksum"] = checksum_of(doc)
    return doc


def require_checksum(doc, where):
    if not isinstance(doc.get("checksum"), str) or not doc["checksum"]:
        raise PmcError(BLOCKED, "MISSING_CHECKSUM %s" % where)
    if checksum_of(doc) != doc["checksum"]:
        raise PmcError(BLOCKED, "CHECKSUM_MISMATCH %s" % where)


# ---------------------------------------------------------------------------
# snapshot strict validators (each raises PmcError BLOCKED_INPUT)
# ---------------------------------------------------------------------------

def validate_order_snapshot(snap):
    where = "order_snapshot[%s]" % snap.get("snapshot_id", "?")
    _text(snap.get("snapshot_id"), "snapshot_id", "order_snapshot")
    require_checksum(snap, where)
    _text(snap.get("order_id"), "order_id", where)
    _text(snap.get("order_no"), "order_no", where)
    lines = snap.get("lines")
    if not isinstance(lines, list) or not lines:
        raise PmcError(BLOCKED, "MISSING_OPERATIONS %s lines" % where)
    for line in lines:
        lw = "%s line" % where
        _text(line.get("order_line_id"), "order_line_id", lw)
        _text(line.get("product_code"), "product_code", lw)
        _text(line.get("uom"), "uom", lw)
        if _dec(line.get("qty"), "qty", lw) <= 0:
            raise PmcError(BLOCKED, "INVALID_VALUE %s qty must be positive" % lw)
    return snap


_ROUTE_OP_GROUP_FIELDS = (
    "required_equipment_codes", "required_equipment_capabilities",
    "required_person_codes", "required_skill_codes",
    "required_tooling_codes", "required_station_codes",
)


def validate_route_snapshot(route, where="route_snapshot"):
    _text(route.get("product_code"), "product_code", where)
    _text(route.get("route_code"), "route_code", where)
    _text(route.get("route_version"), "route_version", where)
    _text(route.get("approval_ref"), "approval_ref", where)
    _text(route.get("snapshot_id"), "snapshot_id", where)
    require_checksum(route, where)
    ops = route.get("operations")
    if not isinstance(ops, list) or not ops:
        raise PmcError(BLOCKED, "MISSING_OPERATIONS %s" % where)
    codes = []
    for i, op in enumerate(ops):
        ow = "%s op[%d]" % (where, i)
        op_code = _text(op.get("op_code"), "op_code", ow)
        codes.append(op_code)
        _text(op.get("name"), "name", ow, allow_empty=True)
        _int(op.get("sequence_no"), "sequence_no", ow, minimum=1)
        if op.get("standard_minutes") is None:
            raise PmcError(BLOCKED, "MISSING_STANDARD_MINUTES %s %s" % (ow, op_code))
        if _dec(op.get("standard_minutes"), "standard_minutes", ow) < 0:
            raise PmcError(BLOCKED, "INVALID_VALUE %s standard_minutes must be >= 0" % ow)
        _int(op.get("quantity_basis"), "quantity_basis", ow, minimum=1)
        _int(op.get("batch_size"), "batch_size", ow, minimum=1)
        preds = op.get("predecessors")
        if preds is None:
            raise PmcError(BLOCKED, "MISSING_PREDECESSORS %s %s" % (ow, op_code))
        preds = _list_of_str(preds, "predecessors", ow)
        if not isinstance(op.get("setup_minutes"), int) or op["setup_minutes"] < 0:
            raise PmcError(BLOCKED, "MISSING_VALUE %s setup_minutes (>=0 int)" % ow)
        if not isinstance(op.get("parallel_allowed"), bool):
            raise PmcError(BLOCKED, "MISSING_VALUE %s parallel_allowed (bool)" % ow)
        groups = {f: _list_of_str(op.get(f), f, ow) for f in _ROUTE_OP_GROUP_FIELDS}
        if not any(groups.values()):
            raise PmcError(BLOCKED, "MISSING_RESOURCE_REQUIREMENT %s %s" % (ow, op_code))
        op["predecessors"] = preds
    if len(set(codes)) != len(codes):
        raise PmcError(BLOCKED, "INVALID_VALUE %s duplicate op_code" % where)
    code_set = set(codes)
    for i, op in enumerate(ops):
        for p in op.get("predecessors", []):
            if p not in code_set:
                raise PmcError(BLOCKED, "MISSING_PREDECESSOR_REF %s op[%d] -> %s" % (where, i, p))
            if p == op["op_code"]:
                raise PmcError(BLOCKED, "INVALID_VALUE %s op[%d] self-predecessor" % (where, i))
    return route


def _validate_equipment(eq, where):
    code = _text(eq.get("equipment_code"), "equipment_code", where)
    _text(eq.get("equipment_type"), "equipment_type", where)
    _list_of_str(eq.get("capability_codes"), "capability_codes", where)
    if _dec(eq.get("capacity_per_hour"), "capacity_per_hour", where) <= 0:
        raise PmcError(BLOCKED, "INVALID_VALUE %s capacity_per_hour > 0" % where)
    if _dec(eq.get("efficiency_factor"), "efficiency_factor", where) <= 0:
        raise PmcError(BLOCKED, "INVALID_VALUE %s efficiency_factor > 0" % where)
    _enum(eq.get("status"), "status", where, STATUS_ENUM)
    _text(eq.get("calendar_ref"), "calendar_ref", where)
    return code


def _validate_tooling(tl, where):
    code = _text(tl.get("tooling_code"), "tooling_code", where)
    _text(tl.get("tooling_type"), "tooling_type", where)
    _list_of_str(tl.get("capability_codes"), "capability_codes", where)
    _list_of_str(tl.get("compatible_product_codes"), "compatible_product_codes", where)
    _int(tl.get("quantity_available"), "quantity_available", where, minimum=1)
    _enum(tl.get("status"), "status", where, STATUS_ENUM)
    _text(tl.get("calendar_ref"), "calendar_ref", where)
    return code


def _validate_person(ps, where):
    code = _text(ps.get("person_code"), "person_code", where)
    _list_of_str(ps.get("skill_codes"), "skill_codes", where)
    _list_of_str(ps.get("qualified_operation_codes"), "qualified_operation_codes", where)
    _int(ps.get("max_parallel_tasks"), "max_parallel_tasks", where, minimum=1, exact=1)
    _enum(ps.get("status"), "status", where, STATUS_ENUM)
    _text(ps.get("calendar_ref"), "calendar_ref", where)
    return code


def _validate_station(st, where):
    code = _text(st.get("station_code"), "station_code", where)
    _text(st.get("work_center_code"), "work_center_code", where)
    _int(st.get("parallel_slots"), "parallel_slots", where, minimum=1)
    _enum(st.get("status"), "status", where, STATUS_ENUM)
    _text(st.get("calendar_ref"), "calendar_ref", where)
    return code


def validate_resource_snapshot(snap):
    where = "resource_snapshot[%s]" % snap.get("snapshot_id", "?")
    _text(snap.get("snapshot_id"), "snapshot_id", "resource_snapshot")
    require_checksum(snap, where)
    seen = {}
    for section, validator in (("equipment", _validate_equipment),
                               ("tooling", _validate_tooling),
                               ("persons", _validate_person),
                               ("stations", _validate_station)):
        items = snap.get(section)
        if items is None:
            raise PmcError(BLOCKED, "MISSING_VALUE %s %s (section required)" % (where, section))
        if not isinstance(items, list):
            raise PmcError(BLOCKED, "INVALID_VALUE %s %s (must be list)" % (where, section))
        for item in items:
            code = validator(item, "%s %s" % (where, section))
            if code in seen:
                raise PmcError(BLOCKED, "INVALID_VALUE %s duplicate resource code %s" % (where, code))
            seen[code] = section
    return snap


def validate_calendar_snapshot(snap):
    where = "calendar_snapshot[%s]" % snap.get("snapshot_id", "?")
    _text(snap.get("snapshot_id"), "snapshot_id", "calendar_snapshot")
    require_checksum(snap, where)
    intervals = snap.get("working_intervals")
    if intervals is None:
        raise PmcError(BLOCKED, "MISSING_VALUE %s working_intervals" % where)
    if not isinstance(intervals, list):
        raise PmcError(BLOCKED, "INVALID_VALUE %s working_intervals" % where)
    for iv in intervals:
        iw = "%s interval" % where
        _text(iv.get("calendar_ref"), "calendar_ref", iw)
        _text(iv.get("shift_code"), "shift_code", iw)
        s = parse_dt(iv.get("start_at"))
        e = parse_dt(iv.get("end_at"))
        if e <= s:
            raise PmcError(BLOCKED, "INVALID_VALUE %s end_at must be > start_at" % iw)
    ua = snap.get("unavailability")
    if ua is None:
        raise PmcError(BLOCKED, "MISSING_VALUE %s unavailability" % where)
    if not isinstance(ua, list):
        raise PmcError(BLOCKED, "INVALID_VALUE %s unavailability" % where)
    for item in ua:
        uw = "%s unavailability" % where
        _text(item.get("resource_code"), "resource_code", uw)
        _enum(item.get("resource_type"), "resource_type", uw, RESOURCE_TYPE_ENUM)
        _text(item.get("reason"), "reason", uw)
        s = parse_dt(item.get("start_at"))
        e = parse_dt(item.get("end_at"))
        if e <= s:
            raise PmcError(BLOCKED, "INVALID_VALUE %s unavailability end_at > start_at" % uw)
    return snap


def validate_supply_snapshot(snap):
    where = "supply_snapshot[%s]" % snap.get("snapshot_id", "?")
    _text(snap.get("snapshot_id"), "snapshot_id", "supply_snapshot")
    require_checksum(snap, where)
    entries = snap.get("entries")
    if entries is None:
        raise PmcError(BLOCKED, "MISSING_VALUE %s entries" % where)
    if not isinstance(entries, list):
        raise PmcError(BLOCKED, "INVALID_VALUE %s entries" % where)
    for entry in entries:
        ew = "%s entry" % where
        _text(entry.get("order_line_id"), "order_line_id", ew)
        if entry.get("op_code") is not None:
            _text(entry.get("op_code"), "op_code", ew)
        _enum(entry.get("readiness"), "readiness", ew, READINESS_ENUM)
        _text(entry.get("requirement_ref"), "requirement_ref", ew)
        _text(entry.get("inventory_snapshot_ref"), "inventory_snapshot_ref", ew)
        for opt in ("shortage_ref", "purchase_ref"):
            if entry.get(opt) is not None:
                _text(entry.get(opt), opt, ew)
        if entry.get("earliest_ready_at") is not None:
            parse_dt(entry["earliest_ready_at"])
    return snap


def validate_constraint_snapshot(snap):
    where = "constraint_snapshot[%s]" % snap.get("snapshot_id", "?")
    _text(snap.get("snapshot_id"), "snapshot_id", "constraint_snapshot")
    require_checksum(snap, where)
    matrix = snap.get("setup_matrix")
    if matrix is None:
        raise PmcError(BLOCKED, "MISSING_VALUE %s setup_matrix" % where)
    if not isinstance(matrix, dict):
        raise PmcError(BLOCKED, "INVALID_VALUE %s setup_matrix" % where)
    for fr, to_map in matrix.items():
        if not isinstance(fr, str) or not isinstance(to_map, dict):
            raise PmcError(BLOCKED, "INVALID_VALUE %s setup_matrix key" % where)
        for to, minutes in to_map.items():
            if not isinstance(to, str):
                raise PmcError(BLOCKED, "INVALID_VALUE %s setup_matrix to-key" % where)
            if not isinstance(minutes, int) or minutes < 0:
                raise PmcError(BLOCKED, "INVALID_VALUE %s setup_matrix minutes (>=0 int)" % where)
    return snap


def validate_bundle(bundle):
    """Strict PMC v2 input gate: every required snapshot/field/checksum present."""
    if not isinstance(bundle, dict):
        raise PmcError(BLOCKED, "MISSING_INPUT_BUNDLE")
    orders = bundle.get("order_snapshots")
    if not isinstance(orders, list) or not orders:
        raise PmcError(BLOCKED, "MISSING_ORDER_SNAPSHOT no order snapshots")
    for snap in orders:
        validate_order_snapshot(snap)
    routes = bundle.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise PmcError(BLOCKED, "MISSING_ROUTE_SNAPSHOT no routes")
    for product_code, route in routes.items():
        validate_route_snapshot(route, "route_snapshot[%s]" % product_code)
        if route.get("product_code") != product_code:
            raise PmcError(BLOCKED, "INVALID_VALUE routes key %s != route.product_code %s"
                           % (product_code, route.get("product_code")))
    for snap_name, validator in (
            ("resource_snapshot", validate_resource_snapshot),
            ("calendar_snapshot", validate_calendar_snapshot),
            ("supply_snapshot", validate_supply_snapshot),
            ("constraint_snapshot", validate_constraint_snapshot)):
        snap = bundle.get(snap_name)
        if snap is None:
            raise PmcError(BLOCKED, "MISSING_%s %s" % (snap_name.upper(), snap_name))
        validator(snap)
    # every scheduled product must have a route
    for order in orders:
        for line in order["lines"]:
            if line["product_code"] not in routes:
                raise PmcError(BLOCKED, "MISSING_ROUTE product_code=%s order_line=%s"
                               % (line["product_code"], line["order_line_id"]))
    # active resources must reference a calendar that actually provides windows
    resource = bundle["resource_snapshot"]
    calendar = bundle["calendar_snapshot"]
    cal_refs = {iv["calendar_ref"] for iv in calendar.get("working_intervals", [])}
    for section, code_key in (("equipment", "equipment_code"), ("persons", "person_code"),
                              ("tooling", "tooling_code"), ("stations", "station_code")):
        for item in resource.get(section, []):
            if item.get("status") == "ACTIVE":
                cref = item.get("calendar_ref")
                if cref not in cal_refs:
                    raise PmcError(BLOCKED, "UNKNOWN_CALENDAR_REF %s=%s calendar_ref=%s (no working intervals)"
                                   % (section, item.get(code_key), cref))
    return bundle


# ---------------------------------------------------------------------------
# builders / adapters
# ---------------------------------------------------------------------------

def build_order_snapshot(order, *, snapshot_id=None, revision=1):
    """Order fact/frozen order dict -> OrderSnapshot (legacy lines accepted)."""
    order_id = _text(order.get("order_id"), "order_id", "order")
    order_no = _text(order.get("order_no"), "order_no", "order")
    lines_src = order.get("lines")
    if not isinstance(lines_src, list) or not lines_src:
        raise PmcError(BLOCKED, "MISSING_OPERATIONS order lines")
    lines = []
    for idx, line in enumerate(lines_src):
        oid = line.get("order_line_id")
        if not oid:
            oid = "%s::L%s" % (order_id, line.get("line_no", idx + 1))
        uom = line.get("uom")
        if not uom:
            uom = "PCS"
        qty = line.get("qty")
        if _dec(qty, "qty", "order line") <= 0:
            raise PmcError(BLOCKED, "INVALID_VALUE order qty must be positive")
        lines.append({
            "order_line_id": str(oid),
            "product_code": _text(line.get("product_code"), "product_code", "order line"),
            "qty": str(qty),
            "uom": str(uom),
            "due_date": line.get("due_date"),
            "priority": line.get("priority"),
            "customer_code": line.get("customer_code"),
        })
    snap = {
        "snapshot_id": snapshot_id or ("SNAP-ORD-%s" % order_id),
        "revision": revision,
        "order_id": order_id,
        "order_no": order_no,
        "lines": lines,
    }
    return finalize_snapshot(snap)


def _resolve_legacy_requirements(op_code, legacy_resource_code, requirement_map, resource_binding):
    """Resolve the v2 resource-requirement groups for one legacy v1 operation.

    requirement_map[op_code] is authoritative when present; otherwise the
    legacy ``resource_code`` is looked up in resource_binding
    ({legacy_code: {"type": "equipment"|"person"|"skill"|"station",
                    "code": <v2 code|skill name>}}).  Neither present ->
    BLOCKED_INPUT (no implicit resources).
    """
    groups = {}
    if requirement_map is not None and op_code in requirement_map:
        groups = dict(requirement_map[op_code] or {})
    elif resource_binding is not None and legacy_resource_code in resource_binding:
        bind = resource_binding[legacy_resource_code]
        kind = bind.get("type")
        code = bind.get("code")
        if kind == "equipment":
            groups = {"required_equipment_codes": [code]}
        elif kind == "person":
            groups = {"required_person_codes": [code]}
        elif kind == "skill":
            groups = {"required_skill_codes": [code]}
        elif kind == "station":
            groups = {"required_station_codes": [code]}
        else:
            raise PmcError(BLOCKED, "INVALID_VALUE resource_binding type=%r" % (kind,))
    groups = {k: _list_of_str(groups.get(k), k, "legacy adapter") for k in _ROUTE_OP_GROUP_FIELDS}
    if not any(groups.values()):
        raise PmcError(BLOCKED,
                       "MISSING_RESOURCE_REQUIREMENT op_code=%s legacy_resource_code=%s "
                       "(needs requirement_map or resource_binding)"
                       % (op_code, legacy_resource_code))
    return groups


def adapt_route_snapshot_from_v1_route_fact(fact, *, batch_size, requirement_map=None,
                                            resource_binding=None, snapshot_id=None,
                                            revision=1):
    """m2.sop-route-fact.v1 (m0.sop.v1 route payload) -> strictly validated RouteSnapshot.

    - route ordering is the authoritative v1 serial predecessor relation; an
      explicit op.predecessor (when present) must reference an existing op.
    - batch_size is REQUIRED (no implicit legacy batch) and becomes both the
      v2 quantity_basis and batch_size so legacy total-time semantics are
      preserved on a reference 60/h machine.
    - every op needs its v2 resource requirements from requirement_map /
      resource_binding; missing -> BLOCKED_INPUT.
    """
    if not isinstance(fact, dict) or not fact.get("route"):
        raise PmcError(BLOCKED, "MISSING_OPERATIONS route_fact.route")
    if fact.get("approval_ref") is None or not str(fact.get("approval_ref")):
        raise PmcError(BLOCKED, "MISSING_APPROVAL_REF route_fact approval_ref required")
    batch_size = _int(batch_size, "batch_size", "legacy route adapter", minimum=1)
    route = fact["route"]
    ops_src = route.get("operations")
    if not isinstance(ops_src, list) or not ops_src:
        raise PmcError(BLOCKED, "MISSING_OPERATIONS route_fact.route.operations")
    ops = []
    for idx, op in enumerate(ops_src):
        ow = "legacy op[%d]" % idx
        op_code = _text(op.get("op_code"), "op_code", ow)
        std = op.get("std_minutes")
        if std is None:
            raise PmcError(BLOCKED, "MISSING_STANDARD_MINUTES op_code=%s" % op_code)
        if _dec(std, "std_minutes", ow) < 0:
            raise PmcError(BLOCKED, "INVALID_VALUE %s std_minutes >= 0" % ow)
        prov = op.get("provenance") or {}
        if prov.get("level") == "SIMULATED_GAP_FILL":
            if not prov.get("approved_by") or not prov.get("approved_at"):
                raise PmcError(BLOCKED,
                               "MISSING_APPROVAL_REF op_code=%s SIMULATED_GAP_FILL provenance "
                               "requires approved_by/approved_at" % op_code)
        if "predecessor" in op and op["predecessor"] is not None:
            preds = [str(op["predecessor"])]
        else:
            preds = [ops_src[idx - 1]["op_code"]] if idx > 0 else []
        groups = _resolve_legacy_requirements(op_code, op.get("resource_code"),
                                              requirement_map, resource_binding)
        extra = (requirement_map or {}).get(op_code) or {}
        setup_minutes = extra.get("setup_minutes", 0)
        if not isinstance(setup_minutes, int) or setup_minutes < 0:
            raise PmcError(BLOCKED, "INVALID_VALUE %s setup_minutes (>=0 int)" % ow)
        ops.append({
            "op_code": op_code,
            "name": op.get("name", ""),
            "sequence_no": idx + 1,
            "predecessors": preds,
            "standard_minutes": int(std),
            "quantity_basis": batch_size,
            "batch_size": batch_size,
            "setup_minutes": setup_minutes,
            "setup_family": extra.get("setup_family"),
            "parallel_allowed": bool(extra.get("parallel_allowed", False)),
            "required_equipment_codes": groups["required_equipment_codes"],
            "required_equipment_capabilities": groups["required_equipment_capabilities"],
            "required_person_codes": groups["required_person_codes"],
            "required_skill_codes": groups["required_skill_codes"],
            "required_tooling_codes": groups["required_tooling_codes"],
            "required_station_codes": groups["required_station_codes"],
            "provenance": prov,
            "approval_ref": str(fact["approval_ref"]),
        })
    route_snap = {
        "snapshot_id": snapshot_id or ("SNAP-RT-%s" % fact.get("route_id", "v1")),
        "revision": revision,
        "product_code": _text(route.get("product_code"), "product_code", "legacy route"),
        "route_code": (route.get("route_code") or str(fact.get("route_id", ""))),
        "route_version": _text(fact.get("version") or route.get("version"),
                               "version", "legacy route"),
        "approval_ref": str(fact["approval_ref"]),
        "source_candidate_ref": fact.get("source_candidate_ref"),
        "adapted_from": "m2.sop-route-fact.v1",
        "operations": ops,
    }
    finalize_snapshot(route_snap)
    validate_route_snapshot(route_snap, "route_snapshot[v1-adapter]")
    return route_snap


def build_supply_snapshot(entries=None, *, snapshot_id="SNAP-SUP-R0", revision=1):
    """SupplySnapshot builder; entries are validated and checksummed."""
    snap = {
        "snapshot_id": snapshot_id,
        "revision": revision,
        "entries": entries if entries is not None else [],
    }
    finalize_snapshot(snap)
    validate_supply_snapshot(snap)
    return snap


def build_constraint_snapshot(setup_matrix=None, *, constraint_version=None,
                              default_batch_size=None, snapshot_id="SNAP-CON-R0",
                              revision=1):
    """ConstraintSnapshot builder (setup_matrix is optional and defaults to {})."""
    snap = {
        "snapshot_id": snapshot_id,
        "revision": revision,
        "setup_matrix": setup_matrix if setup_matrix is not None else {},
        "constraint_version": constraint_version,
        "default_batch_size": default_batch_size,
    }
    finalize_snapshot(snap)
    validate_constraint_snapshot(snap)
    return snap


# legacy adapters -----------------------------------------------------------

def _policy_entry(policy, section, code, keys, where):
    if not isinstance(policy, dict):
        raise PmcError(BLOCKED, "MISSING_LEGACY_POLICY %s" % where)
    section_policy = policy.get(section)
    if not isinstance(section_policy, dict) or code not in section_policy:
        raise PmcError(BLOCKED, "MISSING_LEGACY_POLICY %s %s=%s (v2 attributes required)"
                       % (where, section, code))
    entry = section_policy[code]
    if not isinstance(entry, dict):
        raise PmcError(BLOCKED, "INVALID_VALUE %s policy entry %s" % (where, code))
    out = {}
    for key in keys:
        if key not in entry:
            raise PmcError(BLOCKED, "MISSING_LEGACY_POLICY %s %s=%s policy.%s missing"
                           % (where, section, code, key))
        out[key] = entry[key]
    return out


def adapt_legacy_resources(*, machines, personnel, policy, snapshot_id=None, revision=1):
    """Legacy machine/personnel fixture records -> ResourceSnapshot.

    Legacy machines carry code/name/capacity_per_hour only and personnel carry
    code/name/skills only; every v2-only attribute (equipment_type, capability
    codes, efficiency_factor, status, calendar_ref, qualified operations) must
    be supplied explicitly by ``policy`` — no implicit defaults.
    """
    where = "legacy resource adapter"
    if not isinstance(machines, list) or not isinstance(personnel, list):
        raise PmcError(BLOCKED, "INVALID_VALUE %s machines/personnel lists" % where)
    global_cal = (policy or {}).get("calendar_ref")
    equipments = []
    for m in machines:
        code = _text(m.get("code"), "code", where)
        cap = m.get("capacity_per_hour")
        if _dec(cap, "capacity_per_hour", where) <= 0:
            raise PmcError(BLOCKED, "MISSING_VALUE %s machine %s capacity_per_hour (>0 required)"
                           % (where, code))
        p = _policy_entry(policy, "equipment", code,
                          ("equipment_type", "capability_codes", "efficiency_factor", "status"),
                          where)
        if _dec(p["efficiency_factor"], "efficiency_factor", where) <= 0:
            raise PmcError(BLOCKED, "INVALID_VALUE %s efficiency_factor > 0" % where)
        cref = p.get("calendar_ref") or global_cal
        if not cref:
            raise PmcError(BLOCKED, "MISSING_LEGACY_POLICY %s machine %s calendar_ref"
                           % (where, code))
        equipments.append({
            "equipment_code": code,
            "equipment_type": p["equipment_type"],
            "capability_codes": _list_of_str(p["capability_codes"], "capability_codes", where),
            "capacity_per_hour": str(cap),
            "efficiency_factor": str(p["efficiency_factor"]),
            "status": _enum(p["status"], "status", where, STATUS_ENUM),
            "calendar_ref": cref,
        })
    persons = []
    for person in personnel:
        code = _text(person.get("code"), "code", where)
        p = _policy_entry(policy, "person", code, ("status",), where)
        cref = p.get("calendar_ref") or global_cal
        if not cref:
            raise PmcError(BLOCKED, "MISSING_LEGACY_POLICY %s person %s calendar_ref"
                           % (where, code))
        qualified = _list_of_str(p.get("qualified_operation_codes"),
                                 "qualified_operation_codes", where)
        persons.append({
            "person_code": code,
            "skill_codes": _list_of_str(person.get("skills"), "skills", where),
            "qualified_operation_codes": qualified,
            "max_parallel_tasks": 1,
            "status": _enum(p["status"], "status", where, STATUS_ENUM),
            "calendar_ref": cref,
        })
    snap = {
        "snapshot_id": snapshot_id or "SNAP-RES-LEGACY",
        "revision": revision,
        "equipment": equipments,
        "tooling": [],
        "persons": persons,
        "stations": [],
    }
    finalize_snapshot(snap)
    validate_resource_snapshot(snap)
    return snap


def adapt_legacy_calendar(*, calendar_entries, calendar_ref, snapshot_id=None,
                          revision=1, timezone="+08:00", unavailability=None):
    """Legacy calendar fixture records -> CalendarSnapshot.

    Legacy entries are {date, shift, start, end}; a missing field blocks the
    adapter.  Availability is never assumed — the generated working intervals
    are exactly the declared windows.  Optional explicit unavailability
    (maintenance / leave) entries map 1:1 into unavailability.
    """
    where = "legacy calendar adapter"
    if not isinstance(calendar_entries, list) or not calendar_entries:
        raise PmcError(BLOCKED, "MISSING_OPERATIONS %s calendar entries" % where)
    tz = _tzinfo(timezone)
    intervals = []
    for c in calendar_entries:
        date = _text(c.get("date"), "date", where)
        start = _text(c.get("start"), "start", where)
        end = _text(c.get("end"), "end", where)
        shift = _text(c.get("shift"), "shift", where)
        try:
            s = datetime.fromisoformat("%sT%s%s" % (date, start, timezone))
            e = datetime.fromisoformat("%sT%s%s" % (date, end, timezone))
        except ValueError as exc:
            raise PmcError(BLOCKED, "INVALID_TIMESTAMP %s date=%s start=%s end=%s"
                           % (where, date, start, end)) from exc
        if e <= s:
            raise PmcError(BLOCKED, "INVALID_VALUE %s end must be > start" % where)
        intervals.append({
            "calendar_ref": calendar_ref,
            "start_at": to_iso(s),
            "end_at": to_iso(e),
            "shift_code": shift,
        })
    ua_out = []
    for item in (unavailability or []):
        ua_out.append({
            "resource_code": _text(item.get("resource_code"), "resource_code", where),
            "resource_type": _enum(item.get("resource_type"), "resource_type", where,
                                   RESOURCE_TYPE_ENUM),
            "start_at": to_iso(parse_dt(item.get("start_at"), default_tz=tz)),
            "end_at": to_iso(parse_dt(item.get("end_at"), default_tz=tz)),
            "reason": _text(item.get("reason"), "reason", where),
        })
    snap = {
        "snapshot_id": snapshot_id or "SNAP-CAL-LEGACY",
        "revision": revision,
        "working_intervals": intervals,
        "unavailability": ua_out,
    }
    finalize_snapshot(snap)
    validate_calendar_snapshot(snap)
    return snap
