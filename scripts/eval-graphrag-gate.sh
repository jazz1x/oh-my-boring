#!/bin/sh
# GraphRAG behavioral regression gate — requires a live drudge stack on :7700.
# Injects GraphRAG eval fixtures into the live vault, syncs, runs the A/B harness,
# then cleans up. Mirrors scripts/eval-gate.sh for operational consistency.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

URL="${BORING_URL:-http://localhost:7700}"
EVENT_LOG="$ROOT/agents/shared/event_log.py"
eval_run_id="eval-graphrag-$(date +%Y%m%dT%H%M%S)-$$"
eval_started_at="$(date +%s)"
eval_fixtures_copied=0

log_eval_event() {
  status="$1"
  shift
  if [ -f "$EVENT_LOG" ]; then
    if ! python3 "$EVENT_LOG" --record eval eval_graphrag_gate "$status" \
      --field "run_id=$eval_run_id" "$@"; then
      echo "⚠ eval-graphrag event log write failed" >&2
    fi
  fi
}

cleanup() {
  if [ "$eval_fixtures_copied" -eq 1 ]; then
    rm -f vault/wiki/eval-*.md
    echo "▶ Cleaned up GraphRAG eval fixtures from vault/wiki"
    if curl -s -m600 -X POST "$URL/sync" >/dev/null 2>&1; then
      echo "▶ Re-synced vault after GraphRAG eval cleanup"
    else
      echo "⚠ GraphRAG eval cleanup sync failed; run 'make sync' before relying on briefings" >&2
    fi
  fi
}

finish_eval_event() {
  rc=$?
  cleanup
  duration_s="$(($(date +%s) - eval_started_at))"
  if [ "$rc" -eq 0 ]; then
    log_eval_event ok --field "duration_s=$duration_s" --field "fixtures_copied=$eval_fixtures_copied"
  else
    log_eval_event failed --field "duration_s=$duration_s" --field "fixtures_copied=$eval_fixtures_copied" --field "exit_code=$rc"
  fi
  exit "$rc"
}

trap finish_eval_event EXIT

if ! curl -s -m3 "$URL/health" >/dev/null 2>&1; then
  echo "engine not running ($URL). Run 'make up' first."
  exit 1
fi

if [ ! -f data/eval/run_graph_eval.py ] || [ ! -f data/eval/graph-golden.json ]; then
  echo "⏭  GraphRAG eval harness not present — skipped."
  exit 0
fi

echo "▶ Copying GraphRAG eval fixtures into vault/wiki …"
cp data/eval/fixtures/eval-*.md vault/wiki/
eval_fixtures_copied=1

echo "▶ Syncing GraphRAG eval corpus …"
curl -s -m600 -X POST "$URL/sync" >/dev/null

echo "▶ Running GraphRAG eval …"
python3 data/eval/run_graph_eval.py
