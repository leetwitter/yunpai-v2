"""Field-level validation for M3 inventory and M4 supplier facts."""

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


def validate_inventory_facts(
    items: Any,
    required_material_codes: list[str] | tuple[str, ...] = (),
    *,
    strict: bool = True,
) -> dict[str, Any]:
    rows = items if isinstance(items, list) else []
    required = [str(code).strip() for code in required_material_codes if str(code).strip()]
    missing: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    by_code: dict[str, list[dict[str, Any]]] = {}
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            issues.append({"field": f"inventory[{index}]", "code": "INVALID_ROW"})
            continue
        code = _text(item.get("material_code") or item.get("item_code"))
        if not code:
            missing.append({"field": f"inventory[{index}].material_code"})
            continue
        by_code.setdefault(code, []).append(item)
        if _number(item.get("available_qty", item.get("quantity"))) is None:
            issues.append({"field": f"inventory[{index}].available_qty", "code": "INVALID_QUANTITY"})
        if strict:
            for field in ("warehouse", "lot_no", "qc_status"):
                if not _text(item.get(field)):
                    missing.append({"field": f"inventory[{index}].{field}"})
            # Snapshot time is useful provenance, but older M3 contracts do
            # not require it.  Keep it advisory until the external adapter
            # exposes a stable field in every deployment.
    for code in required:
        if code not in by_code:
            missing.append({"field": f"inventory[{code}]", "reason": "missing_material_snapshot"})
    return {
        "status": "ready" if not missing and not issues else "incomplete",
        "missing_fields": missing,
        "validation_issues": issues,
        "counts": {"rows": len(rows), "materials": len(by_code)},
    }


def validate_supplier_facts(
    facts: Any,
    required_material_codes: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    mapping = facts if isinstance(facts, dict) else {}
    missing: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for code in [str(item).strip() for item in required_material_codes if str(item).strip()]:
        value = mapping.get(code)
        if isinstance(value, dict):
            identity = value.get("supplier_code") or value.get("supplier_id") or value.get("supplier_name")
        else:
            identity = value
        if not _text(identity):
            missing.append({"field": f"supplier_by_material[{code}]", "reason": "missing_supplier"})
    for code, value in mapping.items():
        if isinstance(value, dict) and value.get("confirmed_due_date") and value.get("eta"):
            if str(value["eta"]) > str(value["confirmed_due_date"]):
                issues.append({"field": f"supplier_by_material[{code}]", "code": "ETA_AFTER_REQUIRED_DATE"})
    return {
        "status": "ready" if not missing and not issues else "incomplete",
        "missing_fields": missing,
        "validation_issues": issues,
        "counts": {"materials": len(mapping)},
    }
