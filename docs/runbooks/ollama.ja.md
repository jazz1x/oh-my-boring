# Ollama ランブック

## 目的

Ollama を ohmyboring のローカル OpenAI-compatible バックエンドとして使います。エンジンはそのまま `/v1/chat/completions` と `/v1/embeddings` を呼び、Ollama provider のブートストラップがサーバーを起動し、不足モデルを pull します。

## 前提条件

- Ollama がインストール済み。
- `jq`、`curl`、Docker、`make` が使える。
- Linux では Ollama がデフォルトで `127.0.0.1` にバインドされるため、Docker 到達性のために `OLLAMA_HOST=0.0.0.0:11434` にバインドし、ホストファイアウォールで docker bridge を許可してください。

## 設定

`boring.json` の provider を `ollama` にし、Docker ランタイムでは `host.docker.internal` を使います:

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

`bootstrap: auto` は `make up` が `scripts/llm-providers/ollama.sh` を実行し、`ollama serve` が動作していることを確認して `llm.model` と `llm.embed_model` が欠けていれば pull します。自分で Ollama を起動しておきたいだけなら `bootstrap: manual` にするとヘルスチェックのみ行われます。

ホストで利用可能なモデルを確認します:

```bash
curl -s http://localhost:11434/api/tags | jq -r '.models[].name'
```

`make verify-llm` は設定済みの chat モデルと embedding モデルの両方を検出し、`/v1/embeddings` に直接リクエストして実際のベクトル長が `llm.embed_dim` と一致することを確認します。

## 検証

```bash
make ollama
make verify-llm
make up
make doctor
make readiness
```

期待結果:

- `make ollama` が Ollama が停止中ならバックグラウンドで `ollama serve` を起動します。
- `make verify-llm` が provider スクリプトを見つけ、`/v1/models` に到達し、設定した 2 つのモデル id を確認し、実際の embedding 次元を確認します。
- `make doctor` がエンジン正常、write door open、現在の worker/marker 状態を報告します。
- 予約済みの朝ブリーフィングに頼る前に `make readiness` が green であることを確認します。provider/embed 不一致、worker 失敗、stale marker、stale 最新ノートがあれば失敗します。
- Hermes/Codex 取り込みが有効なら、`make doctor` が Codex ワーカー状態も表示します。

## Embedding 次元

Embedding モデルの次元は保存形式の契約です。よく使う値:

| モデル | `embed_dim` |
| --- | ---: |
| `bge-m3` | 1024 |
| `nomic-embed-text` | 768 |
| `text-embedding-3-small` | 1536 |

`llm.embed_model` を変えるときは `llm.embed_dim` も合わせ、vector モードを信頼する前に `make reset` を実行します。wiki-first recall は Markdown を直接読みますが、vector search、claims、graph、status、brief は vector store の形に依存します。

現在の 1024d リリース経路では、`curl http://localhost:11434/api/tags` に `bge-m3` があり、`make verify-llm` が実際の embedding 次元 1024 を報告する場合だけ Ollama を vector-ready と呼びます。

## トラブルシュート

| 症状 | 確認 |
| --- | --- |
| `make verify-llm` が Ollama に届かない | `make ollama` を実行するか、直接 `ollama serve` を起動します。 |
| `make verify-llm` がモデルを見つけない | `ollama pull <model>` で pull し、`llm.model` / `llm.embed_model` を更新します。 |
| `make verify-llm` が実際の次元不一致を報告する | embedding モデルが `llm.embed_dim` と異なる形状を返しています。意図したモデルをロードするか、`embed_dim` を変更して `make reset` を実行します。 |
| Docker がホスト Ollama に届かない (Linux) | Ollama を `0.0.0.0:11434` にバインドし、ホストファイアウォールで docker bridge を許可します。 |
| Docker がホスト Ollama に届かない (macOS) | `boring.json` では `localhost` ではなく `http://host.docker.internal:11434/v1` を使います。 |
| ホスト上のベンチマークが Ollama に届かない | `scripts/bench-llm.py --base-url` では `http://localhost:11434/v1` を使います。 |
| embedding upsert が失敗 | `llm.embed_dim` が embedding モデルと合っていません。次元を修正し、vector DB を resetします。 |
| `make readiness` が stale marker や stale 最新ノートを報告する | 予約ブリーフィングは準備未完了と見なします。`~/.cache/boring-distill` を確認し、Codex/Hermes worker を検証して、stale marker または取り込み空白を調整してください。 |
