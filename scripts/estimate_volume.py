"""Phase 0 ステップ1b: 年ごとの発言件数を数え、バックフィルの所要時間とDBサイズを見積もる。

件数だけなら maximumRecords=1 の1リクエストで取れるので、年あたり3秒で済む。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ndl_api import MAX_RECORDS, REQUEST_INTERVAL_SEC, count_speeches  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2012)
    parser.add_argument("--end-year", type=int, default=2025)
    args = parser.parse_args()

    print(f"{'年':>6}  {'発言数':>12}")
    print("-" * 22)

    total = 0
    per_year: dict[int, int] = {}
    for year in range(args.start_year, args.end_year + 1):
        count = count_speeches(**{"from": f"{year}-01-01", "until": f"{year}-12-31"})
        per_year[year] = count
        total += count
        print(f"{year:>6}  {count:>12,}")

    print("-" * 22)
    print(f"{'合計':>6}  {total:>12,}")

    for label, years in (("直近5年", 5), ("全期間(指定範囲)", None)):
        if years is None:
            subtotal = total
        else:
            target = sorted(per_year)[-years:]
            subtotal = sum(per_year[y] for y in target)

        requests = -(-subtotal // MAX_RECORDS["speech"])  # 切り上げ
        hours = requests * REQUEST_INTERVAL_SEC / 3600
        # 1発言あたりの本文はおおむね数百バイト〜1KB。保守的に 1.2KB で見積もる
        raw_mb = subtotal * 1.2 / 1024
        print(
            f"\n{label}: {subtotal:,}件 / {requests:,}リクエスト "
            f"/ 約{hours:.1f}時間 / 本文だけで約{raw_mb:.0f}MB"
        )


if __name__ == "__main__":
    main()
