# GraphRAG contract

This document defines the GraphRAG behavior in oh-my-boring: how the graph is built, how it is traversed, and how graph signals are mixed into retrieval.

## Graph model

The graph lives in Postgres alongside the vector store.

- **Document nodes**: `doc:<source_path>`
- **Entity nodes**:
  - `tool:<slug>` — explicit tools from frontmatter `tools`.
  - `concept:<slug>` — explicit concepts from frontmatter `concepts`.
  - `project:<name>` — derived from frontmatter `project`.
  - `topic:<tag>` — derived from frontmatter `tags`.
  - `claim:<subject>:<predicate>` — typed claim axis nodes.
- **Edges**:
  - `uses` — doc → tool
  - `about` — doc → concept
  - `in_project` — doc → project
  - `tagged` — doc → topic
  - `claims` — doc → claim axis

Graph edges are deterministic and rebuilt on every `sync`/`ingest`. They are **not** learned from corpus co-occurrence.

## Relation lanes

Retrieval uses two separate lanes so that one signal does not masquerade as another:

1. **Graph lane** — shared `uses`/`about` entities. Requires ≥2 shared entities to link.
2. **Claim lane** — shared `(subject, predicate)` claim axes. A single shared axis is enough because it is a strong temporal-continuity signal.

Project/topic edges (`in_project`, `tagged`) are stored in the graph but are **not** used as primary GraphRAG evidence. They are used for project grouping and origin boundaries.

## Multi-hop traversal

The graph lane supports configurable k-hop traversal.

- Default depth for `/ask` and `/weekly` related context: **2 hops**.
- The CLI `drudge graph "query" --depth N` and HTTP `/graph` accept a `depth` field.
- Depth is interpreted as document-to-document hops. Internally this is `2*k` edge traversals (doc → entity → doc → …).
- Traversal stays inside the source document's `origin` boundary and excludes eval fixtures / generated briefs.

## Graph reranker

`/ask` enables a lightweight graph reranker on top of the vector + BM25 RRF result.

- `/search` keeps the raw RRF ranking as the external accuracy contract.
- The top vector hit is kept as the anchor.
- Remaining candidates are rescored with graph signals:
  - shared `uses`/`about` nodes with the anchor,
  - shared `claims` axes with the anchor,
  - graph degree,
  - recency decay.
- Signals are min-max normalized and mixed with `alpha = 0.5`.

## Accuracy contract

`make eval-graphrag` enforces the A/B contract:

- Path A: `/search` (vector + BM25 RRF, no graph).
- Path B: `/ask` (vector + BM25 RRF + graph context + graph reranker).
- Gate: `ask_recall@3` must not regress `search_recall@3`.

Graph-only rescues are reported but are **not** required to be >0; recall non-regression is the hard gate.

## Configuration surface

| Surface | Default | Meaning |
|---------|---------|---------|
| `/ask` | rerank on, depth 2 | synthesis endpoint |
| `/search` | rerank off | external recall contract |
| `/graph` JSON `depth` | 2 | k-hop neighborhood of the top-1 vector hit |
| MCP `neighbors` tool `depth` | 2 | k-hop neighborhood |
| CLI `drudge graph --depth` | 2 | k-hop neighborhood |

## Tests

- `drudge/tests/store_integration.rs` — k-hop reachability and graph rerank feature detection.
- `drudge/src/retrieve.rs` — rerank scoring unit tests.
- `make eval-graphrag` — end-to-end non-regression gate.
