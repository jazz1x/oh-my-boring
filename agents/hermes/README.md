# hermes-agent adapter

hermes-agent connects to oh-my-boring over MCP and runs cron-driven automation.

## What runs automatically

| Script | Cron source | Purpose |
|---|---|---|
| `briefing.py` | `hermes_cron_jobs.morning-briefing` (optional) | Daily morning digest via `/brief`. |
| `weekly-briefing.py` | `hermes_cron_jobs.weekly-briefing` (default) | Monday 09:00 KST weekly digest via `/weekly`, covering the previous calendar week (Mon 00:00 KST – Sun 23:59 KST). |
| `ingest-worker.py` | `memory-ingest-worker` job (not config-driven) | Pops one un-ingested Claude Code session per tick and asks the `memory-ingest` skill to store it. |
| `codex-collect-sessions.py` | `codex-memory-ingest-worker` job (not config-driven) | Hermes-safe wrapper that runs the repo collector, pops one eligible Codex session per tick, harvests stable rollout transcripts, skips true subagents, and stores it through the same remember path. |

## Config-driven cron

`boring.json` controls managed cron jobs:

```json
{
  "hermes_cron_jobs": {
    "weekly-briefing": {
      "enabled": true,
      "schedule": "0 9 * * 1",
      "script": "weekly-briefing.py"
    },
    "morning-briefing": {
      "enabled": true,
      "schedule": "0 8 * * *",
      "script": "briefing.py"
    }
  }
}
```

- Jobs are synced into `~/.hermes/cron/jobs.json` when `agent_wiring.py` runs.
- Jobs not listed in `hermes_cron_jobs` are left untouched.
- `enabled: false` pauses the job.
- `memory-ingest-worker` and `codex-memory-ingest-worker` are managed infrastructure jobs, not `hermes_cron_jobs` entries. `make doctor` reports their health and Codex queue status.
- Ingest worker events carry `workflow=memory_ingest`, `workflow_node`, and
  `workflow_outcome` fields that mirror the Rust workflow graph contract.

## Slack delivery format

Hermes delivers cron script stdout through Slack `chat.postMessage` as plain `text` with `mrkdwn` enabled. Keep `briefing.py` and `weekly-briefing.py` output as Slack mrkdwn text, not Block Kit JSON, unless the Hermes Slack adapter grows a `blocks` path. Reference: Slack's [formatting message text](https://docs.slack.dev/messaging/formatting-message-text/), [Block Kit](https://docs.slack.dev/block-kit/), and [`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage) docs.

The briefing scripts use:

- JSON request body `{"since_hours": 24}` for `/brief` and `{"since_hours": <hours-since-last-monday-00:00-kst>}` for `/weekly`. The engine filters records by `updated_at`; the adapter only computes the window and renders the result.
- Slack-safe headings, priority-first grouped bullets (Blocked → Next → Stalled → Risks → Decisions → Done), compact source basenames, and no empty placeholders.
- A shared `slack_briefing.py` renderer that can emit either current Hermes-safe mrkdwn text or a Block Kit-style JSON payload.
- No eval fixture entries: `make eval` uses `eval-*.md` during the gate, then re-syncs after cleanup; the engine also excludes that internal namespace from recency/claim briefing surfaces.
- No generated-brief feedback: scheduler-written `daily-brief-*.md` files stay in `vault/wiki` as output artifacts, but the `daily-brief` tag keeps them out of readiness/health source-corpus checks, recall, duplicate candidates, and DB ingest.

## Briefing philosophy

The **engine synthesizes; the adapter renders**. The adapter (`briefing.py` / `weekly-briefing.py` / `slack_briefing.py`) gives the engine enough context and clear inference rules, then lets the LLM decide what matters. It does not second-guess the summary by hard item caps or forced truncation. The renderer only enforces Slack-safe formatting and collapses exact or fuzzy paraphrase duplicates across labels.

This separation matters for sustainability: the briefing contract lives in the engine (`drudge/src/ask.rs`), while delivery mechanics live here. If the output feels wrong, fix the engine prompt or the retrieval window first; only fix formatting or duplication here.

## LLM, vector, and graph contracts

- **LLM provider contract** — The engine talks to any OpenAI-compatible endpoint configured in `boring.json` (`llm` block): Ollama, LM Studio, vLLM, llama.cpp, or remote OpenAI. The adapter scripts never hardcode a provider. Provider-specific runbooks are in `docs/runbooks/ollama.md` and `docs/runbooks/lmstudio.md`; the common contract is chat + embeddings are separate services, and `llm.embed_dim` must match the embedding model's actual output dimension.
- **Vector contract** — With `BORING_VECTOR=on`, the engine stores chunk embeddings in pgvector and uses HNSW for similarity search. The briefing paths rely on the same recency-first retrieval as `/ask`. See `docs/runbooks/graphrag.md` for the full vector contract.
- **Graph contract / GraphRAG** — In vector mode, `/ask` and the briefing surfaces can use graph-linked context: shared tool/concept nodes and claim axes pull related documents into the synthesis prompt. The adapter does not generate graph edges; edges come from note `frontmatter` and `relates_to` projections. For multi-hop and graph reranker status, see the GraphRAG runbook.

Preview the exact Slack-bound message before a live briefing:

```bash
BORING_URL=http://127.0.0.1:7700 python3 agents/hermes/briefing.py
BORING_URL=http://127.0.0.1:7700 python3 agents/hermes/weekly-briefing.py
```

Preview the future Block Kit payload for Slack's Block Kit Builder or a `blocks`-aware adapter:

```bash
BORING_BRIEFING_FORMAT=blocks BORING_URL=http://127.0.0.1:7700 python3 agents/hermes/briefing.py
```

## Managed skills

`agents/hermes/skills/memory-ingest/` is copied to `~/.hermes/skills/memory-ingest/` on install. The skill tells hermes how to distill a session and call `ohmyboring/remember`, including extracting `next` and `blocked` claims for the `next_actions` register.

## Installation

Enable `hermes-agent` in `boring.json` and run `install.sh`:

```json
{ "id": "hermes-agent", "enabled": true, "adapter": "cron" }
```

`install.sh` / `agent_wiring.py` copies the canonical cron scripts from `agents/hermes/` into `~/.hermes/scripts/`:

- `briefing.py` → `~/.hermes/scripts/briefing.py`
- `weekly-briefing.py` → `~/.hermes/scripts/weekly-briefing.py`
- `slack_briefing.py` renderer → `~/.hermes/scripts/slack_briefing.py`
- `codex-collect-sessions.py` → `~/.hermes/scripts/codex-collect-sessions.py`

Hermes cron jobs reference only the script basename (e.g. `"script": "briefing.py"`), which resolves against `~/.hermes/scripts/`. The scripts use `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` so the renderer import works regardless of the cron working directory.

This also sets `agent.environment_hint` in `~/.hermes/config.yaml` to remind hermes to call `ohmyboring/context` first.
