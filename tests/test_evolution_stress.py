"""知识自进化 + 产品理解 · 系统性测试（压测/幂等/冲突/边界/推理/红线）。

确定性、不联网；LLM 多轮复测见 scripts/。目标：不是「几个用例」，而是覆盖
各种情况、可复测、可压测。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from yunpai_orchestrator.evolution.merge_engine import merge_field, normalize
from yunpai_orchestrator.evolution.pipelines.bom_pipeline import ingest_bom_profiles, parse_bom_records
from yunpai_orchestrator.evolution.profile_inference import infer_profile
from yunpai_orchestrator.evolution.profile_query import build_product_chain
from yunpai_orchestrator.evolution.redline import RedlineMonitor, protected_paths
from yunpai_orchestrator.evolution.repository import EvolutionRepository
from yunpai_orchestrator.evolution.signals import observe_run
from yunpai_orchestrator.models import new_state

from tests.fixtures import xlsx_bytes


@pytest.fixture()
def repo(tmp_path: Path) -> EvolutionRepository:
    return EvolutionRepository(tmp_path / "evolution.sqlite")


def _bom_xlsx(codes: list[str], title: str = "HDMI光纤线") -> bytes:
    rows = [[code, f"MAT-{i:02d}", f"物料{i}", 1.0, "pcs"] for i, code in enumerate(codes)]
    return xlsx_bytes(["型号", "物料编码", "材料名称", "用量", "单位"], rows, title=title)


# ---------------------------------------------------------------------------
# 压测
# ---------------------------------------------------------------------------

def test_stress_merge_field_volume(repo):
    """5000 字段级合并：新键候选、同键强化，计数精确。"""
    repo.upsert_profile(profile_id="PU-STRESS", tenant_id="default", product_code="S-1")
    for i in range(5000):
        merge_field(repo, tenant_id="default", profile_id="PU-STRESS", field_path="materials",
                    key=f"MAT-{i}", value={"material_code": f"MAT-{i}", "qty": 1.0},
                    source_kind="bom_document", document_ref="stress.xlsx")
    evidence = repo.count_field_evidence(profile_id="PU-STRESS", field_path="materials")
    assert evidence == 5000


def test_stress_many_products(repo):
    """100 个产品档案批量建 + 全量回读。"""
    for i in range(100):
        repo.upsert_profile(profile_id=f"PU-P{i:03d}", tenant_id="default",
                            product_code=f"P-{i:03d}", product_name=f"产品{i}",
                            materials=[{"material_code": f"MAT-{i}", "name": "料"}], version=1)
    assert len(repo.list_profiles(tenant_id="default", limit=500)) == 100


# ---------------------------------------------------------------------------
# 幂等 / 复测
# ---------------------------------------------------------------------------

def test_idempotent_reingest_no_dup(repo):
    """同一 BOM 反复入库 6 次：物料不重复，independent_count 随来源递增，版本递增。"""
    raw = _bom_xlsx(["W-H410", "W-H410", "W-H410"])
    for n in range(6):
        ingest_bom_profiles(repo, tenant_id="default", raw=raw, filename="bom.xlsx", document_ref=f"bom-{n}.xlsx")
    profile = repo.get_profile(tenant_id="default", product_code="W-H410")
    assert len(profile["materials"]) == 3          # 去重后仍 3 条
    assert profile["version"] == 6                  # 每次入库版本 +1
    evidence = repo.list_field_evidence(profile_id=profile["profile_id"], field_path="materials", limit=100)
    # 每条物料 evidence 的 independent_count == 6（6 个独立来源文档）
    assert all(e["independent_count"] == 6 for e in evidence)


def test_observe_run_replay_is_idempotent(repo):
    steps = [{"id": "s1", "module": "m0", "tool": "ingest_document", "status": "completed"},
             {"id": "s2", "module": "m2", "tool": "run_bom_sop_workflow", "status": "completed"}]
    state = new_state({"message": "订单"})
    state["steps"] = steps
    for _ in range(5):
        observe_run(repo, state)  # 同 task 重放 5 次
    cands = repo.list_candidates(tenant_id="default", kind="repeated_operation")
    assert len(cands) == 1
    assert cands[0]["independent_count"] == 1      # 独立计数仍 1
    assert cands[0]["support_count"] == 5          # 支持计数 5


# ---------------------------------------------------------------------------
# 冲突
# ---------------------------------------------------------------------------

def test_conflict_detection_and_evidence(repo):
    repo.upsert_profile(profile_id="PU-C1", tenant_id="default", product_code="C1")
    merge_field(repo, tenant_id="default", profile_id="PU-C1", field_path="geometry",
                key="length_mm", value=1520, source_kind="drawing_pdf", document_ref="a.pdf")
    outcome = merge_field(repo, tenant_id="default", profile_id="PU-C1", field_path="geometry",
                          key="length_mm", value=3000, source_kind="drawing_pdf", document_ref="b.pdf")
    assert outcome["action"] == "conflict"
    conflicts = [e for e in repo.list_field_evidence(profile_id="PU-C1", field_path="geometry")
                 if e["status"] == "conflict"]
    assert len(conflicts) == 1


def test_normalize_equivalence():
    """类型感知归一：数值舍入、单位/全半角/空白等价。"""
    assert normalize(1.004) == normalize(1.0)
    assert normalize("  铝  合金 ") == normalize("铝 合金")
    assert normalize(" W-H909 ") == normalize("w-h909")


# ---------------------------------------------------------------------------
# 边界情况
# ---------------------------------------------------------------------------

def test_empty_bom_zero_products(repo):
    raw = xlsx_bytes(["型号", "物料编码"], [], title="空表")
    parsed = parse_bom_records(raw, "empty.xlsx")
    assert parsed["products"] == []
    summary = ingest_bom_profiles(repo, tenant_id="default", raw=raw, filename="empty.xlsx")
    assert summary["products"] == []


def test_unknown_product_code_inference_negative(repo):
    """空库下未知产品：无同类型 → inferred=False（诚实，不硬编）。"""
    chain = build_product_chain(repo, tenant_id="default", product_code="X-999")
    assert chain["exists"] is False
    assert chain["inferred"] is False


def test_observe_run_empty_never_raises(repo):
    result = observe_run(repo, new_state({"message": ""}))
    assert result["observed"] is True


def test_promotion_rejects_sensitive(repo):
    from yunpai_orchestrator.evolution.gate import promotion_checks
    cand = {"kind": "human_correction", "content": {"note": "身份证 11010519491231002X"},
            "applicability": {}}
    problems = promotion_checks(cand)
    assert any(p["code"] == "SENSITIVE_CONTENT" for p in problems)


# ---------------------------------------------------------------------------
# 同类型新产品推理（核心新增）
# ---------------------------------------------------------------------------

def test_inference_same_type_by_name(repo):
    repo.upsert_profile(profile_id="PU-W-H410", tenant_id="default", product_code="W-H410",
                        product_name="HDMI 光纤线", category="hdmi_cable",
                        materials=[{"material_code": "MAT-01", "name": "光纤线材", "category": "线材"},
                                   {"material_code": "MAT-02", "name": "铝合金壳", "category": "外壳"}],
                        process=[{"name": "裁线"}, {"name": "CCD检查"}],
                        geometry={"length_mm": 1000})
    # 新品同类型：名称描述 HDMI 光纤线，型号 W-H415（同族）
    chain = build_product_chain(repo, tenant_id="default", product_code="W-H415", name="HDMI 光纤线 1.5m")
    assert chain["exists"] is False
    assert chain["inferred"] is True
    draft = chain["inference"]["draft"]
    assert len(draft["materials"]) == 2
    assert len(draft["process"]) == 2
    assert all("material_code" not in m for m in draft["materials"])  # 模板不含具体编码
    assert chain["inference"]["source_product_code"] == "W-H410"


def test_inference_by_family_prefix_only(repo):
    """仅型号编码（无描述名）也能靠家族前缀命中同类型。"""
    repo.upsert_profile(profile_id="PU-W-H410", tenant_id="default", product_code="W-H410",
                        product_name="HDMI 光纤线", materials=[{"material_code": "MAT-01", "name": "线材"}])
    chain = build_product_chain(repo, tenant_id="default", product_code="W-H415")
    assert chain["inferred"] is True
    assert chain["inference"]["source_product_code"] == "W-H410"


def test_inference_no_similar_product(repo):
    repo.upsert_profile(profile_id="PU-A1", tenant_id="default", product_code="USB-C-01",
                        product_name="USB-C 数据线", materials=[{"material_code": "M", "name": "线"}])
    # 完全无关的新品
    chain = build_product_chain(repo, tenant_id="default", product_code="Z-1", name="机箱外壳")
    assert chain["inferred"] is False


# ---------------------------------------------------------------------------
# 红线监测
# ---------------------------------------------------------------------------

def test_redline_clean_and_detect(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "workflows").mkdir(parents=True)
    (root / "AGENTS.md").write_text("# a", encoding="utf-8")
    (root / "workflows" / "w.json").write_text("{}", encoding="utf-8")
    assert "AGENTS.md" in [p.name for p in protected_paths(root)]
    monitor = RedlineMonitor(root)
    assert monitor.check()["clean"] is True
    (root / "workflows" / "w.json").write_text('{"changed": true}', encoding="utf-8")
    check = monitor.check()
    assert check["clean"] is False
    assert "workflows/w.json" in check["changed"]
