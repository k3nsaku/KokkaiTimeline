# prototype — sql.js-httpvfs の計測

`docs/ROADMAP.md` §2 の未検証リスク
「**sql.js-httpvfs が 350MB のDBで実用速度を出せるか**」を潰すための計測用。
本番のサイト実装ではない。結論は `docs/DECISIONS.md`。

## 使い方

```bash
python scripts/build_db.py --split-by-year --year 2025 --page-size 8192   # 年DBを作る
cd prototype && npm install && npm run vendor                             # 配布物を用意
npm run serve                                                             # http://127.0.0.1:8787
```

ブラウザで開いて「全シナリオを実行」。

| ボタン | 何を測るか |
|---|---|
| 全シナリオを実行 | 検索・議員ページ・前後表示のリクエスト数とバイト数 |
| チャンクサイズ5種を通しで実行 | `requestChunkSize` を 1024〜32768 で比較 |
| 年またぎ検索を計測 | 同ディレクトリの年DBを全部使い、並列ワーカ版と ATTACH 版を比較 |

「1リクエストに入れる遅延(ms)」はサーバ側で `GET /delay?ms=N` に反映される。
localhost は往復がほぼ0なので、実測したCloudflareエッジまでのRTT（中央値 **19.6ms**）を
入れて初めて本番の効き方が見える。**0のまま読むと速すぎる数字が出る。**

## 構成

| ファイル | 役割 |
|---|---|
| `server.js` | Range対応の静的サーバ。R2 の代わり。**リクエストを数えるのが本体** |
| `vendor.js` | `node_modules` から wasm/worker を `public/vendor/` へコピー |
| `public/bench.js` | 計測シナリオと集計 |

リクエスト数はサーバ側で数えている。ワーカ内の通信はページから差し替えられないため。
`sql.js-httpvfs` 自身の `getStats()` とも突き合わせていて、値は一致している。

## 落とし穴

- **`db.query(sql, params)` の第2引数は配列で渡す。** 型定義は `(sql, ...params)` だが、
  実体は sql.js の `exec(sql, params)` なので、展開して渡すと**黙って束縛されない**
  （`fts5: syntax error near ""` になる）。
- **wasm の URL は絶対パスで渡す。** ワーカ内で解決されるため `./vendor/...` だと
  `/vendor/vendor/...` を見に行って `CompileError` になる。
- **`snippet()` と `MATCH` はスキーマ修飾を受け付けない。** ATTACH した年DBを引くときは
  `FROM y2024.speech_fts` のように FROM だけ修飾し、`snippet(speech_fts, ...)` は素で書く。
- 同梱の SQLite は **3.35.0 / FTS5あり**。`speech_fts_config` の `version=4` と互換。
