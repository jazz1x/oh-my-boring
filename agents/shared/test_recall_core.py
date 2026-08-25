#!/usr/bin/env python3
"""Regression tests for recall_core.py session throttle."""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.pop("BORING_CONFIG", None)
os.environ.pop("BORING_HOME", None)

import recall_core


def _tmp_throttle():
    """Point the throttle file at a temp location for isolated tests."""
    d = tempfile.mkdtemp()
    recall_core._throttle_path = lambda: os.path.join(d, "throttle.json")


def test_session_throttle_blocks_repeated_calls():
    _tmp_throttle()
    assert recall_core._session_throttled("s1") is False
    assert recall_core._session_throttled("s1") is True
    assert recall_core._session_throttled("s2") is False


def test_session_throttle_expires_after_window():
    # Moves the clock instead of sleeping. The sleeping version asserted on a one-second window
    # and went red inside guard.sh once under machine load while passing standalone — a timing
    # test is a load test nobody asked for, and it also spent 1.1s of every CI run.
    _tmp_throttle()
    import time as _time

    original_ttl = recall_core.SESSION_THROTTLE_SECONDS
    original_time = recall_core.time.time
    clock = [1_000_000.0]
    try:
        recall_core.SESSION_THROTTLE_SECONDS = 3600
        recall_core.time.time = lambda: clock[0]
        assert recall_core._session_throttled("s3") is False
        assert recall_core._session_throttled("s3") is True
        clock[0] += 3601
        assert recall_core._session_throttled("s3") is False
    finally:
        recall_core.time.time = original_time
        recall_core.SESSION_THROTTLE_SECONDS = original_ttl
        del _time


def test_empty_session_id_never_throttled():
    _tmp_throttle()
    assert recall_core._session_throttled(None) is False
    assert recall_core._session_throttled("") is False


if __name__ == "__main__":
    test_session_throttle_blocks_repeated_calls()
    test_session_throttle_expires_after_window()
    test_empty_session_id_never_throttled()
    print("ok - recall_core session throttle")
