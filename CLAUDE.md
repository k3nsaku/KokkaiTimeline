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
| **配信・CI・運営コンソールの安全性を触る** | [docs/SECURITY.md](docs/SECURITY.md) — **残っている手当ては運営者の手にしかない** |
| 誤りを指摘された | [docs/CORRECTIONS.md](docs/CORRECTIONS.md) |
| データを取り直す | [docs/BACKFILL.md](docs/BACKFILL.md) |
| 次に何をするか決める | [docs/ROADMAP.md](docs/ROADMAP.md) |

**設計判断を変えるときは [docs/DECISIONS.md](docs/DECISIONS.md) の実測値を先に確認すること。**
そこにある数字はすべて実測であり、推測ではない。

**実装とドキュメントの不一致は禁止。**
少なくともコミット時点で実装とドキュメントの整合性を取ること。

## よくある事故（詳細は PITFALLS.md）

- **「新しい順」は `ORDER BY rowid DESC`。`date` で並べない**（検索で204MB転送）
- **検索語の正規化は `query.ts` の `splitTerms()` を必ず通す。** 会議録の英数字は全部全角で、
  半角のままだと黙って0件。**NFKC は逆方向なので使わない**
- **大文字に畳むのは word 経路の2文字以下だけ。** FTS 経路で畳むと `ＳＤＧｓ` が壊れる
- **検索の引き先は3経路ある。** 絞り込みを足すときは結果取得（`ftsSql` / `topicSql` /
  `wordSql`）と `hitSource()` の**両方**を直す（件数と月別は `hitSource()` を共有する）
- **月ごとに数えるとき `date` で GROUP BY しない**（当たった発言の行を1件ずつ読む。
  実測 1.0ms → 170.6ms）。`rowid` の範囲で割る。**前提は「rowid の昇順 ＝ 日付の昇順」**で、
  崩れると**件数は合ったまま月だけずれる**
- **争点語の `id` は `topics.json` に書いてある不変の識別子。並び順から採らない。**
  **消した id は再利用しない**（配信済みDBの `topic_hit` を別の争点として引く）。
  **語を足すだけなら全期間の作り直しは要らない**（`indexed` が偽なら検索経路で出る）。
  **消す・書き直す・`variants` を足すときは要る**（照合できないものは全部
  検索経路に落ちる ＝ 正しいまま遅くなる。[docs/PIPELINE.md](docs/PIPELINE.md)）
- **頻出語を頻度順に並べない**（実測の上位は `国務大` `日本` `重要` `関係`）。
  並べるのは burst。`frequent.json` は期間DBの作り直しが要らない
- **配信DBの分割規則は2か所にある。** `build_db.py` の `period_of()` と
  `query.ts` の `periodOf()`。**片方だけ変えると存在しないファイルを引きに行く**
- **1ファイルが 512MB を超えると黙って CDN キャッシュから外れる**（RTT 8ms → 77ms）。
  半期分割の最大は 364MB。`--period` を変える判断は DECISIONS の実測値を見てから
- **`data/politician_ids.json` を失うとURLが全部変わる。** 手書き資産はコミットする
- **R2 へ上げるとき `--content-type` を省かない**（[docs/PIPELINE.md](docs/PIPELINE.md)）。
  省くと aws-cli が拡張子から推測する ＝ **ランナー次第で付いたり付かなかったりする。**
  実際 2026-08-18 に日次が差し替えた1本だけヘッダが欠けた。**エラーは出ない**
- **検索語を `?q=` に置かない。** HTTPS でも**配信事業者にURLは届く**。
  条件は `#` に持たせてある（`search.astro`）。**内部リンクも `/search#q=`**。
  フラグメント化しても JS 無効では `?q=` に落ちるので、
  **「どこにも送られません」とは書けない**（文面は index / privacy / about の3ページ）。
  **`#` は要求に載らないだけで、そのページのスクリプトからは読める** ——
  `/search` に解析タグを出さないのはこのため（`operator.ts` の `ANALYTICS_EXCLUDED_PATHS`）
- **検索語の上限は `canonicalQuery()` で掛ける**（`splitTerms()` だけだと
  画面とURLに切る前の語が残る）。URLは `pushUrl` が偽でも書き戻す。
  **URL自体は `URLSearchParams` に通す前に切る**（`maxlength` は JS の代入を止めない）
- **運営コンソールの保存は `revision` を突き合わせる**（`SAVE_LOCK` は同時実行しか
  止められない。**開きっぱなしの別タブが古い一覧で上書きする**）
- **秘密をワークフロー直下の `env:` に置かない**（全 step から見える ＝ `npm ci` の
  postinstall からも見える）。**actions は SHA 固定・wrangler は lockfile 固定**
- **インラインの `style` 属性を書かない**（CSP に黙って消され、見た目だけ静かに壊れる。
  公開時から会派バーが全部同じ長さだった）。色や幅は SVG の `fill` / `width` で出す

## 運営が定期的にやること

**セッションの最初に `OPERATIONS.local.md` を読み、推奨間隔を過ぎている作業があれば
運営者に知らせること。** 黙って実行しない — どれも判断か手入力が要る。

**運営者は `python scripts/admin.py`（運営コンソール）で同じ表を見られる。**
期限切れの作業・争点語・政党の手入力・除外語が1か所にまとまっていて、
材料（件数・候補・会派ごとの政党候補）もそこに出る。**手元だけの道具**で、
JSONを書き換えるだけ（集計は保存後に出るコマンドを手で回す）。

`OPERATIONS.local.md` は `.gitignore`（この端末だけの実績）。**無ければ下の表から
全部「未実施」で作ること。** 仕様はこの表、実績はあちら。

| 作業 | 推奨間隔 | きっかけ・材料 |
|---|---|---|
| 日次更新の成否を見る | 1か月 | `gh run list --workflow=daily.yml`。落ちても数日は放置できる |
| **議員ID台帳の書き戻しを確認** | 議員が増えた日に1回 | ★まだ一度も通っていない。**落ちたまま放置すると台帳を失い公開後のURLが全部変わる** |
| 頻出語のノイズを denylist に入れる | 3か月 | **`admin.py` の「除外語」タブ**（頻出語500件から選ぶ）。→ `data/topic_denylist.json` |
| 政党の手入力 | 3か月 | **`admin.py` の「政党」タブ**（会派ごとの候補が出る）。→ `data/party_overrides.json`（[docs/CORRECTIONS.md](docs/CORRECTIONS.md)） |
| 争点語のレビュー | 3か月 | **`admin.py` の「争点語」タブ**（候補と**その場で数えた件数**が出る）。→ `data/topics.json`。**足すだけなら `build_topics.py` だけ** |
| 公式サイトURLの見直し | 6か月 | `reports/politicians.md`。**Wikidata 掲載・未確認**の外部リンクで、平文 http が437件。失効ドメインの取り直しはコードでは防げない |
| 連絡先の疎通確認 | 6か月 | 1通送る。**届かない窓口は「窓口が無い」のと同じ** |
| **アクセス解析が集めるものの再確認** | 6か月 | 解析を入れてから。**2026-08-21 に検索語を `#` へ移したので、「クエリ文字列を記録しない」への依存は薄れた**（それでも JS 無効時は `?q=` で飛ぶ）。見るのは RUM の自動挿入が復活していないかと、集める項目が増えていないか |
| R2 とドメインの費用を見る | 1か月 | 月1,000円以内が絶対の制約 |
| 配信DBの最大サイズを見る | 1か月 | 日次が `manifest.json` を見て 480MB で落とす。落ちたら分割を細かくする |

**`reports/` は日次のランナー上でだけ作られる**（`.gitignore`）。中身は Actions の
Artifacts（`reports` / 90日保持）から読む。要約に出ているのは件数だけ。

**2文字語の語彙更新はもう要らない**（索引が本文から作られるようになったため）。
全期間の作り直しが要るのは**争点語を消す・書き直す・`variants` を足すとき**だけ。
手順は [docs/PIPELINE.md](docs/PIPELINE.md)「争点語を変えるとき」。

## コマンド

Python 3.12+ のみ。**外部依存パッケージなし**（GitHub Actions で `pip install` を不要にするため）。

```bash
# 運営コンソール（人手が要る作業を1か所に。127.0.0.1 だけ・配るものではない）
python scripts/admin.py            # 期限切れの作業 / 争点語 / 政党の手入力 / 除外語

# 取得（中断しても同じコマンドで再開できる）
python scripts/fetch_range.py --from 2021-01-01 --until 2026-07-31
python scripts/fetch_range.py --from 2021-01-01 --until 2026-07-31 --status

# DB構築
python scripts/build_db.py --fresh                                # 単一DB
python scripts/build_db.py --split --page-size 8192               # 配信用の期間DB（半期）
python scripts/build_db.py --id 2026H2 --page-size 8192           # 日次は触った期間だけ
python scripts/build_db.py --manifest-only                        # 目録と indexed だけ作り直す

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

# 検証（配る前に必ず通す。日次更新もこれを関門にしている）
python scripts/verify_dist.py              # data/dist の全期間（実測21秒）
python scripts/verify_dist.py --id 2026H2  # 触った期間だけ

# 検算（配ったあと。**公開URLが返すもの**を見る。関門ではなく知らせるためのもの）
python scripts/verify_published.py --base https://db.kokkai-timeline.com
```

**順番は `fetch_range` → `build_db`（単一DB）→ `build_politicians` / `build_topics`
/ `build_frequent` → `build_db --split` → `verify_dist`。**
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
npm run check    # 型検査 + テスト199件
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

- `topic` は「何を争点と呼ぶか」という**編集方針**。増やすほど意味が薄まるので膨らませない。
  `topic_hit` は**速くするための索引で、正しさの前提ではない**（持っていない語は
  サイトが検索経路で出す。実測で82語中80語は件数が完全に一致する）
- `word` は2文字語を引くための**索引**。一覧としては表示しない。
  **語彙リストは持たない**ので、期間ごとに中身が違ってよい（無い語は素直に0件）
- `frequent` は「いつ・どれだけ議論されたか」を見せる**一覧**。運営は語を選ばない

**頻出語の選定は自立した run、集計は部分文字列**（数え方が違うのは意図的。
揃えないと一覧の件数と検索結果が食い違う）。詳細は PITFALLS.md。

**`speech.rowid` は日付の昇順。** UIの「新しい順」はこれに依存する。

**`affiliation.party` は NULL がありうる**（会派名から政党を決められないもの）。
`party = '無所属'` とは意味が違うので、政党別の集計で混ぜない。
