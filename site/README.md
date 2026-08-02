# site — 公開するサイト

ROADMAP §3.3。**完全静的サイト**（Astro）。サーバもAPIも持たない。
発言の中身はブラウザが年ごとのDBを HTTP Range で直接引く。

```bash
cd site
npm install
npm run dev        # http://localhost:4321（data/dist を /db で配る。DBのコピーはしない）
npm run build      # dist/ に静的HTMLを出す（1,196ページ / 18MB）
npm run check      # 型検査
```

`npm run dev` / `npm run build` の前に `scripts/vendor.js` が自動で走り、
sql.js-httpvfs のワーカと wasm を `public/vendor/` にコピーする。

## なぜ Astro か

中身の大半はクライアント側でDBから引くので、静的サイトジェネレータに求めたのは
**ページの事前生成**と**素の `<script>` をそのまま書けること**の2つだけ。

- 議員 1,111ページ・争点語 79ページを `getStaticPaths` で事前生成する。
  氏名で検索されたときに、中身のあるHTMLが返るのはここだけ
- UIフレームワークを入れていない。`.astro` の `<script>` に素のTypeScriptを書く。
  Vite がバンドルと型検査をしてくれる。React も Svelte も要らなかった
- 出力は静的HTMLなので、**Astro が壊れても公開中のサイトは動き続ける**

## ページ

| URL | 生成 | 中身 |
|---|---|---|
| `/` | 静的 | 検索窓・直近の急上昇語・争点語 |
| `/search` | 静的（骨組み） | 検索。条件はクエリ文字列に持つ |
| `/politicians` | 静的 | 議員1,111人の一覧。絞り込みはDOM上で完結 |
| `/politician/<id>` | **静的 ×1,111** | 氏名・所属会派の時系列は静的。発言はDBから |
| `/speech/<speech_id>` | 静的1枚 + rewrite | 発言650,785件は事前生成できない（下記） |
| `/topics` | 静的 | 争点語79件 |
| `/topic/<id>` | **静的 ×79** | 頻度推移のSVGと会派比較は**ビルド時に生成**。発言はDBから |
| `/about` | 静的 | データの出どころと限界 |

### `/speech/<id>` の扱い

発言は 650,785 件あって事前生成できない。`/speech` を1枚だけ出して、
`public/_redirects` の `200` rewrite で全部そこへ流す（URLは変わらない）。
開発サーバでは `scripts/dev-data-server.js` が同じ書き換えをしている。

この構成だと **OGP は共通のものしか出せない**。発言ごとのOG画像や説明文が
要るようになったら、そこで初めて Workers を検討する（＝「常時稼働プロセスを
持たない」制約に触れるので、必要になるまでやらない）。

## DBの置き場所

`PUBLIC_DB_BASE` で切り替える。

| | 値 | 誰が配るか |
|---|---|---|
| 開発 | 未設定（`/db`） | `scripts/dev-data-server.js`（Astro に相乗り） |
| 本番 | `https://<R2のカスタムドメイン>` | Cloudflare R2 |

本番は別オリジンになるので、**R2 側に CORS が要る**:

```
Access-Control-Allow-Origin: <サイトのオリジン>
Access-Control-Expose-Headers: content-range, content-length
```

ビルド成果物を本番と同じ「別オリジン」構成で試すには:

```bash
npm run dbserve                                                   # 別ターミナル（:8788）
PUBLIC_DB_BASE=http://127.0.0.1:8788/db npm run build && npm run preview
```

## 検索の引き先は3通り

`src/lib/db.ts` の `resolveQuery()` が決める。**1回だけ解いて、ページ送りでは使い回す。**

| 条件 | 引き先 | 速さ |
|---|---|---|
| 争点語（`topics.json`）に一致 | `topic_hit` | 0.7秒。別表記も合算される |
| 2文字以下の語を含む | `word_hit`（語彙 16,058件） | FTSでは**原理的に引けない**もの |
| それ以外 | FTS5 trigram | 1.5〜6秒 |
| 2文字語が語彙にも無い | 引けない | **黙って0件にせず**、含む争点語を出す |

複数語で2文字語が混ざるときは、**いちばん珍しい2文字語を起点**にして残りを
`instr()` で絞る。走査する行数が起点の語の件数で頭打ちになる。

## 触る前に読むもの

**`docs/PHASE1_PROTOTYPE.md` §2 の3つの制約。** `src/lib/db.ts` の SQL は
全部それに縛られている。特に:

- **「新しい順」は `ORDER BY rowid DESC`。`date` で並べない**
  （検索で204MB転送・議員ページで7,800リクエストになる）
- **ページ送りは OFFSET ではなく rowid の keyset**
  （OFFSET 80 で134リクエスト・4.1秒）
- **年またぎは年ごとに別ワーカで並列**（ATTACH+UNION にしない）
- **`db.query(sql, params)` の第2引数は配列で1個**（展開すると黙って束縛されない）

もうひとつ、モジュールの依存に注意:

- **`src/lib/format.ts` から `db.ts` を import しない。** format.ts は
  `.astro` のフロントマター（ビルド時のNode側）からも読む。db.ts は
  sql.js-httpvfs に依存していてブラウザでしか動かないので、
  間にimportが1本でも通ると SSR が落ちる。
