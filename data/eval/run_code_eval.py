#!/usr/bin/env python3
"""Black-box service-contract eval for the oh-my-boring code lane (AST graph).

This script does NOT test drudge internals (those live in Rust #[cfg(test)]).
It loads data/eval/code-golden.json and calls the live /code-search endpoint
the same way the code-recall hook would, then reports Recall@k against the
golden fixture symbols (data/eval/code-fixtures/, indexed by `make code-index`).

Run via `make eval-code` (requires a live stack on :7700 with BORING_VECTOR=on
and a populated code graph).
"""
import json
import os
import sys
import urllib.request

BORING_URL = os.environ.get("BORING_URL") or "http://localhost:7700"
GOLDEN = os.path.join(os.path.dirname(__file__), "code-golden.json")


def load_golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def code_search(query, k):
    body = json.dumps({"query": query, "max_symbols": k}).encode()
    req = urllib.request.Request(
        f"{BORING_URL}/code-search",
        data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("hits", [])
    except Exception as e:
        print(f"code-search failed for {query!r}: {e}", file=sys.stderr)
        return None  # distinguish endpoint failure from an empty result


def main():
    golden = load_golden()
    queries = golden.get("queries", [])
    if not queries:
        print("no queries in code-golden.json")
        sys.exit(0)
    k = int(golden.get("k", 5))

    recall_at_k = 0
    failures = 0
    for q in queries:
        query = q["query"]
        expect = set(q["expect"])
        hits = code_search(query, k)
        if hits is None:
            failures += 1
            print(f"{query!r} -> endpoint ERROR")
            continue
        names = [h.get("name") or "" for h in hits]
        found = expect & set(names)
        if found:
            recall_at_k += 1
        print(f"{query!r} -> {'HIT' if found else 'MISS'} names={names}")

    n = len(queries)
    print(f"\nRecall@{k}: {recall_at_k}/{n} = {recall_at_k / n:.2f}")
    if failures:
        print(f"endpoint failures: {failures} (engine down or BORING_VECTOR=off?)")
    if recall_at_k < n or failures:
        print("code eval gate: FAIL (run 'make code-index' to populate the code graph)")
        sys.exit(1)
    print("code eval gate: PASS")


if __name__ == "__main__":
    main()
