#!/usr/bin/env python3
"""Contract tests for structural guardrails."""

import ast
import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = ROOT / ".gitignore"
GUARD = ROOT / "scripts" / "guard.sh"
EVAL_GATE = ROOT / "scripts" / "eval-gate.sh"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DOCTOR = ROOT / "scripts" / "doctor.sh"
TEST_DOCTOR = ROOT / "scripts" / "test_doctor.sh"
VERIFY_LLM = ROOT / "scripts" / "verify-llm.sh"
MAKEFILE = ROOT / "Makefile"
PRE_COMMIT = ROOT / ".pre-commit-config.yaml"
DOCKER_COMPOSE = ROOT / "docker-compose.yml"
CONFIG = ROOT / "drudge" / "src" / "config.rs"
ASK = ROOT / "drudge" / "src" / "ask.rs"
FRONTMATTER = ROOT / "drudge" / "src" / "frontmatter.rs"
INGEST = ROOT / "drudge" / "src" / "ingest.rs"
RETRIEVE = ROOT / "drudge" / "src" / "retrieve.rs"
WIKI_RECALL = ROOT / "drudge" / "src" / "wiki_recall.rs"
STORE = ROOT / "drudge" / "src" / "store.rs"
REDACT = ROOT / "drudge" / "src" / "redact.rs"
STORE_INTEGRATION = ROOT / "drudge" / "tests" / "store_integration.rs"
CONTEXT_INTEGRATION = ROOT / "drudge" / "tests" / "context_integration.rs"
SERVE = ROOT / "drudge" / "src" / "serve.rs"
SCHEDULER = ROOT / "drudge" / "src" / "serve" / "scheduler.rs"
HTTP = ROOT / "drudge" / "src" / "serve" / "http.rs"
MCP = ROOT / "drudge" / "src" / "serve" / "mcp.rs"
WORKFLOW_RS = ROOT / "drudge" / "src" / "workflow.rs"
WORKFLOW_DOC = ROOT / "drudge" / "WORKFLOW.md"
VAULT_AUDIT = ROOT / "drudge" / "src" / "vault" / "audit.rs"
VAULT_PROJECTION = ROOT / "drudge" / "src" / "vault" / "projection.rs"
README = ROOT / "README.md"
README_KO = ROOT / "README.ko.md"
README_JA = ROOT / "README.ja.md"
ENFORCEMENT = ROOT / "drudge" / "ENFORCEMENT.md"
CLAUDE = ROOT / "drudge" / "CLAUDE.md"
OHMYBORING_SKILL = ROOT / ".agents" / "skills" / "ohmyboring" / "SKILL.md"
AGENT_WIRING = ROOT / "agents" / "shared" / "agent_wiring.py"
TEST_AGENT_WIRING = ROOT / "agents" / "shared" / "test_agent_wiring.py"
HERMES_INGEST_WORKER = ROOT / "agents" / "hermes" / "ingest-worker.py"
TEST_INGEST_WORKER = ROOT / "agents" / "hermes" / "test_ingest_worker.py"
SCHEDULER_COLLECT = ROOT / "agents" / "schedulers" / "collect-sessions.py"
SCHEDULER_COLLECT_KIMI = ROOT / "agents" / "schedulers" / "collect-kimi-sessions.py"
TEST_SCHEDULER_COLLECTORS = ROOT / "agents" / "schedulers" / "test_collectors.py"
SLACK_BRIEFING = ROOT / "agents" / "hermes" / "slack_briefing.py"
DEDUP_WIKI = ROOT / "scripts" / "dedup-wiki.py"
DATA_STEWARD = ROOT / "scripts" / "data-steward.py"
TEST_DATA_STEWARD = ROOT / "scripts" / "test_data_steward.py"
VAULT_CLEANUP_GATE = ROOT / "scripts" / "vault-cleanup-gate.py"
TEST_VAULT_CLEANUP_GATE = ROOT / "scripts" / "test_vault_cleanup_gate.py"
SCHEDULE_MAINTENANCE = ROOT / "scripts" / "schedule-maintenance.sh"
BACKUP_DB = ROOT / "scripts" / "backup-db.sh"
TEST_BACKUP_DB = ROOT / "scripts" / "test_backup_db.sh"
RESTORE_DB = ROOT / "scripts" / "restore-db.sh"
TEST_RESTORE_DB = ROOT / "scripts" / "test_restore_db.sh"
PY_WORKFLOW_CONTRACT = ROOT / "agents" / "shared" / "workflow_contract.py"
PY_TEST_WORKFLOW_CONTRACT = ROOT / "agents" / "shared" / "test_workflow_contract.py"
DISTILL_CORE = ROOT / "agents" / "shared" / "distill_core.py"
TEST_DISTILL_CORE = ROOT / "agents" / "shared" / "test_distill_core.py"
TRANSCRIPT = ROOT / "agents" / "shared" / "transcript.py"
TEST_TRANSCRIPT = ROOT / "agents" / "shared" / "test_transcript.py"
OMB_ENV = ROOT / "agents" / "shared" / "omb_env.py"
TEST_OMB_ENV = ROOT / "agents" / "shared" / "test_omb_env.py"
BORING_CONFIG = ROOT / "agents" / "shared" / "boring_config.py"
TEST_BORING_CONFIG = ROOT / "agents" / "shared" / "test_boring_config.py"
RECALL_CORE = ROOT / "agents" / "shared" / "recall_core.py"
TEST_RECALL_CORE = ROOT / "agents" / "shared" / "test_recall_core.py"
EVENT_LOG = ROOT / "agents" / "shared" / "event_log.py"
TEST_EVENT_LOG = ROOT / "agents" / "shared" / "test_event_log.py"
TEST_MARKERS = ROOT / "agents" / "shared" / "test_markers.py"
CLAUDE_DISTILL = ROOT / "agents" / "claude-code" / "distill-session.py"
CLAUDE_SESSION_START = ROOT / "agents" / "claude-code" / "session-start-recall.py"
CLAUDE_TEST_HOOKS = ROOT / "agents" / "claude-code" / "test_hooks.py"
CODEX_DISTILL = ROOT / "agents" / "codex" / "distill-session.py"
CODEX_TEST = ROOT / "agents" / "codex" / "test_codex.py"
CODEX_COLLECT = ROOT / "agents" / "codex" / "collect-sessions.py"
KIMI_DISTILL = ROOT / "agents" / "kimi" / "distill-session.py"
KIMI_TEST = ROOT / "agents" / "kimi" / "test_kimi.py"
RETENTION = ROOT / "scripts" / "retention.py"
SELF_VERIFY_LOOP = ROOT / "scripts" / "self_verify_loop.py"
SELF_VERIFY_CYCLE = ROOT / "scripts" / "self-verify-cycle.py"
SELF_VERIFY_CONTRACT = ROOT / "scripts" / "self-verify-contract.py"
GRAPH_GOLDEN = ROOT / "data" / "eval" / "graph-golden.json"
RUN_GRAPH_EVAL = ROOT / "data" / "eval" / "run_graph_eval.py"
EVAL_GRAPHRAG_GATE = ROOT / "scripts" / "eval-graphrag-gate.sh"
VAULT_SCHEMA = ROOT / "vault" / ".rules" / "schema.yaml"
VAULT_FRONTMATTER = ROOT / "vault" / ".rules" / "frontmatter.md"


def test_guard_pycompile_uses_fd_file_listing():
    text = GUARD.read_text(encoding="utf-8")

    assert "fd --type f --extension py" in text
    assert "\nfind " not in text
    assert " xargs " not in text


def test_guard_runs_guard_contract_tests():
    text = GUARD.read_text(encoding="utf-8")

    assert text.count("python3 scripts/test_guard_contract.py") == 1
    assert text.count("python3 agents/shared/test_omb_env.py") == 1


def test_collector_env_policy_numbers_parse_at_boundary():
    omb_env_text = OMB_ENV.read_text(encoding="utf-8")
    omb_env_test_text = TEST_OMB_ENV.read_text(encoding="utf-8")
    scheduler_text = SCHEDULER_COLLECT.read_text(encoding="utf-8")
    scheduler_test_text = TEST_SCHEDULER_COLLECTORS.read_text(encoding="utf-8")
    collector_texts = {
        "codex": CODEX_COLLECT.read_text(encoding="utf-8"),
        "hermes": HERMES_INGEST_WORKER.read_text(encoding="utf-8"),
        "claude-scheduler": scheduler_text,
        "kimi-scheduler": SCHEDULER_COLLECT_KIMI.read_text(encoding="utf-8"),
    }

    assert "def env_positive_int" in omb_env_text
    assert "def env_non_negative_int" in omb_env_text
    assert "def env_positive_float" in omb_env_text
    assert "def env_non_negative_float" in omb_env_text
    assert "test_env_positive_int_rejects_zero_negative_and_invalid" in omb_env_test_text
    assert "test_env_non_negative_int_allows_zero_and_rejects_negative" in omb_env_test_text
    assert "test_env_positive_float_uses_fallback_chain_and_rejects_bad_second_value" in omb_env_test_text
    assert "test_env_non_negative_float_allows_zero_but_rejects_negative_and_non_finite" in omb_env_test_text

    for label, text in collector_texts.items():
        assert "int(os.environ.get(\"COLLECT_LIMIT\")" not in text, label
        assert "float(os.environ.get(\"COLLECT_WINDOW_HOURS\")" not in text, label
        assert "float(os.environ.get(\"COLLECT_MIN_KB\")" not in text, label
        assert "float(os.environ.get(\"COLLECT_PENDING_TTL\")" not in text, label
        assert "float(os.environ.get(\"INGEST_PENDING_TTL\")" not in text, label
        assert "omb_env.env_positive_float" in text, label

    assert 'omb_env.env_positive_int("COLLECT_LIMIT", 1)' in CODEX_COLLECT.read_text(encoding="utf-8")
    assert 'omb_env.env_positive_int("INGEST_WIKI_ATTEMPTS", 3)' in HERMES_INGEST_WORKER.read_text(encoding="utf-8")
    assert "omb_env.env_non_negative_float(\"COLLECT_MIN_KB\", 20.0)" in CODEX_COLLECT.read_text(encoding="utf-8")
    assert "omb_env.env_non_negative_float(\"COLLECT_MIN_KB\", 20.0)" in HERMES_INGEST_WORKER.read_text(encoding="utf-8")
    assert "except OSError:" in scheduler_text
    assert 'return float("inf")' in SCHEDULER_COLLECT_KIMI.read_text(encoding="utf-8")
    assert "test_claude_collector_skips_unstatable_session_candidate" in scheduler_test_text
    assert "test_kimi_collector_skips_unstatable_session_candidate" in scheduler_test_text


def test_distill_core_env_policy_numbers_parse_at_boundary():
    distill_text = DISTILL_CORE.read_text(encoding="utf-8")
    distill_test_text = TEST_DISTILL_CORE.read_text(encoding="utf-8")

    assert 'int(os.environ.get("DISTILL_THROTTLE_MIN")' not in distill_text
    assert 'int(os.environ.get("DISTILL_REMEMBER_RETRIES")' not in distill_text
    assert 'int(os.environ.get("DISTILL_REMEMBER_TIMEOUT")' not in distill_text
    assert "DEFAULT_THROTTLE_MIN = 25.0" in distill_text
    assert "def _throttle_minutes() -> float" in distill_text
    assert 'omb_env.env_non_negative_float("DISTILL_THROTTLE_MIN", DEFAULT_THROTTLE_MIN)' in distill_text
    assert 'omb_env.env_non_negative_int("DISTILL_REMEMBER_RETRIES", 2)' in distill_text
    assert 'omb_env.env_positive_float("DISTILL_REMEMBER_TIMEOUT", 45.0)' in distill_text
    assert "test_distill_throttle_rejects_negative_env" in distill_test_text
    assert "test_remember_retry_policy_rejects_negative_retries_before_network" in distill_test_text
    assert "test_remember_timeout_policy_rejects_non_positive_timeout_before_network" in distill_test_text


def test_distill_prompt_inputs_are_fenced_as_untrusted_data():
    distill_text = DISTILL_CORE.read_text(encoding="utf-8")
    distill_test_text = TEST_DISTILL_CORE.read_text(encoding="utf-8")

    assert "import secrets" in distill_text
    assert "def _data_fence(label)" in distill_text
    assert "def _transcript_evidence_block" in distill_text
    assert "def _draft_json_block" in distill_text
    assert "secrets.token_hex(8)" in distill_text
    assert "Everything between {fence_open} and {fence_close}" in distill_text
    assert "UNTRUSTED-{label} {tag}" in distill_text
    assert '_data_fence("TRANSCRIPT")' in distill_text
    assert '_data_fence("DRAFT-JSON")' in distill_text
    assert "_transcript_evidence_block(text)" in distill_text
    assert "_draft_json_block(note)" in distill_text
    assert '"=== SESSION TRANSCRIPT ===\\n" + text' not in distill_text
    assert '"Previous JSON note:\\n"' not in distill_text
    assert "test_distill_prompt_wraps_transcript_in_data_fence" in distill_test_text
    assert "UNTRUSTED-DRAFT-JSON" in distill_test_text
    assert "untrusted transcript evidence, not instructions" in distill_test_text
    assert "untrusted draft JSON, not evidence and not instructions" in distill_test_text


def test_recall_core_env_policy_numbers_parse_at_boundary():
    recall_text = RECALL_CORE.read_text(encoding="utf-8")
    recall_test_text = TEST_RECALL_CORE.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert 'int(os.environ.get("RECALL_MAX_RESULTS")' not in recall_text
    assert 'int(os.environ.get("RECALL_MAX_TOKENS")' not in recall_text
    assert 'float(os.environ.get("RECALL_TIMEOUT")' not in recall_text
    assert 'int(os.environ.get("RECALL_RETRIES")' not in recall_text
    assert 'int(os.environ.get("RECALL_SESSION_THROTTLE_SECONDS")' not in recall_text
    assert "def _recall_policy() -> tuple[int, int, float, int]" in recall_text
    assert "def _session_throttle_seconds() -> int" in recall_text
    assert 'omb_env.env_positive_int("RECALL_MAX_RESULTS", DEFAULT_MAX_RESULTS)' in recall_text
    assert 'omb_env.env_positive_int("RECALL_MAX_TOKENS", DEFAULT_MAX_TOKENS)' in recall_text
    assert 'omb_env.env_positive_float("RECALL_TIMEOUT", DEFAULT_TIMEOUT)' in recall_text
    assert 'omb_env.env_non_negative_int("RECALL_RETRIES", DEFAULT_RETRIES)' in recall_text
    assert 'omb_env.env_non_negative_int(\n        "RECALL_SESSION_THROTTLE_SECONDS",' in recall_text
    assert "test_recall_policy_rejects_invalid_context_budget" in recall_test_text
    assert "test_recall_policy_allows_zero_retries_and_zero_session_throttle" in recall_test_text
    assert "test_run_recall_reports_invalid_policy_before_search" in recall_test_text
    assert "def _prompt_meta_field" in recall_text
    assert "def _data_fence" in recall_text
    assert "secrets.token_hex(8)" in recall_text
    assert "Everything between {fence_open} and {fence_close}" in recall_text
    assert "«UNTRUSTED-DATA {tag}»" in recall_text
    assert 'src = _prompt_meta_field((h.get("source_path") or "").rsplit("/", 1)[-1])' in recall_text
    assert "test_run_recall_wraps_snippets_in_data_fence_and_collapses_metadata" in recall_test_text
    assert "context caps and timeout must be positive" in docs["README.md"]
    assert "context 상한과 timeout은 양수" in docs["README.ko.md"]
    assert "context 上限と timeout は正数" in docs["README.ja.md"]


def test_event_log_timeout_env_policy_parses_at_boundary():
    event_log_text = EVENT_LOG.read_text(encoding="utf-8")
    event_log_test_text = TEST_EVENT_LOG.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert 'float(os.environ.get("BORING_EVENT_SINK_TIMEOUT")' not in event_log_text
    assert 'int(os.environ.get("BORING_EVENT_RECENT_HOURS")' not in event_log_text
    assert "DEFAULT_EVENT_SINK_TIMEOUT = 0.5" in event_log_text
    assert "def _event_sink_timeout() -> float" in event_log_text
    assert "def _event_recent_hours() -> int" in event_log_text
    assert "def _self_verify_fields() -> dict[str, str]" in event_log_text
    assert "partial self-verify provenance" in event_log_text
    assert "BORING_SELF_VERIFY_CYCLE must be a positive integer" in event_log_text
    assert "def _parse_positive_int_arg(raw: str) -> int" in event_log_text
    assert "def _require_positive_int(value: int, label: str) -> int" in event_log_text
    assert 'limit = _require_positive_int(limit, "limit")' in event_log_text
    assert 'hours = _require_positive_int(hours, "hours")' in event_log_text
    assert "events = [_normalize_engine_event(entry) for entry in reversed(entries) if isinstance(entry, dict)]" in event_log_text
    assert event_log_text.count("return events[-limit:]") == 2
    assert 'parser.add_argument("--max", type=int' not in event_log_text
    assert 'parser.add_argument("--hours", type=int' not in event_log_text
    assert 'omb_env.env_positive_float("BORING_EVENT_SINK_TIMEOUT", DEFAULT_EVENT_SINK_TIMEOUT)' in event_log_text
    assert 'omb_env.env_positive_int("BORING_EVENT_RECENT_HOURS", DEFAULT_RECENT_HOURS)' in event_log_text
    assert "test_event_sink_timeout_rejects_invalid_env_before_network" in event_log_test_text
    assert "test_recent_resolution_failures_rejects_invalid_recent_hours_before_engine_read" in event_log_test_text
    assert "test_recent_resolution_failures_cli_reports_invalid_config_without_traceback" in event_log_test_text
    assert "test_try_append_event_reports_invalid_config_without_traceback" in event_log_test_text
    assert "test_record_cli_reports_invalid_config_without_traceback" in event_log_test_text
    assert "test_tail_cli_reports_invalid_config_without_traceback" in event_log_test_text
    assert "test_recent_resolution_failures_cli_rejects_non_positive_numeric_overrides_before_reads" in event_log_test_text
    assert "test_recent_events_rejects_non_positive_limit_before_reads" in event_log_test_text
    assert "test_recent_events_caps_engine_response_to_requested_limit" in event_log_test_text
    assert "test_recent_resolution_failures_rejects_non_positive_direct_limits_before_reads" in event_log_test_text
    assert "test_append_event_rejects_partial_self_verify_provenance_before_write" in event_log_test_text
    assert "test_append_event_rejects_invalid_self_verify_cycle_before_write" in event_log_test_text
    assert '[event-log] invalid config: {e}' in event_log_text
    assert "positive HTTP timeout" in docs["README.md"]
    assert "positive integer hour window" in docs["README.md"]
    assert "양수 HTTP timeout" in docs["README.ko.md"]
    assert "양의 정수 시간 창" in docs["README.ko.md"]
    assert "正数の HTTP timeout" in docs["README.ja.md"]
    assert "正の整数の時間窓" in docs["README.ja.md"]


def test_event_log_db_first_spool_fallback_contract_is_guarded():
    event_log_text = EVENT_LOG.read_text(encoding="utf-8")
    event_log_test_text = TEST_EVENT_LOG.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "Record local workflow events into the engine DB, with a file spool fallback" in event_log_text
    assert "stored_in_db = _try_store_in_engine(payload)" in event_log_text
    assert "spool_mode = _event_spool_mode(db_enabled)" in event_log_text
    assert 'return "db"' in event_log_text
    assert 'return "on_failure"' in event_log_text
    assert 'return "always"' in event_log_text
    assert 'raw in {"db", "spool", "both"}' in event_log_text
    assert 'legacy in {"0", "false", "no", "off"}' in event_log_text
    assert 'legacy in {"1", "true", "yes", "on"}' in event_log_text
    assert 'line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\\n"' in event_log_text
    assert "f.write(line)" in event_log_text
    assert "f.flush()" in event_log_text
    assert "os.fsync(f.fileno())" in event_log_text
    assert "test_append_event_stores_in_engine_by_default_without_spool" in event_log_test_text
    assert "test_append_event_spools_when_engine_store_fails" in event_log_test_text
    assert "test_append_event_can_use_spool_only_sink" in event_log_test_text
    assert "test_append_event_writes_fsynced_complete_ndjson_line" in event_log_test_text
    assert "test_recent_events_prefers_engine_over_spool" in event_log_test_text
    assert "`db` writes the engine DB first and spools only on failure" in docs["README.md"]
    assert "Events are stored in the local engine DB first" in docs["README.md"]
    assert "NDJSON file is a fallback spool for engine-down cases" in docs["README.md"]
    assert "fallback spool writes fsynced complete NDJSON lines" in docs["README.md"]
    assert "`make events` for the DB view with automatic fallback to the file spool" in docs["README.md"]
    assert "`db`는 엔진 DB에 먼저 쓰고 실패 시에만 스풀" in docs["README.ko.md"]
    assert "로컬 엔진 DB에 먼저 저장됩니다" in docs["README.ko.md"]
    assert "엔진이 내려간 경우의 fallback 스풀" in docs["README.ko.md"]
    assert "fallback 스풀은 완성된 NDJSON 한 줄을 fsync한 append로 기록" in docs["README.ko.md"]
    assert "`make events`는 DB를 먼저 조회하되 실패하면 파일 스풀" in docs["README.ko.md"]
    assert "`db` はエンジン DB に先に書き、失敗時だけスプール" in docs["README.ja.md"]
    assert "ローカルエンジン DB に保存されます" in docs["README.ja.md"]
    assert "エンジン停止時の fallback スプール" in docs["README.ja.md"]
    assert "fallback スプールは完成した NDJSON 1 行を fsync 付き append で記録" in docs["README.ja.md"]
    assert "`make events` は DB を先に読み、失敗時はファイルスプール" in docs["README.ja.md"]


def test_event_log_auxiliary_callers_use_safe_append_boundary():
    offenders = []
    allowed_direct = {EVENT_LOG, DISTILL_CORE}

    for path in (ROOT / "agents").rglob("*.py"):
        if path in allowed_direct or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        if "event_log.append_event(" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, (
        "event_log.append_event must stay behind try_append_event or the distill-core catch: "
        + ", ".join(offenders)
    )

    distill_text = DISTILL_CORE.read_text(encoding="utf-8")
    assert "event_log.append_event(" in distill_text
    assert "except (OSError, ValueError) as e" in distill_text


def test_hermes_cron_day_of_week_rejects_unsupported_values():
    wiring_text = AGENT_WIRING.read_text(encoding="utf-8")
    test_text = TEST_AGENT_WIRING.read_text(encoding="utf-8")

    assert "if cron_dow < 0 or cron_dow > 6:" in wiring_text
    assert 'raise ValueError(f"unsupported cron day-of-week: {part}")' in wiring_text
    assert "result.add((cron_dow + 6) % 7)" in wiring_text
    assert "test_next_cron_run_rejects_unsupported_day_of_week" in test_text
    assert '"0 9 * * 9"' in test_text


def test_make_guard_runs_full_structural_gate():
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"^guard:.*?(?=\n[^\t\n][^:\n]*:|\Z)", text, re.S | re.M)
    assert match, "Makefile guard target not found"
    body = match.group(0)

    assert "Full structural gate (Rust/Python/shell) + vault data hygiene dry-run" in body
    assert "./scripts/guard.sh" in body
    assert "scripts/data-steward.py" in body
    assert "data-steward dry-run" in body
    assert "7) data-steward dry-run" not in body


def test_vault_cleanup_danger_zone_stays_backup_first_and_guarded():
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    guard_text = GUARD.read_text(encoding="utf-8")
    steward_text = DATA_STEWARD.read_text(encoding="utf-8")
    steward_test_text = TEST_DATA_STEWARD.read_text(encoding="utf-8")
    gate_text = VAULT_CLEANUP_GATE.read_text(encoding="utf-8")
    gate_test_text = TEST_VAULT_CLEANUP_GATE.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    steward_fix = re.search(r"^steward-fix:.*?(?=\n[^\t\n][^:\n]*:|\Z)", makefile_text, re.S | re.M)
    cleanup_fix = re.search(r"^vault-cleanup-fix:.*?(?=\n[^\t\n][^:\n]*:|\Z)", makefile_text, re.S | re.M)
    assert steward_fix, "Makefile steward-fix target not found"
    assert cleanup_fix, "Makefile vault-cleanup-fix target not found"
    assert "scripts/vault-cleanup-gate.py --fix" in steward_fix.group(0)
    assert "scripts/data-steward.py --fix" not in steward_fix.group(0)
    assert "scripts/vault-cleanup-gate.py --fix" in cleanup_fix.group(0)
    assert "python3 scripts/test_data_steward.py" in guard_text
    assert "python3 scripts/test_vault_cleanup_gate.py" in guard_text

    assert 'parser.add_argument("--fix", action="store_true"' in steward_text
    assert 'parser.add_argument("--yes", action="store_true"' in steward_text
    assert 'ans = input("\\nApply fixes? [y/N] ")' in steward_text
    assert "bak = n[\"path\"].with_name(n[\"path\"].name + \".bak\")" in steward_text
    assert "def _write_text_atomic(path: Path, text: str) -> None" in steward_text
    assert "tempfile.NamedTemporaryFile" in steward_text
    assert "os.replace(tmp_path, path)" in steward_text
    assert "_write_text_atomic(n[\"path\"]," in steward_text
    assert "n[\"path\"].write_text" not in steward_text
    assert "yaml.safe_load(fm)" in steward_test_text
    assert "test_inline_empty_tags_stays_parseable" in steward_test_text
    assert "test_no_case_duplicate_repo_tag_and_keeps_real_tags" in steward_test_text
    assert "test_fix_uses_atomic_replace_without_temp_leftover" in steward_test_text
    assert "test_fix_preserves_original_when_atomic_replace_fails" in steward_test_text
    assert "atomic report without rewriting notes" in docs["README.md"]
    assert "fsynced tar backup of `vault/wiki`" in docs["README.md"]
    assert "안전한 원자적 steward 수정" in docs["README.ko.md"]
    assert "원자적 리포트 작성" in docs["README.ko.md"]
    assert "fsync tar 백업" in docs["README.ko.md"]
    assert "安全なアトミック steward 修正" in docs["README.ja.md"]
    assert "アトミック report" in docs["README.ja.md"]
    assert "fsync tar backup" in docs["README.ja.md"]

    assert "def _cleanup_temp(path: Path, label: str) -> None" in gate_text
    assert 'tarfile.open(tmp, "w:gz")' in gate_text
    assert "os.fsync(handle.fileno())" in gate_text
    assert "os.replace(tmp, backup)" in gate_text
    assert "backup = _create_backup(wiki_dir, Path(args.backup_dir).expanduser())" in gate_text
    assert "fixed = _apply_safe_fixes(before_notes, data_steward)" in gate_text
    assert gate_text.index("backup = _create_backup") < gate_text.index("fixed = _apply_safe_fixes")
    assert "def _write_text_atomic(path: Path, text: str) -> None" in gate_text
    assert "handle.flush()" in gate_text
    assert "os.replace(tmp, path)" in gate_text
    assert '_write_text_atomic(path, "\\n".join(lines) + "\\n")' in gate_text
    assert "path.write_text" not in gate_text
    assert "after_errors = _frontmatter_errors(wiki_dir)" in gate_text
    assert 'issues.extend(f"post-cleanup parse error: {e}" for e in after_errors)' in gate_text
    assert 'issues.append("backup archive missing or empty")' in gate_text
    assert 'return 0 if status == "ok" else 1' in gate_text
    assert "test_create_backup_publishes_complete_fsynced_archive_without_temp_leftover" in gate_test_text
    assert "test_create_backup_preserves_existing_backup_on_publish_failure" in gate_test_text
    assert "test_write_report_uses_atomic_replace_without_temp_leftover" in gate_test_text
    assert "test_write_report_preserves_existing_report_on_publish_failure" in gate_test_text
    assert "test_fix_creates_backup_and_clears_fixable_issues" in gate_test_text
    assert "test_check_fails_when_fixable_issues_remain" in gate_test_text


def test_unattended_maintenance_uses_backup_first_cleanup_and_fail_fast():
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    script_text = SCHEDULE_MAINTENANCE.read_text(encoding="utf-8")

    maintenance = re.search(r"^maintenance:.*?(?=\n[^\t\n][^:\n]*:|\Z)", makefile_text, re.S | re.M)
    assert maintenance, "Makefile maintenance target not found"
    assert "./scripts/schedule-maintenance.sh run" in maintenance.group(0)
    assert "backup-first vault cleanup" in maintenance.group(0)
    assert "retention" in maintenance.group(0)

    assert "set -eu" in script_text
    assert "backup-first vault cleanup" in script_text
    assert "python3 scripts/vault-cleanup-gate.py --fix --vault" in script_text
    assert "python3 scripts/data-steward.py --fix --yes" not in script_text
    assert "python3 scripts/retention.py --apply --yes" in script_text
    assert script_text.index("python3 scripts/vault-cleanup-gate.py --fix") < script_text.index(
        "python3 scripts/retention.py --apply --yes"
    )


def test_make_maintenance_is_documented_in_readme_command_tables():
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    for name, text in docs.items():
        assert "`make maintenance`" in text, f"{name} must document `make maintenance`"


def test_readme_command_table_make_targets_exist_in_makefile():
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    target_re = re.compile(r"^([a-zA-Z0-9_-]+):.*##", re.M)
    makefile_targets = {m.group(1) for m in target_re.finditer(makefile_text)}

    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    for name, text in docs.items():
        # Match both the Commands table and troubleshooting tables.
        table_targets = set(re.findall(r"\|\s*`make\s+([a-zA-Z0-9_-]+)`\s*\|", text))
        missing = table_targets - makefile_targets
        assert not missing, f"{name} references make targets missing from Makefile: {sorted(missing)}"


def test_backup_restore_db_destructive_shell_guards_are_wired():
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    guard_text = GUARD.read_text(encoding="utf-8")
    backup_text = BACKUP_DB.read_text(encoding="utf-8")
    backup_test_text = TEST_BACKUP_DB.read_text(encoding="utf-8")
    restore_text = RESTORE_DB.read_text(encoding="utf-8")
    restore_test_text = TEST_RESTORE_DB.read_text(encoding="utf-8")

    assert "sh scripts/test_backup_db.sh" in guard_text
    assert "sh scripts/test_restore_db.sh" in guard_text
    assert 'case "$KEEP" in' in backup_text
    assert "BORING_BACKUP_KEEP must be a positive integer" in backup_text
    assert "BORING_BACKUP_KEEP must be at least 1" in backup_text
    assert "tail -n +\"$((KEEP + 1))\"" in backup_text
    assert "BORING_BACKUP_KEEP=0 fails before pruning" in backup_test_text
    assert "BORING_BACKUP_KEEP rejects non-numeric values" in backup_test_text

    assert "$COMPOSE --profile vector stop boring-drudge" in restore_text
    assert "stop boring-drudge || true" not in restore_text
    assert "STUB_STOP_RC" in restore_test_text
    assert "stop failure aborts before dropdb" in restore_test_text

    reset = re.search(r"^reset:.*?(?=\n[^\t\n][^:\n]*:|\Z)", makefile_text, re.S | re.M)
    assert reset, "Makefile reset target not found"
    reset_text = reset.group(0)
    assert "This deletes ./data/pgdata (the vector DB). vault/ markdown is kept." in reset_text
    assert "Continue? [y/N]" in reset_text
    assert "rm -rf ./data/pgdata" in reset_text
    assert "DB reset — startup sync re-ingests after make up" in reset_text
    assert "rm -rf ./vault" not in reset_text


def test_direct_runner_uses_complete_globals_contract():
    text = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    main_calls: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        for stmt in node.body:
            call = stmt.value if isinstance(stmt, ast.Expr) else None
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id.startswith("test_")
            ):
                main_calls.add(call.func.id)

    assert "globals().items()" in text
    assert 'name.startswith("test_")' in text
    assert "callable(fn)" in text
    assert "fn()" in text
    assert not main_calls, f"direct runner must not keep a second manual list: {main_calls}"


def test_guarded_python_tests_have_complete_runners():
    guard_text = GUARD.read_text(encoding="utf-8")
    paths = [
        ROOT / match.group(1)
        for match in re.finditer(r"^python3 (\S+\.py)$", guard_text, re.MULTILINE)
    ]
    assert paths, "guard.sh does not run any Python tests"

    guarded = set(paths)
    expected = {
        path
        for root in (ROOT / "agents", ROOT / "scripts")
        for path in root.rglob("test_*.py")
    }
    missing_from_guard = sorted(str(path.relative_to(ROOT)) for path in expected - guarded)
    assert not missing_from_guard, f"guard.sh does not run Python tests: {missing_from_guard}"

    missing_by_path: dict[str, list[str]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        tests = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        }
        if not tests or _has_unittest_main(tree) or _has_globals_test_runner(text):
            continue

        runner_refs: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                runner_refs.update(_loaded_names(node))
            elif isinstance(node, ast.If):
                runner_refs.update(_loaded_names(node))
        missing = sorted(tests - runner_refs)
        if missing:
            missing_by_path[str(path.relative_to(ROOT))] = missing

    assert not missing_by_path, f"incomplete Python test runners: {missing_by_path}"


def test_guarded_shell_tests_are_listed():
    guard_text = GUARD.read_text(encoding="utf-8")
    paths = [
        ROOT / match.group(2)
        for match in re.finditer(r"^(sh|bash) (\S+test_\S+\.sh)$", guard_text, re.MULTILINE)
    ]
    assert paths, "guard.sh does not run any shell tests"

    guarded = set(paths)
    expected = {
        path
        for root in (ROOT / "agents", ROOT / "hooks", ROOT / "scripts", ROOT / "data" / "eval")
        for path in root.rglob("test_*.sh")
    }
    missing_from_guard = sorted(str(path.relative_to(ROOT)) for path in expected - guarded)
    assert not missing_from_guard, f"guard.sh does not run shell tests: {missing_from_guard}"


def test_boring_config_policy_loader_contract_is_guarded():
    guard_text = GUARD.read_text(encoding="utf-8")
    config_text = BORING_CONFIG.read_text(encoding="utf-8")
    test_text = TEST_BORING_CONFIG.read_text(encoding="utf-8")

    assert "python3 agents/shared/test_boring_config.py" in guard_text
    assert "return Path(__file__).resolve().parents[2]" in config_text
    assert "A corrupt config must not silently look like" in config_text
    assert "not valid JSON" in config_text
    assert "using empty policy" in config_text
    assert "Git identity (remote URL) is more stable" in config_text
    assert "test_repo_root_is_not_the_agents_dir" in test_text
    assert "test_discover_path_targets_repo_root" in test_text
    assert "test_load_warns_on_parse_error" in test_text
    assert "test_classify_prefers_remote_url_over_cwd" in test_text
    assert "test_classify_adversarial_inputs" in test_text
    assert "test_source_dirs_filter_by_adapter_and_agent" in test_text
    assert "test_canonical_repo_normalizes_variants" in test_text


def test_classify_repo_policy_write_contract_is_guarded():
    config_text = CONFIG.read_text(encoding="utf-8")
    mcp_text = MCP.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "fn mcp_classify_repo" in mcp_text
    assert ".parse::<config::Origin>()" in mcp_text
    assert "s.cfg_path" in mcp_text
    assert "config::upsert_repo_rule_at(match_, origin, name, &path)" in mcp_text
    assert "pub fn upsert_repo_rule_at" in config_text
    assert "serde_json::from_str(&txt)" in config_text
    assert "boring.json: repos[] is missing or not an array" in config_text
    assert "crate::vault::write_atomic(path, out)" in config_text
    assert "std::fs::write(path, out)" not in config_text
    assert "upsert_repo_rule_preserves_unknown_fields_and_replaces_match" in config_text
    assert "same-directory atomic write boundary" in docs["README.md"]
    assert "같은 디렉터리의 원자적 쓰기 경계" in docs["README.ko.md"]
    assert "同じディレクトリのアトミック書き込み境界" in docs["README.ja.md"]


def test_agent_wiring_install_contract_is_guarded():
    guard_text = GUARD.read_text(encoding="utf-8")
    wiring_text = AGENT_WIRING.read_text(encoding="utf-8")
    test_text = TEST_AGENT_WIRING.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "python3 agents/shared/test_agent_wiring.py" in guard_text
    assert "Backups are created as `.omb-bak`" in wiring_text
    assert "bak = Path(str(path) + \".omb-bak\")" in wiring_text
    assert "if path.exists() and not bak.exists():" in wiring_text
    assert "shutil.copy2(path, bak)" in wiring_text
    assert "except Exception as e:" in wiring_text
    assert "failed = True" in wiring_text
    assert "settings_path" in wiring_text
    assert "SessionStart" in wiring_text
    assert "test_install_reports_failure" in test_text
    assert "test_settings_path_override" in test_text
    assert "test_wire_claude_code_adds_session_start" in test_text
    assert "test_wire_claude_code_preserves_existing_settings_backup_once" in test_text
    assert "test_hermes_agent_calls_wire_hermes" in test_text
    assert "test_codex_calls_wire_codex" in test_text
    assert "Idempotently configures hooks/MCP for enabled agents" in docs["README.md"]
    assert "idempotent하게 구성" in docs["README.ko.md"]
    assert "idempotent に構成" in docs["README.ja.md"]


def _has_unittest_main(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "main"
            and isinstance(func.value, ast.Name)
            and func.value.id == "unittest"
        ):
            return True
    return False


def _has_globals_test_runner(text: str) -> bool:
    has_test_selector = 'startswith("test_")' in text or "startswith('test_')" in text
    return (
        "globals().items()" in text
        and has_test_selector
        and "callable(" in text
        and "fn()" in text
    )


def _loaded_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


def test_globals_runner_contract_requires_callable_dispatch():
    direct_runner = """
if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
"""
    collected_runner = """
def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
"""
    missing_callable = """
if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
"""
    missing_call = """
if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
"""

    assert _has_globals_test_runner(direct_runner)
    assert _has_globals_test_runner(collected_runner)
    assert not _has_globals_test_runner(missing_callable)
    assert not _has_globals_test_runner(missing_call)


def test_guard_event_logging_is_stack_free_by_default():
    text = GUARD.read_text(encoding="utf-8")

    assert 'BORING_EVENT_SINK="${BORING_EVENT_SINK:-spool}"' in text
    assert "oh-my-boring-guard-events.ndjson" in text
    assert "export BORING_EVENT_SINK BORING_EVENT_LOG" in text


def test_doctor_generated_brief_exclusion_uses_exact_tag():
    doctor_text = DOCTOR.read_text(encoding="utf-8")
    test_text = TEST_DOCTOR.read_text(encoding="utf-8")
    dedup_text = DEDUP_WIKI.read_text(encoding="utf-8")
    dedup_test_text = (ROOT / "scripts" / "test_dedup_wiki.py").read_text(encoding="utf-8")

    assert "--print-newest-source-note" in doctor_text
    assert "scripts/dedup-wiki.py" in doctor_text
    assert "frontmatter_tag_value_is_generated_brief" not in doctor_text
    assert "frontmatter_has_generated_brief_tag" not in doctor_text
    assert "def newest_source_note_path" in dedup_text
    assert "is_source_memory_candidate(parse_note(path))" in dedup_text
    assert "tags:*daily-brief*" not in doctor_text
    assert "*daily-brief*) return 0" not in doctor_text
    assert "test_newest_source_note_path_uses_source_memory_filter" in dedup_test_text
    assert "not-daily-brief" in test_text
    assert "similar tag as generated daily-brief output" in test_text


def test_doctor_compose_ps_failures_are_reported_not_hidden():
    doctor_text = DOCTOR.read_text(encoding="utf-8")
    test_text = TEST_DOCTOR.read_text(encoding="utf-8")

    assert "ps_rc=$?" in doctor_text
    assert "container status unavailable via" in doctor_text
    assert "no compose containers found" in doctor_text
    assert "DOCTOR_DOCKER_PS_FAIL" in test_text
    assert "hid docker compose ps failure as an empty container list" in test_text


def test_doctor_heal_is_safe_repair_not_destructive_recovery():
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    doctor_text = DOCTOR.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
        "ohmyboring/SKILL.md": OHMYBORING_SKILL.read_text(encoding="utf-8"),
    }

    heal = re.search(r"^heal:.*?(?=\n[^\t\n][^:\n]*:|\Z)", makefile_text, re.S | re.M)
    assert heal, "Makefile heal target not found"
    assert "./scripts/doctor.sh --fix" in heal.group(0)

    assert "# Safe auto-repair helpers. Dangerous ops (reset/restore) are intentionally absent." in doctor_text
    for helper in ("fix_env()", "fix_hooks()", "fix_engine()", "fix_ollama()", "fix_containers()"):
        assert helper in doctor_text
    assert "make down >/tmp/omb-doctor-fix-containers.log 2>&1 && make up" in doctor_text
    forbidden = ("make reset", "restore-db", "dropdb", "rm -rf ./data/pgdata", "rm -rf ./vault")
    for phrase in forbidden:
        assert phrase not in doctor_text

    assert "safe mechanical repairs only" in docs["README.md"]
    assert "never reset/restore" in docs["README.md"]
    assert "안전한 기계적 복구만" in docs["README.ko.md"]
    assert "reset/restore 없음" in docs["README.ko.md"]
    assert "安全な機械的修復だけ" in docs["README.ja.md"]
    assert "reset/restore はしない" in docs["README.ja.md"]
    assert "`make heal`" in docs["ohmyboring/SKILL.md"]
    assert "`reset`/`restore-db` 같은 파괴적 복구는 포함하지 않는다." in docs["ohmyboring/SKILL.md"]


def test_guard_documentation_names_full_structural_gate():
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
        "ENFORCEMENT.md": ENFORCEMENT.read_text(encoding="utf-8"),
        "CLAUDE.md": CLAUDE.read_text(encoding="utf-8"),
        ".pre-commit-config.yaml": PRE_COMMIT.read_text(encoding="utf-8"),
        "ohmyboring/SKILL.md": OHMYBORING_SKILL.read_text(encoding="utf-8"),
    }
    stale_phrases = [
        "fmt + clippy + test + Python py-compile",
        "`make guard` = `rustfmt --check` + `clippy -D warnings` + `cargo test`",
        "`cargo fmt --check` → `cargo clippy --all-targets -D warnings` → `cargo test`.",
        "Structural gate (fmt+clippy+test+py-compile+py-unit-tests)",
        "git config core.hooksPath .githooks",
        "== scripts/guard.sh == CI rust-gate",
    ]

    for label, text in docs.items():
        for phrase in stale_phrases:
            assert phrase not in text, f"{label} still describes a narrower guard"

    assert "Python compile/unit tests" in docs["README.md"]
    assert "shell guardrails" in docs["README.md"]
    assert "vault hygiene dry-run" in docs["README.md"]
    assert "temp-spooled guard events" in docs["README.md"]
    assert "Python 컴파일/단위 테스트" in docs["README.ko.md"]
    assert "셸 가드레일" in docs["README.ko.md"]
    assert "vault 정결도 dry-run" in docs["README.ko.md"]
    assert "임시 스풀 guard 이벤트" in docs["README.ko.md"]
    assert "Python コンパイル/単体テスト" in docs["README.ja.md"]
    assert "シェルのガードレール" in docs["README.ja.md"]
    assert "vault 衛生 dry-run" in docs["README.ja.md"]
    assert "一時スプールされた guard イベント" in docs["README.ja.md"]
    assert "Python compile/unit tests" in docs["ENFORCEMENT.md"]
    assert "shell guardrails" in docs["ENFORCEMENT.md"]
    assert "data-steward dry-run" in docs["ENFORCEMENT.md"]
    assert "pre-commit install" in docs["ENFORCEMENT.md"]
    assert "data-steward dry-run" in docs["CLAUDE.md"]
    assert "faster staged-file subset" in docs["CLAUDE.md"]
    assert "전체 구조 게이트는 `make guard`" in docs[".pre-commit-config.yaml"]
    assert "Rust/Python/shell 구조 게이트 + vault 정결도 dry-run" in docs["ohmyboring/SKILL.md"]


def test_readme_ci_job_inventory_matches_workflow():
    workflow_text = CI_WORKFLOW.read_text(encoding="utf-8")
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    jobs_text = workflow_text.split("\njobs:\n", 1)[1]
    jobs = set(re.findall(r"^  ([a-zA-Z0-9_-]+):$", jobs_text, re.MULTILINE))
    expected_jobs = {
        "rust-gate",
        "quality-gate",
        "gitleaks",
        "cargo-deny",
        "trivy",
        "compose-config",
        "docker-build",
        "eval-gate",
    }

    assert jobs == expected_jobs
    assert "run: ./scripts/guard.sh" in workflow_text
    assert "run: make quality" in workflow_text
    assert "run: ./scripts/eval-gate.sh" in workflow_text
    assert "docker compose config >/dev/null || docker-compose config >/dev/null" in workflow_text
    assert "run: make build" in workflow_text
    assert "cargo test --quiet quality_gate" in makefile_text
    assert "./scripts/eval-gate.sh" in makefile_text

    for label, text in docs.items():
        for job in sorted(jobs):
            assert f"`{job}`" in text, f"{label} misses CI job `{job}`"


def test_native_serve_bind_contract_matches_runtime_and_docs():
    serve_text = SERVE.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    loopback_cmd = (
        'BORING_VAULT_DIR="$PWD/../vault" '
        "BORING_HTTP_ADDR=127.0.0.1:7700 cargo run --release -- serve"
    )

    assert 'config::env_set("BORING_HTTP_ADDR")' in serve_text
    assert 'unwrap_or_else(|| "0.0.0.0:7700".to_owned())' in serve_text
    assert 'config::env_set("BORING_VAULT_DIR")' in serve_text

    for label, text in docs.items():
        assert loopback_cmd in text, f"{label} misses loopback native serve command"
        assert "BORING_HTTP_ADDR=127.0.0.1:7700" in text, f"{label} misses loopback override"
        assert "`0.0.0.0:7700`" in text, f"{label} misses default bind disclosure"
        assert "`BORING_VAULT_DIR`" in text, f"{label} misses native vault env requirement"


def test_memory_ingest_workflow_graph_stays_contract_not_orchestrator():
    rust_text = WORKFLOW_RS.read_text(encoding="utf-8")
    doc_text = WORKFLOW_DOC.read_text(encoding="utf-8")
    py_contract = PY_WORKFLOW_CONTRACT.read_text(encoding="utf-8")
    py_test = PY_TEST_WORKFLOW_CONTRACT.read_text(encoding="utf-8")
    guard_text = GUARD.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "This module is the Rust-side \"LangGraph\" contract" in rust_text
    assert "does not execute hooks, call LLMs" in rust_text
    assert "or replace the deterministic semantic graph" in rust_text
    assert "pub enum WorkflowNode" in rust_text
    assert "pub enum WorkflowOutcome" in rust_text
    assert "pub const MEMORY_INGEST_NODES" in rust_text
    assert "pub const MEMORY_INGEST_EDGES" in rust_text
    assert 'name: "memory_ingest"' in rust_text
    assert "assert_eq!(graph.nodes.len(), 11)" in rust_text
    assert "assert_eq!(graph.edges.len(), 16)" in rust_text
    assert "assert_eq!(graph.terminals.len(), 1)" in rust_text
    assert "resolution_failure_routes_to_repair_then_retry" in rust_text
    assert "remember_duplicate_is_a_done_marker" in rust_text

    for forbidden in (
        "std::process",
        "Command::",
        "std::fs",
        "fs::",
        "File::",
        "OpenOptions",
        "std::env",
        "reqwest",
        "ureq",
        "tokio",
        "mark_done",
        "mark_retry",
        "_call_llm",
        "distill_and_remember",
    ):
        assert forbidden not in rust_text, f"workflow.rs grew runtime behavior: {forbidden}"

    assert "It does not execute hooks, call an LLM, inspect launchd/cron, or read markers." in doc_text
    assert "the first slice is a contract, not a new orchestrator" in doc_text
    assert "graph shape stays at 11 nodes, 16 edges, and 1 terminal" in doc_text
    assert "Adapter events carry `workflow=memory_ingest`, `workflow_node`, and" in doc_text

    assert "The canonical graph shape lives in `drudge/src/workflow.rs`." in py_contract
    assert "without making them shell" in py_contract
    assert "out to the Rust binary during hooks" in py_contract
    assert "WORKFLOW_MEMORY_INGEST = \"memory_ingest\"" in py_contract
    assert "def resolution_fields" in py_contract
    assert "def readiness_fields" in py_contract
    assert "def collector_run_fields" in py_contract
    assert "def worker_fields" in py_contract
    for forbidden in ("subprocess", "requests", "urllib", "Path(", "read_text", "open("):
        assert forbidden not in py_contract, f"Python workflow vocabulary grew I/O: {forbidden}"

    assert "test_python_vocabulary_matches_rust_workflow_contract" in py_test
    assert "_rust_as_str_values" in py_test
    assert "test_unknown_workflow_projection_is_rejected" in py_test
    assert "python3 agents/shared/test_workflow_contract.py" in guard_text

    assert "not a second runtime orchestrator" in docs["README.md"]
    assert "두 번째 런타임 오케스트레이터는 아닙니다" in docs["README.ko.md"]
    assert "2 つ目のランタイムオーケストレーターではありません" in docs["README.ja.md"]


def test_self_verify_loop_contract_is_documented_and_guarded():
    loop = load_self_verify_loop_module()
    guard_text = GUARD.read_text(encoding="utf-8")
    make_text = MAKEFILE.read_text(encoding="utf-8")
    loop_text = SELF_VERIFY_LOOP.read_text(encoding="utf-8")
    cycle_text = SELF_VERIFY_CYCLE.read_text(encoding="utf-8")
    cycle_test_text = (ROOT / "scripts" / "test_self_verify_cycle.py").read_text(encoding="utf-8")
    contract_text = SELF_VERIFY_CONTRACT.read_text(encoding="utf-8")
    contract_test_text = (ROOT / "scripts" / "test_self_verify_contract.py").read_text(
        encoding="utf-8"
    )
    loop_test_text = (ROOT / "scripts" / "test_self_verify_loop.py").read_text(encoding="utf-8")
    self_verify_test_texts = {
        "scripts/test_self_verify_loop.py": loop_test_text,
        "scripts/test_self_verify_cycle.py": cycle_test_text,
        "scripts/test_self_verify_contract.py": contract_test_text,
    }
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert loop.REQUIRED_EVERY_CYCLE == (
        "codex-status-strict",
        "readiness",
        "quality",
        "recent-events",
    )
    assert loop.DEFAULT_STAGE == "bootstrap"
    assert loop.TERMINAL_STAGE == "release-candidate"
    assert loop.STAGE_CURSOR_NAME == "stage.txt"
    assert loop.GUARD_STEP == "guard"
    assert loop.EVENT_EMITTING_STEPS == ("codex-status-strict", "readiness", "guard")
    assert loop.EXPECTED_STEP_EVENTS == {
        "codex-status-strict": ("codex-collector", "collector_status"),
        "readiness": ("doctor", "readiness"),
        "guard": ("guard", "structural_guard"),
    }
    assert loop.expected_guard_cycles(12) == {1, 6, 12}
    assert loop.STAGES["bootstrap"]["min_cycles"] == 1
    assert loop.STAGES["bootstrap"]["min_guard_runs"] == 1
    assert loop.STAGES["bootstrap"]["next"] == "soak-2h"
    assert loop.STAGES["soak-2h"]["min_cycles"] == 6
    assert loop.STAGES["soak-2h"]["min_guard_runs"] == 2
    assert loop.STAGES["soak-2h"]["next"] == "day"
    assert loop.STAGES["day"]["min_cycles"] == 72
    assert loop.STAGES["day"]["min_guard_runs"] == 13
    assert loop.STAGES["day"]["next"] == "release-candidate"
    assert loop.next_stage("bootstrap", True) == "soak-2h"
    assert loop.next_stage("bootstrap", False) == "bootstrap"
    assert loop.SUMMARY_EMPTY == "empty"
    assert loop.SUMMARY_MALFORMED == "malformed"
    assert loop.SUMMARY_PRESENT == "present"
    assert loop.CYCLE_ROWS_DUPLICATE == "duplicate_step_rows"
    assert loop.CYCLE_ROWS_VALID == "valid"
    assert loop.summary_rows_state([]) == loop.SUMMARY_EMPTY
    assert loop.summary_rows_contract([]) == (loop.SUMMARY_EMPTY, [])
    assert loop.cycle_rows_state([]) == (loop.CYCLE_ROWS_EMPTY, "")
    assert "def next_stage" in loop_text
    assert "def stage_cursor_path" in loop_text
    assert "def read_stage_cursor" in loop_text
    assert "def write_stage_cursor" in loop_text
    assert "def _write_text_atomic" in loop_text
    assert "tempfile.NamedTemporaryFile" in loop_text
    assert "os.replace(tmp_path, path)" in loop_text
    assert "_write_text_atomic(stage_cursor_path(summary_path)" in loop_text
    assert "stage_cursor_path(summary_path).write_text" not in loop_text
    assert "def summary_rows_state" in loop_text
    assert "def summary_rows_contract" in loop_text
    assert "def cycle_rows_state" in loop_text
    assert "CYCLE_ROWS_DUPLICATE = \"duplicate_step_rows\"" in loop_text
    assert "len(actual_steps) != len(set(actual_steps))" in loop_text
    assert "return CYCLE_ROWS_DUPLICATE, \"\"" in loop_text
    assert "def next_stage" not in contract_text
    assert "def stage_cursor_path" not in contract_text
    assert "def read_stage_cursor" not in contract_text
    assert "def write_stage_cursor" not in contract_text
    assert "def summary_rows_state" not in cycle_text
    assert "def summary_rows_state" not in contract_text
    assert "def summary_rows_contract" not in cycle_text
    assert "def summary_rows_contract" not in contract_text
    assert "def cycle_rows_state" not in cycle_text
    assert "def cycle_rows_state" not in contract_text
    assert 'parser.add_argument("--cycle", type=_positive_int, default=None)' in cycle_text
    assert "def resolve_cycle_and_summary_path" in cycle_text
    assert "def resolve_cycle_for_summary" in cycle_text
    assert "expected_next_cycle(existing_rows)" in cycle_text
    assert "summary_rows_state," in cycle_text
    assert "SUMMARY_PRESENT," in cycle_text
    assert "CYCLE_ROWS_VALID," in cycle_text
    assert "VALID_STEPS," in cycle_text
    assert "summary_rows_contract," in contract_text
    assert "SUMMARY_PRESENT," in contract_text
    assert "TERMINAL_STAGE," in contract_text
    assert "VALID_CURSOR_STAGES," in contract_text
    assert "CYCLE_ROWS_VALID," in contract_text
    assert "EVENT_EMITTING_STEPS," in contract_text
    assert "EXPECTED_STEP_EVENTS," in contract_text
    assert "VALID_STATUSES," in contract_text
    assert "read_stage_cursor," in contract_text
    assert "write_stage_cursor," in contract_text
    assert "cycle_rows_state," in cycle_text
    assert "cycle_rows_state," in contract_text
    assert 'parser.add_argument("--stage", choices=sorted(VALID_CURSOR_STAGES), default=None)' in contract_text
    assert 'parser.add_argument("--no-write-cursor", action="store_true")' in contract_text
    assert "state = summary_rows_state(rows)" in cycle_text
    assert "state == SUMMARY_PRESENT" in cycle_text
    assert "summary_state, issues = summary_rows_contract(rows)" in contract_text
    assert "summary_state != SUMMARY_PRESENT" in contract_text
    assert "apply_step_log_contract(result, summary, rows, stage)" in contract_text
    assert "apply_event_log_contract(result, summary, stage)" in contract_text
    assert "missing_step_logs" in contract_text
    assert "empty_step_logs" in contract_text
    assert "malformed_step_logs" in contract_text
    assert "mismatched_step_logs" in contract_text
    assert "def parse_step_log_header" in contract_text
    assert "def parse_key_value_fields" in contract_text
    assert contract_text.count("parse_key_value_fields(parts[1:])") == 2
    assert contract_text.count('raise ValueError(f"malformed field {part}")') == 2
    assert contract_text.count('raise ValueError(f"duplicate field {key}")') == 1
    assert "test_step_log_header_rejects_malformed_field_token" in contract_test_text
    assert "test_step_log_footer_rejects_malformed_field_token" in contract_test_text
    assert "test_step_log_header_rejects_duplicate_field_key" in contract_test_text
    assert "test_step_log_footer_rejects_duplicate_field_key" in contract_test_text
    assert "step_log_header_mismatch" in contract_text
    assert "ROOT = Path(__file__).resolve().parents[1]" in contract_text
    assert 'for name in ("cycle", "step", "make_bin", "cwd", "event_log", "started_at")' in contract_text
    assert "if not fields.get(name)" in contract_text
    assert 'header["cwd"] != str(ROOT)' in contract_text
    assert 'parse_iso_datetime(fields["started_at"])' in contract_text
    assert "missing_event_log" in contract_text
    assert "empty_event_log" in contract_text
    assert "malformed_event_log" in contract_text
    assert "empty_event_records" in contract_text
    assert "missing_event_provenance" in contract_text
    assert "mismatched_event_provenance" in contract_text
    assert 'EVENT_EMITTING_STEPS = ("codex-status-strict", "readiness", GUARD_STEP)' in loop_text
    assert "EXPECTED_STEP_EVENTS = {" in loop_text
    assert '"codex-status-strict": ("codex-collector", "collector_status")' in loop_text
    assert '"readiness": ("doctor", "readiness")' in loop_text
    assert 'GUARD_STEP: ("guard", "structural_guard")' in loop_text
    assert "missing_step_events" in contract_text
    assert "matched_event_steps.add((cycle, step))" in contract_text
    assert "def event_shape_matches" in contract_text
    assert "if event_shape_matches(record, row):" in contract_text
    assert "def event_provenance_issues" in contract_text
    assert "def event_step_sort_key" in contract_text
    assert "parse_iso_datetime" in contract_text
    assert "ts_out_of_range" in contract_text
    assert "def read_event_log_records" in contract_text
    assert "json.loads(line)" in contract_text
    assert "evidence=failed_step_log" in contract_text
    assert "state != CYCLE_ROWS_VALID" in contract_text
    assert "row_contract_issues," not in contract_text
    assert "row_order_issues," not in contract_text
    assert 'issues.append(f"{summary_state}_summary")' in contract_text
    assert "next_stage," in contract_text
    assert '"next": next_stage(stage, status == "pass")' in contract_text
    assert "write_cursor = args.stage is None and not args.no_write_cursor" in contract_text
    assert 'write_stage_cursor(summary, result["next"])' in contract_text
    assert "def evaluate_terminal" in contract_text
    assert 'result = evaluate(rows, "day")' in contract_text
    assert 'result["next"] = TERMINAL_STAGE' in contract_text
    assert "test_main_uses_stage_cursor_and_advances_it" in contract_test_text
    assert "test_main_failure_keeps_stage_cursor_on_current_stage" in contract_test_text
    assert "test_terminal_stage_cursor_still_requires_day_threshold" in contract_test_text
    assert "test_stage_override_accepts_terminal_stage_without_cursor" in contract_test_text
    assert "test_stage_override_does_not_mutate_existing_cursor" in contract_test_text
    assert "test_stage_cursor_write_uses_atomic_replace_without_temp_leftover" in loop_test_text
    assert "test_stage_cursor_write_preserves_original_on_replace_failure" in loop_test_text
    assert "STEP_EXECUTION_FAILURE_EXIT_CODE = 127" in cycle_text
    assert "step failed before exit code" in cycle_text
    assert "exit_code = STEP_EXECUTION_FAILURE_EXIT_CODE" in cycle_text
    assert "test_run_cycle_records_runner_exception_and_continues" in cycle_test_text
    assert 'assert rows[1]["status"] == "failed"' in cycle_test_text
    assert 'assert rows[1]["exit_code"] == str(cycle.STEP_EXECUTION_FAILURE_EXIT_CODE)' in cycle_test_text
    assert "assert calls == cycle.steps_for_cycle(1)" in cycle_test_text
    assert "test_append_cycle_rows_rejects_duplicate_step_rows_under_lock" in cycle_test_text
    assert "duplicate_step_rows" in loop_test_text
    assert "duplicate_step_rows" in cycle_test_text
    assert "cycle 1 duplicate_step_rows" in contract_test_text
    assert "batch_status, cycle = cycle_rows_state(rows)" in cycle_text
    assert "def fsync_parent_dir" in loop_text
    assert "fsync_parent_dir(path)" in loop_text
    assert "fsync_parent_dir(summary_path)" in cycle_text
    assert "handle.flush()" in cycle_text
    assert "os.fsync(handle.fileno())" in cycle_text
    assert "tmp_path.replace(summary_path)" in cycle_text
    assert "tmp_path.unlink()" in cycle_text
    assert "test_stage_cursor_write_fsyncs_parent_directory_after_replace" in loop_test_text
    assert "test_run_cycle_fsyncs_summary_parent_directory_after_replace" in cycle_test_text
    assert "test_run_cycle_appends_existing_summary_atomically" in cycle_test_text
    assert "test_append_cycle_rows_preserves_summary_on_replace_failure" in cycle_test_text
    assert "state, _ = cycle_rows_state(cycle_rows)" in contract_text
    assert "python3 scripts/test_self_verify_loop.py" in guard_text
    assert "python3 scripts/test_self_verify_cycle.py" in guard_text
    assert "python3 scripts/test_self_verify_contract.py" in guard_text
    for path, test_text in self_verify_test_texts.items():
        assert "globals().items()" in test_text, f"{path} must discover all test functions"
        assert 'name.startswith("test_")' in test_text, f"{path} must select test_ functions"
        assert "callable(fn)" in test_text, f"{path} must skip non-callable globals"
    assert "self-verify-cycle:" in make_text
    assert "scripts/self-verify-cycle.py --cycle" in make_text
    assert 'CYCLE:-1' not in make_text
    assert "scripts/self-verify-cycle.py $${SUMMARY:+--summary" in make_text
    assert "log_dir = summary.parent / \"logs\"" in cycle_text
    assert "event_log = summary.parent / \"events.ndjson\"" in cycle_text
    assert "summary_path=summary" in cycle_text
    assert "def require_summary_path_for_event_log" in cycle_text
    assert "summary_path is required when event_log_path is set" in cycle_text
    assert "def self_verify_summary_path_text" in cycle_text
    assert "duplicate-free" in docs["README.md"]
    assert "Duplicate step rows are reported as `duplicate_step_rows`" in docs["README.md"]
    assert "중복 step 행이 없고" in docs["README.ko.md"]
    assert "중복 step 행은 불완전 cycle이 아니라 `duplicate_step_rows`" in docs["README.ko.md"]
    assert "重複 step 行がなく" in docs["README.ja.md"]
    assert "重複 step 行は不完全 cycle ではなく `duplicate_step_rows`" in docs["README.ja.md"]
    assert "summary_path must name a file" in cycle_text
    assert "def self_verify_event_log_path" in cycle_text
    assert "event_log_path must name a file" in cycle_text
    assert "def require_cycle_for_event_log" in cycle_text
    assert "cycle is required when event_log_path is set" in cycle_text
    assert "def require_cycle_for_log_dir" in cycle_text
    assert "cycle is required when log_dir is set" in cycle_text
    assert "def self_verify_cycle_text" in cycle_text
    assert "cycle must be a positive integer" in cycle_text
    assert "cycle = normalized_cycle(cycle)" in cycle_text
    assert 'return 1, "invalid_cycle"' in cycle_text
    assert "def require_valid_step" in cycle_text
    assert "unknown self-verify step" in cycle_text
    assert "cycle=cycle,\n        event_log_path=event_log_path" in cycle_text
    assert "event_log_path,\n            cycle,\n            summary_path=summary_path" in cycle_text
    assert 'child_env["BORING_EVENT_LOG"] = str(event_log)' in cycle_text
    assert 'child_env["BORING_EVENT_SINK"] = "spool"' in cycle_text
    assert 'child_env["BORING_SELF_VERIFY_EVENT_LOG"] = str(event_log)' in cycle_text
    assert 'child_env["BORING_SELF_VERIFY_SUMMARY"] = summary_path_text' in cycle_text
    assert 'child_env["BORING_SELF_VERIFY_CYCLE"] = cycle_text' in cycle_text
    assert 'child_env["BORING_SELF_VERIFY_STEP"] = step' in cycle_text
    assert "subprocess.Popen(" in cycle_text
    assert "env=child_env" in cycle_text
    assert "stderr=subprocess.STDOUT" in cycle_text
    assert "cycle-{int(self_verify_cycle_text(cycle)):04d}-{step}.log" in cycle_text
    assert "def write_step_log_header" in cycle_text
    assert "self_verify_step cycle=" in cycle_text
    assert "make_bin=" in cycle_text
    assert "event_log=" in cycle_text
    assert "started_at=" in cycle_text
    assert "handle.flush()" in cycle_text
    assert "def write_step_log_footer" in cycle_text
    assert "self_verify_step_complete cycle=" in cycle_text
    assert "normalize_exit_code(int(exit_code))" in cycle_text
    assert "write_step_log_footer(handle, step, cycle, STEP_EXECUTION_FAILURE_EXIT_CODE)\n            raise" in cycle_text
    assert "os.fsync(handle.fileno())" in cycle_text
    assert "test_run_make_step_footer_normalizes_signal_returncode" in cycle_test_text
    assert "exit_code=143" in cycle_test_text
    assert "test_run_make_step_fsyncs_completion_footer" in cycle_test_text
    assert "test_run_make_step_writes_failure_footer_when_process_spawn_fails" in cycle_test_text
    assert "missing make executable should fail" in cycle_test_text
    assert "def read_step_log_footer" in contract_text
    assert "def parse_step_log_footer" in contract_text
    assert "def step_log_footer_mismatch" in contract_text
    assert "incomplete_step_logs" in contract_text
    assert "test_main_blocks_stage_transition_when_step_log_footer_is_missing" in contract_test_text
    assert "test_main_blocks_stage_transition_when_step_log_footer_mismatches_row" in contract_test_text
    assert "self-verify-check:" in make_text
    assert "scripts/self-verify-contract.py --stage" in make_text
    assert "scripts/self-verify-contract.py $${SUMMARY:+--summary" in make_text

    for label, text in docs.items():
        assert "/private/tmp/omb-self-verify/<run>/summary.tsv" in text, f"{label} misses summary path"
        assert "/private/tmp/omb-self-verify/<run>/logs/" in text, f"{label} misses step log path"
        assert "/private/tmp/omb-self-verify/<run>/events.ndjson" in text, f"{label} misses event spool path"
        assert "/private/tmp/omb-self-verify/<run>/stage.txt" in text, f"{label} misses stage cursor path"
        assert "`stage.txt`" in text, f"{label} misses stage cursor filename"
        assert "`CYCLE`" in text, f"{label} misses cycle selection"
        assert "STAGE" in text, f"{label} misses stage override"
        for step in (*loop.REQUIRED_EVERY_CYCLE, loop.GUARD_STEP):
            assert f"`{step}`" in text, f"{label} misses self-verify step {step}"
        for stage in loop.STAGES:
            assert f"`{stage}`" in text, f"{label} misses self-verify stage {stage}"
    assert "`release-candidate`" in text, f"{label} misses terminal self-verify stage"
    assert "Summary appends are fsynced, atomically replaced, and followed by a parent-directory fsync" in docs["README.md"]
    assert "the stage cursor uses the same durable publish boundary" in docs["README.md"]
    assert "summary 추가는 fsync 후 원자적으로 교체되고 parent directory fsync까지 수행" in docs["README.ko.md"]
    assert "단계 cursor도 같은 durable publish 경계" in docs["README.ko.md"]
    assert "summary の追記は fsync 後にアトミック置換され、parent directory fsync まで行い" in docs["README.ja.md"]
    assert "段階カーソルも同じ durable publish 境界" in docs["README.ja.md"]
    assert "coverage for the event-emitting steps (`codex-status-strict`, `readiness`, and scheduled `guard`)" in docs["README.md"]
    assert "이벤트를 내는 step(`codex-status-strict`, `readiness`, 예정된 `guard`)을 모두 덮는 해석 가능한 비어 있지 않은 이벤트 스풀" in docs["README.ko.md"]
    assert "イベントを出す step（`codex-status-strict`、`readiness`、予定された `guard`）の網羅を持つ解析可能で空でないイベントスプール" in docs["README.ja.md"]

    assert "`bootstrap` = 1 cycle + 1 guard" in docs["README.md"]
    assert "`soak-2h` = 6 cycles + 2 guards" in docs["README.md"]
    assert "`day` = 72 cycles + 13 guards" in docs["README.md"]
    assert "`bootstrap` → `soak-2h` → `day` → `release-candidate`" in docs["README.md"]
    assert "`release-candidate` is terminal but not exempt" in docs["README.md"]
    assert "on failure, `next` remains the current stage" in docs["README.md"]
    assert "backed by matching non-empty step logs whose header timestamps fall inside the summary row windows" in docs["README.md"]
    assert "Child event records also carry self-verify summary, event-log, cycle, and step provenance" in docs["README.md"]
    assert "the producer rejects partial or non-positive self-verify provenance before writing" in docs["README.md"]
    assert "Each step log starts with unambiguous parseable `key=value` execution metadata, including the matching cycle, step, run-local event log path, and a header timestamp inside that summary row's time window" in docs["README.md"]
    assert "run-local event log path" in docs["README.md"]
    assert "failed steps print their log path as evidence" in docs["README.md"]
    assert "it ends with a fsynced, unambiguous parseable `key=value` completion footer carrying cycle, step, exit code, and end timestamp" in docs["README.md"]
    assert "completion footers match the summary rows" in docs["README.md"]
    assert "중복 없는 `key=value` 완료 footer를 fsync한 뒤 끝납니다" in docs["README.ko.md"]
    assert "완료 footer가 summary 행과 일치" in docs["README.ko.md"]
    assert "重複のない `key=value` completion footer を fsync して終わります" in docs["README.ja.md"]
    assert "completion footer が summary 行と一致" in docs["README.ja.md"]
    assert "appends the next contiguous cycle" in docs["README.md"]
    assert "CYCLE` overrides the auto-next contract" not in docs["README.md"]
    assert "With no `CYCLE` override" not in docs["README.md"]
    assert "[CYCLE=<override>]" not in make_text
    assert "`bootstrap` = 1 cycle + guard 1회" in docs["README.ko.md"]
    assert "`soak-2h` = 6 cycles + guard 2회" in docs["README.ko.md"]
    assert "`day` = 72 cycles + guard 13회" in docs["README.ko.md"]
    assert "`bootstrap` → `soak-2h` → `day` → `release-candidate`" in docs["README.ko.md"]
    assert "`release-candidate`는 종단 단계지만 예외는 아니며" in docs["README.ko.md"]
    assert "실패하면 `next`는 현재 단계로 남으며" in docs["README.ko.md"]
    assert "헤더 timestamp가 summary 행 시간창 안에 있고 완료 footer가 summary 행과 일치하는 비어 있지 않은 step 로그 증거" in docs["README.ko.md"]
    assert "하위 이벤트 레코드도 self-verify summary, 이벤트 로그, cycle, step provenance를 함께 담" in docs["README.ko.md"]
    assert "producer는 일부만 채워졌거나 양수 cycle이 아닌 self-verify provenance를 쓰기 전에 거부" in docs["README.ko.md"]
    assert "기본 DB-first 이벤트 sink에 의존하지 않고 하위 이벤트 쓰기를 이 run-local 스풀로 강제합니다" in docs["README.ko.md"]
    assert "각 step 로그는 일치하는 cycle, step, run-local 이벤트 로그 경로와 해당 summary 행의 시간창 안에 있는 헤더 timestamp를 포함한 중복 없는 `key=value` 실행 메타데이터로 시작하고" in docs["README.ko.md"]
    assert "실패 step은 로그 경로를 증거로 출력합니다" in docs["README.ko.md"]
    assert "다음 연속 cycle" in docs["README.ko.md"]
    assert "`CYCLE`을 주면 자동 다음 cycle 계약을 재지정" not in docs["README.ko.md"]
    assert "`CYCLE` 재지정이 없으면" not in docs["README.ko.md"]
    assert "`bootstrap` = cycle 1 回 + guard 1 回" in docs["README.ja.md"]
    assert "`soak-2h` = cycle 6 回 + guard 2 回" in docs["README.ja.md"]
    assert "`day` = cycle 72 回 + guard 13 回" in docs["README.ja.md"]
    assert "`bootstrap` → `soak-2h` → `day` → `release-candidate`" in docs["README.ja.md"]
    assert "`release-candidate` は終端段階ですが例外ではなく" in docs["README.ja.md"]
    assert "失敗すると `next` は現在の段階のままで" in docs["README.ja.md"]
    assert "ヘッダー timestamp が summary 行の時間枠内にあり completion footer が summary 行と一致する空でない step ログ証拠" in docs["README.ja.md"]
    assert "子イベントレコードも self-verify summary、イベントログ、cycle、step の provenance を持ち" in docs["README.ja.md"]
    assert "producer は一部だけ、または正の cycle でない self-verify provenance を書き込み前に拒否" in docs["README.ja.md"]
    assert "デフォルトの DB-first イベント sink に頼らず、子イベント書き込みをこの run-local スプールへ固定します" in docs["README.ja.md"]
    assert "各 step ログは、一致する cycle、step、run-local のイベントログパス、その summary 行の時間枠内にあるヘッダー timestamp を含む重複のない `key=value` 実行メタデータから始まり" in docs["README.ja.md"]
    assert "失敗 step はログパスを証拠として出力します" in docs["README.ja.md"]
    assert "次の連続 cycle" in docs["README.ja.md"]
    assert "`CYCLE` 指定で auto-next 契約を上書き" not in docs["README.ja.md"]


def test_readme_memory_contracts_document_core_axes():
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    ingest_text = INGEST.read_text(encoding="utf-8")
    retrieve_text = RETRIEVE.read_text(encoding="utf-8")
    wiki_recall_text = WIKI_RECALL.read_text(encoding="utf-8")
    config_text = CONFIG.read_text(encoding="utf-8")
    ask_text = ASK.read_text(encoding="utf-8")
    frontmatter_text = FRONTMATTER.read_text(encoding="utf-8")
    store_text = STORE.read_text(encoding="utf-8")
    store_test_text = STORE_INTEGRATION.read_text(encoding="utf-8")
    eval_gate_text = EVAL_GATE.read_text(encoding="utf-8")
    projection_text = VAULT_PROJECTION.read_text(encoding="utf-8")
    serve_text = SERVE.read_text(encoding="utf-8")
    http_text = HTTP.read_text(encoding="utf-8")
    mcp_text = MCP.read_text(encoding="utf-8")
    chunk_size, chunk_overlap = rust_default_chunker_values()
    max_context = rust_usize_const(ASK, "MAX_CONTEXT_CHARS")
    mcp_default_results = rust_usize_const(SERVE, "MCP_DEFAULT_RESULTS")
    mcp_default_tokens = rust_usize_const(SERVE, "MCP_DEFAULT_TOKENS")
    mcp_max_results = rust_usize_const(SERVE, "MCP_MAX_RESULTS")
    mcp_max_tokens = rust_usize_const(SERVE, "MCP_MAX_TOKENS")
    context_default_items = rust_usize_const(SERVE, "CONTEXT_DEFAULT_ITEMS")
    context_max_items = rust_usize_const(SERVE, "CONTEXT_MAX_ITEMS")
    related_seed_docs = rust_usize_const(ASK, "BRIEF_RELATED_SEED_DOCS")
    related_doc_limit = rust_usize_const(ASK, "BRIEF_RELATED_DOC_LIMIT")
    related_doc_chars = rust_usize_const(ASK, "BRIEF_RELATED_DOC_CHARS")
    weekly_brief_days = rust_i32_const(ASK, "WEEKLY_BRIEF_WINDOW_DAYS")
    project_status_days = rust_i32_const(ASK, "PROJECT_STATUS_WINDOW_DAYS")
    stalled_default_days = rust_u32_const(ASK, "STALLED_DEFAULT_OLDER_THAN_DAYS")

    for label, text in docs.items():
        assert "raw-witness/...#sha256=..." in text, f"{label} misses raw witness source pointer"
        assert "BORING_RAW_WITNESS_DIR" in text, f"{label} misses raw witness directory env"
        assert "BORING_RETENTION_RAW_WITNESS_DAYS" in text, f"{label} misses raw witness retention env"
        assert "max_results" in text, f"{label} misses recall result slicing"
        assert "max_tokens" in text, f"{label} misses recall token slicing"
        assert "project" in text, f"{label} misses project slicing"
        assert "since_hours" in text, f"{label} misses recency slicing"
        assert "`sha`" in text, f"{label} misses ingest sha contract"
        assert "`upsert`" in text, f"{label} misses ingest upsert contract"
        assert "`prune`" in text, f"{label} misses ingest prune contract"

    assert "Chunking" in docs["README.md"]
    assert f"{chunk_size:,}-character chunks" in docs["README.md"]
    assert f"{chunk_overlap:,}-character overlap" in docs["README.md"]
    assert f"{max_context:,}-character context ceiling" in docs["README.md"]
    assert "wiki-first recall tie-breaks equal scores by `source_path`" in docs["README.md"]
    assert "source lists name only hits that fit inside that capped prompt" in docs["README.md"]
    assert "single-project brief slices scope injected current/stalled claims to that project" in docs["README.md"]
    assert f"{related_seed_docs} seed docs" in docs["README.md"]
    assert f"{related_doc_limit} related docs" in docs["README.md"]
    assert f"{related_doc_chars:,} characters per related record" in docs["README.md"]
    assert "Raw witness" in docs["README.md"]
    assert "actual file path is authoritative for document/chunk `source_path`" in docs["README.md"]
    assert "identity fields (`origin`, `project`, `kind`) are trimmed at the parse boundary" in docs["README.md"]
    assert "Canonical `(subject, predicate)`" in docs["README.md"]
    assert "shared graph-node evidence" in docs["README.md"]
    assert "claim-axis evidence" in docs["README.md"]
    assert "shares N graph nodes: ..." in docs["README.md"]
    assert "shares N claim axes: ..." in docs["README.md"]
    assert "GraphRAG content lane stays stricter" in docs["README.md"]
    assert "shared tool/concept graph nodes" in docs["README.md"]
    assert "claim-axis continuity stays in its own related/claim-authority lane" in docs["README.md"]
    assert "capped so hub notes do not explode into a mesh" in docs["README.md"]
    assert "multiple seed records or both graph-node and claim-axis lanes" in docs["README.md"]
    assert "merges those seed paths and reasons" in docs["README.md"]
    assert "same-kind evidence nodes are deduplicated before the count is shown" in docs["README.md"]
    assert "stronger merged candidates are ranked before the related-document cap" in docs["README.md"]
    assert "exact ranking ties fall back to `source_path`" in docs["README.md"]
    assert "Briefing post-processing drops leaked relation-metadata bullets" in docs["README.md"]
    assert "briefing related context stays inside each seed record's project" in docs["README.md"]
    assert "raw witnesses are local evidence" in docs["README.md"]
    assert "Raw witness snapshots are fsynced before publish" in docs["README.md"]
    assert "원본 증거 스냅샷은 공개 전에 fsync" in docs["README.ko.md"]
    assert "原本証拠スナップショットは公開前に fsync" in docs["README.ja.md"]
    assert "total raw-witness footprint" in docs["README.md"]
    assert "average raw transcript bytes per day" in docs["README.md"]
    assert "missing bytes are a retention warning" in docs["README.md"]
    assert "without LLM synthesis; `make ask` / HTTP `/ask`" in docs["README.md"]
    assert "then run the local synthesis model" in docs["README.md"]
    assert "fast, no LLM. `make ask`" not in docs["README.md"]
    assert "Time-window arguments such as `since_hours` and `older_than_days` are nonnegative integers" in docs["README.md"]
    assert f'"max_items":{context_default_items}' in docs["README.md"]
    assert f"`max_items` (default {context_default_items})" in docs["README.md"]
    assert f"Last {weekly_brief_days} days across projects" in docs["README.md"]
    assert f"{project_status_days}-day project status" in docs["README.md"]
    assert f"`older_than_days` (default {stalled_default_days})" in docs["README.md"]
    assert "청킹" in docs["README.ko.md"]
    assert f"{chunk_size:,}자" in docs["README.ko.md"]
    assert f"{chunk_overlap:,}자" in docs["README.ko.md"]
    assert f"{max_context:,}자 고정 문맥 상한" in docs["README.ko.md"]
    assert "wiki-first recall은 같은 점수의 결과를 `source_path`로 결정화" in docs["README.ko.md"]
    assert "`ask` 출처 목록은 그 상한 안에 실제로 들어간 hit와 주입된 graph/claim 증거만" in docs["README.ko.md"]
    assert "단일 project 브리핑 조각은 주입되는 현재/정체 클레임을 그 project로 좁히고" in docs["README.ko.md"]
    assert f"seed 원천 노트 {related_seed_docs}개" in docs["README.ko.md"]
    assert f"관련 문서 {related_doc_limit}개" in docs["README.ko.md"]
    assert f"관련 기록당 {related_doc_chars:,}자" in docs["README.ko.md"]
    assert "원본 증거" in docs["README.ko.md"]
    assert "document/chunk `source_path`의 권위는 실제 파일 경로" in docs["README.ko.md"]
    assert "frontmatter 식별 필드(`origin`, `project`, `kind`)는 DB 필터나 relation lane에 들어가기 전에 parse boundary에서 공백이 정리" in docs["README.ko.md"]
    assert "정규화된 `(subject, predicate)`" in docs["README.ko.md"]
    assert "공유 그래프 노드 근거" in docs["README.ko.md"]
    assert "왜 이어졌는지 설명 가능한 항목" in docs["README.ko.md"]
    assert "GraphRAG 본문 문맥 경로는 더 엄격" in docs["README.ko.md"]
    assert "공유 도구/개념 그래프 노드만" in docs["README.ko.md"]
    assert "클레임 축 연속성은 별도 관련/클레임 권위 경로" in docs["README.ko.md"]
    assert "허브 노트가 과도한 그물망이 되지 않도록 상한" in docs["README.ko.md"]
    assert "여러 seed 기록이나 그래프 노드/클레임 축 양쪽" in docs["README.ko.md"]
    assert "제목에서 seed 경로와 근거를 합칩니다" in docs["README.ko.md"]
    assert "같은 종류의 근거 노드는 개수를 표시하기 전에 중복 제거" in docs["README.ko.md"]
    assert "병합 근거가 강한 후보가 관련 문서 상한 적용 전에 먼저 정렬" in docs["README.ko.md"]
    assert "완전 동점은 `source_path`로 결정화" in docs["README.ko.md"]
    assert "relation metadata bullet을 버려" in docs["README.ko.md"]
    assert "브리핑 관련 문맥은 호출자가 다른 project를 명시하지 않는 한 각 seed 기록의 project 안에 머뭅니다" in docs["README.ko.md"]
    assert "원본 증거는 로컬 증거" in docs["README.ko.md"]
    assert "전체 원본 증거 용량" in docs["README.ko.md"]
    assert "하루 평균 원본 트랜스크립트 바이트" in docs["README.ko.md"]
    assert "바이트 누락은 보존 경고" in docs["README.ko.md"]
    assert "LLM 합성 없이 `vault/wiki`" in docs["README.ko.md"]
    assert "로컬 합성 모델을 실행합니다" in docs["README.ko.md"]
    assert "빠르고 LLM 불필요. `make ask`" not in docs["README.ko.md"]
    assert "`since_hours`, `older_than_days` 같은 시간 창 값은 0 이상의 정수" in docs["README.ko.md"]
    assert f'"max_items":{context_default_items}' in docs["README.ko.md"]
    assert f"`max_items`(기본 {context_default_items})" in docs["README.ko.md"]
    assert f"최근 {weekly_brief_days}일 전체 프로젝트 브리핑" in docs["README.ko.md"]
    assert f"{project_status_days}일 프로젝트 상태" in docs["README.ko.md"]
    assert f"`older_than_days`(기본 {stalled_default_days})" in docs["README.ko.md"]
    assert "チャンク" in docs["README.ja.md"]
    assert f"{chunk_size:,} 文字" in docs["README.ja.md"]
    assert f"{chunk_overlap:,} 文字" in docs["README.ja.md"]
    assert f"{max_context:,} 文字の固定文脈上限" in docs["README.ja.md"]
    assert "wiki-first recall は同点結果を `source_path` で決定的に並べ" in docs["README.ja.md"]
    assert "`ask` の source list は、その上限内のプロンプトに実際に入った hit" in docs["README.ja.md"]
    assert "単一 project のブリーフィング slice では注入する現在/停滞クレームをその project に絞り" in docs["README.ja.md"]
    assert f"seed 元ノート {related_seed_docs} 件" in docs["README.ja.md"]
    assert f"関連文書 {related_doc_limit} 件" in docs["README.ja.md"]
    assert f"{related_doc_chars:,} 文字" in docs["README.ja.md"]
    assert "原本証拠" in docs["README.ja.md"]
    assert "document/chunk `source_path` の権威は実際のファイルパス" in docs["README.ja.md"]
    assert "frontmatter の識別フィールド（`origin`、`project`、`kind`）は DB filter や relation lane に入る前に parse boundary で空白を整え" in docs["README.ja.md"]
    assert "正規化済みの `(subject, predicate)`" in docs["README.ja.md"]
    assert "共有グラフノード根拠" in docs["README.ja.md"]
    assert "なぜつながったのか説明できる項目" in docs["README.ja.md"]
    assert "GraphRAG 本文文脈の経路はより厳密" in docs["README.ja.md"]
    assert "共有された道具/概念グラフノードだけ" in docs["README.ja.md"]
    assert "クレーム軸の連続性は別の関連/クレーム権威経路" in docs["README.ja.md"]
    assert "ハブノートが過剰な網目にならないよう上限" in docs["README.ja.md"]
    assert "複数の seed 記録" in docs["README.ja.md"]
    assert "見出しで seed 経路と理由を結合します" in docs["README.ja.md"]
    assert "同じ種類の根拠ノードは、件数を表示する前に重複除去" in docs["README.ja.md"]
    assert "統合根拠が強い候補は関連文書上限の適用前に先へ並び" in docs["README.ja.md"]
    assert "完全な同点は `source_path` で決定的に並べ" in docs["README.ja.md"]
    assert "relation metadata bullet を落とし" in docs["README.ja.md"]
    assert "ブリーフィング関連文脈は各 seed 記録の project 内に留まります" in docs["README.ja.md"]
    assert "原本証拠はローカルの証拠" in docs["README.ja.md"]
    assert "原本証拠の総容量" in docs["README.ja.md"]
    assert "1 日あたりの平均原本トランスクリプト bytes" in docs["README.ja.md"]
    assert "バイト列の欠落は保持警告" in docs["README.ja.md"]
    assert "LLM 合成なしで `vault/wiki`" in docs["README.ja.md"]
    assert "ローカル合成モデルを実行します" in docs["README.ja.md"]
    assert "高速、LLM 不要。`make ask`" not in docs["README.ja.md"]
    assert "`since_hours`、`older_than_days` などの時間窓の値は 0 以上の整数" in docs["README.ja.md"]
    assert f'"max_items":{context_default_items}' in docs["README.ja.md"]
    assert f"`max_items`（デフォルト {context_default_items}）" in docs["README.ja.md"]
    assert f"直近{weekly_brief_days}日間の全プロジェクトブリーフィング" in docs["README.ja.md"]
    assert f"{project_status_days}日間のプロジェクト状態" in docs["README.ja.md"]
    assert f"`older_than_days`（デフォルト {stalled_default_days}）" in docs["README.ja.md"]
    assert "size: 1500" in ingest_text
    assert "overlap: 200" in ingest_text
    assert "default_chunker_keeps_short_notes_whole" in ingest_text
    assert "default_chunker_preserves_configured_overlap" in ingest_text
    assert 'DefaultChunker::with_size(10, 3).chunk("short")' in ingest_text
    assert 'DefaultChunker::with_size(10, 3).chunk("abcdefghijklmnop")' in ingest_text
    assert "assert_eq!(&chunks[0][7..], &chunks[1][..3])" in ingest_text
    assert "C::default().chunk(body.trim())" in ingest_text
    assert "if prev.as_deref() == Some(sha.as_str())" in ingest_text
    assert "fn file_modified_time" in ingest_text
    assert 'with_context(|| format!("stat note mtime: {}", path.display()))' in ingest_text
    assert "file_modified_time_reports_missing_path_without_now_fallback" in ingest_text
    assert ".unwrap_or_else(SystemTime::now)" not in ingest_text
    assert "store.upsert_document(&front, &sha, mtime).await?" in ingest_text
    assert "missing document row for claim valid_from" in ingest_text
    assert "upsert_chunk(&Doc" in ingest_text
    assert "store.prune_chunks_from(path, pieces.len()).await?" in ingest_text
    assert "frontmatter::raw_has_generated_brief_tag" in ingest_text
    assert "store.delete_document(path).await?" in ingest_text
    assert "stats.deleted += 1" in ingest_text
    assert "pub const GENERATED_BRIEF_TAG" in frontmatter_text
    assert "path.clone_into(&mut self.source_path);" in frontmatter_text
    assert "self.origin = self.origin.trim().to_owned();" in frontmatter_text
    assert "self.project = self.project.trim().to_owned();" in frontmatter_text
    assert "self.kind = self.kind.trim().to_owned();" in frontmatter_text
    assert "parse_uses_ingest_path_as_source_path_even_when_yaml_has_stale_value" in frontmatter_text
    assert "parse_trims_identity_fields_before_storage" in frontmatter_text
    assert "parse_treats_whitespace_project_as_missing_and_derives_from_path" in frontmatter_text
    assert "tags.iter().any(|tag| tag.trim() == GENERATED_BRIEF_TAG)" in frontmatter_text
    assert "pub fn is_internal_eval_fixture_path" in frontmatter_text
    assert 'name.starts_with("eval-")' in frontmatter_text
    assert 'ext.eq_ignore_ascii_case("md")' in frontmatter_text
    assert "internal_eval_fixture_path_matches_store_boundary" in frontmatter_text
    assert "pub async fn retrieve_budget" in retrieve_text
    assert "fn trim_hits_to_budget" in retrieve_text
    assert "budget -= cut.chars().count();" in retrieve_text
    assert "budget = budget.saturating_sub(cut.chars().count())" not in retrieve_text
    assert "b.score.total_cmp(&a.score)" in retrieve_text
    assert "fused[&b.id].total_cmp(&fused[&a.id])" not in retrieve_text
    assert "partial_cmp(&a.score)" not in retrieve_text
    assert "max_chars / max_results" in retrieve_text
    assert "retrieve_budget_trims_each_hit_and_total_results" in retrieve_text
    assert "retrieve_budget_uses_character_counts_not_bytes" in retrieve_text
    assert "pub(crate) fn trim_hits_to_budget" in wiki_recall_text
    assert "budget -= cut.chars().count();" in wiki_recall_text
    assert "budget = budget.saturating_sub(cut.chars().count())" not in wiki_recall_text
    assert "b.score" in wiki_recall_text
    assert ".total_cmp(&a.score)" in wiki_recall_text
    assert ".then_with(|| a.source_path.cmp(&b.source_path))" in wiki_recall_text
    assert "partial_cmp(&a.score)" not in wiki_recall_text
    assert "wiki_recall_budget_trims_each_hit_and_total_results" in wiki_recall_text
    assert "wiki_recall_budget_uses_character_counts_not_bytes" in wiki_recall_text
    assert "search_tie_breaks_equal_scores_by_source_path" in wiki_recall_text
    assert "use crate::frontmatter::{is_internal_eval_fixture_path, raw_has_generated_brief_tag};" in wiki_recall_text
    assert "if is_internal_eval_fixture_path(&source_path)" in wiki_recall_text
    assert "if raw_has_generated_brief_tag(&content)" in wiki_recall_text
    assert "search_excludes_generated_briefs_and_eval_fixtures" in wiki_recall_text
    assert "health_corpus_count_excludes_generated_brief_and_eval_artifacts" in serve_text
    assert "if is_internal_eval_fixture_path(source_path.as_ref())" in serve_text
    assert "Generated daily briefs and eval fixtures are excluded from source-memory slices" in docs[
        "README.md"
    ]
    assert "eval fixtures are pruned or filtered away from briefing surfaces" in docs["README.md"]
    assert "test corpus entries do not appear in daily or weekly digests" in docs["README.md"]
    assert "생성된 daily brief와 eval fixture는 원천 메모리 조각에서 제외" in docs["README.ko.md"]
    assert "eval fixture는 브리핑 표면에서도 정리되거나 필터링됩니다" in docs["README.ko.md"]
    assert "일간/주간 브리핑에 섞이지 않습니다" in docs["README.ko.md"]
    assert "生成済み daily brief と eval fixture は元メモリのスライスから除外" in docs[
        "README.ja.md"
    ]
    assert "eval fixture はブリーフィング面からも除去または絞り込みされます" in docs["README.ja.md"]
    assert "日次/週次ブリーフィングには混ざりません" in docs["README.ja.md"]
    assert "const INTERNAL_EVAL_FIXTURE_RE" in store_text
    assert r'r"(^|/)eval-[^/]*\.md$"' in store_text
    assert "Internal eval fixtures must remain searchable while `make eval` is running" in store_text
    vector_search_body = rust_function_block(store_text, "vector_search")
    text_search_body = rust_function_block(store_text, "text_search")
    vector_filtered_body = rust_function_block(store_text, "vector_search_filtered")
    text_filtered_body = rust_function_block(store_text, "text_search_filtered")
    nearest_documents_body = rust_function_block(store_text, "nearest_documents")
    duplicate_boundary_body = rust_function_block(
        store_text, "nearest_documents_for_duplicate_boundary"
    )
    assert "Filtered `/search` retrieval intentionally leaves eval fixtures searchable" in store_text
    assert "cp data/eval/fixtures/eval-*.md vault/wiki/" in eval_gate_text
    assert "python3 data/eval/run_eval.py" in eval_gate_text
    assert "d.source_path !~ $4" in vector_search_body
    assert "&INTERNAL_EVAL_FIXTURE_RE" in vector_search_body
    assert "d.source_path !~ $4" in text_search_body
    assert "&INTERNAL_EVAL_FIXTURE_RE" in text_search_body
    assert "INTERNAL_EVAL_FIXTURE_RE" not in vector_filtered_body
    assert "INTERNAL_EVAL_FIXTURE_RE" not in text_filtered_body
    assert "d.source_path !~ $5" in nearest_documents_body
    assert "&INTERNAL_EVAL_FIXTURE_RE" in nearest_documents_body
    assert "d.source_path !~ $6" in duplicate_boundary_body
    assert "&INTERNAL_EVAL_FIXTURE_RE" in duplicate_boundary_body
    assert "assert_no_internal_eval_fixture" in store_test_text
    assert "internal eval fixtures must never feed generated relation context" in store_test_text
    assert "related_doc_content_respects_origin_project_and_internal_fixture_boundaries" in store_test_text
    assert "graph_entry_search_excludes_internal_eval_fixture_top_hits" in store_test_text
    assert "eval-nearest-candidate" in store_test_text
    assert "eval-duplicate-boundary" in store_test_text
    assert "const SECONDS_PER_HOUR: u64 = 3_600;" in wiki_recall_text
    assert "u64::from(h.max(0).unsigned_abs())" in wiki_recall_text
    assert "hours * SECONDS_PER_HOUR" in wiki_recall_text
    assert 'with_context(|| format!("stat wiki mtime: {}", path.display()))' in wiki_recall_text
    assert "mtime," in wiki_recall_text
    assert "refresh_drops_cached_note_when_path_becomes_unreadable" in wiki_recall_text
    assert "file_modified_time_reports_missing_wiki_path_without_now_fallback" in wiki_recall_text
    assert "mtime.unwrap_or_else(SystemTime::now)" not in wiki_recall_text
    assert "try_from(hours.saturating_mul(3600)).unwrap_or(0)" not in wiki_recall_text
    assert 'u32::try_from(raw_version).context("boring.json schema_version exceeds u32")?' in config_text
    assert ".map(|v| u32::try_from(v).unwrap_or(0))" not in config_text
    assert "schema_version_overflow_is_rejected_loudly" in config_text
    assert "fn db_i64_count_to_usize" in store_text
    assert "fn db_u64_rows_to_usize" in store_text
    assert "fn store_usize_to_i32" in store_text
    assert "fn store_usize_to_i64" in store_text
    assert "fn doc_path_from_node_id" in store_text
    assert "document node id missing doc: prefix" in store_text
    assert "doc_path_parser_rejects_missing_doc_prefix" in store_text
    assert "pub async fn doc_updated_at(&self, path: &str) -> Result<Option<SystemTime>>" in store_text
    assert "Ok(rows.first().map(|r| r.get::<_, SystemTime>(0)))" in store_text
    assert ".map_or_else(SystemTime::now" not in store_text
    assert "doc_updated_at_returns_none_for_missing_document" in store_test_text
    assert 'format!("{label} count cannot fit usize")' in store_text
    assert 'format!("{label} affected row count cannot fit usize")' in store_text
    assert 'format!("{label} cannot fit i32")' in store_text
    assert 'format!("{label} cannot fit i64")' in store_text
    assert "db_count_conversion_rejects_impossible_negative_count" in store_text
    assert "store_index_conversion_rejects_unrepresentable_chunk_index" in store_text
    assert "store_limit_conversion_preserves_requested_limit" in store_text
    assert "usize::try_from(n).unwrap_or(0)" not in store_text
    assert "usize::try_from(pruned).unwrap_or(0)" not in store_text
    assert "i32::try_from(from_idx).unwrap_or(i32::MAX)" not in store_text
    assert "i32::try_from(d.chunk_idx).unwrap_or(i32::MAX)" not in store_text
    assert "i64::try_from(k).unwrap_or(i64::MAX)" not in store_text
    assert 'strip_prefix("doc:").unwrap_or(&id)' not in store_text
    assert "project: Option<&str>" in retrieve_text
    assert "since_hours: Option<i32>" in retrieve_text
    assert '"max_results"' in mcp_text
    assert '"max_tokens"' in mcp_text
    assert "pub(crate) const MCP_DEFAULT_RESULTS" in serve_text
    assert "pub(crate) const MCP_DEFAULT_TOKENS" in serve_text
    assert "fn default_max_results() -> usize {\n    MCP_DEFAULT_RESULTS\n}" in serve_text
    assert "fn default_max_tokens() -> usize {\n    MCP_DEFAULT_TOKENS\n}" in serve_text
    assert f"pub(crate) const MCP_MAX_RESULTS: usize = {mcp_max_results};" in serve_text
    assert f"pub(crate) const MCP_MAX_TOKENS: usize = {mcp_max_tokens:,};".replace(",", "_") in serve_text
    assert "pub(crate) const CHARS_PER_TOKEN_ESTIMATE: usize = 4;" in serve_text
    assert "pub(crate) fn recall_max_chars" in serve_text
    assert "checked_mul(CHARS_PER_TOKEN_ESTIMATE)" in serve_text
    assert "recall_max_chars_preserves_token_budget_estimate" in serve_text
    assert "recall_max_chars_rejects_unrepresentable_budget" in serve_text
    assert "MCP_DEFAULT_RESULTS" in mcp_text
    assert "MCP_DEFAULT_TOKENS" in mcp_text
    assert "default {MCP_DEFAULT_RESULTS}, cap {MCP_MAX_RESULTS}" in mcp_text
    assert "default {MCP_DEFAULT_TOKENS}, cap {MCP_MAX_TOKENS}" in mcp_text
    assert "let since_hours_schema = json!" in mcp_text
    assert '"minimum": 0' in mcp_text
    assert "quality_gate_nonnegative_window_schemas_match_runtime_boundary" in mcp_text
    assert (
        'mcp_bounded_usize(args, "max_results", MCP_DEFAULT_RESULTS, MCP_MAX_RESULTS)'
        in mcp_text
    )
    assert (
        'mcp_bounded_usize(args, "max_tokens", MCP_DEFAULT_TOKENS, MCP_MAX_TOKENS)'
        in mcp_text
    )
    assert f"default {mcp_default_results}" not in mcp_text
    assert f"default {mcp_default_tokens}" not in mcp_text
    assert "pub(crate) const CONTEXT_DEFAULT_ITEMS" in serve_text
    assert "fn default_context_max_items() -> usize {\n    CONTEXT_DEFAULT_ITEMS\n}" in serve_text
    assert f"pub(crate) const CONTEXT_MAX_ITEMS: usize = {context_max_items};" in serve_text
    assert "CONTEXT_DEFAULT_ITEMS" in mcp_text
    assert "default {CONTEXT_DEFAULT_ITEMS}, max {CONTEXT_MAX_ITEMS}" in mcp_text
    assert 'mcp_bounded_usize(args, "max_items", CONTEXT_DEFAULT_ITEMS, CONTEXT_MAX_ITEMS)' in mcp_text
    assert "const MCP_EVENTS_DEFAULT_LIMIT" in mcp_text
    assert "const MCP_EVENTS_MAX_LIMIT" in mcp_text
    assert "pub(crate) const WEEKLY_BRIEF_WINDOW_DAYS" in ask_text
    assert "const WEEKLY_BRIEF_WINDOW_HOURS" in ask_text
    assert "unwrap_or(WEEKLY_BRIEF_WINDOW_HOURS)" in ask_text
    assert "last {WEEKLY_BRIEF_WINDOW_DAYS} days" in ask_text
    assert "pub(crate) const PROJECT_STATUS_WINDOW_DAYS" in ask_text
    assert "const PROJECT_STATUS_WINDOW_HOURS" in ask_text
    assert "Some(PROJECT_STATUS_WINDOW_HOURS)" in ask_text
    assert "last {PROJECT_STATUS_WINDOW_DAYS} days" in ask_text
    assert "pub(crate) const STALLED_DEFAULT_OLDER_THAN_DAYS" in ask_text
    assert "i64::from(STALLED_DEFAULT_OLDER_THAN_DAYS)" in ask_text
    assert "Stalled (>{STALLED_DEFAULT_OLDER_THAN_DAYS} days)" in ask_text
    assert "STALLED_DEFAULT_OLDER_THAN_DAYS" in http_text
    assert "WEEKLY_BRIEF_WINDOW_DAYS" in mcp_text
    assert "PROJECT_STATUS_WINDOW_DAYS" in mcp_text
    assert "Weekly recency-first briefing: last {WEEKLY_BRIEF_WINDOW_DAYS} days" in mcp_text
    assert (
        "Status summary for a single project over the last {PROJECT_STATUS_WINDOW_DAYS} days"
        in mcp_text
    )
    assert "format!(\"max events (default {MCP_EVENTS_DEFAULT_LIMIT}, cap {MCP_EVENTS_MAX_LIMIT})\")" in mcp_text
    assert "format!(\"threshold in days (default {STALLED_DEFAULT_OLDER_THAN_DAYS})\")" in mcp_text
    assert "quality_gate_briefing_window_schema_describes_actual_days" in mcp_text
    assert "quality_gate_recall_schema_describes_actual_budget_caps" in mcp_text
    assert "quality_gate_events_schema_describes_actual_limit_caps" in mcp_text
    assert "quality_gate_stalled_schema_describes_actual_default_days" in mcp_text
    assert "MCP_EVENTS_DEFAULT_LIMIT,\n        MCP_EVENTS_MAX_LIMIT" in mcp_text
    assert "unwrap_or(STALLED_DEFAULT_OLDER_THAN_DAYS)" in http_text
    assert "unwrap_or(STALLED_DEFAULT_OLDER_THAN_DAYS)" in mcp_text
    assert "unwrap_or(7)" not in http_text
    assert "cap 20" not in mcp_text
    assert "let max_results = req.max_results.clamp(1, MCP_MAX_RESULTS);" in http_text
    assert "let max_tokens = req.max_tokens.clamp(1, MCP_MAX_TOKENS);" in http_text
    assert "let max_chars = recall_max_chars(max_tokens)?;" in http_text
    assert "recall_max_chars(max_tokens)" in mcp_text
    assert "max_tokens.saturating_mul(4)" not in http_text
    assert "max_tokens.saturating_mul(4)" not in mcp_text
    assert "retrieve::retrieve_budget(" in http_text
    assert ".wiki_recall(" in http_text
    assert "wiki_recall::trim_hits_to_budget(" in http_text
    assert "wiki_recall::trim_hits_to_budget(" in mcp_text
    assert "ask::answer_wiki" in http_text
    assert "ask::answer_wiki" in mcp_text
    assert "fn remaining_context_chars(" in ask_text
    assert "MAX_CONTEXT_CHARS.saturating_sub(context.chars().count() + extra_context.chars().count())" in ask_text
    vector_answer_body = ask_text.split("pub async fn answer(", 1)[1].split(
        "/// wiki-first-class retrieval", 1
    )[0]
    wiki_answer_body = ask_text.split("pub async fn answer_wiki", 1)[1].split(
        "fn brief_system", 1
    )[0]
    assert "fn push_context_entry(" in ask_text
    assert "entry.chars().count() > remaining_context_chars(context, \"\")" in ask_text
    assert "context.len() + entry.len() > MAX_CONTEXT_CHARS" not in ask_text
    assert "push_unique_source(sources, source_path);" in ask_text
    assert "let mut hit_sources = Vec::new();" in vector_answer_body
    assert (
        "push_context_entry(&mut context, &mut hit_sources, &entry, &h.source_path)"
        in vector_answer_body
    )
    assert "let mut sources = hit_sources;" in vector_answer_body
    assert (
        "push_context_entry(&mut context, &mut sources, &entry, &h.source_path)"
        in wiki_answer_body
    )
    assert "hits.into_iter().map(|h| h.source_path).collect()" not in wiki_answer_body
    assert "context_sources_track_only_entries_that_fit_context_budget" in ask_text
    assert "context_budget_counts_characters_not_utf8_bytes" in ask_text
    assert 'i64::try_from(max_items).context("context max_items exceeds i64")?' in ask_text
    assert "unwrap_or(5)" not in ask_text
    graph_source_block = ask_text.split("let mut graph_sources = Vec::new();", 1)[
        1
    ].split("let claim_ctx = format_claim_records_for_prompt", 1)[0]
    assert "let related_source_path = rd.doc.source_path.clone();" in graph_source_block
    assert "let room = remaining_context_chars(&context, &graph_ctx);" in graph_source_block
    assert "let graph_entry = format!" in graph_source_block
    assert "if graph_entry.chars().count() > room" in graph_source_block
    assert "graph_ctx.push_str(&graph_entry);" in graph_source_block
    assert "push_unique_source(&mut graph_sources, &related_source_path);" in graph_source_block
    assert (
        graph_source_block.index("graph_ctx.push_str(&graph_entry);")
        < graph_source_block.index("push_unique_source(&mut graph_sources, &related_source_path);")
    )
    assert "docs.iter().take(BRIEF_RELATED_SEED_DOCS)" in ask_text
    assert "fn brief_single_project(docs: &[RecentDoc]) -> Option<&str>" in ask_text
    assert "let claim_project = brief_single_project(&docs);" in ask_text
    assert ask_text.count("let claim_project = brief_single_project(&docs);") == 2
    assert ask_text.count("claim_project,\n            Some(&[\"next\".to_owned(), \"blocked\".to_owned()]),") == 2
    assert ".recent_claim_records(12, claim_project, None, exclude_origins)" in ask_text
    assert "brief_single_project_scopes_claims_only_when_unambiguous" in ask_text
    assert "let doc_project = related_brief_seed_project(doc, project);" in ask_text
    assert ".related_doc_content(&doc.source_path, 2, exclude_origins, doc_project, Some(2))" in ask_text
    assert ".claim_related_doc_content(&doc.source_path, 2, exclude_origins, doc_project, Some(2))" in ask_text
    assert "related.retain(|relation| related_brief_doc_allowed(&relation.doc, &seen, doc_project));" in ask_text
    assert "sort_related_brief_candidates(&mut candidates);" in ask_text
    assert "sources.len() >= BRIEF_RELATED_DOC_LIMIT" in ask_text
    assert ".take(BRIEF_RELATED_DOC_CHARS)" in ask_text
    assert "related_brief_record_caps_snippet_chars" in ask_text
    assert 'rendered.matches(\'Z\').count(), BRIEF_RELATED_DOC_CHARS' in ask_text
    assert '!rendered.contains("TAIL")' in ask_text
    assert "fn merge_related_brief_candidates" in ask_text
    assert "fn sort_related_brief_candidates" in ask_text
    assert ".then_with(|| a.doc.source_path.cmp(&b.doc.source_path))" in ask_text
    assert "std::cmp::Reverse(brief_related_candidate_rank(candidate))" not in ask_text
    assert "fn brief_related_candidate_rank" in ask_text
    assert "(candidate.seed_paths.len(), candidate.evidence.len(), shared)" in ask_text
    assert "fn format_related_evidences" in ask_text
    assert "fn merge_related_evidence" in ask_text
    assert "fn normalize_related_evidence" in ask_text
    assert "normalize_related_evidence(&mut evidence)" in ask_text
    assert "normalize_related_evidence(&mut next)" in ask_text
    assert "fn normalize_related_nodes" in ask_text
    assert "fn related_evidence_node_count" in ask_text
    assert 'context("related evidence node count cannot fit i64")' in ask_text
    assert "related_evidence_count_rejects_unrepresentable_node_count" in ask_text
    assert "const RELATED_EVIDENCE_LABEL_LIMIT: usize = 4;" in ask_text
    assert "let mut labels = evidence.shared_nodes.clone();" in ask_text
    assert "let display_count = labels.len();" in ask_text
    assert "take(RELATED_EVIDENCE_LABEL_LIMIT)" in ask_text
    assert "display_count - RELATED_EVIDENCE_LABEL_LIMIT" in ask_text
    assert "related_evidence_summary_dedupes_display_nodes_and_count" in ask_text
    assert "related_evidence_summary_marks_omitted_display_nodes" in ask_text
    assert "labels.into_iter().take(4)" not in ask_text
    assert "i64::try_from(evidence.shared_nodes.len()).unwrap_or(i64::MAX)" not in ask_text
    assert "i64::try_from(existing.shared_nodes.len()).unwrap_or(i64::MAX)" not in ask_text
    assert "seed_paths: Vec<String>" in ask_text
    assert "format_related_seed_paths(&related.seed_paths)" in ask_text
    assert "fn format_related_seed_paths" in ask_text
    assert "normalize_related_nodes(&mut paths);" in ask_text
    assert "related_brief_seed_paths_are_collapsed_and_deduped_for_heading" in ask_text
    assert "fn related_brief_seed_project" in ask_text
    assert "related_brief_seed_project(&base, None), Some(\"omb\")" in ask_text
    assert "related_brief_doc_filter_rejects_seen_daily_and_cross_project_docs" in ask_text
    assert 'assert!(!related_brief_doc_allowed(&base, &seen, Some("other")));' in ask_text
    assert "related_brief_record_merges_graph_and_claim_evidence_for_same_doc" in ask_text
    assert "related_brief_record_merges_same_related_doc_across_seed_docs" in ask_text
    assert "related_brief_record_merges_same_kind_evidence_nodes" in ask_text
    assert "related_brief_candidates_prioritize_merged_evidence_before_limit" in ask_text
    assert "related_brief_candidates_tie_break_by_source_path" in ask_text
    assert "related_brief_record_normalizes_single_evidence_count" in ask_text
    assert "related_evidence_summary_exposes_graph_reason_without_multiline_metadata" in ask_text
    assert "assert!(!out.contains('\\n'));" in ask_text
    assert "related_evidence_summary_names_claim_axis_lane" in ask_text
    assert '"shares 1 claim axis: release train / release version"' in ask_text
    assert "fn register_prompt" in ask_text
    assert ask_text.count("register_prompt(") >= 5
    assert "register_claim_context_defangs_untrusted_fields_and_source_metadata" in ask_text
    assert "register_prompt_wraps_claim_context_in_data_fence" in ask_text
    assert "claim_prompt_source_metadata_collapses_to_one_line" in ask_text
    assert "prompt_meta_field(&record.source_path)" in ask_text
    assert "defang(&record.source_path).trim_end()" not in ask_text
    assert "llm.generate(&system, &context).await?" not in ask_text
    assert "coalesce_brief_preserves_identity_punctuation_project_names" in ask_text
    assert "coalesce_brief_strips_trailing_source_metadata_for_dedup" in ask_text
    assert "coalesce_brief_drops_relation_metadata_bullets" in ask_text
    assert "fn is_relation_metadata_bullet" in ask_text
    assert 'normalized.starts_with("shares ")' in ask_text
    assert "strip_brief_source_suffix(&text)" in ask_text
    assert "matches!(ch, '+' | '#' | '.')" in ask_text
    assert ask_text.count(".filter(|d| !has_generated_brief_tag(&d.tags))") >= 2
    related_allowed_body = rust_function_block(ask_text, "related_brief_doc_allowed")
    assert "seen.contains(&doc.source_path)" in related_allowed_body
    assert "has_generated_brief_tag(&doc.tags)" in related_allowed_body
    assert "is_internal_eval_fixture_path(&doc.source_path)" in related_allowed_body
    assert 'doc("vault/wiki/eval-related.md", "omb", vec![])' in ask_text
    assert "fn generated_brief_is_not_a_duplicate_candidate" in mcp_text
    assert "if is_generated_brief(&fm)" in mcp_text
    assert "fn eval_fixture_is_not_a_duplicate_candidate" in mcp_text
    assert "if is_internal_eval_fixture_path(&path.to_string_lossy())" in mcp_text
    assert "const CLAIM_RELATED_LIMIT" in projection_text
    assert "const SEMANTIC_RELATED_LIMIT" in projection_text
    assert "const PROJECT_RECENCY_LINK_MIN" in projection_text
    assert "const PROJECT_RECENCY_LIMIT" in projection_text
    assert "const PROJECT_RELATED_LINK_CAP" in projection_text
    assert "stems.truncate(PROJECT_RELATED_LINK_CAP)" in projection_text
    assert "projected_wiki_stems_keeps_claim_axis_before_cap" in projection_text
    assert "projected_wiki_stems_uses_recency_only_for_isolated_notes" in projection_text
    assert "push_unique_wiki_stem_keeps_wiki_links_deduped" in projection_text
    assert "const SEMANTIC_EDGE_KINDS" in store_text
    assert "const RELATED_DOC_EDGE_KINDS" in store_text
    assert "generated briefs are skipped so summaries never become source memory" in docs["README.md"]
    assert "요약이 다음 요약의 원문이 되지 않습니다" in docs["README.ko.md"]
    assert "要約が次の要約の原文にならないようにします" in docs["README.ja.md"]


def test_scheduler_env_contract_rejects_bad_loop_timing():
    scheduler_text = SCHEDULER.read_text(encoding="utf-8")
    serve_text = SERVE.read_text(encoding="utf-8")

    assert "const SECONDS_PER_HOUR: u64 = 3_600;" in scheduler_text
    assert "const SCHEDULER_DEFAULT_SYNC_HOURS: u64 = 4;" in scheduler_text
    assert "const SCHEDULER_DEFAULT_COMPACT_HOURS: u64 = 24;" in scheduler_text
    assert "const SCHEDULER_DEFAULT_BRIEF_HOUR: u32 = 8;" in scheduler_text
    assert "fn parse_scheduler_hours(" in scheduler_text
    assert "fn parse_scheduler_hour_of_day(" in scheduler_text
    assert "fn scheduler_hours_duration(" in scheduler_text
    assert "pub(crate) fn spawn_scheduler(" in scheduler_text
    assert ") -> Result<()>" in scheduler_text
    assert "scheduler::spawn_scheduler(" in serve_text
    assert "    )?;" in serve_text
    assert (
        'parse_scheduler_hours(\n        "BORING_SYNC_HOURS",\n        config::env_set("BORING_SYNC_HOURS"),\n        SCHEDULER_DEFAULT_SYNC_HOURS,'
        in scheduler_text
    )
    assert (
        'parse_scheduler_hours(\n        "BORING_COMPACT_HOURS",\n        config::env_set("BORING_COMPACT_HOURS"),\n        SCHEDULER_DEFAULT_COMPACT_HOURS,'
        in scheduler_text
    )
    assert (
        'parse_scheduler_hour_of_day(\n        "BORING_BRIEF_HOUR",\n        config::env_set("BORING_BRIEF_HOUR"),\n        SCHEDULER_DEFAULT_BRIEF_HOUR,'
        in scheduler_text
    )
    assert "with_context(|| format!(\"{name} must be a positive integer hour count\"))" in scheduler_text
    assert "bail!(\"{name} must be at least 1 hour\")" in scheduler_text
    assert "checked_mul(SECONDS_PER_HOUR)" in scheduler_text
    assert "with_context(|| format!(\"{name} exceeds Duration seconds\"))" in scheduler_text
    assert "with_context(|| format!(\"{name} must be an integer hour in 0..=23\"))" in scheduler_text
    assert "bail!(\"{name} must be an integer hour in 0..=23\")" in scheduler_text
    assert "scheduler_interval_env_uses_default_only_when_absent" in scheduler_text
    assert "scheduler_rejects_malformed_interval_env" in scheduler_text
    assert "scheduler_rejects_zero_interval_env" in scheduler_text
    assert "scheduler_rejects_duration_overflow" in scheduler_text
    assert "scheduler_brief_hour_env_uses_default_only_when_absent" in scheduler_text
    assert "scheduler_rejects_invalid_brief_hour_env" in scheduler_text
    assert ".and_then(|v| v.parse().ok())" not in scheduler_text
    assert ".unwrap_or(4)" not in scheduler_text
    assert ".unwrap_or(24)" not in scheduler_text
    assert '.unwrap_or_else(|| "8".to_owned())' not in scheduler_text
    assert "Duration::from_mins(1)" not in scheduler_text


def test_raw_witness_defaults_are_documented_from_code_contracts():
    retention_days = python_int_assign(RETENTION, "DEFAULT_RAW_WITNESS_DAYS")
    distill_text = DISTILL_CORE.read_text(encoding="utf-8")
    retention_text = RETENTION.read_text(encoding="utf-8")
    test_retention_text = (ROOT / "scripts" / "test_retention.py").read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert 'os.path.join(BORING_HOME, "data", "raw-witness")' in distill_text
    assert 'home / "data" / "raw-witness"' in retention_text
    assert '"raw_witness_total_bytes"' in retention_text
    assert '"raw_witness_count"' in retention_text
    assert "raw witness snapshots:" in retention_text
    assert "raw = os.environ.get(name)" in retention_text
    assert "return float(os.environ.get(name) or default)" not in retention_text
    assert "except ValueError:\n        return float(default)" not in retention_text
    assert "must be a number of days" in retention_text
    assert "must be non-negative days" in retention_text
    assert "test_env_days_rejects_invalid_policy_value" in test_retention_text
    assert "test_env_days_rejects_negative_policy_value" in test_retention_text
    assert "test_old_raw_witness_is_deleted_but_young_witness_is_kept" in test_retention_text
    assert "test_raw_witness_summary_reports_total_and_eligible_bytes" in test_retention_text

    assert f"`{retention_days}` days" in docs["README.md"]
    assert f"`{retention_days}`일" in docs["README.ko.md"]
    assert f"`{retention_days}` 日" in docs["README.ja.md"]
    for label, text in docs.items():
        assert "data/raw-witness" in text, f"{label} misses raw witness default path"
        assert "BORING_RAW_WITNESS_DIR" in text, f"{label} misses raw witness relocation knob"


def test_retention_apply_failures_are_nonzero_contract():
    retention_text = RETENTION.read_text(encoding="utf-8")
    test_retention_text = (ROOT / "scripts" / "test_retention.py").read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "failed_actions = 0" in retention_text
    assert retention_text.count("failed_actions += 1") == 5
    assert 'print(f"\\n❌ Retention completed with {failed_actions} error(s).", file=sys.stderr)' in retention_text
    assert "raise SystemExit(1)" in retention_text
    assert 'with open(src, "rb") as f_in, open(tmp, "wb") as f_tmp:' in retention_text
    assert 'gzip.GzipFile(fileobj=f_tmp, mode="wb")' in retention_text
    assert "f_tmp.flush()" in retention_text
    assert "os.fsync(f_tmp.fileno())" in retention_text
    assert "os.replace(tmp, archive)" in retention_text
    assert "tmp.unlink()" in retention_text
    assert "archive temp cleanup failed" in retention_text
    assert "test_apply_exits_nonzero_when_action_fails" in test_retention_text
    assert "test_archive_writes_fsynced_gzip_before_source_removal_boundary" in test_retention_text
    assert "test_archive_preserves_source_and_existing_archive_on_publish_failure" in test_retention_text
    assert 'mock.patch.object(sys, "argv", ["retention.py", "--apply", "--yes"])' in test_retention_text
    assert "gzip archives are fsynced before source transcript removal" in docs["README.md"]
    assert "gzip archive는 원본 트랜스크립트 제거 전에 fsync" in docs["README.ko.md"]
    assert "gzip archive は元トランスクリプト削除前に fsync" in docs["README.ja.md"]


def test_distill_adapters_extract_from_raw_witness_snapshots():
    distill_text = DISTILL_CORE.read_text(encoding="utf-8")
    shared_test_text = TEST_DISTILL_CORE.read_text(encoding="utf-8")
    transcript_text = TRANSCRIPT.read_text(encoding="utf-8")
    transcript_test_text = TEST_TRANSCRIPT.read_text(encoding="utf-8")
    claude_text = CLAUDE_DISTILL.read_text(encoding="utf-8")
    claude_test_text = CLAUDE_TEST_HOOKS.read_text(encoding="utf-8")
    codex_text = CODEX_DISTILL.read_text(encoding="utf-8")
    codex_test_text = CODEX_TEST.read_text(encoding="utf-8")
    codex_collect_text = CODEX_COLLECT.read_text(encoding="utf-8")
    codex_readme_text = (ROOT / "agents" / "codex" / "README.md").read_text(encoding="utf-8")
    kimi_text = KIMI_DISTILL.read_text(encoding="utf-8")
    kimi_test_text = KIMI_TEST.read_text(encoding="utf-8")
    hermes_worker_text = HERMES_INGEST_WORKER.read_text(encoding="utf-8")
    hermes_test_text = TEST_INGEST_WORKER.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    contracts = [
        (
            CLAUDE_DISTILL,
            'write_raw_witness(transcript_path, "claude-code", session_id)',
            'text = extract(witness["path"])',
        ),
        (
            CODEX_DISTILL,
            'write_raw_witness(transcript_path, "codex", session_id)',
            'text = extract(witness["path"])',
        ),
        (
            KIMI_DISTILL,
            'write_raw_witness(wire_path, "kimi", session_id)',
            'text = extract_wire(witness["path"])',
        ),
    ]

    for path, witness_call, extract_call in contracts:
        main_text = python_function_source(path, "main")

        assert witness_call in main_text, f"{path.relative_to(ROOT)} does not copy raw witness first"
        assert extract_call in main_text, f"{path.relative_to(ROOT)} does not extract from witness snapshot"
        assert 'sources=[witness["source"]]' in main_text, (
            f"{path.relative_to(ROOT)} does not pass raw witness source pointer to remember"
        )

    assert "normalize_resolution(raw or \"evidence\", default=\"evidence\")" not in distill_text
    assert "using 'evidence'" not in distill_text
    assert "def _publish_raw_witness_bytes(target_path, data):" in distill_text
    assert "def _safe_witness_ext(source_path):" in distill_text
    assert "re.fullmatch" in python_function_source(DISTILL_CORE, "_safe_witness_ext")
    assert "f.flush()" in distill_text
    assert "os.fsync(f.fileno())" in distill_text
    assert "os.replace(tmp_path, target_path)" in distill_text
    assert "os.unlink(tmp_path)" in distill_text
    assert "raw witness temp cleanup failed" in distill_text
    assert "os.utime(target_path, (mtime, mtime))" in distill_text
    assert "raw witness mtime preservation failed" in distill_text
    assert "except OSError:\n            pass" not in distill_text
    assert "test_write_raw_witness_fsyncs_snapshot_publish_without_temp_leftover" in shared_test_text
    assert "test_write_raw_witness_sanitizes_extension_for_source_pointer" in shared_test_text
    assert "test_write_raw_witness_preserves_existing_target_on_publish_failure" in shared_test_text
    assert "test_write_raw_witness_preserves_source_mtime" in shared_test_text
    assert "test_write_raw_witness_reseals_matching_target_mtime" in shared_test_text
    assert "test_write_raw_witness_warns_when_mtime_preservation_fails" in shared_test_text
    assert "invalid BORING_DISTILL_RESOLUTION" in distill_text
    assert "expected one of:" in distill_text
    assert "test_invalid_env_resolution_fails_fast" in shared_test_text
    assert "def run() -> int:" in codex_text
    assert "sys.exit(run())" in codex_text
    assert "test_run_reports_invalid_distill_resolution_without_traceback" in codex_test_text
    assert 'CLAMP = int(os.environ.get("CODEX_DISTILL_CLAMP")' not in codex_text
    assert 'DISTILL_CLAMP = int(os.environ.get("CODEX_DISTILL_CLAMP")' not in codex_collect_text
    assert 'CLAMP = int(os.environ.get("DISTILL_CLAMP")' not in claude_text
    assert 'CLAMP = int(os.environ.get("INGEST_CLAMP")' not in hermes_worker_text
    assert "def parse_clamp_limit" in transcript_text
    assert "test_parse_clamp_limit_rejects_invalid_values" in transcript_test_text
    assert "misconfigured agent does not silently produce empty notes" in transcript_text
    assert "raise ValueError(f\"unsupported transcript format: {fmt}\")" in transcript_text
    assert "test_extract_unknown_format_raises" in transcript_test_text
    assert "_CODEX_USER_NOISE_MARKERS" in transcript_text
    assert "_CODEX_AGENT_NOISE_MESSAGES" in transcript_text
    assert "test_extract_codex_jsonl_user_and_assistant" in transcript_test_text
    assert "EXTERNAL SESSION IMPORTED" in transcript_test_text
    assert "system reminder" in transcript_test_text
    assert "test_extract_kimi_wire_user_and_assistant" in transcript_test_text
    assert "DEFAULT_CODEX_DISTILL_CLAMP = 4000" in codex_text
    assert "DEFAULT_CODEX_DISTILL_CLAMP = 4000" in codex_collect_text
    assert "DEFAULT_DISTILL_CLAMP = 2000" in claude_text
    assert "DEFAULT_DISTILL_CLAMP = 2000" in kimi_text
    assert "DEFAULT_INGEST_CLAMP = 4000" in hermes_worker_text
    assert "_non_negative_int" not in codex_text
    assert "_non_negative_int" not in codex_collect_text
    assert "transcript.parse_clamp_limit" in codex_text
    assert "transcript.parse_clamp_limit" in codex_collect_text
    assert "transcript.parse_clamp_limit" in claude_text
    assert "transcript.parse_clamp_limit" in kimi_text
    assert "transcript.parse_clamp_limit" in hermes_worker_text
    assert "text, was_clamped = transcript.clamp_text(text, _clamp_limit())" in kimi_text
    assert "_effective_distill_clamp" in codex_collect_text
    assert "test_codex_distill_rejects_invalid_payload_clamp" in codex_test_text
    assert "test_codex_collect_rejects_invalid_distill_clamp" in codex_test_text
    assert "test_invalid_distill_clamp_is_rejected_at_boundary" in claude_test_text
    assert "test_distill_clamps_with_direct_hook_budget" in kimi_test_text
    assert "test_distill_rejects_invalid_clamp_env_at_boundary" in kimi_test_text
    assert "test_ingest_clamp_rejects_invalid_env_value" in hermes_test_text
    assert "`0` disables clamping" in codex_readme_text
    assert "invalid or\n  negative values fail before distillation" in codex_readme_text
    assert "invalid values fail before distillation starts" in docs["README.md"]
    assert "잘못된 값은 증류 시작 전에 실패" in docs["README.ko.md"]
    assert "不正な値は蒸留開始前に失敗" in docs["README.ja.md"]
    assert "`0` disables clamping; invalid or negative values fail before distillation" in docs["README.md"]
    assert "Claude/Kimi direct SessionEnd hooks" in docs["README.md"]
    assert "`0`은 clamp 비활성화이며, 잘못된 값이나 음수는 증류 전에 실패" in docs["README.ko.md"]
    assert "Claude/Kimi 직접 SessionEnd hook" in docs["README.ko.md"]
    assert "`0` は clamp 無効化、不正値や負数は蒸留前に失敗" in docs["README.ja.md"]
    assert "Claude/Kimi の直接 SessionEnd hook" in docs["README.ja.md"]
    assert "`0` disables clamping" in docs["README.md"]
    assert "invalid or negative values fail before distillation starts" in docs["README.md"]
    assert "`0`은 clamp 비활성화" in docs["README.ko.md"]
    assert "잘못된 값이나 음수는 증류 시작 전에 실패" in docs["README.ko.md"]
    assert "`0` は clamp 無効化" in docs["README.ja.md"]
    assert "不正値や負数は蒸留開始前に失敗" in docs["README.ja.md"]


def test_codex_worker_harvests_stable_rollouts_but_skips_subagents():
    collect_text = CODEX_COLLECT.read_text(encoding="utf-8")
    scan_source = python_function_source(CODEX_COLLECT, "_scan_sessions")
    subagent_source = python_function_source(CODEX_COLLECT, "_is_subagent")
    wiring_text = AGENT_WIRING.read_text(encoding="utf-8")
    hermes_wrapper_text = (ROOT / "agents" / "hermes" / "codex-collect-sessions.py").read_text(encoding="utf-8")
    codex_test_text = CODEX_TEST.read_text(encoding="utf-8")
    wiring_test_text = TEST_AGENT_WIRING.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
        "agents/codex/README.md": (ROOT / "agents" / "codex" / "README.md").read_text(encoding="utf-8"),
        "agents/hermes/README.md": (ROOT / "agents" / "hermes" / "README.md").read_text(encoding="utf-8"),
    }

    assert 'STABLE_AGE_S = omb_env.env_non_negative_float("COLLECT_STABLE_AGE_SECONDS", 1800.0)' in collect_text
    assert 'INCLUDE_SUBAGENTS = _env_bool("CODEX_INCLUDE_SUBAGENTS")' in collect_text
    assert 'INCLUDE_ROLLOUTS = _env_bool("CODEX_INCLUDE_ROLLOUTS", default=True) or INCLUDE_SUBAGENTS' in collect_text
    assert scan_source.index('scan["too_new"] += 1') < scan_source.index("if not INCLUDE_ROLLOUTS")
    assert scan_source.index("if not INCLUDE_ROLLOUTS") < scan_source.index("if not INCLUDE_SUBAGENTS")
    assert 'meta.get("thread_source") == "subagent"' in subagent_source
    assert 'source.get("subagent")' in subagent_source

    assert "CODEX_HOST_WORKER_INTERVAL_SEC = 20 * 60" in wiring_text
    assert "CODEX_INCLUDE_ROLLOUTS=1" in wiring_text
    assert "COLLECT_STABLE_AGE_SECONDS=1800" in wiring_text
    assert 'script = "codex-collect-sessions.py"' in wiring_text
    assert '"no_agent": True' in wiring_text
    assert 'os.environ.setdefault("CODEX_INCLUDE_ROLLOUTS", "1")' in hermes_wrapper_text
    assert 'os.environ.setdefault("COLLECT_STABLE_AGE_SECONDS", "1800")' in hermes_wrapper_text

    assert "test_collect_scan_can_include_rollouts_without_subagents" in codex_test_text
    assert "test_collect_scan_skips_unstable_recent_sessions" in codex_test_text
    assert "test_status_mode_reports_queue_worker_and_note_without_mutation" in codex_test_text
    assert "CODEX_INCLUDE_ROLLOUTS=1" in wiring_test_text
    assert "COLLECT_STABLE_AGE_SECONDS=1800" in wiring_test_text
    assert 'assert codex_worker["no_agent"] is True' in wiring_test_text

    assert "scans `~/.codex/sessions/**/*.jsonl` every 20 minutes" in docs["README.md"]
    assert "skips transcripts still being written" in docs["README.md"]
    assert "keeps true subagent rollouts out" in docs["README.md"]
    assert "20분마다 `~/.codex/sessions/**/*.jsonl`을 스캔" in docs["README.ko.md"]
    assert "실제 subagent rollout은 건너뛰" in docs["README.ko.md"]
    assert "20 分ごとに `~/.codex/sessions/**/*.jsonl` をスキャン" in docs["README.ja.md"]
    assert "実際の subagent rollout はスキップ" in docs["README.ja.md"]
    assert "stable Codex Desktop rollout transcripts" in docs["agents/codex/README.md"]
    assert "True subagent/guardian roll-outs are skipped unless `CODEX_INCLUDE_SUBAGENTS=1`" in docs[
        "agents/codex/README.md"
    ]
    assert "harvests stable rollout transcripts, skips true subagents" in docs["agents/hermes/README.md"]


def test_raw_witness_default_path_stays_gitignored():
    lines = gitignore_patterns()

    assert "data/*" in lines
    assert "!data/eval/" in lines
    assert "!data/raw-witness/" not in lines
    assert "!data/raw-witness/*" not in lines
    assert "!data/raw-witness/**" not in lines


def test_hermes_container_can_write_default_raw_witness_path():
    text = DOCKER_COMPOSE.read_text(encoding="utf-8")

    assert ".:/host/oh-my-boring:ro" in text
    assert "./data/raw-witness:/host/oh-my-boring/data/raw-witness" in text
    assert "./data/raw-witness:/host/oh-my-boring/data/raw-witness:ro" not in text


def test_mcp_sources_contract_names_raw_witness_pointer():
    mcp_text = MCP.read_text(encoding="utf-8")
    frontmatter_text = FRONTMATTER.read_text(encoding="utf-8")
    audit_text = VAULT_AUDIT.read_text(encoding="utf-8")
    vault_schema_text = VAULT_SCHEMA.read_text(encoding="utf-8")
    vault_frontmatter_text = VAULT_FRONTMATTER.read_text(encoding="utf-8")

    assert "raw-witness/codex/20260703/session.jsonl#sha256=..." in mcp_text
    assert "raw/<file>.md" not in mcp_text
    assert "raw-witness/...#sha256=..." in frontmatter_text
    assert "source-sha-missing" in audit_text
    assert "source-sha-invalid" in audit_text
    assert "source-sha-mismatch" in audit_text
    assert "source-path-escape" in audit_text
    assert "source_exists.then_some(full_path.as_path())" in audit_text
    assert "raw_witness_missing_file_still_requires_sha256_fragment" in audit_text
    assert "raw_witness_source_rejects_parent_dir_escape" in audit_text
    assert "raw_witness_source_rejects_invalid_sha256_fragment" in audit_text
    assert "raw_witness_source_rejects_extra_fragment" in audit_text
    assert '- "raw-witness/"' in vault_schema_text
    assert "`raw-witness/`" in vault_frontmatter_text
    assert "`#sha256=...`" in vault_frontmatter_text
    assert "even when the local witness bytes have been pruned" in vault_frontmatter_text


def test_okf_frontmatter_contract_is_enforced():
    frontmatter_text = FRONTMATTER.read_text(encoding="utf-8")
    audit_text = VAULT_AUDIT.read_text(encoding="utf-8")
    vault_schema_text = VAULT_SCHEMA.read_text(encoding="utf-8")
    vault_frontmatter_text = VAULT_FRONTMATTER.read_text(encoding="utf-8")
    remember_text = (ROOT / "drudge" / "src" / "vault" / "remember.rs").read_text(encoding="utf-8")

    assert "pub okf_version: Option<String>" in frontmatter_text
    assert "pub summary: Option<String>" in frontmatter_text
    assert "pub skills: Vec<String>" in frontmatter_text
    assert "pub contracts: Vec<String>" in frontmatter_text
    assert "pub incidents: Vec<String>" in frontmatter_text
    assert "okf_required:" in vault_schema_text
    assert "okf_recommended:" in vault_schema_text
    assert "session_metadata:" in vault_schema_text
    assert "OKF v0.1" in vault_frontmatter_text
    assert "`type`" in vault_frontmatter_text
    assert "`description`" in vault_frontmatter_text
    assert "`timestamp`" in vault_frontmatter_text
    assert "`skills`" in vault_frontmatter_text
    assert "`contracts`" in vault_frontmatter_text
    assert "`incidents`" in vault_frontmatter_text
    assert "okf-type-missing" in audit_text
    assert "okf-legacy-map" in audit_text
    assert "okf-description-missing" in audit_text
    assert "okf-timestamp-missing" in audit_text
    assert "session-metadata-placeholder" in audit_text
    assert "okf_type: kind" in remember_text or "#[serde(rename = \"type\")]" in remember_text
    assert "timestamp: format!" in remember_text
    assert "okf_version" in remember_text and "0.1" in remember_text


def test_secret_scrub_boundary_fails_closed_and_covers_query_log():
    redact_text = REDACT.read_text(encoding="utf-8")
    store_text = STORE.read_text(encoding="utf-8")
    mcp_text = MCP.read_text(encoding="utf-8")

    assert "Secret scrub — the single leak-boundary guard" in redact_text
    assert "fn redact_scrubs_known_tokens()" in redact_text
    assert "build_secret_re_is_cached" in redact_text
    assert "query_log is exported by backup-db and served by" in store_text
    assert 'context("build query_log secret scrub regex")?' in store_text
    assert "crate::redact::redact(re, query)" in store_text
    assert "crate::redact::redact(re, answer_snippet)" in store_text
    assert "Err(_) => (query.to_owned(), answer_snippet.to_owned())" not in store_text
    assert "parse_remember_scrubs_secrets_in_title_and_claim_value" in mcp_text
    assert "pii_gate_scans_every_rendered_frontmatter_field" in mcp_text
    assert "raw-witness/codex/20260703/[EMAIL]#sha256=abc123" in mcp_text


def test_mcp_remember_duplicate_replacement_exercises_write_door():
    mcp_text = MCP.read_text(encoding="utf-8")
    shared_test_text = TEST_DISTILL_CORE.read_text(encoding="utf-8")

    assert "incoming.score >= current.score + DUPLICATE_REPLACE_MIN_DELTA" in mcp_text
    assert "saturating_add(DUPLICATE_REPLACE_MIN_DELTA)" not in mcp_text
    assert "duplicate_replacement_respects_exact_quality_delta" in mcp_text
    assert "async fn mcp_remember_rewrites_richer_same_session_duplicate_in_place" in mcp_text
    assert "mcp_remember(&state, Some(&args)).await.unwrap()" in mcp_text
    assert '"remembered → wiki/wiki-0001.md (updated duplicate)"' in mcp_text
    assert '!wiki.join("wiki-0002.md").exists()' in mcp_text
    assert "Implemented deterministic duplicate replacement" in mcp_text
    assert '"skipped — duplicate of {}"' in mcp_text
    assert "test_call_remember_treats_actual_duplicate_ack_as_success" in shared_test_text


def test_remember_relation_projection_stays_single_note_bounded():
    mcp_text = MCP.read_text(encoding="utf-8")
    projection_text = VAULT_PROJECTION.read_text(encoding="utf-8")
    finish_body = rust_function_block(mcp_text, "finish_remembered_note")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "pub async fn project_note" in projection_text
    assert "pub async fn project_links" in projection_text
    assert "backlinks" in projection_text
    assert "next periodic full `project_links`" in projection_text
    assert "use crate::frontmatter::raw_has_generated_brief_tag;" in projection_text
    assert "fn projectable_frontmatter" in projection_text
    assert "if raw_has_generated_brief_tag(content)" in projection_text
    assert "projectable_frontmatter_excludes_generated_brief_sources" in projection_text
    assert "bounded: ~3 queries + 1 write" in finish_body
    assert "vault::project_note(store, path, 6).await" in finish_body
    assert "vault::project_links" not in finish_body
    assert "new note's links are updated first; neighbor backlinks catch up on the next `sync`" in docs["README.md"]
    assert "recall is immediate while Obsidian links are eventually consistent" in docs["README.md"]
    assert "duplicate candidates, ingest confirmation markers, Obsidian relation projection, and DB ingest" in docs["README.md"]
    assert "새 노트 쪽 링크가 먼저 보이고, 이웃 backlink는 다음 `sync`에서 따라옵니다" in docs["README.ko.md"]
    assert "회수는 즉시 가능하고 Obsidian 링크만 eventual consistency" in docs["README.ko.md"]
    assert "중복 후보, ingest 확인 마커, Obsidian relation projection, DB 적재" in docs["README.ko.md"]
    assert "新ノート側のリンクが先に見え、隣接ノートの backlink は次回 `sync` で追いつきます" in docs["README.ja.md"]
    assert "recall は即時で、Obsidian link だけが eventual consistency" in docs["README.ja.md"]
    assert "重複候補、ingest 確認マーカー、Obsidian relation projection、DB 取り込み" in docs["README.ja.md"]


def test_write_maintenance_lane_serializes_sync_remember_forget_contract():
    serve_text = SERVE.read_text(encoding="utf-8")
    http_text = HTTP.read_text(encoding="utf-8")
    mcp_text = MCP.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
        "ohmyboring/SKILL.md": OHMYBORING_SKILL.read_text(encoding="utf-8"),
    }

    remember_body = rust_function_block(mcp_text, "finish_remembered_note")
    forget_body = rust_function_block(mcp_text, "mcp_forget")
    sync_body = rust_function_block(mcp_text, "mcp_sync")
    http_sync_body = rust_function_block(http_text, "handle_sync")
    compact_body = rust_function_block(http_text, "handle_compact")

    assert "Serializes the write-maintenance lane" in serve_text
    assert "vector-mode `remember`/`forget` relation rewrites" in serve_text
    assert "try_lock reveals whether the write-maintenance lane (sync/remember/forget)" in http_text
    for body in (remember_body, forget_body, sync_body, http_sync_body, compact_body):
        assert "sync_lock.lock().await" in body

    assert "Write-maintenance lock" in docs["README.md"]
    assert "`sync`, `compact`, `remember`, and `forget` share one `sync_lock`" in docs["README.md"]
    assert "Write-maintenance lock" in docs["README.ko.md"]
    assert "`sync`, `compact`, `remember`, `forget`은 DB 기반 graph/relation 상태를 다시 쓸 때 하나의 `sync_lock`을 공유합니다" in docs["README.ko.md"]
    assert "Write-maintenance lock" in docs["README.ja.md"]
    assert "`sync`、`compact`、`remember`、`forget` は、DB-backed graph/relation 状態を書き換えるとき同じ `sync_lock` を共有します" in docs["README.ja.md"]
    assert "`sync`/`compact`/`remember`/`forget`은 같은 `sync_lock`" in docs["ohmyboring/SKILL.md"]
    assert "`remember`/`forget`은 lock을 잡지 않는다" not in docs["ohmyboring/SKILL.md"]


def gitignore_patterns() -> list[str]:
    return [
        line.strip()
        for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def python_int_assign(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
            return node.value.value
    raise AssertionError(f"{name} int assignment not found in {path.relative_to(ROOT)}")


def python_function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            source = ast.get_source_segment(text, node)
            assert source is not None, f"{path.relative_to(ROOT)}:{name} source unavailable"
            return source
    raise AssertionError(f"{path.relative_to(ROOT)} misses function {name}")


def rust_function_block(text: str, name: str) -> str:
    signature = f"async fn {name}("
    start = text.find(signature)
    if start < 0:
        signature = f"fn {name}("
        start = text.find(signature)
    assert start >= 0, f"rust function not found: {name}"
    brace = text.find("{", start)
    assert brace >= 0, f"rust function body not found: {name}"

    depth = 0
    for idx in range(brace, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    raise AssertionError(f"rust function body did not close: {name}")


def rust_number(raw: str) -> int:
    return int(raw.replace("_", ""))


def rust_usize_const(path: Path, name: str) -> int:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"const {name}: usize = ([\d_]+);", text)
    assert match, f"{name} usize const not found"
    return rust_number(match.group(1))


def rust_u32_const(path: Path, name: str) -> int:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"const {name}: u32 = ([\d_]+);", text)
    assert match, f"{name} u32 const not found"
    return rust_number(match.group(1))


def rust_i32_const(path: Path, name: str) -> int:
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"const {name}: i32 = ([\d_]+);", text)
    assert match, f"{name} i32 const not found"
    return rust_number(match.group(1))


def rust_default_chunker_values() -> tuple[int, int]:
    text = INGEST.read_text(encoding="utf-8")
    match = re.search(
        r"pub fn new\(\) -> Self \{\s*Self \{\s*size: ([\d_]+),\s*overlap: ([\d_]+),\s*\}\s*\}",
        text,
        re.S,
    )
    assert match, "DefaultChunker::new size/overlap contract not found"
    return rust_number(match.group(1)), rust_number(match.group(2))


def rust_string_array_const(path: Path, name: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"const {name}: (?:\[&str; \d+\]|&\[&str\]) = &?\[(.*?)\];",
        text,
        re.S,
    )
    assert match, f"{name} const not found"
    return re.findall(r'"([^"]+)"', match.group(1))


def rust_brief_label_aliases() -> dict[str, str]:
    text = ASK.read_text(encoding="utf-8")
    match = re.search(
        r"const BRIEF_LABEL_ALIASES: &\[\(&str, &str\)\] = &\[(.*?)\];",
        text,
        re.S,
    )
    assert match, "BRIEF_LABEL_ALIASES const not found"
    pairs = re.findall(r'\("([^"]+)", "([^"]+)"\)', match.group(1))
    assert pairs, "BRIEF_LABEL_ALIASES has no aliases"
    assert len(dict(pairs)) == len(pairs), "duplicate BRIEF_LABEL_ALIASES key"
    return dict(pairs)


def rust_brief_label_separators() -> list[str]:
    return rust_string_array_const(ASK, "BRIEF_LABEL_SEPARATORS")


def rust_brief_label_order() -> list[str]:
    return rust_string_array_const(ASK, "BRIEF_LABEL_ORDER")


def load_slack_briefing_module():
    spec = importlib.util.spec_from_file_location("slack_briefing_contract", SLACK_BRIEFING)
    assert spec and spec.loader, "slack_briefing.py module spec not found"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_dedup_wiki_module():
    spec = importlib.util.spec_from_file_location("dedup_wiki_contract", DEDUP_WIKI)
    assert spec and spec.loader, "dedup-wiki.py module spec not found"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_self_verify_loop_module():
    spec = importlib.util.spec_from_file_location("self_verify_loop_contract", SELF_VERIFY_LOOP)
    assert spec and spec.loader, "self_verify_loop.py module spec not found"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rust_key_examples(name: str) -> dict[str, str]:
    text = FRONTMATTER.read_text(encoding="utf-8")
    pairs = re.findall(rf'assert_eq!\({name}\("([^"]+)"\), "([^"]+)"\);', text)
    assert pairs, f"{name} examples not found in frontmatter.rs"
    return dict(pairs)


def skill_mcp_table() -> dict[str, str]:
    text = OHMYBORING_SKILL.read_text(encoding="utf-8")
    section = text.split("### MCP 도구", 1)[1].split("\n---", 1)[0]
    rows = {}
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        cols = [col.strip() for col in line.strip("|").split("|")]
        assert len(cols) == 3, f"malformed MCP tool row: {line}"
        tool = cols[0].removeprefix("`").removesuffix("`")
        rows[tool] = cols[2]
    return rows


def test_ohmyboring_skill_mcp_inventory_matches_rust_contract():
    tools = rust_string_array_const(MCP, "MCP_TOOL_NAMES")
    vector_required = set(rust_string_array_const(MCP, "VECTOR_REQUIRED_TOOLS"))
    skill_text = OHMYBORING_SKILL.read_text(encoding="utf-8")
    skill_rows = skill_mcp_table()

    assert f"### MCP 도구 ({len(tools)}개)" in skill_text
    assert "읽기는 `recall` 계열이 `vault/wiki`를 LLM 합성 없이 직접 읽고" in skill_text
    assert "`make ask`/`/ask`는 같은 wiki-first 검색 뒤 로컬 합성 모델만 실행한다" in skill_text
    assert "읽기(`make ask`, `recall`)는 LLM 없이" not in skill_text
    assert sorted(skill_rows) == sorted(tools)
    for tool in tools:
        availability = skill_rows[tool]
        if tool in vector_required:
            assert "❌" in availability, f"{tool} should be marked vector-required"
        else:
            assert "✅" in availability, f"{tool} should be marked vector-free"


def test_mcp_tool_result_shape_contract_matches_readmes():
    mcp_text = MCP.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    structured_tools = [
        "neighbors",
        "claims",
        "corpus_status",
        "events",
        "config_get",
        "ask",
        "brief",
        "weekly_brief",
        "project_status",
        "decisions",
        "risks",
        "next_actions",
        "stalled",
        "context",
    ]
    text_tools = ["recall", "remember", "forget", "sync", "classify_repo"]

    for label, text in docs.items():
        line = next(
            (line for line in text.splitlines() if "structuredContent" in line),
            "",
        )
        assert line, f"{label} misses MCP structuredContent result-shape contract"
        for tool in structured_tools:
            assert f"`{tool}`" in line, f"{label} misses structured tool `{tool}`"
        for tool in text_tools:
            assert f"`{tool}`" in line, f"{label} misses text tool `{tool}`"

    assert "enum ToolOut" in mcp_text
    assert "Self::Text(text)" in mcp_text
    assert "Self::Structured(value)" in mcp_text
    assert '"structuredContent": value' in mcp_text
    assert "PROSE/ACK tools → text block" in mcp_text
    for tool in text_tools:
        assert f'"{tool}" => ToolOut::Text' in mcp_text
    for tool in structured_tools:
        assert f'"{tool}" => ToolOut::Structured' in mcp_text


def test_context_vector_off_contract_returns_empty_claim_card():
    mcp_text = MCP.read_text(encoding="utf-8")
    http_text = HTTP.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    vector_required = set(rust_string_array_const(MCP, "VECTOR_REQUIRED_TOOLS"))
    vector_free = set(rust_string_array_const(MCP, "VECTOR_FREE_TOOLS"))
    mcp_context_body = rust_function_block(mcp_text, "mcp_context")
    http_context_body = rust_function_block(http_text, "handle_context")

    assert "context" not in vector_required
    assert "context" in vector_free
    assert "quality_gate_vector_mode_docs_match_tool_contract" in mcp_text
    assert "context_returns_empty_card_without_store" in mcp_text

    for body in (mcp_context_body, http_context_body):
        assert "if let Some(store) = s.store.as_ref()" in body
        assert "ask::ContextCard" in body
        assert "decisions: vec![]" in body
        assert "risks: vec![]" in body
        assert "facts: vec![]" in body
        assert "glossary: vec![]" in body
        assert "next_actions: vec![]" in body
        assert "vec_off_rpc()" not in body
        assert "vector_disabled()" not in body

    assert "`context` is callable but returns an empty claim card without the store" in docs["README.md"]
    assert "`context`는 호출 가능하지만 store가 없으면 빈 claim 카드를 반환" in docs["README.ko.md"]
    assert "`context` は呼び出し可能ですが store がない場合は空の claim card を返" in docs["README.ja.md"]


def test_forget_delete_contract_prunes_wiki_and_vector_artifacts():
    mcp_text = MCP.read_text(encoding="utf-8")
    store_text = STORE.read_text(encoding="utf-8")
    store_test_text = STORE_INTEGRATION.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    vector_required = set(rust_string_array_const(MCP, "VECTOR_REQUIRED_TOOLS"))
    vector_free = set(rust_string_array_const(MCP, "VECTOR_FREE_TOOLS"))
    forget_body = rust_function_block(mcp_text, "mcp_forget")
    delete_body = rust_function_block(store_text, "delete_document")

    assert "forget" not in vector_required
    assert "forget" in vector_free
    assert "Remove a note from memory by wiki id or exact title" in mcp_text
    assert "Deletes the wiki file" in mcp_text
    assert "also removes its embeddings, graph edges, and claims" in mcp_text
    assert (
        "`forget` — delete a note by wiki id or exact title. Removes the wiki file and, in vector mode, also purges embeddings, graph edges, and claims; if vector cleanup fails after the wiki delete, the reply says it is partial and the next `sync` prunes the derived artifacts."
        in docs["README.md"]
    )
    assert (
        "`forget` — wiki id나 정확한 제목으로 노트를 삭제합니다. wiki 파일을 제거하고, vector 모드에서는 임베딩·그래프 엣지·claim도 함께 정리합니다. wiki 삭제 뒤 vector 정리가 실패하면 응답에 partial이라고 밝히고, 다음 `sync`가 파생 artifact를 prune합니다."
        in docs["README.ko.md"]
    )
    assert (
        "`forget` — wiki id または正確なタイトルでノートを削除します。wiki ファイルを削除し、vector モードでは embedding・graph edge・claim も削除します。wiki 削除後に vector cleanup が失敗した場合、応答は partial と明示し、次の `sync` が派生 artifact を prune します。"
        in docs["README.ja.md"]
    )

    assert "forget requires either 'id' or 'title'" in forget_body
    assert "id.contains('/') || id.contains('\\\\') || id.contains(\"..\")" in forget_body
    assert "std::fs::remove_file(&path)" in forget_body
    assert "let source_path = path.to_string_lossy().into_owned();" in forget_body
    assert "if let Some(store) = s.store.as_ref()" in forget_body
    assert ".delete_document(&source_path)" in forget_body
    assert "vector cleanup warning (ignored)" in forget_body
    assert "partial: vector cleanup deferred" in forget_body
    assert "map_err(|e| (-32603_i32, format!(\"delete from vector store: {e:#}\")))" not in forget_body
    assert "vault::project_links(store, vault_root, 6)" in forget_body
    assert "partial: relates_to projection deferred" in forget_body

    assert "source_path text NOT NULL REFERENCES document(source_path) ON DELETE CASCADE" in store_text
    assert "DELETE FROM document WHERE source_path = $1;" in delete_body
    assert "DELETE FROM edge WHERE src = $1 OR dst = $1;" in delete_body
    assert "SELECT DISTINCT subject, predicate FROM claim WHERE source_path = $1;" in delete_body
    assert "DELETE FROM claim WHERE source_path = $1;" in delete_body
    assert "re-seal remaining claims after document delete" in delete_body
    assert "unseal latest remaining claim after document delete" in delete_body
    assert "refresh claim graph projection after document delete" in delete_body
    assert "delete_document_removes_claims" in store_test_text
    assert "delete_document_reseals_remaining_claim_history" in store_test_text
    assert "semantic_neighbors" in store_test_text
    assert "deleted newer claim value must not remain as the claim graph label" in store_test_text


def test_slack_briefing_label_contract_matches_rust_coalescer():
    slack = load_slack_briefing_module()
    rust_label_order = rust_brief_label_order()
    ask_text = ASK.read_text(encoding="utf-8")
    slack_text = SLACK_BRIEFING.read_text(encoding="utf-8")
    slack_test_text = (ROOT / "agents" / "hermes" / "test_briefing_format.py").read_text(
        encoding="utf-8"
    )

    assert slack.LABEL_ALIASES == rust_brief_label_aliases()
    assert list(slack.LABEL_SEPARATORS) == rust_brief_label_separators()
    assert set(slack.LABEL_ALIASES.values()) == set(rust_label_order)
    assert list(slack.LABEL_PREFIXES) == sorted(slack.LABEL_ALIASES, key=len, reverse=True)
    assert list(slack.SECTION_ORDER[:-1]) == rust_label_order
    assert slack.SECTION_ORDER[-1] == ""
    assert set(slack.SECTION_EMOJI) == set(slack.SECTION_ORDER)
    assert set(slack.SECTION_TITLE) == set(slack.SECTION_ORDER)
    assert "fn strip_brief_task_marker" in ask_text
    assert "coalesce_brief_dedups_within_label_without_erasing_status" in ask_text
    assert "coalesce_brief_strips_trailing_source_metadata_for_dedup" in ask_text
    assert "coalesce_brief_drops_placeholder_bullets" in ask_text
    assert '"waiting for instructions"' in ask_text
    assert '"다음 지시 기다림"' in ask_text
    assert '"tbd"' in ask_text
    assert '"pending"' in ask_text
    assert "fn strip_brief_source_suffix" in ask_text
    assert "fn is_brief_source_suffix" in ask_text
    assert "fn is_relation_metadata_bullet" in ask_text
    assert "coalesce_brief_accepts_markdown_task_list_markers" in ask_text
    assert "coalesce_brief_accepts_plain_label_headings_and_items" in ask_text
    assert "coalesce_brief_drops_relation_metadata_bullets" in ask_text
    assert "coalesce_brief_preserves_identity_punctuation_project_names" in ask_text
    assert "coalesce_brief_isolates_punctuation_only_project_headings" in ask_text
    assert "name.trim().to_owned()" in ask_text
    assert "test_slack_mrkdwn_dedups_within_label_without_erasing_status" in slack_test_text
    assert "test_slack_mrkdwn_strips_trailing_source_metadata_for_dedup" in slack_test_text
    assert "test_slack_mrkdwn_accepts_markdown_task_list_markers" in slack_test_text
    assert "test_slack_mrkdwn_accepts_plain_label_headings_and_items" in slack_test_text
    assert "test_slack_mrkdwn_drops_relation_metadata_items" in slack_test_text
    assert (
        "test_slack_mrkdwn_preserves_identity_punctuation_project_names_for_dedup"
        in slack_test_text
    )
    assert "test_slack_mrkdwn_isolates_punctuation_only_project_headings" in slack_test_text
    assert 'elif ch in "+#.":' in slack_text
    assert 'return "".join(chars) or name.strip()' in slack_text
    assert "def _should_drop_item" in slack_text
    assert "def _is_relation_metadata" in slack_text
    assert 'lowered.startswith("shares ")' in slack_text
    assert "def render_sources" in slack_text
    assert 'str(source).split("#", 1)[0].strip()' in slack_text
    assert "return os.path.basename(raw) or raw or str(source)" in slack_text
    assert "EMPTY_SOURCE_NAMES" in slack_text
    assert "key = _source_name(source)" in slack_text
    assert "if key in seen:" in slack_text
    assert "seen.add(key)" in slack_text
    assert "if len(labels) >= SOURCE_LIMIT:" in slack_text
    assert "test_source_label_reads_title_as_yaml_frontmatter" in slack_test_text
    assert "test_sources_dedup_path_and_chunk_variants" in slack_test_text
    assert "test_sources_drop_empty_placeholders" in slack_test_text
    assert "test_source_label_falls_back_for_non_string_title" in slack_test_text
    assert "test_sources_dedup_path_and_chunk_variants()" in slack_test_text
    assert "test_sources_drop_empty_placeholders()" in slack_test_text
    assert slack._strip_task_marker("[ ] Next: guard") == "Next: guard"
    assert slack._strip_task_marker("[x] Done: guard") == "Done: guard"
    assert slack._strip_task_marker("[X] 완료: guard") == "완료: guard"
    assert slack._strip_task_marker("[?] Next: guard") == "[?] Next: guard"
    assert slack._strip_source_suffix("guard (source: vault/wiki/wiki-0001.md)") == "guard"
    assert slack._strip_source_suffix("guard (Rust)") == "guard (Rust)"
    assert slack._is_relation_metadata("shares 2 graph nodes: make, briefing")
    assert slack._is_relation_metadata(
        "related to vault/wiki/wiki-0001.md · shares 1 claim axis: release train"
    )
    assert slack._source_name("vault/wiki/wiki-0001.md#chunk_idx=0") == "wiki-0001.md"
    assert slack._source_name("/tmp/vault/wiki/wiki-0001.md") == "wiki-0001.md"
    assert slack._is_empty_source_name("null")


def test_dedup_wiki_keys_match_rust_frontmatter_contract():
    dedup = load_dedup_wiki_module()
    dedup_text = DEDUP_WIKI.read_text(encoding="utf-8")
    dedup_test_text = (ROOT / "scripts" / "test_dedup_wiki.py").read_text(encoding="utf-8")

    for raw, expected in rust_key_examples("semantic_key").items():
        assert dedup.semantic_key(raw) == expected
    for raw, expected in rust_key_examples("claim_key").items():
        assert dedup.claim_key(raw) == expected
    assert "def parse_similarity_threshold" in dedup_text
    assert "type=parse_similarity_threshold" in dedup_text
    assert "type=float" not in dedup_text
    assert "def derive_project" in dedup_text
    assert "return project or derive_project(path)" in dedup_text
    assert 'return note_record(path, "", text, [], project=derive_project(path))' in dedup_text
    assert dedup.parse_similarity_threshold("0.93") == 0.93
    assert dedup.derive_project(Path("/vault/projects/oh-my-boring/wiki-0001.md")) == "oh-my-boring"
    try:
        dedup.parse_similarity_threshold("-0.01")
    except dedup.argparse.ArgumentTypeError:
        pass
    else:
        raise AssertionError("dedup threshold must reject negative values")
    assert "test_parse_similarity_threshold_rejects_invalid_policy_values" in dedup_test_text
    assert "test_parse_note_derives_blank_project_from_path" in dedup_test_text
    assert "test_parse_note_without_frontmatter_derives_project_from_path" in dedup_test_text


def test_offline_dedup_source_memory_filter_matches_runtime_boundaries():
    dedup = load_dedup_wiki_module()
    dedup_text = DEDUP_WIKI.read_text(encoding="utf-8")
    dedup_test_text = (ROOT / "scripts" / "test_dedup_wiki.py").read_text(
        encoding="utf-8"
    )
    generated = {"path": Path("daily-brief-2026-07-03.md"), "tags": ["daily-brief"]}
    eval_fixture = {"path": Path("eval-docker-layer-cache.md"), "tags": []}
    source = {"path": Path("wiki-0001.md"), "tags": ["repo/omb"]}

    assert dedup.source_memory_candidates([generated, eval_fixture, source]) == [source]
    assert "def source_memory_candidates" in dedup_text
    assert "def is_source_memory_candidate" in dedup_text
    assert "def is_generated_brief_note" in dedup_text
    assert "def is_internal_eval_fixture" in dedup_text
    assert 'GENERATED_BRIEF_TAG = "daily-brief"' in dedup_text
    assert 'INTERNAL_EVAL_FIXTURE_PREFIX = "eval-"' in dedup_text
    assert "test_parse_note_keeps_tags_for_source_memory_filter" in dedup_test_text
    assert "test_source_memory_candidates_exclude_generated_and_eval_notes" in dedup_test_text


def test_duplicate_gate_preserves_claim_value_transitions():
    mcp_text = MCP.read_text(encoding="utf-8")
    store_text = STORE.read_text(encoding="utf-8")
    store_test_text = STORE_INTEGRATION.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "fn claim_identity_value_overlap" in mcp_text
    assert "fn claim_axis_value_conflict" in mcp_text
    assert "fn claim_value_equivalent" in mcp_text
    assert "fn ratio_at_least" in mcp_text
    assert "(numerator as u128) * (min.1 as u128)" in mcp_text
    assert "(denominator as u128) * (min.0 as u128)" in mcp_text
    assert "numerator.saturating_mul(min.1)" not in mcp_text
    assert "denominator.saturating_mul(min.0)" not in mcp_text
    assert "duplicate_similarity_ratio_uses_exact_widened_products" in mcp_text
    assert mcp_text.count("!claim_axis_value_conflict(&note.front, existing_fm)") >= 4
    assert "claim_identity_value_overlap(&note.front, existing_fm)" in mcp_text
    assert "note_duplicate_gate_rejects_same_axis_conflicting_claim_value" in mcp_text
    assert "note_duplicate_gate_keeps_same_axis_status_value_changes" in mcp_text
    assert "note_duplicate_gate_rejects_conflict_even_when_another_claim_overlaps" in mcp_text
    assert "exact_title_duplicate_keeps_status_transition_when_body_overlaps" in mcp_text
    assert "probable_session_duplicate_keeps_claim_axis_value_transition" in mcp_text
    assert "embedding_duplicate_candidate_uses_claim_evidence_without_title_body_overlap" in mcp_text
    assert "embedding_duplicate_candidate_rejects_claim_axis_value_conflict" in mcp_text
    assert "claim_upsert_canonicalizes_axis_before_supersede" in store_test_text
    assert "delete_document_reseals_remaining_claim_history" in store_test_text
    assert "typed_claim_axis_node_ids" in store_text
    assert "refresh_claim_axis_projection" in store_text
    assert "SELECT DISTINCT subject, predicate FROM claim WHERE source_path = $1;" in store_text
    assert "re-seal remaining claims after document delete" in store_text
    assert "unseal latest remaining claim after document delete" in store_text
    assert "refresh claim graph projection after document delete" in store_text
    assert "raw witness: copy before distill" in store_test_text
    assert "raw witness: retain for 90 days" in store_test_text
    assert "Canonical `(subject, predicate)` is the identity" in docs["README.md"]
    assert "newer values supersede older rows" in docs["README.md"]
    assert "더 최신 `value`가 이전 행을 대체" in docs["README.ko.md"]
    assert "より新しい `value` が古い行を置き換え" in docs["README.ja.md"]


def test_origin_project_boundaries_protect_duplicate_and_relation_lanes():
    mcp_text = MCP.read_text(encoding="utf-8")
    store_text = STORE.read_text(encoding="utf-8")
    store_test_text = STORE_INTEGRATION.read_text(encoding="utf-8")
    context_test_text = CONTEXT_INTEGRATION.read_text(encoding="utf-8")
    dedup_text = DEDUP_WIKI.read_text(encoding="utf-8")
    dedup_test_text = (ROOT / "scripts" / "test_dedup_wiki.py").read_text(
        encoding="utf-8"
    )
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "fn duplicate_boundary_compatible" in mcp_text
    assert "duplicate_project_compatible(&a.project, &b.project)" in mcp_text
    assert "duplicate_origin_compatible(&a.origin, &b.origin)" in mcp_text
    assert "fn duplicate_origin_key" in mcp_text
    assert "config::Origin::Personal.as_str()" in mcp_text
    assert "note_duplicate_gate_keeps_cross_project_notes" in mcp_text
    assert "note_duplicate_gate_keeps_cross_origin_notes" in mcp_text
    assert "note_duplicate_gate_treats_missing_candidate_origin_as_personal" in mcp_text
    assert "exact_title_duplicate_keeps_cross_project_notes" in mcp_text
    assert "duplicate_gate_reports_invalid_candidate_origin" in mcp_text
    assert "quality_gate_consumption_tools_expose_origin_exclusion" in mcp_text
    assert "mcp_exclude_origins_arg_defaults_and_reuses_array_parser" in mcp_text
    assert "def duplicate_boundary_compatible" in dedup_text
    assert "project_compatible(a, b) and origin_compatible(a, b)" in dedup_text
    assert 'return origin or "personal"' in dedup_text
    assert "test_parse_note_rejects_non_string_origin" in dedup_test_text
    assert "test_cluster_notes_keeps_cross_origin_duplicates_apart" in dedup_test_text

    assert store_text.count("Projection candidates stay inside the source document's origin boundary.") >= 4
    assert "one answer, one consistent origin boundary" in store_text
    assert "Empty legacy origins are normalized to the same default-personal key" in store_text
    assert "Claims carry no origin column, but their parent document does" in store_text

    assert "current_claims_honors_exclude_origins" in store_test_text
    assert "related_doc_content_respects_origin_project_and_internal_fixture_boundaries" in store_test_text
    assert "search_filters_excluded_origins_before_limit" in store_test_text
    assert "search_treats_missing_origin_as_personal_for_exclusion" in store_test_text
    assert "duplicate_boundary_nearest_filters_candidates_before_limit" in store_test_text
    assert "duplicate_boundary_treats_missing_origin_as_personal" in store_test_text
    assert "semantic_related_docs_requires_project_or_graph_evidence" in store_test_text
    assert "context_card_excludes_origins" in context_test_text
    assert "project and origin compatibility" in docs["README.md"]
    assert (
        "Missing or blank project frontmatter is treated as absent and derived from the file path"
        in docs["README.md"]
    )
    assert "project 및 origin 호환성" in docs["README.ko.md"]
    assert "누락되었거나 빈 project frontmatter는 호환성 검사 전에 없는 값으로 보고 파일 경로에서 도출" in docs[
        "README.ko.md"
    ]
    assert "project および origin の互換性" in docs["README.ja.md"]
    assert "欠落または空の project frontmatter は互換性チェック前に欠落として扱い" in docs["README.ja.md"]


def test_ingest_worker_requires_observable_success_before_done_marker():
    worker_text = HERMES_INGEST_WORKER.read_text(encoding="utf-8")
    test_text = TEST_INGEST_WORKER.read_text(encoding="utf-8")
    marker_text = (ROOT / "agents" / "shared" / "markers.py").read_text(encoding="utf-8")
    marker_test_text = TEST_MARKERS.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "A session is marked done only" in worker_text
    assert "after the agent's note is actually observed in vault/wiki" in worker_text
    assert "not in fresh retry state" in worker_text
    assert "wiki-first mode has no chunk counter" in worker_text
    assert "We do not mark unconfirmed sessions done." in worker_text
    assert "markers.is_retry(sid, ttl=RETRY_TTL)" in worker_text
    assert "PRIMARY: per-session idempotency" in worker_text
    assert "markers.mark_done(sid)" in worker_text
    assert "witness=\"missing_note\"" in worker_text
    assert "without observable confirmation — leaving retry marker; not marking done." in worker_text
    assert "markers.mark_retry(sid)" in worker_text
    assert "markers.remove_pending(sid)" in worker_text
    assert "os.remove(pend)" not in worker_text
    assert 'GENERATED_BRIEF_TAG = "daily-brief"' in worker_text
    assert "def _frontmatter(" in worker_text
    assert "def _frontmatter_generated_brief" in worker_text
    assert "if front is None or _frontmatter_generated_brief(front):" in worker_text
    assert "pending_marker_unreadable" in worker_text
    assert "after = _chunk_count()" in worker_text
    assert "before is not None and after is not None and after > before" in worker_text
    assert 'get("total_chunks", -1)' not in worker_text
    assert 'get("total_chunks")' in worker_text
    assert "return -1" not in worker_text
    assert "before = int(before_raw) if before_raw else None" in marker_text
    assert "if len(parts) != 3:" in marker_text
    assert "if (before is not None and before < 0) or attempts < 0:" in marker_text
    assert "def _transition_marker" in marker_text
    assert "_write_marker(target, text)" in marker_text
    assert "tempfile.NamedTemporaryFile" in marker_text
    assert "os.replace(tmp_path, target)" in marker_text
    assert 'open(path, "w", encoding="utf-8")' not in marker_text
    assert "Path(path).unlink(missing_ok=True)" in marker_text

    assert "test_vector_mode_prefers_session_marker_over_chunk_count" in test_text
    assert "test_vector_mode_falls_back_to_chunk_count" in test_text
    assert "test_vector_mode_does_not_complete_from_missing_chunk_baseline" in test_text
    assert "test_vector_mode_rejects_negative_chunk_baseline" in test_text
    assert "test_chunk_count_missing_total_chunks_is_absent_witness" in test_text
    assert "test_find_session_note_ignores_generated_brief_marker" in test_text
    assert "test_wiki_mode_uses_session_marker" in test_text
    assert "test_mismatched_pending_marker_identity_is_corrupt_not_done" in test_text
    assert "test_corrupt_pending_marker_remove_failure_is_visible" in test_text
    assert "test_extra_field_pending_marker_is_corrupt_not_done" in test_text
    assert "test_wiki_mode_increments_attempts_without_marker" in test_text
    assert "test_wiki_mode_moves_pending_to_retry_after_max_attempts" in test_text
    assert "test_fresh_retry_marker_is_not_reoffered" in test_text
    assert "test_stale_retry_marker_is_reoffered" in test_text
    assert "test_health_failure_treated_as_wiki_and_retries" in test_text
    assert "self.assertFalse(self._done_exists(\"s4\"))" in test_text
    assert "self.assertFalse(self._done_exists(\"s5\"))" in test_text
    assert "self.assertTrue(self._retry_exists(\"s5\"))" in test_text
    assert "test_read_ingest_pending_rejects_mismatched_body_session_id" in marker_test_text
    assert "test_ingest_pending_allows_missing_chunk_baseline" in marker_test_text
    assert "test_read_ingest_pending_rejects_negative_numbers" in marker_test_text
    assert "test_read_ingest_pending_rejects_extra_fields" in marker_test_text
    assert "test_marker_write_apis_raise_when_marker_dir_cannot_be_created" in marker_test_text
    assert "test_marker_write_uses_atomic_replace_without_temp_leftover" in marker_test_text
    assert "test_marker_write_preserves_existing_marker_on_replace_failure" in marker_test_text
    assert "test_mark_retry_raises_when_cleanup_remove_fails" in marker_test_text
    assert "test_remove_pending_raises_when_unlink_fails" in marker_test_text

    assert "marker files are atomically published" in docs["README.md"]
    assert "duplicate candidates, ingest confirmation markers, Obsidian relation projection, and DB ingest" in docs["README.md"]
    assert "ingest `.pending` files must parse as exactly `session_id`, chunk baseline, and attempt count" in docs["README.md"]
    assert "marker 파일은 원자적으로 공개" in docs["README.ko.md"]
    assert "중복 후보, ingest 확인 마커, Obsidian relation projection, DB 적재" in docs["README.ko.md"]
    assert "ingest `.pending` 파일은 정확히 `session_id`, chunk baseline, attempt count로 해석" in docs["README.ko.md"]
    assert "marker ファイルはアトミックに公開" in docs["README.ja.md"]
    assert "重複候補、ingest 確認マーカー、Obsidian relation projection、DB 取り込み" in docs["README.ja.md"]
    assert "ingest `.pending` ファイルは正確に `session_id`、chunk baseline、attempt count として解釈" in docs["README.ja.md"]


def test_session_start_context_claims_are_fenced_as_data():
    start_text = CLAUDE_SESSION_START.read_text(encoding="utf-8")
    claude_test = CLAUDE_TEST_HOOKS.read_text(encoding="utf-8")

    assert "def _defang_context_field" in start_text
    assert "def _data_fence" in start_text
    assert "secrets.token_hex(8)" in start_text
    assert "Everything between {fence_open} and {fence_close}" in start_text
    assert "«UNTRUSTED-DATA {tag}»" in start_text
    assert '_defang_context_field(item.get("value", ""))' in start_text
    assert "test_session_start_defangs_context_claims_inside_data_fence" in claude_test
    assert 'self.assertIn("«UNTRUSTED-DATA ", ctx)' in claude_test
    assert 'self.assertIn(" # forged instructions", ctx)' in claude_test
    assert 'self.assertIn("(source: wiki-0010.md)", ctx)' in claude_test


def test_session_end_distill_marker_failures_stay_retry_visible():
    distill_text = DISTILL_CORE.read_text(encoding="utf-8")
    make_text = MAKEFILE.read_text(encoding="utf-8")
    collector_text = SCHEDULER_COLLECT.read_text(encoding="utf-8")
    collector_test_text = TEST_SCHEDULER_COLLECTORS.read_text(encoding="utf-8")
    shared_test = TEST_DISTILL_CORE.read_text(encoding="utf-8")
    claude_test = CLAUDE_TEST_HOOKS.read_text(encoding="utf-8")
    codex_test = CODEX_TEST.read_text(encoding="utf-8")
    kimi_test = KIMI_TEST.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert 'os.environ.get("BORING_DISTILL_NO_MARK")' in distill_text
    assert "distill-now:" in make_text
    assert "python3 agents/schedulers/collect-sessions.py --now" in make_text
    assert 'ap.add_argument(\n        "--now"' in collector_text
    assert "the MOST RECENT session immediately" in collector_text
    assert "normal SessionEnd capture still runs" in collector_text
    assert "batch = todo[:1] if args.now else todo[:LIMIT]" in collector_text
    assert 'label = "distill-now" if args.now else "collect"' in collector_text
    assert 'env["BORING_DISTILL_NO_MARK"] = "1"' in collector_text
    assert "markers.mark_retry(session_id)" in distill_text
    assert "markers.mark_done(session_id)" in distill_text
    assert "return True  # intentional skip" in distill_text
    assert "return remember.ok" in distill_text
    assert "return False" in distill_text

    assert "test_remember_failure_logs_failed_status" in shared_test
    assert "test_resolution_repair_failure_blocks_remember" in shared_test
    assert "remember.assert_not_called()" in shared_test
    assert "test_event_log_failure_does_not_override_remember_success" in shared_test
    assert "except (OSError, ValueError) as e" in distill_text
    assert "test_claude_distill_now_ignores_done_marker_and_leaves_no_mark_env" in collector_test_text
    assert 'mock.patch.object(claude_collect.sys, "argv", ["collect-sessions.py", "--now"])' in collector_test_text
    assert 'run.call_args.kwargs["env"]["BORING_DISTILL_NO_MARK"] == "1"' in collector_test_text
    assert 'payload["session_id"] == "s-new"' in collector_test_text
    assert 'event["mode"] == "distill-now"' in collector_test_text

    assert "test_remember_failure_returns_nonzero_and_marks_retry" in claude_test
    assert 'mark.assert_called_once_with("abc", retry=True)' in claude_test
    assert "test_distill_remember_failure_returns_nonzero_and_marks_retry" in kimi_test
    assert 'mark.assert_called_once_with("session_abc", retry=True)' in kimi_test
    assert "test_large_raw_parse_short_marks_retry" in codex_test
    assert '"marked for retry"' in codex_test
    assert 'mark.assert_called_once_with("codex-abc", retry=True)' in codex_test
    assert "test_small_raw_parse_short_marks_done" in codex_test

    assert "leaves no marker" in docs["README.md"]
    assert "마커를 남기지 않으므로" in docs["README.ko.md"]
    assert "マーカーを残さないため" in docs["README.ja.md"]


def test_claude_collect_backfill_contract_stays_lazy_and_idempotent():
    make_text = MAKEFILE.read_text(encoding="utf-8")
    collector_text = SCHEDULER_COLLECT.read_text(encoding="utf-8")
    collector_test_text = TEST_SCHEDULER_COLLECTORS.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    assert "collect: ## Lazily collect past Claude Code sessions" in make_text
    assert "COLLECT_LIMIT=$${N:-1} python3 agents/schedulers/collect-sessions.py" in make_text
    assert "Lazy backfill collector" in collector_text
    assert "LIMIT of **only the not-yet-done ones**" in collector_text
    assert "number processed per invocation" in collector_text
    assert "Called periodically via launchd/cron" in collector_text
    assert 'LIMIT = omb_env.env_positive_int("COLLECT_LIMIT", 1)' in collector_text
    assert "def _marked(session_id)" in collector_text
    assert "markers.is_done(session_id) or markers.is_pending" in collector_text
    assert "Retry markers are intentionally eligible for backfill" in collector_text
    assert "todo.sort(key=os.path.getmtime, reverse=True)" in collector_text
    assert "batch = todo[:1] if args.now else todo[:LIMIT]" in collector_text
    assert "test_claude_collector_skips_done_markers_and_batches_newest_first" in collector_test_text
    assert '["s-newest", "s-middle"]' in collector_test_text
    assert '"BORING_DISTILL_NO_MARK" not in call.kwargs["env"]' in collector_test_text
    assert 'event["pending"] == 3' in collector_test_text
    assert 'event["batch"] == 2' in collector_test_text
    assert 'event["remaining"] == 1' in collector_test_text

    assert "Newest-first, idempotent (a per-session marker skips already-distilled ones)" in docs["README.md"]
    assert "`N` per run so it never hogs CPU" in docs["README.md"]
    assert "최신순, 멱등(세션별 마커로 이미 증류한 건 건너뜀)" in docs["README.ko.md"]
    assert "한 번에 `N`개만 처리해 CPU를 독점하지 않음" in docs["README.ko.md"]
    assert "新しい順・冪等" in docs["README.ja.md"]
    assert "1 回に `N` 件だけ処理し CPU を占有しません" in docs["README.ja.md"]


def test_related_doc_graph_lane_excludes_claim_axis():
    text = STORE.read_text(encoding="utf-8")
    ask_text = ASK.read_text(encoding="utf-8")
    store_intro = "\n".join(text.splitlines()[:20])

    assert "problem|solution|tool|concept" not in store_intro
    assert "attempt:<path>#<idx>" not in store_intro
    assert "node id convention" in store_intro
    assert "`tool:<slug>`" in store_intro
    assert "`concept:<slug>`" in store_intro
    assert "`claim:<subject>:<predicate>`" in store_intro
    assert "decision|risk|assumption|blocked|goal|term|next:<subject>:<predicate>" in store_intro
    assert "Semantic neighbors (problem/solution/tool/concept/attempt)" not in text
    assert "Semantic neighbors (tool/concept/claim)" in text
    assert rust_string_array_const(STORE, "RELATED_DOC_EDGE_KINDS") == ["uses", "about"]
    assert rust_string_array_const(STORE, "SEMANTIC_EDGE_KINDS") == [
        "uses",
        "about",
        "claims",
    ]
    assert text.count("let edge_kinds = RELATED_DOC_EDGE_KINDS.to_vec();") == 2
    assert text.count("AND kind = ANY($3)") >= 2
    assert text.count("AND e.kind = sn.kind") == 2
    assert "pub struct RelatedEvidence" in text
    assert "pub enum RelatedEvidenceKind" in text
    assert "claim_related_doc_content" in text
    assert "pub struct RelatedDoc" in text
    assert "shared_nodes: Vec<String>" in text
    assert text.count("count(DISTINCT e.dst) AS shared") == 3
    assert text.count("HAVING count(DISTINCT e.dst) >= 2") == 1
    assert "count(DISTINCT COALESCE(n.label, sn.dst)) AS shared" in text
    assert "HAVING count(DISTINCT COALESCE(n.label, sn.dst)) >= 2" in text
    assert "DISTINCT COALESCE(n.label, sn.dst)" in text
    assert "ORDER BY COALESCE(n.label, sn.dst)" in text
    assert "claim related doc content" in text
    assert "fn prompt_meta_field" in ask_text
    assert "format_related_evidence" in ask_text
    assert "shares {} {}" in ask_text
    assert "claim axes" in ask_text
    assert "[Relation metadata]" in ask_text
    assert "not as a standalone memory fact" in ask_text
    assert "not as a fresh work item" in ask_text


def test_mcp_vector_only_tools_gate_on_store():
    """README lists vector-only tools; each mcp.rs handler gates on store availability."""
    mcp_text = MCP.read_text(encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")

    vector_only = [
        "neighbors",
        "claims",
        "corpus_status",
        "events",
        "brief",
        "weekly_brief",
        "project_status",
        "decisions",
        "risks",
        "next_actions",
        "stalled",
    ]

    # README documents the vector-only list and the -32603 behavior.
    assert (
        "tools that rely on recency/vector ordering, the graph, or the local event DB return JSON-RPC `-32603`"
        in readme_text
    )
    for tool in vector_only:
        assert f"`{tool}`" in readme_text, f"README misses vector-only tool {tool}"

    # Each implementation checks the store before use.
    for tool in vector_only:
        block = rust_function_block(mcp_text, f"mcp_{tool}")
        assert (
            "s.store.as_ref().ok_or_else(vec_off_rpc)?" in block
        ), f"mcp_{tool} must gate on store availability"


def test_ollama_lmstudio_provider_contract_matches_verify_llm():
    """Provider bootstrap scripts and verify-llm share the endpoint/transform contract."""
    ollama_text = (ROOT / "scripts" / "llm-providers" / "ollama.sh").read_text(
        encoding="utf-8"
    )
    lmstudio_text = (ROOT / "scripts" / "llm-providers" / "lmstudio.sh").read_text(
        encoding="utf-8"
    )
    openai_text = (ROOT / "scripts" / "llm-providers" / "openai-compatible.sh").read_text(
        encoding="utf-8"
    )
    verify_text = VERIFY_LLM.read_text(encoding="utf-8")
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }

    # Ollama native API lives at /api/tags (no /v1), and host.docker.internal is rewritten to localhost.
    assert "${BASE_URL%/v1}" in ollama_text
    assert "host\\.docker\\.internal" in ollama_text
    assert "localhost" in ollama_text
    assert "api/tags" in ollama_text

    # LM Studio exposes OpenAI-compatible /v1/models.
    assert "host\\.docker\\.internal" in lmstudio_text
    assert "localhost" in lmstudio_text
    assert "/models" in lmstudio_text

    # verify-llm dispatches to provider scripts, probes /api/tags and /v1/models,
    # and checks the actual embedding dimension against llm.embed_dim.
    assert "scripts/llm-providers/${PROVIDER}.sh" in verify_text
    assert "/api/tags" in verify_text
    assert "/models" in verify_text
    assert "/embeddings" in verify_text
    assert "actual embedding dimension" in verify_text
    assert "host\\.docker\\.internal" in verify_text
    assert "localhost" in verify_text
    assert "lmstudio|openai-compatible" in verify_text

    # openai-compatible provider exists and follows the same /v1 surface.
    assert "/models" in openai_text
    assert "host\\.docker\\.internal" in openai_text
    assert "localhost" in openai_text

    # All three providers are selectable in boring.json.
    for text in docs.values():
        assert "`ollama`" in text
        assert "`lmstudio`" in text
        assert "`openai-compatible`" in text


def test_lmstudio_runbook_matches_verify_llm_contract():
    """LM Studio runbook documents the same checkpoints verify-llm enforces."""
    runbooks = {
        "lmstudio.md": (ROOT / "docs" / "runbooks" / "lmstudio.md").read_text(encoding="utf-8"),
        "lmstudio.ko.md": (ROOT / "docs" / "runbooks" / "lmstudio.ko.md").read_text(
            encoding="utf-8"
        ),
        "lmstudio.ja.md": (ROOT / "docs" / "runbooks" / "lmstudio.ja.md").read_text(
            encoding="utf-8"
        ),
    }
    verify_text = VERIFY_LLM.read_text(encoding="utf-8")

    assert "/models" in verify_text
    assert "/embeddings" in verify_text
    assert "embed_dim" in verify_text

    for label, text in runbooks.items():
        assert "/v1/models" in text, f"{label} misses /v1/models"
        assert "/v1/embeddings" in text, f"{label} misses /v1/embeddings"
        assert "embed_dim" in text, f"{label} misses embed_dim"
        assert "host.docker.internal" in text, f"{label} misses host.docker.internal"
        assert "make verify-llm" in text, f"{label} misses make verify-llm"
        assert "make up" in text, f"{label} misses make up"
        assert "make doctor" in text, f"{label} misses make doctor"
        assert "make readiness" in text, f"{label} misses make readiness"


def test_ollama_runbook_matches_provider_contract():
    """Ollama runbook documents the same checkpoints verify-llm and make ollama enforce."""
    runbooks = {
        "ollama.md": (ROOT / "docs" / "runbooks" / "ollama.md").read_text(encoding="utf-8"),
        "ollama.ko.md": (ROOT / "docs" / "runbooks" / "ollama.ko.md").read_text(
            encoding="utf-8"
        ),
        "ollama.ja.md": (ROOT / "docs" / "runbooks" / "ollama.ja.md").read_text(
            encoding="utf-8"
        ),
    }
    verify_text = VERIFY_LLM.read_text(encoding="utf-8")

    assert "/models" in verify_text
    assert "/embeddings" in verify_text
    assert "embed_dim" in verify_text

    for label, text in runbooks.items():
        assert "ollama" in text.lower(), f"{label} misses ollama"
        assert "/api/tags" in text, f"{label} misses /api/tags"
        assert "embed_dim" in text, f"{label} misses embed_dim"
        assert "host.docker.internal" in text, f"{label} misses host.docker.internal"
        assert "make verify-llm" in text, f"{label} misses make verify-llm"
        assert "make ollama" in text, f"{label} misses make ollama"
        assert "make doctor" in text, f"{label} misses make doctor"
        assert "make readiness" in text, f"{label} misses make readiness"


def test_graphrag_runbook_matches_implementation_contract():
    """GraphRAG runbook documents the vector/graph contract and current 1-hop limits."""
    runbooks = {
        "graphrag.md": (ROOT / "docs" / "runbooks" / "graphrag.md").read_text(encoding="utf-8"),
        "graphrag.ko.md": (ROOT / "docs" / "runbooks" / "graphrag.ko.md").read_text(
            encoding="utf-8"
        ),
        "graphrag.ja.md": (ROOT / "docs" / "runbooks" / "graphrag.ja.md").read_text(
            encoding="utf-8"
        ),
    }
    for label, text in runbooks.items():
        assert "BORING_VECTOR=on" in text, f"{label} misses BORING_VECTOR=on"
        assert "embed_dim" in text, f"{label} misses embed_dim"
        assert "make reset" in text, f"{label} misses make reset"
        assert "make sync" in text, f"{label} misses make sync"
        assert "make eval-graphrag" in text, f"{label} misses make eval-graphrag"
        assert "graph_context_chars" in text, f"{label} misses graph_context_chars"
        assert "graph_source_count" in text, f"{label} misses graph_source_count"
        assert "1-hop" in text or "multi-hop" in text, f"{label} misses hop scope note"
        assert "reranker" in text.lower(), f"{label} misses reranker note"


def test_readme_openai_compatible_provider_is_documented_consistently():
    """README explains that openai-compatible works but is not an officially supported backend."""
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    verify_text = VERIFY_LLM.read_text(encoding="utf-8")

    # Code supports three providers; verify-llm handles lmstudio and openai-compatible together.
    assert "lmstudio|openai-compatible" in verify_text

    for label, text in docs.items():
        assert "`ollama`" in text, f"{label} misses ollama provider"
        assert "`lmstudio`" in text, f"{label} misses lmstudio provider"
        assert "`openai-compatible`" in text, f"{label} misses openai-compatible provider"
        # The README must not imply only two providers are selectable.
        assert "only two" not in text, f"{label} implies only two providers"
        # The README must call out that Ollama and LM Studio are officially supported.
        assert "Ollama" in text and "LM Studio" in text, f"{label} misses supported backend names"


def test_graph_projection_contract_matches_readme():
    """Graph projection implementation matches the README Graph contract prose."""
    projection_text = VAULT_PROJECTION.read_text(encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")

    # README describes the four projection lanes and the cap.
    assert "claim continuity" in readme_text
    assert "exact tool/concept overlap" in readme_text
    assert "corroborated semantic neighbors" in readme_text
    assert "same-project recency fallback" in readme_text
    assert "capped so hub notes do not explode into a mesh" in readme_text

    # projection.rs has matching constants and implementation order.
    assert "const CLAIM_RELATED_LIMIT" in projection_text
    assert "const SEMANTIC_RELATED_LIMIT" in projection_text
    assert "const PROJECT_RECENCY_LINK_MIN" in projection_text
    assert "const PROJECT_RECENCY_LIMIT" in projection_text
    assert "const PROJECT_RELATED_LINK_CAP" in projection_text
    assert "stems.truncate(PROJECT_RELATED_LINK_CAP)" in projection_text

    # Claim axis first, then exact graph, then semantic, then recency fallback.
    claim_idx = projection_text.index("claim_related_docs")
    graph_idx = projection_text.index("store.related_docs")
    semantic_idx = projection_text.index("semantic_related_docs")
    recent_idx = projection_text.index("recent_project_docs")
    assert claim_idx < graph_idx < semantic_idx < recent_idx, (
        "projection.rs lanes must follow claim → graph → semantic → recency order"
    )

    # Seed-note id leak guard and the don't-wipe rule.
    assert "is_seed_note" in projection_text
    assert "Never rewrite the tracked seed note" in projection_text
    assert "Don't wipe" in projection_text
    assert "if links.is_empty()" in projection_text

    # remember fast-path calls project_note for immediate relates_to projection.
    mcp_text = MCP.read_text(encoding="utf-8")
    remember_block = rust_function_block(mcp_text, "finish_remembered_note")
    assert "vault::project_note(store, path, 6)" in remember_block


def test_graphrag_eval_artifacts_exist():
    """GraphRAG A/B eval harness files are present and executable."""
    assert GRAPH_GOLDEN.exists(), "graph-golden.json must exist"
    assert RUN_GRAPH_EVAL.exists(), "run_graph_eval.py must exist"
    assert EVAL_GRAPHRAG_GATE.exists(), "eval-graphrag-gate.sh must exist"
    assert EVAL_GRAPHRAG_GATE.stat().st_mode & 0o111, "eval-graphrag-gate.sh must be executable"


def test_graphrag_readme_contract():
    """README documents the GraphRAG eval gate and its A/B contract."""
    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    for label, text in docs.items():
        assert "`make eval-graphrag`" in text, f"{label} misses make eval-graphrag"
        assert "graph-only" in text.lower() or "graph-only rescue" in text.lower(), (
            f"{label} misses graph-only rescue concept"
        )
        assert "vector + graph" in text.lower(), f"{label} misses vector + graph description"
        assert "docs/runbooks/graphrag" in text, f"{label} misses GraphRAG runbook link"


def test_graphrag_query_log_telemetry_contract():
    """Graph context telemetry flows from ask.rs through serve.rs into query_log."""
    ask_text = ASK.read_text(encoding="utf-8")
    http_text = HTTP.read_text(encoding="utf-8")
    store_text = STORE.read_text(encoding="utf-8")
    serve_text = SERVE.read_text(encoding="utf-8")

    assert "pub graph_context_chars: usize" in ask_text
    assert "pub graph_source_count: usize" in ask_text
    assert "graph_context_chars: graph_ctx.chars().count()" in ask_text
    assert "graph_source_count: graph_sources.len()" in ask_text
    assert "meta: Option<Value>" in serve_text
    assert "graph_context_chars" in http_text
    assert "graph_source_count" in http_text
    assert "meta          jsonb" in store_text
    assert "INSERT INTO query_log (endpoint, query, hit_paths, sources, answer_snippet, latency_ms, meta)" in store_text


def test_makefile_eval_graphrag_target_calls_gate_script():
    """Makefile exposes eval-graphrag and wires it to the gate script."""
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    assert "eval-graphrag:" in makefile_text
    assert "scripts/eval-graphrag-gate.sh" in makefile_text
    gate_text = EVAL_GRAPHRAG_GATE.read_text(encoding="utf-8")
    assert "run_graph_eval.py" in gate_text
    assert "graph-golden.json" in gate_text


def test_readme_inline_make_targets_exist_in_makefile():
    """Every inline `make <target>` in README locale files references an existing Makefile target."""
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    target_re = re.compile(r"^([a-zA-Z0-9_-]+):.*##", re.M)
    makefile_targets = {m.group(1) for m in target_re.finditer(makefile_text)}

    docs = {
        "README.md": README.read_text(encoding="utf-8"),
        "README.ko.md": README_KO.read_text(encoding="utf-8"),
        "README.ja.md": README_JA.read_text(encoding="utf-8"),
    }
    for name, text in docs.items():
        inline_targets = set(re.findall(r"`?make\s+([a-zA-Z0-9_-]+)`?", text))
        missing = inline_targets - makefile_targets
        assert not missing, f"{name} references inline make targets missing from Makefile: {sorted(missing)}"


def test_hermes_cron_jobs_is_known_config_field():
    """hermes_cron_jobs is a recognized top-level boring.json field so drudge does not warn."""
    config_text = CONFIG.read_text(encoding="utf-8")
    assert '"hermes_cron_jobs"' in config_text
    # It must appear in the known-top-level list, not only as a struct field.
    known_block = config_text.split("KNOWN_TOP_LEVEL:")[1]
    assert '"hermes_cron_jobs"' in known_block.split("];")[0]


def test_briefing_scripts_self_contained_import_path():
    """Cron may run briefing scripts from any cwd; they add their own dir to sys.path."""
    for path in (ROOT / "agents" / "hermes" / "briefing.py", ROOT / "agents" / "hermes" / "weekly-briefing.py"):
        text = path.read_text(encoding="utf-8")
        assert "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))" in text, (
            f"{path.name} must add its own directory to sys.path for cron imports"
        )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ok - guard contract")
