#!/usr/bin/env python3
"""Network-free tests for Hermes Slack briefing formatting.

These tests live under agents/shared so the weekly-briefing pipeline can be
validated independently of the hermes directory layout.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
HERMES_ROOT = ROOT.parent / "hermes"
sys.path.insert(0, str(HERMES_ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_is_noise_line_drops_standalone_numbers():
    slack_briefing = load_module("slack_briefing_noise", HERMES_ROOT / "slack_briefing.py")

    assert slack_briefing._is_noise_line("29")
    assert slack_briefing._is_noise_line("29.")
    assert slack_briefing._is_noise_line("29/10")
    assert slack_briefing._is_noise_line("29:")
    assert slack_briefing._is_noise_line("29;")
    assert not slack_briefing._is_noise_line("29 docs updated")
    assert not slack_briefing._is_noise_line("Done: fix bug")


def test_group_items_dedup_korean_particle_variants():
    slack_briefing = load_module("slack_briefing_ko", HERMES_ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: README를 최신화
- 완료: README을 최신화
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    # Particle differences should collapse to one bullet.
    assert body.count("README") == 1
    assert "✅ 1" in body


def test_cross_label_decision_dedup_keeps_one_copy():
    slack_briefing = load_module(
        "slack_briefing_decision_dedup", HERMES_ROOT / "slack_briefing.py"
    )

    # The same decision text appears under both Decisions and Done.
    answer = """## kb-rag-bot
- Decisions: PoC 일정 전환
- Done: PoC 일정 전환
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    assert body.count("PoC 일정 전환") == 1
    # Decisions has higher priority than Done, so the decision copy survives.
    assert "💡 1" in body
    assert "✅" not in body


def test_render_body_treats_engine_fallback_as_empty():
    slack_briefing = load_module(
        "slack_briefing_fallback", HERMES_ROOT / "slack_briefing.py"
    )
    # Vector/GraphRAG no-match fallback messages from drudge should not be
    # rendered as a fake "기타" project item.
    assert slack_briefing.render_body_mrkdwn(
        "No related memory found. (ingest first?)"
    ) == ""
    assert slack_briefing.render_body_mrkdwn(
        "No recent work records ingested. (ingest first?)"
    ) == ""
    assert slack_briefing.render_body_mrkdwn(
        "No work records ingested in the last 248 hours. (ingest first?)"
    ) == ""
    assert slack_briefing.render_body_mrkdwn(
        "Brief — No work records ingested in the last 248 hours. (ingest first?)"
    ) == ""


if __name__ == "__main__":
    test_is_noise_line_drops_standalone_numbers()
    test_group_items_dedup_korean_particle_variants()
    test_cross_label_decision_dedup_keeps_one_copy()
    test_render_body_treats_engine_fallback_as_empty()
    print("ok - shared briefing Slack formatting")
