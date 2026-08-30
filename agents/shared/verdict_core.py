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
