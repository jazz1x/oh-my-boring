# GraphRAG & Vector Contract Runbook

## Purpose

This runbook explains when and how to enable the pgvector-backed GraphRAG mode, what guarantees the graph/vector contract provides, and how to verify it stays healthy.

## Preconditions

- `BORING_VECTOR=on` is set (env var or `boring.json` defaults).
- Postgres with pgvector is running (started by `make up` if you use Docker Compose).
- `llm.embed_model` and `llm.embed_dim` match a locally served embedding model.
- `make verify-llm` passes.

## Configuration

Enable vector mode in `boring.json`:

```json
{
  "vector": {
    "enabled": true
  },
  "llm": {
    "embed_model": "bge-m3",
    "embed_dim": 1024
  }
}
```

Or override at runtime:

```bash
BORING_VECTOR=on make up
```

When vector mode is on, `make sync` re-ingests `vault/wiki` into embeddings and graph edges. When it is off, the engine is wiki-first: `/ask`, `/search`, `recall`, and `context` read markdown directly, and graph/claim endpoints return an explicit error.

## Vector Contract

- `llm.embed_dim` must equal the actual dimension returned by `/v1/embeddings` for `llm.embed_model`.
- Changing the embedding model requires updating `llm.embed_dim` **and** running `make reset`, because the vector table shape changes.
- `make verify-llm` enforces this by calling `/v1/embeddings` and comparing the returned length to `llm.embed_dim`.
- The embedding model is the only model whose dimension is wired into storage. The synthesis model (`llm.model`) can be swapped freely.

## Graph Contract

- The graph is deterministic. `tool`, `concept`, and `claim` nodes come from agent-curated note frontmatter, not from an extra LLM extraction pass inside `drudge`.
- `relates_to` links are projected from:
  1. Claim continuity (same normalized `(subject, predicate)` axis).
  2. Exact tool/concept overlap.
  3. Corroborated semantic neighbors.
  4. A small same-project recency fallback.
- A per-source cap prevents hub notes from exploding into a dense mesh.
- When `remember` writes a note, its own `relates_to` projection is immediate. Neighbor backlinks are reconciled by the next `make sync` / full `project_links` pass, so recall is immediate while Obsidian links are eventually consistent.

## GraphRAG Implementation

When `BORING_VECTOR=on`, `/ask` runs local GraphRAG:

1. Takes the top vector + BM25 RRF hits.
2. Expands the graph neighborhood using shared `uses`/`about` tool/concept nodes, with a default **multi-hop traversal** depth of **2 document-to-document hops** (configurable via the `depth` field).
3. Applies a lightweight **graph reranker** to the candidate pool. The top vector hit is kept as the anchor; remaining candidates are rescored using shared graph nodes, shared claim axes, graph degree, and recency decay.
4. Pulls the top related documents into the synthesis prompt.

This rescues answers buried in vector noise without adding a second LLM extraction pass. The graph lane is observable: every `/ask` call writes `graph_context_chars` and `graph_source_count` into `query_log.meta`.

The GraphRAG content lane is stricter than general related context:

- It uses only shared tool/concept graph nodes for expansion.
- Claim-axis continuity stays in its own related/claim-authority lane, so status history does not masquerade as extra GraphRAG evidence.

`/search` keeps the raw vector + BM25 RRF ranking as the external recall contract and does **not** apply graph reranking, so `make eval-graphrag` can A/B the two paths fairly.

## What is NOT implemented yet

- **Neural graph reranker**: the current reranker is a deterministic feature-based mixer, not a trained graph neural network.
- **Arbitrary edge kinds for GraphRAG expansion**: only `uses` and `about` edges drive graph expansion. Project/topic edges (`in_project`, `tagged`) are stored but used for grouping and filtering, not as primary GraphRAG evidence.

If future `make eval-graphrag` runs show recall gaps that depth-2 cannot close, the schema already supports deeper k-hop recursive CTEs and can be migrated to a graph DB without changing the API contract.

## Verification

```bash
make verify-llm
make up
make sync
make eval
make eval-graphrag
make doctor
make readiness
```

Expected result:

- `make verify-llm` confirms the embedding dimension contract.
- `make sync` ingests `vault/wiki` into vectors and graph edges without errors.
- `make eval` passes the recall/answer quality floor on `data/eval/golden.json`.
- `make eval-graphrag` A/B compares `/search` (vector-only) vs `/ask` (vector + graph + claim + LLM) on `data/eval/graph-golden.json` and reports Recall@3 plus graph-only rescue count.
- `make doctor` shows the engine healthy and the vector/graph state current.
- `make readiness` is green before you rely on scheduled briefings.

## Observability

- Query telemetry: `query_log.meta` records `graph_context_chars` and `graph_source_count` for every `/ask` call.
- Events: ingest, sync, and eval emit structured events that mirror the Rust workflow graph contract.
- Logs: `make logs` shows engine logs; `make events` shows the event DB/spool.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Graph endpoints return "vector mode required" | Set `BORING_VECTOR=on` and restart. |
| `make sync` fails with embedding dimension error | `llm.embed_dim` does not match the model; update it and run `make reset`. |
| `make eval-graphrag` recall drops | Check that `vault/wiki` contains notes with `tools:` / `concepts:` frontmatter; GraphRAG depends on them. |
| `graph_source_count` is always 0 | The top vector hits share no tool/concept nodes with other notes; this is expected when memory is sparse. |
| `make readiness` reports stale newest note | Run `make sync` or check ingest workers; briefings should not be trusted until readiness is green. |
