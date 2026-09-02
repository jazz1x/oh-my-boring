"""The pre-registered verdict for the injection-channel window (docs/PRD.md §2).

Pure arithmetic over counts the instrument already records, kept out of the reporting script so
the thresholds are testable and so nobody has to retype them at the moment of judging. That
moment is exactly when a number gets nudged: the window closes, the sample is a little short, and
"close enough" is one edit away. Here the refusal is the default branch.

The contract, transcribed from §2 with the wording that matters:

    표본 하한 | 세션 ≥20 AND 주입된 프롬프트 ≥200. 미달이면 판정 거부 — 숫자를 내지 않는다
    작동      | 처치군 ≥ 대조군 2배 AND 격차 ≥ 3pp
    비작동    | 처치군 ≤ 대조군 + 1pp
    판정 유보 | 그 사이 → 창 1회 연장, 강제 행동 없음

Both rates are per-prompt over the same denominator. A per-hit control against a per-prompt
treatment is a different ratio, not a rougher one — see `uptake_core.session_uptake`.
"""

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

#: Sessions the window needs before any number is reported. Below this the rates are noise from
#: a handful of conversations, and a rate quoted from noise is worse than silence: it gets cited.
MIN_SESSIONS = 20

#: Injected prompts the window needs. Sessions alone can be met by twenty one-prompt sessions.
MIN_INJECTED_PROMPTS = 200

#: "작동" needs both: the treatment has to be a multiple of chance AND far enough above it in
#: absolute terms. Ratio alone declares victory on 0.2% vs 0.1%; gap alone ignores the floor.
WORKS_RATIO = 2.0
WORKS_GAP_PP = 3.0

#: "비작동" — treatment within this margin of chance means the channel, in its current shape,
#: is not doing anything the corpus would not have done by coincidence.
BROKEN_MARGIN_PP = 1.0

REFUSED = "판정 거부"
WORKS = "작동"
BROKEN = "비작동"
WITHHELD = "판정 유보"


class Verdict(NamedTuple):
    """A verdict plus everything needed to check it by hand."""

    label: str
    reason: str
    sessions: int
    total_prompts: int
    treatment_pp: float
    control_pp: float
    gap_pp: float
    ratio: float | None


def _pp(numerator, denominator):
    return (100.0 * numerator / denominator) if denominator else 0.0


def verdict(sessions, used_prompts, total_prompts, used_control_prompts):
    """Judge the window, or refuse to.

    Refusal comes first and is not overridable: the sample floors were registered before the data
    existed precisely so that a short sample could not be argued into a verdict afterwards.
    """
    treatment = _pp(used_prompts, total_prompts)
    control = _pp(used_control_prompts, total_prompts)
    gap = treatment - control
    ratio = (treatment / control) if control else None

    if sessions < MIN_SESSIONS or total_prompts < MIN_INJECTED_PROMPTS:
        short = []
        if sessions < MIN_SESSIONS:
            short.append(f"세션 {sessions} < {MIN_SESSIONS}")
        if total_prompts < MIN_INJECTED_PROMPTS:
            short.append(f"주입 프롬프트 {total_prompts} < {MIN_INJECTED_PROMPTS}")
        return Verdict(
            REFUSED, "표본 하한 미달 — " + " · ".join(short),
            sessions, total_prompts, treatment, control, gap, ratio,
        )

    # Written as a multiplication, which is what §2 says: 처치군 ≥ 대조군 2배. Dividing instead
    # invents a special case at control 0 — the ratio is undefined there, but the condition is
    # not: anything is at least twice nothing. The absolute gap is what stops that from being a
    # free pass, which is exactly why the contract requires both.
    ratio_ok = treatment >= WORKS_RATIO * control
    if ratio_ok and gap >= WORKS_GAP_PP:
        return Verdict(
            WORKS, f"처치 {treatment:.2f}pp ≥ 대조 {control:.2f}pp × {WORKS_RATIO:g} 이고 격차 {gap:.2f}pp ≥ {WORKS_GAP_PP:g}pp",
            sessions, total_prompts, treatment, control, gap, ratio,
        )
    if gap <= BROKEN_MARGIN_PP:
        return Verdict(
            BROKEN, f"처치 {treatment:.2f}pp ≤ 대조 {control:.2f}pp + {BROKEN_MARGIN_PP:g}pp — 현재 형태의 주입 채널 비작동",
            sessions, total_prompts, treatment, control, gap, ratio,
        )
    unmet = []
    if not ratio_ok:
        unmet.append(f"처치 {treatment:.2f}pp < 대조 × {WORKS_RATIO:g}")
    if gap < WORKS_GAP_PP:
        unmet.append(f"격차 {gap:.2f}pp < {WORKS_GAP_PP:g}pp")
    return Verdict(
        WITHHELD, "작동 조건 미충족, 비작동 조건에도 해당 없음 — " + " · ".join(unmet),
        sessions, total_prompts, treatment, control, gap, ratio,
    )


def format_verdict(v):
    """Lines a report can paste verbatim, numbers before the label."""
    ratio = f"{v.ratio:.2f}배" if v.ratio is not None else "대조 0 (비 계산 불가)"
    return [
        f"세션 {v.sessions} · 주입 프롬프트 {v.total_prompts}",
        f"처치 per-prompt {v.treatment_pp:.2f}pp · 대조 per-prompt {v.control_pp:.2f}pp",
        f"격차 {v.gap_pp:.2f}pp · 비 {ratio}",
        f"판정: {v.label} — {v.reason}",
    ]


#: Counters an `injection_uptake` event carries that the verdict adds up.
COUNTERS = ("used_prompts", "total_prompts", "used_control_prompts")

def unreported(rows):
    """(sessions, rows) that were injected into and never scored, from `injection_unreported`.

    A rate says what share of the injections it saw were used. It cannot say what share of the
    injections it saw at all. Sessions that are killed rather than exited never fire SessionEnd,
    so they are never scored and their ledger rows age out — this is what is left of them.

    Reported beside the verdict, never folded into it: these injections have no outcome, so
    counting them either way would invent one.
    """
    sessions = total = 0
    for row in rows or []:
        if row.get("event") != "injection_unreported":
            continue
        sessions += field(row, "aged_sessions")
        total += field(row, "aged_rows")
    return sessions, total


#: The window's own dates and its one-time progress gate, held here beside the thresholds so the
#: PRD-transcription test covers them too. They were previously spelled in `scripts/peek.py`, in
#: GOALS, in the PRD, and in a docstring that still said the window closed 2026-09-09 a day after
#: it was reset to 09-14 — a comment nobody could see rot.
#:
#: The midpoint is a one-time progress gate, not a recurring one: below the floor on that date the
#: window becomes an instrumentation investigation rather than a wait (docs/PRD.md §2). It does not
#: apply to the extension, which changes no threshold.
#: The window's dates are the OWNER'S calendar days, not UTC ones. The morning briefing runs at
#: 08:00 KST, which is 23:00 UTC the day before — so a UTC comparison makes the 09-08 briefing say
#: nothing and the 09-09 one carry the warning, handing back a day of a window that is being
#: watched precisely because it is short. Fixed offset rather than the machine's local zone: this
#: also runs inside a container, and a gate that moves with `TZ` is not a registered date.
WINDOW_TZ = timezone(timedelta(hours=9))
WINDOW_SINCE = "2026-08-31"
WINDOW_UNTIL = "2026-09-14"
MIDPOINT = "2026-09-08"
#: Scored sessions required at the midpoint, **per adapter** — the same basis as MIN_SESSIONS,
#: which is applied per agent because the adapters run different products. Summing them would let
#: 8 Claude Code sessions plus 3 from elsewhere clear a gate neither of them clears.
MIDPOINT_MIN_SCORED = 10

#: The moment the ledger cutoff went from 3 days to 14 (commit 1f45fec, docs/PRD.md §8 D4).
#: Before it, a session that outlived three days had its injection rows pruned before SessionEnd
#: could score them, and `log_uptake_event` returned silently — 93 sessions distilled inside the
#: window, 11 with an uptake row. The owner chose to keep the window and split the sample rather
#: than reset (§8 D4), so this boundary has to be mechanical: it is the commit timestamp, not a
#: date anybody picked after looking at the rates.
LEDGER_REPAIR_AT = "2026-09-02T00:45:38+00:00"


def _instant(value):
    """Parse an event timestamp to an aware datetime, or None.

    Compared as datetimes, not as strings: the rows carry a UTC offset and a lexical compare
    silently reorders anything written with a different one.
    """
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def window_today(override=None):
    """Today, on the window's calendar. `BORING_TODAY` overrides it for tests and rehearsal.

    One definition, so the briefing and doctor cannot disagree about what day the gate is on.
    """
    return override or os.environ.get("BORING_TODAY") or datetime.now(WINDOW_TZ).date().isoformat()


def partition_at_repair(rows, boundary=LEDGER_REPAIR_AT):
    """Split uptake rows into the ones the broken instrument produced and the ones it did not.

    The pre-repair rows are not thrown away — they were real sessions and hiding them would be the
    same move as quoting them. They are reported separately and the verdict reads only the post
    half, which is what "keep the window and split the sample" means in practice.
    """
    edge = _instant(boundary)
    pre, post = [], []
    for row in rows or []:
        at = _instant(row.get("observed_at"))
        # A row whose timestamp will not parse cannot be placed on either side, and guessing would
        # place it wherever the guess is convenient. It counts as pre — the conservative side,
        # since that is the half the verdict does not read.
        (post if (at and edge and at >= edge) else pre).append(row)
    return pre, post


def field(row, key):
    """Counters live at the top level or inside `attributes` depending on the writer."""
    value = row.get(key)
    if value is None:
        value = (row.get("attributes") or {}).get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def collect(rows, since=None, agent=None):
    """Fold uptake events into per-agent totals, skipping rows the instrument predates.

    A session logged before `used_control_prompts` existed reports 0 for it, which would read as
    a zero chance rate rather than as a missing measurement — so those rows are dropped, and the
    count of what was dropped is returned rather than hidden.
    """
    per_agent = defaultdict(lambda: defaultdict(int))
    skipped_old = 0
    for row in rows:
        if row.get("event") != "injection_uptake":
            continue
        observed = row.get("observed_at") or ""
        if since and observed[:10] < since:
            continue
        who = row.get("agent") or (row.get("attributes") or {}).get("agent") or "unknown"
        if agent and who != agent:
            continue
        has_control_prompts = row.get("used_control_prompts") is not None or (
            "used_control_prompts" in (row.get("attributes") or {})
        )
        if not has_control_prompts:
            skipped_old += 1
            continue
        bucket = per_agent[who]
        bucket["sessions"] += 1
        for key in COUNTERS:
            bucket[key] += field(row, key)
    return per_agent, skipped_old


