"""真实文件格式识别：magic bytes + 扩展名双因子，禁止只看扩展名分派。

任务书 §3.2：通过 magic bytes 和 MIME 校验真实格式，不能只看扩展名；
伪扩展名/坏文件必须隔离为 unsupported/mismatch，不能当目标格式解析。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# 支持声明的扩展名（与前端 upload.ts 对齐）。
SUPPORTED_SUFFIXES = (
    ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".json", ".pdf", ".docx",
    ".txt", ".md", ".png", ".jpg", ".jpeg", ".zip", ".rar", ".7z",
    ".dwg", ".et", ".doc", ".pptx",
)

# zip 内用于区分 OOXML 类型的标记。
_XLSX_MARKERS = (b"xl/workbook", b"xl/worksheets", b"[Content_Types].xml")
_XLSM_MARKER = b"xl/workbook.bin"
_DOCX_MARKERS = (b"word/document.xml",)
_PPTX_MARKER = b"ppt/presentation.xml"


@dataclass(frozen=True)
class FormatVerdict:
    declared_suffix: str          # 声明的扩展名（小写，含点）
    detected_format: str          # 嗅探到的真实格式
    mime_type: str
    match: bool                   # 声明与真实格式一致
    reason: str = ""              # mismatch/unsupported 的解释


def _zip_member_names(raw: bytes) -> list[str]:
    try:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            return archive.namelist()
    except Exception:
        return []


def sniff_format(raw: bytes, filename: str = "") -> FormatVerdict:
    """按文件头判断真实格式并与声明扩展名比对。"""
    name = (filename or "").lower()
    suffix = "." + (name.rsplit(".", 1)[1] if "." in name else "")
    head = raw[:16]
    detected = "unknown"
    mime = "application/octet-stream"

    if raw.startswith(b"%PDF"):
        detected, mime = "pdf", "application/pdf"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        detected, mime = "png", "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        detected, mime = "jpg", "image/jpeg"
    elif raw.startswith(b"Rar!\x1a\x07"):
        detected, mime = "rar", "application/vnd.rar"
    elif raw.startswith(b"7z\xbc\xaf\x27\x1c"):
        detected, mime = "7z", "application/x-7z-compressed"
    elif raw.startswith(b"PK\x03\x04") or raw.startswith(b"PK\x05\x06"):
        members = [member.lower() for member in _zip_member_names(raw)]
        if any("word/document.xml" in member for member in members):
            detected, mime = "docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif any("ppt/presentation.xml" in member for member in members):
            detected, mime = "pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        elif any("xl/workbook.bin" in member or "xl/vbaProject.bin" in member for member in members):
            detected, mime = "xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12"
        elif any("xl/workbook.xml" in member or "xl/worksheets/" in member for member in members):
            detected, mime = "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else:
            detected, mime = "zip", "application/zip"
    elif raw.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE2 复合文档：.xls/.doc/.ppt 均为该头，按声明扩展名细分。
        detected = "ole2"
        if suffix == ".doc":
            detected, mime = "doc", "application/msword"
        elif suffix == ".ppt":
            detected, mime = "ppt", "application/vnd.ms-powerpoint"
        else:
            detected, mime = "xls", "application/vnd.ms-excel"
    elif raw.startswith(b"\x7fELF") or raw.startswith(b"MZ"):
        detected = "binary"
    elif _looks_like_text(raw):
        detected = "text"
        if suffix == ".json" and _is_json_text(raw):
            detected, mime = "json", "application/json"
        elif suffix in {".csv", ".tsv"}:
            detected, mime = suffix.lstrip("."), "text/csv" if suffix == ".csv" else "text/tab-separated-values"
        else:
            detected, mime = "text", "text/plain"

    declared = suffix.lstrip(".")
    if declared in {"csv", "tsv", "txt", "md", "json"}:
        match = detected in {"text", declared, "json"}
    elif declared in {"xls", "doc", "ppt"}:
        match = detected == declared or detected == "ole2"
    elif declared in {"xlsx", "xlsm"}:
        # xlsx/xlsm 同族：openpyxl 均可打开，仅宏能力不同；纯 zip 不算 workbook。
        match = detected in {"xlsx", "xlsm"}
    else:
        match = detected == declared
    reason = ""
    if not match:
        reason = f"mismatch: 声明 {declared or '无扩展名'}，实际 {detected}"
    return FormatVerdict(
        declared_suffix=suffix,
        detected_format=detected,
        mime_type=mime,
        match=match,
        reason=reason,
    )


def _looks_like_text(raw: bytes) -> bool:
    if not raw:
        return True
    sample = raw[:4096]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            sample.decode("gb18030")
            return True
        except UnicodeDecodeError:
            return False


def _is_json_text(raw: bytes) -> bool:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
        return isinstance(value, (dict, list))
    except Exception:
        return False


def sniff_documents(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量返回逐文件嗅探结果：filename/mode/verdict/mime/match。"""
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        encoded = item.get("content_b64")
        filename = str(item.get("filename") or "upload.bin")
        if not isinstance(encoded, str) or not encoded.strip():
            out.append({"filename": filename, "status": "skipped", "reason": "missing_content_b64"})
            continue
        import base64

        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            out.append({"filename": filename, "status": "parse_failed", "reason": "invalid_base64"})
            continue
        verdict = sniff_format(raw, filename)
        out.append({
            "filename": filename,
            "status": "accepted" if verdict.match else "unsupported",
            "reason": verdict.reason,
            "detected_format": verdict.detected_format,
            "mime_type": verdict.mime_type,
        })
    return out
