"""Small HTTP client for publishing normalized upload facts to M0."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote
from typing import Any


def publish_records(
    records: list[dict[str, Any]],
    *,
    tenant_id: str,
    task_id: str,
    actor: str = "operator",
    human_override: bool = False,
    override_reason: str = "",
) -> dict[str, Any]:
    """Validate then publish records through the configured M0 HTTP service.

    An unset M0_URL is a local/unit-test configuration and is reported as a
    skipped publication.  A configured service must complete both calls; any
    error is returned to the gate caller so it can remain recoverable.
    """
    if not records:
        return {"status": "skipped", "published": 0, "reason": "no canonical records"}
    base = str(os.getenv("M0_URL") or "").rstrip("/")
    if not base:
        return {"status": "skipped", "published": 0, "reason": "M0_URL is not configured"}
    body = json.dumps({
        "records": records,
        "task_id": task_id,
        "approval": {
            "mode": "human_override" if human_override else "standard",
            "approved_by": actor,
            "reason": override_reason,
        },
    }, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Tenant-ID": tenant_id,
        "X-Yunpai-Tenant": tenant_id,
        "X-Yunpai-Task-ID": task_id,
        "X-Yunpai-Principal": actor,
        "X-Yunpai-Roles": "data-steward,m0-reviewer,admin",
        "X-Yunpai-Approval-Mode": "human_override" if human_override else "standard",
    }
    result: dict[str, Any] = {}
    try:
        for endpoint in ("/api/m0/catalog/ingest/validate", "/api/m0/catalog/ingest/publish"):
            request = Request(f"{base}{endpoint}", data=body, headers=headers, method="POST")
            with urlopen(request, timeout=float(os.getenv("M0_CATALOG_TIMEOUT_S", "8"))) as response:
                raw = response.read()
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                result = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
            if endpoint.endswith("/validate") and isinstance(result, dict) and result.get("publishable") is False:
                return {"status": "rejected", "published": 0, "validation": result}
        # A successful HTTP response is not proof that canonical facts were
        # persisted.  The dedicated M0 backend returns these counters from
        # its transaction/readback boundary; reject ambiguous responses so a
        # candidate Gate cannot be marked complete while PostgreSQL is empty.
        required_counts = ("approved_candidates", "ledger_count", "outbox_count")
        if not isinstance(result, dict) or any(key not in result for key in required_counts):
            return {
                "status": "failed",
                "published": 0,
                "error": "canonical_readback_unverified",
                "result": result,
            }
        counts = {key: int(result.get(key) or 0) for key in required_counts}
        if counts["approved_candidates"] < len(records) or counts["ledger_count"] < len(records) or counts["outbox_count"] < len(records):
            return {
                "status": "failed",
                "published": 0,
                "error": "canonical_readback_incomplete",
                "expected": len(records),
                "counts": counts,
                "result": result,
            }
        return {"status": "published", "published": len(records), "counts": counts, "result": result}
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        return {"status": "failed", "published": 0, "error": str(exc), "record_count": len(records)}
