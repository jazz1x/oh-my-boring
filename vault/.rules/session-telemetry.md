# Session telemetry contract

Every distilled session note (`kind: session`) carries structured metadata so the system can answer "what did I rely on?" and "what kept breaking?" across time.

## Fields

| Field | Type | Purpose |
|-------|------|---------|
| `skills` | list[string] | Skills invoked during the session (e.g., `ohmyboring`, `pr-craft`, `writing-craft`). |
| `contracts` | list[string] | Contracts referenced or established (e.g., `ollama`, `lm-studio`, `vector`, `graph`, `briefing`, `docker`, `okf`). |
| `incidents` | list[string] | Failures, blockers, or repeated errors observed. Each incident should also surface as a `risk` or `blocked` claim. |

## Rules

- Do **not** leave placeholder values such as `-`, `none`, `n/a`, `unknown`.
- Empty lists are allowed when nothing applied.
- `incidents` are first-class signals: they feed the `resolution_quality` gate and the "recent failures" health check.
- `contracts` link session notes to the provider/layer contracts documented in `docs/runbooks/` and `vault/.rules/`.

## Consumption

- `drudge vault lint --strict` warns on placeholder values.
- `scripts/data-steward.py` reports skill/contract/incident frequency when run with `--telemetry`.
- `scripts/doctor.sh --strict` surfaces recent resolution-quality failures derived from incident claims.
- The graph stores `claims` extracted from incidents as `risk`/`blocked` nodes, so later recall can show "what was repeatedly blocked?"

## Example

```yaml
---
id: wiki-0531
title: GraphRAG multi-hop + reranker implementation
kind: session
origin: personal
date: 2026-07-09
skills: [ohmyboring]
contracts: [ollama, vector, graph, docker, briefing, okf]
incidents: [ollama transient unreachable during self-verify cycle 30]
claims:
  - subject: graphrag
    predicate: multi-hop-depth
    value: 2
    kind: fact
    confidence: certain
---
```
