# OKF v0.1 compatibility — oh-my-boring vault

[Open Knowledge Format (OKF) v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) is Google's vendor-neutral standard for packaging knowledge as plain Markdown files with YAML frontmatter. oh-my-boring's compiled `vault/wiki/` pages are **OKF-consumable** with a small, explicit mapping.

## Field mapping

| OKF field | omb field / derivation | Status |
|-----------|------------------------|--------|
| `type` (required) | `kind` (`note` \| `memory` \| `session` \| `decision` \| `code`) or explicit `type` | ✅ mapped; legacy notes without `type` warn as `okf-legacy-map` |
| `title` (recommended) | `title` | ✅ present |
| `description` (recommended) | `summary` or first body line | ✅ mapped |
| `resource` (recommended) | `sources[0]` when it is a URI; otherwise omitted | ⚠️ optional; `sources:` are evidence paths, not asset URIs |
| `tags` (recommended) | `tags` | ✅ present |
| `timestamp` (recommended) | `date` rendered as `<date>T00:00:00Z` | ✅ mapped |
| `okf_version` (optional) | explicit frontmatter only | ✅ tolerated |

omb adds producer-defined keys that OKF consumers must preserve:

- `id`, `origin`, `project`, `sources`, `relates_to`, `claims`, `tools`, `concepts`, `skills`, `contracts`, `incidents`

These are used for recall, graph edges, and session telemetry. They do not break OKF conformance.

## Link model

- OKF uses standard Markdown links (`[text](/path/to/concept.md)`).
- omb uses Obsidian wikilinks (`[[wiki-NNNN]]`) for personal-vault navigation.
- Wikilinks are validated by `drudge vault lint`. An OKF export can convert `[[wiki-NNNN]]` to bundle-relative Markdown links without information loss.

## Reserved filenames

OKF reserves `index.md` and `log.md` as bundle metadata. `drudge vault lint` skips these filenames in `vault/wiki/` because they are not concept documents.

## Bundle vs. vault

- An **OKF bundle** is a self-contained concept tree.
- **oh-my-boring vault** is a personal RAG corpus (`vault/wiki/`) plus raw evidence (`vault/raw/`, `data/raw-witness/`).

The wiki layer can be exported as an OKF bundle by:

1. Mapping `kind` → `type`.
2. Converting `[[wiki-NNNN]]` links to relative Markdown links.
3. Writing an `index.md` directory listing.
4. Writing a `log.md` update history (optional).

## Conformance check

```bash
drudge vault lint --strict
```

Expected behavior:

- Required omb fields (`id`, `title`, `kind`, `origin`, `date`) are errors if missing.
- Missing OKF `type` with a present `kind` is a warning (`okf-legacy-map`), not an error, so legacy notes remain consumable.
- Missing `description`/`summary` or `timestamp` is a warning.
- Unknown frontmatter keys are preserved, not rejected.

## Provider/contract context

The vault is one layer in a stack of contracts:

- **LLM provider contract** (`docs/runbooks/ollama.md`, `docs/runbooks/lmstudio.md`) — chat and embedding models must be reachable and dimension-matched.
- **Vector contract** (`docs/runbooks/graphrag.md` §Vector Contract) — `llm.embed_dim` must match the served embedding model; changing it requires `make reset`.
- **Graph contract** (`docs/runbooks/graphrag.md` §Graph Contract, `vault/.rules/graphrag.md`) — graph nodes/edges are deterministic, derived from frontmatter, and separate from vector retrieval.

OKF compatibility applies to the knowledge layer that the graph and vector pipelines consume.
