#!/usr/bin/env python3
"""Tests for agents/shared/drudge_client.py, especially the write-readiness preflight.

Self-executing plain Python script (no pytest) so scripts/guard.sh can run it directly.
"""
from __future__ import annotations

import io
import json
import urllib.error
from typing import Any
from unittest.mock import patch

from drudge_client import DrudgeClient, DrudgeNotWritableError, check_drudge_writable


def _json_response(payload: dict[str, Any]) -> Any:
    """Build a minimal urllib response double."""
    resp = io.BytesIO(json.dumps(payload).encode("utf-8"))
    resp.status = 200  # some code paths may read this
    return resp


def test_legacy_response_without_db_healthy_passes() -> None:
    with patch(
        "drudge_client.urllib.request.urlopen",
        return_value=_json_response({"status": "ok", "vector": False}),
    ):
        check_drudge_writable()  # should not raise


def test_healthy_vector_mode_passes() -> None:
    with patch(
        "drudge_client.urllib.request.urlopen",
        return_value=_json_response(
            {"status": "ok", "vector": True, "db_healthy": True}
        ),
    ):
        check_drudge_writable()


def test_degraded_status_blocks() -> None:
    with patch(
        "drudge_client.urllib.request.urlopen",
        return_value=_json_response(
            {"status": "degraded", "vector": True, "db_healthy": False}
        ),
    ):
        try:
            check_drudge_writable()
        except DrudgeNotWritableError as exc:
            assert "degraded" in str(exc), f"expected 'degraded' in message, got: {exc}"
        else:
            raise AssertionError("expected DrudgeNotWritableError for degraded status")


def test_db_unhealthy_blocks() -> None:
    with patch(
        "drudge_client.urllib.request.urlopen",
        return_value=_json_response(
            {"status": "ok", "vector": True, "db_healthy": False}
        ),
    ):
        try:
            check_drudge_writable()
        except DrudgeNotWritableError as exc:
            assert "not healthy" in str(exc), f"expected 'not healthy' in message, got: {exc}"
        else:
            raise AssertionError("expected DrudgeNotWritableError for db_healthy=false")


def test_unreachable_health_blocks() -> None:
    with patch(
        "drudge_client.urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        try:
            check_drudge_writable()
        except DrudgeNotWritableError as exc:
            assert "unreachable" in str(exc), f"expected 'unreachable' in message, got: {exc}"
        else:
            raise AssertionError("expected DrudgeNotWritableError for unreachable /health")


def test_accepts_explicit_client() -> None:
    client = DrudgeClient(base_url="http://example.com")
    with patch(
        "drudge_client.urllib.request.urlopen",
        return_value=_json_response(
            {"status": "ok", "vector": True, "db_healthy": True}
        ),
    ):
        check_drudge_writable(client)


if __name__ == "__main__":
    test_legacy_response_without_db_healthy_passes()
    test_healthy_vector_mode_passes()
    test_degraded_status_blocks()
    test_db_unhealthy_blocks()
    test_unreachable_health_blocks()
    test_accepts_explicit_client()
    print("ok - drudge client")
