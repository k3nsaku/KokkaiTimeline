"""Phase 0 ステップ2: 指定期間の発言を月単位で取得し NDJSON に落とす。

月ごとにファイルを分けて、既にあるファイルはスキップする（再開可能）。
バックフィルは数時間かかるので、途中で止めても続きから走れることが重要。

使い方:
    python scripts/fetch_range.py --from 2025-06-01 --until 2025-06-07
    python scripts/fetch_range.py --from 2021-01-01 --until 2025-12-31   # 直近5年
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ndl_api import iter_speeches  # noqa: E402

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "speeches"

logger = logging.getLogger("fetch_range")


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """期間を月単位に割る。1ファイルあたりのサイズと再開粒度のバランス。"""
    chunks: list[tuple[date, date]] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1)
        chunk_start = max(cursor, start)
        chunk_end = min(next_month.toordinal() - 1, end.toordinal())
        chunks.append((chunk_start, date.fromordinal(chunk_end)))
        cursor = next_month
    return chunks


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--until", dest="date_until", required=True)
    parser.add_argument("--force", action="store_true", help="既存ファイルも取り直す")
    args = parser.parse_args()

    start = date.fromisoformat(args.date_from)
    end = date.fromisoformat(args.date_until)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    chunks = month_chunks(start, end)
    logger.info("対象: %s 〜 %s (%d ファイル)", start, end, len(chunks))

    grand_total = 0
    for chunk_start, chunk_end in chunks:
        out_path = RAW_DIR / f"{chunk_start:%Y-%m}.ndjson"
        if out_path.exists() and not args.force:
            existing = sum(1 for _ in out_path.open(encoding="utf-8"))
            logger.info("skip %s (%d件、取得済み)", out_path.name, existing)
            grand_total += existing
            continue

        logger.info("fetch %s 〜 %s", chunk_start, chunk_end)
        count = 0
        tmp_path = out_path.with_suffix(".ndjson.part")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for record in iter_speeches(
                **{"from": chunk_start.isoformat(), "until": chunk_end.isoformat()}
            ):
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        # 完走したときだけ正式名にする（中断ファイルを「取得済み」と誤認しないため）
        tmp_path.replace(out_path)
        logger.info("wrote %s (%d件)", out_path.name, count)
        grand_total += count

    logger.info("合計 %s件 → %s", f"{grand_total:,}", RAW_DIR)


if __name__ == "__main__":
    main()
