#!/usr/bin/env python3
"""Report the injection-channel verdict from the recorded `injection_uptake` events.

Run it on the window's closing date (docs/PRD.md §2 registers 2026-09-09) or any time before,
to see how far the sample has come:

    python3 scripts/uptake-verdict.py
    python3 scripts/uptake-verdict.py --agent claude-code --since 2026-08-26

Reads the ENGINE's event store over HTTP, not `~/.cache/oh-my-boring/events.ndjson`. That file is
a spool the writer fills only when the DB sink fails, so on a healthy install it is empty — and an
empty spool reads exactly like an instrument that never fired. It cost this project a false alarm.

Rates are reported per agent, never pooled: Claude Code injects on every prompt while Kimi
throttles to once per session, so a combined rate answers neither product's question.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agents", "shared"))

import verdict_core  # noqa: E402

DEFAULT_URL = os.environ.get("BORING_URL") or "http://127.0.0.1:7700"
COUNTERS = ("used_prompts", "total_prompts", "used_control_prompts")


def fetch_events(base_url, limit):
    url = f"{base_url.rstrip('/')}/events?limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")).get("entries") or []
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"[uptake-verdict] engine unreachable at {url}: {exc}", file=sys.stderr)
        return None


def field(row, key):
    """Counters live at the top level or inside `attributes` depending on the writer."""
    value = row.get(key)
    if value is None:
        value = (row.get("attributes") or {}).get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def collect(rows, since=None, agent=None):
    """Fold uptake events into per-agent totals, skipping rows the instrument predates.

    A session logged before `used_control_prompts` existed reports 0 for it, which would read as
    a zero chance rate rather than as a missing measurement — so those rows are dropped, and the
    count of what was dropped is returned rather than hidden.
    """
    per_agent = defaultdict(lambda: defaultdict(int))
    skipped_old = 0
    for row in rows:
        if row.get("event") != "injection_uptake":
            continue
        observed = row.get("observed_at") or ""
        if since and observed[:10] < since:
            continue
        who = row.get("agent") or (row.get("attributes") or {}).get("agent") or "unknown"
        if agent and who != agent:
            continue
        has_control_prompts = row.get("used_control_prompts") is not None or (
            "used_control_prompts" in (row.get("attributes") or {})
        )
        if not has_control_prompts:
            skipped_old += 1
            continue
        bucket = per_agent[who]
        bucket["sessions"] += 1
        for key in COUNTERS:
            bucket[key] += field(row, key)
    return per_agent, skipped_old


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--limit", type=int, default=5000, help="events to scan (default 5000)")
    ap.add_argument("--since", help="ISO date; drop events observed before it")
    ap.add_argument("--agent", help="report only this adapter")
    args = ap.parse_args(argv)

    rows = fetch_events(args.url, args.limit)
    if rows is None:
        return 2

    per_agent, skipped_old = collect(rows, since=args.since, agent=args.agent)
    if skipped_old:
        print(
            f"[uptake-verdict] {skipped_old}건 제외 — per-prompt 대조가 없던 시기의 이벤트"
            " (0 으로 세면 우연율이 0 인 것처럼 읽힌다)"
        )
    if not per_agent:
        print("[uptake-verdict] 집계할 injection_uptake 이벤트가 없다 — 계측 조사 대상(PRD §2)")
        return 1

    for who, c in sorted(per_agent.items()):
        v = verdict_core.verdict(
            c["sessions"], c["used_prompts"], c["total_prompts"], c["used_control_prompts"]
        )
        print(f"\n[{who}]")
        for line in verdict_core.format_verdict(v):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
