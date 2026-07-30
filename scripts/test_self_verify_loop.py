#!/usr/bin/env python3
"""Tests for scripts/self_verify_loop.py."""

import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import self_verify_loop as loop  # noqa: E402


def test_steps_for_cycle_and_guard_schedule():
    assert loop.steps_for_cycle(1) == [
        "codex-status-strict",
        "readiness",
        "quality",
        "recent-events",
        "guard",
    ]
    assert loop.steps_for_cycle(2) == [
        "codex-status-strict",
        "readiness",
        "quality",
        "recent-events",
    ]
    assert loop.expected_guard_cycles(12) == {1, 6, 12}


def test_shared_state_vocabulary_values():
    assert loop.SUMMARY_EMPTY == "empty"
    assert loop.SUMMARY_MALFORMED == "malformed"
    assert loop.SUMMARY_PRESENT == "present"
    assert loop.CYCLE_ROWS_EMPTY == "empty_rows"
    assert loop.CYCLE_ROWS_MALFORMED == "malformed_rows"
    assert loop.CYCLE_ROWS_MIXED == "mixed_cycle_rows"
    assert loop.CYCLE_ROWS_DUPLICATE == "duplicate_step_rows"
    assert loop.CYCLE_ROWS_PARTIAL == "partial_cycle_rows"
    assert loop.CYCLE_ROWS_VALID == "valid"


def test_next_stage_advances_only_on_pass():
    assert loop.next_stage("bootstrap", True) == "soak-2h"
    assert loop.next_stage("soak-2h", True) == "day"
    assert loop.next_stage("day", True) == "release-candidate"
    assert loop.next_stage("bootstrap", False) == "bootstrap"
    assert loop.next_stage("soak-2h", False) == "soak-2h"
    assert loop.next_stage("day", False) == "day"


def test_stage_cursor_defaults_and_round_trips():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"

        assert loop.stage_cursor_path(summary) == Path(tmp) / "stage.txt"
        assert loop.read_stage_cursor(summary) == "bootstrap"

        loop.write_stage_cursor(summary, "soak-2h")

        assert loop.read_stage_cursor(summary) == "soak-2h"


def test_stage_cursor_rejects_invalid_values():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        loop.stage_cursor_path(summary).write_text("maybe\n", encoding="utf-8")

        error = _value_error_from(lambda: loop.read_stage_cursor(summary))

    assert "invalid stage cursor: maybe" in str(error)
    assert "invalid stage cursor target: maybe" in str(
        _value_error_from(lambda: loop.write_stage_cursor(summary, "maybe"))
    )


def test_stage_cursor_write_uses_atomic_replace_without_temp_leftover():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"

        loop.write_stage_cursor(summary, "soak-2h")

        assert loop.read_stage_cursor(summary) == "soak-2h"
        leftovers = list(Path(tmp).glob(".stage.txt.*.tmp"))
        assert leftovers == [], leftovers


def test_stage_cursor_write_fsyncs_parent_directory_after_replace():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        calls = []
        old_fsync_parent_dir = loop.fsync_parent_dir

        def record(path):
            calls.append(Path(path))

        loop.fsync_parent_dir = record
        try:
            loop.write_stage_cursor(summary, "soak-2h")
        finally:
            loop.fsync_parent_dir = old_fsync_parent_dir

        assert calls == [loop.stage_cursor_path(summary)]


def test_stage_cursor_write_preserves_original_on_replace_failure():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        cursor = loop.stage_cursor_path(summary)
        cursor.write_text("day\n", encoding="utf-8")
        old_replace = loop.os.replace

        def boom(_src, _dst):
            raise OSError("replace failed")

        loop.os.replace = boom
        try:
            error = _os_error_from(lambda: loop.write_stage_cursor(summary, "soak-2h"))
        finally:
            loop.os.replace = old_replace

        assert "replace failed" in str(error)
        assert cursor.read_text(encoding="utf-8") == "day\n"
        leftovers = list(Path(tmp).glob(".stage.txt.*.tmp"))
        assert leftovers == [], leftovers


def test_row_contract_issues_reject_invalid_values():
    row = _row(1, "codex-status-strict")
    row["status"] = "maybe"
    row["exit_code"] = "-1"
    row["duration_s"] = "nope"
    row["started_at"] = "not-a-time"

    issues = loop.row_contract_issues([row])

    assert "row 2 invalid_status maybe" in issues
    assert "row 2 invalid_exit_code -1" in issues
    assert "row 2 invalid_duration_s nope" in issues
    assert "row 2 invalid_started_at not-a-time" in issues


def test_row_order_issues_reject_wrong_step_order():
    rows = [_row(1, step) for step in loop.steps_for_cycle(1)]
    rows[0], rows[1] = rows[1], rows[0]

    issues = loop.row_order_issues(rows)

    assert any(issue.startswith("cycle 1 step_order expected") for issue in issues)


def test_row_order_issues_reject_missing_cycle_gap():
    rows = [_row(1, step) for step in loop.steps_for_cycle(1)]
    rows.extend(_row(3, step) for step in loop.steps_for_cycle(3))

    issues = loop.row_order_issues(rows)

    assert "cycle_gap missing 2" in issues


def test_summary_rows_state_reports_empty_malformed_and_present():
    assert loop.summary_rows_state([]) == "empty"
    malformed = dict(_row(1, "codex-status-strict"))
    malformed["exit_code"] = "-1"
    assert loop.summary_rows_state([malformed]) == "malformed"
    rows = [_row(1, step) for step in loop.steps_for_cycle(1)]
    assert loop.summary_rows_state(rows) == "present"


def test_summary_rows_contract_returns_state_and_detail_issues():
    assert loop.summary_rows_contract([]) == ("empty", [])
    malformed = dict(_row(1, "codex-status-strict"))
    malformed["exit_code"] = "-1"

    state, issues = loop.summary_rows_contract([malformed])

    assert state == "malformed"
    assert "row 2 invalid_exit_code -1" in issues
    rows = [_row(1, step) for step in loop.steps_for_cycle(1)]
    assert loop.summary_rows_contract(rows) == ("present", [])


def test_cycle_rows_state_rejects_incomplete_or_mixed_batches():
    assert loop.cycle_rows_state([]) == ("empty_rows", "")
    malformed = dict(_row(2, "codex-status-strict"))
    malformed["cycle"] = "x"
    assert loop.cycle_rows_state([malformed]) == ("malformed_rows", "")
    assert loop.cycle_rows_state([_row(2, "codex-status-strict"), _row(3, "readiness")]) == (
        "mixed_cycle_rows",
        "",
    )
    assert loop.cycle_rows_state([
        _row(2, "codex-status-strict"),
        _row(2, "codex-status-strict"),
    ]) == ("duplicate_step_rows", "")
    assert loop.cycle_rows_state([_row(2, "readiness")]) == ("partial_cycle_rows", "")
    rows = [_row(2, step) for step in loop.steps_for_cycle(2)]
    assert loop.cycle_rows_state(rows) == ("valid", "2")


def test_read_summary_rows_enforces_header_and_extra_columns():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good = root / "good.tsv"
        _write_summary(good, [_row(1, "codex-status-strict")])

        rows = loop.read_summary_rows(good)

        assert rows[0]["step"] == "codex-status-strict"

        bad_header = root / "bad-header.tsv"
        bad_header.write_text("cycle\tstep\n1\treadiness\n", encoding="utf-8")
        assert "header expected=" in str(_value_error_from(lambda: loop.read_summary_rows(bad_header)))

        extra = root / "extra.tsv"
        extra.write_text(
            "\t".join(loop.FIELDNAMES) + "\n"
            "1\tcodex-status-strict\tok\t0\t2026-06-30T00:00:00+00:00\t2026-06-30T00:00:01+00:00\t1\textra\n",
            encoding="utf-8",
        )
        assert "row 2 has 1 extra column(s)" in str(
            _value_error_from(lambda: loop.read_summary_rows(extra))
        )


def test_newest_summary_breaks_mtime_ties_by_run_id():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        older = root / "20260703T010203123456Z-a" / "summary.tsv"
        newer = root / "20260703T010203123456Z-b" / "summary.tsv"
        older.parent.mkdir()
        newer.parent.mkdir()
        _write_summary(older, [_row(1, "codex-status-strict")])
        _write_summary(newer, [_row(1, "codex-status-strict")])
        same_mtime = 100
        older.touch()
        newer.touch()

        os.utime(older, (same_mtime, same_mtime))
        os.utime(newer, (same_mtime, same_mtime))

        assert loop.newest_summary(root) == newer


def _row(cycle_num, step):
    return {
        "cycle": str(cycle_num),
        "step": step,
        "status": "ok",
        "exit_code": "0",
        "started_at": "2026-06-30T00:00:00+00:00",
        "ended_at": "2026-06-30T00:00:01+00:00",
        "duration_s": "1",
    }


def _write_summary(path, rows):
    lines = ["\t".join(loop.FIELDNAMES)]
    for row in rows:
        lines.append("\t".join(row[field] for field in loop.FIELDNAMES))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _value_error_from(fn):
    try:
        fn()
    except ValueError as e:
        return e
    raise AssertionError("expected ValueError")


def _os_error_from(fn):
    try:
        fn()
    except OSError as e:
        return e
    raise AssertionError("expected OSError")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - self verify loop")
