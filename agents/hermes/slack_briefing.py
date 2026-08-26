"""Slack briefing renderer.

The current Hermes cron path sends stdout as chat.postMessage text, so the
default renderer returns mrkdwn text. The same parsed structure can also emit a
Block Kit payload for a future adapter that posts JSON with `blocks`.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# `label_core` lives in agents/shared/ because the sample floors are the measurement contract,
# shared with the host labelling tool -- copying a floor in here to save an import is how the two
# copies start disagreeing. The installer lands both files flat in ~/.hermes/scripts, where the
# plain import resolves; running from the repo they are siblings, so add that directory first.
_SHARED_DIR = Path(__file__).resolve().parent.parent / "shared"
if _SHARED_DIR.is_dir() and str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import label_core  # noqa: E402


EMPTY_VALUES = {
    "",
    "-",
    "—",
    "~",
    "...",
    "…",
    "없음",
    "없습니다",
    "해당 없음",
    "해당없음",
    "none",
    "None",
    "N/A",
    "n/a",
    "na",
    "null",
    "nil",
    "tbd",
    "to be determined",
    "to be decided",
    "to be continued",
    "추후 진행 예정",
    "추후 예정",
    "추후 결정",
    "추후 협의",
    "추후",
    "later",
    "pending",
    "보류",
    "待定",
    "待ち",
}
# Phrases that add zero signal to a briefing and should be dropped entirely.
TEMPLATE_BLACKLIST = [
    "다음 지시 기다림",
    "다음 지시를 기다림",
    "다음 지시를 기다리는 중",
    "추후 지시 기다림",
    "추후 지시를 기다림",
    "지시 기다림",
    "지시를 기다림",
    "waiting for instructions",
    "awaiting instructions",
    "wait for next instruction",
    "waiting for next steps",
    "to be continued",
]
SOURCE_LIMIT = 5
PROJECT_LIMIT = 6
#: How many items of a group each renderer shows. One number, not two: the text fallback and the
#: Block Kit payload go into the same message, and Slack picks between them — so a reader whose
#: client falls back was being shown a different set of items (5 vs 4) with a different "+N".
#: A fallback that disagrees with the blocks is a second, quieter briefing.
ITEM_LIMIT = 5
DONE_ITEM_LIMIT = 3  # Done is historical context; keep it short.

#: Blocked is never truncated. It is the reason this briefing exists — a "+N more" hiding a
#: blocker is the one omission that can cost the reader their morning.
NEVER_TRUNCATED = frozenset({"Blocked"})
BLOCK_PROJECT_LIMIT = 5

LABEL_ALIASES = {
    "Done": "Done",
    "완료": "Done",
    "Next": "Next",
    "다음": "Next",
    "Blocked": "Blocked",
    "막힘": "Blocked",
    "Decisions": "Decisions",
    # The engine writes these in the singular ("Decision: …", "Risk: …") and the table only had
    # the plurals, so eight labelled items a day fell into the unlabelled bucket and were
    # reported as "기타". The category was not missing; the alias was.
    "Decision": "Decisions",
    "결정": "Decisions",
    "Risks": "Risks",
    "Risk": "Risks",
    "리스크": "Risks",
    # `fact` is the engine's DEFAULT claim kind, not a rare one -- measured 2026-08-13 the ledger
    # held 4936 facts against 1250 decisions -- and the alias table never had it, so the single
    # largest category the distiller produces was arriving every morning as "기타".
    "Facts": "Facts",
    "Fact": "Facts",
    "사실": "Facts",
    "Stalled": "Stalled",
    "Stall": "Stalled",
    "정체": "Stalled",
    "Block": "Blocked",
    "Blocker": "Blocked",
}
LABELS = set(LABEL_ALIASES)

# Briefing is read, not searched. Group by status priority so the reader
# sees "what blocks me" first, "what to do next" second, and "what finished"
# last. Keep sections short; mobile Slack rewards vertical scannability.
SECTION_ORDER = ["Blocked", "Next", "Stalled", "Risks", "Decisions", "Facts", "Done", ""]
SECTION_EMOJI = {
    "Blocked": "🚨",
    "Next": "▶️",
    "Stalled": "⏸️",
    "Risks": "⚠️",
    "Decisions": "💡",
    "Facts": "📌",
    "Done": "✅",
    "": "•",
}
SECTION_TITLE = {
    "Blocked": "막힘",
    "Next": "다음 행동",
    "Stalled": "정체 중",
    "Risks": "리스크",
    "Decisions": "결정",
    "Facts": "사실",
    "Done": "완료",
    "": "기타",
}


@dataclass
class BriefItem:
    label: str
    text: str


@dataclass
class BriefProject:
    name: str
    items: list[BriefItem] = field(default_factory=list)


@dataclass
class BriefDocument:
    projects: list[BriefProject] = field(default_factory=list)


_VAULT_DIR = Path(
    os.environ.get("OMB_VAULT") or Path(__file__).resolve().parents[2] / "vault"
)


def source_label(source: object) -> str:
    name = os.path.basename(str(source)) or str(source)
    if not name.endswith(".md"):
        return name
    wiki_path = _VAULT_DIR / "wiki" / name
    try:
        head = wiki_path.read_text(encoding="utf-8", errors="ignore")[:2048]
        m = re.search(r"^title:\s*(.+)$", head, re.MULTILINE)
        if m:
            title = m.group(1).strip().strip('"').strip("'")
            if title:
                return f"{title} ({name})"
    except Exception:
        pass
    return name


def audit_notice(label_stats) -> str:
    """One line asking for the human labels the verdict is waiting on, or "" when it is not.

    The measurement window closes whether or not anyone audits, and the LLM judge accruing 24 a
    night makes the ledger *look* healthy while the figure that decides anything stays
    uncomputable. Naming the shortfall where the reader already looks every morning is the
    cheapest way to keep a two-week window from ending in "판단 보류".

    Silent once the floor is met: a standing nag is a line readers learn to skip, and the next
    thing they skip is a real one. Silent too when the caller could not read the counts at all --
    an unreachable endpoint is not evidence that the whole floor is outstanding, and printing the
    maximum backlog because we know nothing would be a number the reader cannot act on.
    """
    if label_stats is None:
        return ""
    owed = label_core.audit_backlog(label_stats)
    if not owed:
        return ""
    return f"📋 판정 대기 — 사람 라벨 {owed}건 더 필요 · `label-recall.py --audit`"


def render_message_mrkdwn(
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
    label_stats=None,
) -> str:
    body = render_body_mrkdwn(answer)
    if not body:
        body = empty_message
    out = f"{title}\n`{stamp}`\n\n{body}"
    notice = audit_notice(label_stats)
    if notice:
        out += f"\n\n{notice}"
    source_text = render_sources(sources)
    if source_text:
        out += f"\n\n_{source_text}_"
    return out


def render_body_mrkdwn(answer: str) -> str:
    """Render a priority-first briefing body.

    The reader should grasp the day in one glance:
    1) summary counts, 2) blockers, 3) next actions, 4) context/decisions,
    5) recently done. Project names stay attached to each item so context
    is never lost.
    """
    doc = parse_brief(answer)
    if not doc.projects:
        return _compact_text(answer)

    items_by_label: dict[str, list[tuple[str, BriefItem]]] = {
        label: [] for label in SECTION_ORDER
    }
    seen: dict[str, tuple[str, BriefItem]] = {}
    for project in doc.projects:
        for item in project.items:
            label = item.label or ""
            if label not in items_by_label:
                label = ""
            key = _dedup_key(item.text)
            if key in seen:
                prev_project, prev_item = seen[key]
                # Merge project names if the text is identical.
                if project.name not in prev_project.split(" / "):
                    seen[key] = (f"{prev_project} / {project.name}", prev_item)
                continue
            seen[key] = (project.name, item)
            items_by_label[label].append((project.name, item))

    if not any(items_by_label.values()):
        return _compact_text(answer)

    counts: list[str] = []
    lines: list[str] = []

    # The same shortlist the blocks lead with. Slack picks between the two renderings, so a
    # reader who falls back must not lose the one thing the message exists to answer.
    # The shortlist earns its place by saving a scroll. When the message is short enough that
    # every pick is already visible in the groups below without scrolling, it only repeats
    # itself — a summary of a screenful is not a summary.
    picks = top_picks(items_by_label)
    if picks and _shortlist_earns_its_place(items_by_label):
        lines.append("*오늘의 1순위*")
        lines.extend(
            f"{n}. {SECTION_EMOJI[label]} {_slack_inline(project_name)} — {_slack_inline(item.text)}"
            for n, (label, project_name, item) in enumerate(picks, 1)
        )
        lines.append("")

    for label in SECTION_ORDER:
        entries = items_by_label[label]
        if entries:
            counts.append(f"{SECTION_EMOJI[label]} {SECTION_TITLE[label]} {len(entries)}")

    for zone_title, labels in ZONES:
        entries = zone_entries(items_by_label, labels)
        if not entries:
            continue
        limit = sum(group_limit(label, len(items_by_label.get(label, ()))) for label in labels)
        lines.append(f"*{zone_title}* ({len(entries)})")
        lines.extend(render_zone_lines(entries, limit))
        followup = zone_followup(zone_title, entries)
        if followup:
            lines.append(f"_→ {followup}_")
        lines.append("")

    done = items_by_label.get("Done") or []
    if done:
        lines.append(f"{SECTION_EMOJI['Done']} {SECTION_TITLE['Done']} {len(done)}건 — 상세는 위키")
    return f"{' · '.join(counts)}\n\n" + "\n".join(lines).strip()


#: Groups that answer "what do I do now", in the order a reader should meet them. Everything else
#: is confirmation, and confirmation belongs below the fold.
ACTIONABLE = ("Blocked", "Stalled", "Next")

#: Six status headings were more precision than the classifier behind them can deliver: the
#: distiller's own labels wander (a "Blocked" row that is really a task — see the 2026-08-26
#: artifact), and a six-way surface amplifies that instead of absorbing it. The reader's question
#: is not "which status is this" but "do I have to move" — so the headings collapse to that, and
#: the status survives as a per-item emoji rather than a box the item might be in wrongly.
#: The unlabelled bucket rides in 참고 rather than getting a zone of its own. It must ride
#: somewhere: an item the distiller failed to label is still an item, and dropping it would make
#: the briefing quietly lossy — which is worse than the ugly "기타" heading it replaces.
ZONES = (
    ("행동", ("Blocked", "Stalled", "Next")),
    ("참고", ("Risks", "Decisions", "Facts", "")),
)

#: What to ask next, per zone. The briefing is a summary of notes the reader cannot open from
#: Slack — there is no URL for a vault file — so naming the sources bought nothing actionable.
#: Naming the MCP call does: the reader is already sitting in front of an agent that can run it,
#: and these are the tools that actually answer each zone (verified against tools/list).
#: The MCP call that digs into a zone. Naming the sources bought nothing — Slack cannot open a
#: vault file — but naming the call does: the reader is already sitting in front of an agent that
#: can run it. `{p}` is filled with a project the zone actually contains rather than left as a
#: placeholder, because the briefing already knows the name.
#:
#: Deterministic tools lead. `recall` and `claims` embed and return; `next_actions`, `stalled`,
#: `decisions`, `risks` and `ask` all run the local LLM, and `next_actions` was measured at over
#: 30s with no response. Suggesting a call that leaves the reader waiting half a minute is worse
#: than suggesting nothing, so the slow ones are named second and marked.
ZONE_FOLLOWUP = {
    "행동": '`recall("…", "{p}")` · 느림 `next_actions("{p}")`',
    "참고": '`claims("{p}")` · 느림 `decisions("{p}")`',
}


def zone_followup(zone_title: str, entries) -> str:
    """The MCP call that digs into this zone, aimed at the project carrying the most of it."""
    template = ZONE_FOLLOWUP.get(zone_title)
    if not template or not entries:
        return ""
    counts: dict[str, int] = {}
    for _label, project_name, _item in entries:
        counts[project_name] = counts.get(project_name, 0) + 1
    busiest = max(counts, key=lambda name: (counts[name], name))
    return template.format(p=_slack_inline(busiest))

#: How many items the top-of-message shortlist carries. Three is what fits above the fold on a
#: phone next to a header and a count line; a shortlist that needs scrolling is not a shortlist.
TOP_PICKS = 3


def top_picks(items_by_label, limit=TOP_PICKS):
    """The first thing the reader should look at, drawn from the actionable groups in order.

    A briefing that opens with a status ledger makes the reader do the triage the briefing was
    supposed to do. Blocked first because it is the reason the message exists, then Stalled
    (something has been sitting), then Next. Returns [] when nothing is actionable — a quiet day
    should not manufacture a priority.
    """
    picks: list[tuple[str, str, Any]] = []
    for label in ACTIONABLE:
        for project_name, item in items_by_label.get(label, []):
            picks.append((label, project_name, item))
            if len(picks) >= limit:
                return picks
    return picks


#: Below this many actionable items the whole message fits on one screen, so a shortlist would
#: only restate what is already visible. Measured against real briefings, which carry 20-30.
SHORTLIST_MIN_ITEMS = 6


def _shortlist_earns_its_place(items_by_label) -> bool:
    """True when there is enough to triage that naming the top three saves the reader a scroll."""
    actionable = sum(len(items_by_label.get(label, ())) for label in ACTIONABLE)
    return actionable >= SHORTLIST_MIN_ITEMS


def group_by_project(entries):
    """[(project, item)] -> [(project, [items])], first-seen order.

    Five consecutive lines that all begin with the same project name spend the first twenty
    characters of every line saying nothing new, and on a phone that is most of the line. The
    name is written once and its items nest under it.
    """
    grouped: list[tuple[str, list]] = []
    index: dict[str, int] = {}
    for project_name, item in entries:
        pos = index.get(project_name)
        if pos is None:
            index[project_name] = len(grouped)
            grouped.append((project_name, [item]))
        else:
            grouped[pos][1].append(item)
    return grouped


#: Endings the distiller pads every line with. Korean puts the verb last, so truncating from the
#: front deletes the action and leaves the object — which is why these are shaved rather than the
#: line being cut. Each pattern keeps the verb stem and drops only the politeness tail, and a line
#: that matches nothing is left exactly as written.
_ENDING_TRIMS = (
    ("해야 합니다.", ""),
    ("해야 한다.", ""),
    ("이 필요합니다.", " 필요"),
    ("가 필요합니다.", " 필요"),
    ("이 필요함.", " 필요"),
    ("가 필요함.", " 필요"),
    ("하였습니다.", "함"),
    ("했습니다.", "함"),
    ("합니다.", "함"),
    ("됩니다.", "됨"),
    ("입니다.", ""),
    ("되었습니다.", "됨"),
    ("있습니다.", "있음"),
)


def shave_ending(text: str) -> str:
    """Drop the politeness tail, keep the verb.

    "…근거를 보강해야 합니다" -> "…근거를 보강". Purely a rendering concern: the wording comes from
    the distillation prompt, and changing that would change the notes the injection channel is
    being measured on, which is frozen until the window closes (docs/PRD.md §5-R3).
    """
    stripped = text.rstrip()
    for tail, replacement in _ENDING_TRIMS:
        if stripped.endswith(tail):
            return (stripped[: -len(tail)] + replacement).rstrip()
    return text


def zone_entries(items_by_label, labels):
    """[(label, project, item)] for one zone, in the labels' priority order."""
    out = []
    for label in labels:
        out.extend((label, project_name, item) for project_name, item in items_by_label.get(label, []))
    return out


def render_zone_lines(entries, limit, sub="   ◦"):
    """Lines for one zone: status rides on the item, the project name is written once.

    The status heading is gone, so each line carries its own emoji — a reader still sees that
    something is blocked rather than merely next, without the briefing having to be right about
    which of six boxes it belongs in.
    """
    lines: list[str] = []
    shown = 0
    grouped: list[tuple[str, list[tuple[str, object]]]] = []
    index: dict[str, int] = {}
    for label, project_name, item in entries:
        pos = index.get(project_name)
        if pos is None:
            index[project_name] = len(grouped)
            grouped.append((project_name, [(label, item)]))
        else:
            grouped[pos][1].append((label, item))

    for project_name, rows in grouped:
        if shown >= limit:
            break
        take = rows[: limit - shown]
        shown += len(take)
        name = _slack_inline(project_name)
        if len(take) == 1:
            label, item = take[0]
            lines.append(f"{SECTION_EMOJI[label]} {name} — {_slack_inline(item.text)}")
        else:
            # Mixed statuses under one project keep their own emoji on each row.
            lines.append(f"• {name}")
            lines.extend(
                f"{sub} {SECTION_EMOJI[label]} {_slack_inline(item.text)}" for label, item in take
            )
    omitted = len(entries) - shown
    if omitted > 0:
        lines.append(f"• _외 {omitted}개 항목_")
    return lines


def render_group_lines(entries, limit, bullet="•", sub="   ◦"):
    """Lines for one status group, nested under project names and honouring the item limit.

    Shared by both renderers so a reader who falls back to text sees the same items in the same
    shape — the limits already agree, and the layout has to as well.
    """
    lines: list[str] = []
    shown = 0
    for project_name, items in group_by_project(entries):
        if shown >= limit:
            break
        room = limit - shown
        take = items[:room]
        shown += len(take)
        name = _slack_inline(project_name)
        if len(take) == 1:
            lines.append(f"{bullet} {name} — {_slack_inline(take[0].text)}")
        else:
            lines.append(f"{bullet} {name}")
            lines.extend(f"{sub} {_slack_inline(i.text)}" for i in take)
    omitted = sum(len(items) for _n, items in group_by_project(entries)) - shown
    if omitted > 0:
        lines.append(f"{bullet} _외 {omitted}개 항목_")
    return lines


def group_limit(label: str, total: int) -> int:
    """How many items of this group to show. The single source both renderers ask.

    Blocked returns everything: an unseen blocker is the failure mode the priority order exists
    to prevent, and a section can be split before a blocker is hidden behind "+N".
    """
    if label in NEVER_TRUNCATED:
        return total
    return DONE_ITEM_LIMIT if label == "Done" else ITEM_LIMIT


def render_blocks_payload(
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
    label_stats=None,
) -> dict[str, Any]:
    """Block Kit version of the priority-first briefing.

    Uses single-column sections instead of two-column fields: each status
    group is a clear visual chunk on mobile.
    """
    doc = parse_brief(answer)
    fallback = render_message_mrkdwn(title, stamp, answer, sources, empty_message, label_stats)
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _plain_text(title, 150), "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _mrkdwn_text(f"`{stamp}`", 2000)}],
        },
    ]

    items_by_label: dict[str, list[tuple[str, BriefItem]]] = {
        label: [] for label in SECTION_ORDER
    }
    seen: dict[str, tuple[str, BriefItem]] = {}
    for project in doc.projects:
        for item in project.items:
            label = item.label or ""
            if label not in items_by_label:
                label = ""
            key = _dedup_key(item.text)
            if key in seen:
                prev_project, prev_item = seen[key]
                if project.name not in prev_project.split(" / "):
                    seen[key] = (f"{prev_project} / {project.name}", prev_item)
                continue
            seen[key] = (project.name, item)
            items_by_label[label].append((project.name, item))

    if not any(items_by_label.values()):
        blocks.append(_section(empty_message))
    else:
        # The count line says the words, not just the emoji: a reader should not have to have
        # memorised a legend to know that 🚨 2 means two things are blocking them.
        counts = [
            f"{SECTION_EMOJI[label]} {SECTION_TITLE[label]} {len(items_by_label[label])}"
            for label in SECTION_ORDER
            if items_by_label[label]
        ]

        # The shortlist goes above everything, because the question at 9am is "what first" and a
        # status ledger makes the reader answer it themselves.
        picks = top_picks(items_by_label)
        if picks and _shortlist_earns_its_place(items_by_label):
            pick_lines = [
                f"{n}. {SECTION_EMOJI[label]} {project_name} — {item.text}"
                for n, (label, project_name, item) in enumerate(picks, 1)
            ]
            blocks.append(_section("*오늘의 1순위*\n" + "\n".join(pick_lines)))
            blocks.append({"type": "divider"})

        for zone_title, labels in ZONES:
            entries = zone_entries(items_by_label, labels)
            if not entries:
                continue
            # Done stays out of the zones entirely: it is confirmation, not work, and on a phone
            # sixteen finished items push the blockers off the first screen.
            zone_limit = sum(
                group_limit(label, len(items_by_label.get(label, ()))) for label in labels
            )
            item_lines = render_zone_lines(entries, zone_limit)
            blocks.append(
                _section(f"*{zone_title}* ({len(entries)})\n" + "\n".join(item_lines))
            )
            followup = zone_followup(zone_title, entries)
            if followup:
                # A context block: present when wanted, visually quiet when not.
                blocks.append(_context(f"→ {followup}"))

        done = items_by_label.get("Done") or []
        if done:
            blocks.append({"type": "divider"})
            blocks.append(
                _context(f"{SECTION_EMOJI['Done']} {SECTION_TITLE['Done']} {len(done)}건 — 상세는 위키")
            )
        blocks.insert(2, _context(" · ".join(counts)))

    source_text = render_sources(sources)
    notice = audit_notice(label_stats)
    if notice:
        blocks.append({"type": "divider"})
        blocks.append(_context(notice))
    if source_text:
        blocks.append({"type": "divider"})
        blocks.append(_context(source_text))
    return {
        "text": fallback,
        "blocks": blocks[:50],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def render_weekly_blocks(title, stamp, projects, intervention, board, trend, sources):
    """Block Kit for the weekly: persistence and trend, never closure.

    Item bullets are deliberately absent except one per persistent project, quoted from the most
    recent daily rather than re-summarised. A weekly that re-lists items is the same information a
    seventh time; what it uniquely knows is which projects held a state all week — something the
    reader could only see by deduping seven messages in their head.
    """
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _plain_text(title, 150), "emoji": True},
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _mrkdwn_text(stamp, 2000)}]},
    ]

    if intervention:
        lines = []
        for week, label, count, span in intervention:
            head = f"• {week.name} — {count}/{span}일 {SECTION_EMOJI.get(label, '•')}"
            lines.append(f"{head}\n  {week.latest_line}" if week.latest_line else head)
        blocks.append(_section("*개입 필요 — 상태가 주 내내 지속*\n" + "\n".join(lines)))
        blocks.append({"type": "divider"})

    if board:
        # Two columns are right here and wrong on the daily: these values are short. Slack caps
        # a section at 10 fields, which is why the caller bounds the list before it arrives.
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": _mrkdwn_text(f"*{w.name}*\n{len(w.days)}/{span}일", 2000)}
                    for w, span in board
                ][:10],
            }
        )

    if trend:
        parts = [
            f"{SECTION_EMOJI.get(label, '•')} {SECTION_TITLE.get(label, label)} {first}→{last}"
            for label, (first, last) in trend.items()
        ]
        # The caveat is part of the contract, not decoration: a reader who adds ✅ 10→10 into
        # "twenty done" has been lied to, and every day here is an independent re-synthesis.
        blocks.append(_context(" · ".join(parts) + " — 일자별 관측치이며 마감률이 아니다"))

    source_text = render_sources(sources)
    if source_text:
        blocks.append(_context(source_text))
    return blocks[:50]


def render_sources(sources: list[object]) -> str:
    """One line naming the corpus, not five titles.

    The full list ran to 277 characters of wiki filenames and titles that the item lines had
    already said. Slack cannot open a vault file — there is no URL — so the reader can do nothing
    with them; what survives is the trust signal that an answer came from the corpus at all.
    """
    labels = [source_label(source) for source in sources[:SOURCE_LIMIT]]
    if not labels:
        return ""
    first = labels[0].split(" (")[-1].rstrip(")") if " (" in labels[0] else labels[0]
    rest = len(labels) - 1
    return f"근거: 위키 {len(labels)}건 ({first}" + (f" 외 {rest})" if rest else ")")


def parse_brief(answer: str) -> BriefDocument:
    doc = BriefDocument()
    current: BriefProject | None = None
    previous_heading = ""
    pending_label = ""

    for raw in answer.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            plain_heading = _plain_label(heading)
            if plain_heading in LABELS:
                # Sub-heading like "### Done" sets the pending label.
                pending_label = canonical_label(plain_heading)
            else:
                if heading and heading != previous_heading:
                    current = BriefProject(heading)
                    doc.projects.append(current)
                    previous_heading = heading
                pending_label = ""
            continue

        plain = _plain_label(stripped)
        if plain in LABELS:
            pending_label = canonical_label(plain)
            continue

        bullet = _strip_bullet(stripped)
        item = parse_item(bullet if bullet is not None else stripped, pending_label)
        # A plain (non-bullet) line consumes the pending label; a bullet line keeps
        # it so multiple bullets under one label heading share the same label.
        if bullet is None:
            pending_label = ""
        if item is None:
            continue
        if current is None:
            current = BriefProject("Brief")
            doc.projects.append(current)
        current.items.append(item)

    doc.projects = [project for project in doc.projects if project.items]
    return doc


def parse_item(text: str, pending_label: str = "") -> BriefItem | None:
    normalized = _slack_inline(text)
    for label in LABELS:
        for sep in (":", "：", " - ", " — "):
            prefix = f"{label}{sep}"
            if normalized.startswith(prefix):
                rest = normalized[len(prefix) :].strip()
                if rest in EMPTY_VALUES or _is_template_noise(rest):
                    return None
                return BriefItem(canonical_label(label), rest)
    if pending_label:
        if normalized in EMPTY_VALUES or _is_template_noise(normalized):
            return None
        return BriefItem(pending_label, normalized)
    if normalized in EMPTY_VALUES or _is_template_noise(normalized):
        return None
    return BriefItem("", normalized)


def canonical_label(label: str) -> str:
    return LABEL_ALIASES.get(label, label)


def maybe_print_blocks_json(
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
    label_stats=None,
) -> bool:
    if os.environ.get("BORING_BRIEFING_FORMAT", "").strip().lower() != "blocks":
        return False
    payload = render_blocks_payload(title, stamp, answer, sources, empty_message, label_stats)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return True


def _strip_bullet(line: str) -> str | None:
    if line.startswith(("- ", "* ", "• ")):
        return line[2:].strip()
    head, sep, tail = line.partition(". ")
    if sep and head.isdigit():
        return tail.strip()
    return None


def _plain_label(line: str) -> str:
    return line.strip().strip("*").strip().rstrip(":：")


def _slack_inline(text: str) -> str:
    return shave_ending(text.replace("**", "*").strip())


def _compact_text(text: str) -> str:
    lines = [_slack_inline(line.strip()) for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def _dedup_key(text: str) -> str:
    """Normalize item text so near-duplicate bullets collapse to one entry."""
    return " ".join(text.lower().split())


def _is_template_noise(text: str) -> bool:
    """Return True for vacuous 'waiting for instructions' style bullets."""
    lowered = text.lower().strip(" .·")
    return any(noise.lower() in lowered for noise in TEMPLATE_BLACKLIST)


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _mrkdwn_text(text, 3000)}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _mrkdwn_text(text, 2000)}]}


def _plain_text(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact[:limit] or "Briefing"


def _mrkdwn_text(text: str, limit: int) -> str:
    """Escape and fit into Slack's per-field limit, saying so when something was dropped.

    A bare slice cuts mid-sentence and mid-word with no sign it happened, which is how a reader
    ends up trusting a sentence that was never finished. Cut at a line boundary when there is one
    nearby, and always leave the ellipsis behind.
    """
    escaped = _escape_mrkdwn(text)
    if len(escaped) <= limit:
        return escaped or " "
    head = escaped[: limit - 2]
    cut = head.rfind("\n")
    if cut > limit // 2:
        head = head[:cut]
    return head.rstrip() + "…"


def _escape_mrkdwn(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
