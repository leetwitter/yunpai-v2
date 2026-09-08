from __future__ import annotations

import io
import zipfile

import pytest

from yunpai_orchestrator.archive_extract import extract_archive_safe, unpack_archive


def test_unpack_zip_lists_members_with_sha_and_parent():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("订单/order1.xlsx", b"PK-content-not-real-but-listed")
        zf.writestr("notes.txt", b"hello")
    raw = buffer.getvalue()
    result = unpack_archive(raw, filename="batch.zip")
    assert result["archive_format"] == "zip"
    assert result["error"] is None
    paths = [member["relative_path"] for member in result["members"]]
    assert "订单/order1.xlsx" in paths
    assert "notes.txt" in paths
    assert all(member["status"] == "accepted" for member in result["members"] if member["status"] != "skipped")
    assert result["members"][0]["sha256"]


def test_unpack_archive_rejects_zip_slip_paths():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("../../evil.sh", b"boom")
    result = unpack_archive(buffer.getvalue(), filename="evil.zip")
    assert result["error"] is None
    bad = [member for member in result["members"] if member["relative_path"] == "../../evil.sh"]
    assert bad and bad[0]["status"] == "skipped"
    assert bad[0]["reason"] == "unsafe_member_path"


def test_unpack_archive_rar_is_unsupported_without_dependency():
    result = unpack_archive(b"Rar!\x1a\x07\x00rest", filename="a.rar")
    assert result["archive_format"] == "rar"
    assert "unsupported" in result["error"]


def test_extract_archive_safe_writes_only_safe_members(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("dir/a.json", b'{"records":[]}')
        zf.writestr("dir/sub/b.json", b'{"records":[]}')
    destination = tmp_path / "unpacked"
    result = extract_archive_safe(buffer.getvalue(), destination, filename="batch.zip")
    assert result["error"] is None
    assert (destination / "dir" / "a.json").exists()
    assert (destination / "dir" / "sub" / "b.json").exists()
    assert {member["relative_path"] for member in result["extracted_members"]} == {"dir/a.json", "dir/sub/b.json"}


@pytest.mark.skipif(__import__("importlib").util.find_spec("py7zr") is None, reason="py7zr not installed")
def test_unpack_7z_roundtrip():
    import py7zr

    buffer = io.BytesIO()
    with py7zr.SevenZipFile(buffer, "w") as archive:
        archive.writestr("a,b\n1,2\n", "member.csv")
    raw = buffer.getvalue()
    result = unpack_archive(raw, filename="a.7z")
    assert result["archive_format"] == "7z"
    assert result["error"] is None
    assert any(member["relative_path"] == "member.csv" and member["status"] == "accepted" for member in result["members"])
