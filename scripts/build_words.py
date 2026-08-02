"""2文字語の語彙を会議録から抽出する（`data/words.json`）。

## なぜ要るか

FTS5 の trigram は3文字未満のトークンを作れないので、**「増税」「憲法」「年金」
「原発」が検索結果0件になる**（`docs/PHASE0_FINDINGS.md`）。日本語の政治キーワードは
2文字が非常に多く、これは検索機能としてつらい。

`topic_hit` は79件の争点語についてこれを解いているが、争点語のリストは
**運営の編集方針そのもの**で、検索できる語を増やすために膨らませるものではない。
そこで機械抽出の語彙を**別に**持ち、`word` / `word_hit` として索引化する。

## 何を語彙に入れるか

漢字またはカタカナが**ちょうど2文字だけ連続する**箇所を「自立した2文字語」と見なし、
それが全期間で `--min-df` 件以上の発言に出てくるものを採る。形態素解析は入れない
（依存パッケージを増やさない方針）。

**索引を作るときは部分文字列で数える。** 「憲法改正」の中の「憲法」も引けないと
検索として意味がないため。語彙の選定（自立しているか）と索引の作成（部分文字列か）で
数え方が違うのは意図的。

## 争点語と違って人名を落とさない

争点語の候補は人がレビューして採用するリストなので、議員名が混ざると事故になる。
こちらは**一覧として表示しない**（「その2文字語を引けるか」を答えるためだけの語彙）ので、
「石破」「高市」が入っていても害はなく、むしろ引けたほうがよい。

使い方:
    python scripts/build_words.py                    # data/kokkai.db → data/words.json
    python scripts/build_words.py --min-df 20        # 語彙を絞る（索引が小さくなる）
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "kokkai.db"
WORDS_PATH = ROOT / "data" / "words.json"

logger = logging.getLogger("words")

# 漢字の並びとカタカナの並び。build_topics.py の RUN_PATTERN と揃えてあるが、
# こちらは長さの上限を置かない（長い連続の中の2文字窓も索引の対象になるため）
RUN_PATTERN = re.compile(r"[一-鿿々]{2,}|[ァ-ヴー]{2,}")

# 索引に入れても検索の役に立たない語。会議録の定型で、ほぼ全発言に出る。
# 落とすのは「引かれないから」ではなく、**1語で数万行を占めて索引を太らせるから**
TOO_COMMON = {
    "委員", "会議", "質問", "答弁", "発言", "審議", "採決", "起立", "異議", "動議",
    "速記", "休憩", "散会", "理事", "大臣", "総理", "議長", "議員", "先生", "参考",
    "以上", "以下", "場合", "必要", "実際", "内容", "本日", "本件", "今回", "今後",
    "現在", "一つ", "二つ", "非常", "本当", "自分", "我々", "皆様", "先ほど",
}


def extract_vocabulary(con: sqlite3.Connection, min_df: int) -> tuple[Counter[str], int]:
    """「ちょうど2文字で自立している」語の出現発言数を数える。

    ここでは**部分文字列を数えない**。「安全保障」から「安全」を切り出すと、
    語として自立していない断片まで語彙に入ってしまう。
    索引を作る側（build_db.py）が部分文字列で拾う。
    """
    df: Counter[str] = Counter()
    n = 0
    started = time.monotonic()

    for (body,) in con.execute("SELECT body FROM speech WHERE speaker_kind = '議員'"):
        n += 1
        # 1発言に何回出ても1件と数える（発言数ベース）
        df.update({run for run in RUN_PATTERN.findall(body) if len(run) == 2})
        if n % 50_000 == 0:
            logger.info("  %s件 / 語彙 %s（%.0f秒）",
                        f"{n:,}", f"{len(df):,}", time.monotonic() - started)

    for term in TOO_COMMON:
        df.pop(term, None)

    return Counter({t: c for t, c in df.items() if c >= min_df}), n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=WORDS_PATH)
    parser.add_argument("--min-df", type=int, default=5,
                        help="この件数以上の発言に自立して出てくる語だけを採る（既定 5）")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"{args.db} が無い。先に build_db.py を実行すること")

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    logger.info("語彙を抽出中（%s）…", args.db.name)
    df, n_speeches = extract_vocabulary(con, args.min_df)

    # **全期間から作る。** 年ごとに作ると、ある年だけ語彙に無くて引けない語ができる
    words = sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "_comment": [
            "2文字語の検索用の語彙。**機械抽出の生成物で、争点語（topics.json）とは別物。**",
            "争点語は運営の編集方針だが、こちらは『その2文字語を引けるか』を決めるだけ。",
            "作り直すには scripts/build_words.py を実行する。",
        ],
        "min_df": args.min_df,
        "source_speeches": n_speeches,
        "words": {term: count for term, count in words},
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    logger.info("語彙 %s件を保存: %s（%.0f KB）",
                f"{len(words):,}", args.out, args.out.stat().st_size / 1024)

    print(f"\n対象の発言: {n_speeches:,}件（議員のみ）")
    print(f"語彙: {len(words):,}件（自立して{args.min_df}件以上の発言に出るもの）")
    print(f"\n{'語':<8}{'自立して出る発言数':>20}")
    for term, count in words[:15]:
        print(f"{term:<8}{count:>20,}")
    print("  …")
    for term, count in words[-5:]:
        print(f"{term:<8}{count:>20,}")

    print("\n--- 動機になった語 ---")
    for term in ("憲法", "年金", "増税", "原発", "残業", "円安", "沖縄", "税制"):
        count = df.get(term)
        print(f"  {term}: {f'{count:,}件' if count else '★語彙に入らなかった'}")


if __name__ == "__main__":
    main()
