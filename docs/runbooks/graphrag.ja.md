# GraphRAG & Vector 契約ランブック

## 目的

pgvector バックエンドの GraphRAG モードをいつ、どのように有効にするか、graph/vector 契約がどんな保証を与えるか、そして健全性をどう検証するかを説明します。

## 前提条件

- `BORING_VECTOR=on` が設定されていること（環境変数または `boring.json` のデフォルト）。
- pgvector 付き Postgres が動作していること（Docker Compose を使えば `make up` が起動します）。
- `llm.embed_model` と `llm.embed_dim` がローカルで配信されている embedding モデルと一致していること。
- `make verify-llm` が通過していること。

## 設定

`boring.json` で vector モードを有効にします:

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

または実行時に上書きします:

```bash
BORING_VECTOR=on make up
```

vector モードが有効のとき、`make sync` は `vault/wiki` を embedding と graph edge に再取り込みします。無効のとき、エンジンは wiki-first です: `/ask`、`/search`、`recall`、`context` は Markdown を直接読み、graph/claim エンドポイントは明示的なエラーを返します。

## Vector 契約

- `llm.embed_dim` は `llm.embed_model` の `/v1/embeddings` が実際に返す次元と等しい必要があります。
- embedding モデルを変更する場合は `llm.embed_dim` を更新し、**`make reset` を実行する必要があります**。vector テーブルの形状が変わるためです。
- `make verify-llm` は `/v1/embeddings` を呼び出し、返却された長さと `llm.embed_dim` を比較します。
- embedding モデルのみがストレージに次元を持ち込む唯一のモデルです。synthesis モデル (`llm.model`) は自由に交換できます。

## Graph 契約

- グラフは決定論的です。`tool`、`concept`、`claim` ノードは `drudge` 内部の追加 LLM 抽出ではなく、エージェントが整えたノート frontmatter から来ます。
- `relates_to` リンクは以下の順で投影されます:
  1. クレームの連続性（正規化済み `(subject, predicate)` 軸）。
  2. 正確な道具/概念の重なり。
  3. 証拠のある意味的な隣接。
  4. 小さな同一プロジェクトの新しさ補完。
- ソースごとの上限により、ハブノートが過剰な網目にならないようにします。
- `remember` がノートを書いた時点で、そのノートの `relates_to` 投影は即時更新されます。隣接ノート側の backlink は次回 `make sync` / 全体 `project_links` で整合するため、recall は即時で、Obsidian link だけが eventual consistency です。

## GraphRAG 実装

`BORING_VECTOR=on` のとき `/ask` はローカル GraphRAG を実行します:

1. 上位の vector + BM25 RRF hit を取ります。
2. 共有 `uses`/`about` 道具/概念グラフノードを使ってグラフ近傍を展開します。デフォルトは **multi-hop 走査** で、深さは **2 ドキュメントホップ** です（`depth` フィールドで変更可能）。
3. 候補プールに軽量な **グラフ reranker** を適用します。上位 vector hit はアンカーとして固定し、残りの候補を共有グラフノード、共有クレーム軸、グラフ次数、新近性減衰で再スコアリングします。
4. 上位の関連文書を合成プロンプトに引き込みます。

これにより vector noise に埋もれた回答を、追加の LLM 抽出なしで救い出します。グラフレーンは観測可能です: すべての `/ask` 呼び出しが `query_log.meta` に `graph_context_chars` と `graph_source_count` を記録します。

GraphRAG 本文文脈の経路は、一般的な関連文脈より厳密です:

- 展開には共有された道具/概念グラフノードだけを使います。
- クレーム軸の連続性は別の関連/クレーム権威経路に残すため、状態履歴が追加の GraphRAG 根拠に見えることはありません。

`/search` は外部 recall 契約として生の vector + BM25 RRF ランキングを保持し、グラフ reranker を適用しません。これにより `make eval-graphrag` で両経路を公平に A/B 比較できます。

## まだ実装されていないもの

- **ニューラルグラフ reranker**: 現在の reranker は決定論的な特徴量ベースのミキサーであり、学習済みの graph-neural reranker ではありません。
- **任意のエッジ種別による GraphRAG 展開**: GraphRAG 展開を駆動するのは `uses`/`about` エッジのみです。プロジェクト/トピックエッジ (`in_project`, `tagged`) は保存されますが、グルーピングとフィルタリングに使われ、GraphRAG の主要な証拠には使われません。

将来の `make eval-graphrag` で深さ 2 で埋められない recall ギャップが出た場合、スキーマは既に k-hop recursive CTE をサポートしており、同じ node/edge モデルを graph DB に移しても API 契約を変えません。

## 検証

```bash
make verify-llm
make up
make sync
make eval
make eval-graphrag
make doctor
make readiness
```

期待結果:

- `make verify-llm` が embedding 次元契約を確認します。
- `make sync` が `vault/wiki` をベクトルとグラフエッジにエラーなく取り込みます。
- `make eval` が `data/eval/golden.json` で recall/answer 品質フロアを通過します。
- `make eval-graphrag` が `data/eval/graph-golden.json` で `/search`（vector-only）と `/ask`（vector + graph + claim + LLM）を A/B 比較し、Recall@3 と graph-only rescue をレポートします。
- `make doctor` がエンジンと vector/graph 状態が正常であることを示します。
- 予約済みの朝ブリーフィングに頼る前に `make readiness` が green である必要があります。

## 観測可能性

- クエリ telemetry: すべての `/ask` 呼び出しが `query_log.meta` に `graph_context_chars` と `graph_source_count` を記録します。
- イベント: 取り込み、sync、eval が Rust workflow graph 契約を反映した構造化イベントを発行します。
- ログ: `make logs` でエンジンログ、`make events` でイベント DB/spool を確認します。

## トラブルシュート

| 症状 | 確認 |
| --- | --- |
| Graph エンドポイントが "vector mode required" を返す | `BORING_VECTOR=on` を設定して再起動します。 |
| `make sync` が embedding 次元エラーで失敗する | `llm.embed_dim` がモデルと一致していません。更新して `make reset` を実行します。 |
| `make eval-graphrag` の recall が低下する | `vault/wiki` のノートに `tools:` / `concepts:` frontmatter があるか確認してください。GraphRAG はこれに依存します。 |
| `graph_source_count` が常に 0 | 上位 vector hit が他のノートと共有する道具/概念ノードがない状態です。メモリが疎な場合は想定されます。 |
| `make readiness` が stale 最新ノートを報告する | `make sync` を実行するか、取り込みワーカーを確認してください。readiness が green になるまでブリーフィングを信頼しないでください。 |
