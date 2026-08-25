#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

make_fake_path() {
    fakebin="$1"
    mkdir -p "$fakebin"

    cat >"$fakebin/curl" <<'SH'
#!/bin/sh
# Two shapes of /health call in doctor.sh: the status probe (-w %{http_code}) and the body read
# that carries build_sha. Answering only the first is what left the drift check untestable.
case " $* " in
  *" -w %{http_code} "*) printf 200; exit 0 ;;
esac
case " $* " in
  *"/health"*)
    if [ -n "${DOCTOR_BUILD_SHA:-}" ]; then
        printf '{"status":"ok","build_sha":"%s"}' "$DOCTOR_BUILD_SHA"
    else
        printf '{"status":"ok"}'
    fi
    exit 0
    ;;
esac
# Anything this fake does not recognise is an unmodelled call, and answering it with a
# cheerful exit 0 is how a fixture stops being able to see a check at all — #217 removed
# exactly this catch-all from the fake python3, and it was re-introduced here one binary
# over. A new curl-based doctor check must fail loudly here until this fake models it.
echo "fake curl: unmodelled call: $*" >&2
exit 7
SH

    cat >"$fakebin/docker" <<'SH'
#!/bin/sh
if [ "${1:-}" = compose ] && [ "${2:-}" = version ]; then
    echo "Docker Compose version v2.27.0"
    exit 0
fi
if [ "${1:-}" = compose ] && [ "${2:-}" = ps ]; then
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
  *) exit 1 ;;
esac
SH

    cat >"$fakebin/python3" <<'SH'
#!/bin/sh
case "${1:-}" in
  */event_log.py)
    if [ "${2:-}" = --record ]; then
        if [ -n "${DOCTOR_EVENT_CALLS:-}" ]; then
            printf '%s %s %s\n' "${3:-}" "${4:-}" "${5:-}" >>"$DOCTOR_EVENT_CALLS"
        fi
        exit 0
    fi
    if [ "${2:-}" = --stale-gates ]; then
        if [ "${DOCTOR_STALE_GATES_FAIL:-0}" = 1 ]; then
            echo "stale gate: eval_graphrag_gate last_seen_days=35"
            exit 1
        fi
        exit 0
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

    mkdir -p "$home/.claude" "$home/.cache/boring-distill" "$boring/vault/wiki" "$boring/agents/codex" "$boring/agents/shared" "$boring/scripts"
    touch "$boring/agents/codex/collect-sessions.py"
    touch "$boring/agents/shared/event_log.py"
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

# doctor compares the engine's build_sha against `git -C "$BORING_HOME" rev-parse HEAD`, so a
# fixture that is not a repo can only ever reach the "cannot read HEAD" branch. This makes the
# case a one-commit repo and prints the sha the assertions compare against.
make_case_repo() {
    case_dir="$1"
    make_case "$case_dir" yes
    git init -q "$case_dir/boring"
    git -C "$case_dir/boring" \
        -c user.email=fixture@example.invalid -c user.name=fixture -c commit.gpgsign=false \
        commit -q --allow-empty -m "fixture head"
    git -C "$case_dir/boring" rev-parse HEAD
}

run_strict() {
    case_dir="$1"
    out="$2"
    HOME="$case_dir/home" \
    BORING_HOME="$case_dir/boring" \
    BORING_URL="http://127.0.0.1:7700" \
    BORING_READINESS_NOTE_MAX_HOURS="${BORING_READINESS_NOTE_MAX_HOURS:-48}" \
    DOCTOR_EVENT_CALLS="$case_dir/events.calls" \
    PATH="$TMP/fakebin:$PATH" \
    sh "$ROOT/scripts/doctor.sh" --strict >"$out" 2>&1
}

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
if ( DOCTOR_VERIFY_LLM_FAIL=1 run_strict "$TMP/provider-fail" "$TMP/provider-fail.out" ); then
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

make_case "$TMP/stale-note" yes
old_note="$TMP/stale-note/boring/vault/wiki/wiki-0001.md"
old_epoch=$(( $(date +%s) - 7200 ))
python3 -c 'import os, sys; os.utime(sys.argv[1], (int(sys.argv[2]), int(sys.argv[2])))' "$old_note" "$old_epoch"
if ( BORING_READINESS_NOTE_MAX_HOURS=1 run_strict "$TMP/stale-note" "$TMP/stale-note.out" ); then
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

make_case "$TMP/stale-marker" yes
touch "$TMP/stale-marker/home/.cache/boring-distill/stale.pending"
old_marker_epoch=$(( $(date +%s) - 7200 ))
python3 -c 'import os, sys; os.utime(sys.argv[1], (int(sys.argv[2]), int(sys.argv[2])))' "$TMP/stale-marker/home/.cache/boring-distill/stale.pending" "$old_marker_epoch"
if ( BORING_READINESS_PENDING_TTL=60 run_strict "$TMP/stale-marker" "$TMP/stale-marker.out" ); then
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
if ( BORING_READINESS_PENDING_TTL=abc run_strict "$TMP/invalid-ttl" "$TMP/invalid-ttl.out" ); then
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

# (a2b) deploy drift. Merging is not deploying — ten merged PRs ran nowhere for two days once.
# The check only warns, so nothing but these assertions can tell it from a deleted block.
make_case "$TMP/build-sha-absent" yes
if ! run_strict "$TMP/build-sha-absent" "$TMP/build-sha-absent.out"; then
    cat "$TMP/build-sha-absent.out"
    echo "FAIL: a missing build_sha is a warning, not a strict failure" >&2
    exit 1
fi
case "$(cat "$TMP/build-sha-absent.out")" in
  *"engine reports no build_sha"*) ;;
  *)
    cat "$TMP/build-sha-absent.out"
    echo "FAIL: strict doctor did not report the missing build_sha" >&2
    exit 1
    ;;
esac

head_sha="$(make_case_repo "$TMP/build-sha-drift")"
# Drift fails readiness as of 2026-08-25. It was a warning, and the warning did not work:
# #221 sat merged-but-not-deployed and collected zero labels while readiness stayed green.
if ( DOCTOR_BUILD_SHA=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef \
     run_strict "$TMP/build-sha-drift" "$TMP/build-sha-drift.out" ); then
    cat "$TMP/build-sha-drift.out"
    echo "FAIL: deploy drift must fail strict readiness, not merely warn" >&2
    exit 1
fi
case "$(cat "$TMP/build-sha-drift.out")" in
  *"DEPLOY DRIFT — engine runs deadbeef, checkout is at "*) ;;
  *)
    cat "$TMP/build-sha-drift.out"
    echo "FAIL: strict doctor did not report deploy drift" >&2
    exit 1
    ;;
esac
case "$(cat "$TMP/build-sha-drift.out")" in
  *"runs the checked-out commit"*)
    cat "$TMP/build-sha-drift.out"
    echo "FAIL: drifted engine was reported as running the checkout" >&2
    exit 1
    ;;
esac

matched_sha="$(make_case_repo "$TMP/build-sha-match")"
if ! ( DOCTOR_BUILD_SHA="$matched_sha" \
       run_strict "$TMP/build-sha-match" "$TMP/build-sha-match.out" ); then
    cat "$TMP/build-sha-match.out"
    echo "FAIL: strict doctor should pass when the engine runs the checkout" >&2
    exit 1
fi
case "$(cat "$TMP/build-sha-match.out")" in
  *"engine runs the checked-out commit ($(printf '%.8s' "$matched_sha"))"*) ;;
  *)
    cat "$TMP/build-sha-match.out"
    echo "FAIL: strict doctor did not confirm the engine runs the checkout" >&2
    exit 1
    ;;
esac
case "$(cat "$TMP/build-sha-match.out")" in
  *"DEPLOY DRIFT"*)
    cat "$TMP/build-sha-match.out"
    echo "FAIL: matching shas were reported as drift" >&2
    exit 1
    ;;
esac

# The fixture's own fakes are checked here, because a fake that answers an unmodelled call with
# exit 0 makes every future check built on it vacuous — that is #217's defect, and it was living
# in the fake curl until 2026-08-25. Nothing else in this file exercises an unmodelled call, so
# without this assertion the guard would be unprovable.
if "$TMP/fakebin/curl" -sf http://127.0.0.1:7700/some-endpoint-the-fake-does-not-model >/dev/null 2>&1; then
    echo "FAIL: fake curl answers unmodelled calls successfully — checks built on it are vacuous" >&2
    exit 1
fi

# (a1) hook wiring. install.sh may register the tilde form, which the shell expands at run time
# but which a grep for the expanded path never matches — doctor called working hooks missing.
make_case "$TMP/hooks-tilde" yes
cat >"$TMP/hooks-tilde/home/.claude/settings.json" <<'JSON'
{"hooks":["~/oh-my-boring/hooks/distill-session.py","~/oh-my-boring/hooks/recall.py"]}
JSON
if ! run_strict "$TMP/hooks-tilde" "$TMP/hooks-tilde.out"; then
    cat "$TMP/hooks-tilde.out"
    echo "FAIL: tilde-form hook paths are wired and must not be called missing" >&2
    exit 1
fi
case "$(cat "$TMP/hooks-tilde.out")" in
  *"Claude Code hooks wired in"*) ;;
  *)
    cat "$TMP/hooks-tilde.out"
    echo "FAIL: strict doctor did not recognise tilde-form hook wiring" >&2
    exit 1
    ;;
esac

make_case "$TMP/hooks-partial" yes
cat >"$TMP/hooks-partial/home/.claude/settings.json" <<'JSON'
{"hooks":["~/oh-my-boring/hooks/distill-session.py"]}
JSON
if run_strict "$TMP/hooks-partial" "$TMP/hooks-partial.out"; then
    cat "$TMP/hooks-partial.out"
    echo "FAIL: strict doctor should fail when only one hook is wired" >&2
    exit 1
fi
case "$(cat "$TMP/hooks-partial.out")" in
  *"Claude Code hooks missing in"*) ;;
  *)
    cat "$TMP/hooks-partial.out"
    echo "FAIL: strict doctor did not report the half-wired hooks" >&2
    exit 1
    ;;
esac

# A gate that stopped running is the failure doctor.sh (d4b) names — silence reads as green.
make_case "$TMP/stale-gates" yes
if ( DOCTOR_STALE_GATES_FAIL=1 run_strict "$TMP/stale-gates" "$TMP/stale-gates.out" ); then
    cat "$TMP/stale-gates.out"
    echo "FAIL: strict doctor should fail when a watched gate has gone stale" >&2
    exit 1
fi
case "$(cat "$TMP/stale-gates.out")" in
  *"a watched gate has gone stale — it is not failing, it stopped running"*) ;;
  *)
    cat "$TMP/stale-gates.out"
    echo "FAIL: strict doctor did not report the stale gate" >&2
    exit 1
    ;;
esac

echo "doctor strict gate tests passed"
