#!/usr/bin/env python3
"""Network-free tests for Hermes Slack briefing formatting."""

from __future__ import annotations

import importlib.util
import json
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

    expected = """🚨 막힘 1 · ▶️ 다음 행동 1 · ✅ 완료 1

*행동* (2)
• oh-my-boring
   ◦ 🚨 LM Studio embedding model is not loaded
   ◦ ▶️ add ops status JSON
_→ `recall("…", "oh-my-boring")` · 느림 `next_actions("oh-my-boring")`_

✅ 완료 1건 — 상세는 위키"""

    assert briefing.slack_mrkdwn(answer) == expected
    assert weekly.slack_mrkdwn(answer) == expected

    # The shortlist half of this test needs a briefing big enough to warrant one: below the
    # threshold the whole message fits on a screen and a summary would only restate it.
    busy = "# oh-my-boring\n" + "\n".join(
        [f"- Next: action {i}" for i in range(6)] + ["- 막힘： LM Studio embedding model is not loaded"]
    )
    payload = slack_briefing.render_blocks_payload(
        "☀️ 아침 브리핑",
        "2026-07-01 Wed",
        busy,
        ["/vault/wiki/wiki-0001.md"],
        "비어 있음",
    )
    assert payload["text"].startswith("☀️ 아침 브리핑")
    assert payload["blocks"][0]["type"] == "header"
    assert payload["blocks"][1]["type"] == "context"
    assert payload["blocks"][2]["type"] == "context"
    assert "🚨 막힘 1" in payload["blocks"][2]["elements"][0]["text"]
    # The shortlist is the third block on purpose: the question at 9am is "what first", and a
    # reader who has to scroll past a status ledger to find it is doing the triage themselves.
    assert payload["blocks"][3]["type"] == "section"
    shortlist = payload["blocks"][3]["text"]["text"]
    assert shortlist.startswith("*오늘의 1순위*")
    assert "LM Studio" in shortlist, "the blocker must be the first thing the reader sees"
    assert payload["blocks"][4]["type"] == "divider"
    # Six status headings were more precision than the classifier behind them delivers, so they
    # collapse into 행동/참고 and the status rides on each item as an emoji instead.
    group = payload["blocks"][5]["text"]["text"]
    assert group.startswith("*행동* ("), group
    assert "🚨" in group, "the status must survive on the item"
    assert "LM Studio" in group
    assert "Blocked: -" not in payload["text"]
    assert payload["blocks"][-1]["type"] == "context"
    assert "wiki-0001.md" in payload["blocks"][-1]["elements"][0]["text"]


def test_blocked_is_never_truncated_and_both_renderers_agree():
    slack_briefing = load_module("slack_briefing_limits", ROOT / "slack_briefing.py")

    # Eight blockers and eight next-actions: more than any per-group limit.
    # The zone limit is the sum of its labels' limits, so Next has to exceed that sum before
    # truncation happens at all — a fixture below it would assert on a cap that never fires.
    lines = ["# proj"]
    lines += [f"- Blocked: blocker {i}" for i in range(8)]
    lines += [f"- Next: action {i}" for i in range(20)]
    answer = "\n".join(lines)

    body = slack_briefing.render_body_mrkdwn(answer)
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty")
    blocks_text = "\n".join(
        b["text"]["text"] for b in payload["blocks"] if b.get("type") == "section"
    )

    # A "+N more" hiding a blocker is the one omission that can cost the reader their morning.
    for i in range(8):
        assert f"blocker {i}" in body, f"text renderer dropped blocker {i}"
        assert f"blocker {i}" in blocks_text, f"block renderer dropped blocker {i}"

    # Next is truncated — and both renderers must truncate it to the SAME set, because Slack
    # picks between them and a fallback that disagrees is a second, quieter briefing.
    shown_text = {i for i in range(20) if f"action {i}" in body}
    shown_blocks = {i for i in range(20) if f"action {i}" in blocks_text}
    assert shown_text == shown_blocks, f"fallback and blocks disagree: {shown_text} vs {shown_blocks}"
    assert len(shown_text) < 20, "Next must actually be capped, or this test proves nothing"


def test_done_does_not_push_blockers_off_the_first_screen():
    slack_briefing = load_module("slack_briefing_done", ROOT / "slack_briefing.py")

    answer = "\n".join(
        ["# proj", "- Blocked: the one blocker"]
        + [f"- Done: finished {i}" for i in range(10)]
    )
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty")
    sections = [b for b in payload["blocks"] if b.get("type") == "section"]
    section_text = "\n".join(b["text"]["text"] for b in sections)

    # Done is confirmation, not work: it gets a count line at the bottom, never bullets.
    for i in range(10):
        assert f"finished {i}" not in section_text, "Done items must not occupy sections"
    tail = "\n".join(
        e["text"]
        for b in payload["blocks"]
        if b.get("type") == "context"
        for e in b["elements"]
    )
    assert "완료 10" in tail
    assert "the one blocker" in sections[0]["text"]["text"], "the blocker leads the message"


def test_long_text_is_cut_at_a_boundary_and_says_so():
    slack_briefing = load_module("slack_briefing_trim", ROOT / "slack_briefing.py")

    # A bare slice cuts mid-sentence with no sign it happened, which is how a reader ends up
    # trusting a sentence that was never finished.
    text = "\n".join(f"line {i} with some words" for i in range(400))
    out = slack_briefing._mrkdwn_text(text, 300)
    assert len(out) <= 300
    assert out.endswith("…"), "a truncated field must admit it"
    assert not out.rstrip("…").endswith("wor"), "cut should land on a line boundary"

    short = slack_briefing._mrkdwn_text("intact", 300)
    assert short == "intact", "text that fits must not be touched"


def _week(days_spec):
    """[(date, brief_markdown)] -> [(date, BriefDocument)] using the real parser."""
    slack_briefing = load_module("slack_briefing_week", ROOT / "slack_briefing.py")
    return [(d, slack_briefing.parse_brief(text)) for d, text in days_spec]


def test_weekly_reports_persistence_not_closure():
    trend = load_module("weekly_trend_persist", ROOT / "weekly_trend.py")

    # The same project blocked all week, worded differently every day — which is what actually
    # happens, because each daily is re-synthesised by the model (measured: 252 items over a
    # week, adjacent-day overlap 0,0,0,2,0,0).
    days = _week(
        [
            (f"2026-08-2{i}", f"# kb-rag-bot\n- Blocked: corpus boundary issue variant {i}\n")
            for i in range(4)
        ]
        # Blocked on exactly one day: a thing can be blocked overnight and cleared by lunch,
        # and calling that persistent would fill the intervention list with noise. It must be a
        # *blocked* project — a Done one is filtered by label anyway and would prove nothing.
        + [("2026-08-24", "# overnight\n- Blocked: cleared by lunch\n")]
    )
    projects = trend.collect_week(days)
    rows = trend.needs_intervention(projects)
    assert [(w.name, label, n) for w, label, n in rows] == [("kb-rag-bot", "Blocked", 4)], rows

    # A project seen once is not persistence.
    assert all(w.name != "overnight" for w, _l, _n in rows), "one day is not persistence"


def test_weekly_never_sums_label_counts_across_days():
    trend = load_module("weekly_trend_sum", ROOT / "weekly_trend.py")

    days = _week(
        [
            ("2026-08-20", "# p\n- Done: a\n- Done: b\n"),
            ("2026-08-21", "# p\n- Done: c\n- Done: d\n"),
            ("2026-08-22", "# p\n- Done: e\n"),
        ]
    )
    got = trend.label_trend(days)
    # Endpoints only. A sum would say "5 done this week" when each day is an independent
    # re-observation of the same corpus and no key can tell a repeat from a new item.
    assert got == {"Done": (2, 1)}, got


def test_weekly_quotes_the_latest_daily_rather_than_resummarising():
    trend = load_module("weekly_trend_quote", ROOT / "weekly_trend.py")

    days = _week(
        [
            ("2026-08-20", "# p\n- Blocked: the old wording\n"),
            ("2026-08-21", "# p\n- Blocked: the old wording again\n"),
            ("2026-08-22", "# p\n- Blocked: today's exact wording\n"),
        ]
    )
    projects = trend.collect_week(days)
    assert projects["p"].latest_line == "today's exact wording"


def test_the_weekly_text_carries_what_the_blocks_carry():
    """The persistence weekly could only be emitted as JSON, so it never shipped.

    Cron delivery is text-only, and the trend path sat behind `BORING_BRIEFING_FORMAT=blocks`
    which production does not set — so every Monday sent a re-synthesised `/weekly` instead,
    the exact thing this weekly exists to replace.
    """
    slack_briefing = load_module("slack_briefing_wtext", ROOT / "slack_briefing.py")
    weekly_trend = load_module("weekly_trend_wtext", ROOT / "weekly_trend.py")

    week = weekly_trend.ProjectWeek(name="kb-rag-bot")
    week.days = {"2026-08-25", "2026-08-26", "2026-08-27"}
    week.latest_line = "컨플루언스 상태 확인 불가"
    intervention = [(week, "Stalled", 3, 7)]
    board = [(week, 7)]
    trend = {"Done": (8, 5)}

    text = slack_briefing.render_weekly_mrkdwn("주간", "W36", intervention, board, trend, [])

    assert "kb-rag-bot — 3/7일" in text, text
    assert "컨플루언스 상태 확인 불가" in text, "the quoted line is the weekly's only item detail"
    # The scoreboard writes "name N/M일"; the intervention line writes "name — N/M일". Asserting
    # the bare count matches either, so removing the scoreboard entirely still passed.
    assert "주간 점유" in text and "kb-rag-bot 3/7일" in text, text
    assert "8→5" in text, text
    # Dropping the caveat lets a reader add ✅ 10→10 into "twenty done"; every day is an
    # independent re-synthesis, so the two numbers are observations, not a rate.
    assert "일자별 관측치이며 마감률이 아니다" in text, text


def test_weekly_blocks_carry_the_not_a_closure_rate_caveat():
    slack_briefing = load_module("slack_briefing_caveat", ROOT / "slack_briefing.py")
    trend = load_module("weekly_trend_caveat", ROOT / "weekly_trend.py")

    days = _week([("2026-08-2%d" % i, "# p\n- Blocked: x%d\n" % i) for i in range(3)])
    projects = trend.collect_week(days)
    blocks = slack_briefing.render_weekly_blocks(
        "T", "S", projects,
        [(w, l, n, 3) for w, l, n in trend.needs_intervention(projects)],
        [(w, 3) for w in trend.scoreboard(projects)],
        trend.label_trend(days), [],
    )
    tail = "\n".join(
        e["text"] for b in blocks if b.get("type") == "context" for e in b["elements"]
    )
    # A reader who adds "✅ 10→10" into "twenty done" has been lied to. The caveat is contract.
    assert "마감률이 아니다" in tail
    # No item bullets beyond the one quoted line per persistent project.
    sections = [b for b in blocks if b.get("type") == "section" and "text" in b]
    assert len(sections) == 1, "the weekly must not re-list items"


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
    assert "*행동*" in body, "blocked items live in the action zone now"
    assert body.count("🚨") >= 2, "each blocked item keeps its status emoji"
    assert body.count("first blocker") == 1
    assert body.count("second blocker") == 1
    assert "없음" not in body  # EMPTY_VALUES should be dropped
    assert "기타" not in body  # both bullets inherited the Blocked label

    # An item the distiller failed to label is still an item. There is no "기타" heading any
    # more — it rides in 참고 — but it must never vanish, or the briefing is quietly lossy.
    misc = """# p

- UnknownLabel: something odd
- plain bullet without a label
"""
    body = slack_briefing.render_body_mrkdwn(misc)
    assert "*참고*" in body
    assert "something odd" in body, "an unknown label must not silently drop the item"
    assert "plain bullet without a label" in body
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


def test_slack_mrkdwn_dedups_duplicate_bullets_across_project_sections():
    slack_briefing = load_module("slack_briefing_test3", ROOT / "slack_briefing.py")

    answer = """## kb-rag-bot
- Done: README 최신화
- Next: 컨플루언스 문서 업데이트
## qa-tests
- Done: PoC 일정 전환
## kb-rag-bot
- Done: README 최신화
- Blocked: 토큰 문제
"""
    body = slack_briefing.render_body_mrkdwn(answer)
    # Done is a count line now, so its items never reach the body at all — the dedup that
    # matters here is on the labels that do print.
    assert body.count("README 최신화") == 0
    # Blocked from the second kb-rag-bot section is preserved.
    assert body.count("토큰 문제") == 1
    # Summary counts reflect dedup.
    assert "✅ 완료 2" in body  # README 최신화 + PoC 일정 전환
    assert "🚨 막힘 1" in body


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
    # A Done item survives parsing but not the body: it is counted, not bulleted.
    assert "게이트 4단계 구현" not in body
    assert "✅ 완료" in body
    assert "출처 강등 처리" in body


def test_done_is_counted_in_the_text_renderer_too():
    slack_briefing = load_module("slack_briefing_test5", ROOT / "slack_briefing.py")

    answer = "## kb-rag-bot\n" + "\n".join(f"- Done: task {i}" for i in range(10))
    body = slack_briefing.render_body_mrkdwn(answer)
    # Done used to be capped at three bullets here and demoted to a count in the blocks — the
    # two renderings disagreeing is the defect this whole contract exists to prevent.
    assert body.count("task") == 0
    assert "✅ 완료 10건" in body


def test_singular_labels_are_not_dumped_into_the_unlabelled_bucket():
    slack_briefing = load_module("slack_briefing_alias", ROOT / "slack_briefing.py")

    # The engine writes these in the singular. The alias table only had the plurals, so eight
    # labelled items a day were reported as "기타" — the category was not missing, the alias was.
    answer = "# p\n- Decision: B 단독\n- Risk: 역방향 감사 누락\n- Blocker: 외부 차단\n"
    body = slack_briefing.render_body_mrkdwn(answer)
    assert "💡 결정 1" in body, body
    assert "⚠️ 리스크 1" in body, body
    assert "🚨 막힘 1" in body, body
    assert "기타" not in body, "a labelled item must never land in the unlabelled bucket"


def test_the_engines_default_claim_kind_is_a_category_not_a_leftover():
    slack_briefing = load_module("slack_briefing_fact", ROOT / "slack_briefing.py")

    # `fact` is what a claim is unless it says otherwise, so it is the biggest thing the
    # distiller emits -- and it was landing in the unlabelled bucket, the same defect the
    # singular aliases above fixed, on a much larger share of the day's items.
    answer = "# p\n- Fact: 재배포 없이는 배달되지 않는다\n- Decision: B 단독\n"
    body = slack_briefing.render_body_mrkdwn(answer)
    assert "📌 사실 1" in body, body
    assert "기타" not in body, "the engine's default claim kind is not an unlabelled leftover"
    # It belongs with what the reader consults, not with what the reader must act on. Assert
    # positively: "absent from 행동" also holds when the item was dropped altogether, which is
    # the louder bug of the two.
    action_zone, _, reference_zone = body.partition("*참고*")
    assert "재배포 없이는" in reference_zone, body
    assert "재배포 없이는" not in action_zone, body


def test_the_window_sample_is_named_in_both_renderings_and_falls_silent_when_met():
    slack_briefing = load_module("slack_briefing_window", ROOT / "slack_briefing.py")
    V = slack_briefing.verdict_core

    # The verdict's counts only move when a session *ends*, and sessions here run for days. The
    # ledger can look busy for a week while the floor sits still, and without this line nobody
    # finds out until the window closes on a refusal.
    answer = "# p\n- Next: 뭔가 한다\n"
    behind = {"sessions": 3, "total_prompts": 120}

    body = slack_briefing.render_message_mrkdwn("*T*", "S", answer, [], "empty", None, behind)
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty", None, behind)

    def all_text(blocks):
        out = []
        for b in blocks:
            if isinstance(b.get("text"), dict):
                out.append(b["text"].get("text", ""))
            for el in b.get("elements") or []:
                if isinstance(el, dict) and el.get("text"):
                    out.append(el["text"])
        return "\n".join(out)

    for where, text in (("fallback", body), ("blocks", all_text(payload["blocks"]))):
        assert f"3/{V.MIN_SESSIONS}" in text, (where, text)
        assert f"120/{V.MIN_INJECTED_PROMPTS}" in text, (where, text)

    met = {"sessions": V.MIN_SESSIONS, "total_prompts": V.MIN_INJECTED_PROMPTS}
    assert slack_briefing.window_notice(met) == "", "a met floor stops reporting the sample"

    # One floor met is not both — the line has to keep reporting until the verdict is computable.
    assert slack_briefing.window_notice(
        {"sessions": V.MIN_SESSIONS, "total_prompts": 1}
    ) != ""

    assert slack_briefing.window_notice(None) == "", "an unreachable engine is not a zero sample"


def test_the_audit_backlog_is_named_in_both_renderings_and_falls_silent_when_met():
    slack_briefing = load_module("slack_briefing_audit", ROOT / "slack_briefing.py")

    # The LLM judge accrues 24 a night on its own; the human side only moves when a person
    # sits down, and until it does the agreement figure -- the one the verdict contract
    # actually reads -- cannot be computed at all. Silence about that is how a two-week
    # window ends in "판단 보류".
    answer = "# p\n- Next: 뭔가 한다\n"
    behind = {"judges": [{"judge": "llm", "relevant": 18, "irrelevant": 30}], "compared": 4}

    body = slack_briefing.render_message_mrkdwn("*T*", "S", answer, [], "empty", behind)
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty", behind)
    def all_text(blocks):
        # context blocks carry their text in `elements`, sections in `text` -- read both, or
        # the assertion passes for the wrong reason on whichever shape it forgot.
        out = []
        for b in blocks:
            if isinstance(b.get("text"), dict):
                out.append(b["text"].get("text", ""))
            for el in b.get("elements") or []:
                if isinstance(el, dict) and el.get("text"):
                    out.append(el["text"])
        return "\n".join(out)

    blocks_text = all_text(payload["blocks"])
    owed = str(slack_briefing.label_core.MIN_COMPARED - 4)
    # Slack picks between the two renderings; a line in only one of them is a second, quieter
    # briefing -- the defect this whole contract exists to prevent.
    assert owed in body and "--audit" in body, body
    assert owed in blocks_text and "--audit" in blocks_text, blocks_text

    met = {"judges": [], "compared": slack_briefing.label_core.MIN_COMPARED}
    assert slack_briefing.audit_notice(met) == "", "a met floor must stop asking"

    # Unknown is not the same as outstanding: an unreachable endpoint would otherwise print the
    # full floor as backlog, a number nobody can act on.
    assert slack_briefing.audit_notice(None) == "", "unknown counts must not be reported as owed"


def test_the_text_fallback_carries_the_shortlist_too():
    slack_briefing = load_module("slack_briefing_fb", ROOT / "slack_briefing.py")

    answer = "# p\n- Blocked: the blocker\n" + "\n".join(
        f"- Next: action {i}" for i in range(6)
    )
    body = slack_briefing.render_body_mrkdwn(answer)
    payload = slack_briefing.render_blocks_payload("T", "S", answer, [], "empty")
    blocks_text = "\n".join(
        b["text"]["text"] for b in payload["blocks"] if b.get("type") == "section" and "text" in b
    )
    # Slack picks between the two renderings. A fallback missing the one thing the message exists
    # to answer is a second, quieter briefing — the defect this contract was written for.
    assert "*오늘의 1순위*" in body, body
    assert "*오늘의 1순위*" in blocks_text
    shortlist_at = body.index("*오늘의 1순위*")
    group_at = body.index("*행동*")
    assert shortlist_at < group_at, "the shortlist comes before the groups"
    assert "the blocker" in body[shortlist_at:group_at], "the blocker must lead the shortlist"


def test_zones_replace_status_headings_but_keep_the_status():
    slack_briefing = load_module("slack_briefing_zone", ROOT / "slack_briefing.py")

    answer = "# p\n- Blocked: cannot start\n- Stalled: sitting a week\n- Next: do the thing\n- Risk: might break\n"
    body = slack_briefing.render_body_mrkdwn(answer)

    # Six headings were more precision than the classifier delivers — the distiller's own labels
    # wander, and a six-way surface amplifies that instead of absorbing it.
    for gone in ("*막힘*", "*정체 중*", "*다음 행동*", "*리스크*"):
        assert gone not in body, f"{gone} should have collapsed into a zone"
    assert "*행동* (3)" in body
    assert "*참고* (1)" in body
    # The status survives on the item, so a reader still sees blocked-vs-next without the
    # briefing having to be right about which of six boxes the item belongs in.
    for emoji in ("🚨", "⏸️", "▶️", "⚠️"):
        assert emoji in body, f"{emoji} must ride on its item"

    # Two rendering paths carry the emoji — one project with several items nests them under the
    # name, one project with a single item writes it inline. A mutation in either must be caught.
    single = slack_briefing.render_body_mrkdwn(
        "# alpha\n- Blocked: only item\n## beta\n- Next: also only item\n"
    )
    assert "🚨 alpha — only item" in single, single
    assert "▶️ beta — also only item" in single


def test_each_zone_names_the_call_that_digs_into_it():
    slack_briefing = load_module("slack_briefing_follow", ROOT / "slack_briefing.py")

    answer = "# kb-rag-bot\n- Blocked: a\n- Next: b\n- Risk: c\n## other\n- Next: d\n"
    body = slack_briefing.render_body_mrkdwn(answer)

    # Naming the sources bought nothing — Slack cannot open a vault file. Naming the call does:
    # the reader is already in front of an agent that can run it.
    assert "recall(" in body and "claims(" in body, body
    # The project is filled in, not left as a placeholder for the reader to supply.
    assert '"kb-rag-bot"' in body
    assert "project)" not in body, "the briefing knows the name; do not make the reader type it"
    # Deterministic tools lead. next_actions was measured at over 30s with no response, so a
    # suggestion that leaves the reader waiting half a minute must at least say so.
    action_line = next(ln for ln in body.split("\n") if ln.startswith("_→") and "recall(" in ln)
    assert action_line.index("recall(") < action_line.index("next_actions("), action_line
    assert "느림" in action_line


def test_endings_are_shaved_not_truncated():
    slack_briefing = load_module("slack_briefing_shave", ROOT / "slack_briefing.py")

    # Korean puts the verb last, so cutting from the front deletes the action and leaves the
    # object. The tail is shaved instead and the verb stem stays.
    answer = "# p\n- Next: 재현 스크립트를 확보하여 근거를 보강해야 합니다.\n- Next: 코퍼스 경계 검증이 필요합니다.\n"
    body = slack_briefing.render_body_mrkdwn(answer)
    assert "근거를 보강" in body, body
    assert "해야 합니다" not in body
    assert "코퍼스 경계 검증 필요" in body
    # A line matching no pattern is left exactly as written.
    assert slack_briefing.shave_ending("no korean tail here") == "no korean tail here"


def test_sources_are_one_line_not_five_titles():
    slack_briefing = load_module("slack_briefing_src", ROOT / "slack_briefing.py")

    sources = [f"/vault/wiki/wiki-{i:04d}.md" for i in range(5)]
    line = slack_briefing.render_sources(sources)
    # Slack cannot open a vault file, so five titles were 277 characters the reader could do
    # nothing with. What survives is the trust signal.
    assert len(line) <= 40, line
    assert "위키 5건" in line
    assert slack_briefing.render_sources([]) == ""


def test_repeated_project_names_are_written_once():
    slack_briefing = load_module("slack_briefing_group", ROOT / "slack_briefing.py")

    answer = "# omcr\n" + "\n".join(f"- Next: action {i}" for i in range(4))
    body = slack_briefing.render_body_mrkdwn(answer)
    # Five lines that all begin with the same project name spend the first twenty characters of
    # every line saying nothing new, which on a phone is most of the line.
    # Item lines only: the follow-up line names the project too, and that repetition is the
    # point there — it is the argument the reader would otherwise have to type.
    item_lines = [ln for ln in body.split("\n") if ln.startswith(("•", "   ◦"))]
    assert sum(ln.count("omcr") for ln in item_lines) == 1, item_lines
    for i in range(4):
        assert f"action {i}" in body
    assert "◦" in body, "items must nest under the project name"


if __name__ == "__main__":
    test_slack_mrkdwn_uses_flat_readable_bullets()
    test_blocked_is_never_truncated_and_both_renderers_agree()
    test_done_does_not_push_blockers_off_the_first_screen()
    test_long_text_is_cut_at_a_boundary_and_says_so()
    test_weekly_reports_persistence_not_closure()
    test_weekly_never_sums_label_counts_across_days()
    test_weekly_quotes_the_latest_daily_rather_than_resummarising()
    test_the_weekly_text_carries_what_the_blocks_carry()
    test_weekly_blocks_carry_the_not_a_closure_rate_caveat()
    test_slack_mrkdwn_handles_adversarial_inputs()
    test_slack_mrkdwn_dedups_duplicate_bullets_across_project_sections()
    test_slack_mrkdwn_filters_placeholders_and_noise()
    test_done_is_counted_in_the_text_renderer_too()
    test_singular_labels_are_not_dumped_into_the_unlabelled_bucket()
    test_the_engines_default_claim_kind_is_a_category_not_a_leftover()
    test_the_window_sample_is_named_in_both_renderings_and_falls_silent_when_met()
    test_the_audit_backlog_is_named_in_both_renderings_and_falls_silent_when_met()
    test_the_text_fallback_carries_the_shortlist_too()
    test_zones_replace_status_headings_but_keep_the_status()
    test_each_zone_names_the_call_that_digs_into_it()
    test_endings_are_shaved_not_truncated()
    test_sources_are_one_line_not_five_titles()
    test_repeated_project_names_are_written_once()
    print("ok - hermes briefing Slack formatting")
