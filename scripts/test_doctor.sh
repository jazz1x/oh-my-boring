#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
REAL_PYTHON="$(command -v python3)"
export REAL_PYTHON
trap 'rm -rf "$TMP"' EXIT INT TERM

make_fake_path() {
    fakebin="$1"
    mkdir -p "$fakebin"

    cat >"$fakebin/curl" <<'SH'
#!/bin/sh
case " $* " in
  *" -w %{http_code} "*) printf 200 ;;
  *)
    if [ -n "${DOCTOR_HEALTH_BODY:-}" ]; then
      printf '%s' "$DOCTOR_HEALTH_BODY"
    else
      printf '{"status":"%s","vector":true,"sync":"idle","corpus_count":1,"db_healthy":%s}' \
        "${DOCTOR_HEALTH_STATUS:-ok}" "${DOCTOR_HEALTH_DB_HEALTHY:-true}"
    fi
    ;;
esac
exit 0
SH

    cat >"$fakebin/docker" <<'SH'
#!/bin/sh
if [ "${1:-}" = compose ] && [ "${2:-}" = version ]; then
    echo "Docker Compose version v2.27.0"
    exit 0
fi
if [ "${1:-}" = compose ] && [ "${2:-}" = ps ]; then
    if [ "${DOCTOR_DOCKER_PS_FAIL:-0}" = 1 ]; then
        echo "permission denied while trying to connect to the docker API" >&2
        exit 1
    fi
    echo "boring-drudge Up"
    exit 0
fi
exit 1
SH

    cat >"$fakebin/jq" <<'SH'
#!/bin/sh
case "${2:-}" in
  '.llm.provider // "ollama"') echo ollama ;;
  '.llm.base_url // "http://host.docker.internal:11434/v1"') echo "http://localhost:11434/v1" ;;
  'if has("db_healthy") then .db_healthy else "" end')
    if [ -n "${DOCTOR_HEALTH_DB_HEALTHY+set}" ]; then
      printf '%s' "$DOCTOR_HEALTH_DB_HEALTHY"
    else
      printf 'true'
    fi
    ;;
  '.status')
    printf '%s' "${DOCTOR_HEALTH_STATUS:-ok}"
    ;;
  *) exit 1 ;;
esac
SH

    cat >"$fakebin/python3" <<'SH'
#!/bin/sh
case "${1:-}" in
  */dedup-wiki.py)
    exec "$REAL_PYTHON" "$@"
    ;;
  */event_log.py)
    if [ "${2:-}" = --record ]; then
        if [ -n "${DOCTOR_EVENT_CALLS:-}" ]; then
            printf '%s %s %s\n' "${3:-}" "${4:-}" "${5:-}" >>"$DOCTOR_EVENT_CALLS"
        fi
        exit 0
    fi
    if [ "${BORING_EVENT_RECENT_HOURS:-24}" = 0 ]; then
        echo "[event-log] invalid config: BORING_EVENT_RECENT_HOURS must be a positive integer, got 0" >&2
        exit 2
    fi
    echo "resolution_quality recent_failures=0 log=/tmp/events.ndjson"
    exit 0
    ;;
esac
if [ "${2:-}" = --status ]; then
    echo "[codex-status] host_worker found=true loaded=true kind=launchd path=/tmp/fake.plist"
    exit 0
fi
exit 1
SH

    chmod +x "$fakebin/curl" "$fakebin/docker" "$fakebin/jq" "$fakebin/python3"
}

make_case() {
    case_dir="$1"
    with_note="$2"
    home="$case_dir/home"
    boring="$case_dir/boring"

    mkdir -p "$home/.claude" "$home/.cache/boring-distill" "$boring/vault/wiki" "$boring/agents/codex" "$boring/agents/shared" "$boring/scripts/lib"
    touch "$boring/agents/codex/collect-sessions.py"
    touch "$boring/agents/shared/event_log.py"
    cp "$ROOT/scripts/dedup-wiki.py" "$boring/scripts/dedup-wiki.py"
    cp "$ROOT/scripts/lib/drudge_health_readiness.sh" "$boring/scripts/lib/drudge_health_readiness.sh"
    touch "$home/.cache/boring-distill/session.ts"
    [ "$with_note" = yes ] && touch "$boring/vault/wiki/wiki-0001.md"
    printf 'DRUDGE_TOKEN=local\n' >"$boring/.env"
    chmod 600 "$boring/.env"
    cat >"$boring/boring.json" <<'JSON'
{"llm":{"provider":"ollama","base_url":"http://localhost:11434/v1"}}
JSON
    cat >"$home/.claude/settings.json" <<JSON
{"hooks":["$boring/hooks/distill-session.py","$boring/hooks/recall.py"]}
JSON
    cat >"$boring/scripts/verify-llm.sh" <<'SH'
#!/bin/sh
if [ "${DOCTOR_VERIFY_LLM_FAIL:-0}" = 1 ]; then
    echo "verify-llm failed by test"
    exit 1
fi
echo "verify-llm ok"
SH
    chmod +x "$boring/scripts/verify-llm.sh"
}

run_strict() (
    case_dir="$1"
    out="$2"
    HOME="$case_dir/home" \
    BORING_HOME="$case_dir/boring" \
    BORING_URL="http://127.0.0.1:7700" \
    BORING_READINESS_NOTE_MAX_HOURS="${BORING_READINESS_NOTE_MAX_HOURS:-48}" \
    DOCTOR_EVENT_CALLS="$case_dir/events.calls" \
    PATH="$TMP/fakebin:$PATH" \
    sh "$ROOT/scripts/doctor.sh" --strict >"$out" 2>&1
)

make_fake_path "$TMP/fakebin"

make_case "$TMP/pass" yes
if ! run_strict "$TMP/pass" "$TMP/pass.out"; then
    cat "$TMP/pass.out"
    echo "FAIL: strict doctor should pass when every readiness proof exists" >&2
    exit 1
fi
case "$(cat "$TMP/pass/events.calls")" in
  *"doctor readiness ok"*) ;;
  *)
    cat "$TMP/pass/events.calls"
    echo "FAIL: strict doctor pass event was not recorded" >&2
    exit 1
    ;;
esac

make_case "$TMP/fail" no
if run_strict "$TMP/fail" "$TMP/fail.out"; then
    cat "$TMP/fail.out"
    echo "FAIL: strict doctor should fail without a distilled note" >&2
    exit 1
fi
case "$(cat "$TMP/fail.out")" in
  *"readiness: one or more doctor checks failed"*) ;;
  *)
    cat "$TMP/fail.out"
    echo "FAIL: strict doctor failure message missing" >&2
    exit 1
    ;;
esac
case "$(cat "$TMP/fail/events.calls")" in
  *"doctor readiness failed"*) ;;
  *)
    cat "$TMP/fail/events.calls"
    echo "FAIL: strict doctor failure event was not recorded" >&2
    exit 1
    ;;
esac

make_case "$TMP/provider-fail" yes
if DOCTOR_VERIFY_LLM_FAIL=1 run_strict "$TMP/provider-fail" "$TMP/provider-fail.out"; then
    cat "$TMP/provider-fail.out"
    echo "FAIL: strict doctor should fail when verify-llm fails" >&2
    exit 1
fi
case "$(cat "$TMP/provider-fail.out")" in
  *"LLM provider/model/embed contract failed"*) ;;
  *)
    cat "$TMP/provider-fail.out"
    echo "FAIL: strict doctor did not surface verify-llm failure" >&2
    exit 1
    ;;
esac
unset DOCTOR_VERIFY_LLM_FAIL

make_case "$TMP/docker-ps-fail" yes
if DOCTOR_DOCKER_PS_FAIL=1 run_strict "$TMP/docker-ps-fail" "$TMP/docker-ps-fail.out"; then
    cat "$TMP/docker-ps-fail.out"
    echo "FAIL: strict doctor should fail when compose ps cannot inspect containers" >&2
    exit 1
fi
case "$(cat "$TMP/docker-ps-fail.out")" in
  *"container status unavailable via"*"permission denied while trying to connect to the docker API"*) ;;
  *)
    cat "$TMP/docker-ps-fail.out"
    echo "FAIL: strict doctor hid docker compose ps failure as an empty container list" >&2
    exit 1
    ;;
esac
unset DOCTOR_DOCKER_PS_FAIL

make_case "$TMP/stale-note" yes
old_note="$TMP/stale-note/boring/vault/wiki/wiki-0001.md"
old_epoch=$(( $(date +%s) - 7200 ))
python3 -c 'import os, sys; os.utime(sys.argv[1], (int(sys.argv[2]), int(sys.argv[2])))' "$old_note" "$old_epoch"
if BORING_READINESS_NOTE_MAX_HOURS=1 run_strict "$TMP/stale-note" "$TMP/stale-note.out"; then
    cat "$TMP/stale-note.out"
    echo "FAIL: strict doctor should fail when newest note is stale" >&2
    exit 1
fi
case "$(cat "$TMP/stale-note.out")" in
  *"note_freshness age_s="*"newest note is stale"*) ;;
  *)
    cat "$TMP/stale-note.out"
    echo "FAIL: strict doctor did not report note freshness failure" >&2
    exit 1
    ;;
esac

make_case "$TMP/generated-brief-fresh" yes
source_note="$TMP/generated-brief-fresh/boring/vault/wiki/wiki-0001.md"
generated_note="$TMP/generated-brief-fresh/boring/vault/wiki/wiki-0002.md"
old_epoch=$(( $(date +%s) - 7200 ))
python3 -c 'import os, sys; os.utime(sys.argv[1], (int(sys.argv[2]), int(sys.argv[2])))' "$source_note" "$old_epoch"
cat >"$generated_note" <<'MD'
---
tags:
  - daily-brief
---
generated briefing output, not source memory
MD
if BORING_READINESS_NOTE_MAX_HOURS=1 run_strict "$TMP/generated-brief-fresh" "$TMP/generated-brief-fresh.out"; then
    cat "$TMP/generated-brief-fresh.out"
    echo "FAIL: strict doctor should ignore generated brief freshness" >&2
    exit 1
fi
case "$(cat "$TMP/generated-brief-fresh.out")" in
  *"path=$source_note"*"newest note is stale"*) ;;
  *)
    cat "$TMP/generated-brief-fresh.out"
    echo "FAIL: strict doctor used generated brief as source freshness evidence" >&2
    exit 1
    ;;
esac

make_case "$TMP/similar-brief-tag-is-source" yes
old_source_note="$TMP/similar-brief-tag-is-source/boring/vault/wiki/wiki-0001.md"
similar_tag_note="$TMP/similar-brief-tag-is-source/boring/vault/wiki/wiki-0002.md"
old_epoch=$(( $(date +%s) - 7200 ))
python3 -c 'import os, sys; os.utime(sys.argv[1], (int(sys.argv[2]), int(sys.argv[2])))' "$old_source_note" "$old_epoch"
cat >"$similar_tag_note" <<'MD'
---
tags:
  - not-daily-brief
---
source memory that should still count for freshness
MD
if ! BORING_READINESS_NOTE_MAX_HOURS=1 run_strict "$TMP/similar-brief-tag-is-source" "$TMP/similar-brief-tag-is-source.out"; then
    cat "$TMP/similar-brief-tag-is-source.out"
    echo "FAIL: strict doctor treated a similar tag as generated daily-brief output" >&2
    exit 1
fi
case "$(cat "$TMP/similar-brief-tag-is-source.out")" in
  *"path=$similar_tag_note"*) ;;
  *)
    cat "$TMP/similar-brief-tag-is-source.out"
    echo "FAIL: strict doctor did not use the similar-tag source note for freshness" >&2
    exit 1
    ;;
esac

make_case "$TMP/stale-marker" yes
touch "$TMP/stale-marker/home/.cache/boring-distill/stale.pending"
old_marker_epoch=$(( $(date +%s) - 7200 ))
python3 -c 'import os, sys; os.utime(sys.argv[1], (int(sys.argv[2]), int(sys.argv[2])))' "$TMP/stale-marker/home/.cache/boring-distill/stale.pending" "$old_marker_epoch"
if BORING_READINESS_PENDING_TTL=60 run_strict "$TMP/stale-marker" "$TMP/stale-marker.out"; then
    cat "$TMP/stale-marker.out"
    echo "FAIL: strict doctor should fail when pending marker is stale" >&2
    exit 1
fi
case "$(cat "$TMP/stale-marker.out")" in
  *"marker_health writable=1 stale_pending=1"*) ;;
  *)
    cat "$TMP/stale-marker.out"
    echo "FAIL: strict doctor did not report stale marker failure" >&2
    exit 1
    ;;
esac

make_case "$TMP/invalid-ttl" yes
if BORING_READINESS_PENDING_TTL=abc run_strict "$TMP/invalid-ttl" "$TMP/invalid-ttl.out"; then
    cat "$TMP/invalid-ttl.out"
    echo "FAIL: strict doctor should fail on invalid marker TTL" >&2
    exit 1
fi
case "$(cat "$TMP/invalid-ttl.out")" in
  *"invalid pending marker TTL 'abc'"*) ;;
  *)
    cat "$TMP/invalid-ttl.out"
    echo "FAIL: strict doctor did not report invalid marker TTL" >&2
    exit 1
    ;;
esac

make_case "$TMP/invalid-recent-hours" yes
if BORING_EVENT_RECENT_HOURS=0 run_strict "$TMP/invalid-recent-hours" "$TMP/invalid-recent-hours.out"; then
    cat "$TMP/invalid-recent-hours.out"
    echo "FAIL: strict doctor should fail on invalid recent event window" >&2
    exit 1
fi
case "$(cat "$TMP/invalid-recent-hours.out")" in
  *"[event-log] invalid config: BORING_EVENT_RECENT_HOURS must be a positive integer"*) ;;
  *)
    cat "$TMP/invalid-recent-hours.out"
    echo "FAIL: strict doctor did not report invalid recent event window" >&2
    exit 1
    ;;
esac

make_case "$TMP/db-degraded" yes
if DOCTOR_HEALTH_DB_HEALTHY=false DOCTOR_HEALTH_STATUS=degraded run_strict "$TMP/db-degraded" "$TMP/db-degraded.out"; then
    cat "$TMP/db-degraded.out"
    echo "FAIL: strict doctor should fail when db_healthy=false" >&2
    exit 1
fi
case "$(cat "$TMP/db-degraded.out")" in
  *"postgres is degraded"*) ;;
  *)
    cat "$TMP/db-degraded.out"
    echo "FAIL: strict doctor did not report postgres degraded" >&2
    exit 1
    ;;
esac
unset DOCTOR_HEALTH_DB_HEALTHY DOCTOR_HEALTH_STATUS

make_case "$TMP/vector-off" yes
if DOCTOR_HEALTH_DB_HEALTHY='' run_strict "$TMP/vector-off" "$TMP/vector-off.out"; then
    :
else
    cat "$TMP/vector-off.out"
    echo "FAIL: strict doctor should pass when db_healthy is absent (vector off)" >&2
    exit 1
fi
case "$(cat "$TMP/vector-off.out")" in
  *"engine /health 200"*"write door reachable"*) ;;
  *)
    cat "$TMP/vector-off.out"
    echo "FAIL: strict doctor did not report healthy engine for vector-off response" >&2
    exit 1
    ;;
esac
unset DOCTOR_HEALTH_DB_HEALTHY

echo "doctor strict gate tests passed"
