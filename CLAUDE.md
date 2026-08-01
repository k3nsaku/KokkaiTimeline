# CLAUDE.md

国会会議録から政治家の発言を収集・構造化し、横断検索と時系列表示を提供する
**完全静的サイト**。有権者が政治家について判断する材料を出すことが目的。

現在 **Phase 0（技術検証）完了**。Phase 1（サイト実装）は未着手。
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

# Wikidata から議員リストを取得
python scripts/fetch_wikidata.py

# 名寄せ（reports/name_matching.md にレポートが出る）
python scripts/match_politicians.py
```

バックフィルの詳細は `docs/BACKFILL.md`。

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

- **DBは年ごとに分割する**（1ファイル335〜378MB）。日次更新で変わるのは当年分だけ。
- **DBは R2 に置く。** Cloudflare Pages は1ファイル25MiB上限、GitHub は100MB上限で置けない。
- **Vercel は使わない**（帯域課金。バズると個人の財布が痛む）。

## データモデル

```
meeting      : issue_id, 会期, 院, 会議名, 号数, 日付, meeting_url, pdf_url
speech       : speech_id, issue_id, 発言順, 日付, 発言者, よみ, 会派, 肩書, 役割,
               本文, speech_url, is_speech, speaker_kind, politician_id
speech_fts   : FTS5 + trigram。議員の発言のみを索引化（本文は speech 側）
politician   : 名寄せ後の議員マスタ（Phase 1 で投入）
affiliation  : 所属政党の時系列（Phase 1 で投入）
```

`speaker_kind` は `議員 / 参考人 / 公述人 / 証人 / 政府参考人等 / 非発言`。
**全発言をDBに持ち、全文検索の索引は「議員」だけに張る。**
参考人等は検索対象外だが、前後の文脈表示のためDBに残してある。

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
  → 争点語の集計テーブルで別途対応する（Phase 1 の必須タスク）。
- **`detail=column` / `detail=none` は使えない。** trigram はフレーズクエリを使うため、
  位置情報を落とすと4文字以上が検索不能になる。`detail=full` 固定。
- **`INSERT INTO speech_fts(speech_fts) VALUES('rebuild')` を使わない。**
  全行を索引化してしまう。議員だけを入れるには `SELECT ... WHERE speaker_kind='議員'` で入れる。
- **`SELECT COUNT(*) FROM speech_fts` は索引件数にならない。**
  external content 構成では本体テーブルに委譲される。`speech` 側で数えること。

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

### Git

- `data/` は `.gitignore` 済み。ただし **`data/party_map.json` は手書きの資産なのでコミットする**
  （生成物ではない。失うと作り直しになる）。

---

## ドキュメントの地図

| ファイル | 役割 | いつ読むか |
|---|---|---|
| `CLAUDE.md` | これ。作業のための要点 | 常に |
| `docs/ROADMAP.md` | 進捗・今後の計画・未解決の課題 | 次に何をするか決めるとき |
| `whatiwant.md` | 実現したいことの原典（発注側の要望） | 要件を確認するとき |
| `PROJECT_BRIEF.md` | 企画書。意思決定とその理由、法的整理 | 「なぜそうなっているか」を知りたいとき |
| `docs/PHASE0_FINDINGS.md` | Phase 0 の検証結果。実測値と判断の根拠 | 設計判断を疑うとき |
| `docs/BACKFILL.md` | バックフィルの操作手順 | データを取り直すとき |

**設計判断を変えるときは `docs/PHASE0_FINDINGS.md` の実測値を確認すること。**
そこにある数字はすべて実測であり、推測ではない。
