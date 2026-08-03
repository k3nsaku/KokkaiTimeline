# アーキテクチャ

**いまどうなっているかを書く。なぜそうなったかは [DECISIONS.md](DECISIONS.md)、
触るときに踏む穴は [PITFALLS.md](PITFALLS.md)。**

```
[GitHub Actions（日次 cron）]
    ↓ NDL 国会会議録API（前月と当月だけ取り直す）
    ↓ SQLite を生成（FTS5 + trigram / 年ごとに1ファイル）
    ↓ Cloudflare R2 にアップロード（変わった年のDBと目録だけ）
[Cloudflare Pages] ← 完全静的サイト（Astro / Direct Upload）
    ↓
[ブラウザ] sql.js-httpvfs が HTTP Range で必要な 8KB ページだけ取得
```

常時稼働しているものは無い。パイプラインが失敗しても、サイトは前回のデータで動き続ける。

## 配信

| | |
|---|---|
| サイト | Cloudflare Pages（`kokkai-timeline.com`） |
| DB | Cloudflare R2（`db.kokkai-timeline.com`）。**年ごとに1ファイル**・350〜420MB |
| 目録 | `manifest.json`。サイトはこれで年の一覧を知る |

**DBは年で分ける。** 日次更新で変わるのは当年だけなので、過去年は CDN に載ったまま。
`page_size` は **8192**（ブラウザ側の `requestChunkSize` と一致させる）。

**R2 には Cache Rule が要る。** Cloudflare は拡張子でキャッシュを判定していて、
`.db` も `.json` も既定の対象外。無いと `cf-cache-status: DYNAMIC` のままで
RTT が約10倍（8ms → 77ms）になる。設定は [PIPELINE.md](PIPELINE.md)。

### 目録が持つ2つの指紋

```json
{"year": 2025, "file": "kokkai-2025.db", "size": 409935872,
 "version": "6fd6f7e28daf6b10", "vocabulary": "fa497eb0aaeb0224"}
```

- **`version`（中身の指紋）** — DBのURLに `?v=` で付く。
  **これが無いと、DBを差し替えた瞬間に開いていたページが壊れる**
  （`sql.js-httpvfs` は読んだページをオフセットで覚えているので、
  古いページと新しいページが混ざる）。CDN のパージも要らなくなる
- **`vocabulary`（2文字語の語彙の指紋）** — **年をまたいで一致していなければならない。**
  検索は「語彙は年によらない」前提で最新年だけを見て判定するので、食い違うと
  過去年が黙って0件になる。`build_db.py` が検出して日次ワークフローを失敗させる

## データモデル

```
meeting      : issue_id, 会期, 院, 会議名, 号数, 日付, meeting_url, pdf_url
speech       : speech_id, issue_id, 発言順, 日付, 発言者, よみ, 会派, 肩書, 役割,
               本文, speech_url, is_speech, speaker_kind, politician_id
speech_fts   : FTS5 + trigram。議員の発言のみを索引化（本文は speech 側）
politician   : 名寄せ後の議員マスタ（1,111人）。id は URL に出る
affiliation  : 会派の時系列。party は特定できたときだけ入る（NULL あり）
topic        : 争点語（82件）。リストは data/topics.json
topic_hit    : 争点語 → 発言
word         : 2文字語の語彙（16,264件）。リストは data/words.json
word_hit     : 2文字語 → 発言
meta         : 年DBの素性（version / vocabulary の指紋）
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
| 何 | 争点語82件。**運営の編集方針** | 2文字語16,264件。**機械抽出** |
| 用途 | 頻度推移・会派比較・検索の入口 | 2文字語を引けるようにするだけ |
| 一覧表示 | する（`/topics`） | **しない**（引けるかを答えるだけ） |
| 別表記 | `variants` で合算する | 無い |
| 人名 | 落とす（一覧に出るので事故になる） | 落とさない（「石破」を引けてよい） |

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
| 争点語（`topics.json`）に一致 | `topic_hit` | 0.7秒。別表記も合算される |
| 2文字以下の語を含む | `word_hit` | FTS では**原理的に引けない**もの |
| それ以外 | FTS5 trigram | 1.5〜6秒 |
| 2文字語が語彙にも無い | 引けない | **黙って0件にせず**、含む争点語を出す |

**引き先が3つあるということは、直す場所も3つあるということ。**
絞り込みを足すときは結果取得（`ftsSql` / `topicSql` / `wordSql`）と
件数（`countQuery()`）の**両方を3経路すべて**直す。

### 検索語の正規化

**入口は `query.ts` の `splitTerms()` ひとつ。** 新しく引く経路を足すときも必ず通す。

| | 何を | どの経路に |
|---|---|---|
| `toFullWidth()` | 英数字を全角にする（`A-Za-z0-9` を +0xFEE0） | **3経路すべて** |
| `toWordKey()` | 全角ラテンを大文字に畳む | **word 経路の2文字以下だけ** |
| `canonicalQuery()` | 画面・入力欄・URL に出す形（上の2つを合わせたもの） | 表示 |

会議録の英数字は**全部全角**で、半角で引くと0件になる。
大小の扱いが経路で逆になる理由は [DECISIONS.md](DECISIONS.md)。

### 年をまたぐ検索

**年ごとに別ワーカを立てて並列に引き、JS側でマージする**（`mergePages()`）。
1ワーカ内のリクエストは同期XHRで直列になるため、ATTACH+UNION だと年数に比例して遅くなる。

年DBは日付で綺麗に分かれているので**並べ替えは要らない**。新しい年から詰めれば
全体が日付の降順になる。ただし年ごとに LIMIT を掛けているので、
**全体で LIMIT 件に切り、1件も出さなかった年はカーソルを進めない。**

ページ送りは **OFFSET ではなく rowid の keyset**。

## 配信物（年DB以外）

| | |
|---|---|
| `data/dist/topics.json`（258KB） | 月×会派の出現件数と**分母（その月の発言数）**。頻度推移ページはこれだけで描ける。**分母で割らずにグラフにしないこと** |
| `data/dist/trending.json`（10KB） | 直近の国会で急に増えた語。**カレンダー週で区切らない**（発言のあった日を5日ずつまとめる）。中身は「その週に審議された法案の専門用語」が主で、**「今週の争点」ではない** |
| `data/dist/manifest.json` | 年DBの目録（上記） |

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
| `build_words.py` | 2文字語の語彙を抽出する |

`prototype/` は sql.js-httpvfs の性能を測るための実験台で、本番には要らない。
