# site — 公開するサイト

**完全静的サイト**（Astro）。サーバもAPIも持たない。
発言の中身はブラウザが年ごとのDBを HTTP Range で直接引く。

```bash
cd site
npm install
npm run dev        # http://localhost:4321（data/dist を /db で配る。DBのコピーはしない）
npm run build      # dist/ に静的HTMLを出す（1,691ページ / 22MB）
npm run test       # 回帰テスト（68件・0.2秒）
npm run check      # 型検査 + テスト
```

`npm run dev` / `npm run build` の前に `scripts/vendor.js` が自動で走り、
sql.js-httpvfs のワーカと wasm を `public/vendor/` にコピーする。

## なぜ Astro か

中身の大半はクライアント側でDBから引くので、静的サイトジェネレータに求めたのは
**ページの事前生成**と**素の `<script>` をそのまま書けること**の2つだけ。

- 議員 1,111ページ・争点語 82ページ・頻出語 489ページを `getStaticPaths` で事前生成する。
  氏名や語で検索されたときに、中身のあるHTMLが返るのはここだけ
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
| `/topics` | 静的 | 争点語82件（**運営の編集方針**） |
| `/topic/<id>` | **静的 ×82** | 頻度推移のSVGと会派比較は**ビルド時に生成**。発言はDBから |
| `/frequent` | 静的 | 頻出語500件（**機械抽出**）。山が立った年ごとに並べる。**会期で絞れる**（下記） |
| `/word/<語>` | **静的 ×489** | 頻度推移のSVG。発言は既存の `word_hit` / FTS から |
| `/about` | 静的 | データの出どころと限界 |
| `/disclaimer` | 静的 | 免責事項・引用の考え方・**訂正依頼の窓口**・運営者表示 |
| `/privacy` | 静的 | プライバシーポリシー |

### 運営者名と連絡先は `src/lib/operator.ts` の1か所

`/about` `/disclaimer` `/privacy` の3ページがここを見ている。
**未記入のあいだは3ページとも⚠を出す**（`missing` に欠けている項目名が入る。
docs/SCOPE.md。空になるまで公開しない）。

メールの表示は `src/components/Mail.astro`。**アドレスをそのままHTMLに置かない**
（ユーザ名とドメインを別属性に分け、画面には全角の＠で出す。素のHTMLに
`user@domain` の並びが1度も現れないことを確認済み）。コピーボタンはJSで後から差し込む
—— 動かない環境に押せないボタンを残さないため。スタイルを `is:global` にしてあるのは
そのボタンにスコープ付きの属性が付かないから。
アクセス解析を入れたら `analytics` を必ず埋めること — `/privacy` の記述が変わる。

`operator.ts` から `db.ts` を import しないこと。`format.ts` と同じ理由で、
ビルド時（Node側）に読まれるので、ブラウザ専用のコードを引き込むと SSR が落ちる。

### `/frequent` の会期絞り込み

**DBを引かない。** 会期ごとの件数は `data/dist/frequent.json` に入っていて、
並べ替えは `src/lib/frequent.ts` の `rankBySession()` がページの中でやる。
セレクタは**JSで差し込む**（Mail.astro のコピーボタンと同じ。動かない環境に
選べないセレクタを残さないため）。JSが無ければ全期間の一覧がそのまま残る。

- **ブラウザから `word_hit` / FTS で範囲集計しない。** 年330万行の範囲集計は
  全走査になり、`ORDER BY date DESC` と同じ穴に落ちる（docs/PITFALLS.md）
- **並びは率**（その会期の出現率 ÷ 全期間の出現率）。件数順にすると
  どの会期を選んでも `日本` `国民` が並ぶ
- ページに埋める JSON は500語ぶんで 42KB（gzip 13KB）。月の系列は落としてある

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
| 2文字以下の語を含む | `word_hit`（語彙 16,264件） | FTSでは**原理的に引けない**もの |
| それ以外 | FTS5 trigram | 1.5〜6秒 |
| 2文字語が語彙にも無い | 引けない | **黙って0件にせず**、含む争点語を出す |

複数語で2文字語が混ざるときは、**いちばん珍しい2文字語を起点**にして残りを
`instr()` で絞る。走査する行数が起点の語の件数で頭打ちになる。

**★ 検索語の全角化はこの3経路の手前、`query.ts` の `splitTerms()` でやっている**
（`toFullWidth()`・`docs/DECISIONS.md`）。会議録の英数字は全部全角で、
素の半角で引くと0件になる。**引き先を足すときも必ずこの入口を通すこと。**

- **NFKC を使わない。** 全角→半角に潰す**逆方向**の正規化で、やると全滅する。
  必要なのは `A-Za-z0-9` を `+0xFEE0` する片方向の写像だけ
- **大小の扱いは経路で逆になる。揃えようとするとどちらかが壊れる。**
  - **FTS 経路は畳まない。** trigram が全角ラテンの大小を自分で畳むので幅だけで足りる。
    寄せると `ＳＤＧｓ` `ｉＰＳ` `ＩｏＴ` が引けなくなるだけ。代わりに畳むのは
    `matchTopic()`（争点語の突合）と `highlightTerm()`（強調）の2か所
  - **word 経路は畳まないと引けない。** `w.term = ?` は BINARY 比較で、SQLite の
    `NOCASE` も `upper()` も ASCII 限定なので全角に効かない。`toWordKey()` で
    **2文字以下だけ**大文字に寄せる（3文字以上に掛けると `ＳＤＧｓ` が壊れる）。
    語彙側も `build_words.py` の `fold()` が同じ写像を掛けてある
  - 画面・入力欄・URL に出すのは `canonicalQuery()` の結果（＝実際に引いた語）
- ハイライトに渡す `terms` も `splitTerms()` から取る（本文が全角なので、半角のままだと光らない）

**引き先が3通りあるということは、直す場所も3か所あるということ。**
絞り込み条件を足すときは、結果取得（`ftsSql` / `topicSql` / `wordSql`）と
件数（`countQuery()`）の**両方を3経路すべて**直す。片方だけだと、
一覧の中身と画面上部の件数が黙って食い違う。

## 年をまたぐページ送り

`search()` も `politicianSpeeches()` も、年ごとに別ワーカで **LIMIT 件ずつ**引く。
その結果を扱うのは `mergePages()` ひとつ。

- **年ごとの結果をそのまま連結しない。** 6年ぶんなら 20件のつもりが120件返るうえ、
  「2026年の21件目」を飛ばして2025年へ進む。次ページで飛ばした分が後ろに付くので、
  画面の「新しい順」がそこで崩れる。
- 全体で LIMIT 件に切り、**1件も出さなかった年はカーソルを進めない**。
  次ページで同じところから引き直すが、読んだページはワーカに残っているので安い。
- 並べ替えは要らない。年DBは日付で綺麗に分かれているので、新しい年から順に
  詰めれば全体が日付の降順になる。

## 検索まわりのファイル構成とテスト

DBとの通信と、SQL の組み立てを分けてある。**分けてあるのはテストのため。**

| ファイル | 中身 | ブラウザが要るか |
|---|---|---|
| `src/lib/query.ts` | SQL の組み立て・`mergePages()`・検索語の正規化 | **要らない**（純粋な関数だけ） |
| `src/lib/db.ts` | ワーカの管理・年ごとの並列問い合わせ | 要る（sql.js-httpvfs） |

```bash
npm run test    # site/test/*.test.ts。node:test + node:sqlite（依存パッケージ無し）
```

- `test/paging.test.ts` — 年DBを模した corpus をページ送りして、
  **画面に並んだ順が全体の日付降順と1件ずつ一致するか**を見る
- `test/query.test.ts` — 年DBと同じ形の小さなDBを `node:sqlite` でメモリに作り、
  **`searchQuery()` が返した行数と `countQuery()` が返した数が一致するか**を
  3経路 × 絞り込みの組み合わせで突き合わせる

DBの読み直し（`db.ts` の `query()`）だけは Node から検証できないので、
**配信側を壊して確かめる**仕掛けを `dev-data-server.js` に置いてある。

```bash
POISON=1 node scripts/dev-data-server.js   # retry= の付かないURLにゼロを返す
```

別オリジン構成（上の `npm run dbserve` の手順）で開くと、全年で
`file is not a database` が出たうえで**自力で復旧する**のが正しい状態。
復旧しなくなったら、やり直しが URL を変えられていない。

**`query.ts` に sql.js-httpvfs を import しない。** 1本でも通ると
Node からテストを走らせられなくなる（`db.ts` 側に置くこと）。
テストは `.ts` のまま Node が直接読むので、**enum や
constructor の parameter property は使えない**（型を消すだけの変換のため）。

## 触る前に読むもの

**[docs/PITFALLS.md](../docs/PITFALLS.md) の「ブラウザからDBを引く」。** `src/lib/query.ts` の SQL は
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
