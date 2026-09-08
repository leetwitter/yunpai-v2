"""工程图理解管线（U2 管线 C，EV-3）。

诚实边界：27B 与 DeepSeek 文本模型都不读图。本管线走「确定性几何解析 + 文本归纳」：
- DWG → ODA File Converter → DXF → ezdxf 提取（标注文本/孔位/外廓/标题栏）；
- PDF 工程图 → pymupdf 文字层 + 矢量图元统计；
- DOCX/PDF 承认书 → 文档表格参数抽取。
缺依赖时显式 unsupported/blocked，绝不静默降级成二进制摘要。
"""
from __future__ import annotations

from typing import Any

from ..merge_engine import merge_field
from ..repository import EvolutionRepository


def parse_pdf_drawing(raw: bytes, filename: str = "") -> dict[str, Any]:
    """PDF 工程图：文字层（尺寸/标题栏/规格）+ 矢量图元统计。"""
    import fitz  # pymupdf

    doc = fitz.open(stream=raw, filetype="pdf")
    texts: list[str] = []
    drawing_count = 0
    rects: list[list[float]] = []
    try:
        for page in doc:
            texts.append(page.get_text())
            drawings = page.get_drawings()
            drawing_count += len(drawings)
            for item in drawings:
                rect = item.get("rect")
                if rect:
                    rects.append([round(float(v), 1) for v in rect])
    finally:
        doc.close()
    has_text_layer = any(t.strip() for t in texts)
    bbox = _overall_bbox(rects)
    return {"filename": filename, "pages": len(texts), "text": "\n".join(texts),
            "has_text_layer": has_text_layer, "vector_drawings": drawing_count,
            "bbox": bbox, "source": "pdf_text_layer"}


def parse_spec_docx(raw: bytes, filename: str = "") -> dict[str, Any]:
    """DOCX 承认书/规格书：表格参数键值对 + 段落流程文字。"""
    import io

    import docx as _docx

    document = _docx.Document(io.BytesIO(raw))
    params: dict[str, Any] = {}
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            cells = [c for c in cells if c]
            if len(cells) >= 2:
                params[cells[0]] = cells[1]
    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    return {"filename": filename, "params": params, "paragraphs": paragraphs,
            "source": "docx_table"}


def parse_dxf(dxf_path: str) -> dict[str, Any]:
    """DXF → 几何特征（需 ezdxf；缺失时返回 unsupported）。"""
    try:
        import ezdxf
    except ImportError:
        return {"supported": False, "reason": "ezdxf 未安装（parsers extras）"}
    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as exc:
        return {"supported": False, "reason": f"dxf 读取失败: {exc}"}
    texts: list[str] = []
    holes: list[dict[str, Any]] = []
    modelspace = doc.modelspace()
    for e in modelspace:
        if e.dxftype() == "TEXT":
            texts.append(str(e.dxf.text))
        elif e.dxftype() == "MTEXT":
            texts.append(str(e.text))
        elif e.dxftype() == "CIRCLE":
            holes.append({"center": [round(float(e.dxf.center[0]), 2), round(float(e.dxf.center[1]), 2)],
                          "radius": round(float(e.dxf.radius), 2)})
    return {"supported": True, "source": "dxf", "texts": texts, "holes": holes,
            "layer_count": len(doc.layers), "entity_count": len(modelspace)}


def convert_dwg_to_dxf(dwg_path: str, out_dir: str) -> dict[str, Any]:
    """DWG → DXF（外部 ODA File Converter CLI；缺失/失败返回 blocked）。"""
    import shutil
    import subprocess
    from pathlib import Path

    exe = shutil.which("ODAFileConverter")
    if not exe:
        return {"supported": False, "reason": "ODAFileConverter 未安装"}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([exe, str(Path(dwg_path).parent), str(out), "ACAD2018", "DXF", "0", "1"],
                       check=True, capture_output=True, timeout=120)
    except Exception as exc:
        return {"supported": False, "reason": f"转换失败: {exc}"}
    dxf = out / (Path(dwg_path).stem + ".dxf")
    return {"supported": bool(dxf.exists()), "dxf_path": str(dxf) if dxf.exists() else ""}


def _overall_bbox(rects: list[list[float]]) -> list[float] | None:
    if not rects:
        return None
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[2] for r in rects)
    y1 = max(r[3] for r in rects)
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]


def ingest_geometry_profile(repo: EvolutionRepository, *, tenant_id: str, product_code: str,
                            geometry: dict[str, Any], document_ref: str = "",
                            source_kind: str = "drawing_pdf") -> dict[str, Any]:
    """把特征卡片并入产品档案 geometry。"""
    profile_id = f"PU-{product_code}"
    existing = repo.get_profile(tenant_id=tenant_id, product_code=product_code)
    profile_id = str(existing["profile_id"]) if existing else profile_id
    actions = {"candidate_created": 0, "reinforced": 0, "conflict": 0}
    for field, value in geometry.items():
        outcome = merge_field(repo, tenant_id=tenant_id, profile_id=profile_id,
                              field_path="geometry", key=field, value=value,
                              source_kind=source_kind, document_ref=document_ref,
                              locator={"field": field})
        actions[outcome["action"]] += 1
    repo.upsert_profile(profile_id=profile_id, tenant_id=tenant_id, product_code=product_code,
                        geometry=geometry, version=int(existing["version"]) + 1 if existing else 1)
    return {"product_code": product_code, "fields": len(geometry), "actions": actions}


async def ingest_pdf_drawing_llm(repo: EvolutionRepository, llm: Any, *, tenant_id: str,
                                 product_code: str, raw: bytes, filename: str = "") -> dict[str, Any]:
    """PDF 工程图：确定性文字层抽取 + 27B 特征归纳（P5/P6）→ 档案 geometry。"""
    parsed = parse_pdf_drawing(raw, filename)
    geometry: dict[str, Any] = {}
    if parsed["bbox"] is not None:
        geometry["outline_bbox"] = parsed["bbox"]
        geometry["vector_drawings"] = parsed["vector_drawings"]
    if parsed["has_text_layer"] and getattr(llm, "enabled", False):
        summarized = await llm.summarize_drawing(parsed["text"])
        if isinstance(summarized, dict):
            geometry.update(summarized)
    note = "no_text_layer" if not parsed["has_text_layer"] else ("llm" if geometry else "deterministic_only")
    result = ingest_geometry_profile(repo, tenant_id=tenant_id, product_code=product_code,
                                     geometry=geometry, document_ref=filename or "drawing.pdf",
                                     source_kind="drawing_pdf")
    result["note"] = note
    return result
