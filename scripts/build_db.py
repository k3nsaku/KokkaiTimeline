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

## 年ごとの分割（Phase 1）

`--split-by-year` を付けると `data/dist/kokkai-YYYY.db` を年ごとに作る。
日次更新で変わるのは当年分だけなので、R2 へのアップロードが1ファイルで済み、
過去年は CDN キャッシュが効き続ける（`docs/DECISIONS.md`）。

配信するDBは **WAL にしない**。WAL は本体ファイルの外に -wal を持つため、
HTTP Range で1ファイルだけ読む sql.js-httpvfs から開けない。
ビルド中だけ WAL を使い、最後に `journal_mode=DELETE` + `VACUUM` で単一ファイルに畳む。

使い方:
    python scripts/build_db.py --fresh                  # 従来どおり単一DB
    python scripts/build_db.py --split-by-year          # 年ごとに data/dist/ へ
    python scripts/build_db.py --split-by-year --year 2025
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
WORDS_PATH = ROOT / "data" / "words.json"

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
# scripts/build_words.py の RUN_PATTERN と**同じもの**にすること。
# 語彙の側とずれると、語彙にあるのに索引に入らない語が出る。
# 全角ラテンを入れているのは `ＡＩ` `ＤＸ` `ＧＸ` `Ｇ７` のため（`docs/DECISIONS.md`）
WORD_RUN_PATTERN = re.compile(r"[一-鿿々]{2,}|[ァ-ヴー]{2,}|[Ａ-Ｚａ-ｚ０-９]{2,}")

# 全角ラテンの小文字 → 大文字。**語彙（words.json）も同じ形で作られている。**
# 畳む理由と、`ＳＤＧｓ` を壊さない理由は build_words.py の LATIN_FOLD を読むこと
WORD_LATIN_FOLD = {c: c - 0x20 for c in range(0xFF41, 0xFF5B)}


def fold_word_run(run: str) -> str:
    """全角ラテンの並びだけ大文字に畳む。build_words.py の fold() と同じもの。"""
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

-- 2文字語の語彙。FTS5(trigram) は3文字未満のトークンを作れないので、
-- 「増税」「憲法」「年金」「原発」はこれが無いと**原理的に引けない**。
-- 争点語（topic）とは役割が違う: あちらは運営が選ぶ編集方針で、頻度推移にも使う。
-- こちらは機械抽出の語彙で、**一覧としては見せない**（引けるかどうかを決めるだけ）。
-- リストは data/words.json、作るのは scripts/build_words.py。
CREATE TABLE IF NOT EXISTS word (
    id              INTEGER PRIMARY KEY,
    term            TEXT NOT NULL UNIQUE,
    -- **この年の件数**。複数語の検索でどれを起点にするか選ぶのに使う
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

-- 年DBの素性。**年をまたいで食い違ってはいけないもの**を記録する。
-- 特に vocabulary（2文字語の語彙の指紋）: 日次更新で当年だけ作り直す設計なので、
-- 語彙を作り直すと当年だけ新しくなる。検索は「語彙は年によらない」前提で
-- 新しい年1つだけを見て引けるか判定するため、**過去年が黙って0件になる**。
-- write_manifest() が食い違いを検出して警告する
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


def raw_files(raw_dir: Path, year: str | None = None) -> list[Path]:
    """NDJSON は `YYYY-MM.ndjson`。年の指定があればファイル名で絞る。"""
    pattern = f"{year}-*.ndjson" if year else "*.ndjson"
    files = sorted(raw_dir.glob(pattern))
    if not files:
        target = f"（{year}年）" if year else ""
        raise SystemExit(f"NDJSON が見つからない{target}: {raw_dir}\n"
                         f"先に scripts/fetch_range.py を実行すること")
    return files


def available_years(raw_dir: Path) -> list[str]:
    years = {m.group(1) for path in raw_dir.glob("*.ndjson")
             if (m := re.fullmatch(r"(\d{4})-\d{2}", path.stem))}
    if not years:
        raise SystemExit(f"NDJSON が見つからない: {raw_dir}")
    return sorted(years)


def iter_raw_records(raw_dir: Path, year: str | None = None):
    for path in raw_files(raw_dir, year):
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


def load_topics(path: Path) -> list[dict]:
    """争点語のリスト。無ければ空で通す（争点語の機能が無いDBになるだけ）。"""
    if not path.exists():
        logger.warning("%s が無い。争点語の索引を作らない", path.name)
        return []
    topics = json.loads(path.read_text(encoding="utf-8"))["topics"]
    for i, topic in enumerate(topics, 1):
        topic.setdefault("id", i)
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


def load_words(path: Path) -> list[str]:
    """2文字語の語彙。無ければ空で通す（2文字語を引けないDBになるだけ）。"""
    if not path.exists():
        logger.warning("%s が無い。2文字語の索引を作らない", path.name)
        return []
    return list(json.loads(path.read_text(encoding="utf-8"))["words"])


def fingerprint(items: list[str]) -> str:
    """語彙の指紋。年をまたいで同じでなければならないものを比べるのに使う。"""
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


def build_word_index(con: sqlite3.Connection, words: list[str]) -> int:
    """2文字語の索引を作る。

    **語彙の選定と数え方が違う。** `build_words.py` は「ちょうど2文字で自立している」
    箇所だけを数えて語彙を決めるが、索引は**部分文字列**で拾う。
    「憲法改正」の中の「憲法」を引けないと検索として意味がないため。

    そのために漢字/カタカナ/全角ラテンの連続を取り出し、その中の2文字窓を全部見る。
    2文字の漢字列は必ず長さ2以上の漢字連続の中にあるので、これで取りこぼさない。
    全角ラテンは大文字に畳んでから窓を切る（語彙も同じ形で作られている）。

    行数が多い（1年で約350万行）ので、一定件数ごとに流し込む。
    全部ためると数百MBのリストになる。
    """
    if not words:
        return 0

    ids = {term: i for i, term in enumerate(words, 1)}
    con.executemany("INSERT OR REPLACE INTO word (id, term, n_speeches) VALUES (?,?,0)",
                    [(i, term) for term, i in ids.items()])

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
                word_id = ids.get(run[i:i + 2])
                if word_id is not None:
                    found.add(word_id)
        for word_id in found:
            counts[word_id] += 1
        batch.extend((word_id, rowid) for word_id in found)
        total += len(found)
        if len(batch) >= 500_000:
            flush()

    flush()
    con.executemany("UPDATE word SET n_speeches = ? WHERE id = ?",
                    [(n, word_id) for word_id, n in counts.items()])
    con.commit()
    return total


def load(con: sqlite3.Connection, raw_dir: Path, year: str | None = None,
         unit_map: dict[str, int] | None = None) -> tuple[int, int]:
    meetings: dict[str, tuple] = {}
    speeches: list[tuple] = []
    out_of_range = 0
    unit_map = unit_map or {}
    unresolved = 0

    for record in iter_raw_records(raw_dir, year):
        # ファイル名ではなくレコードの日付で年を決める。取得月とズレていても落とさない
        if year and not record["date"].startswith(year):
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
        logger.warning("  %s年のファイルに他年のレコード %s件。除外した",
                       year, f"{out_of_range:,}")
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


def build_year(raw_dir: Path, year: str, *, page_size: int, with_fts: bool,
               dist_dir: Path, politicians: list[dict], unit_map: dict[str, int],
               topics: list[dict], words: list[str]) -> dict:
    """1年分のDBを作って統計を返す。

    議員マスタは**全期間分をそのまま入れる**（1,111人で数百KB）。年ごとに絞ると
    「その年に発言していない議員のページ」が作れなくなるし、`n_speeches` の
    意味が年によって変わってしまう。
    """
    db_path = dist_dir / f"kokkai-{year}.db"
    started = time.monotonic()
    logger.info("=== %s年 → %s ===", year, db_path.name)

    con = open_fresh(db_path, page_size)
    con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
        ("vocabulary", fingerprint(words)),
        ("n_words", str(len(words))),
        ("topics", fingerprint([t["term"] for t in topics])),
    ])
    insert_politicians(con, politicians)
    n_meetings, n_speeches = load(con, raw_dir, year, unit_map)
    n_hits = build_topic_index(con, topics)
    logger.info("  2文字語の索引を構築中（語彙 %s件）…", f"{len(words):,}")
    n_word_hits = build_word_index(con, words)
    if with_fts:
        logger.info("  FTS5(trigram) を構築中…")
        build_fts(con)
    indexed = count_indexed(con)
    finalize(con, db_path)
    con.close()

    size = db_path.stat().st_size
    logger.info("  会議 %s / 発言 %s（索引 %s / 争点語 %s / 2文字語 %s） / %.1f MB / %.0f秒",
                f"{n_meetings:,}", f"{n_speeches:,}", f"{indexed:,}", f"{n_hits:,}",
                f"{n_word_hits:,}", size / 1024**2, time.monotonic() - started)
    return {"year": year, "path": db_path, "meetings": n_meetings,
            "speeches": n_speeches, "indexed": indexed, "hits": n_hits,
            "word_hits": n_word_hits, "size": size}


def count_indexed(con: sqlite3.Connection) -> int:
    """external content の FTS5 は COUNT(*) を本体テーブルへ委譲するため speech 側で数える。"""
    placeholders = ",".join("?" * len(INDEXED_KINDS))
    return con.execute(
        f"SELECT COUNT(*) FROM speech WHERE speaker_kind IN ({placeholders})",
        INDEXED_KINDS).fetchone()[0]


def write_manifest(dist_dir: Path) -> Path:
    """`data/dist/manifest.json` を出力先の実物から作り直す。

    サイトは「どの年DBがあるか」をこれで知る（年ごとに別ワーカを立てるため、
    年の一覧が要る）。**引数の年ではなくディレクトリを走査する**ので、
    `--year 2026` だけを作り直しても他の年が消えない。

    ファイルが手元に無い年は、**前回の目録の記載を引き継ぐ**。日次更新（CI）は
    当年しか手元に置かないので、走査だけにすると目録が当年1年になってしまい、
    サイトが過去年を引かなくなる。引き継いだ年はログに出す。
    """
    out = dist_dir / "manifest.json"
    previous: dict[int, dict] = {}
    if out.exists():
        try:
            previous = {e["year"]: e
                        for e in json.loads(out.read_text(encoding="utf-8"))["databases"]}
        except (json.JSONDecodeError, KeyError):
            logger.warning("既存の %s を読めなかった。走査した年だけで作り直す", out.name)

    files = []
    for path in sorted(dist_dir.glob("kokkai-*.db")):
        year = path.stem.removeprefix("kokkai-")
        if not year.isdigit():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            meta = dict(con.execute("SELECT key, value FROM meta"))
        except sqlite3.OperationalError:
            meta = {}                      # meta を持たない古いDB
        finally:
            con.close()
        files.append({"year": int(year), "file": path.name, "size": path.stat().st_size,
                      "version": file_version(path), "vocabulary": meta.get("vocabulary")})

    built = {f["year"] for f in files}
    for year, entry in sorted(previous.items()):
        if year not in built:
            logger.info("目録: %s年は手元に無いので前回の記載を引き継ぐ", year)
            files.append(entry)
    files.sort(key=lambda f: f["year"])

    warn_vocabulary_drift(files)

    manifest = {"years": [f["year"] for f in files], "databases": files}
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return out


def warn_vocabulary_drift(files: list[dict]) -> None:
    """年をまたいで2文字語の語彙が食い違っていないか見る。

    **食い違うと検索が黙って壊れる。** 検索は「語彙は年によらない」前提で
    いちばん新しい年だけを見て「その2文字語を引けるか」を判定するので、
    当年にしかない語を引くと**過去年が0件のまま返る**。エラーも出ない。

    日次更新で `build_words.py` を回してしまうとこうなる。
    語彙を作り直したら**全年を作り直すこと**（実測で約6分）。
    """
    # 指紋を持たないDB（meta 以前に作ったもの）も「別の語彙」として扱う。
    # 中身が同じである保証がどこにも無いため、素通ししない
    seen = {f["vocabulary"] for f in files}
    if len(seen) <= 1:
        return
    logger.error("★ 2文字語の語彙が年によって違う。検索が過去年で黙って0件になる")
    for f in files:
        logger.error("   %s: vocabulary=%s", f["file"], f["vocabulary"] or "(無し)")
    logger.error("   → data/words.json を固定したうえで**全年を作り直すこと**:")
    logger.error("      python scripts/build_db.py --split-by-year --page-size 8192")


def run_split(args: argparse.Namespace) -> None:
    years = args.year or available_years(args.raw)
    politicians, unit_map = load_politicians(args.politicians)
    topics = load_topics(args.topics)
    words = load_words(args.words)
    logger.info("年ごとに分割して構築: %s（議員 %s人 / 争点語 %s件 / 2文字語 %s件）",
                " ".join(years), f"{len(politicians):,}", f"{len(topics):,}", f"{len(words):,}")
    results = [build_year(args.raw, year, page_size=args.page_size,
                          with_fts=not args.no_fts, dist_dir=args.dist,
                          politicians=politicians, unit_map=unit_map, topics=topics,
                          words=words)
               for year in years]

    print(f"\n--- 年ごとDB（page_size={args.page_size} / {args.dist}）---")
    print(f"{'年':<6}{'会議':>8}{'発言':>10}{'索引':>10}{'争点語':>10}{'2文字語':>12}{'サイズ':>12}")
    for r in results:
        print(f"{r['year']:<6}{r['meetings']:>8,}{r['speeches']:>10,}"
              f"{r['indexed']:>10,}{r['hits']:>10,}{r['word_hits']:>12,}"
              f"{r['size'] / 1024**2:>10.1f} MB")
    total = sum(r["size"] for r in results)
    print(f"{'合計':<6}{'':>8}{sum(r['speeches'] for r in results):>10,}"
          f"{sum(r['indexed'] for r in results):>10,}"
          f"{sum(r['hits'] for r in results):>10,}"
          f"{sum(r['word_hits'] for r in results):>12,}{total / 1024**3:>10.2f} GB")
    print(f"\nR2無料枠 10GB に対して {100 * total / (10 * 1024**3):.1f}%")

    manifest = write_manifest(args.dist)
    print(f"目録: {manifest}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--raw", type=Path, default=RAW_DIR)
    parser.add_argument("--no-fts", action="store_true", help="全文検索インデックスを作らない")
    parser.add_argument("--fresh", action="store_true", help="既存DBを削除してから作る")
    parser.add_argument("--split-by-year", action="store_true",
                        help="年ごとに data/dist/kokkai-YYYY.db を作る（配信用）")
    parser.add_argument("--year", action="append", metavar="YYYY",
                        help="対象の年。複数指定可。省略時は取得済みの全年（--split-by-year 用）")
    parser.add_argument("--dist", type=Path, default=DIST_DIR, help="年ごとDBの出力先")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
                        help=f"SQLiteのページサイズ（既定 {DEFAULT_PAGE_SIZE}）")
    parser.add_argument("--politicians", type=Path, default=POLITICIANS_PATH,
                        help="scripts/build_politicians.py の出力。無ければ議員を紐づけない")
    parser.add_argument("--topics", type=Path, default=TOPICS_PATH,
                        help="争点語のリスト。無ければ争点語の索引を作らない")
    parser.add_argument("--words", type=Path, default=WORDS_PATH,
                        help="2文字語の語彙（scripts/build_words.py の出力）。"
                             "無ければ2文字語の索引を作らない")
    args = parser.parse_args()

    if args.split_by_year or args.year:
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
