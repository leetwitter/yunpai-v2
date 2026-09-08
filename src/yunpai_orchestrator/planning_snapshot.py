"""六类 M5 snapshot 的确定性组装与规范化 checksum（任务书 T3）。

Orchestrator 在主链求解前把 M1/M2/M3/M4/canonical 的已批准事实组装成六类
不可变 snapshot：order_snapshots / routes / resource_snapshot /
calendar_snapshot / supply_snapshot / constraint_snapshot。每类必须携带
snapshot_id、正整数 revision、checksum、source_system、source_ref、
source_observed_at、tenant_id/site_id。

checksum 使用规范化 JSON（sort_keys + 紧凑分隔符 + ensure_ascii=False），
禁止对 str(dict) 直接哈希。

本模块是确定性纯函数；不访问数据库，不调用工具，只把已批准事实转换为
符合 pmc_v2_snapshots.validate_bundle 的 bundle 结构。缺权威事实时返回
结构化 BLOCKED_INPUT（missing_fields/source_module/required_tool/recovery）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

SNAPSHOT_KINDS = (
    "order_snapshots",
    "routes",
    "resource_snapshot",
    "calendar_snapshot",
    "supply_snapshot",
    "constraint_snapshot",
)

SIX_CLASS_BUNDLE = "six-class-bundle"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_bytes(value: Any, *, skip: tuple[str, ...] = ("checksum",)) -> bytes:
    """规范化 JSON 序列化：sort_keys、紧凑分隔、保留 unicode，用于 checksum。

    递归删除顶层/嵌套的 ``checksum`` 键，保证 checksum 自指稳定。
    """
    def prune(item: Any) -> Any:
        if isinstance(item, dict):
            return {k: prune(v) for k, v in item.items() if k not in skip}
        if isinstance(item, list):
            return [prune(v) for v in item]
        return item
    return json.dumps(
        prune(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def checksum_of(value: Any) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


def snapshot_header(*, kind: str, snapshot_id: str, revision: int = 1,
                    source_system: str = "orchestrator", source_ref: str = "",
                    source_observed_at: str = "", tenant_id: str = "default",
                    site_id: str = "default") -> dict[str, Any]:
    """为每个快照装配通用头字段。revision 必须为正整数。"""
    if int(revision) < 1:
        raise ValueError("snapshot revision 必须为正整数")
    return {
        "snapshot_kind": kind,
        "snapshot_id": snapshot_id,
        "revision": int(revision),
        "source_system": source_system,
        "source_ref": source_ref,
        "source_observed_at": source_observed_at or _now_iso(),
        "tenant_id": tenant_id,
        "site_id": site_id,
    }


def finalize(snapshot: dict[str, Any]) -> dict[str, Any]:
    """为单个 snapshot 计算并写入 checksum（规范化 JSON）。"""
    snapshot["checksum"] = checksum_of(snapshot)
    return snapshot


def blocked_input(*, missing_fields: list[str], source_module: str,
                  required_tool: str = "", recovery: str = "") -> dict[str, Any]:
    return {
        "success": False,
        "code": "BLOCKED_INPUT",
        "errors": [{
            "code": "BLOCKED_INPUT",
            "message": f"{source_module} 缺少权威输入，无法组装 {SIX_CLASS_BUNDLE}",
            "details": [{"snapshot_kind": SIX_CLASS_BUNDLE}],
        }],
        "data": {
            "missing_fields": sorted(set(missing_fields)),
            "source_module": source_module,
            "required_tool": required_tool,
            "recovery": recovery,
            "snapshot_kind": SIX_CLASS_BUNDLE,
        },
        "trace_id": f"orchestrator:{SIX_CLASS_BUNDLE}",
        "evidence": [{
            "module": "orchestrator", "source_ref": SIX_CLASS_BUNDLE,
            "evidence_ref": f"orchestrator:{SIX_CLASS_BUNDLE}",
            "detail": f"缺少 {sorted(set(missing_fields))}；需要 {source_module} 权威输入",
        }],
    }


def _pick(items: list[Any] | None, kind: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items or []:
        if isinstance(item, dict) and item.get("snapshot_kind") == kind:
            out.append(item)
    return out


def assemble_bundle(*, orders: list[dict[str, Any]], routes: list[dict[str, Any]],
                    resource_snapshot: dict[str, Any] | None,
                    calendar_snapshot: dict[str, Any] | None,
                    supply_snapshot: dict[str, Any] | None,
                    constraint_snapshot: dict[str, Any] | None,
                    tenant_id: str = "default", site_id: str = "default",
                    source_system: str = "orchestrator") -> dict[str, Any]:
    """把已批准事实组装为六类 snapshot bundle（带通用头与 checksum）。

    不做隐式默认：任何必需快照缺失时由调用方（bridge）先返回 BLOCKED_INPUT；
    本函数只负责确定性组装与 checksum 化。
    """
    bundle: dict[str, Any] = {}
    bundle["order_snapshots"] = [
        finalize({**snapshot_header(
            kind="order_snapshots",
            snapshot_id=str(item.get("snapshot_id") or f"SNAP-ORD-{item.get('order_id', '')}-{item.get('order_line_id') or 'L1'}"),
            source_system=source_system,
            source_ref=str(item.get("source_ref") or ""),
            source_observed_at=str(item.get("source_observed_at") or ""),
            tenant_id=tenant_id, site_id=site_id,
        ), **{k: v for k, v in item.items() if k != "checksum"}})
        for item in orders
    ]
    bundle["routes"] = {}
    for route in routes:
        product_code = str(route.get("product_code") or route.get("product_id") or "")
        bundle["routes"][product_code] = finalize({
            **snapshot_header(
                kind="routes",
                snapshot_id=str(route.get("snapshot_id") or f"SNAP-RTE-{product_code}"),
                source_system=source_system,
                source_ref=str(route.get("source_ref") or route.get("route_code") or ""),
                source_observed_at=str(route.get("source_observed_at") or ""),
                tenant_id=tenant_id, site_id=site_id,
            ),
            **{k: v for k, v in route.items() if k != "checksum"},
        })
    for kind, value in (
        ("resource_snapshot", resource_snapshot),
        ("calendar_snapshot", calendar_snapshot),
        ("supply_snapshot", supply_snapshot),
        ("constraint_snapshot", constraint_snapshot),
    ):
        if value is None:
            continue
        bundle[kind] = finalize({
            **snapshot_header(
                kind=kind,
                snapshot_id=str(value.get("snapshot_id") or f"SNAP-{kind.upper().replace('_SNAPSHOT', '')}"),
                source_system=source_system,
                source_ref=str(value.get("source_ref") or ""),
                source_observed_at=str(value.get("source_observed_at") or ""),
                tenant_id=tenant_id, site_id=site_id,
            ),
            **{k: v for k, v in value.items() if k != "checksum"},
        })
    return bundle


def bundle_checksum(bundle: dict[str, Any]) -> str:
    """整包规范化 checksum（确定性组装验证用）。"""
    return checksum_of(bundle)


def snapshot_counts(bundle: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in SNAPSHOT_KINDS:
        value = bundle.get(kind)
        counts[kind] = len(value) if isinstance(value, list) else (1 if isinstance(value, dict) else 0)
    return counts


def verify_bundle(bundle: dict[str, Any]) -> list[str]:
    """返回 bundle 缺失的 snapshot 种类；用于失败关闭。"""
    missing: list[str] = []
    for kind in SNAPSHOT_KINDS:
        value = bundle.get(kind)
        if kind == "order_snapshots":
            if not isinstance(value, list) or not value:
                missing.append(kind)
        elif kind == "routes":
            if not isinstance(value, dict) or not value:
                missing.append(kind)
        elif not isinstance(value, dict) or not value:
            missing.append(kind)
    return missing
