# アーキテクチャ

**いまどうなっているかだけを書く。** なぜそうなったかは [DECISIONS.md](DECISIONS.md)、
触るときに踏む穴は [PITFALLS.md](PITFALLS.md)。

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
| DB | Cloudflare R2（`db.kokkai-timeline.com`）。**半期ごとに1ファイル**・最大 360MB 前後（満期の上半期） |
| 目録 | `manifest.json`。サイトはこれで期間の一覧と引き先のファイル名を知る |

- **DBは半期で割る**（`2026H1` = 1〜6月）。日次で変わるのはいま開いている期間だけ
- **分割規則は `build_db.py` の `period_of()` と `query.ts` の `periodOf()` の2か所**
- `page_size` は **8192**（ブラウザ側の `requestChunkSize` と一致させる）
- 利用者に見せる絞り込みは**年**。期間が年に閉じているので取りこぼしが出ない
  （`periodsInYearRange()`）
- **R2 には Cache Rule が要る**（`.db` も `.json` も既定のキャッシュ対象外）。
  設定は [PIPELINE.md](PIPELINE.md) 手順3.5

### 目録

```json
{"period": "half",
 "periods": ["2021H1", "…", "2026H2"],
 "databases": [{"id": "2025H1", "file": "kokkai-2025H1.db", "size": 356659200,
                "version": "6fd6f7e28daf6b10",
                "from": "2025-01-22", "to": "2025-06-21",
                "topics": {"ids": "1-82", "fp": "70b47bf2c8276c63"}}]}
```

| 項目 | 意味 |
|---|---|
| `version` | 中身の指紋。DBのURLに `?v=` で付く。**世代分離とCDNパージ回避を兼ねる** |
| `file` | 引き先。**目録の記載を使う**（規則で組み立てない） |
| `from` / `to` | 実データの収録範囲 |
| `topics` | **その期間が持っている争点語**（`ids` と指紋）。`stamp_indexed()` が照合する |

## データモデル

```
meeting      : issue_id, 会期, 院, 会議名, 号数, 日付, meeting_url, pdf_url
speech       : speech_id, issue_id, 発言順, 日付, 発言者, よみ, 会派, 肩書, 役割,
               本文, speech_url, is_speech, speaker_kind, politician_id
speech_fts   : FTS5 + trigram。議員の発言のみを索引化（本文は speech 側）
politician   : 名寄せ後の議員マスタ（約1,100人）。id は URL に出る
affiliation  : 会派の時系列（約1,800行）。party は特定できたときだけ入る（NULL あり）
topic        : 争点語（82件）。リストは data/topics.json。**id は不変**
topic_hit    : 争点語 → 発言。**速くするための索引で、正しさの前提ではない**
word         : 2文字語の索引（本文の2文字窓を全部）。期間ごとに1万〜11万語（開始直後の期間は少ない）
word_hit     : 2文字語 → 発言
meta         : 期間DBの素性（period / period_rule / from / to / 争点語の指紋）
```

**収録は約65万発言 / うち議員の発言が約51万件**（78.7%）。
**正確な件数は日々変わる**ので、サイトの `/about` が配信物から出している。

- **`speech.rowid` は日付の昇順**（`build_db.py` の `load()` が並べ替えている）。
  UIの「新しい順」はすべて `ORDER BY rowid DESC`
- `speaker_kind` は `議員 / 参考人 / 公述人 / 証人 / 政府参考人等 / 非発言`。
  **全発言をDBに持ち、全文検索の索引は「議員」だけに張る**（参考人等は文脈表示に使う）

### `topic` と `word` は役割が違う

**混ぜないこと。** 検索できる語を増やしたいときに `topics.json` を膨らませない。

| | topic | word |
|---|---|---|
| 何 | 争点語82件。**運営の編集方針** | 本文の2文字窓を全部。**機械生成** |
| 用途 | 頻度推移・会派比較・検索の入口 | 2文字語を引けるようにするだけ |
| 一覧表示 | する（`/topics`） | **しない** |
| 別表記 | `variants` で合算する | 無い |
| 人名 | 落とす（一覧に出るので事故になる） | 落とさない（「石破」を引けてよい） |
| 期間ごとの一致 | 揃っていなくてよい（持っている期間だけ使う） | 揃っていなくてよい |

`frequent`（頻出語500件）は**DBに入らない**。`dist/frequent.json` として配る一覧で、
選定は「自立した run」・集計は部分文字列と、**数え方が意図的に違う**（[PITFALLS.md](PITFALLS.md)）。

### 会派と政党

- **`affiliation.party` には NULL がある**（議員の発言の 3.6%）。統一会派の分は
  `data/party_overrides.json` に人力で入れてある（約150件）
- 残る NULL は `無所属` `各派に属しない議員` `有志の会` `沖縄の風` `碧水会` `改革の会`。
  **会派名から政党を決められないもの**（設計どおり）
- **`party = '無所属'` と `party IS NULL` は意味が違う。** 前者は「政党に所属して
  いないと分かっている」、後者は「特定できていない」。政党別の集計で混ぜない
- **1会派 = 1政党とは限らない。** `party_overrides.json` に `periods` を書くと
  所属レコードが期間で分割される
- **議員IDは `data/politician_ids.json` の台帳で維持する**（URLに出る）。
  採番は `scripts/build_politicians.py`

## 検索の3経路

`site/src/lib/db.ts` の `resolveQuery()` が引き先を1回だけ決め、ページ送りで使い回す。

| 条件 | 引き先 | 速さ |
|---|---|---|
| 争点語に一致し、**配信済みDBが持っている**（`indexed`） | `topic_hit` | 0.7秒。別表記も合算される |
| 2文字以下の語を含む | `word_hit` | FTS では**原理的に引けない**もの |
| それ以外 | FTS5 trigram | 1.5〜6秒 |

- **争点語を足しても全期間のDBを作り直さなくてよい。** 持っていない語は `indexed` が
  偽になり、下の2経路に落ちる（結果は同じで、3文字以上の語だけ 3.3倍遅い）。
  判定は `build_db.py` の `stamp_indexed()`
- **「引けない語」は無い。** 索引に無い2文字語は素直に0件になる
- `resolveQuery()` は**対象の全期間から件数を引いて合算**し、いちばん珍しい2文字語を
  起点にする（索引の中身は期間ごとに違う）
- **引き先が3つある ＝ 直す場所も複数ある。** 絞り込みを足すときは結果取得
  （`ftsSql` / `topicSql` / `wordSql`）と **`hitSource()`** の両方。`hitSource()` は
  3経路ぶんの FROM と WHERE を1か所で組み、**件数（`countQuery()`）と
  月別（`monthlyQuery()`）が共有する**

### 月ごとの件数（検索結果のグラフ）

`speech.rowid` が日付の昇順であることを使い、**月の先頭 rowid を境界に `CASE` で割る**
（日付では GROUP BY しない）。索引だけで済むので、件数を数えるのと同じ手間で出る。

月の先頭 rowid は `idx_speech_date` の seek（covering index）で採り、
期間 × 世代でキャッシュする。**DBにも目録にも持たせていない。**

### 検索語の正規化

**入口は `query.ts` の `splitTerms()` ひとつ。** 新しく引く経路を足すときも必ず通す。

| | 何を | どの経路に |
|---|---|---|
| `toFullWidth()` | 英数字を全角にする（`A-Za-z0-9` を +0xFEE0） | **3経路すべて** |
| `toWordKey()` | 全角ラテンを大文字に畳む | **word 経路の2文字以下だけ** |
| `canonicalQuery()` | 画面・入力欄・URL に出す形（上の2つ＋語数と長さの上限） | 表示 |

会議録の英数字は**全部全角**で、半角で引くと0件になる。
大小の扱いが経路で逆になる理由は [DECISIONS.md](DECISIONS.md) §4。

### 期間をまたぐ検索

- **期間ごとに別ワーカを立てて並列に引き、JS側でマージする**（`mergePages()`）。
  1ワーカ内のリクエストは同期XHRで直列になるため
- 期間DBは日付で綺麗に分かれているので**並べ替えは要らない**。新しい期間から詰める
- 期間ごとに LIMIT を掛けているので、**全体で LIMIT 件に切り、1件も出さなかった
  期間はカーソルを進めない**
- ページ送りは **OFFSET ではなく rowid の keyset**

## 配信物（期間DB以外）

| | |
|---|---|
| `dist/topics.json`（292KB） | 月×会派の出現件数と**分母**（`speech_totals`）。会議名×月の分母（`meeting_totals`・22KB）も入る。各語の `indexed` は**配信済みDBが `topic_hit` を持っているか** |
| `dist/frequent.json` | 頻出語500件。月の系列と会期ごとの件数（`sessions`） |
| `dist/politician_activity.json` | 議員ごとの発言数の推移（月×委員会） |
| `dist/trending.json`（10KB） | 直近で急に増えた語。**カレンダー週で区切らない**（発言のあった日を5日ずつ）。中身は「その週に審議された法案の専門用語」が主 |
| `dist/manifest.json` | 期間DBの目録（上記） |

## スクリプト

Python 3.12+ / **外部依存パッケージなし**。

| | |
|---|---|
| `ndl_api.py` | NDL APIクライアント。3秒間隔・リトライ・ページングを内蔵 |
| `fetch_range.py` | 月ごとのNDJSONへバックフィル。**再開可能**（[BACKFILL.md](BACKFILL.md)） |
| `build_db.py` | NDJSON → SQLite。FTS5・争点語・2文字語の索引を作る |
| `fetch_wikidata.py` | Wikidata から議員リストを SPARQL で取得 |
| `match_politicians.py` | 名寄せの検証レポート（DBは書き換えない） |
| `build_politicians.py` | 議員マスタを確定させ、ID台帳を更新する。`--fix` で訂正 |
| `build_topics.py` | 争点語の候補出し（`--propose`）と頻度推移の集計 |
| `build_frequent.py` | 頻出語500件の抽出 |
| `build_activity.py` | 議員ごとの発言数の推移 |
| `verify_dist.py` | **配る前**の検証（関門）。[PIPELINE.md](PIPELINE.md) 手順7 |
| `verify_published.py` | **配ったあと**の検算（知らせるだけ）。手順13 |
| `admin.py` | **運営コンソール**。`127.0.0.1` にしか listen しない手元だけの道具 |

`prototype/` は sql.js-httpvfs の性能を測るための実験台で、本番には要らない。
