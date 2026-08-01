"""Phase 0 ステップ2: 指定期間の発言を月単位で取得し NDJSON に落とす。

## 中断・再開の設計

直近5年のバックフィルは約5時間かかるため、**途中で止まっても安全に再開できる**
ことを前提に作っている。同じコマンドを再実行すれば続きから走る。

    data/raw/speeches/YYYY-MM.ndjson        完了した月（件数検証済み）
    data/raw/speeches/YYYY-MM.ndjson.part   取得中の月（再開可能）

- **月単位の再開** — 完了した月はスキップする
- **月の途中からの再開** — `.part` の行数を数えて、その続き（startRecord）から再開する
- **件数の検証** — 月の完了時に API 申告の件数と実際の行数を突き合わせる。
  合わなければ `.part` のまま残し、次回に取り直す
- **1か月の失敗で全体を止めない** — 失敗した月は記録して次の月へ進み、最後にまとめて報告する
- **100件ごとにフラッシュ** — 強制終了しても失われるのは最大100件

使い方:
    python scripts/fetch_range.py --from 2021-01-01 --until 2025-12-31   # 直近5年
    python scripts/fetch_range.py --from 2021-01-01 --until 2025-12-31   # ← 同じコマンドで再開
    python scripts/fetch_range.py --from 2021-01-01 --until 2025-12-31 --status  # 進捗確認のみ
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ndl_api import NDLAPIError, count_speeches, iter_speeches  # noqa: E402

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "speeches"
FLUSH_EVERY = 100  # 1ページ分。強制終了時の損失をこの件数に抑える

logger = logging.getLogger("fetch_range")


def month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """期間を月単位に割る。1ファイルのサイズと再開の粒度のバランス。"""
    chunks: list[tuple[date, date]] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1)
        chunks.append((
            max(cursor, start),
            date.fromordinal(min(next_month.toordinal() - 1, end.toordinal())),
        ))
        cursor = next_month
    return chunks


def paths_for(chunk_start: date) -> tuple[Path, Path]:
    done = RAW_DIR / f"{chunk_start:%Y-%m}.ndjson"
    return done, done.with_suffix(".ndjson.part")


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def show_status(chunks: list[tuple[date, date]]) -> None:
    done_n = part_n = todo_n = 0
    total_records = 0
    print(f"{'月':<10}{'状態':<10}{'件数':>10}")
    print("-" * 32)
    for chunk_start, _ in chunks:
        done, part = paths_for(chunk_start)
        if done.exists():
            n = count_lines(done)
            done_n, total_records = done_n + 1, total_records + n
            print(f"{chunk_start:%Y-%m}   {'完了':<10}{n:>10,}")
        elif part.exists():
            n = count_lines(part)
            part_n, total_records = part_n + 1, total_records + n
            print(f"{chunk_start:%Y-%m}   {'途中':<10}{n:>10,}  ← ここから再開")
        else:
            todo_n += 1
            print(f"{chunk_start:%Y-%m}   {'未取得':<10}{'—':>10}")
    print("-" * 32)
    print(f"完了 {done_n}か月 / 途中 {part_n}か月 / 未取得 {todo_n}か月 "
          f"（取得済み {total_records:,}件）")
    if todo_n or part_n:
        remaining_requests = todo_n * 100  # ざっくり: 1か月あたり約100リクエスト
        print(f"残り時間の目安: 約{remaining_requests * 3.1 / 3600:.1f}時間")


def fetch_month(chunk_start: date, chunk_end: date, *, force: bool) -> tuple[str, int]:
    """1か月分を取得する。戻り値は (状態, 件数)。"""
    done, part = paths_for(chunk_start)

    if done.exists() and not force:
        return "skip", count_lines(done)

    expected = count_speeches(**{"from": chunk_start.isoformat(), "until": chunk_end.isoformat()})

    if expected == 0:
        done.write_text("", encoding="utf-8")
        part.unlink(missing_ok=True)
        return "empty", 0

    already = 0 if force else count_lines(part)
    if force:
        part.unlink(missing_ok=True)
    if already >= expected:
        # 取り切っているのに rename されていない（前回の異常終了）
        part.replace(done)
        return "recovered", already

    if already:
        logger.info("  %s: %s件まで取得済み。%s件目から再開", f"{chunk_start:%Y-%m}",
                    f"{already:,}", f"{already + 1:,}")

    written = already
    with part.open("a", encoding="utf-8") as handle:
        for record in iter_speeches(
                start_record=already + 1,
                **{"from": chunk_start.isoformat(), "until": chunk_end.isoformat()}):
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
            if written % FLUSH_EVERY == 0:
                handle.flush()

    actual = count_lines(part)
    if actual != expected:
        # 件数が合わない月は完了扱いにしない。次回の実行で取り直す
        logger.warning("  %s: 件数不一致（API申告 %s / 実際 %s）。.part のまま残す",
                       f"{chunk_start:%Y-%m}", f"{expected:,}", f"{actual:,}")
        return "mismatch", actual

    part.replace(done)
    return "done", actual


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--until", dest="date_until", required=True)
    parser.add_argument("--force", action="store_true", help="完了済みの月も取り直す")
    parser.add_argument("--status", action="store_true", help="進捗を表示して終了する")
    args = parser.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    chunks = month_chunks(date.fromisoformat(args.date_from), date.fromisoformat(args.date_until))

    if args.status:
        show_status(chunks)
        return

    logger.info("対象: %s 〜 %s (%dか月)", args.date_from, args.date_until, len(chunks))

    grand_total = 0
    failures: list[tuple[str, str]] = []
    for index, (chunk_start, chunk_end) in enumerate(chunks, 1):
        label = f"{chunk_start:%Y-%m}"
        try:
            status, count = fetch_month(chunk_start, chunk_end, force=args.force)
        except (NDLAPIError, OSError) as exc:
            # 1か月の失敗で全体を止めない。次回の実行で取り直せる
            logger.error("[%d/%d] %s: 失敗 — %s", index, len(chunks), label, exc)
            failures.append((label, str(exc)))
            continue

        grand_total += count
        verb = {"skip": "スキップ", "done": "取得", "empty": "0件",
                "recovered": "復旧", "mismatch": "件数不一致"}[status]
        logger.info("[%d/%d] %s: %s (%s件) / 累計 %s件",
                    index, len(chunks), label, verb, f"{count:,}", f"{grand_total:,}")
        if status == "mismatch":
            failures.append((label, "件数不一致"))

    print(f"\n合計 {grand_total:,}件 → {RAW_DIR}")
    if failures:
        print(f"\n⚠️ {len(failures)}か月が未完了です。同じコマンドを再実行すれば取り直します:")
        for label, reason in failures:
            print(f"  {label}: {reason}")
        sys.exit(1)
    print("全期間の取得が完了しました。")


if __name__ == "__main__":
    main()
