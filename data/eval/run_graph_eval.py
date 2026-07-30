#!/usr/bin/env python3
"""GraphRAG contribution eval for oh-my-boring.

Compares two retrieval paths against the same live stack on :7700:

1. `/search`  -> vector + BM25 RRF (no graph, no claim authority, no LLM).
2. `/ask`     -> vector + BM25 RRF + graph-linked documents + claim authority + LLM synthesis.

The harness reports Recall@3 for each path and counts "graph-only" recalls:
documents that appear in `/ask` sources but not in `/search` top-3 hits.

Run via `make eval-graphrag` (requires a live stack). Fixtures are copied into
vault/wiki, synced, evaluated, then cleaned up by the caller (`eval-graphrag-gate.sh`).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BORING_URL = os.environ.get("BORING_URL") or "http://localhost:7700"
GOLDEN = Path(__file__).with_name("graph-golden.json")
K = 3


def load_golden() -> dict:
    with GOLDEN.open(encoding="utf-8") as f:
        return json.load(f)


def call_search(query: str, k: int = K) -> tuple[list[str], float]:
    body = json.dumps({"query": query, "max_results": k, "max_tokens": 2000}).encode()
    req = urllib.request.Request(
        f"{BORING_URL}/search",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"search failed for {query!r}: {e}", file=sys.stderr)
        return [], 0.0
    elapsed_ms = (time.monotonic() - started) * 1000
    hits = data.get("hits", [])
    ids = []
    for h in hits:
        src = h.get("source_path") or ""
        base = Path(src).stem
        if base:
            ids.append(base)
    return ids, elapsed_ms


def call_ask(question: str, k: int = K) -> tuple[list[str], float]:
    body = json.dumps({"question": question, "max_results": k, "max_tokens": 2000}).encode()
    req = urllib.request.Request(
        f"{BORING_URL}/ask",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
    except urllib.error.URLError as e:
        print(f"ask failed for {question!r}: {e}", file=sys.stderr)
        return [], 0.0
    elapsed_ms = (time.monotonic() - started) * 1000
    sources = data.get("sources", []) or []
    ids = []
    for src in sources:
        base = Path(str(src)).stem
        if base:
            ids.append(base)
    return ids, elapsed_ms


def first_rank(ids: list[str], expected: set[str]) -> int | None:
    for i, sid in enumerate(ids, start=1):
        if sid in expected:
            return i
    return None


def main() -> int:
    golden = load_golden()
    queries = golden.get("queries", [])
    if not queries:
        print("no queries in graph-golden.json")
        return 0

    search_recall = 0
    ask_recall = 0
    graph_only_recall = 0
    search_total_ms = 0.0
    ask_total_ms = 0.0
    rows: list[dict] = []

    for q in queries:
        query = q["query"]
        expected = set(q["expect"])
        search_ids, search_ms = call_search(query, K)
        ask_ids, ask_ms = call_ask(query, K)
        search_rank = first_rank(search_ids, expected)
        ask_rank = first_rank(ask_ids, expected)
        search_hit = search_rank is not None
        ask_hit = ask_rank is not None
        graph_only_ids = [sid for sid in ask_ids if sid not in search_ids]
        graph_only_hit = any(sid in expected for sid in graph_only_ids)

        if search_hit:
            search_recall += 1
        if ask_hit:
            ask_recall += 1
        if graph_only_hit:
            graph_only_recall += 1
        search_total_ms += search_ms
        ask_total_ms += ask_ms

        rows.append(
            {
                "query": query,
                "expected": sorted(expected),
                "search_rank": search_rank,
                "ask_rank": ask_rank,
                "search_ids": search_ids,
                "ask_ids": ask_ids,
                "graph_only_ids": graph_only_ids,
                "search_ms": round(search_ms, 1),
                "ask_ms": round(ask_ms, 1),
            }
        )
        print(
            f"{query!r}\n"
            f"  search -> rank={search_rank} ids={search_ids} ({search_ms:.0f}ms)\n"
            f"  ask    -> rank={ask_rank} ids={ask_ids} ({ask_ms:.0f}ms)\n"
            f"  graph-only ids={graph_only_ids}"
        )

    n = len(queries)
    print(f"\nSearch Recall@{K}: {search_recall}/{n} = {search_recall / n:.2f}")
    print(f"Ask    Recall@{K}: {ask_recall}/{n} = {ask_recall / n:.2f}")
    print(f"Graph-only rescued: {graph_only_recall}/{n}")
    print(f"Avg latency search={search_total_ms / n:.0f}ms ask={ask_total_ms / n:.0f}ms")

    summary = {
        "search_recall_at_k": search_recall / n,
        "ask_recall_at_k": ask_recall / n,
        "graph_only_rescued": graph_only_recall / n,
        "avg_search_ms": search_total_ms / n,
        "avg_ask_ms": ask_total_ms / n,
        "queries": rows,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # Gate: Ask must not regress search. If search already perfect, ask must stay perfect.
    if ask_recall < search_recall:
        print("eval-graphrag: FAIL (ask recall below search recall)")
        return 1
    print("eval-graphrag: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
