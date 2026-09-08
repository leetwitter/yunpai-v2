from __future__ import annotations

import json
from unittest.mock import patch

from yunpai_orchestrator.m0_catalog import publish_records


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _records():
    return [{"entity_type": "order", "canonical_key": "W-H909"}]


def test_publish_requires_canonical_readback(monkeypatch):
    monkeypatch.setenv("M0_URL", "http://m0.test")
    responses = iter([
        _Response({"data": {"publishable": True}}),
        _Response({"data": {"status": "published"}}),
    ])
    with patch("yunpai_orchestrator.m0_catalog.urlopen", side_effect=lambda *args, **kwargs: next(responses)):
        result = publish_records(_records(), tenant_id="t", task_id="task")
    assert result["status"] == "failed"
    assert result["error"] == "canonical_readback_unverified"


def test_publish_rejects_zero_canonical_readback(monkeypatch):
    monkeypatch.setenv("M0_URL", "http://m0.test")
    responses = iter([
        _Response({"data": {"publishable": True}}),
        _Response({
            "data": {
                "status": "published",
                "approved_candidates": 0,
                "ledger_count": 0,
                "outbox_count": 0,
            }
        }),
    ])
    with patch("yunpai_orchestrator.m0_catalog.urlopen", side_effect=lambda *args, **kwargs: next(responses)):
        result = publish_records(_records(), tenant_id="t", task_id="task")
    assert result["status"] == "failed"
    assert result["error"] == "canonical_readback_incomplete"


def test_publish_returns_verified_counts(monkeypatch):
    monkeypatch.setenv("M0_URL", "http://m0.test")
    responses = iter([
        _Response({"data": {"publishable": True}}),
        _Response({
            "data": {
                "status": "published",
                "approved_candidates": 1,
                "ledger_count": 1,
                "outbox_count": 1,
            }
        }),
    ])
    with patch("yunpai_orchestrator.m0_catalog.urlopen", side_effect=lambda *args, **kwargs: next(responses)):
        result = publish_records(_records(), tenant_id="t", task_id="task")
    assert result["status"] == "published"
    assert result["counts"] == {
        "approved_candidates": 1,
        "ledger_count": 1,
        "outbox_count": 1,
    }
