"""产品理解（EV-2）测试：merge_engine / BOM 管线 / SOP 管线 / profile_query。"""
from __future__ import annotations

from pathlib import Path

import pytest

from yunpai_orchestrator.evolution.merge_engine import confidence, merge_field, normalize
from yunpai_orchestrator.evolution.pipelines.bom_pipeline import ingest_bom_profiles, parse_bom_records
from yunpai_orchestrator.evolution.pipelines.sop_pipeline import ingest_sop_profiles, parse_sop_pdf, split_station_pages
from yunpai_orchestrator.evolution.profile_query import build_product_chain
from yunpai_orchestrator.evolution.repository import EvolutionRepository

from tests.fixtures import xlsx_bytes


@pytest.fixture()
def repo(tmp_path: Path) -> EvolutionRepository:
    return EvolutionRepository(tmp_path / "evolution.sqlite")


# ---------------------------------------------------------------------------
# merge_engine
# ---------------------------------------------------------------------------

def test_normalize():
    assert normalize(1.005) == 1.0
    assert normalize("  铝  合金 ") == "铝 合金"
    assert normalize("  W-H909 ") == "w-h909"


def test_confidence_ranks():
    assert confidence(1) == "low"
    assert confidence(2) == "medium"
    assert confidence(3) == "high"
    assert confidence(1, has_human=True) == "high"


def test_merge_field_lifecycle(repo):
    repo.upsert_profile(profile_id="PU-P1", tenant_id="default", product_code="P1")
    a = merge_field(repo, tenant_id="default", profile_id="PU-P1", field_path="materials",
                    key="M-1", value={"material_code": "M-1", "name": "铜箔"},
                    source_kind="bom_document", document_ref="b.xlsx")
    assert a["action"] == "candidate_created"
    b = merge_field(repo, tenant_id="default", profile_id="PU-P1", field_path="materials",
                    key="M-1", value={"material_code": "M-1", "name": "铜箔"},
                    source_kind="bom_document", document_ref="c.xlsx")
    assert b["action"] == "reinforced"
    assert b["independent_count"] == 2
    c = merge_field(repo, tenant_id="default", profile_id="PU-P1", field_path="materials",
                    key="M-1", value={"material_code": "M-1", "name": "铝箔"},
                    source_kind="bom_document", document_ref="d.xlsx")
    assert c["action"] == "conflict"
    assert c["existing_evidence_id"]


# ---------------------------------------------------------------------------
# BOM 管线
# ---------------------------------------------------------------------------

def test_parse_bom_records(repo):
    raw = xlsx_bytes(
        ["型号", "物料编码", "材料名称", "用量", "单位"],
        [["W-H410", "MAT-01", "光纤线材", 1.02, "m"], ["W-H410", "MAT-02", "铝合金壳", 1, "pcs"]],
        title="HDMI光纤线",
    )
    parsed = parse_bom_records(raw, "bom.xlsx")
    assert parsed["products"]
    assert parsed["products"][0]["product_code"] == "W-H410"
    assert len(parsed["products"][0]["lines"]) == 2


def test_ingest_bom_profiles_reinforces(repo):
    raw = xlsx_bytes(
        ["型号", "物料编码", "材料名称", "用量", "单位"],
        [["W-H410", "MAT-01", "光纤线材", 1.02, "m"], ["W-H410", "MAT-02", "铝合金壳", 1, "pcs"]],
        title="HDMI光纤线",
    )
    first = ingest_bom_profiles(repo, tenant_id="default", raw=raw, filename="bom.xlsx", document_ref="bom.xlsx")
    assert first["products"][0]["materials"] == 2
    # 二次入库同一 BOM：material_code 相同 → 强化（不重复），profile 版本 +1。
    second = ingest_bom_profiles(repo, tenant_id="default", raw=raw, filename="bom.xlsx", document_ref="bom.xlsx")
    profile = repo.get_profile(tenant_id="default", product_code="W-H410")
    assert profile is not None
    assert len(profile["materials"]) == 2
    assert profile["version"] == 2


# ---------------------------------------------------------------------------
# SOP 管线
# ---------------------------------------------------------------------------

def test_split_station_pages():
    pages = [
        (0, "总流程图：裁线 -> 前处理 -> ..."),  # 首页无标记，跳过
        (1, "1 OF 41\n裁线\n作业内容：按尺寸裁切"),
        (2, "2 OF 41\n自动机前处理"),
        (3, "3 OF 41\n分线与排线"),
    ]
    stations = split_station_pages(pages)
    assert [s["seq"] for s in stations] == [1, 2, 3]
    assert stations[0]["name"] == "裁线"
    assert stations[0]["total"] == 41


def test_parse_sop_pdf_skips_flow_page():
    pages = ["总流程图", "1 OF 41\n裁线", "2 OF 41\n前处理"]
    stations = parse_sop_pdf(pages)
    assert len(stations) == 2


def test_ingest_sop_profiles(repo):
    stations = [
        {"seq": 1, "name": "裁线", "page": 1, "std_minutes": 2.0},
        {"seq": 2, "name": "CCD检查", "page": 2, "std_minutes": 1.5},
    ]
    summary = ingest_sop_profiles(repo, tenant_id="default", product_code="W-H410",
                                  stations=stations, document_ref="80806-129.pdf")
    assert summary["stations"] == 2
    chain = build_product_chain(repo, tenant_id="default", product_code="W-H410")
    assert chain["exists"] is True
    assert len(chain["process"]) == 2
    assert "sop" not in chain["missing"]  # 有工序后不再缺 SOP


# ---------------------------------------------------------------------------
# profile_query
# ---------------------------------------------------------------------------

def test_build_product_chain_missing(repo):
    chain = build_product_chain(repo, tenant_id="default", product_code="NOPE")
    assert chain["exists"] is False
    assert set(chain["missing"]) == {"bom", "sop", "drawing"}


def test_build_product_chain_full(repo):
    repo.upsert_profile(profile_id="PU-W1", tenant_id="default", product_code="W1",
                        product_name="测试线", materials=[{"material_code": "M-1"}],
                        process=[{"name": "裁线"}], geometry={"length_mm": 1000}, confidence=0.8)
    chain = build_product_chain(repo, tenant_id="default", product_code="W1")
    assert chain["exists"] is True
    assert chain["materials"][0]["material_code"] == "M-1"
    assert chain["geometry"]["length_mm"] == 1000
    assert chain["missing"] == []  # bom/sop/drawing 都有


# ---------------------------------------------------------------------------
# API：profile 查询 + extract
# ---------------------------------------------------------------------------

def test_profile_api_routes(repo):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yunpai_orchestrator.evolution.api import create_evolution_router

    repo.upsert_profile(profile_id="PU-API", tenant_id="default", product_code="W-H909",
                        product_name="HDMI 8K 光纤线", materials=[{"material_code": "M-1"}])
    app = FastAPI()
    app.include_router(create_evolution_router(repo))
    client = TestClient(app)

    resp = client.get("/api/evolution/profiles/W-H909")
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is True
    assert body["product_name"] == "HDMI 8K 光纤线"

    resp2 = client.get("/api/evolution/profiles")
    assert resp2.status_code == 200
    assert len(resp2.json()["profiles"]) >= 1
