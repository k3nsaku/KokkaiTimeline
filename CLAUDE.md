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
| **常に** | この4行下の「よくある事故」 |
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
- **`topics.json` を変えたら全年のDBを作り直す**（`topic_id` がずれて別の争点を引く）
- **`build_words.py` を日次で回さない**（過去年の検索が黙って0件になる）
- **`data/politician_ids.json` を失うとURLが全部変わる。** 手書き資産はコミットする

## コマンド

Python 3.12+ のみ。**外部依存パッケージなし**（GitHub Actions で `pip install` を不要にするため）。

```bash
# 取得（中断しても同じコマンドで再開できる）
python scripts/fetch_range.py --from 2021-01-01 --until 2026-07-31
python scripts/fetch_range.py --from 2021-01-01 --until 2026-07-31 --status

# DB構築
python scripts/build_db.py --fresh                                # 単一DB
python scripts/build_db.py --split-by-year --page-size 8192       # 配信用の年DB
python scripts/build_db.py --year 2026 --page-size 8192           # 日次は当年だけ

# 議員マスタ
python scripts/fetch_wikidata.py
python scripts/match_politicians.py                # 検証レポートだけ（DBは書き換えない）
python scripts/build_politicians.py                # 確定させる（→ reports/party_todo.md）
python scripts/build_politicians.py --fix 浜田聡   # 所属政党の訂正（docs/CORRECTIONS.md）

# 集計
python scripts/build_topics.py --propose   # 争点語の候補（data/topics.json は手で選ぶ）
python scripts/build_topics.py             # 頻度推移（dist/topics.json・trending.json）
python scripts/build_words.py              # 2文字語の語彙（data/words.json）
```

**順番は `fetch_range` → `build_db`（単一DB）→ `build_politicians` / `build_topics`
/ `build_words` → `build_db --split-by-year`。**
集計スクリプトは `data/kokkai.db` を材料にするので、単一DBが先に要る。

### サイト（`site/`）

Node 24 / Astro。

```bash
cd site && npm install
npm run dev      # http://localhost:4321。data/dist を /db で配る（DBはコピーしない）
npm run build    # dist/ に 1,201ページ・18MB
npm run check    # 型検査 + テスト54件
```

**検索まわりを触ったら `npm run check` を通すこと。** `src/lib/query.ts`
（SQLの組み立てと年またぎのページ送り）は sql.js-httpvfs に依存しない純粋な
関数にしてあり、`site/test/` から直接呼んで検証している。**ここに
sql.js-httpvfs を持ち込むとテストが動かなくなる。**

## データモデル（要点だけ。詳細は ARCHITECTURE.md）

```
meeting / speech / speech_fts / politician / affiliation
topic      : 争点語82件。**運営の編集方針**（data/topics.json）
word       : 2文字語16,264件。**機械抽出**（data/words.json）
```

**`topic` と `word` は役割が違う。混ぜないこと。**
検索できる語を増やしたいときに `topics.json` を膨らませない
（そのための第3の層が ROADMAP の「頻出語レイヤー」）。

**`speech.rowid` は日付の昇順。** UIの「新しい順」はこれに依存する。

**`affiliation.party` は NULL がありうる**（会派名から政党を決められないもの）。
`party = '無所属'` とは意味が違うので、政党別の集計で混ぜない。
