#!/bin/sh
# Shared /health readiness judge for the release/briefing gates.
#
#   . scripts/lib/drudge_health_readiness.sh
#   check_drudge_db_healthy "$URL" || <caller's own failure idiom>
#
# /health stays HTTP 200 even when Postgres is unreachable, because it is a liveness
# signal — readiness is the caller's question, and `db_healthy` is the answer. Gates
# that only check for 200 would report a healthy write door during a total outage.
#
# Fails ONLY on an explicit db_healthy=false. A missing field (vector off, older
# engine), an unreachable engine, absent jq, or a non-JSON body all pass: absence of
# evidence must not turn a gate red, and engine reachability is a separate check the
# callers already make.
#
# Note the filter avoids jq's `//`, which treats `false` as absent and would report
# every degraded engine as healthy.
check_drudge_db_healthy() {
    _url="${1%/}/health"
    _body=$(curl -s -m5 "$_url" 2>/dev/null) || true
    _db_healthy=$(
        printf '%s' "$_body" |
            jq -r 'if has("db_healthy") then .db_healthy else "" end' 2>/dev/null
    ) || true

    case "$_db_healthy" in
        false)
            printf 'engine /health reports db_healthy=false — postgres is degraded, writes will fail\n'
            return 1
            ;;
        true)
            printf 'engine /health db_healthy=true — write door healthy\n'
            return 0
            ;;
        '')
            printf 'engine /health db_healthy absent — vector off or older engine, treating as pass\n'
            return 0
            ;;
        *)
            printf 'engine /health db_healthy is unexpected value "%s" — treating as pass\n' "$_db_healthy"
            return 0
            ;;
    esac
}
