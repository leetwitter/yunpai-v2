"""SOP 理解管线（U2 管线 B）。

80806-129.pdf 结构极其规整：42 页 = 1 页总流程图 + 41 页工站页（页内含 "N OF M"）。
工站切分是确定性正则，零 LLM；只有顺序推断这种 hard 任务才需要 DeepSeek 兜底。
"""
from __future__ import annotations

import re
from typing import Any

from ..merge_engine import merge_field
from ..repository import EvolutionRepository

_STATION_MARK_RE = re.compile(r"(\d+)\s*OF\s*(\d+)")


def split_station_pages(page_texts: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """从 (页码, 文本) 列表切分工站页。返回按工站号排序的 stations。"""
    stations: list[dict[str, Any]] = []
    for page_index, text in page_texts:
        match = _STATION_MARK_RE.search(text or "")
        if not match:
            continue
        station_no = int(match.group(1))
        total = int(match.group(2))
        name = _first_meaningful_line(text, exclude={"OF"})
        stations.append({"seq": station_no, "total": total, "name": name,
                         "page": page_index, "raw_text": (text or "")[:2000]})
    stations.sort(key=lambda s: s["seq"])
    return stations


def _first_meaningful_line(text: str, *, exclude: set[str]) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if any(token.lower() in line.lower() for token in exclude):
            continue
        if len(line) <= 80 and not line.isdigit():
            return line
    return ""


def parse_sop_pdf(pages: list[str]) -> list[dict[str, Any]]:
    """pages = 每页文本字符串；首页（总流程图）无 'N OF M' 会被自动跳过。"""
    return split_station_pages([(i, text) for i, text in enumerate(pages)])


def parse_sop_xls(raw: bytes, filename: str = "") -> dict[str, Any]:
    """xls 型 SOP：表头词典定位步骤表。返回 {steps: [...]}。"""
    import io

    import xlrd

    steps: list[dict[str, Any]] = []
    book = xlrd.open_workbook(file_contents=raw)
    for sheet in book.sheets():
        header_index, mapping = _xls_header(sheet)
        if header_index is None:
            continue
        for r in range(header_index + 1, sheet.nrows):
            values = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
            seq = values[mapping["seq"]] if mapping.get("seq") is not None else str(r - header_index)
            name = values[mapping["name"]] if mapping.get("name") is not None else ""
            if not name or name in {"合计", "总计"}:
                continue
            steps.append({
                "seq": seq, "name": name,
                "std_minutes": _xls_std_minutes(values, mapping, sheet, r),
                "sheet": sheet.name,
            })
    return {"steps": steps, "filename": filename}


def _xls_header(sheet: Any) -> tuple[int | None, dict[str, int | None]]:
    synonyms = {"seq": ("工步", "序号", "步骤号", "顺序"), "name": ("步骤", "作业内容", "工步内容", "操作内容"),
                "std_minutes": ("IE工时", "工时", "标准工时")}
    for r in range(min(sheet.nrows, 20)):
        row = [str(sheet.cell_value(r, c)).strip() for c in range(sheet.ncols)]
        mapping: dict[str, int | None] = {"seq": None, "name": None, "std_minutes": None}
        for field, keys in synonyms.items():
            for c, cell in enumerate(row):
                if cell and any(k in cell for k in keys):
                    mapping[field] = c
                    break
        if mapping["name"] is not None:
            return r, mapping
    return None, {"seq": None, "name": None, "std_minutes": None}


def _xls_std_minutes(values: list[str], mapping: dict[str, int | None], sheet: Any, row: int) -> float | None:
    col = mapping.get("std_minutes")
    if col is None:
        return None
    cell = sheet.cell_value(row, col)
    if isinstance(cell, (int, float)):
        minutes = float(cell)
        return round(minutes / 60.0, 2) if minutes > 60 else round(minutes, 2)
    try:
        return round(float(str(cell).strip()), 2)
    except (ValueError, TypeError):
        return None


def ingest_sop_profiles(repo: EvolutionRepository, *, tenant_id: str, product_code: str,
                        stations: list[dict[str, Any]], document_ref: str = "") -> dict[str, Any]:
    existing = repo.get_profile(tenant_id=tenant_id, product_code=product_code)
    profile_id = str(existing["profile_id"]) if existing else f"PU-{product_code}"
    actions = {"candidate_created": 0, "reinforced": 0, "conflict": 0}
    for station in stations:
        key = f"{station.get('seq')}-{station.get('name')}"
        outcome = merge_field(
            repo, tenant_id=tenant_id, profile_id=profile_id, field_path="process",
            key=key, value={"seq": station.get("seq"), "name": station.get("name"),
                            "std_minutes": station.get("std_minutes"),
                            "order_source": "station_page"},
            source_kind="sop_document", document_ref=document_ref,
            locator={"page": station.get("page"), "seq": station.get("seq")},
        )
        actions[outcome["action"]] += 1
    from ..merge_engine import confidence as _conf

    confirmed = [e["value"] for e in repo.list_field_evidence(profile_id=profile_id, field_path="process", limit=500)
                 if e["status"] != "conflict"]
    ranks = {"low": 0.33, "medium": 0.66, "high": 1.0}
    conf = round(sum(ranks.get(_conf(int(e.get("independent_count") or 1)), 0.33)
                     for e in repo.list_field_evidence(profile_id=profile_id, field_path="process", limit=500)) / max(1, len(confirmed)), 2)
    repo.upsert_profile(profile_id=profile_id, tenant_id=tenant_id, product_code=product_code,
                        process=confirmed, confidence=conf,
                        version=int(existing["version"]) + 1 if existing else 1)
    return {"product_code": product_code, "stations": len(stations), "actions": actions}


async def ingest_sop_profiles_llm(repo: EvolutionRepository, llm: Any, *, tenant_id: str,
                                  product_code: str, stations: list[dict[str, Any]],
                                  document_ref: str = "") -> dict[str, Any]:
    """SOP 理解 + 27B 工序名归一（P2）。LLM 失败回退保留原工序名。"""
    names = [str(s.get("name") or "") for s in stations]
    if names and getattr(llm, "enabled", False):
        normalized = await llm.normalize_operation_names(names)
        for station, item in zip(stations, normalized):
            station["name"] = str(item.get("canonical_name") or station.get("name") or "")
            station["normalized_confidence"] = item.get("confidence")
    return ingest_sop_profiles(repo, tenant_id=tenant_id, product_code=product_code,
                               stations=stations, document_ref=document_ref)
