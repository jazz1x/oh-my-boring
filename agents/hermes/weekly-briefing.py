#!/usr/bin/env python3
"""주간 브리핑 — ohmyboring RAG 회수·합성을 stdout 으로.

hermes-agent cron --no-agent --script 로 호출 → stdout 이 그대로 Slack DM 등으로 배달.
지능은 ohmyboring 엔진이 SSOT. 이 스크립트는 호출+포맷만 담당.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

# Allow cron to run this script from any cwd while still importing the sibling
# renderer. The renderer lives in the same directory as this script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from briefing_quality import check_briefing_quality, format_quality_log
from slack_briefing import (
    maybe_print_blocks_json,
    render_body_mrkdwn,
    render_message_mrkdwn,
)

HERMES_URL = os.environ.get("BORING_URL") or os.environ.get(
    "DRUDGE_URL", "http://boring-drudge:7700"
)
KST = timezone(timedelta(hours=9))


def _weekly_window(now: datetime) -> tuple[datetime, datetime, int, int]:
    """Return (last_monday_midnight_kst, last_sunday_235959_kst, since_hours, until_hours).

    The weekly digest covers the previous calendar week: Monday 00:00 KST
    through Sunday 23:59:59 KST. `since_hours` is the distance from now back
    to last Monday midnight; `until_hours` is the distance from now back to
    last Sunday 23:59:59 so the engine can apply a hard upper bound.
    """
    this_monday = now - timedelta(days=now.weekday())
    this_monday_mid = this_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    last_monday_mid = this_monday_mid - timedelta(days=7)
    last_sunday_2359 = this_monday_mid - timedelta(seconds=1)
    since_hours = max(24, int((now - last_monday_mid).total_seconds() // 3600))
    until_hours = max(0, int((now - last_sunday_2359).total_seconds() // 3600))
    return last_monday_mid, last_sunday_2359, since_hours, until_hours


# ISO week: YYYY-WNN
TODAY = datetime.now(KST)
WEEK = TODAY.strftime("%G-W%V")
LAST_MONDAY, LAST_SUNDAY, SINCE_HOURS, UNTIL_HOURS = _weekly_window(TODAY)
PERIOD = f"{LAST_MONDAY.strftime('%Y-%m-%d')} ~ {LAST_SUNDAY.strftime('%Y-%m-%d')}"
TITLE = "📅 주간 브리핑"
STAMP = f"{WEEK} · {PERIOD}"
EMPTY_MESSAGE = "지난 주는 새로 짚을 진행/막힘 항목이 회수되지 않았어요."


def header(body: str) -> str:
    return f"*{TITLE}*\n`{STAMP}`\n\n{body}"


def slack_mrkdwn(answer: str) -> str:
    return render_body_mrkdwn(answer)


def main() -> None:
    req = urllib.request.Request(
        f"{HERMES_URL}/weekly",
        data=json.dumps(
            {"since_hours": SINCE_HOURS, "until_hours": UNTIL_HOURS}
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(header(f"⚠️ ohmyboring(RAG) 응답 없음 — 엔진 가동 확인 필요. ({e})"))
        return
    except json.JSONDecodeError:
        print(header("⚠️ 응답 파싱 실패 — ohmyboring 점검 필요."))
        return

    answer = (data.get("answer") or "").strip()
    sources = data.get("sources") or []
    if not answer:
        print(header(EMPTY_MESSAGE))
        return

    quality = check_briefing_quality(answer, sources, SINCE_HOURS, UNTIL_HOURS, kind="weekly")
    if quality.level == "fail":
        print(
            header(
                "⚠️ 브리핑 품질 계약 위반 — 엔진/prompt 점검 필요. "
                f"({' · '.join(quality.metrics.violations)})"
            )
        )
        sys.stderr.write(format_quality_log(quality) + "\n")
        sys.exit(1)
    if quality.level == "warn":
        sys.stderr.write(format_quality_log(quality) + "\n")

    if maybe_print_blocks_json(TITLE, STAMP, answer, sources, EMPTY_MESSAGE):
        return
    print(render_message_mrkdwn(f"*{TITLE}*", STAMP, answer, sources, EMPTY_MESSAGE))


if __name__ == "__main__":
    main()
    sys.exit(0)
