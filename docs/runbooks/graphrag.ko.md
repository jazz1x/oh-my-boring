# GraphRAG & Vector 계약 런북

## 목적

pgvector 기반 GraphRAG 모드를 언제, 어떻게 켜는지, 그래프/벡터 계약이 어떤 보장을 주는지, 그리고 건강 상태를 어떻게 검증하는지 설명합니다.

## 사전 조건

- `BORING_VECTOR=on`이 설정되어 있어야 합니다(환경 변수 또는 `boring.json` 기본값).
- pgvector가 있는 Postgres가 실행 중이어야 합니다(Docker Compose를 쓰면 `make up`이 시작합니다).
- `llm.embed_model`과 `llm.embed_dim`이 로컬로 서빙 중인 embedding 모델과 일치해야 합니다.
- `make verify-llm`이 통과해야 합니다.

## 설정

`boring.json`에서 vector 모드를 켭니다:

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

또는 런타임에 덮어씁니다:

```bash
BORING_VECTOR=on make up
```

vector 모드가 켜지면 `make sync`가 `vault/wiki`를 임베딩과 그래프 엣지로 다시 적재합니다. 꺼져 있으면 엔진은 wiki-first입니다: `/ask`, `/search`, `recall`, `context`는 마크다운을 직접 읽고, graph/claim 엔드포인트는 명시적 오류를 반환합니다.

## 벡터 계약

- `llm.embed_dim`은 `llm.embed_model`의 `/v1/embeddings`가 실제로 반환하는 차원과 같아야 합니다.
- embedding 모델을 바꾸면 `llm.embed_dim`을 갱신하고 **`make reset`을 실행해야 합니다**. vector 테이블 형태가 바뀌기 때문입니다.
- `make verify-llm`이 `/v1/embeddings`를 호출해 반환된 길이와 `llm.embed_dim`을 비교합니다.
- embedding 모델만 저장소에 차원이 연결된 유일한 모델입니다. synthesis 모델(`llm.model`)은 자유롭게 바꿀 수 있습니다.

## 그래프 계약

- 그래프는 결정론적입니다. `tool`, `concept`, `claim` 노드는 `drudge` 내부의 추가 LLM 추출이 아니라 에이전트가 정리한 노트 frontmatter에서 옵니다.
- `relates_to` 링크는 다음 순서로 투영됩니다:
  1. 클레임 연속성(정규화된 `(subject, predicate)` 축).
  2. 정확한 도구/개념 겹침.
  3. 증거가 있는 의미 이웃.
  4. 작은 동일 프로젝트 최신성 보강.
- 소스별 상한으로 허브 노트가 과도한 그물망이 되지 않도록 막습니다.
- `remember`가 노트를 쓰면 그 노트의 `relates_to` 투영은 즉시 갱신됩니다. 이웃 backlink는 다음 `make sync` / 전체 `project_links`에서 조정되므로, 회수는 즉시 가능하고 Obsidian 링크만 eventual consistency입니다.

## GraphRAG 구현

`BORING_VECTOR=on`일 때 `/ask`는 로컬 GraphRAG를 실행합니다:

1. 상위 vector + BM25 RRF hit를 잡습니다.
2. 공유 `uses`/`about` 도구/개념 그래프 노드를 사용해 그래프 이웃을 확장합니다. 기본값은 **multi-hop 순회**이며 깊이는 **2 문서 홉**입니다(`depth` 필드로 조정 가능).
3. 후보 풀에 경량 **그래프 reranker**를 적용합니다. 상위 vector hit는 앵커로 고정하고 나머지 후보는 공유 그래프 노드, 공유 클레임 축, 그래프 차수, 최신성 감쇠로 재점수화합니다.
4. 상위 관련 문서를 합성 프롬프트에 끌어옵니다.

이렇게 vector noise에 묻힌 답을 추가 LLM 추출 없이 구출합니다. 그래프 경로는 관찰 가능합니다: 모든 `/ask` 호출이 `query_log.meta`에 `graph_context_chars`와 `graph_source_count`를 기록합니다.

GraphRAG 본문 문맥 경로는 일반 관련 문맥보다 더 엄격합니다:

- 확장에는 공유 도구/개념 그래프 노드만 사용합니다.
- 클레임 축 연속성은 별도 관련/클레임 권위 경로에 남기므로 상태 이력이 추가 GraphRAG 근거처럼 보이지 않습니다.


`/search`는 외부 recall 계약으로 원시 vector + BM25 RRF 순위를 유지하고 그래프 reranker를 적용하지 않으므로, `make eval-graphrag`에서 두 경로를 공정하게 A/B 비교할 수 있습니다.

## 아직 구현되지 않은 것

- **뉴럴 그래프 reranker**: 현재 reranker는 결정론적 특징량 기반 믹서이며, 학습된 graph-neural reranker는 아닙니다.
- **임의의 엣지 종류로 GraphRAG 확장**: GraphRAG 확장을 이끄는 것은 `uses`/`about` 엣지뿐입니다. 프로젝트/토픽 엣지(`in_project`, `tagged`)는 저장되지만 그룹핑과 필터링에 사용되며 GraphRAG의 주요 근거로 사용되지 않습니다.

향후 `make eval-graphrag`에서 깊이 2로 메울 수 없는 recall 공백이 보이면, 스키마는 이미 k-hop recursive CTE를 지원하고 동일한 노드/엣지 모델을 graph DB로 옮겨도 API 계약을 바꾸지 않습니다.

## 검증

```bash
make verify-llm
make up
make sync
make eval
make eval-graphrag
make doctor
make readiness
```

기대 결과:

- `make verify-llm`이 embedding 차원 계약을 확인합니다.
- `make sync`가 `vault/wiki`를 벡터와 그래프 엣지로 오류 없이 적재합니다.
- `make eval`이 `data/eval/golden.json`에서 recall/answer 품질 하한을 통과합니다.
- `make eval-graphrag`이 `data/eval/graph-golden.json`에서 `/search`(vector-only)와 `/ask`(vector + graph + claim + LLM)를 A/B 비교해 Recall@3와 graph-only 구출 수를 리포트합니다.
- `make doctor`가 엔진과 vector/graph 상태가 정상임을 보여줍니다.
- 예약 브리핑에 의존하기 전 `make readiness`가 초록불이어야 합니다.

## 관찰 가능성

- 쿼리 telemetry: 모든 `/ask` 호출이 `query_log.meta`에 `graph_context_chars`와 `graph_source_count`를 기록합니다.
- 이벤트: 적재, sync, eval이 Rust workflow graph 계약을 반영하는 구조화된 이벤트를 낽니다.
- 로그: `make logs`로 엔진 로그를, `make events`로 이벤트 DB/spool을 봅니다.

## 문제 해결

| 증상 | 확인 |
| --- | --- |
| Graph 엔드포인트가 "vector mode required" 반환 | `BORING_VECTOR=on`을 설정하고 재시작합니다. |
| `make sync`가 embedding 차원 오류로 실패 | `llm.embed_dim`이 모델과 맞지 않습니다. 갱신하고 `make reset`을 실행합니다. |
| `make eval-graphrag` recall 하락 | `vault/wiki` 노트에 `tools:` / `concepts:` frontmatter가 있는지 확인하세요. GraphRAG는 이에 의존합니다. |
| `graph_source_count`가 항상 0 | 상위 vector hit가 다른 노트와 공유하는 도구/개념 노드가 없는 것입니다. 메모리가 희소할 때 예상됩니다. |
| `make readiness`가 stale 최신 노트 보고 | `make sync`를 실행하거나 적재 워커를 확인하세요. readiness가 초록불이 될 때까지 브리핑을 믿지 마세요. |
