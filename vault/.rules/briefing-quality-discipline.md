# 브리핑 품질 규율 — Briefing Quality Discipline

> oh-my-boring 의 일간/주간 브리핑은 **읽는 사람이 하루/한 주를 한눈에 파악**할 수 있어야 한다.
> 이 문서는 브리핑 출력물이 반드시 지켜야 할 품질 계약(contract)과, 그 계약을 자동으로 측정/감사하는 방법을 정의한다.

---

## 1. 계약 개요

| ID | Contract | 위반 시 증상 | 측정 방법 |
|---|---|---|---|
| Q01 | **No empty fallback noise** | 엔진이 "No related memory found" 같은 fallback 을 본문에 그대로 출력 | `EMPTY_ANSWER_PATTERNS` 매칭 → empty 처리 |
| Q02 | **No duplicate items** | 같은 내용이 여러 프로젝트/라벨 아래 반복 | dedup key + fuzzy similarity 로 중복율 측정 |
| Q03 | **Correct grouping** | 항목이 Blocked/Next/Done 등 상태별로 모이지 않고 흩어짐 | 섹션별 항목 분포 및 미분류 비율 측정 |
| Q04 | **Date window compliance** | since_hours/until_hours 이 의도한 기간을 벗어남 | daily=24h, weekly=지난 주 Mon 00:00~Sun 23:59:59 KST 검증 |
| Q05 | **No placeholder bullets** | "다음 지시 기다림", "없음", "-" 같은 공허한 항목 | `EMPTY_VALUES` + `TEMPLATE_BLACKLIST` 제거율 측정 |
| Q06 | **Deduplicated sources** | 같은 wiki 를 chunk/path 변형으로 중복 나열 | source basename 으로 dedup |
| Q07 | **Readable length** | 한 항목이 240자를 넘거나, 한 섹션에 지나치게 많은 항목 | truncation / 섹션 cap 적용 여부 검증 |
| Q08 | **Langchain/LangGraph contract aware** | GraphRAG relation metadata 가 브리핑 본문으로 새어 나옴 | relation metadata 제거율 측정 |

---

## 2. 품질 메트릭 정의

`agents/hermes/briefing_quality.py` 가 다음 메트릭을 계산한다.

### 2.1 `empty_fallback_detected` (boolean)
원본 `answer` 가 `EMPTY_ANSWER_PATTERNS` 에 매칭되면 `True`.  
이는 엔진이 데이터를 찾지 못한 경우이며, 브리핑은 빈 본문 + empty message 를 출력해야 한다.

### 2.2 `duplicate_item_rate` (float 0..1)
```
duplicate_item_rate = (원본 item 수 - dedup 후 item 수) / 원본 item 수
```
허용 한계: **≤ 0.20** (20% 이하).  
같은 사실이 여러 프로젝트/상태에서 반복되는 것은 정상적일 수 있으나, 그 비율이 20% 를 넘으면 중복 제거 실패로 본다.

### 2.3 `ungrouped_item_rate` (float 0..1)
```
ungrouped_item_rate = label이 ""(기타) 인 item 수 / 전체 item 수
```
허용 한계: **≤ 0.30** (30% 이하).  
LLM 이 인식하지 못하는 라벨은 기타로 모인다. 과도하면 라벨 alias 계약이 깨진 것이다.

### 2.4 `placeholder_item_rate` (float 0..1)
```
placeholder_item_rate = 제거된 placeholder/noise item 수 / 원본 item 수
```
허용 한계: **≤ 0.50** (50% 이하).  
Placeholder 가 50% 를 넘으면 엔진 prompt 가 쓸모 없는 응답을 만들고 있음을 의미한다.

### 2.5 `source_dedup_rate` (float 0..1)
```
source_dedup_rate = (원본 source 수 - dedup 후 source 수) / 원본 source 수
```
허용 한계: **≤ 0.50**.  
Chunk/path 변형으로 인한 중복은 정상이지만 50% 를 넘으면 source 렌더링 계약 문제다.

### 2.6 `rendered_section_balance` (dict)
각 섹션별 항목 수.  
- `Blocked` 가 0 이고 `Next` 가 0 인 브리핑은 "할 일 없음"으로 처리할 수 있다.
- `Done` 이 전체의 80% 이상이면 브리핑이 사후 정리 보고가 된 것이므로 주의.

### 2.7 `date_window_compliant` (boolean)
- Daily: `since_hours == 24` (KST 기준 어제 00:00~오늘 00:00 범위를 커버).
- Weekly: `until_hours` 가 존재하고, `since_hours - until_hours == 168` (7일) 이어야 한다.  
  (엔진이 상한을 적용할 수 있도록 since/until 모두 전달.)

### 2.8 `max_item_length_ok` (boolean)
모든 렌더링된 항목이 `ITEM_TEXT_MAX_CHARS` (240자) 이하이면 `True`.

---

## 3. 품질 게이트 (Quality Gate)

`briefing_quality.py::check_briefing_quality()` 는 위 메트릭을 종합하여 다음 중 하나를 반환한다.

| Level | 조건 | 조치 |
|---|---|---|
| `pass` | 모든 메트릭이 허용 한계 이내 | 정상 출력 |
| `warn` | empty_fallback, placeholder_item_rate > 0.30, max_item_length_ok == False, 그 외 1건 초과 | 출력은 하되, `stderr` 에 경고 로그 |
| `fail` | duplicate_item_rate > 0.20, ungrouped_item_rate > 0.30, placeholder_item_rate > 0.50, source_dedup_rate > 0.50, done_dominance > 0.80, date_window_compliant == False 중 하나 이상 | 출력을 중단하고 엔진/prompt 점검 메시지를 반환 |

---

## 4. 자율 감사 루프

1. cron 이 `briefing.py` / `weekly-briefing.py` 를 실행한다.
2. 두 스크립트는 출력 전 `check_briefing_quality()` 를 호출한다.
3. `fail` 이면:
   - Slack 으로는 `"브리핑 품질 계약 위반 — 엔진/prompt 점검 필요"` 메시지를 본문 대신 전송.
   - `stderr` 에 상세한 metrics JSON 과 위반 항목을 기록.
   - retry marker 를 남겨 다음 사이클에서 재시도.
4. `warn` 이면:
   - 브리핑은 정상 전송.
   - `stderr` 에 경고를 기록하여 추이(trend)를 모니터링.
5. 매주 `data-steward.py` dry-run 시 `briefing_quality.py` 의 최근 로그를 요약하여 weak-claim 리포트와 함께 출력.

---

## 5. Ollama / LM Studio / GraphRAG 관련 특수 계약

### 5.1 임베딩/LLM 공급자 fallback
- Ollama 나 LM Studio 모델이 unload 되어 있으면 엔진이 fallback 메시지를 반환할 수 있다.
- 브리핑 렌더러는 이를 **데이터 없음**으로 처리해야 하며, "No related memory found" 를 사용자에게 그대로 보여서는 안 된다.

### 5.2 GraphRAG relation metadata
- LangChain/LangGraph 기반 GraphRAG 는 검색 결과에 `shares N graph nodes` 나 `related to vault/wiki/...` 같은 relation metadata 를 포함할 수 있다.
- 이 메타데이터는 브리핑 항목으로 표시되지 않아야 하며, `_is_relation_metadata()` 로 필터링한다.

### 5.3 Multi-hop / reranker
- GraphRAG multi-hop traversal 의 결과가 브리핑에 반영될 때는, 각 항목이 **최종적으로 선택된 source** 에 근거해야 한다.
- Reranker 에 의해 낮은 순위로 밀린 항목은 브리핑에 노출되지 않아야 한다.

---

## 6. 문서 정합성

이 규율은 다음 문서/코드와 함께 유지보수된다.

- `agents/hermes/slack_briefing.py` — 렌더링 및 dedup 로직
- `agents/hermes/briefing_quality.py` — 본 규율의 측정 구현
- `agents/hermes/test_briefing_quality.py` — 계약 테스트
- `docs/runbooks/graphrag.md` 및 번역본 — GraphRAG multi-hop/reranker 설명
- `vault/.rules/frontmatter.md` — source title 추출 규약
- `vault/.rules/session-telemetry.md` — 품질 메트릭 telemetry

---

## 7. 변경 시 체크리스트

브리핑 품질 규율을 변경하려면:

1. 이 문서의 메트릭/한계 값을 갱신.
2. `briefing_quality.py` 의 `QUALITY_CONTRACT` 상수를 동기화.
3. `test_briefing_quality.py` 에 경계값 테스트 추가.
4. `scripts/guard.sh` 의 Python unit test 목록에 `agents/hermes/test_briefing_quality.py` 가 포함되어 있는지 확인.
5. `make eval` 과 `python3 agents/hermes/test_briefing_quality.py` 통과 확인.
6. 72-cycle self-verify soak 한 번 이상 통과 확인.
