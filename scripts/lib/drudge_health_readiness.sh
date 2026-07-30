#!/bin/sh
# drudge_health_readiness.sh — shared /health db_healthy judge for readiness scripts.
#
# Usage: . scripts/lib/drudge_health_readiness.sh
#        check_drudge_db_healthy "$URL"
#
# Fetches /health once and inspects the db_healthy field.
#   - missing / empty / true  -> return 0 (healthy or vector-off/legacy)
#   - false                   -> return 1 (postgres degraded)
# Prints one diagnostic line to stdout in both cases.
#
# Important: jq's `//` treats `false` and `null` as missing, so we use `has()`
# to distinguish "field present and false" from "field absent".

check_drudge_db_healthy() {
    _url="${1%/}/health"
    _body=$(curl -s -m5 "$_url" 2>/dev/null) || true
    _db_healthy=$(printf '%s' "$_body" | jq -r 'if has("db_healthy") then .db_healthy else "" end' 2>/dev/null) || true

    case "$_db_healthy" in
        false)
            printf 'engine /health reports db_healthy=false — postgres is degraded\n'
            return 1
            ;;
        true)
            printf 'engine /health db_healthy=true — write door healthy\n'
            return 0
            ;;
        '')
            printf 'engine /health db_healthy absent — vector off or legacy response, treating as pass\n'
            return 0
            ;;
        *)
            printf 'engine /health db_healthy has unexpected value "%s"; treating as pass\n' "$_db_healthy"
            return 0
            ;;
    esac
}
