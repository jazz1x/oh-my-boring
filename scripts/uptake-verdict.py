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

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agents", "shared"))

import verdict_core  # noqa: E402
from verdict_core import collect, partition_at_repair, unreported  # noqa: E402

DEFAULT_URL = os.environ.get("BORING_URL") or "http://127.0.0.1:7700"


def fetch_events(base_url, limit):
    url = f"{base_url.rstrip('/')}/events?limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8")).get("entries") or []
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"[uptake-verdict] engine unreachable at {url}: {exc}", file=sys.stderr)
        return None


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

    # The owner kept the window and split the sample (PRD §8 D4). Rows written while the ledger
    # was pruned at 3 days measured a sample biased toward short sessions, so they are reported
    # but never read by the verdict — and they are reported rather than dropped, because hiding
    # them is the same move as quoting them.
    pre_rows, post_rows = partition_at_repair(rows)
    pre_agent, _ = collect(pre_rows, since=args.since, agent=args.agent)
    per_agent, skipped_old = collect(post_rows, since=args.since, agent=args.agent)
    if skipped_old:
        print(
            f"[uptake-verdict] {skipped_old}건 제외 — per-prompt 대조가 없던 시기의 이벤트"
            " (0 으로 세면 우연율이 0 인 것처럼 읽힌다)"
        )
    if not per_agent:
        # §2 turns "0 events" into an instrumentation investigation, but the clause it registers
        # is "세션 종료가 있는데 ... 0건" — sessions ended and nothing was recorded. Rows that were
        # skipped for predating `used_control_prompts` are proof the instrument fires; what is
        # young is the counter, not the measurement. Calling that a fault sends someone to debug
        # a hook that works, and worse, teaches them to distrust the clause when it is real.
        if skipped_old:
            print(
                f"[uptake-verdict] 아직 판정할 이벤트가 없다 — 계측은 돌고 있고"
                f"(옛 형식 {skipped_old}건) per-prompt 대조 카운터가 그보다 어리다."
                " 계측 결함이 아니라 표본이 쌓이는 중"
            )
            return 1
        if pre_agent:
            # Same trap the `skipped_old` branch above exists to avoid: an empty post-repair
            # bucket is not silence from the instrument, it is a boundary that everything is
            # still on the near side of. Calling it a fault sends someone to debug a hook that
            # works, and teaches them to distrust the clause when it finally is real.
            pre_sessions = sum(c["sessions"] for c in pre_agent.values())
            print(
                f"[uptake-verdict] 수리 이후 이벤트가 아직 없다 — 계측은 돌고 있고"
                f"(수리 이전 세션 {pre_sessions}건) 경계 {verdict_core.LEDGER_REPAIR_AT}"
                " 이후 표본이 쌓이는 중. 계측 결함이 아니다"
            )
            return 1
        print("[uptake-verdict] 집계할 injection_uptake 이벤트가 없다 — 계측 조사 대상(PRD §2)")
        return 1

    lost_sessions, lost_rows = unreported(rows)
    if lost_sessions:
        print(
            f"[uptake-verdict] 측정 안 된 주입: 세션 {lost_sessions} · 프롬프트 {lost_rows}"
            " — 종료되지 않고 사라진 세션. 판정에 합산하지 않는다(결과가 없으므로)"
        )

    if pre_agent:
        print(
            f"\n[수리 이전 · 판정에 쓰지 않음]  경계 {verdict_core.LEDGER_REPAIR_AT}"
        )
        for who, c in sorted(pre_agent.items()):
            used, total = c["used_prompts"], c["total_prompts"]
            print(
                f"  {who}: 세션 {c['sessions']} · 주입 프롬프트 {total} · 처치 {used}"
                f" · 대조 {c['used_control_prompts']}"
            )
        print(
            "  원장이 3일에 잘리던 시기다 — 3일보다 오래 산 세션은 채점 전에 증거가 사라졌고,"
            " 그 편향은 세션 길이 방향이다(PRD §8 D4). 비율을 내지 않는다."
        )

    for who, c in sorted(per_agent.items()):
        v = verdict_core.verdict(
            c["sessions"], c["used_prompts"], c["total_prompts"], c["used_control_prompts"]
        )
        print(f"\n[{who} · 수리 이후]")
        for line in verdict_core.format_verdict(v):
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
