#!/bin/sh
# Schema parity: a database upgraded from the committed baseline must end up identical to one
# created from scratch.
#
# Every DB suite in this repo starts from an empty container, so "the migration was never written"
# is a defect class no test can see. It has already cost real work: `CHECK (language IN ('rust'))`
# lives inside `CREATE TABLE IF NOT EXISTS`, which never runs again once the table exists, so
# widening the literal reached new databases only. A worker hid that by hand-ALTERing the live
# database to make its own proof pass, and every gate stayed green.
#
# `data/schema/baseline.sql` is a snapshot of the schema as shipped. It is deliberately NOT
# regenerated when the schema changes — it stands for "a database already out there". Refresh it
# only when dropping support for upgrading from that far back, and say so in the commit.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASELINE="$ROOT/data/schema/baseline.sql"
[ -f "$BASELINE" ] || { echo "✗ baseline not found: $BASELINE" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "✗ docker required" >&2; exit 1; }

NAME="drudge-schema-parity-$$"
TMP="$(mktemp -d)"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

echo "▶ starting disposable pgvector container ($NAME) …"
docker run -d --name "$NAME" -p 127.0.0.1::5432 \
    -e POSTGRES_USER=boring -e POSTGRES_PASSWORD=boring -e POSTGRES_DB=boring \
    pgvector/pgvector:pg16 >/dev/null || { echo "✗ failed to start $NAME" >&2; exit 1; }

ready=0
for _ in $(seq 1 30); do
    docker exec "$NAME" pg_isready -U boring -d boring >/dev/null 2>&1 && { ready=1; break; }
    sleep 1
done
[ "$ready" = 1 ] || { echo "✗ pgvector container never became ready" >&2; exit 1; }
port="$(docker port "$NAME" 5432/tcp | cut -d: -f2)"

# Two databases in one container: cheaper than two containers, and they cannot see each other.
docker exec "$NAME" psql -U boring -d postgres -q -c 'CREATE DATABASE fresh;' >/dev/null
docker exec "$NAME" psql -U boring -d postgres -q -c 'CREATE DATABASE upgraded;' >/dev/null

# `upgraded` starts life as the shipped schema, the way a real installation does.
docker exec -i "$NAME" psql -U boring -d upgraded -q -v ON_ERROR_STOP=1 <"$BASELINE" >/dev/null

# Both databases then get the current code's initialize path. `fresh` builds from nothing;
# `upgraded` must migrate. The suites are the initialize path — running them is how this
# script avoids a second, drift-prone copy of the DDL.
run_suites() {
    db="$1"
    BORING_TEST_DATABASE_URL="postgresql://boring:boring@127.0.0.1:$port/$db" \
        sh -c 'cd "$0/drudge" && cargo test -p drudge --test store_integration --test code_index_integration -- --test-threads=1' \
        "$ROOT" >"$TMP/$db.log" 2>&1 || {
        echo "✗ initialize/suites failed against '$db' — see $TMP/$db.log" >&2
        tail -20 "$TMP/$db.log" >&2
        return 1
    }
}
echo "▶ initializing 'fresh' from nothing …"
run_suites fresh
echo "▶ initializing 'upgraded' from $BASELINE …"
run_suites upgraded

# Compare structure only. Row data differs (each suite writes its own fixtures) and is not the
# subject; `--schema-only` already excludes it.
dump() {
    # `\restrict`/`\unrestrict` carry a nonce pg_dump regenerates per run, so they differ on
    # every comparison and say nothing about the schema. Dropping those two lines is the whole
    # normalisation — everything else stays comparable, including constraint definitions.
    docker exec "$NAME" pg_dump -U boring -d "$1" --schema-only --no-owner --no-privileges \
        | sed '/^--/d; /^$/d; /^\\restrict /d; /^\\unrestrict /d' | sort
}
dump fresh >"$TMP/fresh.sql"
dump upgraded >"$TMP/upgraded.sql"

if diff -u "$TMP/fresh.sql" "$TMP/upgraded.sql" >"$TMP/diff.txt"; then
    echo "✓ schema parity — an upgraded database matches a fresh one"
    exit 0
fi

echo "✗ SCHEMA PARITY BROKEN — a database upgraded from the baseline is not what fresh code builds." >&2
echo "  Something changed the schema without a migration that repairs an existing database." >&2
echo "  (-fresh / +upgraded)" >&2
head -40 "$TMP/diff.txt" >&2
exit 1
