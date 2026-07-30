# Code Graph Runbook

## Purpose

Index source code as AST-parsed symbols and relations so oh-my-boring can recall **code context** (functions, classes, imports, call graphs) alongside session memory. The code lane is deterministic: it uses tree-sitter, not an LLM, and stores results in the same `node`/`edge` graph used for wiki semantics.

## Preconditions

- `BORING_VECTOR=on` is set (the graph store requires Postgres).
- `make up` has brought up `boring-postgres` and `boring-drudge`.
- `make verify-llm` passes (embedding dimension must match `llm.embed_dim`).

## Configuration

Enable code indexing in `boring.json`:

```json
{
  "code_index": {
    "enabled": true,
    "languages": ["rust", "python", "typescript", "kotlin"],
    "max_symbols_per_file": 200,
    "exclude_paths": ["target", "node_modules", "__pycache__", ".git"]
  }
}
```

| Field | Meaning |
| --- | --- |
| `enabled` | Master switch. When false, `make code-index` and code-context recall are no-ops. |
| `languages` | Languages to parse. Currently supports `rust`, `python`, `typescript` (`.ts`/`.tsx`), and `kotlin` (`.kt`/`.kts`). |
| `max_symbols_per_file` | Cap on symbols extracted per file to keep the graph bounded. |
| `exclude_paths` | Repo-relative path prefixes that are skipped during indexing. |

## What gets indexed

- **Symbols**: functions, methods, classes, structs, enums, traits, modules, imports, constants, variables.
- **Relations**: `code_calls`, `code_imports`, `code_inherits`, `code_contains`, `code_uses`.
- **Node ids**: `code:<kind>:<source_path>:<name>` (e.g. `code:function:src/lib.rs:parse`).

Code symbols are **not** embedded into vectors; they are graph nodes only. The wiki text chunks remain the vector/FTS lane.

## Commands

```bash
make code-index        # full refresh: replace the code graph with the current tree-sitter walk
make code-hotspots     # repeated code queries from query_log — what the agent keeps forgetting
make eval-code         # code-lane behavioral gate (/code-search golden fixtures)
```

`make code-index` is a **full refresh**, not an incremental upsert: it wipes all `code:*`
nodes and code↔code edges, then re-inserts what the walk finds — stale symbols of
renamed or deleted files disappear instead of accumulating. Safeguards:

- An empty walk (wrong `--root`, misconfigured excludes) leaves the existing graph untouched.
- **Wrong-root guard**: if the walk shares *no* files with the indexed graph, the run is
  refused — replacing the whole graph with an unrelated tree is almost always a wrong
  `--root` (e.g. `data/eval/code-fixtures` instead of the repo root). Pass
  `drudge code-index --root <path> --force` for an intentional root change.
- Doc→code `code_uses` edges written by `remember_code` survive the wipe: symbol node ids
  are deterministic, so note edges re-attach to the re-created nodes. Edges whose symbols
  were renamed away are reclaimed at the end of the pass (`note_edges_gc` in the output).
  Caveat: refresh assumes one code graph per DB — indexing a second repo root into the
  same database replaces the first repo's graph (the guard above forces an explicit
  `--force` for that).

Related surfaces:

- `POST /code-search` — HTTP code-symbol search (`{"query", "max_symbols≤20"}` → `{hits: [{kind, name, source_path, signature}], notes: [{source_path, title, snippet, symbol_name, symbol_path}]}`). `notes` lists wiki notes linked to the matched symbols by `remember_code` (empty until that tool is used). Rejects with the vector-off contract when `BORING_VECTOR=off`.
- `remember_code` (MCP) — store a note linked to an AST symbol (`path`, `symbol`, `symbol_kind`). Use it when the user corrects or emphasizes how a specific symbol behaves — a convention, gotcha, or constraint they should not have to re-explain. The note gets `kind: code` + `code_symbols` frontmatter; the graph gains a `code_uses` edge from the note to the symbol node, which survives re-indexing (see above). Link-only semantics: it inserts a stub node only when the symbol is not indexed yet (`ON CONFLICT DO NOTHING`) — it never overwrites the parsed signature of an indexed symbol. Deduplication matches `remember`: a near-duplicate call is skipped, and a richer one rewrites the existing note in place — the rewrite merges `code_symbols` so the note keeps every symbol it was ever linked to.
- `hooks/code-recall.py` (Kimi UserPromptSubmit) — when a prompt looks like a coding question, injects a bounded "code map" of matching AST symbols plus a "code notes" section with any linked notes. Wired by `agent_wiring.py --install`; tunables: `CODE_RECALL_MAX_SYMBOLS` (default 5), `CODE_RECALL_TIMEOUT` (3s), `CODE_RECALL_RETRIES` (0).
- `/ask` — code-like queries (`is_code_query` gate: code keywords, snake_case/camelCase identifiers, `::` paths, `.rs`/`.py`) get a bounded code-symbol context prepended before synthesis.

## Verification

```bash
make guard       # structural gate (includes code_recall_core Python tests)
make eval-code   # behavioral gate: /code-search must find all golden fixture symbols
make code-hotspots
```

Expected result:

- `make code-index` completes without parse errors on the enabled repos.
- `node` table contains rows with `id LIKE 'code:%'` after indexing.
- `make eval-code` prints `code eval gate: PASS` (Recall@5 = 1.00 on the fixtures in `data/eval/code-fixtures/`).
- `make ask` returns code symbols when the query contains code-like tokens (`::`, `fn`, `import`, file paths, identifiers).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `make code-index` says "code indexing disabled" | Set `code_index.enabled: true` in `boring.json`. |
| `make code-index` prints a clap usage error via docker | The `boring-drudge` image is stale — `docker compose --profile vector build boring-drudge && docker compose --profile vector up -d boring-drudge`. |
| No code symbols in the graph | Verify `BORING_VECTOR=on` and re-run `make code-index`. |
| Parse errors on a specific file | Check that the file is valid Rust/Python and not excluded by `exclude_paths`. |
| Code recall is too noisy | Lower `CODE_RECALL_MAX_SYMBOLS` or add more `exclude_paths`. |
| `make eval-code` skips with "no eval fixtures" | Run `make code-index` — the fixtures in `data/eval/code-fixtures/` are indexed like any repo file. |
| `make eval` fails after enabling code index | The existing eval fixtures are unaffected; if they fail, check that code edges did not leak into semantic queries (they are filtered by `kind`). |

## Contract notes

- **Deterministic**: code graph edges come only from tree-sitter parses; no LLM extraction.
- **Bounded**: symbols per file are capped; code context injected into prompts is capped to a small number of symbols and characters.
- **Isolated**: code edge kinds are prefixed with `code_` so wiki semantic lanes can exclude them.
- **Rebuildable**: the code graph is derived from source files; every `make code-index` pass replaces it with the current walk (full refresh). Deleting the DB and re-running `make code-index` rebuilds it exactly.
- **Note edges survive**: `remember_code` doc→code edges are user data, not derived data — they are preserved across refreshes and garbage-collected only when their symbol has genuinely disappeared (`note_edges_gc`).
