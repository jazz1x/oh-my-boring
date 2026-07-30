"""Shared self-verification loop contract."""

import csv
import datetime as dt
import os
import tempfile
from pathlib import Path


DEFAULT_ROOT = Path("/private/tmp/omb-self-verify")
DEFAULT_STAGE = "bootstrap"
TERMINAL_STAGE = "release-candidate"
STAGE_CURSOR_NAME = "stage.txt"
REQUIRED_EVERY_CYCLE = ("codex-status-strict", "readiness", "quality", "recent-events")
GUARD_STEP = "guard"
EVENT_EMITTING_STEPS = ("codex-status-strict", "readiness", GUARD_STEP)
EXPECTED_STEP_EVENTS = {
    "codex-status-strict": ("codex-collector", "collector_status"),
    "readiness": ("doctor", "readiness"),
    GUARD_STEP: ("guard", "structural_guard"),
}
FIELDNAMES = ("cycle", "step", "status", "exit_code", "started_at", "ended_at", "duration_s")
VALID_STEPS = {*REQUIRED_EVERY_CYCLE, GUARD_STEP}
VALID_STATUSES = {"ok", "failed"}
SUMMARY_EMPTY = "empty"
SUMMARY_MALFORMED = "malformed"
SUMMARY_PRESENT = "present"
CYCLE_ROWS_EMPTY = "empty_rows"
CYCLE_ROWS_MALFORMED = "malformed_rows"
CYCLE_ROWS_MIXED = "mixed_cycle_rows"
CYCLE_ROWS_DUPLICATE = "duplicate_step_rows"
CYCLE_ROWS_PARTIAL = "partial_cycle_rows"
CYCLE_ROWS_VALID = "valid"

STAGES = {
    "bootstrap": {"min_cycles": 1, "min_guard_runs": 1, "next": "soak-2h"},
    "soak-2h": {"min_cycles": 6, "min_guard_runs": 2, "next": "day"},
    "day": {"min_cycles": 72, "min_guard_runs": 13, "next": "release-candidate"},
}
VALID_CURSOR_STAGES = {*STAGES, TERMINAL_STAGE}


def steps_for_cycle(cycle):
    steps = list(REQUIRED_EVERY_CYCLE)
    if cycle in expected_guard_cycles(cycle):
        steps.append(GUARD_STEP)
    return steps


def expected_guard_cycles(max_cycle):
    return {1, *range(6, max_cycle + 1, 6)}


def next_stage(stage, passed):
    return STAGES[stage]["next"] if passed else stage


def stage_cursor_path(summary_path):
    return Path(summary_path).with_name(STAGE_CURSOR_NAME)


def read_stage_cursor(summary_path):
    cursor = stage_cursor_path(summary_path)
    if not cursor.exists():
        return DEFAULT_STAGE
    stage = cursor.read_text(encoding="utf-8").strip()
    if stage not in VALID_CURSOR_STAGES:
        display = stage or "(empty)"
        raise ValueError(f"invalid stage cursor: {display}")
    return stage


def _write_text_atomic(path, text):
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        fsync_parent_dir(path)
    except OSError:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        raise


def fsync_parent_dir(path):
    fd = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_stage_cursor(summary_path, stage):
    if stage not in VALID_CURSOR_STAGES:
        display = stage or "(empty)"
        raise ValueError(f"invalid stage cursor target: {display}")
    _write_text_atomic(stage_cursor_path(summary_path), f"{stage}\n")


def newest_summary(root=DEFAULT_ROOT):
    candidates = [p for p in root.glob("*/summary.tsv") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime, path.parent.name))


def read_summary_rows(path):
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != list(FIELDNAMES):
            raise ValueError(
                f"header expected={','.join(FIELDNAMES)} got={','.join(reader.fieldnames or [])}"
            )
        rows = []
        for index, row in enumerate(reader, start=2):
            extra = row.get(None)
            if extra:
                raise ValueError(f"row {index} has {len(extra)} extra column(s)")
            rows.append(row)
        return rows


def row_contract_issues(rows):
    issues = []
    for index, row in enumerate(rows, start=2):
        missing = [field for field in FIELDNAMES if not row.get(field)]
        if missing:
            issues.append(f"row {index} missing {','.join(missing)}")
            continue
        if not _is_positive_int(row["cycle"]):
            issues.append(f"row {index} invalid_cycle {row['cycle']}")
        if row["step"] not in VALID_STEPS:
            issues.append(f"row {index} unknown_step {row['step']}")
        if row["status"] not in VALID_STATUSES:
            issues.append(f"row {index} invalid_status {row['status']}")
        if not _is_non_negative_int(row["exit_code"]):
            issues.append(f"row {index} invalid_exit_code {row['exit_code']}")
        if not _is_non_negative_int(row["duration_s"]):
            issues.append(f"row {index} invalid_duration_s {row['duration_s']}")
        started_at = _parse_iso_datetime(row["started_at"])
        ended_at = _parse_iso_datetime(row["ended_at"])
        if started_at is None:
            issues.append(f"row {index} invalid_started_at {row['started_at']}")
        if ended_at is None:
            issues.append(f"row {index} invalid_ended_at {row['ended_at']}")
        if started_at is not None and ended_at is not None and ended_at < started_at:
            issues.append(f"row {index} ended_before_started")
    return issues


def row_order_issues(rows):
    issues = []
    previous_cycle = 0
    steps_by_cycle = {}
    seen_cycles = set()
    for row in rows:
        if not row.get("cycle", "").isdigit():
            continue
        cycle = int(row["cycle"])
        seen_cycles.add(cycle)
        if cycle < previous_cycle:
            issues.append(f"cycle_order_regressed {previous_cycle}>{cycle}")
        previous_cycle = cycle
        if row.get("step") in VALID_STEPS:
            steps_by_cycle.setdefault(cycle, []).append(row["step"])
    if seen_cycles:
        missing = sorted(set(range(1, max(seen_cycles) + 1)) - seen_cycles)
        if missing:
            issues.append(f"cycle_gap missing {','.join(str(cycle) for cycle in missing)}")
    for cycle, actual_steps in sorted(steps_by_cycle.items()):
        expected_steps = steps_for_cycle(cycle)
        if actual_steps != expected_steps:
            issues.append(
                f"cycle {cycle} step_order expected {','.join(expected_steps)} "
                f"got {','.join(actual_steps)}"
            )
    return issues


def summary_rows_state(rows):
    state, _ = summary_rows_contract(rows)
    return state


def summary_rows_contract(rows):
    if not rows:
        return SUMMARY_EMPTY, []
    issues = row_contract_issues(rows)
    issues.extend(row_order_issues(rows))
    if issues:
        return SUMMARY_MALFORMED, issues
    return SUMMARY_PRESENT, []


def cycle_rows_state(rows):
    if not rows:
        return CYCLE_ROWS_EMPTY, ""
    if row_contract_issues(rows):
        return CYCLE_ROWS_MALFORMED, ""
    cycles = {row["cycle"] for row in rows}
    if len(cycles) != 1:
        return CYCLE_ROWS_MIXED, ""
    cycle = rows[0]["cycle"]
    expected_steps = steps_for_cycle(int(cycle))
    actual_steps = [row["step"] for row in rows]
    if len(actual_steps) != len(set(actual_steps)):
        return CYCLE_ROWS_DUPLICATE, ""
    if actual_steps != expected_steps:
        return CYCLE_ROWS_PARTIAL, ""
    return CYCLE_ROWS_VALID, cycle


def _is_positive_int(value):
    return value.isdigit() and int(value) >= 1


def _is_non_negative_int(value):
    return value.isdigit()


def _parse_iso_datetime(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed
