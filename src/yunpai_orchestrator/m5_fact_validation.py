"""Pre-flight validation for M5 resource, calendar and WIP facts."""

from __future__ import annotations

from typing import Any


def _text(value: Any) -> str:
    return str(value or "").strip()


def validate_m5_facts(
    *,
    resource_snapshot: Any,
    calendar_snapshot: Any,
    wip_status: Any = None,
    require_wip: bool = False,
) -> dict[str, Any]:
    missing: list[str] = []
    issues: list[dict[str, Any]] = []
    resource = resource_snapshot if isinstance(resource_snapshot, dict) else {}
    calendar = calendar_snapshot if isinstance(calendar_snapshot, dict) else {}

    if not resource:
        missing.append("resource_snapshot")
    else:
        for section in ("equipment", "persons", "stations", "tooling"):
            items = resource.get(section)
            if not isinstance(items, list):
                missing.append(f"resource_snapshot.{section}")
                continue
            for index, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    issues.append({"field": f"resource_snapshot.{section}[{index}]", "code": "INVALID_ROW"})
                    continue
                if section == "equipment":
                    if not _text(item.get("equipment_code")):
                        missing.append(f"resource_snapshot.equipment[{index}].equipment_code")
                    if not item.get("capability_codes"):
                        missing.append(f"resource_snapshot.equipment[{index}].capability_codes")
                    if not _text(item.get("calendar_ref")):
                        missing.append(f"resource_snapshot.equipment[{index}].calendar_ref")
                elif section == "persons":
                    if not _text(item.get("person_code")):
                        missing.append(f"resource_snapshot.persons[{index}].person_code")
                    if not item.get("skill_codes"):
                        missing.append(f"resource_snapshot.persons[{index}].skill_codes")
                elif section == "stations":
                    if not _text(item.get("station_code")):
                        missing.append(f"resource_snapshot.stations[{index}].station_code")
                    if not _text(item.get("calendar_ref")):
                        missing.append(f"resource_snapshot.stations[{index}].calendar_ref")

    if not calendar:
        missing.append("calendar_snapshot")
    else:
        intervals = calendar.get("working_intervals")
        if not isinstance(intervals, list) or not intervals:
            missing.append("calendar_snapshot.working_intervals")
        else:
            for index, item in enumerate(intervals, start=1):
                if not _text(item.get("calendar_ref")):
                    missing.append(f"calendar_snapshot.working_intervals[{index}].calendar_ref")
                if not _text(item.get("start_at")) or not _text(item.get("end_at")):
                    missing.append(f"calendar_snapshot.working_intervals[{index}].start_at/end_at")
        if not isinstance(calendar.get("unavailability"), list):
            missing.append("calendar_snapshot.unavailability")

    if require_wip and not isinstance(wip_status, (dict, list)):
        missing.append("wip_status")
    return {
        "status": "ready" if not missing and not issues else "incomplete",
        "missing_fields": sorted(set(missing)),
        "validation_issues": issues,
    }
