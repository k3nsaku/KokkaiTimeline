"""議員ごとの発言数の推移を作る（`data/dist/politician_activity.json`）。

## 何を出して、何を出さないか

出すのは**発言の数だけ**で、中身には触れない。並べるのも**その議員のページの中**だけで、
**議員をまたいだ順位は作らない**（[docs/SCOPE.md](../docs/SCOPE.md)）。

発言数は委員会の所属と役職で決まるものなので、多い＝よく働いている、ではない。
**ページ側にもそう書くこと。** グラフに出すのは「いつ、どの委員会で発言したか」であって、
活動量の評価ではない。

## 月の並びは全期間で固定する

その議員の発言がある月だけに詰めない。**サイトが持っている全期間**（2021年1月〜）を
横軸にすると、**いつ発言が始まり、いつ止まったか**が見える。当選・落選・大臣就任で
発言が途切れるのは会議録から読み取れる事実で、詰めてしまうと消える。

## 委員会は上位N＋「その他」

5年で20以上の委員会に出ている議員がいる。全部を積み上げると色が足りず読めないので、
発言数の多い順に `--top-committees` 件だけ残し、残りは「その他」にまとめる。
**「その他」は色相を与えない**（identity ではないので、灰にする）。

出力は疎な形（`[月の添字, 件数]` の並び）。密に持つと 1,111人 × 64月 × 8系列 になる。

## ★ 実行の順番

**`build_politicians.py` のあとに `build_db.py` を回した単一DB**が要る。
`politician_id` は「`politicians.json` があるときに `build_db` を回した」ときだけ入る
（`build_db.py` の `load_politicians()`）。入っていなければその場で落とす。

年DBを読めば必ず入っているが、**日次のランナーには過去年の年DBが無い**
（2GBあるので落としていない）。だから単一DBを使う。

使い方:
    python scripts/build_activity.py
    python scripts/build_activity.py --top-committees 5
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "kokkai.db"
DIST_DIR = ROOT / "data" / "dist"

logger = logging.getLogger("activity")

OTHER = "その他"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DIST_DIR / "politician_activity.json")
    parser.add_argument("--top-committees", type=int, default=7,
                        help="議員ごとに色を与える委員会の数（残りは「その他」）")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"{args.db} が無い。先に build_db.py を実行すること")

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    started = time.monotonic()

    # ★ 単一DBの politician_id は「politicians.json があるときに build_db を回した」
    #   ときだけ入る（build_db.py の load_politicians）。無い状態で通すと**黙って
    #   空のファイルができる**ので、ここで落とす。
    #
    #   年DBを読めば必ず入っているが、**日次のランナーには過去年の年DBが無い**
    #   （2GBあるので落としていない）。だから単一DBを使う。
    if not con.execute(
            "SELECT EXISTS(SELECT 1 FROM speech WHERE politician_id IS NOT NULL)").fetchone()[0]:
        raise SystemExit(
            f"{args.db.name} に politician_id が入っていない。\n"
            "  build_politicians.py を実行してから build_db.py を回し直すこと\n"
            "  （politicians.json が無い状態で作った単一DBは politician_id が NULL）。")

    # (議員, 月, 会議名) → 発言数
    cells: dict[int, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    months: set[str] = set()
    n = 0
    for politician_id, date, meeting in con.execute(
            "SELECT s.politician_id, s.date, m.name FROM speech s"
            " JOIN meeting m ON m.issue_id = s.issue_id"
            " WHERE s.speaker_kind = '議員' AND s.politician_id IS NOT NULL"):
        n += 1
        month = date[:7]
        months.add(month)
        cells[politician_id][meeting][month] += 1

    # ★ 全期間。**その議員が発言した月だけに詰めない**（冒頭の注記）
    month_list = sorted(months)
    index = {m: i for i, m in enumerate(month_list)}
    logger.info("走査 %s件 / 議員 %s / 月 %s（%.0f秒）",
                f"{n:,}", f"{len(cells):,}", len(month_list), time.monotonic() - started)

    out: dict[str, dict] = {}
    n_folded = 0
    for politician_id, by_meeting in cells.items():
        ranked = sorted(by_meeting.items(), key=lambda kv: -sum(kv[1].values()))
        kept = ranked[:args.top_committees]
        rest = ranked[args.top_committees:]
        if rest:
            n_folded += 1
            merged: Counter[str] = Counter()
            for _, counts in rest:
                merged.update(counts)
            kept.append((OTHER, merged))

        out[str(politician_id)] = {
            "committees": [name for name, _ in kept],
            # 疎な形。密にすると 1,111人 × 64月 × 8系列 になる
            "series": [
                sorted([index[m], c] for m, c in counts.items())
                for _, counts in kept
            ],
        }

    data = {
        "_comment": [
            "議員ごとの発言数の推移。**発言の中身には触れていない。**",
            "月は全期間で固定（発言のある月だけに詰めない）。委員会は上位N＋その他。",
            "★議員をまたいだ順位を作らないこと（docs/SCOPE.md）。",
            "作り直すには scripts/build_activity.py を実行する。",
        ],
        "months": month_list,
        "params": {"top_committees": args.top_committees},
        "politicians": out,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
                        encoding="utf-8")
    logger.info("議員 %s件を保存: %s（%.1f MB）",
                f"{len(out):,}", args.out, args.out.stat().st_size / 1024**2)
    logger.info("「その他」にまとめた議員 %s件（上位%d委員会に収まらなかった）",
                f"{n_folded:,}", args.top_committees)


if __name__ == "__main__":
    main()
