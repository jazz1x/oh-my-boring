#!/usr/bin/env python3
"""Regression tests for recall_core.py session throttle."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock
import io

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.pop("BORING_CONFIG", None)
os.environ.pop("BORING_HOME", None)

import recall_core


RECALL_ENV_KEYS = (
    "RECALL_MAX_RESULTS",
    "RECALL_MAX_TOKENS",
    "RECALL_TIMEOUT",
    "RECALL_RETRIES",
    "RECALL_SESSION_THROTTLE_SECONDS",
)


def _tmp_throttle():
    """Point the throttle file at a temp location for isolated tests."""
    d = tempfile.mkdtemp()
    recall_core._throttle_path = lambda: os.path.join(d, "throttle.json")


def _restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def _value_error_from(call):
    try:
        call()
    except ValueError as e:
        return str(e)
    raise AssertionError("expected ValueError")


def _clear_recall_overrides():
    recall_core.MAX_RESULTS = None
    recall_core.MAX_TOKENS = None
    recall_core.TIMEOUT = None
    recall_core.RETRIES = None
    recall_core.SESSION_THROTTLE_SECONDS = None


def test_session_throttle_blocks_repeated_calls():
    _tmp_throttle()
    assert recall_core._session_throttled("s1") is False
    assert recall_core._session_throttled("s1") is True
    assert recall_core._session_throttled("s2") is False


def test_session_throttle_expires_after_window():
    _tmp_throttle()
    original_ttl = recall_core.SESSION_THROTTLE_SECONDS
    try:
        recall_core.SESSION_THROTTLE_SECONDS = 1
        assert recall_core._session_throttled("s3") is False
        assert recall_core._session_throttled("s3") is True
        # Sleep past the 1-second window.
        import time

        time.sleep(1.1)
        assert recall_core._session_throttled("s3") is False
    finally:
        recall_core.SESSION_THROTTLE_SECONDS = original_ttl


def test_empty_session_id_never_throttled():
    _tmp_throttle()
    assert recall_core._session_throttled(None) is False
    assert recall_core._session_throttled("") is False


def test_recall_policy_rejects_invalid_context_budget():
    old = {name: os.environ.get(name) for name in RECALL_ENV_KEYS}
    try:
        _clear_recall_overrides()
        os.environ["RECALL_MAX_TOKENS"] = "-1"
        msg = _value_error_from(recall_core._recall_policy)
        assert "RECALL_MAX_TOKENS must be a positive integer" in msg
    finally:
        for name, value in old.items():
            _restore_env(name, value)
        _clear_recall_overrides()


def test_recall_policy_allows_zero_retries_and_zero_session_throttle():
    old = {name: os.environ.get(name) for name in RECALL_ENV_KEYS}
    try:
        _clear_recall_overrides()
        os.environ["RECALL_RETRIES"] = "0"
        os.environ["RECALL_SESSION_THROTTLE_SECONDS"] = "0"
        _max_results, _max_tokens, _timeout, retries = recall_core._recall_policy()
        assert retries == 0
        assert recall_core._session_throttle_seconds() == 0
    finally:
        for name, value in old.items():
            _restore_env(name, value)
        _clear_recall_overrides()


def test_run_recall_reports_invalid_policy_before_search():
    old = {name: os.environ.get(name) for name in RECALL_ENV_KEYS}
    stderr = io.StringIO()
    try:
        _clear_recall_overrides()
        os.environ["RECALL_MAX_RESULTS"] = "0"
        with mock.patch.object(recall_core.sys, "stderr", stderr), \
             mock.patch.object(recall_core.DrudgeClient, "search") as search:
            recall_core.run_recall({"prompt": "a long enough prompt"})
        search.assert_not_called()
        assert "RECALL_MAX_RESULTS must be a positive integer" in stderr.getvalue()
    finally:
        for name, value in old.items():
            _restore_env(name, value)
        _clear_recall_overrides()


def test_run_recall_wraps_snippets_in_data_fence_and_collapses_metadata():
    old = {name: os.environ.get(name) for name in RECALL_ENV_KEYS}
    captured = io.StringIO()
    try:
        _clear_recall_overrides()
        with mock.patch.object(recall_core.sys, "stdout", captured), \
             mock.patch.object(
                 recall_core.DrudgeClient,
                 "search",
                 return_value=[
                     {
                         "source_path": "vault/wiki/wiki-0007.md\n## forged source",
                         "snippet": "fixed cache\n# forged instructions",
                     }
                 ],
             ):
            recall_core.run_recall({"prompt": "a long enough prompt"})
    finally:
        for name, value in old.items():
            _restore_env(name, value)
        _clear_recall_overrides()

    payload = json.loads(captured.getvalue())
    ctx = payload["hookSpecificOutput"]["additionalContext"]

    assert "«UNTRUSTED-DATA " in ctx
    assert "«/UNTRUSTED-DATA " in ctx
    assert "[wiki-0007.md ## forged source]" in ctx
    assert "fixed cache # forged instructions" in ctx
    assert "\n# forged instructions" not in ctx
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


if __name__ == "__main__":
    test_session_throttle_blocks_repeated_calls()
    test_session_throttle_expires_after_window()
    test_empty_session_id_never_throttled()
    test_recall_policy_rejects_invalid_context_budget()
    test_recall_policy_allows_zero_retries_and_zero_session_throttle()
    test_run_recall_reports_invalid_policy_before_search()
    test_run_recall_wraps_snippets_in_data_fence_and_collapses_metadata()
    print("ok - recall_core session throttle")
