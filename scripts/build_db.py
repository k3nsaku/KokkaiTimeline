"""Phase 0 ステップ3: NDJSON を SQLite に投入する。

## 確定した方針（2026-08-01）

**案A: 本文をDBに持つ。** contentless FTS5（本文を持たず索引だけ）も検証したが、
`whatiwant.md` L27 の必須要件「検索キーに該当する部分をハイライト」を満たすには
`snippet()` / `highlight()` が必要で、contentless では使えないため不採用。

**案Y: 全発言を保持し、全文検索の索引は議員の発言だけに張る。**
アプリの目的が「有権者が政治家について判断する材料の提供」なので、
参考人・公述人・政府参考人は検索対象にしない。ただしDBには保持しておき、
前後の文脈を表示するときはローカルのJOINで引く（APIを叩かない）。

  → 索引を絞ることで 5年分で 2.00GB → 1.76GB
  → 後から「参考人も検索対象に」となっても `INSERT INTO speech_fts` を足すだけで済む

## テーブル構成

    meeting      : 会議のメタ情報
    speech       : APIレコードをほぼそのまま（全発言を保持）
    speech_fts   : FTS5 + trigram。**議員の発言のみ**を索引化
    politician   : 名寄せ後の議員マスタ（Phase 1 で投入）
    affiliation  : 所属政党の時系列（Phase 1 で投入）

## 期間ごとの分割（Phase 1）

`--split` を付けると `data/dist/kokkai-<期間ID>.db` を期間ごとに作る。
日次更新で変わるのは**いま開いている期間だけ**なので、R2 へのアップロードが
1ファイルで済み、閉じた期間は CDN キャッシュが効き続ける（`docs/DECISIONS.md`）。

**既定は半期**（`2023H1` = 1〜6月 / `2023H2` = 7〜12月）。年ではなく半期なのは、
1ファイルが 512MB を超えると**黙って CDN キャッシュから外れる**ため
（RTT 8ms → 77ms。待ち時間はリクエスト数×RTT でほぼ決まる）。実測で満年は
368〜419MB あり、2文字語の索引を全bigramにすると 450MB まで来て余裕が無い。
半期なら最大 356MB（2024H1）で、上限に対して3割の余裕が残る。

**期間の境界は年に閉じている。** だから利用者に見せる絞り込みは「年」のままにできる
（1年＝2ファイル、日付の取りこぼしなし）。会期で割るとこれが成立しない
（年をまたぐ会期が実測で5本ある）。

配信するDBは **WAL にしない**。WAL は本体ファイルの外に -wal を持つため、
HTTP Range で1ファイルだけ読む sql.js-httpvfs から開けない。
ビルド中だけ WAL を使い、最後に `journal_mode=DELETE` + `VACUUM` で単一ファイルに畳む。

使い方:
    python scripts/build_db.py --fresh                  # 従来どおり単一DB
    python scripts/build_db.py --split                  # 期間ごとに data/dist/ へ
    python scripts/build_db.py --split --year 2025      # その年の全期間（2本）
    python scripts/build_db.py --id 2026H2              # 1本だけ（日次更新はこれ）
    python scripts/build_db.py --split --period year    # 年で割る（元の挙動）
    python scripts/build_db.py --no-fts
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "speeches"
DEFAULT_DB = ROOT / "data" / "kokkai.db"
DIST_DIR = ROOT / "data" / "dist"
POLITICIANS_PATH = ROOT / "data" / "politicians.json"
TOPICS_PATH = ROOT / "data" / "topics.json"

# 配信DBの分割単位。**サイト側の `periodOf()`（site/src/lib/query.ts）と同じ写像**を
# 持つこと。片方だけ変えると、存在しないファイルを引きに行って検索が丸ごと止まる。
DEFAULT_PERIOD = "half"

# sql.js-httpvfs は requestChunkSize 単位で HTTP Range を投げる。
# page_size をそれに合わせると1ページ＝1リクエストになり、無駄な読みが出ない。
# 8192 は実測で決めた値（`docs/DECISIONS.md`）。
# 4096 比でリクエスト18%減・DBサイズ4%減。16384 以上は転送量が2〜4倍になる。
# **変えるときはブラウザ側の requestChunkSize も同じ値にすること。**
DEFAULT_PAGE_SIZE = 8192

logger = logging.getLogger("build_db")

# 「会議録情報」は各会議の冒頭に1件だけ入るヘッダ（出席委員名簿など）で、人の発言ではない。
NON_SPEECH_SPEAKERS = {"会議録情報", "会議録情報等", "目次"}

# 全文検索の対象にする発言者の種別
INDEXED_KINDS = ("議員",)

# 2文字語の索引を作るときに走査する範囲。漢字・カタカナ・全角ラテン/数字の連続。
# scripts/build_frequent.py の RUN_PATTERN と**同じもの**にすること
# （頻出語の一覧に出る語と、検索で引ける語がずれる）。
# 全角ラテンを入れているのは `ＡＩ` `ＤＸ` `ＧＸ` `Ｇ７` のため（`docs/DECISIONS.md`）
WORD_RUN_PATTERN = re.compile(r"[一-鿿々]{2,}|[ァ-ヴー]{2,}|[Ａ-Ｚａ-ｚ０-９]{2,}")

# 全角ラテンの小文字 → 大文字。**索引はこの形で持つ。**
#
# ここは畳む。`word.term` は `w.term = ?` の BINARY 比較で引くので SQLite は畳んで
# くれず（NOCASE は ASCII 限定）、畳まないと `ai` と打たれた語が `ａｉ` のまま
# 索引の `ＡＩ` に当たらない。**FTS 経路は逆に畳んではいけない**（`ＳＤＧｓ` が壊れる）。
# 詳細は `docs/DECISIONS.md`「大文字に寄せるかは経路で逆になる」。
# 2文字の全角ラテンで小文字を含むのは8語・69発言しかなく、失うものは無い
WORD_LATIN_FOLD = {c: c - 0x20 for c in range(0xFF41, 0xFF5B)}


def fold_word_run(run: str) -> str:
    """全角ラテンの並びだけ大文字に畳む。

    1つの `run` は同じ文字クラスだけでできている（パターンが選択で分かれている）ので、
    先頭1文字で判別できる。漢字・カタカナは U+9FFF 以下、全角ラテン/数字は U+FF10 以上。
    """
    return run.translate(WORD_LATIN_FOLD) if run[0] > "鿿" else run

SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting (
    issue_id        TEXT PRIMARY KEY,
    session         INTEGER NOT NULL,
    house           TEXT NOT NULL,
    name            TEXT NOT NULL,
    issue           TEXT,
    date            TEXT NOT NULL,
    meeting_url     TEXT,
    pdf_url         TEXT
);

-- ★ rowid は日付の昇順になるよう投入する（load() が並べ替えている）。
--   これに依存して、UIの「新しい順」はすべて `ORDER BY rowid DESC` で書く。
--   `ORDER BY date DESC` にすると一時B-TREEができてヒット全件を読みに行き、
--   HTTP Range 越しでは 200MB 単位の転送になる（`docs/DECISIONS.md` の実測）。
--   rowid 順なら FTS5 も通常のインデックスも降順スキャンで早期終了できる。
CREATE TABLE IF NOT EXISTS speech (
    speech_id        TEXT PRIMARY KEY,
    issue_id         TEXT NOT NULL REFERENCES meeting(issue_id),
    speech_order     INTEGER NOT NULL,
    date             TEXT NOT NULL,
    speaker          TEXT NOT NULL,
    speaker_yomi     TEXT,
    speaker_group    TEXT,
    speaker_position TEXT,
    speaker_role     TEXT,
    body             TEXT NOT NULL,
    start_page       INTEGER,
    speech_url       TEXT NOT NULL,
    -- 人の発言か。会議録情報・目次を除く。検索対象かどうかの判定に使う
    is_speech        INTEGER NOT NULL,
    -- 議員 / 参考人 / 公述人 / 証人 / 政府参考人等 / 非発言
    speaker_kind     TEXT NOT NULL,
    politician_id    INTEGER REFERENCES politician(id)
);

-- ★ 絞り込みキーに date を足さないこと。
--   SQLite のインデックスは末尾に rowid が付くので、`(politician_id)` だけなら
--   並びは (politician_id, rowid) ＝ (politician_id, 日付) になり、
--   `WHERE politician_id=? ORDER BY rowid DESC LIMIT 50` が降順スキャンで早期終了できる。
--   `(politician_id, date)` にすると並びが (politician_id, date, rowid) になって
--   SQLite が rowid 順を保証できなくなり、**一時B-TREEでその議員の全発言を読む**。
--   date 単体のインデックスは、日付から rowid の境界を求めるために残す。
CREATE INDEX IF NOT EXISTS idx_speech_date ON speech(date);
CREATE INDEX IF NOT EXISTS idx_speech_speaker ON speech(speaker);
CREATE INDEX IF NOT EXISTS idx_speech_issue ON speech(issue_id, speech_order);
CREATE INDEX IF NOT EXISTS idx_speech_kind ON speech(speaker_kind);
CREATE INDEX IF NOT EXISTS idx_speech_politician ON speech(politician_id);

-- id は URL に出る（/politician/123）。data/politician_ids.json の台帳で維持していて、
-- 作り直しても変わらない。採番は scripts/build_politicians.py。
CREATE TABLE IF NOT EXISTS politician (
    id              INTEGER PRIMARY KEY,
    -- 会議録での代表表記。通称（あべ俊子）で通っている議員がいるので
    -- Wikidata の表記では上書きしない
    name            TEXT NOT NULL,
    name_kana       TEXT,
    house           TEXT,
    district        TEXT,
    wikidata_id     TEXT UNIQUE,
    wikidata_name   TEXT,
    official_url    TEXT,
    -- 全期間の集計。年ごとDBに分割しても値は同じ（議員一覧の並べ替えに使う）
    n_speeches      INTEGER NOT NULL DEFAULT 0,
    first_date      TEXT,
    last_date       TEXT
);

CREATE INDEX IF NOT EXISTS idx_politician_name ON politician(name);

CREATE TABLE IF NOT EXISTS affiliation (
    id              INTEGER PRIMARY KEY,
    politician_id   INTEGER NOT NULL REFERENCES politician(id),
    -- 会議録に書いてある事実。集計の既定単位はこちら
    kaiha           TEXT,
    -- 会派から政党を特定できたときだけ入る。**特定できなければ NULL**。
    -- `'無所属'` は「政党に所属していないと分かっている」という値で、
    -- NULL（＝特定できていない）とは意味が違う。政党別に集計するときは
    -- 政党のひとつとして混ぜず、別の区分として出すこと
    party           TEXT,
    -- 「その会派で発言した最初と最後の日」。在籍期間そのものではない
    start_date      TEXT,
    end_date        TEXT,
    n_speeches      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_affiliation_politician ON affiliation(politician_id, start_date);

-- 争点語。FTS5(trigram) が引けない2文字以下の語への対策であり、
-- よく引かれる語をFTSから外して速くするための索引でもある
-- （`docs/DECISIONS.md`）。リストは data/topics.json。
CREATE TABLE IF NOT EXISTS topic (
    id              INTEGER PRIMARY KEY,
    term            TEXT NOT NULL UNIQUE,
    category        TEXT,
    variants        TEXT,     -- JSON配列。合算した別表記
    -- **この年の件数**。全期間の集計は data/dist/topics.json 側にある
    n_speeches      INTEGER NOT NULL DEFAULT 0
);

-- 争点語 → 発言。speech_rowid の降順スキャンで「新しい順」を早期終了できるよう、
-- 主キーの並びをそのまま使う（WITHOUT ROWID にして重複した索引を持たない）
CREATE TABLE IF NOT EXISTS topic_hit (
    topic_id        INTEGER NOT NULL REFERENCES topic(id),
    speech_rowid    INTEGER NOT NULL,
    n               INTEGER NOT NULL,   -- その発言中の出現回数
    PRIMARY KEY (topic_id, speech_rowid)
) WITHOUT ROWID;

-- 2文字語の索引。FTS5(trigram) は3文字未満のトークンを作れないので、
-- 「増税」「憲法」「年金」「原発」はこれが無いと**原理的に引けない**。
-- 争点語（topic）とは役割が違う: あちらは運営が選ぶ編集方針で、頻度推移にも使う。
-- こちらは**本文から機械的に採った索引で、一覧としては見せない**。
--
-- **語彙リストは持たない。** 本文に出てくる2文字窓を全部入れる（build_word_index）。
-- したがって中身は期間ごとに違ってよい。「この語を引けるか」を事前に判定する
-- 仕組みは無く、索引に無い語は素直に0件になる。
CREATE TABLE IF NOT EXISTS word (
    id              INTEGER PRIMARY KEY,
    term            TEXT NOT NULL UNIQUE,
    -- **この期間の件数**。複数語の検索でどれを起点にするか選ぶのに使う
    n_speeches      INTEGER NOT NULL DEFAULT 0
);

-- 2文字語 → 発言。topic_hit と同じ形で、speech_rowid の降順スキャンで
-- 「新しい順」を早期終了できるよう主キーの並びをそのまま使う。
-- topic_hit と違って出現回数を持たない（並べ替えにも表示にも使わないため。
-- 1行あたり数バイトの差でも、行数が2桁多いので効いてくる）
CREATE TABLE IF NOT EXISTS word_hit (
    word_id         INTEGER NOT NULL REFERENCES word(id),
    speech_rowid    INTEGER NOT NULL,
    PRIMARY KEY (word_id, speech_rowid)
) WITHOUT ROWID;

-- 期間DBの素性。
--   period / period_rule : この配信物がどの単位で割られているか（`2026H1` / `half`）
--   from / to            : 実データの収録範囲。目録に載せて期間の選択に使う
--   topics               : 争点語の指紋。**期間をまたいで一致していること**
--                          （食い違うと `topic_id` がずれ、別の争点を引く）
-- 2文字語の語彙は**期間ごとに違ってよい**ので、ここには入れない。
CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

-- 名寄せレポートの材料
CREATE VIEW IF NOT EXISTS speaker_stats AS
SELECT speaker, speaker_yomi, speaker_group,
       COUNT(*)  AS n_speeches,
       MIN(date) AS first_date,
       MAX(date) AS last_date
FROM speech
WHERE speaker_kind = '議員'
GROUP BY speaker, speaker_yomi, speaker_group;
"""

# external content 方式。本文は speech テーブルにあるので snippet()/highlight() が使える。
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS speech_fts USING fts5(
    body,
    content='speech',
    content_rowid='rowid',
    tokenize='trigram'
);
"""


def classify(record: dict) -> tuple[int, str]:
    """(is_speech, speaker_kind) を返す。

    実データで確認した事実:
      - speakerYomi と speakerGroup の欠落は完全に一致する
      - speakerRole が付くレコードに speakerYomi は付かない（重複ゼロ）
    したがって以下の判定は排他的で、順序に依存しない。
    """
    if record.get("speaker") in NON_SPEECH_SPEAKERS:
        return 0, "非発言"
    role = record.get("speakerRole")
    if role in ("参考人", "公述人", "証人"):
        return 1, role
    if record.get("speakerYomi"):
        return 1, "議員"
    return 1, "政府参考人等"


def period_of(date: str, rule: str) -> str:
    """日付（`YYYY-MM-DD`）→ 期間ID。

    ★ **サイト側の `periodOf()`（site/src/lib/query.ts）と同じ写像にすること。**
      DBの分割規則そのもので、食い違うと引き先のファイルが存在しなくなる。

    半期の境界を7月1日に置いているのは、常会（1月召集）が上半期にほぼ収まり、
    残りが小さいファイルになるため。実測では H1 が 309〜356MB、H2 が 6〜110MB。
    偏りは害にならない（小さいDBは B-tree が浅く、引くのが安い）。
    """
    if rule == "year":
        return date[:4]
    return f"{date[:4]}H{'1' if date[5:7] <= '06' else '2'}"


def period_year(period: str) -> str:
    """期間ID → 年。**期間は必ず年に閉じている**ので、先頭4文字でよい。"""
    return period[:4]


def raw_files(raw_dir: Path, period: str | None = None) -> list[Path]:
    """NDJSON は `YYYY-MM.ndjson`。

    期間の指定があっても**その年ぶんを全部読む**。ファイルの月と中身の日付は
    ずれることがあり（取得月をまたぐ会議録）、月で絞ると半期の境界で落ちる。
    絞り込みは `load()` がレコードの日付で行う。
    """
    pattern = f"{period_year(period)}-*.ndjson" if period else "*.ndjson"
    files = sorted(raw_dir.glob(pattern))
    if not files:
        target = f"（{period}）" if period else ""
        raise SystemExit(f"NDJSON が見つからない{target}: {raw_dir}\n"
                         f"先に scripts/fetch_range.py を実行すること")
    return files


def available_periods(raw_dir: Path, rule: str) -> list[str]:
    """取得済みの NDJSON から期間IDを起こす。ファイル名の年月で決める。"""
    periods = {period_of(f"{m.group(1)}-{m.group(2)}-01", rule)
               for path in raw_dir.glob("*.ndjson")
               if (m := re.fullmatch(r"(\d{4})-(\d{2})", path.stem))}
    if not periods:
        raise SystemExit(f"NDJSON が見つからない: {raw_dir}")
    return sorted(periods)


def iter_raw_records(raw_dir: Path, period: str | None = None):
    for path in raw_files(raw_dir, period):
        logger.info("読み込み %s", path.name)
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def load_politicians(path: Path) -> tuple[list[dict], dict[str, int]]:
    """議員マスタと「突合単位 → 議員ID」の対応。無ければ空で通す。

    無くてもDBは作れる（politician_id が NULL になるだけ）ので、
    先に build_politicians.py を回していなくても止めない。
    """
    if not path.exists():
        logger.warning("%s が無い。politician_id は NULL のままになる。"
                       "先に scripts/build_politicians.py を実行すること", path.name)
        return [], {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["politicians"], data["units"]


def insert_politicians(con: sqlite3.Connection, politicians: list[dict]) -> int:
    con.executemany(
        "INSERT OR REPLACE INTO politician"
        " (id, name, name_kana, house, district, wikidata_id, wikidata_name,"
        "  official_url, n_speeches, first_date, last_date)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(p["id"], p["name"], p["name_kana"], p["house"], None, p["wikidata_id"],
          p["wikidata_name"], p["official_url"], p["n_speeches"],
          p["first_date"], p["last_date"]) for p in politicians])
    con.executemany(
        "INSERT INTO affiliation"
        " (politician_id, kaiha, party, start_date, end_date, n_speeches)"
        " VALUES (?,?,?,?,?,?)",
        [(p["id"], a["kaiha"], a["party"], a["start_date"], a["end_date"], a["n_speeches"])
         for p in politicians for a in p["affiliations"]])
    con.commit()
    return sum(len(p["affiliations"]) for p in politicians)


def check_topic_ids(topics: list[dict], path: Path) -> None:
    """`id` が**書いてあること**と、重複が無いことを確かめる。

    **並び順から採らない。** 以前は `enumerate` で振っていたが、それだと語を1つ
    差し込むだけで以降の `topic_id` が全部ずれ、配信済みDBの `topic_hit` が
    **別の争点の発言を黙って返す**（件数は正しいまま中身だけ入れ替わる）。
    id を書かせることで、`topics.json` の編集と配信済みDBが独立になる。
    """
    missing = [t.get("term") for t in topics if not isinstance(t.get("id"), int)]
    if missing:
        raise SystemExit(
            f"★ {path} に id の無い争点語がある: {missing}\n"
            f"  id は不変の識別子。未使用の最大値+1 を手で振ること（消した id は再利用しない）")
    seen: dict[int, str] = {}
    for topic in topics:
        if topic["id"] in seen:
            raise SystemExit(
                f"★ {path} の id {topic['id']} が重複している"
                f"（{seen[topic['id']]!r} と {topic['term']!r}）")
        seen[topic["id"]] = topic["term"]


def topics_fingerprint(items: list[tuple[int, str, list[str]]]) -> str:
    """争点語の**中身**の指紋。`(id, term, variants)` の組で作る。

    id だけでは足りない。`term` を書き換えて作り直さないと、配信済みDBの
    `topic_hit` は**古い語**のヒットを持ったまま新しい語の名前で表示される。
    目録（`write_manifest`）に期間ごとに載せて、サイト側が
    「この期間の `topic_hit` を信じてよいか」を判断する材料にする。
    """
    return fingerprint([f"{i}\t{term}\t{chr(31).join(variants)}"
                        for i, term, variants in sorted(items)])


def format_id_ranges(ids: list[int]) -> str:
    """`[1,2,3,5]` → `"1-3,5"`。**目録に82個の数値を並べないため**だけの表現。

    `json.dumps(indent=1)` は配列を1要素1行で書くので、素直に持つと目録が
    1,000行近くになる（期間12本 × 争点語82件）。人が読める形のまま短くする。
    """
    runs: list[list[int]] = []
    for value in sorted(set(ids)):
        if runs and value == runs[-1][1] + 1:
            runs[-1][1] = value
        else:
            runs.append([value, value])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def parse_id_ranges(text: str) -> list[int]:
    """`"1-3,5"` → `[1,2,3,5]`。読めない表記は空（＝その期間は争点語を持たない扱い）。"""
    ids: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start, end = (int(x) for x in part.split("-", 1))
                ids.extend(range(start, end + 1))
            else:
                ids.append(int(part))
        except ValueError:
            return []
    return sorted(set(ids))


def read_topics(con: sqlite3.Connection) -> list[tuple[int, str, list[str]]]:
    """期間DBが実際に持っている争点語。`topic` 表が無ければ `OperationalError`。"""
    return [(int(i), term, json.loads(variants or "[]"))
            for i, term, variants in con.execute(
                "SELECT id, term, variants FROM topic ORDER BY id")]


def stamp_indexed(topics_json: Path, manifest_json: Path) -> tuple[int, int]:
    """`dist/topics.json` の各語に `indexed` を付け直す。**サイトの引き先が決まる。**

    `indexed` は「**配信済みの全期間に、この id が、この語のまま入っている**」。
    偽なら `/topic/<id>` と検索は `topic_hit` を使わず、普通の検索経路
    （2文字語は `word`、3文字以上は FTS）に落ちる。**結果は同じで、遅くなるだけ**
    （実測: 82語のうち別表記を持つ2語を除いて件数が完全に一致する）。

    これがあるので **`topics.json` に語を足すのに全期間のDBの作り直しが要らない。**
    作り直しは「新しい語を FTS 速度から救う」ための任意の作業になる。

    ★ **`write_manifest()` の後に呼ぶこと。** 目録がその期間の中身の正本で、
      手元に無い期間は前回の記載を引き継いでいる（日次更新はそれで正しい）。

    戻り値は (indexed の数, 争点語の数)。
    """
    if not topics_json.exists():
        return (0, 0)
    data = json.loads(topics_json.read_text(encoding="utf-8"))
    topics = data.get("topics", [])
    by_id = {t["id"]: t for t in topics}

    periods: list[tuple[str, set[int]]] = []
    trusted = True
    if manifest_json.exists():
        databases = json.loads(manifest_json.read_text(encoding="utf-8"))["databases"]
    else:
        databases = []
        trusted = False
        logger.warning("%s が無い。争点語の索引は使わない扱いにする", manifest_json.name)

    unrecorded: list[str] = []
    for entry in databases:
        held = entry.get("topics") or {}
        if not held:
            unrecorded.append(entry["id"])
        ids = set(parse_id_ranges(held.get("ids", "")))
        periods.append((entry["id"], ids))
        # **中身の照合。** id が同じでも term を書き換えていれば、配信済みDBの
        # topic_hit は古い語のヒットを持ったまま新しい語の名前で出てしまう。
        #
        # ★ 指紋は**その期間が持っている語ぜんぶ**で作ってあるので、
        #   `topics.json` から語を消すと照合できなくなる（消えた語の term が手元に無い）。
        #   **照合できないものは信じない**。遅くなるだけで、間違ったものは出ない
        if dropped := sorted(ids - by_id.keys()):
            trusted = False
            logger.warning("%s は topics.json に無い争点語を持っている（id=%s）。"
                           "照合できないので topic_hit を使わない"
                           " - 全期間を作り直すと解消する",
                           entry["id"], format_id_ranges(dropped))
            continue
        if ids and held.get("fp") != topics_fingerprint(
                [(i, by_id[i]["term"], by_id[i]["variants"]) for i in sorted(ids)]):
            trusted = False
            logger.error("★ %s の争点語が topics.json と違う（同じ id のまま term か "
                         "variants を書き換えた）。全期間を作り直すまで topic_hit を使わない",
                         entry["id"])

    # **記載が無い期間は「持っていない」扱い**（推測しない）。全部が検索経路に落ちるので
    # 遅くなるだけだが、放置する理由も無い。手元にDBがあるなら --manifest-only で載る
    if unrecorded:
        logger.warning("目録に争点語の記載が無い期間: %s"
                       " - その期間を持っていない扱いにする（争点語の検索が遅くなる）。"
                       "手元にDBを揃えて build_db.py --manifest-only で載る",
                       " ".join(unrecorded))

    n_indexed = 0
    for topic in topics:
        ok = bool(trusted and periods) and all(topic["id"] in ids for _, ids in periods)
        topic["indexed"] = ok
        n_indexed += ok

    topics_json.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    return (n_indexed, len(topics))


def load_topics(path: Path) -> list[dict]:
    """争点語のリスト。無ければ空で通す（争点語の機能が無いDBになるだけ）。"""
    if not path.exists():
        logger.warning("%s が無い。争点語の索引を作らない", path.name)
        return []
    topics = json.loads(path.read_text(encoding="utf-8"))["topics"]
    check_topic_ids(topics, path)
    for topic in topics:
        topic.setdefault("variants", [])
        topic.setdefault("category", None)
    return topics


def build_topic_index(con: sqlite3.Connection, topics: list[dict]) -> int:
    """議員の発言を走査して争点語の索引を作る。

    rowid は投入順（＝日付順）に振られているので、そのまま「新しい順」に使える。
    投入直後にDBから読み直しているのは、rowid を推測せず実際の値を使うため。
    """
    if not topics:
        return 0
    forms = [(t["id"], [t["term"], *t["variants"]]) for t in topics]
    hits: list[tuple[int, int, int]] = []
    counts: dict[int, int] = defaultdict(int)

    for rowid, body in con.execute(
            "SELECT rowid, body FROM speech WHERE speaker_kind = ?", INDEXED_KINDS[:1]):
        for topic_id, form_list in forms:
            n = sum(body.count(form) for form in form_list)
            if n:
                hits.append((topic_id, rowid, n))
                counts[topic_id] += 1

    con.executemany(
        "INSERT OR REPLACE INTO topic (id, term, category, variants, n_speeches)"
        " VALUES (?,?,?,?,?)",
        [(t["id"], t["term"], t["category"], json.dumps(t["variants"], ensure_ascii=False),
          counts.get(t["id"], 0)) for t in topics])
    con.executemany("INSERT OR REPLACE INTO topic_hit VALUES (?,?,?)", hits)
    con.commit()
    return len(hits)


def fingerprint(items: list[str]) -> str:
    """期間をまたいで同じでなければならないもの（争点語）を比べる指紋。"""
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()[:16]


def file_version(path: Path) -> str:
    """DBファイルの中身の指紋。**URL に付けて配信の世代を分ける**ために使う。

    日次更新で当年のDBを差し替えると、開きっぱなしのページは
    「古いページと新しいページが混ざったDB」を読むことになり、
    `no such table: speech_fts` のような形で壊れる。
    URL が世代ごとに変わっていれば、CDN のキャッシュもそのまま入れ替わる
    （毎日パージしなくてよい）。
    """
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()[:16]


def build_word_index(con: sqlite3.Connection) -> tuple[int, int]:
    """2文字語の索引を作る。**語彙リストを持たず、出てきた2文字窓を全部入れる。**

    漢字/カタカナ/全角ラテンの連続を取り出し、その中の2文字窓を全部見る。
    2文字の漢字列は必ず長さ2以上の漢字連続の中にあるので、これで取りこぼさない。
    全角ラテンは大文字に畳んでから窓を切る（`ａｉ` と打たれても `ＡＩ` に当たるように）。

    かつては `data/words.json`（機械抽出の語彙）に載っている語だけを索引にしていた。
    やめた理由は実測（2026年DB・議員の発言 45,756件）:

        現行の語彙 16,264語 : word_hit 1,759,180行 / 索引の正味 17.6 MiB
        全bigram   87,846語 : word_hit 3,017,020行 / 索引の正味 32.8 MiB

    **行数は 1.72倍にしかならず、配布サイズの増加は +7.3%。** 語彙の裾（1発言に
    しか出ない語）は語数の38%を占めるのに行数では1%しか食わないので、絞る意味が
    ほとんど無かった。代わりに得たものが3つある:

      - 「治体」「務大」「ロナ」のような**語跨ぎの断片や低頻度語も引ける**
      - 「その語は索引に無い」という状態が消える（引けない語＝本当に0件）
      - **語彙が期間ごとに違ってよくなる**。日次更新で当該期間だけ作り直しても
        過去の期間が黙って0件になる事故（docs/PITFALLS.md）が原理的に起きない

    行数が多いので一定件数ごとに流し込む。全部ためると数百MBのリストになる。
    戻り値は (語数, 行数)。
    """
    ids: dict[str, int] = {}
    counts: dict[int, int] = defaultdict(int)
    batch: list[tuple[int, int]] = []
    total = 0

    def flush() -> None:
        nonlocal batch
        con.executemany("INSERT OR REPLACE INTO word_hit VALUES (?,?)", batch)
        batch = []

    for rowid, body in con.execute(
            "SELECT rowid, body FROM speech WHERE speaker_kind = ?", INDEXED_KINDS[:1]):
        found = set()
        for run in WORD_RUN_PATTERN.findall(body):
            run = fold_word_run(run)
            for i in range(len(run) - 1):
                term = run[i:i + 2]
                word_id = ids.get(term)
                if word_id is None:
                    word_id = ids[term] = len(ids) + 1
                found.add(word_id)
        for word_id in found:
            counts[word_id] += 1
        batch.extend((word_id, rowid) for word_id in found)
        total += len(found)
        if len(batch) >= 500_000:
            flush()

    flush()
    # n_speeches は**この期間の件数**。複数語の検索で起点を選ぶのに使う
    con.executemany("INSERT OR REPLACE INTO word (id, term, n_speeches) VALUES (?,?,?)",
                    [(i, term, counts[i]) for term, i in ids.items()])
    con.commit()
    return len(ids), total


def load(con: sqlite3.Connection, raw_dir: Path, period: str | None = None,
         unit_map: dict[str, int] | None = None,
         rule: str = DEFAULT_PERIOD) -> tuple[int, int]:
    meetings: dict[str, tuple] = {}
    speeches: list[tuple] = []
    out_of_range = 0
    unit_map = unit_map or {}
    unresolved = 0

    for record in iter_raw_records(raw_dir, period):
        # ファイル名ではなくレコードの日付で期間を決める。取得月とズレていても落とさない
        if period and period_of(record["date"], rule) != period:
            out_of_range += 1
            continue
        issue_id = record["issueID"]
        if issue_id not in meetings:
            meetings[issue_id] = (
                issue_id, record["session"], record["nameOfHouse"], record["nameOfMeeting"],
                record.get("issue"), record["date"],
                record.get("meetingURL"), record.get("pdfURL"),
            )
        is_speech, kind = classify(record)
        politician_id = None
        if kind == "議員":
            # キーの組み立ては build_politicians.py の unit_key() と揃えること
            key = "\t".join([record["speaker"], record.get("speakerYomi") or "",
                             record.get("speakerGroup") or ""])
            politician_id = unit_map.get(key)
            if politician_id is None:
                unresolved += 1
        speeches.append((
            record["speechID"], issue_id, record["speechOrder"], record["date"],
            record["speaker"], record.get("speakerYomi"), record.get("speakerGroup"),
            record.get("speakerPosition"), record.get("speakerRole"),
            record["speech"], record.get("startPage"), record.get("speechURL"),
            is_speech, kind, politician_id,
        ))

    # rowid を日付の昇順にするための並べ替え。理由は SCHEMA のコメントを参照。
    # NDJSON は月ごとのファイルなので、そのままだと月境界で数十件が前後する（実測124件）。
    speeches.sort(key=lambda row: (row[3], row[1], row[2]))  # date, issue_id, speech_order

    con.executemany("INSERT OR REPLACE INTO meeting VALUES (?,?,?,?,?,?,?,?)", meetings.values())
    con.executemany(
        "INSERT OR REPLACE INTO speech "
        "(speech_id, issue_id, speech_order, date, speaker, speaker_yomi, speaker_group,"
        " speaker_position, speaker_role, body, start_page, speech_url, is_speech,"
        " speaker_kind, politician_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        speeches,
    )
    con.commit()
    if out_of_range:
        # 半期で割ると、同じ年のもう一方の期間ぶんがここに来る（毎回出る・異常ではない）
        logger.info("  %s の対象外レコード %s件。除外した", period, f"{out_of_range:,}")
    if unresolved and unit_map:
        logger.warning("  議員に紐づかなかった発言 %s件。build_politicians.py が古い可能性",
                       f"{unresolved:,}")
    return len(meetings), len(speeches)


def build_fts(con: sqlite3.Connection) -> None:
    """議員の発言だけを索引化する。

    'rebuild' は全行を索引化してしまうので使わない。
    """
    con.executescript(FTS_SCHEMA)
    placeholders = ",".join("?" * len(INDEXED_KINDS))
    con.execute(
        f"INSERT INTO speech_fts(rowid, body)"
        f" SELECT rowid, body FROM speech WHERE speaker_kind IN ({placeholders})",
        INDEXED_KINDS,
    )
    con.commit()


def check_highlight(con: sqlite3.Connection) -> None:
    """必須要件（whatiwant.md L27）のハイライトが動くことを確認する。"""
    row = con.execute("""
        SELECT s.speaker, snippet(speech_fts, 0, '[', ']', '…', 12)
        FROM speech_fts f JOIN speech s ON s.rowid = f.rowid
        WHERE speech_fts MATCH '"再稼働"' LIMIT 1
    """).fetchone()
    if row:
        print(f"\nハイライト動作確認: {row[0]} — {row[1]}")
    else:
        print("\nハイライト動作確認: 該当なし（検証語がこの範囲に無い）")


def open_fresh(db_path: Path, page_size: int) -> sqlite3.Connection:
    """空のDBを作って開く。page_size は最初のテーブルを作る前にしか変えられない。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)

    con = sqlite3.connect(db_path)
    con.execute(f"PRAGMA page_size = {page_size}")
    con.execute("PRAGMA journal_mode = WAL")  # 投入中だけ WAL。配信前に畳む
    con.executescript(SCHEMA)
    return con


def finalize(con: sqlite3.Connection, db_path: Path) -> None:
    """配信できる単一ファイルにする。

    WAL のままだと本体の外に -wal が要るので、HTTP Range で1ファイルしか読まない
    sql.js-httpvfs から開けない。VACUUM はページの断片化も解消し、
    1回の検索で触るページが散らばりにくくなる。
    """
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.execute("PRAGMA journal_mode = DELETE")
    con.execute("VACUUM")
    con.commit()
    for suffix in ("-wal", "-shm"):
        db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)


def build_period(raw_dir: Path, period: str, *, rule: str, page_size: int, with_fts: bool,
                 dist_dir: Path, politicians: list[dict], unit_map: dict[str, int],
                 topics: list[dict]) -> dict:
    """1期間分のDBを作って統計を返す。

    議員マスタは**全期間分をそのまま入れる**（1,111人で数百KB）。期間ごとに絞ると
    「その期間に発言していない議員のページ」が作れなくなるし、`n_speeches` の
    意味が期間によって変わってしまう。
    """
    db_path = dist_dir / f"kokkai-{period}.db"
    started = time.monotonic()
    logger.info("=== %s → %s ===", period, db_path.name)

    con = open_fresh(db_path, page_size)
    insert_politicians(con, politicians)
    n_meetings, n_speeches = load(con, raw_dir, period, unit_map, rule)
    n_hits = build_topic_index(con, topics)
    logger.info("  2文字語の索引を構築中…")
    n_words, n_word_hits = build_word_index(con)
    if with_fts:
        logger.info("  FTS5(trigram) を構築中…")
        build_fts(con)
    indexed = count_indexed(con)

    # 収録範囲は実データから採る。目録に載せてサイトの期間選択に使う
    covers = con.execute("SELECT MIN(date), MAX(date) FROM speech").fetchone()
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("period", period),
        ("period_rule", rule),
        ("from", covers[0] or ""),
        ("to", covers[1] or ""),
        ("n_words", str(n_words)),
        ("topics", fingerprint([t["term"] for t in topics])),
    ])
    con.commit()
    finalize(con, db_path)
    con.close()

    size = db_path.stat().st_size
    logger.info("  会議 %s / 発言 %s（索引 %s / 争点語 %s / 2文字語 %s語 %s行）"
                " / %.1f MB / %.0f秒",
                f"{n_meetings:,}", f"{n_speeches:,}", f"{indexed:,}", f"{n_hits:,}",
                f"{n_words:,}", f"{n_word_hits:,}", size / 1e6,
                time.monotonic() - started)
    return {"period": period, "path": db_path, "meetings": n_meetings,
            "speeches": n_speeches, "indexed": indexed, "hits": n_hits,
            "words": n_words, "word_hits": n_word_hits, "size": size,
            "from": covers[0] or "", "to": covers[1] or ""}


def count_indexed(con: sqlite3.Connection) -> int:
    """external content の FTS5 は COUNT(*) を本体テーブルへ委譲するため speech 側で数える。"""
    placeholders = ",".join("?" * len(INDEXED_KINDS))
    return con.execute(
        f"SELECT COUNT(*) FROM speech WHERE speaker_kind IN ({placeholders})",
        INDEXED_KINDS).fetchone()[0]


def write_manifest(dist_dir: Path) -> Path:
    """`data/dist/manifest.json` を出力先の実物から作り直す。

    サイトは「どの期間DBがあるか」をこれで知る（期間ごとに別ワーカを立てるため）。
    **引数ではなくディレクトリを走査する**ので、`--id 2026H2` だけを作り直しても
    他の期間が消えない。

    ファイルが手元に無い期間は、**前回の目録の記載を引き継ぐ**。日次更新（CI）は
    当該期間しか手元に置かないので、走査だけにすると目録が1本になってしまい、
    サイトが過去を引かなくなる。引き継いだ期間はログに出す。

    `from` / `to` を載せるのは、サイトが「この年を検索する」を期間IDに直すため。
    **規則（`periodOf`）でも引けるが、目録の実データを正とする。**

    `topics` は**その期間DBが実際に持っている争点語**（`{"ids": "1-82", "fp": …}`）。
    `topics.json` に語を足しても古い期間DBには `topic_hit` が無いので、
    どの期間が何を持っているかをここに残す。**引き継いだ期間の記載も同じ意味で正しい**
    （手元に無い＝作り直していない＝中身は前回のまま）。使うのは `stamp_indexed()`。
    """
    out = dist_dir / "manifest.json"
    previous: dict[str, dict] = {}
    rule = DEFAULT_PERIOD
    if out.exists():
        try:
            loaded = json.loads(out.read_text(encoding="utf-8"))
            previous = {e["id"]: e for e in loaded["databases"]}
            rule = loaded.get("period", rule)
        except (json.JSONDecodeError, KeyError):
            logger.warning("既存の %s を読めなかった。走査した分だけで作り直す", out.name)

    files = []
    for path in sorted(dist_dir.glob("kokkai-*.db")):
        period = path.stem.removeprefix("kokkai-")
        if not re.fullmatch(r"\d{4}(H[12])?", period):
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            meta = dict(con.execute("SELECT key, value FROM meta"))
        except sqlite3.OperationalError:
            meta = {}                      # meta を持たない古いDB
        try:
            held = read_topics(con)
        except sqlite3.OperationalError:
            held = []                      # 争点語を持たないDB
        finally:
            con.close()
        rule = meta.get("period_rule", rule)
        entry = {"id": period, "file": path.name, "size": path.stat().st_size,
                 "version": file_version(path),
                 "from": meta.get("from", ""), "to": meta.get("to", "")}
        if held:
            entry["topics"] = {"ids": format_id_ranges([i for i, _, _ in held]),
                               "fp": topics_fingerprint(held)}
        files.append(entry)

    built = {f["id"] for f in files}
    for period, entry in sorted(previous.items()):
        if period not in built:
            logger.info("目録: %s は手元に無いので前回の記載を引き継ぐ", period)
            files.append(entry)
    # 期間IDは辞書順が時系列順（2021H1 < 2021H2 < 2022H1）
    files.sort(key=lambda f: f["id"])

    manifest = {"period": rule, "periods": [f["id"] for f in files], "databases": files}
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return out


def run_split(args: argparse.Namespace) -> None:
    rule = args.period
    periods = args.id or []
    for year in args.year or []:
        periods += [p for p in available_periods(args.raw, rule) if period_year(p) == year]
    periods = sorted(set(periods)) or available_periods(args.raw, rule)

    politicians, unit_map = load_politicians(args.politicians)
    topics = load_topics(args.topics)
    logger.info("期間ごとに分割して構築（%s）: %s（議員 %s人 / 争点語 %s件）",
                rule, " ".join(periods), f"{len(politicians):,}", f"{len(topics):,}")
    results = [build_period(args.raw, period, rule=rule, page_size=args.page_size,
                            with_fts=not args.no_fts, dist_dir=args.dist,
                            politicians=politicians, unit_map=unit_map, topics=topics)
               for period in periods]

    print(f"\n--- 期間ごとDB（page_size={args.page_size} / {args.dist}）---")
    print(f"{'期間':<8}{'会議':>8}{'発言':>10}{'索引':>10}{'争点語':>10}"
          f"{'2文字語':>10}{'word_hit':>12}{'サイズ':>12}")
    for r in results:
        print(f"{r['period']:<8}{r['meetings']:>8,}{r['speeches']:>10,}"
              f"{r['indexed']:>10,}{r['hits']:>10,}{r['words']:>10,}{r['word_hits']:>12,}"
              f"{r['size'] / 1e6:>9.1f} MB")
    total = sum(r["size"] for r in results)
    biggest = max(results, key=lambda r: r["size"])
    print(f"{'合計':<8}{'':>8}{sum(r['speeches'] for r in results):>10,}"
          f"{sum(r['indexed'] for r in results):>10,}"
          f"{sum(r['hits'] for r in results):>10,}{'':>10}"
          f"{sum(r['word_hits'] for r in results):>12,}{total / 1e9:>9.2f} GB")
    print(f"\nR2無料枠 10GB に対して {100 * total / 10e9:.1f}%")
    # 512MB を超えたファイルは**黙って CDN キャッシュから外れる**（docs/DECISIONS.md）
    print(f"最大のファイル: {biggest['period']} {biggest['size'] / 1e6:.0f} MB"
          f"（CDNキャッシュ上限 512MB に対して {100 * biggest['size'] / 512e6:.0f}%）")
    if biggest["size"] > 480e6:
        logger.error("★ 512MB に近い。超えると黙ってキャッシュされなくなる"
                     "（RTT 8ms → 77ms）。分割を細かくすること")

    print(refresh_manifest(args.dist))


def refresh_manifest(dist_dir: Path) -> str:
    """目録を書き直し、`dist/topics.json` の `indexed` を付け直す。**この順で。**

    目録がその期間の中身の正本なので、印はそれを見てから付ける。
    """
    manifest = write_manifest(dist_dir)
    n_indexed, n_topics = stamp_indexed(dist_dir / "topics.json", manifest)
    return (f"目録: {manifest}\n"
            f"争点語: {n_indexed}/{n_topics}件を topic_hit で引ける"
            f"（残りは検索経路で出る）")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--raw", type=Path, default=RAW_DIR)
    parser.add_argument("--no-fts", action="store_true", help="全文検索インデックスを作らない")
    parser.add_argument("--fresh", action="store_true", help="既存DBを削除してから作る")
    parser.add_argument("--split", action="store_true",
                        help="期間ごとに data/dist/kokkai-<期間ID>.db を作る（配信用）")
    parser.add_argument("--period", choices=("half", "year"), default=DEFAULT_PERIOD,
                        help=f"分割の単位（既定 {DEFAULT_PERIOD}）。"
                             "★変えたら site/src/lib/query.ts の periodOf() も揃えること")
    parser.add_argument("--year", action="append", metavar="YYYY",
                        help="対象の年（その年の全期間）。複数指定可")
    parser.add_argument("--id", action="append", metavar="YYYYH1",
                        help="対象の期間ID。複数指定可。日次更新はこれを使う")
    parser.add_argument("--dist", type=Path, default=DIST_DIR, help="期間ごとDBの出力先")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
                        help=f"SQLiteのページサイズ（既定 {DEFAULT_PAGE_SIZE}）")
    parser.add_argument("--politicians", type=Path, default=POLITICIANS_PATH,
                        help="scripts/build_politicians.py の出力。無ければ議員を紐づけない")
    parser.add_argument("--topics", type=Path, default=TOPICS_PATH,
                        help="争点語のリスト。無ければ争点語の索引を作らない")
    parser.add_argument("--manifest-only", action="store_true",
                        help="DBを作らず、手元の data/dist を走査して目録と "
                             "dist/topics.json の indexed だけ作り直す")
    args = parser.parse_args()

    if args.manifest_only:
        print(refresh_manifest(args.dist))
        return

    if args.split or args.year or args.id:
        run_split(args)
        return

    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and args.db.exists():
        args.db.unlink()
        for suffix in ("-wal", "-shm"):
            args.db.with_name(args.db.name + suffix).unlink(missing_ok=True)

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)

    # 初回は politicians.json がまだ無い（この kokkai.db を材料に作る）ので、
    # 無ければ紐づけずに進む。2回目以降の再構築で埋まる
    politicians, unit_map = load_politicians(args.politicians)
    if politicians:
        insert_politicians(con, politicians)
    n_meetings, n_speeches = load(con, args.raw, None, unit_map)
    logger.info("投入: 会議 %s件 / 発言 %s件", f"{n_meetings:,}", f"{n_speeches:,}")

    print("\n--- 発言者の種別 ---")
    for kind, n in con.execute(
            "SELECT speaker_kind, COUNT(*) FROM speech GROUP BY 1 ORDER BY 2 DESC"):
        mark = " ★検索対象" if kind in INDEXED_KINDS else ""
        print(f"  {kind:<12} {n:>7,}件 ({100*n/n_speeches:5.1f}%){mark}")

    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size_before = args.db.stat().st_size
    print(f"\nFTSなしのサイズ: {size_before / 1024 / 1024:.2f} MB")

    if not args.no_fts:
        logger.info("FTS5(trigram) を構築中… 対象: %s", " / ".join(INDEXED_KINDS))
        build_fts(con)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size_after = args.db.stat().st_size
        indexed = count_indexed(con)
        print(f"FTSありのサイズ: {size_after / 1024 / 1024:.2f} MB "
              f"(索引 {indexed:,}件 / +{(size_after - size_before) / 1024 / 1024:.2f} MB)")

        per_row = size_after / n_speeches
        print(f"\n1発言あたり {per_row:,.0f} バイト（母数は全発言）")
        for label, count in (("直近5年", 591_773), ("2012年以降", 1_675_402)):
            print(f"  → {label} {count:,}件 なら約 {per_row * count / 1024**3:.2f} GB")
        check_highlight(con)

    con.close()


if __name__ == "__main__":
    main()
