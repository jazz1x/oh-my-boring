#!/usr/bin/env python3
"""Black-box service-contract eval for oh-my-boring retrieval.

This script does NOT test drudge internals (those live in Rust #[cfg(test)]).
It loads data/eval/golden.json and calls the live /search endpoint the same
way an external agent would, then reports Recall@k and MRR@k against the
golden fixture ids. It also reports a two-sided relevance error rate
(false_drop / false_pass) by exercising the same filter predicate that
`agents/shared/recall_core.py` ships.

Run via `make eval` (requires a live stack on :7700).
"""
import json
import os
import sys
import urllib.request

# The gate scores the predicate the agents actually run, imported rather than restated:
# a second copy would drift from the shipped behaviour while still reporting green.
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "agents", "shared")
)
from recall_core import RELEVANCE_MAX_DIST, exceeds_relevance_ceiling  # noqa: E402

BORING_URL = os.environ.get("BORING_URL") or "http://localhost:7700"
GOLDEN = os.path.join(os.path.dirname(__file__), "golden.json")
K = 3


def load_golden():
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)


def search(query, k=K):
    body = json.dumps({"query": query, "max_results": k, "max_tokens": 2000}).encode()
    req = urllib.request.Request(
        f"{BORING_URL}/search",
        data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("hits", [])
    except Exception as e:
        print(f"search failed for {query!r}: {e}", file=sys.stderr)
        return []


def source_ids(hits):
    out = []
    for h in hits:
        src = h.get("source_path") or ""
        base = os.path.basename(src)
        name = base.replace(".md", "")
        # wiki-NNNN ids can't be mapped back to fixture ids without extra metadata,
        # but a synced fixture copy keeps its basename.
        out.append(name)
    return out


def is_dropped(hit):
    """True if enforcing the shipped ceiling would discard this hit.

    This is `recall_core.exceeds_relevance_ceiling` itself, not a copy of it — the point of
    the gate is to score the predicate the agents actually run.
    """
    return exceeds_relevance_ceiling(hit)


def first_expected_rank(hits, expect):
    """Return (rank, hit) of the first hit whose source id is in expect, or (None, None)."""
    ids = source_ids(hits)
    for i, sid in enumerate(ids):
        if sid in expect:
            return i + 1, hits[i]
    return None, None


def main():
    golden = load_golden()
    queries = golden.get("queries", [])
    negatives = golden.get("negatives", [])
    if not queries:
        print("no queries in golden.json")
        sys.exit(0)

    recall_at_k = 0
    mrr_sum = 0.0
    false_drop = 0

    for q in queries:
        query = q["query"]
        expect = q["expect"]
        hits = search(query)
        ids = source_ids(hits)
        rank, hit = first_expected_rank(hits, expect)
        if rank:
            recall_at_k += 1
            mrr_sum += 1.0 / rank
            if is_dropped(hit):
                false_drop += 1
                print(
                    f"{query!r} -> rank={rank} ids={ids} "
                    f"FALSE DROP dist={hit.get('dist')} over {RELEVANCE_MAX_DIST}"
                )
            else:
                print(f"{query!r} -> rank={rank} ids={ids}")
        else:
            print(f"{query!r} -> rank={rank} ids={ids}")

    n = len(queries)
    print(f"\nRecall@{K}: {recall_at_k}/{n} = {recall_at_k / n:.2f}")
    print(f"MRR@{K}: {mrr_sum / n:.3f}")

    # Two-sided relevance pass: negatives are queries that should be suppressed by
    # a working relevance filter. A false_pass is a negative where at least one hit
    # survives the filter. With RELEVANCE_ENFORCE off by default this does not gate
    # the build; it reports the error rate a future threshold/mechanism must beat.
    false_pass = 0
    for neg in negatives:
        query = neg["query"]
        hits = search(query)
        surviving = [h for h in hits if not is_dropped(h)]
        if surviving:
            false_pass += 1
            ids = source_ids(surviving)
            print(
                f"[negative] {query!r} -> FALSE PASS ids={ids} "
                f"(survived {len(surviving)}/{len(hits)} hits)"
            )
        else:
            print(f"[negative] {query!r} -> suppressed ({len(hits)} hits dropped)")

    if negatives:
        print(f"\nRelevance filter (forced ON, max_dist={RELEVANCE_MAX_DIST}):")
        print(f"  false_drop: {false_drop}/{n} positives = {false_drop / n:.3f}")
        print(
            f"  false_pass: {false_pass}/{len(negatives)} negatives = {false_pass / len(negatives):.3f}"
        )

    # The gate's original exit condition is preserved and no new failing condition is added.
    # Not because these numbers cannot fail a build — is_dropped() ignores RELEVANCE_ENFORCE,
    # so false_drop is a real forced-ON measurement, not zero by construction — but because
    # nobody has yet characterised them on this fixture corpus. A bound picked in the same
    # commit that first measures the quantity encodes today's accident as tomorrow's contract,
    # which is exactly how the 0.514 ceiling this gate exists to judge came to be. Run it,
    # read the numbers over a few commits, then choose a bound in a contract of its own.
    if recall_at_k < n:
        print("eval gate: FAIL")
        sys.exit(1)
    print("eval gate: PASS")


if __name__ == "__main__":
    main()
