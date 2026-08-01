"""Phase 0 ステップ1: 小範囲で NDL API を叩き、フィールドの実態を確認する。

いきなり5年分のバックフィルには行かない。まず数日分だけ取って
「どのフィールドに何が入っているか / 何が欠損するか」を目で見る。

使い方:
    python scripts/probe.py                        # 既定: 2025-06-02〜2025-06-06
    python scripts/probe.py --from 2024-04-01 --until 2024-04-03
    python scripts/probe.py --limit 300
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ndl_api import count_speeches, iter_speeches  # noqa: E402

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

# 名寄せの可否を左右するフィールド。欠損率を必ず見る。
KEY_FIELDS = ["speaker", "speakerYomi", "speakerGroup", "speakerPosition", "speakerRole"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="date_from", default="2025-06-02", help="開会日付/始点")
    parser.add_argument("--until", dest="date_until", default="2025-06-06", help="開会日付/終点")
    parser.add_argument("--limit", type=int, default=500, help="取得する発言数の上限")
    return parser.parse_args()


def summarize(records: list[dict]) -> None:
    print(f"\n{'=' * 60}")
    print(f"取得レコード数: {len(records):,}")
    print("=" * 60)

    if not records:
        return

    all_keys = sorted({k for r in records for k in r})
    print("\n--- フィールド一覧と欠損率 ---")
    for key in all_keys:
        filled = sum(1 for r in records if r.get(key) not in (None, "", []))
        missing_pct = 100 * (1 - filled / len(records))
        mark = " ★" if key in KEY_FIELDS else ""
        print(f"  {key:<22} 欠損 {missing_pct:5.1f}%{mark}")

    print("\n--- サンプル1件（本文は先頭120字） ---")
    sample = dict(records[0])
    if isinstance(sample.get("speech"), str):
        sample["speech"] = sample["speech"][:120] + " …"
    print(json.dumps(sample, ensure_ascii=False, indent=2))

    print("\n--- speakerGroup（会派）の出現 ---")
    for value, count in Counter(r.get("speakerGroup") or "(空)" for r in records).most_common(20):
        print(f"  {count:>5}  {value}")

    print("\n--- speakerPosition（肩書き）の出現 上位15 ---")
    for value, count in Counter(r.get("speakerPosition") or "(空)" for r in records).most_common(15):
        print(f"  {count:>5}  {value}")

    print("\n--- 発言者名 上位15 ---")
    for value, count in Counter(r.get("speaker") or "(空)" for r in records).most_common(15):
        print(f"  {count:>5}  {value}")

    speakers = {r.get("speaker") for r in records if r.get("speaker")}
    print(f"\nユニーク発言者数: {len(speakers):,}")

    # 名寄せの障害になりそうな表記を早めに見ておく
    suspicious = sorted(s for s in speakers if " " in s or "　" in s)
    if suspicious:
        print(f"\n--- 空白を含む発言者名（表記ゆれ候補 {len(suspicious)}件） ---")
        for name in suspicious[:20]:
            print(f"  {name!r}")

    meetings = Counter(f"{r.get('nameOfHouse')} {r.get('nameOfMeeting')}" for r in records)
    print(f"\n--- 会議 {len(meetings)}種 ---")
    for value, count in meetings.most_common(10):
        print(f"  {count:>5}  {value}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    args = parse_args()

    search = {"from": args.date_from, "until": args.date_until}

    total = count_speeches(**search)
    print(f"{args.date_from} 〜 {args.date_until} の該当発言数: {total:,}")
    if total == 0:
        print("この期間は0件。国会が開いていない期間かもしれない。--from/--until を変えて再実行。")
        return

    records = list(iter_speeches(limit=args.limit, **search))

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RAW_DIR / f"probe_{args.date_from}_{args.date_until}.json"
    out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    summarize(records)
    print(f"\n生JSONを保存: {out_path}")
    print(f"（この期間の全 {total:,} 件のうち {len(records):,} 件を取得）")


if __name__ == "__main__":
    main()
