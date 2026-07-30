# Frontmatter + Wikilink conventions — human-facing SSOT

> The machine-parsing SSOT for bots/tools is [`.rules/schema.yaml`](schema.yaml). This file must stay consistent with it.
>
> These conventions apply to the **compiled `vault/wiki/wiki-NNNN.md`** pages. The distill notes in `vault/raw/`
> are free-form markdown without frontmatter; `drudge vault compile` curates raw→wiki and generates the frontmatter.

---

## Required fields (required_frontmatter)

| Field | Type | Description |
|------|------|------|
| `id` | string | Page ID. Matches the filename stem. Pattern `wiki-NNNN[N]` |
| `title` | string | Page title. One line, clear |
| `kind` | enum | `note` \| `memory` \| `session` \| `decision` \| `code` |
| `origin` | enum | `personal` \| `company` |
| `date` | string | Creation date `YYYY-MM-DD` |

## OKF v0.1 compatibility fields

oh-my-boring wiki notes are also [OKF v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) concepts.

| Field | OKF status | Description |
|------|------|------|
| `type` | required | OKF concept type. Rendered from omb's `kind` (`note`/`memory`/`session`/`decision`/`code`). Legacy notes without `type` are tolerated during lint and can be made explicit with `drudge vault migrate-okf`. |
| `description` | recommended | One-line summary. Rendered from `summary` or the first body line. |
| `timestamp` | recommended | ISO-8601 UTC timestamp. Rendered from `date` as `<date>T00:00:00Z`. |
| `okf_version` | optional | OKF dialect version, e.g. `"0.1"`. |

Reserved OKF filenames (`index.md`, `log.md`) are skipped by `vault lint` because they are bundle metadata, not concept documents.

## Optional fields (optional)

| Field | Type | Description |
|------|------|------|
| `project` | string | Project slug. Powers `project_status`, per-project recall filters, and `repo/<slug>` tag inference. Prefer the repo/org slug (e.g., `ohmyboring`, `kb-rag-bot`). |
| `sources` | list[string] | Source file paths (prefix: `raw/`, `raw-witness/`, `meta/`, `.rules/`). `raw-witness/` entries must include `#sha256=...` even when the local witness bytes have been pruned; `vault lint` verifies the local bytes when the witness file is present. |
| `relates_to` | list[string] | List of related page IDs (`wiki-NNNN`) |
| `tags` | list[string] | Classification tags (Obsidian-safe: spaces/special chars → `-`. Includes `repo/<slug>` nested tags) |
| `superseded_by` | string | ID of the page that superseded this one (`wiki-NNNN`) |
| `summary` | string | One-line summary (recommended under 200 chars). Maps to OKF `description`. |

## Session metadata fields (distilled from transcripts)

| Field | Type | Description |
|------|------|------|
| `skills` | list[string] | Skills invoked during the session (e.g., `ohmyboring`, `pr-craft`, `writing-craft`). See `session-telemetry.md`. |
| `contracts` | list[string] | Contracts referenced or established (e.g., `ollama`, `lm-studio`, `graph`, `vector`, `briefing`, `docker`, `okf`). See `session-telemetry.md`. |
| `incidents` | list[string] | Failures, blockers, or repeated errors observed. Each incident is also surfaced as a `risk` claim. See `session-telemetry.md`. |

## Semantic fields (for recall & graph)

| Field | Type | Description |
|------|------|------|
| `tools` | list[string] | Concrete tools/commands used in this note |
| `concepts` | list[string] | Recurring ideas/axes |
| `claims` | list[{subject, predicate, value, kind, confidence}] | Durable facts/decisions/risks. Curated by the distillation agent; drudge stores them as temporal authority and graph nodes. |
| `code_symbols` | list[string] | AST symbol refs (`path:symbol`) linking the note into the code graph. Written by `remember_code` on `kind: code` notes; the `code_uses` edge survives `make code-index` refreshes and the note surfaces in `/code-search` results. |

### Claims

Claims are the most important field for later recall. Each claim is a `(subject, predicate, value, kind, confidence)` record.

- `subject`: project or component name (e.g., `kb-rag-bot`)
- `predicate`: property/decision axis (e.g., `model-interface`, `status`, `release-version`)
- `value`: concrete fact (e.g., `bedrock-converse`, `removed`, `0.1.3`)
- `kind`: one of `fact` (default), `decision`, `assumption`, `risk`, `blocked`, `goal`, `term`, `next`
- `confidence`: one of `certain` (default), `likely`, `assumption`, `outdated`

Aim for 3–5 claims per session-distilled note. Avoid vague values like "검토" or "확인" — they sound like next-steps, not facts.

Use `decision` for concrete choices, `risk` for open uncertainties, `blocked` for active obstacles, `goal` for committed targets, `term` for project-specific glossary entries (subject=term, value=definition), and `next` for concrete follow-up actions still pending after the session.

---

## ID rules

- Pattern: `^wiki-\d{4,5}$` (4–5 digits). Filename stem == frontmatter `id`.
- Monotonically increasing. Once assigned, an ID is never reused.
- On deletion: instead of deleting the file, tombstone it — empty the body and leave `superseded_by`.

## Wikilink conventions

- Body page references: `[[wiki-NNNN]]` (Obsidian standard).
- Cross-layer links (`[[raw/...]]`, `[[meta/...]]`) are forbidden — reference via the `sources:` field.
- A dangling `[[wiki-NNNN]]` (missing target) is an error in `vault lint`.
