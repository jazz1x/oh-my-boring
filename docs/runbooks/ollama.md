# Ollama Runbook

## Purpose

Use Ollama as the local OpenAI-compatible backend for ohmyboring. The engine still calls `/v1/chat/completions` and `/v1/embeddings`; Ollama's provider bootstrap starts the server and pulls missing models.

## Preconditions

- Ollama is installed.
- `jq`, `curl`, Docker, and `make` are available.
- On Linux, Ollama binds `127.0.0.1` by default. For Docker reachability, bind it to `0.0.0.0:11434` (`OLLAMA_HOST=0.0.0.0:11434`) and allow the docker bridge in the host firewall.

## Configuration

Set `boring.json` to `ollama`. Use `host.docker.internal` for the Docker runtime:

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

`bootstrap: auto` tells `make up` to run `scripts/llm-providers/ollama.sh`, which ensures `ollama serve` is running and pulls `llm.model` and `llm.embed_model` if they are missing. Use `bootstrap: manual` if you start Ollama yourself and only want health checks.

List available models on the host:

```bash
curl -s http://localhost:11434/api/tags | jq -r '.models[].name'
```

`make verify-llm` must see both the configured chat model and the configured embedding model; it also calls `/v1/embeddings` and confirms the returned vector length equals `llm.embed_dim`.

## Verification

```bash
make ollama
make verify-llm
make up
make doctor
make readiness
```

Expected result:

- `make ollama` starts `ollama serve` in the background if it is not running.
- `make verify-llm` finds the provider script, reaches `/v1/models`, sees both configured model ids, and confirms the actual embedding dimension.
- `make doctor` reports the engine healthy, the write door open, and current worker/marker state.
- `make readiness` is green before you rely on a scheduled morning briefing; it fails on provider/embed mismatch, worker failure, stale markers, or stale newest notes.
- If Hermes/Codex ingestion is enabled, `make doctor` also reports the Codex worker state.

## Embedding Dimension

The embedding model dimension is part of the storage contract. Common values:

| Model | `embed_dim` |
| --- | ---: |
| `bge-m3` | 1024 |
| `nomic-embed-text` | 768 |
| `text-embedding-3-small` | 1536 |

When changing `llm.embed_model`, update `llm.embed_dim` and run `make reset` before relying on vector mode. Wiki-first recall still reads markdown directly, but vector search, claims, graph, status, and brief depend on the vector store shape.

For the current 1024d release path, do not call Ollama vector-ready unless `curl http://localhost:11434/api/tags` lists `bge-m3` and `make verify-llm` reports an actual embedding dimension of 1024.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `make verify-llm` cannot reach Ollama | `make ollama` or start `ollama serve` manually. |
| `make verify-llm` cannot find a model | Pull it with `ollama pull <model>` and update `llm.model` / `llm.embed_model`. |
| `make verify-llm` reports an actual dimension mismatch | The embedding model returns a different shape than `llm.embed_dim`; either pull the intended model or change `embed_dim` and run `make reset`. |
| Docker cannot reach host Ollama (Linux) | Bind Ollama to `0.0.0.0:11434` and allow the docker bridge in the host firewall. |
| Docker cannot reach host Ollama (macOS) | Use `http://host.docker.internal:11434/v1` in `boring.json`, not `localhost`. |
| Host benchmark cannot reach Ollama | Use `http://localhost:11434/v1` with `scripts/bench-llm.py --base-url`. |
| Embedding upsert fails | `llm.embed_dim` does not match the embedding model; update it and reset the vector DB. |
| `make readiness` reports stale markers or stale newest note | Treat the scheduled briefing as not ready. Inspect `~/.cache/boring-distill`, verify the Codex/Hermes workers, and reconcile the stale marker or ingestion gap before relying on the brief. |
