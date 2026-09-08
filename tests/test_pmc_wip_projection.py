from yunpai_orchestrator.pmc_wip_projection import project_wip_pmc


def test_wip_projection_emits_station_worker_and_edge_facts():
    schedule = {
        "operations": [
            {"op_code": "OP01", "order_line_id": "OL1", "sequence_no": 1,
             "station_code": "S01", "person_code": "W01", "equipment_code": "M01",
             "plan_start": "2026-09-01T08:00:00+08:00", "plan_end": "2026-09-01T08:30:00+08:00"},
            {"op_code": "OP02", "order_line_id": "OL1", "sequence_no": 2,
             "station_code": "S02", "person_code": "W02", "equipment_code": "M02",
             "plan_start": "2026-09-01T08:45:00+08:00", "plan_end": "2026-09-01T09:00:00+08:00"},
        ]
    }
    resources = {
        "stations": [{"station_code": "S01", "station_name": "裁线", "parallel_slots": 1}, {"station_code": "S02", "station_name": "压接", "parallel_slots": 1}],
        "persons": [{"person_code": "W01", "person_name": "甲"}, {"person_code": "W02", "person_name": "乙"}],
        "equipment": [{"equipment_code": "M01", "name": "机一"}, {"equipment_code": "M02", "name": "机二"}],
        "working_intervals": [{"calendar_ref": "CAL", "start_at": "2026-09-01T08:00:00+08:00", "end_at": "2026-09-01T17:00:00+08:00"}],
    }
    result = project_wip_pmc(schedule, resources, require_bindings=True)
    assert result["production_blocked"] is False
    assert len(result["state_segments"]) == 2
    assert result["wip_edges"][0]["current_wip_minutes"] == 15
    assert {item["worker_id"] for item in result["worker_utilization"]} == {"W01", "W02"}
    assert result["summary_metrics"]["average_wip_wait_minutes"] == 15


def test_wip_projection_blocks_unbound_production_but_keeps_inspection_facts():
    result = project_wip_pmc({"operations": [{"op_code": "OP01", "plan_start": "2026-09-01T08:00:00+08:00", "plan_end": "2026-09-01T08:10:00+08:00"}]}, {}, require_bindings=True)
    assert result["production_blocked"] is True
    assert result["summary_metrics"]["missing_station_bindings"] == 1
    assert result["summary_metrics"]["missing_worker_bindings"] == 1
    assert result["state_segments"][0]["station_id"].startswith("UNBOUND-STATION")
