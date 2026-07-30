#!/usr/bin/env python3
"""Tests for scripts/self-verify-contract.py."""

from contextlib import redirect_stdout
import io
import json
import os
import importlib.util
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "self_verify_contract", str(ROOT / "scripts" / "self-verify-contract.py")
)
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


def test_bootstrap_passes_with_one_green_cycle_and_guard():
    rows = _rows(cycles=1, guard_cycles={1})
    result = contract.evaluate(rows, "bootstrap")
    assert result["status"] == "pass"
    assert result["next"] == "soak-2h"


def test_empty_summary_reports_named_issue():
    result = contract.evaluate([], "bootstrap")

    assert result["status"] == "failed"
    assert result["next"] == "bootstrap"
    assert "empty_summary" in result["issues"]


def test_failed_row_blocks_stage_transition():
    rows = _rows(cycles=1, guard_cycles={1})
    rows[1]["status"] = "failed"
    rows[1]["exit_code"] = "2"
    result = contract.evaluate(rows, "bootstrap")
    assert result["status"] == "failed"
    assert result["next"] == "bootstrap"
    assert result["failed_rows"]
    assert "failed_steps 1:readiness:2" in result["issues"]


def test_failed_stage_keeps_current_stage_as_next():
    cases = (
        ("bootstrap", 1, {1}),
        ("soak-2h", 6, {1, 6}),
        ("day", 72, {1, *range(6, 73, 6)}),
    )
    for stage, cycles, guard_cycles in cases:
        rows = _rows(cycles=cycles, guard_cycles=guard_cycles)
        rows[0]["status"] = "failed"
        rows[0]["exit_code"] = "1"

        result = contract.evaluate(rows, stage)

        assert result["status"] == "failed"
        assert result["next"] == stage


def test_main_prints_failed_step_identity():
    rows = _rows(cycles=1, guard_cycles={1})
    rows[1]["status"] = "failed"
    rows[1]["exit_code"] = "2"
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "failed_rows=1" in text
    assert "issue=failed_steps 1:readiness:2" in text
    assert f"evidence=failed_step_log {Path(tmp) / 'logs' / 'cycle-0001-readiness.log'}" in text


def test_main_blocks_stage_transition_when_step_logs_are_missing():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert "issue=missing_step_logs 5 first=" in text


def test_main_blocks_stage_transition_when_step_logs_are_empty():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows, body="")
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert "issue=empty_step_logs 5 first=" in text


def test_main_blocks_stage_transition_when_step_log_header_is_malformed():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _step_log(summary, "readiness").write_text("not a self verify header\n", encoding="utf-8")
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=malformed_step_logs 1 first={Path(tmp) / 'logs' / 'cycle-0001-readiness.log'}" in text


def test_main_blocks_stage_transition_when_step_log_footer_is_missing():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        readiness = next(row for row in rows if row["step"] == "readiness")
        _step_log(summary, "readiness").write_text(
            _step_log_header_line(summary, readiness),
            encoding="utf-8",
        )
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=incomplete_step_logs 1 first={Path(tmp) / 'logs' / 'cycle-0001-readiness.log'}" in text


def test_main_blocks_stage_transition_when_step_log_footer_mismatches_row():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        readiness = next(row for row in rows if row["step"] == "readiness")
        _step_log(summary, "readiness").write_text(
            _step_log_header_line(summary, readiness)
            + "self_verify_step_complete cycle=0001 step=readiness exit_code=7 "
            "ended_at=2026-06-30T00:00:01+09:00\n",
            encoding="utf-8",
        )
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=incomplete_step_logs 1 first={Path(tmp) / 'logs' / 'cycle-0001-readiness.log'}" in text


def test_main_blocks_stage_transition_when_step_log_header_lacks_execution_metadata():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _step_log(summary, "readiness").write_text(
            "self_verify_step cycle=0001 step=readiness "
            f"event_log={summary.with_name('events.ndjson')} "
            "started_at=2026-06-30T00:00:00+09:00\n",
            encoding="utf-8",
        )
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=malformed_step_logs 1 first={Path(tmp) / 'logs' / 'cycle-0001-readiness.log'}" in text


def test_main_blocks_stage_transition_when_step_log_header_has_empty_execution_metadata():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _step_log(summary, "readiness").write_text(
            "self_verify_step cycle=0001 step=readiness "
            f"make_bin= cwd= event_log={summary.with_name('events.ndjson')} "
            "started_at=2026-06-30T00:00:00+09:00\n",
            encoding="utf-8",
        )
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=malformed_step_logs 1 first={Path(tmp) / 'logs' / 'cycle-0001-readiness.log'}" in text


def test_main_blocks_stage_transition_when_step_log_header_mismatches_summary():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _step_log(summary, "readiness").write_text(
            "self_verify_step cycle=0001 step=quality "
            f"make_bin=make cwd={ROOT} event_log={summary.with_name('events.ndjson')} "
            "started_at=2026-06-30T00:00:00+09:00\n",
            encoding="utf-8",
        )
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=mismatched_step_logs 1 first={Path(tmp) / 'logs' / 'cycle-0001-readiness.log'}" in text


def test_main_blocks_stage_transition_when_step_log_header_cwd_mismatches_runner_root():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _step_log(summary, "readiness").write_text(
            "self_verify_step cycle=0001 step=readiness "
            f"make_bin=make cwd={Path(tmp) / 'elsewhere'} event_log={summary.with_name('events.ndjson')} "
            "started_at=2026-06-30T00:00:00+09:00\n",
            encoding="utf-8",
        )
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=mismatched_step_logs 1 first={Path(tmp) / 'logs' / 'cycle-0001-readiness.log'}" in text


def test_main_blocks_stage_transition_when_step_log_header_time_is_outside_summary_row():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _step_log(summary, "readiness").write_text(
            "self_verify_step cycle=0001 step=readiness "
            f"make_bin=make cwd={ROOT} event_log={summary.with_name('events.ndjson')} "
            "started_at=2026-06-30T00:00:02+09:00\n",
            encoding="utf-8",
        )
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=mismatched_step_logs 1 first={Path(tmp) / 'logs' / 'cycle-0001-readiness.log'}" in text


def test_main_blocks_stage_transition_when_event_log_is_missing():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=missing_event_log {Path(tmp) / 'events.ndjson'}" in text


def test_main_blocks_stage_transition_when_event_log_is_empty():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary, body="")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=empty_event_log {Path(tmp) / 'events.ndjson'}" in text


def test_main_blocks_stage_transition_when_event_log_has_no_records():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary, body="\n")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=empty_event_records {Path(tmp) / 'events.ndjson'}" in text


def test_main_blocks_stage_transition_when_event_log_is_malformed():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary, body="not-json\n")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=malformed_event_log {Path(tmp) / 'events.ndjson'}: line 1:" in text


def test_main_blocks_stage_transition_when_event_log_record_is_not_object():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary, body="[]\n")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert f"issue=malformed_event_log {Path(tmp) / 'events.ndjson'}: line 1: expected object" in text


def test_main_blocks_stage_transition_when_event_log_lacks_self_verify_provenance():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary, body='{"component":"self-verify","event":"test","status":"ok"}\n')
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert "issue=missing_event_provenance 1 first=line 1 missing=" in text


def test_main_blocks_stage_transition_when_event_log_points_to_unknown_step():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary, step="not-a-step")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert "issue=mismatched_event_provenance 1 first=line 1 cycle=1 step=not-a-step" in text


def test_main_blocks_stage_transition_when_event_time_is_outside_step_window():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary, cycle=1, step="readiness", ts="2026-06-30T00:00:02+09:00")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert "issue=mismatched_event_provenance 1 first=line 1 cycle=1 step=readiness ts_out_of_range=" in text


def test_main_blocks_stage_transition_when_event_emitting_step_has_no_record():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary, step="readiness")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert "issue=missing_step_events 2 first=cycle=1 step=codex-status-strict" in text


def test_main_blocks_stage_transition_when_event_shape_mismatches_step_contract():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        records = _event_records_for_summary(summary)
        for record in records:
            if record["self_verify_step"] == "readiness":
                record["component"] = "self-verify"
                record["event"] = "test"
        _write_event_records(summary, records)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert "issue=missing_step_events 1 first=cycle=1 step=readiness" in text


def test_main_blocks_stage_transition_when_event_status_mismatches_summary_row():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        records = _event_records_for_summary(summary)
        for record in records:
            if record["self_verify_step"] == "readiness":
                record["status"] = "failed"
        _write_event_records(summary, records)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])

    text = stdout.getvalue()
    assert rc == 1
    assert "status=failed" in text
    assert "next=bootstrap" in text
    assert "issue=missing_step_events 1 first=cycle=1 step=readiness" in text


def test_main_uses_stage_cursor_and_advances_it():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary)])
        cursor_text = (Path(tmp) / "stage.txt").read_text(encoding="utf-8")

    text = stdout.getvalue()
    assert rc == 0
    assert "stage=bootstrap" in text
    assert "next=soak-2h" in text
    assert cursor_text == "soak-2h\n"


def test_main_failure_keeps_stage_cursor_on_current_stage():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary)
        (Path(tmp) / "stage.txt").write_text("soak-2h\n", encoding="utf-8")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary)])
        cursor_text = (Path(tmp) / "stage.txt").read_text(encoding="utf-8")

    text = stdout.getvalue()
    assert rc == 1
    assert "stage=soak-2h" in text
    assert "next=soak-2h" in text
    assert cursor_text == "soak-2h\n"


def test_main_rejects_invalid_stage_cursor_without_traceback():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        (Path(tmp) / "stage.txt").write_text("maybe\n", encoding="utf-8")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary)])

    text = stdout.getvalue()
    assert rc == 1
    assert "reason=invalid_stage_cursor" in text
    assert "invalid stage cursor: maybe" in text


def test_terminal_stage_cursor_validates_existing_summary():
    rows = _rows(cycles=72, guard_cycles={1, *range(6, 73, 6)})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary)
        (Path(tmp) / "stage.txt").write_text("release-candidate\n", encoding="utf-8")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary)])

    text = stdout.getvalue()
    assert rc == 0
    assert "stage=release-candidate" in text
    assert "status=pass" in text
    assert "next=release-candidate" in text


def test_terminal_stage_cursor_still_requires_day_threshold():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary)
        (Path(tmp) / "stage.txt").write_text("release-candidate\n", encoding="utf-8")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary)])

    text = stdout.getvalue()
    assert rc == 1
    assert "stage=release-candidate" in text
    assert "status=failed" in text
    assert "next=release-candidate" in text
    assert "issue=cycles 1 < required 72" in text
    assert "issue=guard_runs 1 < required 13" in text


def test_stage_override_accepts_terminal_stage_without_cursor():
    rows = _rows(cycles=72, guard_cycles={1, *range(6, 73, 6)})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary)
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "release-candidate"])
        cursor_exists = (Path(tmp) / "stage.txt").exists()

    text = stdout.getvalue()
    assert rc == 0
    assert "stage=release-candidate" in text
    assert "next=release-candidate" in text
    assert not cursor_exists


def test_stage_override_does_not_mutate_existing_cursor():
    rows = _rows(cycles=1, guard_cycles={1})
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        cursor = Path(tmp) / "stage.txt"
        _write_summary(summary, rows)
        _write_step_logs(summary, rows)
        _write_event_log(summary)
        cursor.write_text("day\n", encoding="utf-8")
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(summary), "--stage", "bootstrap"])
        cursor_text = cursor.read_text(encoding="utf-8")

    text = stdout.getvalue()
    assert rc == 0
    assert "stage=bootstrap" in text
    assert "next=soak-2h" in text
    assert cursor_text == "day\n"


def test_duplicate_step_rows_block_stage_transition():
    rows = _rows(cycles=1, guard_cycles={1})
    rows.append(dict(rows[0]))
    result = contract.evaluate(rows, "bootstrap")
    assert result["status"] == "failed"
    assert "duplicate_steps 1:codex-status-strict" in result["issues"]
    assert "cycle 1 duplicate_step_rows" in result["issues"]


def test_partial_cycle_uses_shared_cycle_batch_contract():
    rows = _rows(cycles=1, guard_cycles={1})
    rows = [row for row in rows if row["step"] != "quality"]

    result = contract.evaluate(rows, "bootstrap")

    assert result["status"] == "failed"
    assert "cycle 1 partial_cycle_rows" in result["issues"]


def test_malformed_or_unknown_rows_block_stage_transition():
    rows = _rows(cycles=1, guard_cycles={1})
    rows.append(
        {
            "cycle": "x",
            "step": "readiness-typo",
            "status": "maybe",
            "exit_code": "nope",
            "started_at": "2026-06-30T00:00:00+0900",
            "ended_at": "2026-06-30T00:00:01+0900",
            "duration_s": "-1",
        }
    )

    result = contract.evaluate(rows, "bootstrap")

    assert result["status"] == "failed"
    assert "malformed_summary" in result["issues"]
    assert "row 7 invalid_cycle x" in result["issues"]
    assert "row 7 unknown_step readiness-typo" in result["issues"]
    assert "row 7 invalid_status maybe" in result["issues"]
    assert "row 7 invalid_exit_code nope" in result["issues"]
    assert "row 7 invalid_duration_s -1" in result["issues"]


def test_missing_summary_columns_block_stage_transition():
    rows = _rows(cycles=1, guard_cycles={1})
    broken = dict(rows[0])
    broken.pop("started_at")
    rows.append(broken)

    result = contract.evaluate(rows, "bootstrap")

    assert result["status"] == "failed"
    assert "row 7 missing started_at" in result["issues"]


def test_invalid_or_reversed_timestamps_block_stage_transition():
    rows = _rows(cycles=1, guard_cycles={1})
    rows[0]["started_at"] = "not-a-time"
    rows[1]["ended_at"] = "also-not-a-time"
    rows[2]["started_at"] = "2026-06-30T00:00:02+09:00"
    rows[2]["ended_at"] = "2026-06-30T00:00:01+09:00"
    rows[3]["started_at"] = "2026-06-30T00:00:00"

    result = contract.evaluate(rows, "bootstrap")

    assert result["status"] == "failed"
    assert "row 2 invalid_started_at not-a-time" in result["issues"]
    assert "row 3 invalid_ended_at also-not-a-time" in result["issues"]
    assert "row 4 ended_before_started" in result["issues"]
    assert "row 5 invalid_started_at 2026-06-30T00:00:00" in result["issues"]


def test_step_order_blocks_stage_transition():
    rows = _rows(cycles=1, guard_cycles={1})
    rows[0], rows[1] = rows[1], rows[0]

    result = contract.evaluate(rows, "bootstrap")

    assert result["status"] == "failed"
    assert "malformed_summary" in result["issues"]
    assert any(issue.startswith("cycle 1 step_order expected") for issue in result["issues"])


def test_cycle_order_regression_blocks_stage_transition():
    rows = _rows(cycles=2, guard_cycles={1})
    rows.insert(0, rows.pop(5))

    result = contract.evaluate(rows, "bootstrap")

    assert result["status"] == "failed"
    assert "cycle_order_regressed 2>1" in result["issues"]


def test_soak_requires_six_cycles_and_two_guard_runs():
    rows = _rows(cycles=6, guard_cycles={1})
    result = contract.evaluate(rows, "soak-2h")
    assert result["status"] == "failed"
    assert "guard_runs 1 < required 2" in result["issues"]
    assert "missing_guard_cycles 6" in result["issues"]

    rows = _rows(cycles=6, guard_cycles={1, 6})
    result = contract.evaluate(rows, "soak-2h")
    assert result["status"] == "pass"
    assert result["next"] == "day"


def test_soak_rejects_non_contiguous_cycles_and_wrong_guard_positions():
    rows = [row for row in _rows(cycles=7, guard_cycles={2, 7}) if row["cycle"] != "1"]
    result = contract.evaluate(rows, "soak-2h")
    assert result["status"] == "failed"
    assert "missing_cycles 1" in result["issues"]
    assert "missing_guard_cycles 1,6" in result["issues"]


def test_day_requires_seventy_two_cycles_and_thirteen_guard_runs():
    rows = _rows(cycles=72, guard_cycles={1, *range(6, 73, 6)})
    result = contract.evaluate(rows, "day")
    assert result["status"] == "pass"
    assert result["guard_runs"] == 13
    assert result["next"] == "release-candidate"


def test_newest_summary_picks_latest_summary_file():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = root / "old"
        new = root / "new"
        old.mkdir()
        new.mkdir()
        old_summary = old / "summary.tsv"
        new_summary = new / "summary.tsv"
        old_summary.write_text("cycle\tstep\tstatus\texit_code\n", encoding="utf-8")
        new_summary.write_text("cycle\tstep\tstatus\texit_code\n", encoding="utf-8")
        os.utime(old_summary, (1, 1))
        os.utime(new_summary, (2, 2))
        assert contract.newest_summary(root) == new_summary


def test_read_rows_rejects_non_contract_header():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text(
            "cycle\tstep\tstatus\texit_code\n"
            "1\tcodex-status-strict\tok\t0\n",
            encoding="utf-8",
        )

        error = _value_error_from(lambda: contract.read_rows(summary))

    assert "header expected=" in str(error)


def test_read_rows_rejects_extra_row_columns():
    with tempfile.TemporaryDirectory() as tmp:
        summary = Path(tmp) / "summary.tsv"
        summary.write_text(
            "\t".join(contract.FIELDNAMES) + "\n"
            "1\tcodex-status-strict\tok\t0\t2026-06-30T00:00:00+09:00\t2026-06-30T00:00:01+09:00\t1\textra\n",
            encoding="utf-8",
        )

        error = _value_error_from(lambda: contract.read_rows(summary))

    assert "row 2 has 1 extra column(s)" in str(error)


def test_step_log_header_rejects_malformed_field_token():
    error = _value_error_from(
        lambda: contract.parse_step_log_header(
            "self_verify_step cycle=0001 step=readiness bare-token "
            f"make_bin=make cwd={ROOT} event_log=/tmp/events.ndjson "
            "started_at=2026-06-30T00:00:00+09:00"
        )
    )

    assert "malformed field bare-token" in str(error)


def test_step_log_footer_rejects_malformed_field_token():
    error = _value_error_from(
        lambda: contract.parse_step_log_footer(
            "self_verify_step_complete cycle=0001 step=readiness bare-token "
            "exit_code=0 ended_at=2026-06-30T00:00:01+09:00"
        )
    )

    assert "malformed field bare-token" in str(error)


def test_step_log_header_rejects_duplicate_field_key():
    error = _value_error_from(
        lambda: contract.parse_step_log_header(
            "self_verify_step cycle=0001 cycle=0002 step=readiness "
            f"make_bin=make cwd={ROOT} event_log=/tmp/events.ndjson "
            "started_at=2026-06-30T00:00:00+09:00"
        )
    )

    assert "duplicate field cycle" in str(error)


def test_step_log_footer_rejects_duplicate_field_key():
    error = _value_error_from(
        lambda: contract.parse_step_log_footer(
            "self_verify_step_complete cycle=0001 step=readiness step=quality "
            "exit_code=0 ended_at=2026-06-30T00:00:01+09:00"
        )
    )

    assert "duplicate field step" in str(error)


def test_main_rejects_missing_explicit_summary_without_traceback():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "missing.tsv"
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            rc = contract.main(["--summary", str(missing)])

    text = stdout.getvalue()
    assert rc == 1
    assert "reason=no_summary_found" in text
    assert str(missing) in text


def _rows(cycles, guard_cycles):
    rows = []
    for cycle in range(1, cycles + 1):
        for step in contract.REQUIRED_EVERY_CYCLE:
            rows.append(_row(cycle, step))
        if cycle in guard_cycles:
            rows.append(_row(cycle, contract.GUARD_STEP))
    return rows


def _row(cycle, step):
    return {
        "cycle": str(cycle),
        "step": step,
        "status": "ok",
        "exit_code": "0",
        "started_at": "2026-06-30T00:00:00+09:00",
        "ended_at": "2026-06-30T00:00:01+09:00",
        "duration_s": "1",
    }


def _value_error_from(fn):
    try:
        fn()
    except ValueError as e:
        return e
    raise AssertionError("expected ValueError")


def _write_summary(path, rows):
    lines = ["\t".join(contract.FIELDNAMES)]
    for row in rows:
        lines.append("\t".join(row[name] for name in contract.FIELDNAMES))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_step_logs(summary, rows, body=None):
    log_dir = summary.with_name("logs")
    log_dir.mkdir()
    for row in rows:
        path = log_dir / f"cycle-{int(row['cycle']):04d}-{row['step']}.log"
        text = body if body is not None else _step_log_header(summary, row)
        path.write_text(text, encoding="utf-8")


def _step_log(summary, step, cycle=1):
    return summary.with_name("logs") / f"cycle-{cycle:04d}-{step}.log"


def _step_log_header(summary, row):
    return _step_log_header_line(summary, row) + _step_log_footer_line(row)


def _step_log_header_line(summary, row):
    return (
        f"self_verify_step cycle={int(row['cycle']):04d} step={row['step']} "
        f"make_bin=make cwd={ROOT} event_log={summary.with_name('events.ndjson')} "
        "started_at=2026-06-30T00:00:00+09:00\n"
    )


def _step_log_footer_line(row):
    return (
        f"self_verify_step_complete cycle={int(row['cycle']):04d} step={row['step']} "
        f"exit_code={row['exit_code']} ended_at=2026-06-30T00:00:01+09:00\n"
    )


def _write_event_log(
    summary,
    body=None,
    cycle=None,
    step=None,
    ts="2026-06-30T00:00:00.500+09:00",
    component=None,
    event=None,
    status=None,
):
    if body is None:
        records = (
            [_event_record(summary, cycle, step, ts, component, event, status)]
            if cycle or step
            else _event_records_for_summary(summary)
        )
        body = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    summary.with_name("events.ndjson").write_text(body, encoding="utf-8")


def _write_event_records(summary, records):
    body = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    summary.with_name("events.ndjson").write_text(body, encoding="utf-8")


def _event_records_for_summary(summary):
    return [
        _event_record(
            summary,
            row["cycle"],
            row["step"],
            "2026-06-30T00:00:00.500+09:00",
            status=row["status"],
        )
        for row in contract.read_rows(summary)
        if row["step"] in contract.EVENT_EMITTING_STEPS
    ]


def _event_record(summary, cycle, step, ts, component=None, event=None, status=None):
    expected_component, expected_event = contract.EXPECTED_STEP_EVENTS.get(
        step or "readiness",
        ("self-verify", "test"),
    )
    return {
        "ts": ts,
        "component": component or expected_component,
        "event": event or expected_event,
        "status": status or "ok",
        "self_verify_summary": str(summary),
        "self_verify_event_log": str(summary.with_name("events.ndjson")),
        "self_verify_cycle": str(cycle or 1),
        "self_verify_step": step or "readiness",
    }


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - self verify contract")
