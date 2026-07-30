#!/bin/sh
# Code-lane behavioral gate (full stack) — drudge must be up on :7700 with
# BORING_VECTOR=on and a populated code graph (`make code-index`).
# Sibling of eval-gate.sh (semantic lane); this one covers the AST code lane.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

URL="${BORING_URL:-http://localhost:7700}"

if ! curl -s -m3 "$URL/health" >/dev/null 2>&1; then
  echo "engine not running ($URL). Run 'make up' first."
  exit 1
fi

# Graceful skip: without the code graph there is nothing to gate on.
probe=$(curl -s -m10 -X POST "$URL/code-search" -H 'content-type: application/json' \
  -d '{"query":"eval_fixture_parse","max_symbols":1}' 2>/dev/null || true)
case "$probe" in
  *'"hits"'*) : ;;
  *)
    echo "⏭  code lane unavailable ($URL/code-search rejected — BORING_VECTOR=off?) — code eval gate skipped."
    exit 0
    ;;
esac
case "$probe" in
  *eval_fixture_parse*) : ;;
  *)
    echo "⏭  code graph has no eval fixtures — run 'make code-index' first. Code eval gate skipped."
    exit 0
    ;;
esac

echo "▶ Running code-lane eval …"
python3 data/eval/run_code_eval.py
