"""Deterministic validation for M2 engineering facts.

This module validates facts; it never invents BOM/SOP values and never changes
the approval policy.  Drafts may be incomplete, but the result tells the Gate
exactly which canonical fields are still missing.
"""

from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_engineering_facts(
    *,
    product_code: Any,
    bom_lines: Any,
    bom_version: Any = None,
    bom_effective_from: Any = None,
    bom_effective_to: Any = None,
    route_steps: Any = None,
    sop_version: Any = None,
    sop_effective_from: Any = None,
    sop_effective_to: Any = None,
) -> dict[str, Any]:
    """Return field-level readiness without mutating the supplied facts."""
    missing: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    product = _text(product_code)
    if not product:
        missing.append({"field": "product_code", "source": "m2.product_profile"})

    lines = bom_lines if isinstance(bom_lines, list) else []
    if not lines:
        missing.append({"field": "bom_lines", "source": "m2.bom"})
    if not _text(bom_version):
        missing.append({"field": "bom_version", "source": "m2.bom"})
    if not _text(bom_effective_from):
        missing.append({"field": "bom_effective_from", "source": "m2.bom"})
    for index, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            issues.append({"field": f"bom_lines[{index}]", "code": "INVALID_LINE"})
            continue
        where = f"bom_lines[{index}]"
        if not _text(line.get("material_code")):
            missing.append({"field": f"{where}.material_code", "source": "m2.bom"})
        qty = line.get("qty_per", line.get("quantity", line.get("qty")))
        if _number(qty) is None or _number(qty) <= 0:
            issues.append({"field": f"{where}.qty_per", "code": "INVALID_QUANTITY"})
        if not _text(line.get("uom", line.get("unit"))):
            missing.append({"field": f"{where}.uom", "source": "m2.bom"})

    steps = route_steps if isinstance(route_steps, list) else []
    if not steps:
        missing.append({"field": "route_steps", "source": "m2.sop"})
    if not _text(sop_version):
        missing.append({"field": "sop_version", "source": "m2.sop"})
    if not _text(sop_effective_from):
        missing.append({"field": "sop_effective_from", "source": "m2.sop"})
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            issues.append({"field": f"route_steps[{index}]", "code": "INVALID_STEP"})
            continue
        where = f"route_steps[{index}]"
        if not _text(step.get("operation_id", step.get("operation_code"))):
            missing.append({"field": f"{where}.operation_id", "source": "m2.sop"})
        minutes = step.get("standard_minutes", step.get("std_minutes"))
        if _number(minutes) is None or _number(minutes) <= 0:
            missing.append({"field": f"{where}.standard_minutes", "source": "m2.sop"})
        if not _text(step.get("station", step.get("station_code"))):
            missing.append({"field": f"{where}.station", "source": "m2.sop"})
        equipment = step.get("required_equipment_codes", step.get("equipment_codes"))
        if not isinstance(equipment, list) or not any(_text(item) for item in equipment):
            missing.append({"field": f"{where}.required_equipment_codes", "source": "m2.sop"})

    if _text(bom_effective_to) and _text(bom_effective_from) and str(bom_effective_to) < str(bom_effective_from):
        issues.append({"field": "bom_effective_to", "code": "INVALID_EFFECTIVE_RANGE"})
    if _text(sop_effective_to) and _text(sop_effective_from) and str(sop_effective_to) < str(sop_effective_from):
        issues.append({"field": "sop_effective_to", "code": "INVALID_EFFECTIVE_RANGE"})

    return {
        "status": "ready_for_approval" if not missing and not issues else "incomplete",
        "missing_fields": missing,
        "validation_issues": issues,
        "counts": {"bom_lines": len(lines), "route_steps": len(steps)},
    }
