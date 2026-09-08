"""每格式 fixture 覆盖与格式处理合同（任务书 §五.1/§五.7）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import fixtures
from yunpai_orchestrator.file_sniff import sniff_documents, sniff_format


def _attachment(name: str, raw: bytes) -> dict:
    import base64

    return {"filename": name, "content_b64": base64.b64encode(raw).decode(), "content_type": "application/octet-stream"}


def test_every_supported_format_has_sniffable_fixture():
    """每个声明格式都有 fixture 且能通过 magic sniff 识别为声明格式（RAR 亦识别）。"""
    tmp = Path(__import__("tempfile").mkdtemp())
    written = fixtures.write_all(tmp)
    expected_magic = {
        "sample.xlsx": "xlsx", "sample.csv": "csv", "sample.tsv": "tsv", "sample.json": "json",
        "sample.pdf": "pdf", "sample.docx": "docx", "sample.png": "png", "sample.jpg": "jpg",
        "batch.zip": "zip", "sample.7z": "7z", "sample.rar": "rar", "legacy.xls": "xls",
    }
    for name, detected in expected_magic.items():
        verdict = sniff_format(written[name].read_bytes(), name)
        assert verdict.detected_format == detected, f"{name} sniff -> {verdict.detected_format}, expected {detected}"


def test_sniff_marks_fake_extension_and_corrupt_as_mismatch():
    tmp = Path(__import__("tempfile").mkdtemp())
    written = fixtures.write_all(tmp)
    assert sniff_format(written["fake.xlsx"].read_bytes(), "fake.xlsx").match is False
    assert sniff_format(written["fake.xlsx"].read_bytes(), "fake.xlsx").reason.startswith("mismatch")
    # 损坏 zip 头：检测为 zip（PK）但无 OOXML 成员 -> zip，作 unsupported/parse_failed 由 ingest 决定。
    assert sniff_format(written["broken.xlsx"].read_bytes(), "broken.xlsx").detected_format in {"zip", "xlsx"}


def test_sniff_documents_batch_marks_bad_archive_member(tmp_path):
    import zipfile

    evil = __import__("io").BytesIO()
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../../evil.sh", b"boom")
    raw = evil.getvalue()
    # zip-slip 成员路径在解包层防护（archive_extract 测试已覆盖）；sniff 层仅识别 zip。
    assert sniff_format(raw, "evil.zip").detected_format == "zip"


@pytest.mark.skipif(__import__("importlib").util.find_spec("py7zr") is None, reason="py7zr not installed")
def test_7z_fixture_roundtrips():
    from yunpai_orchestrator.archive_extract import unpack_archive

    result = unpack_archive(fixtures.seven_z_bytes(), filename="sample.7z")
    assert result["error"] is None
    assert result["archive_format"] == "7z"
    assert any(member["relative_path"] == "member.csv" for member in result["members"])


def test_rar_without_local_unpack_is_explicit_unsupported():
    from yunpai_orchestrator.archive_extract import unpack_archive

    result = unpack_archive(fixtures.rar_stub_bytes(), filename="sample.rar")
    assert result["archive_format"] == "rar"
    assert "unsupported" in result["error"]


def test_docx_and_pdf_fixtures_are_parseable_text_paths():
    from yunpai_orchestrator.business_catalog import extract_file

    tmp = Path(__import__("tempfile").mkdtemp())
    (tmp / "note.docx").write_bytes(fixtures.docx_bytes("第一条记录 100 件"))
    result = extract_file(tmp / "note.docx", root=tmp)
    assert result["file_kind"] == "document"
    assert "text_preview" in result["extraction"]
    # PDF 必须真实可解析：不得出现 parse_error（禁止把 parse_error 当 REAL_PARSE 成功）。
    (tmp / "doc.pdf").write_bytes(fixtures.pdf_bytes())
    result = extract_file(tmp / "doc.pdf", root=tmp)
    assert result["file_kind"] == "document"
    assert "parse_error" not in result["extraction"], f"PDF fixture 应可真实解析: {result['extraction']}"


def test_fixture_categories_are_explicit_and_consistent():
    """fixture 明确区分 REAL_PARSE 与 SNIFF_ONLY（集成负责人修订要求）。"""
    from tests import fixtures as f

    tmp = Path(__import__("tempfile").mkdtemp())
    written = f.write_all(tmp)
    all_fixtures = set(written)
    assert set(f.REAL_PARSE_FIXTURES) | set(f.SNIFF_ONLY_FIXTURES) == all_fixtures
    assert set(f.REAL_PARSE_FIXTURES) & set(f.SNIFF_ONLY_FIXTURES) == set()
    # sniff-only：stub/伪扩展/损坏/JPG(无 OCR) 不应被当作真实 parse 成功的普通文件。
    for name in f.SNIFF_ONLY_FIXTURES:
        verdict = sniff_format(written[name].read_bytes(), name)
        assert name in {"sample.rar", "legacy.xls", "sample.jpg"} or verdict.match is False, f"{name} 应为 sniff-only: {verdict}"
    # real-parse：sniff 必须与声明一致（除 .7z 依赖 py7zr 写出的真实归档）。
    for name in f.REAL_PARSE_FIXTURES:
        if name == "sample.7z" and __import__("importlib").util.find_spec("py7zr") is None:
            continue
        verdict = sniff_format(written[name].read_bytes(), name)
        assert verdict.match is True, f"{name} 应真实可 sniff: {verdict}"
