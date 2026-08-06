# 国会タイムライン

国会会議録から政治家の発言を集めて構造化し、**横断検索と、争点語の推移**を出す
完全静的サイト。有権者が政治家について判断する材料を出すことを目的にしている。

- サイト: **<https://kokkai-timeline.com>**（公開中。日次で自動更新）
- 収録: **2021年1月以降 / 約65万発言 / 約6,200会議 / 議員約1,100人**
  — 正確な件数は[サイトの「このサイトについて」](https://kokkai-timeline.com/about)にある
  （毎日変わるので、ここには概数だけ書く）
- 出典: [国立国会図書館 国会会議録検索システム API](https://kokkai.ndl.go.jp/api.html)

## 何ができるか

| | |
|---|---|
| **争点語の頻度推移** | 82件の争点語について、いつ・どの会派がどれだけ使ったかを月ごとに出す。**発言数で割った率**で見せる（国会は通年で開いていないので、割らないと開催日数の多い月が争点に見える） |
| **頻出語** | 会議録から機械的に抽出した500語。**運営は語を選ばない**（争点語とは別の層）。ある時期に集中して使われた順に並べ、語ごとに月次の推移を出す |
| **横断検索** | 議員 × キーワード × 期間 × 会議名。該当箇所をハイライトする |
| **議員ごとのタイムライン** | 約1,100人ぶん。発言を新しい順に並べ、所属会派の変遷も出す |
| **発言のパーマリンク** | 1発言 = 1URL。前後の発言も表示する |

**やらないことがある。** 独自の点数付け・ランキング・議員の評価はしない。
LLM に「立場が変わったか」を判定させて事実として出すこともしない。
理由は [docs/SCOPE.md](docs/SCOPE.md)。

## 設計の前提

この2つが他のすべての判断に優先する。

1. **常時稼働プロセスを持たない。** サーバーもDBサーバーもAPIサーバーも立てない
2. **運用工数は月1時間以下。** 壊れて困るものを作らない

## どう動いているか

```
[GitHub Actions（日次）]
    ↓ NDL 国会会議録API（差分のみ）
    ↓ SQLite を生成（FTS5 + trigram / 年ごとに1ファイル）
    ↓ Cloudflare R2 にアップロード
[Cloudflare Pages] ← 完全静的サイト（Astro）
    ↓
[ブラウザ] sql.js-httpvfs が HTTP Range で必要なページだけ取得
```

**検索はブラウザの中で完結する。** 検索語はどこにも送られない。
ブラウザが 350〜420MB の SQLite を「必要な 8KB ページだけ」HTTP Range で
読みながら引く。詳しくは [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## リポジトリの構成

```
scripts/     データ取得・DB構築・名寄せ・集計（Python 3.12・外部依存なし）
site/        公開サイト（Astro / Node 24）
prototype/   sql.js-httpvfs の性能計測に使った実験台
data/        生成物。.gitignore 済み（手で維持している5ファイルだけコミットする）
docs/        設計・運用のドキュメント
```

`data/` の中で**コミットするのは手書きの資産だけ**（`politician_ids.json`
`topics.json` `party_map.json` `party_overrides.json` `topic_denylist.json`）。
生成物は再構築できるので置かない。

## 動かす

Python 3.12+ と Node 24。**Python 側に外部依存パッケージは無い**
（GitHub Actions で `pip install` を不要にするため）。

```bash
# 1. 会議録を取る（中断しても同じコマンドで再開できる。全期間で約5時間）
python scripts/fetch_range.py --from 2021-01-01 --until 2026-07-31

# 2. 単一DBを作る（このあとの集計スクリプトの材料になる）
python scripts/build_db.py --fresh

# 3. 議員マスタ・争点語・2文字語の語彙・頻出語を作る
python scripts/fetch_wikidata.py
python scripts/build_politicians.py
python scripts/build_topics.py
python scripts/build_words.py
python scripts/build_frequent.py

# 4. 配信用の年ごとDBを作る（data/dist/kokkai-YYYY.db）
python scripts/build_db.py --split-by-year --page-size 8192
```

**順番は入れ替えられない。** 3 は単一DBを読むので 2 が要り、
4 は 3 の生成物（議員マスタ・争点語・語彙）を年DBに埋め込む。

```bash
cd site && npm install
npm run dev      # http://localhost:4321（data/dist を /db で配る。コピーはしない）
npm run build    # dist/ に 1,691ページ
npm run check    # 型検査 + テスト54件
```

**検索まわりを触ったら `npm run check` を通すこと。**
「黙って誤った結果を出す」たぐいの壊れ方をする場所なので、回帰テストで固めてある。

## ドキュメント

| | |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | **決まっている仕様。** データモデル・配信構成・検索の3経路 |
| [docs/DECISIONS.md](docs/DECISIONS.md) | **なぜそうなっているか。** 実測値つきの決定一覧 |
| [docs/PITFALLS.md](docs/PITFALLS.md) | **DO / DON'T。** 実際に踏んだ穴と、守らないと壊れる制約 |
| [docs/SCOPE.md](docs/SCOPE.md) | やらないことと、その法的な理由 |
| [docs/PIPELINE.md](docs/PIPELINE.md) | 日次更新の運用と Cloudflare 側の設定 |
| [docs/BACKFILL.md](docs/BACKFILL.md) | 会議録を取り直す手順 |
| [docs/CORRECTIONS.md](docs/CORRECTIONS.md) | 所属政党などの訂正依頼への対応手順 |
| [docs/ROADMAP.md](docs/ROADMAP.md) | これからやること |
| [site/README.md](site/README.md) | サイトの実装 |

## データの扱い

発言そのものは国会会議録（公的記録）で、著作権法40条1項により政治上の演説等は
自由に利用できる。ただし但し書きが「同一の著作者のものを編集して利用する場合」を
除いているため、**特定議員の発言だけを集めた編集物に見えない設計**にしてある
（一覧は抜粋＋原典リンクに留め、全文は会議という文脈の中で出す）。
**全レコードに原典URLを付ける。**

名寄せ・会派から政党への対応づけ・抜粋位置には限界がある。
サイト上の `/disclaimer` に書いてあるとおり、**誤りは指摘があれば直す**
（手順は [docs/CORRECTIONS.md](docs/CORRECTIONS.md)）。

このリポジトリの法的整理は専門家のレビューを経ていない。

## ライセンス

**[MIT](LICENSE)。** 対象は**このリポジトリに入っているもの**
（Python スクリプト・サイトの実装・ドキュメント・手書きの `data/*.json` 5ファイル）。

**会議録データには及ばない。** 本文はこのリポジトリに1バイトも入っていない
（`data/raw/` と `data/*.db` は `.gitignore`）ので、こちらが再配布しておらず、
ライセンスを付ける立場にない。実体は実行時に NDL の API から取る。

範囲の詳細は [NOTICE.md](NOTICE.md)、データ側の法的整理は
上の「データの扱い」と [docs/SCOPE.md](docs/SCOPE.md)。
