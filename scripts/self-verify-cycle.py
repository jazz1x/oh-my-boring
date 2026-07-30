#!/usr/bin/env python3
"""Run one self-verification cycle and append evidence rows to summary.tsv."""

import argparse
from contextlib import contextmanager
import csv
import datetime as dt
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from self_verify_loop import (
    CYCLE_ROWS_VALID,
    DEFAULT_ROOT,
    FIELDNAMES,
    SUMMARY_PRESENT,
    VALID_STEPS,
    cycle_rows_state,
    fsync_parent_dir,
    newest_summary,
    read_summary_rows,
    steps_for_cycle,
    summary_rows_state,
)

ROOT = Path(__file__).resolve().parents[1]
STEP_EXECUTION_FAILURE_EXIT_CODE = 127


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one self-verification cycle")
    parser.add_argument("--cycle", type=_positive_int, default=None)
    parser.add_argument(
        "--summary",
        help="summary.tsv path; defaults to the newest existing run, or a new run for cycle 1",
    )
    parser.add_argument("--make-bin", default="make")
    args = parser.parse_args(argv)

    cycle, summary = resolve_cycle_and_summary_path(args.cycle, args.summary)
    if summary is None:
        display_cycle = cycle if cycle is not None else "auto"
        print(f"self_verify_cycle cycle={display_cycle} status=failed reason=no_existing_summary")
        return 1
    log_dir = summary.parent / "logs"
    event_log = summary.parent / "events.ndjson"
    rc, reason = run_cycle_status(
        cycle,
        summary,
        lambda step: run_make_step(
            args.make_bin,
            step,
            log_dir,
            cycle,
            event_log,
            summary_path=summary,
        ),
    )
    reason_text = "" if rc == 0 else f" reason={reason}"
    print(
        f"self_verify_cycle cycle={cycle} summary={summary} "
        f"status={'ok' if rc == 0 else 'failed'}{reason_text}"
    )
    return rc


def resolve_cycle_and_summary_path(explicit_cycle=None, explicit_summary=None, root=DEFAULT_ROOT):
    summary = resolve_summary_path(explicit_cycle, explicit_summary, root)
    if summary is None:
        return explicit_cycle, None
    cycle = resolve_cycle_for_summary(summary, explicit_cycle)
    return cycle, summary


def resolve_summary_path(cycle, explicit_summary=None, root=DEFAULT_ROOT):
    if explicit_summary:
        return Path(explicit_summary)
    if cycle in {None, 1}:
        existing = newest_summary(root)
        if existing is not None and cycle is None:
            return existing
        return new_summary_path(root)
    existing = newest_summary(root)
    return existing


def resolve_cycle_for_summary(summary_path, explicit_cycle=None):
    if explicit_cycle is not None:
        return explicit_cycle
    existing_status, existing_rows = read_existing_rows(summary_path)
    if existing_status == "present":
        return expected_next_cycle(existing_rows)
    return 1


def new_summary_path(root=DEFAULT_ROOT, now=None, nonce=None):
    instant = now or dt.datetime.now(dt.timezone.utc)
    run_nonce = nonce if nonce is not None else uuid.uuid4().hex
    run_id = f"{instant.strftime('%Y%m%dT%H%M%S%fZ')}-{run_nonce}"
    return root / run_id / "summary.tsv"


def run_cycle(cycle, summary_path, runner):
    rc, _reason = run_cycle_status(cycle, summary_path, runner)
    return rc


def run_cycle_status(cycle, summary_path, runner):
    try:
        cycle = normalized_cycle(cycle)
    except ValueError:
        return 1, "invalid_cycle"
    with summary_lock(summary_path):
        cycle_state = existing_cycle_state(summary_path, cycle)
        if cycle_state == "missing":
            return 1, "no_existing_summary"
        if cycle_state in {"empty", "malformed", "unreadable"}:
            return 1, f"{cycle_state}_summary"
        if cycle_state == "recorded":
            return 1, "cycle_already_recorded"
        if cycle_state == "cycle_not_next":
            return 1, "cycle_not_next"
        return run_cycle_locked(cycle, summary_path, runner)


def run_cycle_locked(cycle, summary_path, runner):
    failed = 0
    rows = []
    for step in steps_for_cycle(cycle):
        row = run_step(cycle, step, runner)
        rows.append(row)
        if row["exit_code"] != "0":
            failed += 1
    append_status = append_cycle_rows_locked(summary_path, rows)
    if append_status != "appended":
        return 1, append_status
    return (1, "step_failed") if failed else (0, "ok")


@contextmanager
def summary_lock(summary_path):
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = summary_path.with_name(f".{summary_path.name}.lock")
    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        yield


def append_cycle_rows(summary_path, rows):
    with summary_lock(summary_path):
        return append_cycle_rows_locked(summary_path, rows)


def append_cycle_rows_locked(summary_path, rows):
    batch_status, cycle = cycle_rows_state(rows)
    if batch_status != CYCLE_ROWS_VALID:
        return batch_status
    existing_status, existing_rows = read_existing_rows(summary_path)
    if existing_status == "absent" and cycle != "1":
        return "missing"
    if existing_status in {"empty", "malformed", "unreadable"}:
        return existing_status
    if any(row.get("cycle") == cycle for row in existing_rows):
        return "recorded"
    if existing_rows and int(cycle) != expected_next_cycle(existing_rows):
        return "cycle_not_next"

    tmp_path = summary_path.with_name(f".{summary_path.name}.tmp-{os.getpid()}")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, delimiter="\t")
            writer.writeheader()
            writer.writerows(existing_rows)
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(summary_path)
        fsync_parent_dir(summary_path)
    except OSError:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return "appended"


def existing_cycle_state(summary_path, cycle):
    existing_status, existing_rows = read_existing_rows(summary_path)
    if existing_status == "absent":
        return "clear" if cycle == 1 else "missing"
    if existing_status != "present":
        return existing_status
    if any(row.get("cycle") == str(cycle) for row in existing_rows):
        return "recorded"
    if cycle != expected_next_cycle(existing_rows):
        return "cycle_not_next"
    return "clear"


def expected_next_cycle(rows):
    return max(int(row["cycle"]) for row in rows) + 1


def read_existing_rows(summary_path):
    if not summary_path.exists():
        return "absent", []
    try:
        rows = read_summary_rows(summary_path)
    except ValueError:
        return "malformed", []
    except OSError:
        return "unreadable", []
    state = summary_rows_state(rows)
    return state, rows if state == SUMMARY_PRESENT else []


def run_step(cycle, step, runner):
    started_at = now_iso()
    started = time.monotonic()
    try:
        exit_code = normalize_exit_code(int(runner(step)))
    except Exception as e:
        print(f"[self-verify-cycle] step failed before exit code: {step}: {e}", file=sys.stderr)
        exit_code = STEP_EXECUTION_FAILURE_EXIT_CODE
    ended_at = now_iso()
    duration_s = max(0, int(round(time.monotonic() - started)))
    return {
        "cycle": str(cycle),
        "step": step,
        "status": "ok" if exit_code == 0 else "failed",
        "exit_code": str(exit_code),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": str(duration_s),
    }


def normalize_exit_code(returncode):
    if returncode < 0:
        return 128 + abs(returncode)
    return returncode


def run_make_step(make_bin, step, log_dir=None, cycle=None, event_log_path=None, *, summary_path=None):
    require_valid_step(step)
    require_cycle_for_log_dir(log_dir, cycle)
    require_event_log_path(event_log_path)
    require_summary_path_for_event_log(event_log_path, summary_path)
    require_cycle_for_event_log(event_log_path, cycle)
    if log_dir is not None:
        return run_make_step_with_log(
            make_bin,
            step,
            log_dir,
            cycle,
            event_log_path,
            summary_path=summary_path,
        )
    return run_make_step_with_log(
        make_bin,
        step,
        cycle=cycle,
        event_log_path=event_log_path,
        summary_path=summary_path,
    )


def run_make_step_with_log(make_bin, step, log_dir=None, cycle=None, event_log_path=None, *, summary_path=None):
    require_valid_step(step)
    require_cycle_for_log_dir(log_dir, cycle)
    require_event_log_path(event_log_path)
    require_summary_path_for_event_log(event_log_path, summary_path)
    require_cycle_for_event_log(event_log_path, cycle)
    if log_dir is None or cycle is None:
        return run_make_step_stream(
            make_bin,
            step,
            None,
            event_log_path,
            cycle,
            summary_path=summary_path,
        )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"cycle-{int(self_verify_cycle_text(cycle)):04d}-{step}.log"
    with log_path.open("w", encoding="utf-8") as handle:
        write_step_log_header(handle, make_bin, step, cycle, event_log_path)
        try:
            exit_code = run_make_step_stream(make_bin, step, handle, event_log_path, cycle, summary_path=summary_path)
        except Exception:
            write_step_log_footer(handle, step, cycle, STEP_EXECUTION_FAILURE_EXIT_CODE)
            raise
        write_step_log_footer(handle, step, cycle, exit_code)
        return exit_code


def write_step_log_header(handle, make_bin, step, cycle, event_log_path=None):
    cycle_text = self_verify_cycle_text(cycle)
    event_log_text = "" if event_log_path is None else f" event_log={self_verify_event_log_path(event_log_path)}"
    handle.write(
        f"self_verify_step cycle={int(cycle_text):04d} step={step} "
        f"make_bin={make_bin} cwd={ROOT}{event_log_text} started_at={now_iso()}\n"
    )
    handle.flush()


def write_step_log_footer(handle, step, cycle, exit_code):
    cycle_text = self_verify_cycle_text(cycle)
    handle.write(
        f"self_verify_step_complete cycle={int(cycle_text):04d} step={step} "
        f"exit_code={normalize_exit_code(int(exit_code))} ended_at={now_iso()}\n"
    )
    handle.flush()
    os.fsync(handle.fileno())


def run_make_step_stream(make_bin, step, log_handle, event_log_path=None, cycle=None, *, summary_path=None):
    require_valid_step(step)
    require_event_log_path(event_log_path)
    require_summary_path_for_event_log(event_log_path, summary_path)
    require_cycle_for_event_log(event_log_path, cycle)
    child_env = os.environ.copy()
    if event_log_path is not None:
        event_log = self_verify_event_log_path(event_log_path)
        summary_path_text = self_verify_summary_path_text(summary_path)
        cycle_text = self_verify_cycle_text(cycle)
        event_log.parent.mkdir(parents=True, exist_ok=True)
        child_env["BORING_EVENT_LOG"] = str(event_log)
        child_env["BORING_EVENT_SINK"] = "spool"
        child_env["BORING_SELF_VERIFY_EVENT_LOG"] = str(event_log)
        child_env["BORING_SELF_VERIFY_SUMMARY"] = summary_path_text
        child_env["BORING_SELF_VERIFY_STEP"] = step
        child_env["BORING_SELF_VERIFY_CYCLE"] = cycle_text
    process = subprocess.Popen(
        [make_bin, step],
        cwd=ROOT,
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    for chunk in process.stdout:
        print(chunk, end="")
        if log_handle is not None:
            log_handle.write(chunk)
            log_handle.flush()
    return process.wait()


def require_summary_path_for_event_log(event_log_path, summary_path):
    if event_log_path is not None and summary_path is None:
        raise ValueError("summary_path is required when event_log_path is set")
    if event_log_path is not None:
        self_verify_summary_path_text(summary_path)


def require_event_log_path(event_log_path):
    if event_log_path is not None:
        self_verify_event_log_path(event_log_path)


def require_cycle_for_event_log(event_log_path, cycle):
    if event_log_path is not None and cycle is None:
        raise ValueError("cycle is required when event_log_path is set")
    if event_log_path is not None:
        self_verify_cycle_text(cycle)


def require_cycle_for_log_dir(log_dir, cycle):
    if log_dir is not None and cycle is None:
        raise ValueError("cycle is required when log_dir is set")
    if log_dir is not None:
        self_verify_cycle_text(cycle)


def require_valid_step(step):
    if step not in VALID_STEPS:
        raise ValueError(f"unknown self-verify step: {step}")


def self_verify_event_log_path(event_log_path):
    path = Path(event_log_path)
    if not path.name:
        raise ValueError(f"event_log_path must name a file: {event_log_path}")
    return path


def self_verify_summary_path_text(summary_path):
    path = Path(summary_path)
    if not path.name:
        raise ValueError(f"summary_path must name a file: {summary_path}")
    return str(path)


def self_verify_cycle_text(cycle):
    try:
        value = int(cycle)
    except (TypeError, ValueError) as e:
        raise ValueError(f"cycle must be a positive integer: {cycle}") from e
    if value < 1:
        raise ValueError(f"cycle must be a positive integer: {cycle}")
    return str(value)


def normalized_cycle(cycle):
    return int(self_verify_cycle_text(cycle))


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _positive_int(raw):
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


if __name__ == "__main__":
    sys.exit(main())
