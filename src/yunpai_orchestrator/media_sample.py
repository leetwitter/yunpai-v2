"""图片 / 扫描 PDF 采样：把多模态可读内容降采样成 base64，喂给 agent。

确定性、无理解：只做「读内容」+「控制 token」，不做分类/映射。图片不走 OCR，
直接降采样后交给多模态 agent（35B 级多模态模型已实测具备视觉能力）。
"""
from __future__ import annotations

import base64
import io
from typing import Any


def _to_jpeg_b64(image: Any, *, max_edge: int, quality: int = 82) -> str:
    image = image.convert("RGB")
    width, height = image.size
    scale = min(1.0, max_edge / max(width, height))
    if scale < 1.0:
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))))
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def downscale_image(raw: bytes, *, max_edge: int = 1568, quality: int = 82) -> str:
    """把单张图片降采样到长边 <= max_edge，返回 JPEG base64。"""
    from PIL import Image

    image = Image.open(io.BytesIO(raw))
    return _to_jpeg_b64(image, max_edge=max_edge, quality=quality)


def render_pdf_pages(raw: bytes, *, max_pages: int = 3, max_edge: int = 1568) -> list[str]:
    """把 PDF 前 max_pages 页渲染成图，返回 base64 列表（供多模态 agent 读）。"""
    import fitz  # pymupdf

    from PIL import Image

    doc = fitz.open(stream=raw, filetype="pdf")
    images: list[str] = []
    try:
        for page in doc[:max_pages]:
            pix = page.get_pixmap(dpi=150)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            images.append(_to_jpeg_b64(image, max_edge=max_edge))
    finally:
        doc.close()
    return images
