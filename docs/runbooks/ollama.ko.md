# Ollama 런북

## 목적

Ollama를 ohmyboring의 로컬 OpenAI-compatible 백엔드로 사용합니다. 엔진은 그대로 `/v1/chat/completions`와 `/v1/embeddings`를 호출하며, Ollama provider 부트스트랩이 서버를 시작하고 없는 모델을 pull합니다.

## 사전 조건

- Ollama가 설치되어 있어야 합니다.
- `jq`, `curl`, Docker, `make`를 사용할 수 있어야 합니다.
- Linux에서는 Ollama가 기본적으로 `127.0.0.1`에 바인딩되므로 Docker 도달성을 위해 `OLLAMA_HOST=0.0.0.0:11434`로 바인딩하고 호스트 방화벽에서 docker bridge를 허용해야 합니다.

## 설정

`boring.json`의 provider를 `ollama`로 두고, Docker 런타임에서는 `host.docker.internal`을 사용합니다:

```json
{
  "llm": {
    "provider": "ollama",
    "base_url": "http://host.docker.internal:11434/v1",
    "model": "qwen3:14b",
    "embed_model": "bge-m3",
    "embed_dim": 1024,
    "api_key_env": "BORING_LLM_API_KEY",
    "bootstrap": "auto"
  }
}
```

`bootstrap: auto`는 `make up`이 `scripts/llm-providers/ollama.sh`를 실행해 `ollama serve`가 떠 있는지 확인하고 `llm.model`과 `llm.embed_model`이 없으면 pull하도록 합니다. 직접 Ollama를 켜고 싶기만 하다면 `bootstrap: manual`로 두면 건강 검사만 수행합니다.

호스트에서 사용 가능한 모델을 확인합니다:

```bash
curl -s http://localhost:11434/api/tags | jq -r '.models[].name'
```

`make verify-llm`은 설정된 chat 모델과 embedding 모델을 모두 찾아야 하며, `/v1/embeddings`에 직접 요청해서 실제 벡터 길이가 `llm.embed_dim`과 같은지도 확인합니다.

## 검증

```bash
make ollama
make verify-llm
make up
make doctor
make readiness
```

기대 결과:

- `make ollama`가 Ollama가 꺼져 있으면 백그라운드에서 `ollama serve`를 시작합니다.
- `make verify-llm`이 provider 스크립트를 찾고, `/v1/models`에 접근하며, 설정된 두 모델 id를 모두 확인하고 실제 embedding 차원을 확인합니다.
- `make doctor`가 엔진 정상, write door open, 현재 워커/marker 상태를 보고합니다.
- 예약된 아침 브리핑에 의존하기 전에 `make readiness`가 초록불이어야 합니다. provider/embed 불일치, 워커 실패, stale marker, stale 최신 노트가 있으면 실패합니다.
- Hermes/Codex 적재가 켜져 있으면 `make doctor`가 Codex 워커 상태도 함께 보여줍니다.

## Embedding 차원

Embedding 모델 차원은 저장소 계약입니다. 흔한 값:

| 모델 | `embed_dim` |
| --- | ---: |
| `bge-m3` | 1024 |
| `nomic-embed-text` | 768 |
| `text-embedding-3-small` | 1536 |

`llm.embed_model`을 바꿀 때는 `llm.embed_dim`도 맞게 바꾸고, vector 모드를 믿기 전에 `make reset`을 실행합니다. wiki-first recall은 마크다운을 직접 읽지만, vector search, claims, graph, status, brief는 vector store 형태에 의존합니다.

현재 1024d 릴리즈 경로에서는 `curl http://localhost:11434/api/tags`에 `bge-m3`가 있고 `make verify-llm`이 실제 embedding 차원 1024를 보고할 때만 Ollama를 vector-ready라고 부릅니다.

## 문제 해결

| 증상 | 확인 |
| --- | --- |
| `make verify-llm`이 Ollama에 닿지 못함 | `make ollama`를 실행하거나 직접 `ollama serve`를 시작합니다. |
| `make verify-llm`이 모델을 못 찾음 | `ollama pull <model>`로 pull하고 `llm.model` / `llm.embed_model`을 갱신합니다. |
| `make verify-llm`이 실제 차원 불일치를 보고함 | embedding 모델이 `llm.embed_dim`과 다른 형태를 반환합니다. 의도한 모델을 로드하거나 `embed_dim`을 바꾸고 `make reset`을 실행합니다. |
| Docker가 호스트 Ollama에 접근 못 함 (Linux) | Ollama를 `0.0.0.0:11434`에 바인딩하고 호스트 방화벽에서 docker bridge를 허용합니다. |
| Docker가 호스트 Ollama에 접근 못 함 (macOS) | `boring.json`에는 `localhost`가 아니라 `http://host.docker.internal:11434/v1`을 씁니다. |
| 호스트 벤치마크가 Ollama에 접근 못 함 | `scripts/bench-llm.py --base-url`에는 `http://localhost:11434/v1`을 씁니다. |
| embedding upsert 실패 | `llm.embed_dim`이 embedding 모델과 맞지 않습니다. 차원을 수정하고 vector DB를 reset합니다. |
| `make readiness`가 stale marker나 stale 최신 노트를 보고함 | 예약 브리핑은 준비되지 않은 상태로 봅니다. `~/.cache/boring-distill`을 확인하고 Codex/Hermes 워커를 검증한 뒤 stale marker 또는 적재 공백을 조정하세요. |
