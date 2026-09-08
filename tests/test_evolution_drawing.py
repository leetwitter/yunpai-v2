"""EV-3 测试：工程图管线（PDF 文字层 / DOCX 承认书 / DXF 优雅降级）+ 身份对齐 + DeepSeek 路由。"""
from __future__ import annotations

from pathlib import Path

import pytest

from yunpai_orchestrator.evolution.identity_resolver import (
    extract_product_codes,
    resolve_product_code,
    spec_signature,
)
from yunpai_orchestrator.evolution.llm_fallback import LEVEL_L1, LEVEL_L2, LEVEL_L3, DeepSeekConfig, TaskRouter
from yunpai_orchestrator.evolution.pipelines.drawing_pipeline import (
    ingest_geometry_profile,
    parse_pdf_drawing,
    parse_spec_docx,
)
from yunpai_orchestrator.evolution.repository import EvolutionRepository


@pytest.fixture()
def repo(tmp_path: Path) -> EvolutionRepository:
    return EvolutionRepository(tmp_path / "evolution.sqlite")


def _text_pdf(texts: list[str]) -> bytes:
    import fitz

    doc = fitz.open()
    for text in texts:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    return doc.tobytes()


def _spec_docx(params: list[tuple[str, str]]) -> bytes:
    import io

    import docx

    document = docx.Document()
    table = document.add_table(rows=len(params), cols=2)
    for i, (k, v) in enumerate(params):
        table.rows[i].cells[0].text = k
        table.rows[i].cells[1].text = v
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


# ---------------------------------------------------------------------------
# PDF 工程图
# ---------------------------------------------------------------------------

def test_parse_pdf_drawing_text_layer():
    result = parse_pdf_drawing(_text_pdf(["标题栏 80826-041 客户 milan2", "L=152mm 公差 ±20mm"]))
    assert result["has_text_layer"] is True
    assert "152mm" in result["text"]
    assert result["source"] == "pdf_text_layer"


def test_parse_pdf_drawing_no_text_layer():
    import fitz

    doc = fitz.open()
    doc.new_page()  # 空页无文字层
    result = parse_pdf_drawing(doc.tobytes())
    assert result["has_text_layer"] is False


# ---------------------------------------------------------------------------
# DOCX 承认书
# ---------------------------------------------------------------------------

def test_parse_spec_docx():
    raw = _spec_docx([("外壳材质", "AL6063"), ("镀层", "镀金"), ("长度", "22.5mm")])
    result = parse_spec_docx(raw)
    assert result["params"]["外壳材质"] == "AL6063"
    assert result["params"]["镀层"] == "镀金"


# ---------------------------------------------------------------------------
# DXF 优雅降级（ezdxf 未装）
# ---------------------------------------------------------------------------

def test_parse_dxf_unsupported_without_ezdxf(tmp_path: Path):
    from yunpai_orchestrator.evolution.pipelines.drawing_pipeline import parse_dxf

    fake = tmp_path / "fake.dxf"
    fake.write_text("0\nSECTION\n2\nENTITIES\n", encoding="utf-8")
    result = parse_dxf(str(fake))
    # ezdxf 未装 → 明确 unsupported；若装了则正常解析（两者都不得静默假成功）。
    assert ("supported" in result) and (result.get("supported") is False or result.get("source") == "dxf")


# ---------------------------------------------------------------------------
# 几何特征并入档案
# ---------------------------------------------------------------------------

def test_ingest_geometry_profile(repo):
    summary = ingest_geometry_profile(
        repo, tenant_id="default", product_code="W-H909",
        geometry={"overall_length_mm": 1520, "shell_material": "铝合金", "hole_count": 4},
        document_ref="80826-041.pdf",
    )
    assert summary["fields"] == 3
    chain = repo.get_profile(tenant_id="default", product_code="W-H909")
    assert chain["geometry"]["overall_length_mm"] == 1520
    assert chain["geometry"]["hole_count"] == 4


# ---------------------------------------------------------------------------
# 身份对齐
# ---------------------------------------------------------------------------

def test_identity_exact_code_and_signature():
    assert extract_product_codes("订单 W-H410 数量 3000") == ["W-H410"]
    sig = spec_signature("HDMI TO HDMI 8K 铝合金光纤线.dwg")
    assert "HDMI" in sig.get("interface", [])
    assert "8K" in sig.get("version", [])
    assert "铝合金" in sig.get("shell", [])


def test_resolve_product_code_chain():
    known = {"W-H909", "W-H410"}
    assert resolve_product_code("型号 W-H909", known_codes=known)["method"] == "exact_code"
    assert resolve_product_code("HDMI 8K 铝合金光纤线", known_codes=known)["method"] == "spec_signature"
    assert resolve_product_code("某个不认识的名称", known_codes=known)["method"] == "unresolved"


# ---------------------------------------------------------------------------
# DeepSeek 三级路由
# ---------------------------------------------------------------------------

def test_task_router_l1_l2_l3():
    off = TaskRouter(DeepSeekConfig(enabled=False, api_key=""))
    assert off.route(LEVEL_L1)["target"] == "qwen"
    assert off.route(LEVEL_L2, qwen_ok=False)["target"] == "blocked"
    assert off.route(LEVEL_L3)["target"] == "blocked"

    on = TaskRouter(DeepSeekConfig(enabled=True, api_key="sk-test"))
    assert on.route(LEVEL_L2, qwen_ok=False)["target"] == "deepseek"
    assert on.route(LEVEL_L3)["target"] == "deepseek"
    assert on.route(LEVEL_L2, qwen_ok=True)["target"] == "qwen"
