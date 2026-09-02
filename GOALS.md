<!-- derived-from: PRD v3 -->
# GOALS

Derived from `docs/PRD.md`. The PRD says what the product is; this file says which of its
requirements the current slice enforces, and with which gates. Every gate row carries the `[R#]`
it comes from — a gate that derives from no requirement does not belong here, which is how this
file stops becoming a defect log again.

## North Star

oh-my-boring should ingest useful local work memory without interrupting the user.
The write door stays gated, deterministic, and observable; the read door stays fast.

## Current Slice

**Injection-channel demand verdict** (2026-08-31 → 09-14 — the first window was reset for an instrumentation fault, PRD §8 D1; no threshold changed). Derived from **[R1]** and the PRD's
§2 contract. The instrumentation shipped; this slice spends the window collecting, and ships no
change to the channel being measured.

In scope:

- Run the uptake window to its end — treatment and control counts per session **[R1]**.
- Accumulate labels: the daily pass collects 24, and human `--audit` runs alongside **[R1]**.
- Two documents: remove the throttle falsehood from the Claude adapter README, and keep this
  file in step with the PRD **[R1]**.

Out of scope for this slice — and why:

- **Any change to injection frequency, budget, position, or ranking.** The window's sample is
  defined on the current channel; changing it mid-window voids the contract **[R1]**.
- **Distillation changes**, including R3's derived work. They change the notes the channel will
  inject, so they are not orthogonal to the measurement. Starts after 09-14 **[R3]**.
- Bulk vault mutation, renumbering, DB reset **[R4]**.

Work that is orthogonal to the channel — gate hardening, schema parity, doc corrections — stays
allowed. The discipline is "do not touch the thing being measured", not "do not build".

Requirements this slice carries no new work for, and why:

- **[R5]** boundaries (origin split, PII gate, injection fence) are in place and enforced by the
  existing gates; this slice guards against regression only. The `forget` path-traversal question
  is the one open item and it is the owner's to confirm.
- **[R6]** the read door is deliberately frozen: its parameters are the thing being measured.
  Changing budget, position, or throttle mid-window voids the §2 contract.

## Verification Contract [R4]

All numbers below are gate thresholds, not descriptive examples.

| Gate | Threshold | Failure Handling |
| --- | ---: | --- |
| `codex-status-strict` | 100% pass | Block stage transition; inspect worker, marker, queue, newest note. |
| `make readiness` | 100% pass | Block briefing/release stage; inspect doctor output. |
| `make quality` | 100% pass | Block PR/release stage; fix MCP/docs/quality drift. |
| `make guard` | 100% pass for scheduled guard runs | Block PR/release stage; fix compile/lint/test/root cause. |
| failed self-verify steps | 0 | Block next stage. |
| stale pending markers | 0 | Block readiness. |
| stale retry markers | 0 | Block readiness. |
| dead-letter markers | 0 | Block readiness. |
| recent resolution failures | 0 in doctor window | Block readiness. |
| sync-degraded collector event | allowed only if remember batch succeeded | Keep visible; does not block bootstrap/soak unless paired with failed batch. |

## Stage Contract [R4]

The live self-verification loop writes a TSV summary with one row per step.
`scripts/self-verify-contract.py` evaluates that summary.

| Stage | Required Cycles | Required Guard Runs | Required Steps | Transition |
| --- | ---: | ---: | --- | --- |
| `bootstrap` | 1 | 1 | `codex-status-strict`, `readiness`, `quality`, `recent-events`, `guard` | Pass -> `soak-2h` |
| `soak-2h` | 6 | 2 | every cycle has status/readiness/quality/events; cycle 1 and 6 include guard | Pass -> `day` |
| `day` | 72 | 13 | every cycle has status/readiness/quality/events; guard at cycle 1 and every 6 cycles | Pass -> release-candidate briefing confidence |

No stage may advance with a failed row in the evaluated summary.

## Graph Contract [R2]

The Rust workflow graph is the closed transition vocabulary.

| Metric | Value |
| --- | ---: |
| graph name | `memory_ingest` |
| nodes | 11 |
| edges | 16 |
| terminal nodes | 1 |
| entry | `session_discovered` |
| terminal | `readiness_projected` |

Required graph paths:

- happy path: `session_discovered -> transcript_prepared -> distill_requested -> resolution_verified -> remember_requested -> done_marked -> resolution_event_recorded -> readiness_projected`
- repair path: `resolution_verified --fail--> resolution_repaired --pass--> remember_requested`
- retry path: `resolution_repaired --fail--> retry_marked -> resolution_event_recorded -> readiness_projected`
- duplicate path: `remember_requested --duplicate--> done_marked`
- skip path: `transcript_prepared --skip--> skipped -> resolution_event_recorded -> readiness_projected`

## Operating Policy [R3]

- If a gate fails, do not add timeout/retry/null-check symptom treatment before writing the root cause.
- If the root cause is a contract mismatch, update the graph/test contract first.
- If the root cause is host state, keep it in Python/launchd/doctor and project it into workflow events.
- If the root cause is weak content, improve resolution/quality gates before increasing ingestion rate.
- If the root cause requires bulk vault mutation, propose candidates first and do not apply automatically.

## Current Live Loop

Retired 2026-08-26. Nothing in the PRD requires 72 self-verification cycles a day, and nobody
had read the result in weeks — enforcement of **[R3]** and **[R4]** is carried by readiness,
dead-letter, and the CI gates, which are checked. `make self-verify-check` still exists for
anyone who wants a stage report; it is no longer a standing expectation.
