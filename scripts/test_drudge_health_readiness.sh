#!/bin/sh
# Direct test of scripts/lib/drudge_health_readiness.sh using the real jq binary.
# Verifies the helper's contract without relying on the fake jq stub used by test_doctor.sh.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT INT TERM

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not installed — cannot run real-jq helper tests"
    exit 0
fi

# shellcheck source=scripts/lib/drudge_health_readiness.sh
. "$ROOT/scripts/lib/drudge_health_readiness.sh"

# Helpers to inject fake curl/jq binaries at the front of PATH.
make_fake_curl() {
    _body="$1"
    _name="$2"
    _fakebin="$TMP/curl-$_name"
    mkdir -p "$_fakebin"
    cat >"$_fakebin/curl" <<SH
#!/bin/sh
printf '%s' '$_body'
exit 0
SH
    chmod +x "$_fakebin/curl"
    printf '%s' "$_fakebin"
}

make_failing_curl() {
    _fakebin="$TMP/curl-fail"
    mkdir -p "$_fakebin"
    cat >"$_fakebin/curl" <<'SH'
#!/bin/sh
exit 7
SH
    chmod +x "$_fakebin/curl"
    printf '%s' "$_fakebin"
}

make_no_jq_path() {
    _fakebin="$TMP/no-jq"
    mkdir -p "$_fakebin"
    # Intentionally no jq binary here.
    printf '%s' "$_fakebin"
}

run_case() {
    _label="$1"
    _expected="$2"
    _fakebin="$3"

    if PATH="$_fakebin:$PATH" check_drudge_db_healthy "http://127.0.0.1:7700" >/dev/null 2>&1; then
        _rc=0
    else
        _rc=1
    fi

    if [ "$_rc" -ne "$_expected" ]; then
        echo "FAIL: $_label — expected exit $_expected, got $_rc"
        exit 1
    fi
    echo "PASS: $_label"
}

# Real jq cases: the helper must distinguish false from absent.
run_case "db_healthy=false" 1 "$(make_fake_curl '{"db_healthy":false}' 'false')"
run_case "db_healthy=true" 0 "$(make_fake_curl '{"db_healthy":true}' 'true')"
run_case "db_healthy absent" 0 "$(make_fake_curl '{"status":"ok","vector":false}' 'absent')"

# Degenerate paths: helper must not fail closed on malformed/unavailable data.
run_case "curl failure (empty body)" 0 "$(make_failing_curl)"
run_case "non-JSON body" 0 "$(make_fake_curl 'not valid json' 'nonjson')"
run_case "jq not in PATH" 0 "$(make_no_jq_path)"

echo "drudge_health_readiness helper tests passed"
