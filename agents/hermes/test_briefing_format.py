#!/usr/bin/env python3
"""Network-free tests for Hermes Slack briefing formatting."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
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


def test_slack_mrkdwn_uses_flat_readable_bullets():
    briefing = load_module("briefing", ROOT / "briefing.py")
    weekly = load_module("weekly_briefing", ROOT / "weekly-briefing.py")
    slack_briefing = load_module("slack_briefing_test", ROOT / "slack_briefing.py")

    answer = """# oh-my-boring

- Done: fixed **DB** primary event logging
1. Next: add ops status JSON
Blocked:
- Blocked: -
## oh-my-boring
- 막힘： LM Studio embedding model is not loaded
"""

    expected = """🚨 1 · ▶️ 1 · ✅ 1
📌 집중: 🚨 막힘 1개를 먼저 확인하세요.

🚨 *막힘*
• oh-my-boring — LM Studio embedding model is not loaded

▶️ *다음 행동*
• oh-my-boring — add ops status JSON

✅ *완료*
• oh-my-boring — fixed *DB* primary event logging"""

    assert briefing.slack_mrkdwn(answer) == expected
    assert weekly.slack_mrkdwn(answer) == expected

    payload = slack_briefing.render_blocks_payload(
        "☀️ 아침 브리핑",
        "2026-07-01 Wed",
        answer,
        ["/vault/wiki/wiki-0001.md"],
        "비어 있음",
    )
    assert payload["text"].startswith("☀️ 아침 브리핑")
    assert payload["blocks"][0]["type"] == "header"
    assert payload["blocks"][1]["type"] == "context"
    assert payload["blocks"][2]["type"] == "context"
    assert "🚨 1" in payload["blocks"][2]["elements"][0]["text"]
    assert payload["blocks"][3]["type"] == "divider"
    assert payload["blocks"][4]["type"] == "section"
    assert "막힘" in payload["blocks"][4]["text"]["text"]
    assert payload["blocks"][5]["type"] == "section"
    assert "LM Studio" in payload["blocks"][5]["text"]["text"]
    assert "Blocked: -" not in payload["text"]
    assert payload["blocks"][-1]["type"] == "context"
    assert "wiki-0001.md" in payload["blocks"][-1]["elements"][0]["text"]


def test_slack_mrkdwn_handles_adversarial_inputs():
    slack_briefing = load_module("slack_briefing_test2", ROOT / "slack_briefing.py")

    # Empty answer falls back to the empty message.
    assert slack_briefing.render_body_mrkdwn("") == ""

    # Project heading with no items falls back to compact text (render_message_mrkdwn
    # will substitute the empty message when the body is empty).
    assert slack_briefing.render_body_mrkdwn("# empty-project\n") == "# empty-project"

    # Label without a value is skipped, and a label heading applies to multiple bullets.
    multi = """# p

Blocked:
- first blocker
- second blocker
- 없음
"""
    body = slack_briefing.render_body_mrkdwn(multi)
    assert "🚨 *막힘*" in body
    assert body.count("first blocker") == 1
    assert body.count("second blocker") == 1
    assert "없음" not in body  # EMPTY_VALUES should be dropped
    assert "기타" not in body  # both bullets inherited the Blocked label

    # Unknown labels and label-free bullets land in "기타".
    misc = """# p

- UnknownLabel: something odd
- plain bullet without a label
"""
    body = slack_briefing.render_body_mrkdwn(misc)
    assert "• *기타*" in body
    assert "something odd" in body
    assert "plain bullet without a label" in body

    # HTML-like characters are preserved in mrkdwn and escaped in Block Kit.
    html = """# p

- Next: fix <body> & "quotes"
"""
    body = slack_briefing.render_body_mrkdwn(html)
    assert "fix <body> & \"quotes\"" in body
    payload = slack_briefing.render_blocks_payload(
        "t", "s", html, [], "empty"
    )
    # Fallback mrkdwn keeps raw characters; Block Kit blocks escape them.
    block_blob = json.dumps(payload["blocks"], ensure_ascii=False)
    assert "&lt;body&gt;" in block_blob
    assert "&amp;" in block_blob


def test_source_label_reads_title_as_yaml_frontmatter():
    slack_briefing = load_module("slack_briefing_source_label", ROOT / "slack_briefing.py")

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        wiki = vault / "wiki"
        wiki.mkdir(parents=True)
        note = wiki / "wiki-0001.md"
        note.write_text(
            "---\ntitle: 'omb: release note' # primary source\n---\nbody\n",
            encoding="utf-8",
        )
        slack_briefing._VAULT_DIR = vault

        assert slack_briefing.source_label("wiki-0001.md") == "omb: release note (wiki-0001.md)"


def test_sources_dedup_path_and_chunk_variants():
    slack_briefing = load_module("slack_briefing_source_variants", ROOT / "slack_briefing.py")

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        wiki = vault / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "wiki-0001.md").write_text(
            "---\ntitle: omb raw witness\n---\nbody\n",
            encoding="utf-8",
        )
        (wiki / "wiki-0002.md").write_text(
            "---\ntitle: omb retention\n---\nbody\n",
            encoding="utf-8",
        )
        slack_briefing._VAULT_DIR = vault

        sources = [
            "vault/wiki/wiki-0001.md#chunk_idx=0",
            str(wiki / "wiki-0001.md"),
            "wiki-0001.md",
            "wiki-0002.md#chunk_idx=1",
        ]

        text = slack_briefing.render_sources(sources)

        assert text.count("omb raw witness (wiki-0001.md)") == 1
        assert "wiki-0001.md#chunk" not in text
        assert "omb retention (wiki-0002.md)" in text


def test_sources_drop_empty_placeholders():
    slack_briefing = load_module("slack_briefing_source_empty", ROOT / "slack_briefing.py")

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        wiki = vault / "wiki"
        wiki.mkdir(parents=True)
        (wiki / "wiki-0001.md").write_text(
            "---\ntitle: omb source contract\n---\nbody\n",
            encoding="utf-8",
        )
        slack_briefing._VAULT_DIR = vault

        text = slack_briefing.render_sources([None, "", "  ", "null", "wiki-0001.md"])

        assert text == "근거: omb source contract (wiki-0001.md)"
        assert slack_briefing.render_sources([None, "", "null"]) == ""


def test_source_label_falls_back_for_non_string_title():
    slack_briefing = load_module("slack_briefing_source_label_bad", ROOT / "slack_briefing.py")

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        wiki = vault / "wiki"
        wiki.mkdir(parents=True)
        note = wiki / "wiki-0001.md"
        note.write_text("---\ntitle: [bad]\n---\nbody\n", encoding="utf-8")
        slack_briefing._VAULT_DIR = vault

        assert slack_briefing.source_label("wiki-0001.md") == "wiki-0001.md"


def test_slack_mrkdwn_dedups_duplicate_bullets_across_project_sections():
    slack_briefing = load_module("slack_briefing_test3", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: README 최신화
- Next: 컨플루언스 문서 업데이트
## qa-tests
- Done: PoC 일정 전환
## docs
- Done: README 최신화
## kb-rag-bot
- Done: README 최신화
- Blocked: 토큰 문제
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    # README 최신화는 exact duplicate → 1회만.
    assert body.count("README 최신화") == 1
    assert "kb-rag-bot / docs" in body
    # Blocked from the second kb-rag-bot section is preserved.
    assert body.count("토큰 문제") == 1
    # Summary counts reflect dedup.
    assert "✅ 2" in body  # README 최신화 + PoC 일정 전환
    assert "🚨 1" in body


def test_slack_mrkdwn_dedups_within_label_without_erasing_status():
    slack_briefing = load_module("slack_briefing_test_status", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: release-note 확인.
- 완료: release note 확인
- Next: release note 확인
- Done: C++/.NET 검증
- 완료: cpp dotnet 검증
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    payload = slack_briefing.render_blocks_payload("t", "s", answer, [], "empty")
    block_blob = json.dumps(payload["blocks"], ensure_ascii=False)

    # Cross-label dedup keeps the highest-priority copy once.
    assert body.count("release note 확인") == 1
    assert body.count("C++/.NET 검증") == 1
    assert "▶️ 1" in body
    assert "✅ 1" in body
    assert "release note 확인" in block_blob
    assert "cpp dotnet 검증" not in block_blob


def test_slack_mrkdwn_strips_trailing_source_metadata_for_dedup():
    slack_briefing = load_module("slack_briefing_test_source_suffix", ROOT / "slack_briefing.py")

    answer = """## omb
- Done: guard 로그 확인
- 완료: guard 로그 확인 (source: vault/wiki/wiki-0001.md)
- Next: guard 로그 확인 [wiki-0002.md]
- Done: validate parser (Rust)
"""
    body = slack_briefing.render_body_mrkdwn(answer)

    # Cross-label dedup keeps the Next copy; source suffixes are stripped.
    assert body.count("guard 로그 확인") == 1
    assert "source:" not in body
    assert "wiki-000" not in body
    assert "validate parser (Rust)" in body
    assert "▶️ 1" in body
    assert "✅ 1" in body


def test_slack_mrkdwn_merges_project_aliases_without_flattening_workstreams():
    slack_briefing = load_module("slack_briefing_test_workstream", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: root cleanup
## kb-rag-bot/otel
- Done: trace cleanup
## KB RAG BOT / OTEL
- 완료: trace-cleanup
## docs
- Done: trace cleanup
"""
    body = slack_briefing.render_body_mrkdwn(answer)

    assert "• kb-rag-bot — root cleanup" in body
    assert "kb-rag-bot/otel / KB RAG BOT / OTEL" not in body
    assert "kb-rag-bot/otel / docs" in body
    assert body.count("trace cleanup") == 1


def test_slack_mrkdwn_preserves_identity_punctuation_project_names_for_dedup():
    slack_briefing = load_module(
        "slack_briefing_test_identity_projects", ROOT / "slack_briefing.py"
    )

    answer = """## C++
- Done: compiler
## C#
- Done: compiler
## C
- Done: compiler
"""
    body = slack_briefing.render_body_mrkdwn(answer)

    assert "C++ / C# / C" in body
    assert body.count("compiler") == 1
    assert slack_briefing._project_key("C++") == "c++"
    assert slack_briefing._project_key("C#") == "c#"
    assert slack_briefing._project_key("C") == "c"


def test_slack_mrkdwn_isolates_punctuation_only_project_headings():
    slack_briefing = load_module(
        "slack_briefing_test_punctuation_projects", ROOT / "slack_briefing.py"
    )

    answer = """## omb
- Done: root item
## ---
- Done: separator item
"""
    body = slack_briefing.render_body_mrkdwn(answer)

    assert "• omb — root item" in body
    assert "• --- — separator item" in body


def test_slack_mrkdwn_accepts_coalescer_label_contract():
    slack_briefing = load_module("slack_briefing_test_aliases", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- 해야 할 일 — release note 확인
• 블로커： LM Studio 모델 미기동
1. 위험: 회귀 테스트 공백
- 결정사항: pgvector는 후보 탐색만 수행
### 완료됨
- PR #12 merged
## omb
– done – duplicate gate hardened
* Completed: star bullet accepted
2) risks: claim conflict regression
3) Next actions - guard 로그 확인
4. Blockers — deploy gate
5. Stale - 오래된 문서 검토
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    payload = slack_briefing.render_blocks_payload("t", "s", answer, [], "empty")
    block_blob = json.dumps(payload["blocks"], ensure_ascii=False)

    assert "release note 확인" in body
    assert "LM Studio 모델 미기동" in body
    assert "회귀 테스트 공백" in body
    assert "pgvector는 후보 탐색만 수행" in body
    assert "PR #12 merged" in body
    assert "duplicate gate hardened" in body
    assert "star bullet accepted" in body
    assert "claim conflict regression" in block_blob
    assert "오래된 문서 검토" in block_blob
    assert "deploy gate" in block_blob
    assert "• *기타*" not in body
    assert "🚨 2" in body
    assert "▶️ 2" in body
    assert "⏸️ 1" in body
    assert "⚠️ 2" in body
    assert "💡 1" in body
    assert "✅ 3" in body


def test_slack_mrkdwn_accepts_markdown_task_list_markers():
    slack_briefing = load_module("slack_briefing_test_tasks", ROOT / "slack_briefing.py")

    answer = """## omb
- [ ] Next: guard 로그 확인
- [x] Done: guard 로그 확인
1. [ ] Blocked: release lock
2) [X] 완료: guard 로그 확인
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    payload = slack_briefing.render_blocks_payload("t", "s", answer, [], "empty")
    block_blob = json.dumps(payload["blocks"], ensure_ascii=False)

    assert "• *기타*" not in body
    assert "[ ]" not in body
    assert "[x]" not in body
    assert "🚨 1" in body
    assert "▶️ 1" in body
    # Cross-label dedup: the Next copy of "guard 로그 확인" wins over Done/완료.
    assert "✅ 0" not in body
    assert body.count("guard 로그 확인") == 1
    assert "release lock" in block_blob


def test_slack_mrkdwn_accepts_plain_label_headings_and_items():
    slack_briefing = load_module("slack_briefing_test_plain_labels", ROOT / "slack_briefing.py")

    answer = """## omb
Blocked:
- release lock
Next:
guard 로그 확인
Risks: claim conflict regression
Done:
shipped guard
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    payload = slack_briefing.render_blocks_payload("t", "s", answer, [], "empty")
    block_blob = json.dumps(payload["blocks"], ensure_ascii=False)

    assert "release lock" in body
    assert "guard 로그 확인" in body
    assert "claim conflict regression" in block_blob
    assert "shipped guard" in block_blob
    assert "🚨 1" in body
    assert "▶️ 1" in body
    assert "⚠️ 1" in body
    assert "✅ 1" in body
    assert "• *기타*" not in body


def test_slack_mrkdwn_filters_placeholders_and_noise():
    slack_briefing = load_module("slack_briefing_test4", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: 게이트 4단계 구현
- Next: 다음 지시 기다림
- Blocked: -
- Risks: 없음
- Decisions: 출처 강등 처리
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    # Vacuous bullets dropped.
    assert "다음 지시 기다림" not in body
    assert "Blocked: -" not in body
    assert "없음" not in body
    # Real bullets preserved.
    assert "게이트 4단계 구현" in body
    assert "출처 강등 처리" in body


def test_slack_mrkdwn_drops_relation_metadata_items():
    slack_briefing = load_module("slack_briefing_test_relation_meta", ROOT / "slack_briefing.py")

    answer = """## omb
- Next: shares 2 graph nodes: make, briefing
- Done: related to vault/wiki/wiki-0001.md · shares 1 claim axis: release train / release version
- Next: finish guard
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    payload = slack_briefing.render_blocks_payload("t", "s", answer, [], "empty")
    block_blob = json.dumps(payload["blocks"], ensure_ascii=False)

    assert "shares 2 graph nodes" not in body
    assert "related to vault/wiki" not in block_blob
    assert "finish guard" in body
    assert "▶️ 1" in body


def test_slack_mrkdwn_recognizes_label_without_space_before_backtick():
    """LLM sometimes emits 'Done:`code` text' without a space after the colon."""
    slack_briefing = load_module("slack_briefing_test_backtick", ROOT / "slack_briefing.py")

    answer = """## omb
- Done:`doctor — probe` 플래그 추가
- Next: `fix` 배포
"""
    body = slack_briefing.render_body_mrkdwn(answer)

    assert "✅ *완료*" in body
    assert "doctor — probe" in body
    assert "▶️ *다음 행동*" in body
    assert "fix" in body
    assert "• *기타*" not in body


def test_slack_mrkdwn_handles_flat_project_label_list():
    """LLM sometimes emits a flat list like 'project — *Label*: text'.

    The parser must group these by status instead of falling back to the
    ugly compact text, and must drop stray summary-count lines.
    """
    slack_briefing = load_module("slack_briefing_test_flat", ROOT / "slack_briefing.py")

    answer = """29

*기타*
eng-llm-kb — *Done*: finished docs migration
eng-llm-kb — *Next*: next API review
eng-llm-kb / org-llm-kb — *Blocked*: real blocker
kb-rag-bot — *Risks*: risk model drift
kb-rag-bot — *Decisions*: decision use axum
"""
    body = slack_briefing.render_body_mrkdwn(answer)

    # Stray number and empty placeholders dropped.
    assert "29" not in body
    # Items grouped by status, not left as a flat bullet list.
    assert "🚨 *막힘*" in body
    assert "⚠️ *리스크*" in body
    assert "💡 *결정*" in body
    assert "▶️ *다음 행동*" in body
    assert "✅ *완료*" in body
    assert "finished docs migration" in body
    assert "next API review" in body
    assert "risk model drift" in body
    assert "decision use axum" in body
    # Multi-project prefix preserved on shared items.
    assert "eng-llm-kb / org-llm-kb" in body


def test_slack_mrkdwn_dedups_cross_label_repeated_text():
    """The same bullet text under Decisions and Done should appear only once."""
    slack_briefing = load_module("slack_briefing_test_cross_label", ROOT / "slack_briefing.py")

    answer = """## omb
- Done: finalize parser contract
- Decisions: finalize parser contract
- Next: finalize parser contract
"""
    body = slack_briefing.render_body_mrkdwn(answer)

    # Highest-priority label wins (Blocked > Next > Risks > Decisions > Done).
    assert body.count("finalize parser contract") == 1
    assert "▶️ *다음 행동*" in body
    assert "💡 *결정*" not in body
    assert "✅ *완료*" not in body


def test_slack_mrkdwn_truncates_long_item_text():
    """Very long bullets are capped for Slack mobile readability."""
    slack_briefing = load_module("slack_briefing_test_truncate", ROOT / "slack_briefing.py")

    long_text = "word " * 100
    answer = f"## omb\n- Next: {long_text}\n"
    body = slack_briefing.render_body_mrkdwn(answer)

    assert "…" in body
    assert len(body.split("word ")[-1].split("…")[0]) < 10


def test_slack_mrkdwn_caps_done_items():
    slack_briefing = load_module("slack_briefing_test5", ROOT / "slack_briefing.py")

    answer = "## kb-rag-bot\n" + "\n".join(
        f"- Done: task {i}" for i in range(10)
    )
    body = slack_briefing.render_body_mrkdwn(answer)
    # Only first 3 Done items shown; rest collapsed.
    assert body.count("task") == 3
    assert "외 7개 항목" in body


def test_weekly_window_covers_previous_calendar_week():
    """The weekly digest must cover Mon 00:00 KST through Sun 23:59:59 KST."""
    weekly = load_module("weekly_briefing_window", ROOT / "weekly-briefing.py")
    from datetime import datetime, timezone, timedelta

    KST = timezone(timedelta(hours=9))
    monday_morning = datetime(2026, 7, 6, 9, 0, 0, tzinfo=KST)
    last_monday, last_sunday, since_hours, until_hours = weekly._weekly_window(monday_morning)

    assert last_monday == datetime(2026, 6, 29, 0, 0, 0, tzinfo=KST)
    assert last_sunday == datetime(2026, 7, 5, 23, 59, 59, tzinfo=KST)
    assert since_hours == 24 * 7 + 9
    assert until_hours == 9  # Monday 09:00 -> last Sunday 23:59:59 is 9h ago

    # Late Sunday night: the current week is Mon 29 – Sun 05, so the previous week is Mon 22 – Sun 28.
    sunday_night = datetime(2026, 7, 5, 23, 30, 0, tzinfo=KST)
    last_monday2, last_sunday2, since_hours2, until_hours2 = weekly._weekly_window(sunday_night)
    assert last_monday2 == datetime(2026, 6, 22, 0, 0, 0, tzinfo=KST)
    assert last_sunday2 == datetime(2026, 6, 28, 23, 59, 59, tzinfo=KST)
    assert since_hours2 == 24 * 7 * 2 - 1  # 13 days + 23.5h ≈ 335h
    assert until_hours2 == 24 * 7 - 1  # 6 days + 23.5h ≈ 167h


def test_slack_mrkdwn_dedups_fuzzy_paraphrases():
    """Paraphrased versions of the same fact across labels collapse to one."""
    slack_briefing = load_module(
        "slack_briefing_test_fuzzy", ROOT / "slack_briefing.py"
    )

    answer = """## kb-rag-bot
- Blocked: PR #227 기다림 (classifier가 kb-rag-bot의 자가 병합 차단)
- Next: PR #227을 main에 병합하고, classifier가 kb-rag-bot의 자가 병합을 차단하고 있어 인공적으로 병합해야 함
- Done: axum은 tokio/hyper 스택과 호환되어 선택함
"""
    body = slack_briefing.render_body_mrkdwn(answer)

    # PR #227 fact should appear only once, under the highest-priority label (Blocked).
    assert body.count("PR #227") == 1
    assert "🚨 *막힘*" in body
    assert "▶️ *다음 행동*" not in body
    assert "✅ *완료*" in body


if __name__ == "__main__":
    test_slack_mrkdwn_uses_flat_readable_bullets()
    test_slack_mrkdwn_handles_adversarial_inputs()
    test_source_label_reads_title_as_yaml_frontmatter()
    test_sources_dedup_path_and_chunk_variants()
    test_sources_drop_empty_placeholders()
    test_source_label_falls_back_for_non_string_title()
    test_slack_mrkdwn_dedups_duplicate_bullets_across_project_sections()
    test_slack_mrkdwn_dedups_within_label_without_erasing_status()
    test_slack_mrkdwn_strips_trailing_source_metadata_for_dedup()
    test_slack_mrkdwn_merges_project_aliases_without_flattening_workstreams()
    test_slack_mrkdwn_preserves_identity_punctuation_project_names_for_dedup()
    test_slack_mrkdwn_isolates_punctuation_only_project_headings()
    test_slack_mrkdwn_accepts_coalescer_label_contract()
    test_slack_mrkdwn_accepts_markdown_task_list_markers()
    test_slack_mrkdwn_accepts_plain_label_headings_and_items()
    test_slack_mrkdwn_filters_placeholders_and_noise()
    test_slack_mrkdwn_drops_relation_metadata_items()
    test_slack_mrkdwn_recognizes_label_without_space_before_backtick()
    test_slack_mrkdwn_handles_flat_project_label_list()
    test_slack_mrkdwn_dedups_cross_label_repeated_text()
    test_slack_mrkdwn_truncates_long_item_text()
    test_slack_mrkdwn_caps_done_items()
    test_slack_mrkdwn_dedups_fuzzy_paraphrases()
    test_weekly_window_covers_previous_calendar_week()
    print("ok - hermes briefing Slack formatting")
