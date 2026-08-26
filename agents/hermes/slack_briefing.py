"""Slack briefing renderer.

The current Hermes cron path sends stdout as chat.postMessage text, so the
default renderer returns mrkdwn text. The same parsed structure can also emit a
Block Kit payload for a future adapter that posts JSON with `blocks`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    "결정": "Decisions",
    "Risks": "Risks",
    "리스크": "Risks",
    "Stalled": "Stalled",
    "정체": "Stalled",
}
LABELS = set(LABEL_ALIASES)

# Briefing is read, not searched. Group by status priority so the reader
# sees "what blocks me" first, "what to do next" second, and "what finished"
# last. Keep sections short; mobile Slack rewards vertical scannability.
SECTION_ORDER = ["Blocked", "Next", "Stalled", "Risks", "Decisions", "Done", ""]
SECTION_EMOJI = {
    "Blocked": "🚨",
    "Next": "▶️",
    "Stalled": "⏸️",
    "Risks": "⚠️",
    "Decisions": "💡",
    "Done": "✅",
    "": "•",
}
SECTION_TITLE = {
    "Blocked": "막힘",
    "Next": "다음 행동",
    "Stalled": "정체 중",
    "Risks": "리스크",
    "Decisions": "결정",
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


def render_message_mrkdwn(
    title: str,
    stamp: str,
    answer: str,
    sources: list[object],
    empty_message: str,
) -> str:
    body = render_body_mrkdwn(answer)
    if not body:
        body = empty_message
    out = f"{title}\n`{stamp}`\n\n{body}"
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
    for label in SECTION_ORDER:
        entries = items_by_label[label]
        if not entries:
            continue
        emoji = SECTION_EMOJI[label]
        title = SECTION_TITLE[label]
        counts.append(f"{emoji} {title} {len(entries)}")
        lines.append(f"{emoji} *{title}*")
        limit = group_limit(label, len(entries))
        for project_name, item in entries[:limit]:
            text = _slack_inline(item.text)
            lines.append(f"• {_slack_inline(project_name)} — {text}")
        omitted = max(0, len(entries) - limit)
        if omitted:
            lines.append(f"• _외 {omitted}개 항목_")
        lines.append("")

    return f"{' · '.join(counts)}\n\n" + "\n".join(lines).strip()


#: Groups that answer "what do I do now", in the order a reader should meet them. Everything else
#: is confirmation, and confirmation belongs below the fold.
ACTIONABLE = ("Blocked", "Stalled", "Next")

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
) -> dict[str, Any]:
    """Block Kit version of the priority-first briefing.

    Uses single-column sections instead of two-column fields: each status
    group is a clear visual chunk on mobile.
    """
    doc = parse_brief(answer)
    fallback = render_message_mrkdwn(title, stamp, answer, sources, empty_message)
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
        if picks:
            pick_lines = [
                f"{n}. {SECTION_EMOJI[label]} {project_name} — {item.text}"
                for n, (label, project_name, item) in enumerate(picks, 1)
            ]
            blocks.append(_section("*오늘의 1순위*\n" + "\n".join(pick_lines)))
            blocks.append({"type": "divider"})

        for label in SECTION_ORDER:
            entries = items_by_label[label]
            if not entries:
                continue
            emoji = SECTION_EMOJI[label]
            title_text = SECTION_TITLE[label]
            # Done is confirmation, not work. One context line at the bottom, no bullets: on a
            # phone, ten finished items push the blockers off the first screen.
            if label == "Done":
                continue
            block_limit = group_limit(label, len(entries))
            item_lines = [
                f"• {project_name} — {item.text}" for project_name, item in entries[:block_limit]
            ]
            omitted = max(0, len(entries) - block_limit)
            if omitted:
                item_lines.append(f"• _외 {omitted}개 항목_")
            # Label and items share one section: a label-only section plus a divider cost two
            # blocks each and bought nothing but scrolling.
            blocks.append(
                _section(f"{emoji} *{title_text}* ({len(entries)})\n" + "\n".join(item_lines))
            )

        done = items_by_label.get("Done") or []
        if done:
            blocks.append({"type": "divider"})
            blocks.append(
                _context(f"{SECTION_EMOJI['Done']} {SECTION_TITLE['Done']} {len(done)}건 — 상세는 위키")
            )
        blocks.insert(2, _context(" · ".join(counts)))

    source_text = render_sources(sources)
    if source_text:
        blocks.append({"type": "divider"})
        blocks.append(_context(source_text))
    return {
        "text": fallback,
        "blocks": blocks[:50],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def render_sources(sources: list[object]) -> str:
    labels = [source_label(source) for source in sources[:SOURCE_LIMIT]]
    return "근거: " + " · ".join(labels) if labels else ""


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
) -> bool:
    if os.environ.get("BORING_BRIEFING_FORMAT", "").strip().lower() != "blocks":
        return False
    payload = render_blocks_payload(title, stamp, answer, sources, empty_message)
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
    return text.replace("**", "*").strip()


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
