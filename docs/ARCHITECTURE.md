# アーキテクチャ

**いまどうなっているかを書く。なぜそうなったかは [DECISIONS.md](DECISIONS.md)、
触るときに踏む穴は [PITFALLS.md](PITFALLS.md)。**

```
[GitHub Actions（日次 cron）]
    ↓ NDL 国会会議録API（前月と当月だけ取り直す）
    ↓ SQLite を生成（FTS5 + trigram / 半期ごとに1ファイル）
    ↓ Cloudflare R2 にアップロード（変わった期間のDBと目録だけ）
[Cloudflare Pages] ← 完全静的サイト（Astro / Direct Upload）
    ↓
[ブラウザ] sql.js-httpvfs が HTTP Range で必要な 8KB ページだけ取得
```

常時稼働しているものは無い。パイプラインが失敗しても、サイトは前回のデータで動き続ける。

## 配信

| | |
|---|---|
| サイト | Cloudflare Pages（`kokkai-timeline.com`） |
| DB | Cloudflare R2（`db.kokkai-timeline.com`）。**半期ごとに1ファイル**・8〜377MB |
| 目録 | `manifest.json`。サイトはこれで期間の一覧と引き先のファイル名を知る |

**DBは半期で分ける**（`2026H1` = 1〜6月）。日次更新で変わるのはいま開いている期間だけなので、
閉じた期間は CDN に載ったまま。**1ファイルが 512MB を超えると黙ってキャッシュから
外れる**のが分割の理由で、年だと最大419MB・半期なら377MB（[DECISIONS.md](DECISIONS.md)）。
`page_size` は **8192**（ブラウザ側の `requestChunkSize` と一致させる）。

**分割の規則は2か所にある**（`build_db.py` の `period_of()` と `query.ts` の `periodOf()`）。
**片方だけ変えると存在しないファイルを引きに行く。**

利用者に見せる絞り込みは**年のまま**。期間が年に閉じているので、年 → 期間の変換で
日付の取りこぼしが出ない（`periodsInYearRange()`）。会期で割るとこれが成立しない。

**R2 には Cache Rule が要る。** Cloudflare は拡張子でキャッシュを判定していて、
`.db` も `.json` も既定の対象外。無いと `cf-cache-status: DYNAMIC` のままで
RTT が約10倍（8ms → 77ms）になる。設定は [PIPELINE.md](PIPELINE.md)。

### 目録

```json
{"period": "half",
 "periods": ["2021H1", "…", "2026H2"],
 "databases": [{"id": "2025H1", "file": "kokkai-2025H1.db", "size": 356659200,
                "version": "6fd6f7e28daf6b10",
                "from": "2025-01-24", "to": "2025-06-21"}]}
```

- **`version`（中身の指紋）** — DBのURLに `?v=` で付く。
  **これが無いと、DBを差し替えた瞬間に開いていたページが壊れる**
  （`sql.js-httpvfs` は読んだページをオフセットで覚えているので、
  古いページと新しいページが混ざる）。CDN のパージも要らなくなる
- **`file`** — 引き先は**目録の記載を使う**（規則で組み立てない）
- **`from` / `to`** — 実データの収録範囲

2文字語の語彙の指紋（`vocabulary`）は**もう無い**。索引を本文から作るようになり、
期間ごとに中身が違ってよくなったため。

## データモデル

```
meeting      : issue_id, 会期, 院, 会議名, 号数, 日付, meeting_url, pdf_url
speech       : speech_id, issue_id, 発言順, 日付, 発言者, よみ, 会派, 肩書, 役割,
               本文, speech_url, is_speech, speaker_kind, politician_id
speech_fts   : FTS5 + trigram。議員の発言のみを索引化（本文は speech 側）
politician   : 名寄せ後の議員マスタ（1,111人）。id は URL に出る
affiliation  : 会派の時系列。party は特定できたときだけ入る（NULL あり）
topic        : 争点語（82件）。リストは data/topics.json。**id は不変**（並び順ではない）
topic_hit    : 争点語 → 発言。**速くするための索引で、正しさの前提ではない**
               （持っていない語はサイトが検索経路で出す。docs/DECISIONS.md）
word         : 2文字語の索引（本文の2文字窓を全部。期間ごとに 31,000〜108,000語）
word_hit     : 2文字語 → 発言
meta         : 期間DBの素性（period / period_rule / from / to / 作った時点の争点語の指紋）
```

**`speech.rowid` は日付の昇順**（`build_db.py` の `load()` が並べ替えている）。
UIの「新しい順」はすべて `ORDER BY rowid DESC` で書く。これは性能上の要請で、
外すと破綻する（[PITFALLS.md](PITFALLS.md)）。

`speaker_kind` は `議員 / 参考人 / 公述人 / 証人 / 政府参考人等 / 非発言`。
**全発言をDBに持ち、全文検索の索引は「議員」だけに張る。**
参考人等は検索対象外だが、前後の文脈表示のためDBに残してある。

### `topic` と `word` は役割が違う

**混ぜないこと。** 検索できる語を増やしたいときに `topics.json` を膨らませない。

| | topic | word |
|---|---|---|
| 何 | 争点語82件。**運営の編集方針** | 本文の2文字窓を全部。**機械生成** |
| 用途 | 頻度推移・会派比較・検索の入口 | 2文字語を引けるようにするだけ |
| 一覧表示 | する（`/topics`） | **しない**（一覧としては見せない） |
| 別表記 | `variants` で合算する | 無い |
| 人名 | 落とす（一覧に出るので事故になる） | 落とさない（「石破」を引けてよい） |
| 期間ごとの一致 | **揃っていること**（`topic_id` がずれる） | 揃っていなくてよい |

### 会派と政党

**`affiliation.party` には NULL がある**（発言の3.6%）。
統一会派の分は `data/party_overrides.json` に人力で入れて解消済み（141件）。
残る NULL は `無所属` `各派に属しない議員` `有志の会` `沖縄の風` `碧水会` `改革の会` で、
**会派名から政党を決められないもの**（設計どおり。議長は自党の会派を抜けるが党籍は残る）。

**`party = '無所属'` と `party IS NULL` は意味が違う。**
前者は「政党に所属していないと分かっている」、後者は「特定できていない」。
政党別の集計で混ぜないこと。

**1会派 = 1政党とは限らない。** 統一会派は名前が変わらないまま構成政党が入れ替わる。
`party_overrides.json` に `periods` を書くと所属レコードが期間で分割される。

**議員IDは `data/politician_ids.json` の台帳で維持する。** URLに出るので
作り直すとリンクが全部壊れる。採番は `scripts/build_politicians.py`。

## 検索の3経路

`site/src/lib/db.ts` の `resolveQuery()` が引き先を1回だけ決め、ページ送りで使い回す。

| 条件 | 引き先 | 速さ |
|---|---|---|
| 争点語に一致し、**配信済みDBが持っている**（`indexed`） | `topic_hit` | 0.7秒。別表記も合算される |
| 2文字以下の語を含む | `word_hit` | FTS では**原理的に引けない**もの |
| それ以外 | FTS5 trigram | 1.5〜6秒 |

**争点語を足しても全期間のDBを作り直さなくてよい。** 配信済みDBが持っていない語は
`indexed` が偽になり、下の2経路に落ちる（結果は同じで、3文字以上の語だけ 3.3倍遅い）。
判定は `scripts/build_db.py` の `stamp_indexed()` が目録と突き合わせて付ける。

**「引けない語」は無い。** 索引に無い2文字語は素直に0件になる。
`resolveQuery()` は**対象の全期間から件数を引いて合算**し、いちばん珍しい2文字語を
起点にする（索引の中身は期間ごとに違うので、1期間だけ見て決めてはいけない）。

**引き先が3つあるということは、直す場所も複数あるということ。**
絞り込みを足すときは結果取得（`ftsSql` / `topicSql` / `wordSql`）と
**`hitSource()`** の両方を直す。`hitSource()` は3経路ぶんの FROM と WHERE を
1か所で組む関数で、**件数（`countQuery()`）と月別（`monthlyQuery()`）が共有する**。

### 月ごとの件数（検索結果のグラフ）

検索語の推移を出すのに、**日付では GROUP BY しない**（当たった発言の行を
1件ずつ読みに行くことになる。実測で 1.0ms → 170.6ms）。`speech.rowid` が
日付の昇順であることを使って、月の先頭 rowid を境界に `CASE` で割る。
索引だけで済むので、**件数を数えるのと同じ手間**で月別が出る。

月の先頭 rowid は `idx_speech_date` の seek（covering index）で採り、
期間 × 世代でキャッシュする。**DBにも目録にも持たせていない** —
持たせると全期間の作り直しと 2.4GB の上げ直しが要る（[DECISIONS.md](DECISIONS.md)）。

### 検索語の正規化

**入口は `query.ts` の `splitTerms()` ひとつ。** 新しく引く経路を足すときも必ず通す。

| | 何を | どの経路に |
|---|---|---|
| `toFullWidth()` | 英数字を全角にする（`A-Za-z0-9` を +0xFEE0） | **3経路すべて** |
| `toWordKey()` | 全角ラテンを大文字に畳む | **word 経路の2文字以下だけ** |
| `canonicalQuery()` | 画面・入力欄・URL に出す形（上の2つを合わせたもの） | 表示 |

会議録の英数字は**全部全角**で、半角で引くと0件になる。
大小の扱いが経路で逆になる理由は [DECISIONS.md](DECISIONS.md)。

### 期間をまたぐ検索

**期間ごとに別ワーカを立てて並列に引き、JS側でマージする**（`mergePages()`）。
1ワーカ内のリクエストは同期XHRで直列になるため、ATTACH+UNION だと本数に比例して遅くなる。

期間DBは日付で綺麗に分かれているので**並べ替えは要らない**。新しい期間から詰めれば
全体が日付の降順になる。ただし期間ごとに LIMIT を掛けているので、
**全体で LIMIT 件に切り、1件も出さなかった期間はカーソルを進めない。**

ページ送りは **OFFSET ではなく rowid の keyset**。

**ファイル数を増やしても検索の費用はほとんど変わらない**（実測・2026-08-12）。
年6本のときの 707リクエスト / 5.6MB に対し、半期12本で **661リクエスト / 5.2MiB**
（`風力発電`・全期間）。開くファイルは倍だが、1本あたりの読みが半分になるため。
数え方は `site/scripts/dev-data-server.js` の `/db/__trace`。

## 配信物（期間DB以外）

| | |
|---|---|
| `data/dist/topics.json`（292KB） | 月×会派の出現件数と**分母（その月の発言数）**。頻度推移ページはこれだけで描ける。**分母で割らずにグラフにしないこと**。会議名×月の分母（`meeting_totals`・22KB）も入っていて、検索を会議名で絞ったときの分母になる。各語の `indexed` は**配信済みDBが `topic_hit` を持っているか**（偽ならサイトが検索経路で出す） |
| `data/dist/trending.json`（10KB） | 直近の国会で急に増えた語。**カレンダー週で区切らない**（発言のあった日を5日ずつまとめる）。中身は「その週に審議された法案の専門用語」が主で、**「今週の争点」ではない** |
| `data/dist/manifest.json` | 期間DBの目録（上記）。ファイル名・大きさ・世代（`?v=`）・収録範囲に加えて、**その期間が持っている争点語**（`topics.ids` / `topics.fp`）を載せる |

## スクリプト

Python 3.12+ / **外部依存パッケージなし**。

| | |
|---|---|
| `ndl_api.py` | NDL APIクライアント。3秒間隔・リトライ・ページングを内蔵 |
| `fetch_range.py` | 月ごとのNDJSONへバックフィル。**再開可能** |
| `build_db.py` | NDJSON → SQLite。FTS5・争点語・2文字語の索引を作る |
| `fetch_wikidata.py` | Wikidata から議員リストを SPARQL で取得 |
| `match_politicians.py` | 名寄せの検証レポート（DBは書き換えない） |
| `build_politicians.py` | 議員マスタを確定させ、ID台帳を更新する。`--fix` で訂正 |
| `build_topics.py` | 争点語の候補出し（`--propose`）と頻度推移の集計 |
| `build_frequent.py` | 頻出語500件の抽出（`dist/frequent.json`） |
| `build_activity.py` | 議員ごとの発言数の推移（`dist/politician_activity.json`） |

`prototype/` は sql.js-httpvfs の性能を測るための実験台で、本番には要らない。
