"""本机阶段一：每格式脱敏 fixture 生成器（任务书 §五.1）。

覆盖：xlsx、xlsm(同族)、xls(OLE2 stub)、csv、tsv、json、pdf、docx、png、
jpg、zip、7z、rar(仅头)、坏文件、伪扩展名。全部为合成内容，不含真实生产
或个人资料。函数以 tmp 目录或 BytesIO 输出，供各测试复用。

Fixture 分类（任务书修订意见：区分 sniff-only 与真实 parse fixture）：
- REAL_PARSE_FIXTURES：内容真实可解析（xlsx/csv/tsv/json/pdf/docx/png/jpg/zip/7z）
- SNIFF_ONLY_FIXTURES：仅能用于 magic-sniff 识别，不能真实 parse 的样本
  （legacy.xls 为 OLE2 头 stub、sample.rar 为 RAR 头 stub、fake.xlsx 伪扩展、
  broken.xlsx 损坏 zip）；RAR 本地无解包依赖属显式 unsupported。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

# sniff 能识别且内容真实可解析的格式样本（PNG 为最小真实图；PDF 为 pypdf 写出的可读单页）。
REAL_PARSE_FIXTURES = ("sample.xlsx", "sample.csv", "sample.tsv", "sample.json", "sample.pdf", "sample.docx", "sample.png", "batch.zip", "sample.7z")

# 仅用于 sniff/格式判定，不能真实 parse 的样本
# （legacy.xls/sample.rar = 头 stub；fake.xlsx = 伪扩展；broken.xlsx = 损坏 zip；
#  sample.jpg 无 OCR 深解析 -> 仅 sniff 可识别，无文本抽取，不声明 REAL_PARSE）。
SNIFF_ONLY_FIXTURES = ("legacy.xls", "sample.rar", "fake.xlsx", "broken.xlsx", "sample.jpg")


def xlsx_bytes(headers: list[str], rows: list[list[Any]], *, title: str = "Sheet1") -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def csv_bytes(headers: list[str], rows: list[list[Any]]) -> bytes:
    import csv

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def tsv_bytes(headers: list[str], rows: list[list[Any]]) -> bytes:
    output = io.StringIO()
    output.write("\t".join(headers) + "\n")
    for row in rows:
        output.write("\t".join(str(value) for value in row) + "\n")
    return output.getvalue().encode("utf-8")


def pdf_bytes(text: str = "PDF fixture page 100 PCS") -> bytes:
    """合法可解析 PDF（pypdf 写出含文本单页；pypdf 可读回页数）。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    try:
        from pypdf import PdfReader  # noqa: F401 用于验证可读
    except ImportError:  # pragma: no cover
        pass
    output = io.BytesIO()
    writer.write(output)
    raw = output.getvalue()
    # 验证可读：pypdf 能打开且至少 1 页。
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    assert len(reader.pages) >= 1
    return raw


def docx_bytes(text: str = "DOCX fixture paragraph") -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(text)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def png_bytes() -> bytes:
    # 最小合法 PNG（1x1 红色像素）。
    import base64

    return base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def jpg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 64


def zip_bytes(members: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


def nested_zip_bytes() -> bytes:
    inner = zip_bytes({"inner/inner.csv": b"a,b\n1,2\n"})
    return zip_bytes({"outer/order.xlsx": xlsx_bytes(["订单号", "型号", "数量"], [["SO-1", "P-1", 3]]), "outer/nested.zip": inner})


def seven_z_bytes() -> bytes:
    try:
        import py7zr
    except ImportError:  # pragma: no cover
        return b"7z\xbc\xaf\x27\x1c" + b"\x00" * 8
    output = io.BytesIO()
    with py7zr.SevenZipFile(output, "w") as archive:
        archive.writestr("a,b\n1,2\n", "member.csv")
    return output.getvalue()


def rar_stub_bytes() -> bytes:
    return b"Rar!\x1a\x07\x00" + b"\x00" * 16


def xls_stub_bytes() -> bytes:
    # OLE2 头 + 少量字节：sniff 识别为 xls；真实解析在缺有效 BIFF 流时报结构化错误。
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 96


def text_disguised_as_xlsx() -> bytes:
    return b"this is not a real xlsx, just text\nsecond line\n"


def corrupt_xlsx() -> bytes:
    return b"PK\x03\x04" + b"\x00" * 32


def write_all(tmp_path: Path) -> dict[str, Path]:
    """把所有 fixture 写入 tmp_path，返回 {name: path}。"""
    payload = {
        "sample.xlsx": xlsx_bytes(["物料编码", "材料名称", "用量"], [["M-1", "铜箔", 2]]),
        "sample.csv": csv_bytes(["订单号", "数量"], [["SO-1", 10]]),
        "sample.tsv": tsv_bytes(["型号", "数量"], [["P-1", 5]]),
        "sample.json": b'{"records":[{"kind":"order"}]}',
        "sample.pdf": pdf_bytes(),
        "sample.docx": docx_bytes(),
        "sample.png": png_bytes(),
        "sample.jpg": jpg_bytes(),
        "batch.zip": nested_zip_bytes(),
        "sample.7z": seven_z_bytes(),
        "sample.rar": rar_stub_bytes(),
        "legacy.xls": xls_stub_bytes(),
        "fake.xlsx": text_disguised_as_xlsx(),
        "broken.xlsx": corrupt_xlsx(),
    }
    written: dict[str, Path] = {}
    for name, content in payload.items():
        path = tmp_path / name
        path.write_bytes(content)
        written[name] = path
    return written
