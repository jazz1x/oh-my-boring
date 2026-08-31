#!/usr/bin/env python3
"""Tests for verdict_core.py — above all, that a short sample cannot be argued into a verdict."""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import verdict_core as V

REPO = HERE.parent.parent
PRD = REPO / "docs" / "PRD.md"


def test_a_short_sample_is_refused_before_any_rate_is_computed():
    # The floors were registered before the data existed so a thin window could not be talked
    # into a result afterwards. Rates that would otherwise read as a strong effect must not
    # change the label.
    v = V.verdict(sessions=5, used_prompts=50, total_prompts=100, used_control_prompts=1)
    assert v.label == V.REFUSED, v
    assert "세션 5" in v.reason and "주입 프롬프트 100" in v.reason, v.reason
    # The numbers are still carried so a reader can see what was withheld and why.
    assert v.treatment_pp == 50.0 and v.control_pp == 1.0


def test_sessions_alone_cannot_clear_the_floor():
    """Twenty one-prompt sessions is twenty sessions and almost no evidence."""
    v = V.verdict(sessions=25, used_prompts=5, total_prompts=25, used_control_prompts=0)
    assert v.label == V.REFUSED
    assert "주입 프롬프트" in v.reason and "세션" not in v.reason.split("—")[1]


def test_works_needs_the_ratio_and_the_gap_together():
    # 8pp vs 2pp: 4x and a 6pp gap.
    v = V.verdict(sessions=30, used_prompts=80, total_prompts=1000, used_control_prompts=20)
    assert v.label == V.WORKS, v

    # Same 4x ratio, but 0.8pp against 0.2pp. Ratio alone must not declare victory — and the
    # contract is stronger than "unproven" here: a gap inside the 1pp margin is 비작동, however
    # flattering the multiple looks.
    v = V.verdict(sessions=30, used_prompts=8, total_prompts=1000, used_control_prompts=2)
    assert v.label == V.BROKEN, v

    # A big gap with too small a ratio is not 작동 either: 30pp vs 25pp is 5pp apart but only
    # 1.2x, which is the shape of a corpus that echoes itself.
    v = V.verdict(sessions=30, used_prompts=300, total_prompts=1000, used_control_prompts=250)
    assert v.label == V.WITHHELD, v
    assert "대조 ×" in v.reason


def test_treatment_within_a_point_of_chance_is_declared_broken():
    # 2.0pp vs 1.5pp — a 0.5pp gap. The channel is doing what coincidence does.
    v = V.verdict(sessions=30, used_prompts=20, total_prompts=1000, used_control_prompts=15)
    assert v.label == V.BROKEN, v


def test_the_live_shape_of_the_window_lands_in_withheld():
    """The 2026-08-31 reading: 4.8x but only 1.85pp apart. Neither branch may claim it."""
    v = V.verdict(sessions=30, used_prompts=24, total_prompts=1026, used_control_prompts=5)
    assert v.label == V.WITHHELD, v
    assert v.ratio is not None and v.ratio > V.WORKS_RATIO
    assert v.gap_pp < V.WORKS_GAP_PP


def test_a_zero_control_is_not_an_infinite_effect():
    """No control events means the ratio has no denominator; the gap has to carry the claim."""
    v = V.verdict(sessions=30, used_prompts=100, total_prompts=1000, used_control_prompts=0)
    assert v.ratio is None
    assert v.label == V.WORKS, "a 10pp gap over a zero floor still clears the absolute bar"

    thin = V.verdict(sessions=30, used_prompts=5, total_prompts=1000, used_control_prompts=0)
    assert thin.label == V.BROKEN, "0.5pp over a zero floor is within the broken margin"


def _event(agent="claude-code", when="2026-09-01", control=1, **counts):
    row = {
        "event": "injection_uptake",
        "observed_at": f"{when}T00:00:00+00:00",
        "agent": agent,
        "used_prompts": counts.get("used_prompts", 2),
        "total_prompts": counts.get("total_prompts", 100),
    }
    if control is not None:
        row["used_control_prompts"] = control
    return row


def test_events_predating_the_control_counter_are_excluded_not_zeroed():
    """A missing measurement is not a chance rate of zero.

    Folding those rows in with `used_control_prompts = 0` would drag the control rate towards
    zero, which is the direction that makes the channel look effective — the single most
    flattering way to be wrong here.
    """
    rows = [_event(control=None), _event(control=None), _event(control=3)]

    per_agent, skipped = V.collect(rows)

    assert skipped == 2, skipped
    assert per_agent["claude-code"]["sessions"] == 1
    assert per_agent["claude-code"]["used_control_prompts"] == 3


def test_rows_are_not_pooled_across_adapters():
    """Claude Code injects every prompt; Kimi throttles to once a session. One rate answers
    neither, so the two never share a denominator."""
    rows = [_event(agent="claude-code"), _event(agent="kimi"), _event(agent="kimi")]

    per_agent, _ = V.collect(rows)

    assert set(per_agent) == {"claude-code", "kimi"}
    assert per_agent["kimi"]["sessions"] == 2


def test_since_and_agent_narrow_the_window():
    rows = [_event(when="2026-08-20"), _event(when="2026-09-02"), _event(agent="kimi")]

    recent, _ = V.collect(rows, since="2026-09-01")
    assert recent["claude-code"]["sessions"] == 1

    only, _ = V.collect(rows, agent="kimi")
    assert set(only) == {"kimi"}


def test_counters_are_read_from_attributes_when_that_is_where_they_are():
    """The store returns the numbers nested under `attributes`; a top-level-only read counts 0
    and a zero denominator refuses every verdict for a reason that is not true."""
    row = {
        "event": "injection_uptake",
        "observed_at": "2026-09-01T00:00:00+00:00",
        "attributes": {
            "agent": "claude-code",
            "used_prompts": 5,
            "total_prompts": 200,
            "used_control_prompts": 1,
        },
    }

    per_agent, skipped = V.collect([row])

    assert skipped == 0
    assert per_agent["claude-code"]["total_prompts"] == 200
    assert per_agent["claude-code"]["used_prompts"] == 5


def test_the_thresholds_still_match_the_registered_contract():
    """The numbers here are a transcription of docs/PRD.md §2, and transcriptions drift.

    A verdict computed from thresholds that quietly stopped matching the pre-registration is
    not a pre-registered verdict at all — it is a number chosen after seeing the data.
    """
    text = PRD.read_text(encoding="utf-8")
    section = text.split("## 2.")[1].split("\n## ")[0]
    for value, what in (
        (V.MIN_SESSIONS, "세션 하한"),
        (V.MIN_INJECTED_PROMPTS, "프롬프트 하한"),
        (int(V.WORKS_RATIO), "작동 배수"),
        (int(V.WORKS_GAP_PP), "작동 격차"),
        (int(V.BROKEN_MARGIN_PP), "비작동 여유"),
    ):
        assert re.search(rf"\b{value}\b", section), f"{what} {value} 가 PRD §2 에 없다"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - verdict_core: floors refuse, thresholds match the pre-registration")
