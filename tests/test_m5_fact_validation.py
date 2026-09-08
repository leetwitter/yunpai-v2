from yunpai_orchestrator.m5_fact_validation import validate_m5_facts


def test_m5_fact_validation_requires_resource_and_calendar_facts():
    result = validate_m5_facts(resource_snapshot=None, calendar_snapshot=None)
    assert result["status"] == "incomplete"
    assert "resource_snapshot" in result["missing_fields"]
    assert "calendar_snapshot" in result["missing_fields"]


def test_m5_fact_validation_accepts_traceable_resource_calendar():
    result = validate_m5_facts(
        resource_snapshot={
            "equipment": [{"equipment_code": "EQ-1", "capability_codes": ["CUT"], "calendar_ref": "CAL-1"}],
            "persons": [{"person_code": "P-1", "skill_codes": ["CUT"], "calendar_ref": "CAL-1"}],
            "stations": [{"station_code": "ST-1", "calendar_ref": "CAL-1"}],
            "tooling": [],
        },
        calendar_snapshot={
            "working_intervals": [{"calendar_ref": "CAL-1", "start_at": "2026-09-05T08:00:00+08:00", "end_at": "2026-09-05T17:00:00+08:00"}],
            "unavailability": [],
        },
    )
    assert result["status"] == "ready"


def test_m5_fact_validation_requires_wip_for_wip_scenario():
    result = validate_m5_facts(
        resource_snapshot={"equipment": [], "persons": [], "stations": [], "tooling": []},
        calendar_snapshot={"working_intervals": [{"calendar_ref": "C", "start_at": "2026-09-05T08:00:00+08:00", "end_at": "2026-09-05T09:00:00+08:00"}], "unavailability": []},
        require_wip=True,
    )
    assert result["status"] == "incomplete"
    assert "wip_status" in result["missing_fields"]
