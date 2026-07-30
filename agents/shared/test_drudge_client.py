#!/usr/bin/env python3
"""Tests for agents/shared/drudge_client.py, especially the write-readiness preflight."""
from __future__ import annotations

import io
import json
import urllib.error
from typing import Any
from unittest.mock import patch

import pytest

from drudge_client import DrudgeClient, DrudgeNotWritableError, check_drudge_writable


def _json_response(payload: dict[str, Any]) -> Any:
    """Build a minimal urllib response double."""
    resp = io.BytesIO(json.dumps(payload).encode("utf-8"))
    resp.status = 200  # some code paths may read this
    return resp


class TestCheckDrudgeWritable:
    def test_legacy_response_without_db_healthy_passes(self):
        with patch(
            "drudge_client.urllib.request.urlopen",
            return_value=_json_response({"status": "ok", "vector": False}),
        ):
            check_drudge_writable()  # should not raise

    def test_healthy_vector_mode_passes(self):
        with patch(
            "drudge_client.urllib.request.urlopen",
            return_value=_json_response(
                {"status": "ok", "vector": True, "db_healthy": True}
            ),
        ):
            check_drudge_writable()

    def test_degraded_status_blocks(self):
        with patch(
            "drudge_client.urllib.request.urlopen",
            return_value=_json_response(
                {"status": "degraded", "vector": True, "db_healthy": False}
            ),
        ), pytest.raises(DrudgeNotWritableError, match="degraded"):
            check_drudge_writable()

    def test_db_unhealthy_blocks(self):
        with patch(
            "drudge_client.urllib.request.urlopen",
            return_value=_json_response(
                {"status": "ok", "vector": True, "db_healthy": False}
            ),
        ), pytest.raises(DrudgeNotWritableError, match="not healthy"):
            check_drudge_writable()

    def test_unreachable_health_blocks(self):
        with patch(
            "drudge_client.urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ), pytest.raises(DrudgeNotWritableError, match="unreachable"):
            check_drudge_writable()

    def test_accepts_explicit_client(self):
        client = DrudgeClient(base_url="http://example.com")
        with patch(
            "drudge_client.urllib.request.urlopen",
            return_value=_json_response(
                {"status": "ok", "vector": True, "db_healthy": True}
            ),
        ):
            check_drudge_writable(client)
