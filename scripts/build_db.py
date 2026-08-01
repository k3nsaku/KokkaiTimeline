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

使い方:
    python scripts/build_db.py --fresh
    python scripts/build_db.py --no-fts
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "speeches"
DEFAULT_DB = ROOT / "data" / "kokkai.db"

logger = logging.getLogger("build_db")

# 「会議録情報」は各会議の冒頭に1件だけ入るヘッダ（出席委員名簿など）で、人の発言ではない。
NON_SPEECH_SPEAKERS = {"会議録情報", "会議録情報等", "目次"}

# 全文検索の対象にする発言者の種別
INDEXED_KINDS = ("議員",)

SCHEMA = """
PRAGMA journal_mode = WAL;

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

CREATE INDEX IF NOT EXISTS idx_speech_date ON speech(date);
CREATE INDEX IF NOT EXISTS idx_speech_speaker ON speech(speaker);
CREATE INDEX IF NOT EXISTS idx_speech_issue ON speech(issue_id, speech_order);
CREATE INDEX IF NOT EXISTS idx_speech_kind ON speech(speaker_kind, date);
CREATE INDEX IF NOT EXISTS idx_speech_politician ON speech(politician_id, date);

CREATE TABLE IF NOT EXISTS politician (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    name_kana       TEXT,
    house           TEXT,
    district        TEXT,
    wikidata_id     TEXT UNIQUE,
    official_url    TEXT
);

CREATE TABLE IF NOT EXISTS affiliation (
    id              INTEGER PRIMARY KEY,
    politician_id   INTEGER NOT NULL REFERENCES politician(id),
    party           TEXT NOT NULL,
    kaiha           TEXT,
    start_date      TEXT,
    end_date        TEXT
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


def iter_raw_records(raw_dir: Path):
    files = sorted(raw_dir.glob("*.ndjson"))
    if not files:
        raise SystemExit(f"NDJSON が見つからない: {raw_dir}\n先に scripts/fetch_range.py を実行すること")
    for path in files:
        logger.info("読み込み %s", path.name)
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def load(con: sqlite3.Connection, raw_dir: Path) -> tuple[int, int]:
    meetings: dict[str, tuple] = {}
    speeches: list[tuple] = []

    for record in iter_raw_records(raw_dir):
        issue_id = record["issueID"]
        if issue_id not in meetings:
            meetings[issue_id] = (
                issue_id, record["session"], record["nameOfHouse"], record["nameOfMeeting"],
                record.get("issue"), record["date"],
                record.get("meetingURL"), record.get("pdfURL"),
            )
        is_speech, kind = classify(record)
        speeches.append((
            record["speechID"], issue_id, record["speechOrder"], record["date"],
            record["speaker"], record.get("speakerYomi"), record.get("speakerGroup"),
            record.get("speakerPosition"), record.get("speakerRole"),
            record["speech"], record.get("startPage"), record.get("speechURL"),
            is_speech, kind,
        ))

    con.executemany("INSERT OR REPLACE INTO meeting VALUES (?,?,?,?,?,?,?,?)", meetings.values())
    con.executemany(
        "INSERT OR REPLACE INTO speech "
        "(speech_id, issue_id, speech_order, date, speaker, speaker_yomi, speaker_group,"
        " speaker_position, speaker_role, body, start_page, speech_url, is_speech, speaker_kind)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        speeches,
    )
    con.commit()
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--raw", type=Path, default=RAW_DIR)
    parser.add_argument("--no-fts", action="store_true", help="全文検索インデックスを作らない")
    parser.add_argument("--fresh", action="store_true", help="既存DBを削除してから作る")
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.fresh and args.db.exists():
        args.db.unlink()
        for suffix in ("-wal", "-shm"):
            args.db.with_name(args.db.name + suffix).unlink(missing_ok=True)

    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA)

    n_meetings, n_speeches = load(con, args.raw)
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
        # external content の FTS5 は COUNT(*) を本体テーブルへ委譲するため、
        # speech_fts を数えても索引件数にならない。speech 側で数える。
        placeholders = ",".join("?" * len(INDEXED_KINDS))
        indexed = con.execute(
            f"SELECT COUNT(*) FROM speech WHERE speaker_kind IN ({placeholders})",
            INDEXED_KINDS).fetchone()[0]
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
