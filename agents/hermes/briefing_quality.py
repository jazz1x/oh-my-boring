"""브리핑 품질 감사 — Briefing Quality Discipline.

Implements the measurable contracts defined in
`vault/.rules/briefing-quality-discipline.md`.

The module is intentionally dependency-free (stdlib only) so it can run
inside the hermes-agent container alongside the renderer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from slack_briefing import (
    EMPTY_ANSWER_PATTERNS,
    EMPTY_VALUES,
    ITEM_TEXT_MAX_CHARS,
    SECTION_ORDER,
    SECTION_TITLE,
    TEMPLATE_BLACKLIST,
    _dedup_key,
    _is_relation_metadata,
    _is_template_noise,
    _label_prefix,
    _project_key,
    _slack_inline,
    _source_name,
    _strip_bullet,
    _strip_source_suffix,
    _strip_task_marker,
    group_items_by_label,
    parse_brief,
)


# Mirrors the contract table in vault/.rules/briefing-quality-discipline.md.
QUALITY_CONTRACT = {
    "duplicate_item_rate_max": 0.20,
    "ungrouped_item_rate_max": 0.30,
    "placeholder_item_rate_max": 0.50,
    "source_dedup_rate_max": 0.50,
    "done_dominance_max": 0.80,
}


@dataclass
class QualityMetrics:
    """Measurable quality metrics for a single briefing."""

    kind: str  # "daily" or "weekly"
    empty_fallback_detected: bool = False
    raw_item_count: int = 0
    final_item_count: int = 0
    unique_dedup_key_count: int = 0
    duplicate_item_rate: float = 0.0
    placeholder_count: int = 0
    placeholder_item_rate: float = 0.0
    relation_metadata_count: int = 0
    relation_metadata_rate: float = 0.0
    ungrouped_item_count: int = 0
    ungrouped_item_rate: float = 0.0
    raw_source_count: int = 0
    deduped_source_count: int = 0
    source_dedup_rate: float = 0.0
    date_window_compliant: bool = False
    max_item_length_ok: bool = True
    done_dominance: float = 0.0
    section_counts: dict[str, int] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "empty_fallback_detected": self.empty_fallback_detected,
            "raw_item_count": self.raw_item_count,
            "final_item_count": self.final_item_count,
            "unique_dedup_key_count": self.unique_dedup_key_count,
            "duplicate_item_rate": round(self.duplicate_item_rate, 3),
            "placeholder_count": self.placeholder_count,
            "placeholder_item_rate": round(self.placeholder_item_rate, 3),
            "relation_metadata_count": self.relation_metadata_count,
            "relation_metadata_rate": round(self.relation_metadata_rate, 3),
            "ungrouped_item_count": self.ungrouped_item_count,
            "ungrouped_item_rate": round(self.ungrouped_item_rate, 3),
            "raw_source_count": self.raw_source_count,
            "deduped_source_count": self.deduped_source_count,
            "source_dedup_rate": round(self.source_dedup_rate, 3),
            "date_window_compliant": self.date_window_compliant,
            "max_item_length_ok": self.max_item_length_ok,
            "done_dominance": round(self.done_dominance, 3),
            "section_counts": self.section_counts,
            "violations": self.violations,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


@dataclass
class QualityResult:
    """Outcome of a quality gate check."""

    level: str  # "pass", "warn", or "fail"
    metrics: QualityMetrics
    message: str = ""

    def is_ok(self) -> bool:
        return self.level == "pass"


def _count_raw_items(answer: str) -> int:
    """Estimate the number of candidate items in the raw engine answer.

    A candidate item is any non-empty, non-noise line that looks like a bullet
    or a flat "project — Label: text" entry. This count is used as the
    denominator for placeholder/duplicate rates.
    """
    count = 0
    for raw in answer.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        # Skip stray numbers and pure punctuation.
        if re.fullmatch(r"\d+([./:;]\d*)?", stripped.strip("*-–—•· ")):
            continue
        # Skip headings.
        if stripped.startswith("#"):
            continue
        # Skip bare label lines with no value.
        labeled = _label_prefix(stripped)
        if labeled is not None:
            label, body = labeled
            if body:
                count += 1
            continue
        # Bulleted or plain lines.
        if _strip_bullet(stripped) is not None or stripped:
            count += 1
    return max(count, 0)


def _is_placeholder_text(text: str) -> bool:
    """True if the text is an empty placeholder or template noise."""
    cleaned = text.strip()
    if cleaned in EMPTY_VALUES:
        return True
    if _is_template_noise(cleaned):
        return True
    # Blacklisted phrases that appear anywhere in the line.
    lowered = cleaned.lower()
    return any(noise.lower() in lowered for noise in TEMPLATE_BLACKLIST)


def _collect_item_stats(answer: str) -> tuple[int, int, int]:
    """Return (placeholder_count, relation_metadata_count, raw_count).

    Scans the raw answer line-by-line before the renderer drops items.
    """
    placeholder = 0
    relation_meta = 0
    raw = 0
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Extract body from label-prefixed items or bullets.
        body = _strip_bullet(stripped) or stripped
        labeled = _label_prefix(body)
        if labeled is not None:
            _, body = labeled
        body = _strip_source_suffix(_strip_task_marker(body)).strip()
        if not body:
            continue
        raw += 1
        if _is_placeholder_text(body):
            placeholder += 1
        if _is_relation_metadata(body):
            relation_meta += 1
    return placeholder, relation_meta, raw


def _source_dedup_count(sources: list[object]) -> tuple[int, int]:
    """Return (raw_count, deduped_count) for source names."""
    raw = 0
    seen: set[str] = set()
    for source in sources:
        name = _source_name(source)
        if not name or name.lower() in {"", "none", "null", "nil", "n/a"}:
            continue
        raw += 1
        key = _project_key(name)
        seen.add(key)
    return raw, len(seen)


def _max_rendered_item_length(answer: str) -> int:
    """Return the longest pre-truncation item text length.

    The renderer caps each item at ITEM_TEXT_MAX_CHARS. If the original item
    is already over the limit, the rendered output is truncated, which is a
    quality warning only when many items exceed it.
    """
    doc = parse_brief(answer)
    max_len = 0
    for project in doc.projects:
        for item in project.items:
            text = _strip_source_suffix(_slack_inline(item.text))
            max_len = max(max_len, len(text))
    return max_len


def analyze_briefing_quality(
    answer: str,
    sources: list[object],
    since_hours: int | None,
    until_hours: int | None,
    kind: str = "daily",
) -> QualityMetrics:
    """Compute quality metrics for a rendered briefing."""
    metrics = QualityMetrics(kind=kind)

    # Q01: empty fallback detection.
    metrics.empty_fallback_detected = bool(EMPTY_ANSWER_PATTERNS.match(answer.strip()))
    if metrics.empty_fallback_detected:
        # There is no meaningful content to score; only the date window matters.
        metrics.date_window_compliant = _date_window_compliant(kind, since_hours, until_hours)
        if not metrics.date_window_compliant:
            metrics.violations.append("date_window_compliant=False")
        return metrics

    # Raw item stats (before renderer drops placeholders).
    placeholder_count, relation_meta_count, raw_count = _collect_item_stats(answer)
    metrics.raw_item_count = raw_count
    metrics.placeholder_count = placeholder_count
    metrics.relation_metadata_count = relation_meta_count

    if raw_count > 0:
        metrics.placeholder_item_rate = placeholder_count / raw_count
        metrics.relation_metadata_rate = relation_meta_count / raw_count

    # Parsed document and grouping.
    doc = parse_brief(answer)
    metrics.final_item_count = sum(len(p.items) for p in doc.projects)

    grouped = group_items_by_label(doc)
    total_grouped = sum(len(entries) for entries in grouped.values())
    metrics.section_counts = {
        (SECTION_TITLE.get(label, label) or "기타"): len(entries)
        for label, entries in grouped.items()
    }
    metrics.ungrouped_item_count = len(grouped.get("", []))
    if total_grouped > 0:
        metrics.ungrouped_item_rate = metrics.ungrouped_item_count / total_grouped
        metrics.done_dominance = len(grouped.get("Done", [])) / total_grouped

    # Duplicate rate: compare raw candidates to unique dedup keys.
    unique_keys: set[str] = set()
    for project in doc.projects:
        for item in project.items:
            unique_keys.add(_dedup_key(item.text))
    metrics.unique_dedup_key_count = len(unique_keys)
    if metrics.final_item_count > 0:
        metrics.duplicate_item_rate = (
            metrics.final_item_count - metrics.unique_dedup_key_count
        ) / metrics.final_item_count

    # Source dedup.
    raw_src, deduped_src = _source_dedup_count(sources)
    metrics.raw_source_count = raw_src
    metrics.deduped_source_count = deduped_src
    if raw_src > 0:
        metrics.source_dedup_rate = (raw_src - deduped_src) / raw_src

    # Date window compliance.
    metrics.date_window_compliant = _date_window_compliant(kind, since_hours, until_hours)

    # Item length.
    metrics.max_item_length_ok = _max_rendered_item_length(answer) <= ITEM_TEXT_MAX_CHARS

    # Build violation list.
    violations: list[str] = []
    if metrics.duplicate_item_rate > QUALITY_CONTRACT["duplicate_item_rate_max"]:
        violations.append(
            f"duplicate_item_rate={metrics.duplicate_item_rate:.2f} > "
            f"{QUALITY_CONTRACT['duplicate_item_rate_max']}"
        )
    if metrics.ungrouped_item_rate > QUALITY_CONTRACT["ungrouped_item_rate_max"]:
        violations.append(
            f"ungrouped_item_rate={metrics.ungrouped_item_rate:.2f} > "
            f"{QUALITY_CONTRACT['ungrouped_item_rate_max']}"
        )
    if metrics.placeholder_item_rate > QUALITY_CONTRACT["placeholder_item_rate_max"]:
        violations.append(
            f"placeholder_item_rate={metrics.placeholder_item_rate:.2f} > "
            f"{QUALITY_CONTRACT['placeholder_item_rate_max']}"
        )
    if metrics.source_dedup_rate > QUALITY_CONTRACT["source_dedup_rate_max"]:
        violations.append(
            f"source_dedup_rate={metrics.source_dedup_rate:.2f} > "
            f"{QUALITY_CONTRACT['source_dedup_rate_max']}"
        )
    if metrics.done_dominance > QUALITY_CONTRACT["done_dominance_max"]:
        violations.append(
            f"done_dominance={metrics.done_dominance:.2f} > "
            f"{QUALITY_CONTRACT['done_dominance_max']}"
        )
    if not metrics.date_window_compliant:
        violations.append("date_window_compliant=False")
    if not metrics.max_item_length_ok:
        violations.append("max_item_length_ok=False")
    metrics.violations = violations

    return metrics


def _date_window_compliant(
    kind: str, since_hours: int | None, until_hours: int | None
) -> bool:
    """Check that the time window matches the briefing kind."""
    if since_hours is None or since_hours <= 0:
        return False
    if kind == "daily":
        # Daily briefing covers roughly the last 24 hours.
        return since_hours == 24
    if kind == "weekly":
        # Weekly briefing must have an upper bound and a 7-day span.
        if until_hours is None or until_hours < 0:
            return False
        return (since_hours - until_hours) == 168
    return False


def check_briefing_quality(
    answer: str,
    sources: list[object],
    since_hours: int | None,
    until_hours: int | None,
    kind: str = "daily",
) -> QualityResult:
    """Run the quality gate and return pass/warn/fail.

    Failures block rendering; warnings allow rendering but log the issue.
    """
    metrics = analyze_briefing_quality(answer, sources, since_hours, until_hours, kind)

    # Failures are contract breaches that make the briefing misleading.
    if metrics.violations:
        if (
            metrics.duplicate_item_rate > QUALITY_CONTRACT["duplicate_item_rate_max"]
            or metrics.ungrouped_item_rate > QUALITY_CONTRACT["ungrouped_item_rate_max"]
            or metrics.placeholder_item_rate > QUALITY_CONTRACT["placeholder_item_rate_max"]
            or metrics.source_dedup_rate > QUALITY_CONTRACT["source_dedup_rate_max"]
            or metrics.done_dominance > QUALITY_CONTRACT["done_dominance_max"]
            or not metrics.date_window_compliant
        ):
            return QualityResult(
                level="fail",
                metrics=metrics,
                message="브리핑 품질 계약 위반 — 출력을 중단합니다.",
            )
        return QualityResult(
            level="warn",
            metrics=metrics,
            message="브리핑 품질 경고 — 출력은 하되 추이를 모니터링합니다.",
        )

    # Empty fallback is not a renderer failure, but it is worth warning about.
    if metrics.empty_fallback_detected or metrics.placeholder_item_rate > 0.30:
        return QualityResult(
            level="warn",
            metrics=metrics,
            message="브리핑에 실제 항목이 거의 없습니다.",
        )

    return QualityResult(level="pass", metrics=metrics, message="브리핑 품질 계약 통과.")


def format_quality_log(result: QualityResult) -> str:
    """Return a one-line stderr-friendly log entry."""
    return f"[briefing-quality] level={result.level} {result.metrics.to_json()}"
