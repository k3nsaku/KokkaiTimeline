# CLAUDE.md

国会会議録から政治家の発言を収集・構造化し、横断検索と争点語の推移を提供する
**完全静的サイト**。概要は [README.md](README.md)。

## 絶対に守る制約

この3つはプロジェクトの前提であり、他のすべての判断に優先する。

1. **月1,000円以内**（年12,000円）。実際にかかるのはドメイン代のみ（月250円）
2. **常時稼働プロセスを持たない。** サーバーもDBサーバーもAPIサーバーも立てない
3. **運用工数は月1時間以下。** 壊れて困るものを作らない

やらないことは [docs/SCOPE.md](docs/SCOPE.md)。特に重要なのは
**独自の点数付け・ランキング・議員の評価をしない**（公職選挙法・名誉毀損リスク）、
**報道記事を収集しない**、**LLMに立場変化を判定させない**の3つ。

## 触る前に読むもの

| 何をするとき | 読む |
|---|---|
| **常に** | この4行下の「よくある事故」と、`OPERATIONS.local.md`（期限切れの作業を知らせる） |
| 仕様を知りたい | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 「なぜそうなっているか」を疑う | [docs/DECISIONS.md](docs/DECISIONS.md) — **数字はすべて実測値** |
| **検索・DB構築・配信を触る** | [docs/PITFALLS.md](docs/PITFALLS.md) — **全部いちど実際に踏んだもの** |
| サイトを実装する | [site/README.md](site/README.md) |
| 配信・日次更新を触る | [docs/PIPELINE.md](docs/PIPELINE.md) |
| 誤りを指摘された | [docs/CORRECTIONS.md](docs/CORRECTIONS.md) |
| データを取り直す | [docs/BACKFILL.md](docs/BACKFILL.md) |
| 次に何をするか決める | [docs/ROADMAP.md](docs/ROADMAP.md) |

**設計判断を変えるときは [docs/DECISIONS.md](docs/DECISIONS.md) の実測値を先に確認すること。**
そこにある数字はすべて実測であり、推測ではない。

## よくある事故（詳細は PITFALLS.md）

- **「新しい順」は `ORDER BY rowid DESC`。`date` で並べない**（検索で204MB転送）
- **検索語の正規化は `query.ts` の `splitTerms()` を必ず通す。** 会議録の英数字は全部全角で、
  半角のままだと黙って0件。**NFKC は逆方向なので使わない**
- **大文字に畳むのは word 経路の2文字以下だけ。** FTS 経路で畳むと `ＳＤＧｓ` が壊れる
- **検索の引き先は3経路ある。** 絞り込みを足すときは結果取得と件数の**両方を3経路とも**直す
- **`topics.json` を変えたら全期間のDBを作り直す**（`topic_id` がずれて別の争点を引く）
- **頻出語を頻度順に並べない**（実測の上位は `国務大` `日本` `重要` `関係`）。
  並べるのは burst。`frequent.json` は期間DBの作り直しが要らない
- **配信DBの分割規則は2か所にある。** `build_db.py` の `period_of()` と
  `query.ts` の `periodOf()`。**片方だけ変えると存在しないファイルを引きに行く**
- **1ファイルが 512MB を超えると黙って CDN キャッシュから外れる**（RTT 8ms → 77ms）。
  半期分割の最大は 364MB。`--period` を変える判断は DECISIONS の実測値を見てから
- **`data/politician_ids.json` を失うとURLが全部変わる。** 手書き資産はコミットする
- **インラインの `style` 属性を書かない**（CSP に黙って消され、見た目だけ静かに壊れる。
  公開時から会派バーが全部同じ長さだった）。色や幅は SVG の `fill` / `width` で出す

## 運営が定期的にやること

**セッションの最初に `OPERATIONS.local.md` を読み、推奨間隔を過ぎている作業があれば
運営者に知らせること。** 黙って実行しない — どれも判断か手入力が要る。

`OPERATIONS.local.md` は `.gitignore`（この端末だけの実績）。**無ければ下の表から
全部「未実施」で作ること。** 仕様はこの表、実績はあちら。

| 作業 | 推奨間隔 | きっかけ・材料 |
|---|---|---|
| 日次更新の成否を見る | 1か月 | `gh run list --workflow=daily.yml`。落ちても数日は放置できる |
| **議員ID台帳の書き戻しを確認** | 議員が増えた日に1回 | ★まだ一度も通っていない。**落ちたまま放置すると台帳を失い公開後のURLが全部変わる** |
| 頻出語のノイズを denylist に入れる | 3か月 | `reports/frequent_words.md` → `data/topic_denylist.json` に1行 |
| 政党の手入力 | 3か月 | `reports/party_todo.md` → `data/party_overrides.json`（[docs/CORRECTIONS.md](docs/CORRECTIONS.md)） |
| 争点語のレビュー | 6か月 | `reports/trending_new_terms.md` → `data/topics.json`。**全期間のDBの作り直しを伴う** |
| 連絡先の疎通確認 | 6か月 | 1通送る。**届かない窓口は「窓口が無い」のと同じ** |
| R2 とドメインの費用を見る | 1か月 | 月1,000円以内が絶対の制約 |
| 配信DBの最大サイズを見る | 1か月 | 日次が `manifest.json` を見て 480MB で落とす。落ちたら分割を細かくする |

**`reports/` は日次のランナー上でだけ作られる**（`.gitignore`）。中身は Actions の
Artifacts（`reports` / 90日保持）から読む。要約に出ているのは件数だけ。

**2文字語の語彙更新はもう要らない**（索引が本文から作られるようになったため）。
全期間の作り直しが要るのは争点語を変えたときだけ。手順は
[docs/PIPELINE.md](docs/PIPELINE.md)「★争点語を変えるとき」。

## コマンド

Python 3.12+ のみ。**外部依存パッケージなし**（GitHub Actions で `pip install` を不要にするため）。

```bash
# 取得（中断しても同じコマンドで再開できる）
python scripts/fetch_range.py --from 2021-01-01 --until 2026-07-31
python scripts/fetch_range.py --from 2021-01-01 --until 2026-07-31 --status

# DB構築
python scripts/build_db.py --fresh                                # 単一DB
python scripts/build_db.py --split --page-size 8192               # 配信用の期間DB（半期）
python scripts/build_db.py --id 2026H2 --page-size 8192           # 日次は触った期間だけ

# 議員マスタ
python scripts/fetch_wikidata.py
python scripts/match_politicians.py                # 検証レポートだけ（DBは書き換えない）
python scripts/build_politicians.py                # 確定させる（→ reports/party_todo.md）
python scripts/build_politicians.py --fix 浜田聡   # 所属政党の訂正（docs/CORRECTIONS.md）

# 集計
python scripts/build_topics.py --propose   # 争点語の候補（data/topics.json は手で選ぶ）
python scripts/build_topics.py             # 頻度推移（dist/topics.json・trending.json）
python scripts/build_frequent.py           # 頻出語500語（dist/frequent.json）
python scripts/build_activity.py           # 議員ごとの発言数の推移（dist/politician_activity.json）
```

**順番は `fetch_range` → `build_db`（単一DB）→ `build_politicians` / `build_topics`
/ `build_frequent` → `build_db --split`。**
集計スクリプトは `data/kokkai.db` を材料にするので、単一DBが先に要る。

**★`build_activity.py` だけは `build_politicians.py` の後に `build_db` を回し直した
単一DBが要る。** `speech.politician_id` は「`politicians.json` があるときに `build_db` を
回した」ときだけ入るため（初回は NULL のまま）。入っていなければその場で落ちる。

### サイト（`site/`）

Node 24 / Astro。

```bash
cd site && npm install
npm run dev      # http://localhost:4321。data/dist を /db で配る（DBはコピーしない）
npm run build    # dist/ に 1,702ページ・22MB
npm run check    # 型検査 + テスト120件
```

**検索まわりを触ったら `npm run check` を通すこと。** `src/lib/query.ts`
（SQLの組み立てと期間またぎのページ送り）は sql.js-httpvfs に依存しない純粋な
関数にしてあり、`site/test/` から直接呼んで検証している。**ここに
sql.js-httpvfs を持ち込むとテストが動かなくなる。**

## データモデル（要点だけ。詳細は ARCHITECTURE.md）

```
meeting / speech / speech_fts / politician / affiliation
topic      : 争点語82件。**運営の編集方針**（data/topics.json）
word       : 2文字語の索引。**本文の2文字窓を全部**（期間ごとに 31,000〜106,000語）
frequent   : 頻出語500件。**機械抽出の一覧**（dist/frequent.json・DBには入らない）
```

**この3つは役割が違う。混ぜないこと。**

- `topic` は「何を争点と呼ぶか」という**編集方針**。増やすほど意味が薄まるので膨らませない
- `word` は2文字語を引くための**索引**。一覧としては表示しない。
  **語彙リストは持たない**ので、期間ごとに中身が違ってよい（無い語は素直に0件）
- `frequent` は「いつ・どれだけ議論されたか」を見せる**一覧**。運営は語を選ばない

**頻出語の選定は自立した run、集計は部分文字列**（数え方が違うのは意図的。
揃えないと一覧の件数と検索結果が食い違う）。詳細は PITFALLS.md。

**`speech.rowid` は日付の昇順。** UIの「新しい順」はこれに依存する。

**`affiliation.party` は NULL がありうる**（会派名から政党を決められないもの）。
`party = '無所属'` とは意味が違うので、政党別の集計で混ぜない。
