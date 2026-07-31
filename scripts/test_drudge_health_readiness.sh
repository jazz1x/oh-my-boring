#!/bin/sh
# Guardrail: scripts/lib/drudge_health_readiness.sh, exercised through the REAL jq.
#
# The helper's whole job is distinguishing "db_healthy is false" from "db_healthy is
# absent". jq's `//` collapses those two — `false // empty` prints nothing — so a filter
# written that way reports every degraded engine as healthy and the gates stay green
# through an outage. A fake jq cannot catch that; only the real binary can.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/drudge_health_readiness.sh
. "$ROOT/scripts/lib/drudge_health_readiness.sh"

if ! command -v jq >/dev/null 2>&1; then
    echo "SKIP: jq not installed — this test exists to check real jq semantics"
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null' EXIT

fails=0
check() {
    label="$1"; expected="$2"; url="$3"
    check_drudge_db_healthy "$url" >/dev/null 2>&1
    actual=$?
    if [ "$actual" = "$expected" ]; then
        echo "PASS: $label (exit $actual)"
    else
        echo "FAIL: $label — expected exit $expected, got $actual"
        fails=$((fails + 1))
    fi
}

# A one-shot HTTP server per case keeps this hermetic: no drudge, no postgres, no docker.
serve_once() {
    body="$1"; port="$2"
    python3 - "$body" "$port" <<'PY' &
import sys, http.server
body, port = sys.argv[1].encode(), int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
http.server.HTTPServer(('127.0.0.1', port), H).serve_forever()
PY
    SRV=$!
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        curl -sf -m1 "http://127.0.0.1:$port/health" >/dev/null 2>&1 && return 0
        sleep 0.3
    done
    echo "FAIL: stub server did not come up on $port"
    fails=$((fails + 1))
}

stop_server() { [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null; SRV=""; sleep 0.2; }

# The case the `//` bug silently inverted.
serve_once '{"status":"degraded","vector":true,"sync":"idle","corpus_count":5,"db_healthy":false}' 7841
check "db_healthy=false blocks" 1 "http://127.0.0.1:7841"
stop_server

serve_once '{"status":"ok","vector":true,"sync":"idle","corpus_count":5,"db_healthy":true}' 7842
check "db_healthy=true passes" 0 "http://127.0.0.1:7842"
stop_server

# Wiki-first engine or a build older than the probe: absence must not fail a gate.
serve_once '{"status":"ok","vector":false,"sync":"idle","corpus_count":5}' 7843
check "absent db_healthy passes" 0 "http://127.0.0.1:7843"
stop_server

serve_once 'not json at all' 7844
check "non-JSON body passes" 0 "http://127.0.0.1:7844"
stop_server

# Engine reachability is the callers' own check; this helper must not double-fail it.
check "unreachable engine passes" 0 "http://127.0.0.1:7845"

# Without jq the helper cannot parse anything, so it must fail open rather than red.
PATH_BACKUP="$PATH"
mkdir -p "$TMP/nojq"
PATH="$TMP/nojq"
serve_once_status=0
check "missing jq passes" 0 "http://127.0.0.1:7846"
PATH="$PATH_BACKUP"

if [ "$fails" -ne 0 ]; then
    echo "FAILED: $fails case(s)"
    exit 1
fi
echo "ok - drudge health readiness helper"
