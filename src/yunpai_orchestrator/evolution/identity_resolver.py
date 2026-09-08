from __future__ import annotations

import re
from typing import Any

# 型号编码形态（W-H410 / FC-15 / RH0006 等）。确定性前缀+数字/字母组合。
_CODE_RE = re.compile(r"\b[A-Z]{1,4}-[A-Z0-9]{2,}\b")
# 供应商承认书/规格书里常见的图号/料号（80806-129、YA.06.0011、RH0006）。
_PARTNO_RE = re.compile(r"\b(?:\d{4,6}-\d{3}|YA\.[A-Z0-9.]+|RH\d{4}|[A-Z]{2,4}\d{4,})\b")

# 规格签名关键词（接口 / 版本 / 壳材 / 特征）。用于图纸文件名 → 产品族匹配。
_SIGNATURE_KEYWORDS = {
    "interface": ("HDMI", "DP", "USB", "TYPE-C", "TYPEC", "网线", "RJ45", "水晶头"),
    "version": ("2.0", "2.1", "3.0", "3.1", "4K", "8K", "HDTV", "5类", "6类", "7类", "六类", "七类"),
    "shell": ("铝合金", "铝壳", "铁壳", "塑胶", "铜", "镀金", "不锈钢"),
    "feature": ("光纤", "菱形", "弧形", "扁线", "屏蔽", "编织", "防尘盖", "机箱"),
}


def extract_product_codes(text: str) -> list[str]:
    return _CODE_RE.findall(text or "")


def extract_part_numbers(text: str) -> list[str]:
    return _PARTNO_RE.findall(text or "")


def spec_signature(text: str) -> dict[str, list[str]]:
    """从名称/文件名提取规格签名（确定性关键词，无 LLM）。"""
    upper = (text or "").upper()
    signature: dict[str, list[str]] = {}
    for field, keywords in _SIGNATURE_KEYWORDS.items():
        hits = [kw for kw in keywords if kw.upper() in upper]
        if hits:
            signature[field] = hits
    return signature


def resolve_product_code(text: str, *, known_codes: list[str] | None = None,
                         aliases: dict[str, str] | None = None) -> dict[str, Any]:
    """五级匹配链的确定性部分（M1~M4）；M5 LLM 判断由调用方兜底。

    返回 {product_code, method, confidence} 或 {product_code: "", method: "unresolved"}。
    """
    known = set(known_codes or [])
    aliases = aliases or {}
    # M1 精确编码
    for code in extract_product_codes(text):
        if code in known:
            return {"product_code": code, "method": "exact_code", "confidence": 1.0}
    # M2 别名
    for alias, code in aliases.items():
        if alias in text:
            return {"product_code": code, "method": "alias", "confidence": 1.0}
    # M3 规格签名（族级，无唯一型号）
    signature = spec_signature(text)
    if signature:
        return {"product_code": "", "method": "spec_signature", "confidence": 0.6,
                "signature": signature}
    # M4 无法确定 → 交由人工 Gate。
    return {"product_code": "", "method": "unresolved", "confidence": 0.0}
