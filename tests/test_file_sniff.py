from __future__ import annotations

import io
import zipfile

import pytest

from yunpai_orchestrator.file_sniff import sniff_documents, sniff_format


def test_sniff_detects_xlsx_by_magic_even_with_plain_name():
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active["A1"] = "物料编码"
    output = io.BytesIO()
    workbook.save(output)
    verdict = sniff_format(output.getvalue(), "weird-name.bin")
    assert verdict.detected_format == "xlsx"
    assert verdict.match is False  # 扩展名不匹配 -> mismatch


def test_sniff_rejects_text_disguised_as_xlsx():
    verdict = sniff_format(b"order_id,quantity\nSO-1,1\n", "fake.xlsx")
    assert verdict.detected_format == "text"
    assert verdict.match is False
    assert "mismatch" in verdict.reason


def test_sniff_accepts_true_xlsx_with_xlsx_name():
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active["A1"] = "x"
    output = io.BytesIO()
    workbook.save(output)
    verdict = sniff_format(output.getvalue(), "bom.xlsx")
    assert verdict.detected_format == "xlsx"
    assert verdict.match is True


def test_sniff_detects_pdf_png_zip_7z_rar_headers():
    assert sniff_format(b"%PDF-1.7\n...", "doc.pdf").detected_format == "pdf"
    assert sniff_format(b"\x89PNG\r\n\x1a\n....", "img.png").detected_format == "png"
    assert sniff_format(b"\xff\xd8\xff\xe0", "img.jpg").detected_format == "jpg"
    assert sniff_format(b"Rar!\x1a\x07\x00abc", "a.rar").detected_format == "rar"
    assert sniff_format(b"7z\xbc\xaf\x27\x1cdef", "a.7z").detected_format == "7z"


def test_sniff_ole2_xls_matches_xls_extension():
    verdict = sniff_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32, "legacy.xls")
    assert verdict.detected_format == "xls"
    assert verdict.match is True


def test_sniff_documents_batch_marks_missing_b64_skipped():
    results = sniff_documents([{"filename": "a.xlsx"}])
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == "missing_content_b64"


def test_sniff_documents_batch_marks_bad_base64_parse_failed():
    results = sniff_documents([{"filename": "a.xlsx", "content_b64": "!!!bad"}])
    assert results[0]["status"] == "parse_failed"
    assert results[0]["reason"] == "invalid_base64"


def test_sniff_xlsm_and_docx_distinguish_from_plain_zip():
    def minimal_xlsx_zip():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("xl/workbook.xml", "<workbook/>")
        return buffer.getvalue()

    xlsx = minimal_xlsx_zip()
    assert sniff_format(xlsx, "a.xlsx").detected_format == "xlsx"

    docx_buffer = io.BytesIO()
    with zipfile.ZipFile(docx_buffer, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    assert sniff_format(docx_buffer.getvalue(), "a.docx").detected_format == "docx"

    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("data.txt", "hi")
    assert sniff_format(plain.getvalue(), "a.zip").detected_format == "zip"
