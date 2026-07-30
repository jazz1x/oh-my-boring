#!/usr/bin/env python3
"""Evaluate the self-verification loop summary against stage thresholds."""

import argparse
from collections import Counter
import datetime as dt
import json
from pathlib import Path
import sys

from self_verify_loop import (
    CYCLE_ROWS_VALID,
    DEFAULT_ROOT,
    EVENT_EMITTING_STEPS,
    EXPECTED_STEP_EVENTS,
    FIELDNAMES,
    GUARD_STEP,
    REQUIRED_EVERY_CYCLE,
    STAGES,
    SUMMARY_PRESENT,
    TERMINAL_STAGE,
    VALID_CURSOR_STAGES,
    VALID_STATUSES,
    cycle_rows_state,
    expected_guard_cycles,
    newest_summary,
    next_stage,
    read_stage_cursor,
    read_summary_rows,
    stage_cursor_path,
    summary_rows_contract,
    write_stage_cursor,
)

ROOT = Path(__file__).resolve().parents[1]
VALID_LOG_STEPS = {*REQUIRED_EVERY_CYCLE, GUARD_STEP}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check self-verification stage contract")
    parser.add_argument("--summary", help="summary.tsv path; defaults to newest under /private/tmp/omb-self-verify")
    parser.add_argument("--stage", choices=sorted(VALID_CURSOR_STAGES), default=None)
    parser.add_argument("--no-write-cursor", action="store_true")
    args = parser.parse_args(argv)

    summary = Path(args.summary) if args.summary else newest_summary(DEFAULT_ROOT)
    if summary is None:
        print("self_verify_contract status=failed reason=no_summary_found")
        return 1
    if not summary.is_file():
        print(f"self_verify_contract status=failed reason=no_summary_found summary={summary}")
        return 1

    try:
        rows = read_rows(summary)
    except OSError as e:
        print(f"self_verify_contract status=failed reason=summary_unreadable summary={summary} detail={e}")
        return 1
    except ValueError as e:
        print(f"self_verify_contract status=failed reason=malformed_summary summary={summary} detail={e}")
        return 1
    try:
        stage = args.stage or read_stage_cursor(summary)
    except OSError as e:
        print(
            "self_verify_contract "
            f"status=failed reason=stage_cursor_unreadable summary={summary} "
            f"cursor={stage_cursor_path(summary)} detail={e}"
        )
        return 1
    except ValueError as e:
        print(
            "self_verify_contract "
            f"status=failed reason=invalid_stage_cursor summary={summary} "
            f"cursor={stage_cursor_path(summary)} detail={e}"
        )
        return 1

    result = evaluate_terminal(rows) if stage == TERMINAL_STAGE else evaluate(rows, stage)
    apply_step_log_contract(result, summary, rows, stage)
    apply_event_log_contract(result, summary, stage)
    write_cursor = args.stage is None and not args.no_write_cursor
    if write_cursor:
        try:
            write_stage_cursor(summary, result["next"])
        except (OSError, ValueError) as e:
            print(
                "self_verify_contract "
                f"stage={stage} status=failed reason=stage_cursor_write_failed "
                f"summary={summary} cursor={stage_cursor_path(summary)} detail={e}"
            )
            return 1
    print(
        "self_verify_contract "
        f"stage={stage} status={result['status']} "
        f"summary={summary} cycles={result['cycles']} guard_runs={result['guard_runs']} "
        f"failed_rows={len(result['failed_rows'])} next={result['next']} "
        f"cursor={stage_cursor_path(summary)}"
    )
    for issue in result["issues"]:
        print(f"  issue={issue}")
    for log_path in result["failed_step_logs"]:
        print(f"  evidence=failed_step_log {log_path}")
    return 0 if result["status"] == "pass" else 1


def read_rows(path):
    return read_summary_rows(path)


def evaluate(rows, stage):
    thresholds = STAGES[stage]
    summary_state, issues = summary_rows_contract(rows)
    if summary_state != SUMMARY_PRESENT:
        issues.append(f"{summary_state}_summary")
    cycles = sorted({int(row["cycle"]) for row in rows if row.get("cycle", "").isdigit()})
    cycle_count = len(cycles)
    guard_cycles = sorted(
        {int(row["cycle"]) for row in rows if row.get("step") == GUARD_STEP and row.get("cycle", "").isdigit()}
    )
    guard_runs = len(guard_cycles)
    failed_rows = [row for row in rows if row.get("status") != "ok" or row.get("exit_code") != "0"]
    by_cycle = {}
    rows_by_cycle = {}
    step_counts = Counter()
    for row in rows:
        if row.get("cycle", "").isdigit():
            cycle = int(row["cycle"])
            step = row.get("step", "")
            by_cycle.setdefault(cycle, set()).add(step)
            rows_by_cycle.setdefault(cycle, []).append(row)
            step_counts[(cycle, step)] += 1

    expected_cycles = set(range(1, thresholds["min_cycles"] + 1))
    missing_cycles = sorted(expected_cycles - set(cycles))
    required_guard_cycles = expected_guard_cycles(thresholds["min_cycles"])
    missing_guard_cycles = sorted(required_guard_cycles - set(guard_cycles))

    if cycle_count < thresholds["min_cycles"]:
        issues.append(f"cycles {cycle_count} < required {thresholds['min_cycles']}")
    if missing_cycles:
        issues.append(f"missing_cycles {','.join(str(cycle) for cycle in missing_cycles)}")
    if guard_runs < thresholds["min_guard_runs"]:
        issues.append(f"guard_runs {guard_runs} < required {thresholds['min_guard_runs']}")
    if missing_guard_cycles:
        issues.append(f"missing_guard_cycles {','.join(str(cycle) for cycle in missing_guard_cycles)}")
    if failed_rows:
        issues.append(f"failed_rows {len(failed_rows)} > 0")
        issues.append(
            "failed_steps "
            + ",".join(
                f"{_issue_value(row, 'cycle')}:{_issue_value(row, 'step')}:{_issue_value(row, 'exit_code')}"
                for row in failed_rows
            )
        )
    duplicate_steps = sorted(
        (cycle, step) for (cycle, step), count in step_counts.items() if count > 1
    )
    if duplicate_steps:
        issues.append(
            "duplicate_steps "
            + ",".join(f"{cycle}:{step or '(blank)'}" for cycle, step in duplicate_steps)
        )
    for cycle, cycle_rows in sorted(rows_by_cycle.items()):
        state, _ = cycle_rows_state(cycle_rows)
        if state != CYCLE_ROWS_VALID:
            issues.append(f"cycle {cycle} {state}")

    for cycle in range(1, thresholds["min_cycles"] + 1):
        missing = [step for step in REQUIRED_EVERY_CYCLE if step not in by_cycle.get(cycle, set())]
        if missing:
            issues.append(f"cycle {cycle} missing {','.join(missing)}")

    status = "pass" if not issues else "failed"
    return {
        "status": status,
        "cycles": cycle_count,
        "guard_runs": guard_runs,
        "failed_rows": failed_rows,
        "rows_for_event_provenance": rows,
        "issues": issues,
        "next": next_stage(stage, status == "pass"),
        "failed_step_logs": [],
        "missing_step_logs": [],
        "empty_step_logs": [],
        "malformed_step_logs": [],
        "mismatched_step_logs": [],
        "incomplete_step_logs": [],
        "missing_event_log": None,
        "empty_event_log": None,
        "malformed_event_log": None,
        "empty_event_records": None,
        "missing_event_provenance": [],
        "mismatched_event_provenance": [],
        "missing_step_events": [],
    }


def evaluate_terminal(rows):
    result = evaluate(rows, "day")
    result["next"] = TERMINAL_STAGE
    return result


def apply_step_log_contract(result, summary_path, rows, stage):
    log_paths = [path for path in (step_log_path(summary_path, row) for row in rows) if path is not None]
    missing_logs = [path for path in log_paths if not path.is_file()]
    empty_logs = [path for path in log_paths if path.is_file() and path.stat().st_size == 0]
    malformed_logs = []
    mismatched_logs = []
    incomplete_logs = []
    for row in rows:
        path = step_log_path(summary_path, row)
        if path is None or not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            header = read_step_log_header(path)
        except ValueError:
            malformed_logs.append(path)
            continue
        if step_log_header_mismatch(header, row, event_log_path(summary_path)):
            mismatched_logs.append(path)
            continue
        try:
            footer = read_step_log_footer(path)
        except ValueError:
            incomplete_logs.append(path)
            continue
        if step_log_footer_mismatch(footer, row):
            incomplete_logs.append(path)
    failed_logs = [
        path
        for path in (step_log_path(summary_path, row) for row in result["failed_rows"])
        if path is not None
    ]
    result["missing_step_logs"] = missing_logs
    result["empty_step_logs"] = empty_logs
    result["malformed_step_logs"] = malformed_logs
    result["mismatched_step_logs"] = mismatched_logs
    result["incomplete_step_logs"] = incomplete_logs
    result["failed_step_logs"] = failed_logs
    if missing_logs:
        result["issues"].append(f"missing_step_logs {len(missing_logs)} first={missing_logs[0]}")
    if empty_logs:
        result["issues"].append(f"empty_step_logs {len(empty_logs)} first={empty_logs[0]}")
    if malformed_logs:
        result["issues"].append(f"malformed_step_logs {len(malformed_logs)} first={malformed_logs[0]}")
    if mismatched_logs:
        result["issues"].append(f"mismatched_step_logs {len(mismatched_logs)} first={mismatched_logs[0]}")
    if incomplete_logs:
        result["issues"].append(f"incomplete_step_logs {len(incomplete_logs)} first={incomplete_logs[0]}")
    if missing_logs or empty_logs or malformed_logs or mismatched_logs or incomplete_logs:
        result["status"] = "failed"
        result["next"] = TERMINAL_STAGE if stage == TERMINAL_STAGE else stage


def read_step_log_header(path):
    with path.open(encoding="utf-8") as handle:
        return parse_step_log_header(handle.readline().strip())


def read_step_log_footer(path):
    last = ""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last = line.strip()
    return parse_step_log_footer(last)


def parse_step_log_header(line):
    parts = line.split()
    if not parts or parts[0] != "self_verify_step":
        raise ValueError("missing self_verify_step header")
    fields = parse_key_value_fields(parts[1:])
    missing = [
        name
        for name in ("cycle", "step", "make_bin", "cwd", "event_log", "started_at")
        if not fields.get(name)
    ]
    if missing:
        raise ValueError(f"missing {'/'.join(missing)}")
    if not fields["cycle"].isdigit():
        raise ValueError(f"invalid cycle {fields['cycle']}")
    if parse_iso_datetime(fields["started_at"]) is None:
        raise ValueError(f"invalid started_at {fields['started_at']}")
    return fields


def parse_step_log_footer(line):
    parts = line.split()
    if not parts or parts[0] != "self_verify_step_complete":
        raise ValueError("missing self_verify_step_complete footer")
    fields = parse_key_value_fields(parts[1:])
    missing = [
        name
        for name in ("cycle", "step", "exit_code", "ended_at")
        if not fields.get(name)
    ]
    if missing:
        raise ValueError(f"missing {'/'.join(missing)}")
    if not fields["cycle"].isdigit():
        raise ValueError(f"invalid cycle {fields['cycle']}")
    if not fields["exit_code"].isdigit():
        raise ValueError(f"invalid exit_code {fields['exit_code']}")
    if parse_iso_datetime(fields["ended_at"]) is None:
        raise ValueError(f"invalid ended_at {fields['ended_at']}")
    return fields


def parse_key_value_fields(parts):
    fields = {}
    for part in parts:
        if "=" not in part:
            raise ValueError(f"malformed field {part}")
        key, value = part.split("=", 1)
        if not key:
            raise ValueError(f"malformed field {part}")
        if key in fields:
            raise ValueError(f"duplicate field {key}")
        fields[key] = value
    return fields


def step_log_header_mismatch(header, row, expected_event_log):
    header_started_at = parse_iso_datetime(header["started_at"])
    row_started_at = parse_iso_datetime(row["started_at"])
    row_ended_at = parse_iso_datetime(row["ended_at"])
    return (
        int(header["cycle"]) != int(row["cycle"])
        or header["step"] != row["step"]
        or header["cwd"] != str(ROOT)
        or header["event_log"] != str(expected_event_log)
        or row_started_at is None
        or row_ended_at is None
        or header_started_at < row_started_at
        or header_started_at > row_ended_at
    )


def step_log_footer_mismatch(footer, row):
    footer_ended_at = parse_iso_datetime(footer["ended_at"])
    row_started_at = parse_iso_datetime(row["started_at"])
    row_ended_at = parse_iso_datetime(row["ended_at"])
    return (
        int(footer["cycle"]) != int(row["cycle"])
        or footer["step"] != row["step"]
        or footer["exit_code"] != row["exit_code"]
        or row_started_at is None
        or row_ended_at is None
        or footer_ended_at < row_started_at
        or footer_ended_at > row_ended_at
    )


def apply_event_log_contract(result, summary_path, stage):
    path = event_log_path(summary_path)
    if not path.is_file():
        result["missing_event_log"] = path
        result["issues"].append(f"missing_event_log {path}")
    elif path.stat().st_size == 0:
        result["empty_event_log"] = path
        result["issues"].append(f"empty_event_log {path}")
    else:
        try:
            records = read_event_log_records(path)
        except ValueError as e:
            result["malformed_event_log"] = path
            result["issues"].append(f"malformed_event_log {path}: {e}")
        else:
            if not records:
                result["empty_event_records"] = path
                result["issues"].append(f"empty_event_records {path}")
            else:
                missing, mismatched, missing_step_events = event_provenance_issues(
                    records,
                    summary_path,
                    rows_for_event_provenance(result),
                )
                result["missing_event_provenance"] = missing
                result["mismatched_event_provenance"] = mismatched
                result["missing_step_events"] = missing_step_events
                if missing:
                    result["issues"].append(f"missing_event_provenance {len(missing)} first={missing[0]}")
                if mismatched:
                    result["issues"].append(f"mismatched_event_provenance {len(mismatched)} first={mismatched[0]}")
                if missing_step_events:
                    result["issues"].append(
                        f"missing_step_events {len(missing_step_events)} first={missing_step_events[0]}"
                    )
                if not missing and not mismatched and not missing_step_events:
                    return
    result["status"] = "failed"
    result["next"] = TERMINAL_STAGE if stage == TERMINAL_STAGE else stage


def rows_for_event_provenance(result):
    return result.get("rows_for_event_provenance") or []


def event_provenance_issues(records, summary_path, rows):
    rows_by_step = {
        (row["cycle"], row["step"]): row
        for row in rows
        if row.get("cycle", "").isdigit() and row.get("step") in VALID_LOG_STEPS
    }
    expected_event_steps = {
        (row["cycle"], row["step"])
        for row in rows
        if row.get("cycle", "").isdigit() and row.get("step") in EVENT_EMITTING_STEPS
    }
    expected_summary = str(summary_path)
    expected_event_log = str(event_log_path(summary_path))
    missing = []
    mismatched = []
    matched_event_steps = set()
    required = (
        "self_verify_summary",
        "self_verify_event_log",
        "self_verify_cycle",
        "self_verify_step",
        "component",
        "event",
        "status",
        "ts",
    )
    for line_number, record in enumerate(records, start=1):
        missing_fields = [field for field in required if not record.get(field)]
        if missing_fields:
            missing.append(f"line {line_number} missing={','.join(missing_fields)}")
            continue
        cycle = str(record["self_verify_cycle"])
        step = str(record["self_verify_step"])
        row = rows_by_step.get((cycle, step))
        if (
            str(record["self_verify_summary"]) != expected_summary
            or str(record["self_verify_event_log"]) != expected_event_log
            or row is None
        ):
            mismatched.append(f"line {line_number} cycle={cycle} step={step}")
            continue
        event_ts = parse_iso_datetime(str(record["ts"]))
        started_at = parse_iso_datetime(row["started_at"])
        ended_at = parse_iso_datetime(row["ended_at"])
        if event_ts is None:
            mismatched.append(f"line {line_number} invalid_ts={record['ts']}")
            continue
        if started_at is None or ended_at is None:
            continue
        if event_ts < started_at or event_ts > ended_at:
            mismatched.append(f"line {line_number} cycle={cycle} step={step} ts_out_of_range={record['ts']}")
            continue
        if event_shape_matches(record, row):
            matched_event_steps.add((cycle, step))
    missing_step_events = [
        f"cycle={cycle} step={step}"
        for cycle, step in sorted(
            expected_event_steps - matched_event_steps,
            key=event_step_sort_key,
        )
    ]
    return missing, mismatched, missing_step_events


def event_step_sort_key(item):
    cycle, step = item
    return (int(cycle), EVENT_EMITTING_STEPS.index(step))


def event_shape_matches(record, row):
    expected = EXPECTED_STEP_EVENTS.get(row["step"])
    if expected is None:
        return False
    component, event = expected
    status = str(record["status"])
    return (
        str(record["component"]) == component
        and str(record["event"]) == event
        and status in VALID_STATUSES
        and status == row["status"]
    )


def parse_iso_datetime(value):
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def read_event_log_records(path):
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"line {line_number}: {e.msg}") from e
        if not isinstance(record, dict):
            raise ValueError(f"line {line_number}: expected object")
        records.append(record)
    return records


def step_log_path(summary_path, row):
    if not row.get("cycle", "").isdigit() or row.get("step") not in VALID_LOG_STEPS:
        return None
    return Path(summary_path).with_name("logs") / f"cycle-{int(row['cycle']):04d}-{row['step']}.log"


def event_log_path(summary_path):
    return Path(summary_path).with_name("events.ndjson")


def _issue_value(row, key):
    return row.get(key) or "(blank)"


if __name__ == "__main__":
    sys.exit(main())
