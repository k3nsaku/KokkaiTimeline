# CLAUDE.md

国会会議録から政治家の発言を収集・構造化し、横断検索と時系列表示を提供する
**完全静的サイト**。有権者が政治家について判断する材料を出すことが目的。

現在 **Phase 1 着手中**。§3.1（プロトタイプ検証）〜§3.3（サイト実装）まで動いている。
サイトの実装は `site/`（Astro）。次は §3.4 の日次パイプライン。
進捗と計画は `docs/ROADMAP.md`。

---

## 絶対に守る制約

この3つはプロジェクトの前提であり、他のすべての判断に優先する。

1. **月1,000円以内**（年12,000円）。実際にかかるのはドメイン代のみ（月250円）。
   全発言へのAI処理は予算に収まらない（初回55,000円・年11,000円）ので採らない。
2. **常時稼働プロセスを持たない。** サーバーもDBサーバーもAPIサーバーも立てない。
3. **運用工数は月1時間以下。** 壊れて困るものを作らない。

やらないことは `PROJECT_BRIEF.md` §9 に一覧がある。特に重要なのは
**独自の点数付け・ランキング・議員の評価をしない**（公職選挙法・名誉毀損リスク）、
**報道記事を収集しない**、**LLMに立場変化を判定させない**の3つ。

---

## コマンド

Python 3.12+ のみ。**外部依存パッケージなし**（GitHub Actions で `pip install` を不要にするため）。

```bash
# 取得の進捗確認
python scripts/fetch_range.py --from 2021-01-01 --until 2026-07-31 --status

# 会議録の取得（中断しても同じコマンドで再開できる）
python scripts/fetch_range.py --from 2021-01-01 --until 2026-07-31

# DB構築（data/kokkai.db を作り直す）
python scripts/build_db.py --fresh

# 配信用の年ごとDB（data/dist/kokkai-YYYY.db）。日次更新は当年だけ作り直す
python scripts/build_db.py --split-by-year --page-size 8192
python scripts/build_db.py --year 2026 --page-size 8192

# プロトタイプの計測サーバ（詳細は prototype/README.md）
cd prototype && npm install && npm run vendor && npm run serve

# Wikidata から議員リストを取得
python scripts/fetch_wikidata.py

# 名寄せの検証レポートだけ出す（DBは書き換えない）
python scripts/match_politicians.py

# 議員マスタを確定させる（data/politicians.json と ID台帳を更新）
# → 政党の手入力が要る分は reports/party_todo.md に出る
python scripts/build_politicians.py

# 所属政党の訂正（手順は docs/CORRECTIONS.md）。名前でもIDでも引ける
python scripts/build_politicians.py --fix 浜田聡

# 争点語の候補出し（reports/topic_candidates.md）→ data/topics.json は手で選ぶ
python scripts/build_topics.py --propose

# 2文字語の語彙（data/words.json）。FTSが3文字未満を引けないことへの対応
python scripts/build_words.py

# 頻度推移（data/dist/topics.json・258KB）と週次トレンド（trending.json・10KB）
python scripts/build_topics.py
```

**順番は `fetch_range` → `build_db`（単一DB）→ `build_politicians` / `build_topics`
/ `build_words` → `build_db --split-by-year`。**
`build_politicians.py` `build_topics.py` `build_words.py` は `data/kokkai.db` を
材料にするので、単一DBが先に要る。

バックフィルの詳細は `docs/BACKFILL.md`。

### サイト（`site/`）

Node 24 / Astro。詳細は **`site/README.md`**。

```bash
cd site && npm install
npm run dev      # http://localhost:4321。data/dist を /db で配る（DBはコピーしない）
npm run build    # dist/ に 1,196ページ・18MB
npm run test     # 検索SQLとページ送りの回帰テスト（36件・0.1秒）
npm run check    # 型検査 + テスト
```

**検索まわりを触ったら `npm run check` を通すこと。** `src/lib/query.ts`
（SQLの組み立てと年またぎのページ送り）は sql.js-httpvfs に依存しない純粋な
関数にしてあり、`site/test/` から直接呼んで検証している。**ここに
sql.js-httpvfs を持ち込むとテストが動かなくなる。**

---

## アーキテクチャ

```
[GitHub Actions 日次]
    ↓ NDL 国会会議録API（差分のみ）
    ↓ SQLite を生成（FTS5 + trigram）
    ↓ Cloudflare R2 にアップロード
[Cloudflare Pages] ← 完全静的サイト
    ↓
[ブラウザ] sql.js-httpvfs が HTTP Range で必要なページだけ取得
```

- **DBは年ごとに分割する**（1ファイル約360MB / `page_size=8192`）。日次更新で変わるのは当年分だけ。
- **DBは R2 に置く。** Cloudflare Pages は1ファイル25MiB上限、GitHub は100MB上限で置けない。
- **Vercel は使わない**（帯域課金。バズると個人の財布が痛む）。
- **年またぎ検索は年ごとに別ワーカを立てて並列に引き、JS側でマージする。**
  1ワーカ内のリクエストは同期XHRで直列になるため、ATTACH+UNION だと年数に比例して遅くなる。

## データモデル

```
meeting      : issue_id, 会期, 院, 会議名, 号数, 日付, meeting_url, pdf_url
speech       : speech_id, issue_id, 発言順, 日付, 発言者, よみ, 会派, 肩書, 役割,
               本文, speech_url, is_speech, speaker_kind, politician_id
speech_fts   : FTS5 + trigram。議員の発言のみを索引化（本文は speech 側）
politician   : 名寄せ後の議員マスタ（1,111人）。id は URL に出る
affiliation  : 会派の時系列。party は特定できたときだけ入る（NULL あり）
topic        : 争点語（79件）。リストは data/topics.json
topic_hit    : 争点語 → 発言。頻度推移の入口 兼 FTS回避の索引
word         : 2文字語の語彙（16,058件）。リストは data/words.json
word_hit     : 2文字語 → 発言。FTS が3文字未満を索引化できないことへの対応
```

**`topic` と `word` は役割が違う。混ぜないこと。**

| | topic | word |
|---|---|---|
| 何 | 争点語79件。**運営の編集方針** | 2文字語16,058件。**機械抽出** |
| 用途 | 頻度推移・会派比較・検索の入口 | 2文字語を引けるようにするだけ |
| 一覧表示 | する（`/topics`） | **しない**（引けるかを答えるだけ） |
| 別表記 | `variants` で合算する | 無い |
| 人名 | 落とす（一覧に出るので事故になる） | 落とさない（「石破」を引けてよい） |

検索できる語を増やしたいときに **`topics.json` を膨らませない。** あれは
「何を争点として扱うか」の判断であって、検索の索引ではない。

配信物は年DBのほかに2つある。

**`data/dist/topics.json`（258KB）** — 月×会派の出現件数と**分母（その月の発言数）**。
頻度推移ページはこれだけで描ける。**分母で割らずにグラフにしないこと。**
国会は通年で開いていないので、割らないと「開催日数が多い月」が争点に見える。

**`data/dist/trending.json`（10KB）** — 直近の国会で急に増えた語。検索の入口に使う。
**カレンダー週で区切らない**（実測で直近8週のうち2週が発言不足で集計にならなかった）。
発言のあった日を5日ずつまとめている。中身は「その週に審議された法案の専門用語」が
主で、**「今週の争点」ではない**。表示の言葉を誇張しないこと。

**`data/dist/manifest.json`** — 年DBの目録。`build_db.py --split-by-year` が
出力先を走査して作り直す（引数の年ではなくディレクトリを見るので、`--year 2026` だけ
作り直しても他の年が消えない。手元に無い年は前回の記載を引き継ぐ ＝ CI で当年しか
置かなくても目録が痩せない）。**サイトはこれで年の一覧を知る**ので、
R2 へのアップロードでは DB と一緒に上げること。

目録には年ごとに2つの指紋が入る。

- **`version`（中身の指紋）** — DBのURLに `?v=` で付く。
  **これが無いと、DBを差し替えた瞬間に開いていたページが壊れる**
  （`sql.js-httpvfs` は読んだページをオフセットで覚えているので、
  古いページと新しいページが混ざる）。CDN のパージも要らなくなる。
- **`vocabulary`（2文字語の語彙の指紋）** — 年をまたいで一致していなければならない。
  食い違うと `build_db.py` が警告し、日次ワークフローは失敗する。

**議員IDは `data/politician_ids.json` の台帳で維持する。** URLに出るので作り直すと
リンクが全部壊れる。**この台帳と `data/party_overrides.json` はコミットすること。**
採番は `scripts/build_politicians.py`。`build_db.py` は `data/politicians.json` を読むだけ。

**`affiliation.party` は NULL がある（発言の3.6%）。** 統一会派の分は
`data/party_overrides.json` に人力で入れて解消済み（141件）。残る NULL は
`無所属` `各派に属しない議員` `有志の会` `沖縄の風` `碧水会` `改革の会` で、
**会派名から政党を決められないもの**（設計どおり。議長は自党の会派を抜けるが党籍は残る）。

**`party = '無所属'` と `party IS NULL` は意味が違う。**
前者は「政党に所属していないと分かっている」、後者は「特定できていない」。
政党別の集計で混ぜないこと。作業リストは `reports/party_todo.md`。

**1会派 = 1政党とは限らない。** 統一会派は名前が変わらないまま構成政党が入れ替わる。
`party_overrides.json` に `periods` を書くと所属レコードが期間で分割される
（発言数は実データから数え直す）。**区間に隙間があると発言が落ちるので警告が出る。**
訂正の手順は `docs/CORRECTIONS.md`。`--fix 議員名` で編集用の雛形が出る。

`speaker_kind` は `議員 / 参考人 / 公述人 / 証人 / 政府参考人等 / 非発言`。
**全発言をDBに持ち、全文検索の索引は「議員」だけに張る。**
参考人等は検索対象外だが、前後の文脈表示のためDBに残してある。

**`speech.rowid` は日付の昇順**（`build_db.py` の `load()` が並べ替えている）。
UIの「新しい順」はこれに依存する。詳しくは下の落とし穴を見ること。

---

## 落とし穴

実際に踏んだもの。同じ穴を掘り直さないこと。

### NDL API

- **`REQUEST_INTERVAL_SEC`（3秒）を短くしない。** NDL は「運用に支障があると判断した場合は
  アクセスを遮断することがある」と明記している。遮断されたら詰む。
- `Access-Control-Allow-Origin: *` を返すので**ブラウザから直接叩ける**。
- 検索条件がひとつも無いとエラー（19007）になる。

### 全文検索（FTS5 + trigram）

- **2文字以下の語は原理的に引けない。**「増税」「憲法」「年金」「原発」が全滅する。
  → `word` / `word_hit`（2文字語の索引）で対応済み。下の「2文字語」を見ること。
- **`detail=column` / `detail=none` は使えない。** trigram はフレーズクエリを使うため、
  位置情報を落とすと4文字以上が検索不能になる。`detail=full` 固定。
- **`INSERT INTO speech_fts(speech_fts) VALUES('rebuild')` を使わない。**
  全行を索引化してしまう。議員だけを入れるには `SELECT ... WHERE speaker_kind='議員'` で入れる。
- **`SELECT COUNT(*) FROM speech_fts` は索引件数にならない。**
  external content 構成では本体テーブルに委譲される。`speech` 側で数えること。

### ブラウザからDBを引く（sql.js-httpvfs）

実測の根拠は `docs/PHASE1_PROTOTYPE.md`。数字はすべて 350MB のDBでの実測。

- **`ORDER BY date DESC` と書かない。`ORDER BY rowid DESC` にする。**
  date で並べると一時B-TREEができて**ヒット全件を読みに行く**。
  検索で **204MB 転送**、議員ページで **7,800リクエスト**になる。
  rowid は日付昇順に投入してあるので、結果は date 順と完全に一致する。
- **絞り込み用のインデックスに `date` を足さない。** SQLite のインデックスは末尾に
  rowid が付くので、`speech(politician_id)` だけで `(politician_id, 日付)` 順になる。
  `(politician_id, date)` にすると rowid 順が保証されなくなり、上と同じ罠に落ちる
  （議員ページが 27リクエスト → **509リクエスト**）。
- **配信するDBを WAL にしない。** `-wal` が本体の外に要るのでブラウザから開けない。
  `build_db.py` の `finalize()` が `journal_mode=DELETE` + `VACUUM` で畳んでいる。
- **`db.query(sql, params)` の第2引数は配列で渡す。** 型定義は `(sql, ...params)` だが
  実体は sql.js の `exec(sql, params)`。展開して渡すと**黙って束縛されない**。
- **wasm の URL は絶対パスで渡す。** ワーカ内で解決されるため相対パスだと壊れる。
- **やり直しでは URL を変える（`&retry=`）。同じURLを引き直しても救えない。**
  年DBは `immutable` で1年握らせるので、**壊れたものが一度ブラウザキャッシュに
  入ると Ctrl+Shift+R でも読み直されない**。過去年は差し替わらないので `?v=` も
  変わらず、利用者は手でキャッシュを消すまで直せない（実際に踏んだ。
  Chrome だけ全ページで `file is not a database`、シークレットと他ブラウザでは正常）。
  再現は `POISON=1 node scripts/dev-data-server.js`（`retry=` の無いURLにゼロを返す）。
- **ページ送りに OFFSET を使わない。** 5ページ目（OFFSET 80）で134リクエスト・4.1秒。
  `rowid < ?` の keyset で進めれば何ページ目でも1ページ目と同じコストで済む。
- **年またぎのマージソートは要らない。だが連結もしてはいけない。**
  年DBは日付で綺麗に分かれているので、新しい年から順に並べれば全体が日付の降順になる。
  **ただし年ごとに LIMIT を掛けている**ので、そのまま連結すると 20件のつもりが
  6年ぶんで120件返り、しかも「2026年の21件目」を飛ばして2025年へ進む。
  次ページで飛ばした分が後ろに付くので、画面の「新しい順」がそこで崩れる。
  → **全体で LIMIT 件に切り、1件も出さなかった年はカーソルを進めない**
  （`db.ts` の `mergePages()`。次ページで引き直すが、読んだページはワーカに残る）。
- **件数の SQL に絞り込み条件を付け忘れない。** `countQuery()` は結果取得とは
  別のSQLなので、片方だけ絞ると一覧と画面上部の件数が食い違う
  （実際に会議名の絞り込みが件数に効いておらず、2,594件と557件が入れ替わっていた）。
  FTS・争点語・2文字語の**3経路すべて**にある。
- **まだ無い `<option>` の値を `select.value` に入れても選択されない。**
  会議名の一覧はDBから非同期で来るので、URLから条件を復元するときは
  その会議名の `<option>` を先に自分で足す。待つと、共有されたURLの
  最初の検索が絞り込み無しで走る。
- **検索の開始ごとに世代番号を進めて、古い結果を捨てる。** 検索の速さは語で
  大きく変わる（長い語ほど遅い）ので、重い検索の裏で条件を変えると、
  前の結果が新しい画面に後から混ざる。
- **`snippet()` のマーカーに `<mark>` を直接入れない。** 本文は会議録そのままで
  `<` や `&` が入りうる。`char(1)/char(2)` で受けて、HTMLエスケープしてから置き換える。
- **`site/src/lib/format.ts` から `db.ts` を import しない。** format.ts は
  `.astro` のフロントマター（ビルド時のNode側）からも読む。db.ts は
  sql.js-httpvfs に依存していてブラウザでしか動かないので、
  import が1本でも通ると SSR が落ちる。
- **遅いのはヒット件数ではなく検索語の長さ。** trigram は N文字を N-2 トークンに展開する。
  ヒット14件の「デジタル田園都市国家構想」が、ヒット2,864件の「安全保障」の2.4倍遅い。
- **争点語リストにある語は FTS を使わない。** `topic_hit` を引くほうが 3.3倍速い
  （0.7秒 vs 2.3秒）。

### R2 での配信

- **R2 にカスタムドメインを付けただけではキャッシュされない。** Cloudflare は
  **拡張子で判定**していて `.db` も `.json` も既定の対象外
  （`cf-cache-status: DYNAMIC`）。**Cache Rule が要る**（`docs/PIPELINE.md` 手順3.5）。
  実測で **RTT 77ms → 8ms**。この構成は「リクエスト数 × RTT」で決まるので約10倍効く。
- **`MISS` は1回でオブジェクト全体をエッジに載せる。** 以後は未読の位置でも 8ms。
- **無料プランのキャッシュ上限は1ファイル 512MB。** 最大は2022年の419MB。
  **超えた年DBは黙ってキャッシュされなくなる。**
- **Edge TTL を固定値にしない。** アップロード時の `cache-control` に任せる
  （DBは `immutable` で1年、目録は300秒）。固定すると目録の更新が反映されない。

### 2文字語

FTS5 の trigram は3文字未満のトークンを作れない。`word` / `word_hit` で対応している。

- **語彙の選び方と索引の作り方で数え方が違う。意図的。**
  `build_words.py` は「漢字/カタカナが**ちょうど2文字だけ**連続する」箇所を数えて
  語彙を決める（＝語として自立しているもの）。`build_db.py` の索引は
  **部分文字列**で拾う（「憲法改正」の中の「憲法」を引けないと意味がない）。
- **語彙は全期間（`data/kokkai.db`）から作る。** 年ごとに作ると、
  ある年だけ語彙に無くて引けない語ができる。
- **`RUN_PATTERN` を片方だけ変えない。** `build_words.py` と `build_db.py` の
  `WORD_RUN_PATTERN` は同じものにすること。ずれると語彙にあるのに索引に入らない語が出る。
- 複数語のときは**いちばん珍しい2文字語を起点**にして、残りは `instr()` で絞る。
  走査する行数が起点の語の件数で頭打ちになる。
- コストは実測で **DBサイズ +9.3%**（1年で約330万行 / 全体 1.91GB→2.09GB）、
  索引の構築は1年あたり21秒。引くのは0.2ms（ローカル）。
- **ひらがなだけの2文字語は引けない。** 語彙が漢字とカタカナの連続しか見ていないため。

### 名寄せ

- **突合の単位は `(発言者, 読み, 会派)`。** 読みだけだと同姓同名が1行に潰れて分離できなくなる。
- **`speakerYomi` が第一キー。** 会議録は「あべ俊子」「早稲田ゆき」のような通称を使うので、
  漢字表記だけで突合すると落ちる。
- **Wikidata の `kana`(P1814) は空白入り**（7,844人が `'みき ぶきち'` 形式）。
  NFKC → カタカナ→ひらがな → 空白除去、の正規化をしないと**1件もマッチしない**。
- **`data/party_map.json` を推測で埋めない。** 誤った対応表は同姓同名を**誤って分離する**ため、
  無記入より有害。所属議員の Wikidata の政党(P102)を集計して決め、
  判断がつかない会派は空リストにする。
- **任期(P580)は付与率13.7%で当てにならない。** 同姓同名の分離は会派が最も強い信号。
- **院は分離に使えない。** 大臣・副大臣は他院の委員会にも出るし、党首討論は `両院` になる。

### Wikidata SPARQL

- **ラベルの全走査（`CONTAINS` で役職を探す）はエンドポイントが502/504を返す。**
  役職QIDは `wbsearchentities`（検索API）で特定する。
  衆議院議員 `Q17506823` / 参議院議員 `Q14552828`。
- 人物属性と任期は**別クエリに分ける**。1本にまとめると OPTIONAL の直積で行数が爆発する。
- User-Agent を明示しないと403。

### 争点語

- **`data/topics.json` は運営の編集方針そのもの。** 何を争点として扱うかは判断が要る。
  初版79件は `--propose` の機械抽出から選んだだけで、**レビューを経ていない**。
- 語を**互いに部分文字列にしない**。`夫婦別姓` と `選択的夫婦別姓` を並べると二重に数える。
  同じ争点の別表記は `variants` に入れる（`build_topics.py` が重なりを警告する）。
- 候補は**頻度上位ではなく増減で見る。** 頻度上位は「日本」「議論」「重要」で埋まる。
  年ごとの発言数が違う（2026年は7月まで）ので、割合に直してから比べる。
- **両端（最初の年と最後の年）を比べない。ピーク年と中央値を比べる。**
  両端比だと途中で山を作って収束した争点が消える
  （裏金は両端比2.4だがピーク比12.4、マイナンバーは両端比0.2でピーク比4.6）。
- **機械抽出に人名が混ざる。** 「〜議員」「〜大臣」で終わる語は姓の照合を待たずに落とす
  （姓が1文字だと照合をすり抜けて「簗議員」が漏れた）。
  それでも取り切れない分は `data/topic_denylist.json` に1行足して消す。

### Git

- `data/` は `.gitignore` 済み。ただし**手書きの資産はコミットする**
  （生成物ではない。失うと作り直しになる）:
  `party_map.json` / `party_overrides.json` / `politician_ids.json` /
  `topics.json` / `topic_denylist.json`。
  **`words.json` は生成物なのでコミットしない**（`build_words.py` で作り直せる）。
  特に `politician_ids.json` を失うと**公開後はURLが全部変わる**。

---

## ドキュメントの地図

| ファイル | 役割 | いつ読むか |
|---|---|---|
| `CLAUDE.md` | これ。作業のための要点 | 常に |
| `docs/ROADMAP.md` | 進捗・今後の計画・未解決の課題 | 次に何をするか決めるとき |
| `whatiwant.md` | 実現したいことの原典（発注側の要望） | 要件を確認するとき |
| `PROJECT_BRIEF.md` | 企画書。意思決定とその理由、法的整理 | 「なぜそうなっているか」を知りたいとき |
| `docs/PHASE0_FINDINGS.md` | Phase 0 の検証結果。実測値と判断の根拠 | 設計判断を疑うとき |
| `docs/PHASE1_PROTOTYPE.md` | ブラウザからDBを引く性能の実測。**守るべき制約3つ** | 検索まわりを実装するとき |
| `docs/PIPELINE.md` | **日次更新の運用**。Cloudflare側の設定と踏んではいけない手順 | 配信まわりを触るとき |
| `docs/CORRECTIONS.md` | **所属政党の訂正手順**。期間分割・ID台帳の注意 | 誤りを指摘されたとき |
| `docs/BACKFILL.md` | バックフィルの操作手順 | データを取り直すとき |
| `site/README.md` | **サイトの構成・DBの置き場所・触る前の注意** | サイトを実装するとき |
| `prototype/README.md` | 計測サーバの使い方 | 性能を測り直すとき |

**設計判断を変えるときは `docs/PHASE0_FINDINGS.md` と `docs/PHASE1_PROTOTYPE.md` の
実測値を確認すること。** そこにある数字はすべて実測であり、推測ではない。
