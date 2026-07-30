#!/usr/bin/env python3
"""Tests for the briefing quality discipline."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_empty_fallback_is_detected_and_passes():
    bq = load_module("briefing_quality", ROOT / "briefing_quality.py")

    answer = "No work records ingested in the last 24 hours (ingest first)"
    result = bq.check_briefing_quality(answer, [], 24, None, kind="daily")
    assert result.metrics.empty_fallback_detected is True
    # Empty fallback is not a fail; it warns that there is nothing to show.
    assert result.level in ("pass", "warn")


def test_daily_date_window_compliance():
    bq = load_module("briefing_quality_daily", ROOT / "briefing_quality.py")

    ok = bq.check_briefing_quality("# p\n- Next: x", [], 24, None, kind="daily")
    assert ok.metrics.date_window_compliant is True

    bad = bq.check_briefing_quality("# p\n- Next: x", [], 48, None, kind="daily")
    assert bad.metrics.date_window_compliant is False
    assert bad.level == "fail"


def test_weekly_date_window_compliance():
    bq = load_module("briefing_quality_weekly", ROOT / "briefing_quality.py")

    ok = bq.check_briefing_quality("# p\n- Next: x", [], 192, 24, kind="weekly")
    assert ok.metrics.date_window_compliant is True

    missing_upper = bq.check_briefing_quality("# p\n- Next: x", [], 168, None, kind="weekly")
    assert missing_upper.metrics.date_window_compliant is False

    wrong_span = bq.check_briefing_quality("# p\n- Next: x", [], 180, 24, kind="weekly")
    assert wrong_span.metrics.date_window_compliant is False


def test_duplicate_item_rate_flagged():
    bq = load_module("briefing_quality_dup", ROOT / "briefing_quality.py")

    answer = """# p
- Done: README 최신화
- Done: README 최신화
- Done: README 최신화
- Done: README 최신화
- Next: other task
"""
    result = bq.check_briefing_quality(answer, [], 24, None, kind="daily")
    assert result.metrics.duplicate_item_rate > 0.20
    assert result.level == "fail"


def test_ungrouped_item_rate_flagged():
    bq = load_module("briefing_quality_ungrouped", ROOT / "briefing_quality.py")

    answer = """# p
- something odd
- plain bullet
- another unknown
- Next: real task
"""
    result = bq.check_briefing_quality(answer, [], 24, None, kind="daily")
    assert result.metrics.ungrouped_item_rate > 0.30
    assert result.level == "fail"


def test_placeholder_rate_flagged():
    bq = load_module("briefing_quality_placeholder", ROOT / "briefing_quality.py")

    answer = """# p
- Next: real next task
- Next: 다음 지시 기다림
- Blocked: -
- Risks: 없음
"""
    result = bq.check_briefing_quality(answer, [], 24, None, kind="daily")
    assert result.metrics.placeholder_item_rate > 0.50
    assert result.level == "fail"


def test_relation_metadata_is_counted():
    bq = load_module("briefing_quality_rel", ROOT / "briefing_quality.py")

    answer = """# p
- Next: shares 2 graph nodes: make, briefing
- Next: real task
"""
    result = bq.check_briefing_quality(answer, [], 24, None, kind="daily")
    assert result.metrics.relation_metadata_count == 1
    assert result.metrics.relation_metadata_rate == 0.5
    # Relation metadata is dropped by the renderer, so final item count is 1.
    assert result.metrics.final_item_count == 1


def test_source_dedup_rate_flagged():
    bq = load_module("briefing_quality_src", ROOT / "briefing_quality.py")

    sources = [
        "vault/wiki/wiki-0001.md#chunk_idx=0",
        "wiki-0001.md",
        "vault/wiki/wiki-0001.md#chunk_idx=1",
        "vault/wiki/wiki-0001.md#chunk_idx=2",
        "wiki-0002.md",
    ]
    result = bq.check_briefing_quality("# p\n- Next: x", sources, 24, None, kind="daily")
    assert result.metrics.source_dedup_rate > 0.50
    assert result.level == "fail"


def test_max_item_length_warns():
    bq = load_module("briefing_quality_len", ROOT / "briefing_quality.py")

    long_text = "word " * 100
    answer = f"# p\n- Next: {long_text}\n"
    result = bq.check_briefing_quality(answer, [], 24, None, kind="daily")
    assert result.metrics.max_item_length_ok is False
    assert result.level == "warn"


def test_done_dominance_flagged():
    bq = load_module("briefing_quality_done", ROOT / "briefing_quality.py")

    items = "\n".join(f"- Done: task {i}" for i in range(10))
    answer = f"# p\n{items}\n- Next: one thing\n"
    result = bq.check_briefing_quality(answer, [], 24, None, kind="daily")
    assert result.metrics.done_dominance > 0.80
    assert result.level == "fail"


def test_clean_briefing_passes():
    bq = load_module("briefing_quality_clean", ROOT / "briefing_quality.py")

    answer = """# kb-rag-bot
- Blocked: token issue
- Next: update docs
- Done: README refresh
# omb
- Next: guard log check
"""
    result = bq.check_briefing_quality(answer, ["wiki-0001.md"], 24, None, kind="daily")
    assert result.level == "pass"
    assert result.metrics.date_window_compliant is True
    assert result.metrics.max_item_length_ok is True
    assert result.metrics.duplicate_item_rate == 0.0


def test_metrics_json_is_serializable():
    bq = load_module("briefing_quality_json", ROOT / "briefing_quality.py")

    result = bq.check_briefing_quality("# p\n- Next: x", [], 24, None, kind="daily")
    blob = result.metrics.to_json()
    assert '"level"' not in blob  # metrics do not include the result level
    assert '"kind": "daily"' in blob
    assert '"date_window_compliant": true' in blob


if __name__ == "__main__":
    test_empty_fallback_is_detected_and_passes()
    test_daily_date_window_compliance()
    test_weekly_date_window_compliance()
    test_duplicate_item_rate_flagged()
    test_ungrouped_item_rate_flagged()
    test_placeholder_rate_flagged()
    test_relation_metadata_is_counted()
    test_source_dedup_rate_flagged()
    test_max_item_length_warns()
    test_done_dominance_flagged()
    test_clean_briefing_passes()
    test_metrics_json_is_serializable()
    print("ok - briefing quality discipline tests")
