#!/bin/sh
# Guardrail tests for backup-db.sh retention policy parsing.
# BORING_BACKUP_KEEP controls deletion of old dumps; zero/invalid values must
# fail before Docker is touched, or a new backup can be pruned immediately.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$ROOT/scripts/backup-db.sh"
PASS=0
FAIL=0

check() {
  if [ "$2" = 0 ]; then
    echo "ok - $1"
    PASS=$((PASS + 1))
  else
    echo "FAIL - $1"
    FAIL=$((FAIL + 1))
  fi
}

run_keep() {
  WORK="$(mktemp -d)"
  RC=0
  BORING_BACKUP_DIR="$WORK/backups" BORING_BACKUP_KEEP="$1" sh "$SCRIPT" >"$WORK/out" 2>&1 || RC=$?
}

teardown() {
  rm -rf "$WORK"
}

run_keep 0
case "$(cat "$WORK/out")" in
  *"BORING_BACKUP_KEEP must be at least 1"*) msg=0 ;;
  *) msg=1 ;;
esac
{ [ "$RC" = 1 ] && [ "$msg" = 0 ]; }
check "BORING_BACKUP_KEEP=0 fails before pruning" $?
teardown

run_keep nope
case "$(cat "$WORK/out")" in
  *"BORING_BACKUP_KEEP must be a positive integer"*) msg=0 ;;
  *) msg=1 ;;
esac
{ [ "$RC" = 1 ] && [ "$msg" = 0 ]; }
check "BORING_BACKUP_KEEP rejects non-numeric values" $?
teardown

echo
echo "backup-db guardrails: $PASS passed, $FAIL failed."
[ "$FAIL" = 0 ]
