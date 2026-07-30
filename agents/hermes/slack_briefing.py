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
from difflib import SequenceMatcher
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
    "해당 항목에 blocked 사항은 없습니다",
    "해당 항목에 차단 사항은 없습니다",
    "해당 항목에 막힘 사항은 없습니다",
    "blocked 사항은 없습니다",
    "차단 사항은 없습니다",
    "막힘 사항은 없습니다",
    "no blockers for this item",
    "no blockers",
    "no blocked items",
]
SOURCE_LIMIT = 5
ITEM_LIMIT = 5
DONE_ITEM_LIMIT = 3  # Done is historical context; keep it short.
BLOCK_ITEM_LIMIT = 4
ITEM_TEXT_MAX_CHARS = 240  # Slack mobile reward; prompt asks for 140, this is a hard cap.
EMPTY_SOURCE_NAMES = {"", "none", "null", "nil", "n/a"}
FUZZY_DEDUP_THRESHOLD = 0.60  # SequenceMatcher ratio; catches LLM paraphrases without over-collapsing.

# Engine fallback messages that look like content but should render as "no data".
EMPTY_ANSWER_PATTERNS = re.compile(
    r"^(No related memory found\.?|No recent work records ingested\.?|"
    r"No work records ingested in the last \d+ hours\.?|"
    r"Brief — No work records ingested in the last \d+ hours\.?)\s*\(?ingest first\)?",
    re.IGNORECASE,
)

LABEL_ALIASES = {
    "Done": "Done",
    "Completed": "Done",
    "완료": "Done",
    "완료됨": "Done",
    "Next": "Next",
    "Next actions": "Next",
    "Todo": "Next",
    "TODO": "Next",
    "다음": "Next",
    "할 일": "Next",
    "해야 할 일": "Next",
    "Blocked": "Blocked",
    "Blockers": "Blocked",
    "막힘": "Blocked",
    "차단": "Blocked",
    "블로커": "Blocked",
    "Decisions": "Decisions",
    "Decision": "Decisions",
    "결정": "Decisions",
    "결정사항": "Decisions",
    "Risks": "Risks",
    "Risk": "Risks",
    "리스크": "Risks",
    "위험": "Risks",
    "Stalled": "Stalled",
    "Stale": "Stalled",
    "정체": "Stalled",
    "정체됨": "Stalled",
    "멈춤": "Stalled",
}
LABEL_PREFIXES = tuple(sorted(LABEL_ALIASES, key=len, reverse=True))
LABEL_SEPARATORS = (":", "：", "-", "–", "—")

# Headings that mean "everything under here is unlabeled/misc".
_MISC_HEADINGS = {"기타", "misc", "others", "other"}

# Briefing is read, not searched. Group by status priority so the reader
# sees "what blocks me" first, "what to do next" second, and "what finished"
# last. Keep sections short; mobile Slack rewards vertical scannability.
SECTION_ORDER = ["Blocked", "Next", "Risks", "Decisions", "Stalled", "Done", ""]
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


BriefEntriesByLabel = dict[str, list[tuple[str, BriefItem]]]


_VAULT_DIR = Path(
    os.environ.get("OMB_VAULT") or Path(__file__).resolve().parents[2] / "vault"
)


def source_label(source: object) -> str:
    name = _source_name(source)
    if _is_empty_source_name(name):
        return ""
    if not name.endswith(".md"):
        return name
    wiki_path = _VAULT_DIR / "wiki" / name
    title = _source_title(wiki_path)
    return f"{title} ({name})" if title else name


def _source_name(source: object) -> str:
    raw = str(source).split("#", 1)[0].strip()
    return os.path.basename(raw) or raw or str(source)


def _source_title(wiki_path: Path) -> str:
    """Extract `title` from YAML frontmatter without requiring PyYAML.

    This keeps the renderer dependency-free so it can run inside minimal
    containers (e.g. hermes-agent) that may not have `yaml` installed.
    """
    try:
        text = wiki_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n")
    if end == -1:
        return ""
    for line in text[4:end].splitlines():
        stripped = line.strip()
        if stripped.startswith("title:"):
            value = stripped.split(":", 1)[1].strip()
            # Drop inline YAML comment.
            value = value.split(" #", 1)[0].rstrip()
            quoted = (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            )
            if quoted:
                return value[1:-1] or ""
            # Reject YAML non-string scalars (lists/maps/numbers/bools/null).
            if value.startswith(("[", "{")) or re.fullmatch(
                r"(true|false|null|~|yes|no|on|off|\d+(\.\d+)?)", value, re.IGNORECASE
            ):
                return ""
            return value or ""
    return ""


def _is_empty_source_name(name: str) -> bool:
    return name.strip().lower() in EMPTY_SOURCE_NAMES


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


def _executive_summary(items_by_label: BriefEntriesByLabel) -> str:
    """Return a one-line priority hook for the reader.

    The summary tells the reader which section deserves attention first,
    without repeating a specific item text that could collide with tests
    or future dedup logic.
    """
    blocked = items_by_label.get("Blocked", [])
    if blocked:
        return f"📌 집중: 🚨 막힘 {len(blocked)}개를 먼저 확인하세요."
    next_items = items_by_label.get("Next", [])
    if next_items:
        return f"📌 집중: ▶️ 다음 행동 {len(next_items)}개가 기다리고 있습니다."
    risks = items_by_label.get("Risks", [])
    if risks:
        return f"📌 집중: ⚠️ 리스크 {len(risks)}개를 점검하세요."
    return ""


def _render_section_entries(
    entries: list[tuple[str, BriefItem]], limit: int
) -> tuple[list[str], int]:
    """Render entries grouped by project name.

    Keeps first-seen project order; multiple items under one project are
    indented so the reader sees project clusters at a glance.
    """
    project_order: list[str] = []
    by_project: dict[str, list[tuple[str, BriefItem]]] = {}
    for project_name, item in entries:
        if project_name not in by_project:
            project_order.append(project_name)
        by_project.setdefault(project_name, []).append((project_name, item))

    lines: list[str] = []
    rendered = 0
    for project_name in project_order:
        group = by_project[project_name]
        remaining = limit - rendered
        if remaining <= 0:
            break
        take = min(len(group), remaining)
        if take == 1 and len(group) == 1:
            item = group[0][1]
            text = _truncate_item_text(_slack_inline(item.text))
            lines.append(f"• {_slack_inline(project_name)} — {text}")
        else:
            lines.append(f"• {_slack_inline(project_name)}")
            for _, item in group[:take]:
                text = _truncate_item_text(_slack_inline(item.text))
                lines.append(f"  • {text}")
        rendered += take

    omitted = max(0, len(entries) - rendered)
    return lines, omitted


def render_body_mrkdwn(answer: str) -> str:
    """Render a priority-first briefing body.

    The reader should grasp the day in one glance:
    1) summary counts, 2) a one-line priority hook, 3) blockers,
    4) next actions, 5) context/decisions, 6) recently done.
    Project names stay attached to each item so context is never lost.
    """
    if EMPTY_ANSWER_PATTERNS.match(answer.strip()):
        return ""

    doc = parse_brief(answer)
    if not doc.projects:
        return _compact_text(answer)

    items_by_label = group_items_by_label(doc)

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
        counts.append(f"{emoji} {len(entries)}")
        lines.append(f"{emoji} *{title}*")
        limit = DONE_ITEM_LIMIT if label == "Done" else ITEM_LIMIT
        section_lines, omitted = _render_section_entries(entries, limit)
        lines.extend(section_lines)
        if omitted:
            lines.append(f"• _외 {omitted}개 항목_")
        lines.append("")

    summary = _executive_summary(items_by_label)
    header_lines = [" · ".join(counts)]
    if summary:
        header_lines.append(summary)
    return "\n".join(header_lines) + "\n\n" + "\n".join(lines).strip()


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
    if EMPTY_ANSWER_PATTERNS.match(answer.strip()):
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": _plain_text(title, 150), "emoji": True}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": _mrkdwn_text(f"`{stamp}`", 2000)}]},
            _section(empty_message),
        ]
        return {"text": f"*{title}*\n`{stamp}`\n\n{empty_message}", "blocks": blocks, "unfurl_links": False, "unfurl_media": False}

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

    items_by_label = group_items_by_label(doc)

    if not any(items_by_label.values()):
        blocks.append(_section(empty_message))
    else:
        counts: list[str] = []
        for label in SECTION_ORDER:
            entries = items_by_label[label]
            if not entries:
                continue
            emoji = SECTION_EMOJI[label]
            title_text = SECTION_TITLE[label]
            counts.append(f"{emoji} {len(entries)}")
            blocks.append({"type": "divider"})
            blocks.append(_section(f"{emoji} *{title_text}*"))
            item_lines: list[str] = []
            block_limit = DONE_ITEM_LIMIT if label == "Done" else BLOCK_ITEM_LIMIT
            for project_name, item in entries[:block_limit]:
                text = _truncate_item_text(item.text)
                item_lines.append(f"• {project_name} — {text}")
            omitted = max(0, len(entries) - block_limit)
            if omitted:
                item_lines.append(f"• _외 {omitted}개 항목_")
            if item_lines:
                blocks.append(_section("\n".join(item_lines)))
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
    labels: list[str] = []
    seen: set[str] = set()
    for source in sources:
        key = _source_name(source)
        if _is_empty_source_name(key):
            continue
        if key in seen:
            continue
        seen.add(key)
        label = source_label(source)
        if label:
            labels.append(label)
        if len(labels) >= SOURCE_LIMIT:
            break
    return "근거: " + " · ".join(labels) if labels else ""


def group_items_by_label(doc: BriefDocument) -> BriefEntriesByLabel:
    items_by_label: BriefEntriesByLabel = {label: [] for label in SECTION_ORDER}
    seen: dict[tuple[str, str], tuple[str, int, set[str]]] = {}
    for project in doc.projects:
        project_key = _project_key(project.name)
        for item in project.items:
            label = item.label or ""
            if label not in items_by_label:
                label = ""
            key = (label, _dedup_key(item.text))
            if key in seen:
                previous_label, index, project_keys = seen[key]
                previous_project, previous_item = items_by_label[previous_label][index]
                if project_key not in project_keys:
                    items_by_label[previous_label][index] = (
                        f"{previous_project} / {project.name}",
                        previous_item,
                    )
                    project_keys.add(project_key)
                continue
            seen[key] = (label, len(items_by_label[label]), {project_key})
            items_by_label[label].append((project.name, item))

    # Cross-label dedup: the same fact sometimes appears under multiple labels
    # because the LLM paraphrases an update. Exact match first, then fuzzy
    # sequence similarity (SequenceMatcher on dedup-normalized text) so Korean
    # particles attached to the same stems do not break paraphrase detection.
    # Keep the highest-priority label so the reader sees the most actionable
    # copy once.
    # Fuzzy comparison is restricted to different labels only; same-label
    # duplicates (including exact text across projects) were already merged above.
    priority = {label: i for i, label in enumerate(SECTION_ORDER)}
    representatives: list[tuple[str, int, str, BriefItem]] = []
    for label in SECTION_ORDER:
        for i, (project_name, item) in enumerate(items_by_label[label]):
            text_key = _dedup_key(item.text)
            merged = False
            for rep_idx, (rep_label, rep_i, rep_project, rep_item) in enumerate(
                representatives
            ):
                if rep_label == label:
                    continue
                rep_key = _dedup_key(rep_item.text)
                if text_key == rep_key or _fuzzy_similarity(
                    text_key, rep_key
                ) >= FUZZY_DEDUP_THRESHOLD:
                    merged = True
                    if priority[label] < priority[rep_label]:
                        representatives[rep_idx] = (label, i, project_name, item)
                    break
            if not merged:
                representatives.append((label, i, project_name, item))

    result: BriefEntriesByLabel = {label: [] for label in SECTION_ORDER}
    for label, _i, project_name, item in representatives:
        result[label].append((project_name, item))
    return result


def parse_brief(answer: str) -> BriefDocument:
    doc = BriefDocument()
    current: BriefProject | None = None
    previous_heading = ""
    pending_label = ""

    for raw in answer.splitlines():
        stripped = raw.strip()
        if not stripped or _is_noise_line(stripped):
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            plain_heading = _plain_label(heading)
            canonical_heading = _canonical_label(plain_heading)
            if canonical_heading is not None:
                # Sub-heading like "### Done" sets the pending label.
                pending_label = canonical_heading
            else:
                if heading and heading != previous_heading:
                    current = BriefProject(heading)
                    doc.projects.append(current)
                    previous_heading = heading
                pending_label = ""
            continue

        plain = _plain_label(stripped)
        canonical_plain = _canonical_label(plain)
        # A bare label line like "Blocked:" sets the pending label, but only
        # if it is not itself a "Label: value" item and not the project side of a
        # flat "project — Label: text" item.
        if canonical_plain is not None and not _looks_like_prefixed_item(stripped):
            bullet = _strip_bullet(stripped) or stripped
            labeled = _label_prefix(bullet)
            if labeled is None or labeled[1] in EMPTY_VALUES:
                pending_label = canonical_plain
                continue
            # Fall through: the line is an actual "Label: value" item.
        # Headings like "기타" / "misc" mean subsequent items have no label.
        if plain.casefold() in _MISC_HEADINGS:
            pending_label = ""
            continue

        # Try the flat "project — Label: text" format the LLM sometimes emits
        # instead of "## project\n- Label: text" headings.
        split = _split_prefixed_item(stripped)
        if split is not None:
            project_name, label, body = split
            if project_name and project_name != previous_heading:
                current = BriefProject(project_name)
                doc.projects.append(current)
                previous_heading = project_name
                pending_label = ""
            item = parse_item(body, label or pending_label)
        else:
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
    normalized = _strip_source_suffix(_slack_inline(_strip_task_marker(text)))
    labeled = _label_prefix(normalized)
    if labeled is not None:
        label, rest = labeled
        rest = _strip_source_suffix(rest)
        if _should_drop_item(rest):
            return None
        return BriefItem(label, rest)
    if pending_label:
        if _should_drop_item(normalized):
            return None
        return BriefItem(pending_label, normalized)
    if _should_drop_item(normalized):
        return None
    return BriefItem("", normalized)


def canonical_label(label: str) -> str:
    return _canonical_label(label) or _plain_label(label)


def _canonical_label(label: str) -> str | None:
    clean = _plain_label(label)
    for alias in LABEL_PREFIXES:
        if alias.casefold() == clean.casefold():
            return LABEL_ALIASES[alias]
    return None


def _label_prefix(text: str) -> tuple[str, str] | None:
    # Strip leading markdown formatting so "*Done*: ..." is treated like "Done: ...".
    stripped = text.strip().lstrip("*_ ").lstrip()
    folded = stripped.casefold()
    for alias in LABEL_PREFIXES:
        if not folded.startswith(alias.casefold()):
            continue
        # Strip bold/italic markers right after the alias (e.g. "Done*:" -> ":"),
        # but keep whitespace so the separator (":", "-", em-dash, etc.) stays.
        rest = stripped[len(alias) :].strip("*_").lstrip()
        for separator in LABEL_SEPARATORS:
            if not rest.startswith(separator):
                continue
            # Drop the separator and any following whitespace; body may start
            # with markdown (e.g. "Done:`code` ...") so do not require a space.
            body = rest[len(separator) :].strip()
            if body:
                return LABEL_ALIASES[alias], body
    return None


_FLAT_ITEM_SEPARATORS = (" — ", " – ", " - ", "--")


def _looks_like_prefixed_item(line: str) -> bool:
    """Return True if the line looks like 'project — Label: text'.

    Avoids treating a bare label line such as 'Blocked:' or a label-value
    line like '해야 할 일 — release note 확인' as a project.
    """
    line = _strip_bullet(line) or line
    for sep in _FLAT_ITEM_SEPARATORS:
        if sep in line:
            left, _, right = line.partition(sep)
            left = left.strip()
            right = right.strip()
            if not left or not right:
                continue
            # The left side must not itself be a label, and the right side
            # must start with a recognized label.
            if _canonical_label(left.lstrip("*_ ").lstrip()) is not None:
                continue
            clean_right = right.lstrip("*_ ").lstrip()
            if _label_prefix(clean_right) is not None:
                return True
    return False


def _split_prefixed_item(line: str) -> tuple[str, str, str] | None:
    """Parse 'project — Label: text' into (project, label, body).

    Returns None when the line is not in this format. The project side may
    contain multiple projects separated by '/'.
    """
    line = _strip_bullet(line) or line
    for sep in _FLAT_ITEM_SEPARATORS:
        if sep not in line:
            continue
        left, _, right = line.partition(sep)
        left = left.strip()
        right = right.strip()
        if not left or not right:
            continue
        # The left side must not itself be a label (avoid splitting
        # "Blocked: something" on the colon or "해야 할 일 — text" on the em-dash).
        if _canonical_label(left.lstrip("*_ ").lstrip()) is not None:
            continue
        # Try to extract a leading label from the right side.
        clean_right = right.lstrip("*_ ").lstrip()
        labeled = _label_prefix(clean_right)
        if labeled is not None:
            label, body = labeled
            return left, label, body
        # No recognized label on the right side: this is not a flat prefixed item.
        # (Do not treat "Done:`code` with — in body" as a project-name prefix.)
        return None
    return None


_NOISE_NUMBER_RE = re.compile(r"^\d+([./:;]\d*)?$")


def _is_noise_line(line: str) -> bool:
    """Drop stray summary counts the LLM sometimes injects."""
    # Pure numbers (e.g. "29", "29.", "5/10") or punctuation-only lines.
    stripped = line.strip("*-–—•· ")
    return bool(_NOISE_NUMBER_RE.match(stripped)) or not stripped


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
    if line.startswith(("- ", "* ", "• ", "– ", "— ")):
        return line[2:].strip()
    for separator in (". ", ") "):
        head, sep, tail = line.partition(separator)
        if sep and head.isdigit():
            return tail.strip()
    return None


def _strip_task_marker(text: str) -> str:
    stripped = text.strip()
    if len(stripped) >= 3 and stripped[0] == "[" and stripped[2] == "]":
        mark = stripped[1].strip().lower()
        if mark in ("", "x"):
            return stripped[3:].strip()
    return text


def _plain_label(line: str) -> str:
    return line.strip().strip("*_").strip().rstrip(":：")


def _slack_inline(text: str) -> str:
    return text.replace("**", "*").strip()


def _truncate_item_text(text: str, limit: int = ITEM_TEXT_MAX_CHARS) -> str:
    """Hard cap for Slack mobile readability; prefers breaking at a word boundary."""
    if len(text) <= limit:
        return text
    # Try to break at the last space before the limit to keep readable words.
    truncated = text[:limit]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated + "…"


def _compact_text(text: str) -> str:
    lines = [_slack_inline(line.strip()) for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip()


def _normalize_korean_particles(text: str) -> str:
    """Strip common Korean particles from word endings for deduplication.

    Two bullets that differ only by particles such as '을/를' or '이/가'
    should collapse to one entry.
    """
    particles = ("을", "를", "이", "가", "은", "는", "과", "와", "으로", "로")
    tokens = text.split()
    out: list[str] = []
    for token in tokens:
        for particle in particles:
            if token.endswith(particle):
                token = token[: -len(particle)]
                break
        if token:
            out.append(token)
    return " ".join(out)


def _dedup_key(text: str) -> str:
    """Normalize item text so near-duplicate bullets collapse to one entry."""
    normalized = (
        _strip_source_suffix(text)
        .lower()
        .replace("c++", "cpp")
        .replace("c#", "csharp")
        .replace(".net", "dotnet")
    )
    normalized = _normalize_korean_particles(normalized)
    chars = [ch if ch.isalnum() else " " for ch in normalized]
    return " ".join("".join(chars).split())


def _fuzzy_similarity(a: str, b: str) -> float:
    """Return sequence similarity of normalized item texts.

    Token Jaccard struggles with Korean particles attached to stems, and
    character n-grams are noisy for short bullets. SequenceMatcher on the
    dedup-normalized string catches paraphrases while keeping distinct facts
    separate.
    """
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _strip_source_suffix(text: str) -> str:
    stripped = text.strip()
    for opener, closer in (("(", ")"), ("[", "]")):
        if not stripped.endswith(closer):
            continue
        start = stripped.rfind(opener)
        if start < 0:
            continue
        inner = stripped[start + len(opener) : -len(closer)].strip()
        if _is_source_suffix(inner):
            return stripped[:start].rstrip()
    return stripped


def _is_source_suffix(text: str) -> bool:
    lowered = text.strip().lower()
    return (
        lowered.startswith(("source:", "sources:", "출처:", "근거:", "出典:"))
        or "vault/wiki/" in lowered
        or ("wiki-" in lowered and ".md" in lowered)
    )


def _project_key(name: str) -> str:
    """Normalize project aliases while keeping slash-delimited workstreams."""
    chars: list[str] = []
    for ch in name.strip().lower():
        if ch.isalnum():
            chars.append(ch)
        elif ch in "+#.":
            chars.append(ch)
        elif ch == "/" and chars and chars[-1] != "/":
            chars.append("/")
    while chars and chars[-1] == "/":
        chars.pop()
    return "".join(chars) or name.strip()


def _is_template_noise(text: str) -> bool:
    """Return True for vacuous 'waiting for instructions' style bullets."""
    lowered = text.lower().strip(" .·")
    return any(noise.lower() in lowered for noise in TEMPLATE_BLACKLIST)


def _should_drop_item(text: str) -> bool:
    return text in EMPTY_VALUES or _is_template_noise(text) or _is_relation_metadata(text)


def _is_relation_metadata(text: str) -> bool:
    lowered = " ".join(text.lower().split())
    return (
        lowered.startswith("shares ")
        and (
            " graph node" in lowered
            or " claim axis" in lowered
            or " claim axes" in lowered
        )
    ) or lowered.startswith("related to vault/wiki/")


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _mrkdwn_text(text, 3000)}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _mrkdwn_text(text, 2000)}]}


def _plain_text(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    return compact[:limit] or "Briefing"


def _mrkdwn_text(text: str, limit: int) -> str:
    return _escape_mrkdwn(text)[:limit] or " "


def _escape_mrkdwn(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
