#!/bin/sh
# Structural gate (stack-free) — pre-commit + local. Enforces here the
# *mechanically enforceable* parts of PHILOSOPHY.md/RUST-STYLE.md. No stack (pg/ollama) needed.
#   1) rustfmt   — formatting (linear readability)
#   2) clippy -D — §A no-unwrap/expect/panic, todo, unreachable + ADT (wildcard), pedantic
#   3) test      — guardrail tests
#   4) py-compile — syntax gate for all Python touched by pre-commit
#   5) py-unit   — network-free Python regression tests (incl. destructive-path planners)
#   6) sh-unit   — destructive shell-path guardrails (backup retention + restore-db drop ordering)
#   7) sh-unit   — readiness gate guardrails (doctor --strict exit semantics)
#   8) sh-unit   — provider/model guardrails (verify-llm embedding shape)
# No bypassing (git commit --no-verify) — on failure, fix the root cause (don't paper over the symptom).
set -eu
PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-${TMPDIR:-/tmp}/oh-my-boring-pyc}"
export PYTHONPYCACHEPREFIX
mkdir -p "$PYTHONPYCACHEPREFIX"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
EVENT_LOG="$ROOT/agents/shared/event_log.py"
BORING_EVENT_SINK="${BORING_EVENT_SINK:-spool}"
BORING_EVENT_LOG="${BORING_EVENT_LOG:-${TMPDIR:-/tmp}/oh-my-boring-guard-events.ndjson}"
export BORING_EVENT_SINK BORING_EVENT_LOG
guard_run_id="guard-$(date +%Y%m%dT%H%M%S)-$$"
guard_started_at="$(date +%s)"

log_guard_event() {
  status="$1"
  shift
  if [ -f "$EVENT_LOG" ]; then
    if ! python3 "$EVENT_LOG" --record guard structural_guard "$status" \
      --field "run_id=$guard_run_id" "$@"; then
      echo "⚠ guard event log write failed" >&2
    fi
  fi
}

finish_guard_event() {
  rc=$?
  duration_s="$(($(date +%s) - guard_started_at))"
  if [ "$rc" -eq 0 ]; then
    log_guard_event ok --field "duration_s=$duration_s"
  else
    log_guard_event failed --field "duration_s=$duration_s" --field "exit_code=$rc"
  fi
  exit "$rc"
}

trap finish_guard_event EXIT

cd "$ROOT/drudge"
echo "1) rustfmt --check…"
cargo fmt --check
echo "2) clippy (-D warnings)…"
cargo clippy --quiet --all-targets -- -D warnings
echo "3) test…"
cargo test --quiet
cd "$ROOT"
echo "4) python py-compile (agents + hooks + scripts + data/eval)…"
fd --type f --extension py --hidden --no-ignore . agents hooks scripts data/eval -x python3 -m py_compile
echo "5) python unit tests…"
python3 agents/shared/test_boring_config.py
python3 agents/shared/test_omb_env.py
python3 agents/shared/test_agent_wiring.py
python3 agents/shared/test_distill_core.py
python3 agents/shared/test_event_log.py
python3 agents/shared/test_workflow_contract.py
python3 agents/shared/test_markers.py
python3 agents/shared/test_resolution_quality.py
python3 agents/shared/test_transcript.py
python3 agents/shared/test_recall_core.py
python3 agents/shared/test_code_recall_core.py
python3 agents/shared/test_briefing_format.py
python3 agents/shared/test_drudge_client.py
python3 agents/claude-code/test_hooks.py
python3 agents/codex/test_codex.py
python3 agents/kimi/test_kimi.py
python3 agents/schedulers/test_collectors.py
python3 agents/hermes/test_briefing_format.py
python3 agents/hermes/test_briefing_quality.py
python3 agents/hermes/test_ingest_worker.py
python3 scripts/test_guard_contract.py
python3 scripts/test_dedup_wiki.py
python3 scripts/test_data_steward.py
python3 scripts/test_vault_cleanup_gate.py
python3 scripts/test_retention.py
python3 scripts/test_self_verify_loop.py
python3 scripts/test_self_verify_cycle.py
python3 scripts/test_self_verify_contract.py
echo "6) shell destructive-path guardrails (backup/restore DB)…"
sh scripts/test_backup_db.sh
sh scripts/test_restore_db.sh
echo "7) shell readiness gate guardrails (doctor --strict + health helper)…"
sh scripts/test_doctor.sh
sh scripts/test_drudge_health_readiness.sh
echo "8) shell LLM/provider guardrails (verify-llm)…"
sh scripts/test_verify_llm.sh
echo "9) vault lint --strict (schema/frontmatter/wikilink/sources)…"
"$ROOT/drudge/target/release/drudge" vault lint --strict
echo "✅ 구조 게이트 통과 — 컴파일러/clippy/test + Python adapters + vault lint 무위반."
