#!/usr/bin/env python3
"""Network-free tests for shared env parsing helpers."""
import os
import sys
from contextlib import contextmanager
from pathlib import Path

SHARED_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SHARED_DIR))

import omb_env  # noqa: E402


@contextmanager
def patched_env(values):
    old = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in old.items():
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


def test_env_positive_int_uses_default_and_env_value():
    with patched_env({"COLLECT_LIMIT": None}):
        assert omb_env.env_positive_int("COLLECT_LIMIT", 1) == 1
    with patched_env({"COLLECT_LIMIT": "3"}):
        assert omb_env.env_positive_int("COLLECT_LIMIT", 1) == 3


def test_env_positive_int_rejects_zero_negative_and_invalid():
    for raw in ("0", "-1", "soon"):
        with patched_env({"COLLECT_LIMIT": raw}):
            msg = _value_error_from(lambda: omb_env.env_positive_int("COLLECT_LIMIT", 1))
            assert "COLLECT_LIMIT must be a positive integer" in msg


def test_env_non_negative_int_allows_zero_and_rejects_negative():
    with patched_env({"DISTILL_REMEMBER_RETRIES": "0"}):
        assert omb_env.env_non_negative_int("DISTILL_REMEMBER_RETRIES", 2) == 0
    for raw in ("-1", "soon"):
        with patched_env({"DISTILL_REMEMBER_RETRIES": raw}):
            msg = _value_error_from(lambda: omb_env.env_non_negative_int("DISTILL_REMEMBER_RETRIES", 2))
            assert "DISTILL_REMEMBER_RETRIES must be a non-negative integer" in msg


def test_env_positive_float_uses_fallback_chain_and_rejects_bad_second_value():
    with patched_env({"COLLECT_PENDING_TTL": "", "INGEST_PENDING_TTL": "30"}):
        assert omb_env.env_positive_float(("COLLECT_PENDING_TTL", "INGEST_PENDING_TTL"), 1800.0) == 30.0
    for raw in ("-1", "0", "nan", "inf"):
        with patched_env({"COLLECT_PENDING_TTL": "", "INGEST_PENDING_TTL": raw}):
            msg = _value_error_from(
                lambda: omb_env.env_positive_float(("COLLECT_PENDING_TTL", "INGEST_PENDING_TTL"), 1800.0)
            )
            assert "INGEST_PENDING_TTL must be a positive number" in msg


def test_env_non_negative_float_allows_zero_but_rejects_negative_and_non_finite():
    with patched_env({"COLLECT_MIN_KB": "0"}):
        assert omb_env.env_non_negative_float("COLLECT_MIN_KB", 20.0) == 0.0
    for raw in ("-0.1", "nan", "inf"):
        with patched_env({"COLLECT_MIN_KB": raw}):
            msg = _value_error_from(lambda: omb_env.env_non_negative_float("COLLECT_MIN_KB", 20.0))
            assert "COLLECT_MIN_KB must be a non-negative number" in msg


def test_env_positive_float_error_names_actual_fallback_env():
    with patched_env({"COLLECT_PENDING_TTL": "", "INGEST_PENDING_TTL": "-1"}):
        msg = _value_error_from(
            lambda: omb_env.env_positive_float(("COLLECT_PENDING_TTL", "INGEST_PENDING_TTL"), 1800.0)
        )
        assert "INGEST_PENDING_TTL must be a positive number" in msg


if __name__ == "__main__":
    test_env_positive_int_uses_default_and_env_value()
    test_env_positive_int_rejects_zero_negative_and_invalid()
    test_env_non_negative_int_allows_zero_and_rejects_negative()
    test_env_positive_float_uses_fallback_chain_and_rejects_bad_second_value()
    test_env_non_negative_float_allows_zero_but_rejects_negative_and_non_finite()
    test_env_positive_float_error_names_actual_fallback_env()
    print("ok - omb env")
