"""頻出語レイヤーを作る（`data/dist/frequent.json`）。ROADMAP §1-C。

## `topic` とも `word` とも役割が違う（CLAUDE.md）

    topic     争点語82件。**運営の編集方針**。何を争点と呼ぶかは人が決める
    word      2文字語16,264件。**索引**。「その語を引けるか」を決めるだけで、一覧にしない
    frequent  ここ。**機械抽出の一覧**。語の側から「いつ・どれだけ議論されたか」を見せる

NDL は「語を入れたら発言が出る」まで。**語を並べて推移を見せる機能は無い**ので、
ここが差別化の本体になる。`data/topics.json` を膨らませて代用してはいけない
（あれは編集方針で、増やすほど「運営が選んだ争点」の意味が薄まる）。

## 数え方（★ ここを間違えると数字が合わない）

**選定は「自立した run」、集計は「部分文字列の発言数」。**
`build_words.py` が語彙の選定と索引の作成で数え方を変えているのと同じ理由:

- 選定を部分文字列にすると**断片**が入る（実測: `国務大`←国務大臣、`御異議`）
- 集計を run 単位にすると**検索結果と数が合わない**。検索は `word_hit` も FTS も
  部分文字列で当てるので、「グラフは1,200件なのに検索は300件」になる

## 並べ方

**頻度順では並べない。** 実測すると上位は `日本` `重要` `関係` `指摘` `是非` で、
読む価値のある一覧にならない（`build_topics.py` の `propose()` にも同じ注記がある）。
代わりに **burst（ピーク年の出現率 ÷ 中央値の年の出現率）** で並べる。
これで 2021年のコロナ・2022年の統一教会・2024年の裏金・2026年の中東が
それぞれの時期の語として出てくる。

## 出力

    data/dist/frequent.json    月×語・会期×語の発言数 + 分母（上位500語で約155KB）
    reports/frequent_words.md  採用した語の一覧（運営が目で見るため。gitignore）

会期ぶんを別に持っているのは、**会期が月境界で始まらない**ため
（第204回は 2021-01-18 開会で、同じ月に第203回が同居する）。
月の合算で代用すると境目の発言が隣の会期に混ざる。

**会派の内訳は持たない。** 実測で上位500語に `topics.json` と同じ密度で付けると
1.6MB になる（語だけなら88KB）。必要になったら**別ファイルに分けて**上位数十語だけ付ける。

**年DBの作り直しは要らない。** 検索は既存の `word_hit`（2文字）と FTS（3文字以上）に
流すので、このスクリプトが作るのは JSON だけ。

使い方:
    python scripts/build_frequent.py
    python scripts/build_frequent.py --top 300 --min-df 500
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

from build_topics import STOPWORDS, make_word_filter

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "kokkai.db"
WORDS_PATH = ROOT / "data" / "words.json"
TOPICS_PATH = ROOT / "data" / "topics.json"
DENYLIST_PATH = ROOT / "data" / "topic_denylist.json"
DIST_DIR = ROOT / "data" / "dist"

logger = logging.getLogger("frequent")

# ★ `build_db.py` の WORD_RUN_PATTERN と同じもの。**片方だけ変えないこと。**
# ここで数えた語が検索で引けなければ、一覧から飛んだ先が0件になる
RUN_PATTERN = re.compile(r"[一-鿿々]{2,}|[ァ-ヴー]{2,}|[Ａ-Ｚａ-ｚ０-９]{2,}")

# 全角ラテンの小文字→大文字。語彙（words.json）も索引（word_hit）もこの形で持っている
LATIN_FOLD = {c: c - 0x20 for c in range(0xFF41, 0xFF5B)}

# 2〜4文字だけを見る。5文字以上は候補が急に減るうえ、走査の費用が長さに比例して増える。
# 実測では上位500語の98%が4文字以下だった
LENGTHS = (2, 3, 4)

# 漢数字。`五年` `十二` のような語が頻度でも burst でも上位に来る
NUMERALS = set("一二三四五六七八九十百千万零〇")
# 数量・日付。**数字で始まって単位で終わる語は、語ではなく値**。
# 実測で上位に出たもの: `五月八日` `四日間` `十三兆円` `百三万円` `八年度`。
# 「百三万円の壁」のように争点そのものを指す値もあるが、それは運営が
# `data/topics.json` に書くべきもので、機械抽出の一覧に置く語ではない
QUANTITY = re.compile(r"^[一二三四五六七八九十百千万零〇元].*[年月日円兆億万時分間]")


def fold(run: str) -> str:
    """全角ラテンの並びだけ大文字に畳む（`build_words.py` の `fold()` と同じ）。"""
    return run.translate(LATIN_FOLD) if run[0] > "鿿" else run


def is_noise(term: str) -> bool:
    """会議録の定型と数。`make_word_filter` が見ないぶんをここで落とす。"""
    return (term in STOPWORDS
            or all(c in NUMERALS for c in term)
            or bool(QUANTITY.match(term))
            # 「御指摘」「御答弁」「御異議」。会議録の定型で、語としての中身が無い
            or term.startswith(("御", "ご")))


def latin_fragments(standalone_df: dict[str, int], df_total: Counter[str],
                    ratio: float) -> set[str]:
    """2文字の全角ラテンのうち、**長い略語の一部でしかないもの**。

    `words.json` は `ＡＩ` `ＤＸ` `Ｇ７` のために2文字の全角ラテンを語彙に入れている
    （3文字未満は FTS で原理的に引けないため）。その副作用で、`ＯＴＣ` から `ＴＣ`、
    `ＪＢＩＣ` から `ＢＩ` のような**略語の断片**が一覧に出る。

    見分けるのに使えるものが既にある: `words.json` の件数は**自立して出てくる発言数**、
    ここで数えたのは**部分文字列の発言数**。`ＡＩ` は 5,394 / 5,412 でほぼ一致するが、
    断片は自立してほとんど出てこないので比が小さくなる。
    """
    return {term for term in df_total
            if len(term) == 2 and term[0] >= "Ａ" and df_total[term]
            and standalone_df.get(term, 0) / df_total[term] < ratio}


def build_pool(con: sqlite3.Connection, words_path: Path,
               min_standalone: int) -> tuple[set[str], dict[str, int]]:
    """候補プール。**自立して出てくる語だけ**を入れる。

    2文字は `data/words.json` をそのまま使う。**別に作らないこと。**
    年DBの `word` 索引はこのファイルから作られているので、ここで独自に選ぶと
    「一覧には出るのに検索できない語」ができる。

    3〜4文字は走査して拾う（`words.json` は2文字しか持っていない）。
    プールが大きくても次の走査の費用は変わらない（本文の長さで決まる）。
    """
    if not words_path.exists():
        raise SystemExit(f"{words_path} が無い。先に `python scripts/build_words.py` を実行すること")
    # 件数（自立して出てくる発言数）も返す。2文字の略語の断片を見分けるのに使う
    standalone_df: dict[str, int] = json.loads(words_path.read_text(encoding="utf-8"))["words"]
    pool = set(standalone_df)
    n2 = len(pool)

    standalone: Counter[str] = Counter()
    for (body,) in con.execute("SELECT body FROM speech WHERE speaker_kind = '議員'"):
        standalone.update({fold(r) for r in RUN_PATTERN.findall(body) if len(r) in (3, 4)})
    pool |= {t for t, c in standalone.items() if c >= min_standalone}

    logger.info("候補プール %s（2文字 %s / 3〜4文字 %s）",
                f"{len(pool):,}", f"{n2:,}", f"{len(pool) - n2:,}")
    return pool, standalone_df


class Counted(NamedTuple):
    """走査の結果。月と会期の2つの軸で持つ。"""
    months: list[str]
    denom: Counter[str]
    by_month: dict[str, Counter[str]]
    df_total: Counter[str]
    #: 会期番号 → その会期の議員発言数
    session_denom: Counter[int]
    #: 会期番号 → 語 → 発言数
    by_session: dict[int, Counter[str]]
    #: 会期番号 → (開会日, 最終日)
    session_span: dict[int, tuple[str, str]]


def count(con: sqlite3.Connection, pool: set[str]) -> Counted:
    """月 × 語 と 会期 × 語 の発言数を数える。**部分文字列で数える**（検索と揃えるため）。

    ★ **会期を月の合算で代用しない。** 会期は月境界で始まらない
    （第204回は 2021-01-18 開会で、同じ月に第203回が同居する）。
    月を足し合わせると境目の発言が隣の会期に混ざるので、ここで直接数える。
    500語×19会期でも数十KBしか増えない。
    """
    by_len = {length: {t for t in pool if len(t) == length} for length in LENGTHS}
    months: set[str] = set()
    denom: Counter[str] = Counter()
    df_total: Counter[str] = Counter()
    by_month: dict[str, Counter[str]] = defaultdict(Counter)
    session_denom: Counter[int] = Counter()
    by_session: dict[int, Counter[str]] = defaultdict(Counter)
    session_span: dict[int, tuple[str, str]] = {}

    n = 0
    started = time.monotonic()
    for date, session, body in con.execute(
            "SELECT s.date, m.session, s.body FROM speech s"
            " JOIN meeting m ON m.issue_id = s.issue_id"
            " WHERE s.speaker_kind = '議員'"):
        n += 1
        month = date[:7]
        months.add(month)
        denom[month] += 1
        session_denom[session] += 1
        span = session_span.get(session)
        session_span[session] = ((min(span[0], date), max(span[1], date))
                                 if span else (date, date))
        found: set[str] = set()
        for run in RUN_PATTERN.findall(body):
            run = fold(run)
            for length in LENGTHS:
                vocab = by_len[length]
                for i in range(len(run) - length + 1):
                    term = run[i:i + length]
                    if term in vocab:
                        found.add(term)
        counts = by_month[month]
        session_counts = by_session[session]
        for term in found:
            df_total[term] += 1
            counts[term] += 1
            session_counts[term] += 1
        if n % 100_000 == 0:
            logger.info("  %s件（%.0f秒）", f"{n:,}", time.monotonic() - started)

    logger.info("走査 %s件 / 月 %s / 会期 %s（%.0f秒）",
                f"{n:,}", len(months), len(session_denom), time.monotonic() - started)
    return Counted(sorted(months), denom, by_month, df_total,
                   session_denom, by_session, session_span)


def drop_near_duplicates(terms: list[str], df: Counter[str], ratio: float,
                         window: int) -> list[str]:
    """短い語がほぼ長い語の一部でしかないなら落とす。

    実測で並んでいたもの: `変異`/`変異株`、`参政`/`参政党`、`無害`/`無害化`、
    `規正`/`規正法`、`鎮静`/`鎮静化`。**同じことを指す行が2つ並ぶと一覧が読めない。**

    `陽性`（1,248）と `陽性者`（651）のように短いほうが単独でも使われる語は残る。

    総当たりは上位 `window` 語だけにする（候補は8,000語あって二乗が効く）。
    長いほうは短いほうと出現がほぼ重なる＝推移も似るので、burst 順で近くに来る。
    """
    head = terms[:window]
    ranked = set(head)
    dropped = []
    for term in head:
        for other in ranked:
            if len(other) > len(term) and term in other and df[other] >= df[term] * ratio:
                dropped.append((term, other))
                break
    drop = {t for t, _ in dropped}
    if dropped:
        logger.info("近い重複を除外 %d件（例: %s）", len(dropped),
                    "、".join(f"{t}←{o}" for t, o in dropped[:5]))
    return [t for t in terms if t not in drop]


def select(df_total: Counter[str], by_month: dict[str, Counter[str]], months: list[str],
           denom: Counter[str], keep, excluded: set[str], *, min_df: int, top: int,
           dup_ratio: float) -> tuple[list[str], object]:
    """採用する語を決める。**頻度順では並べない**（冒頭の注記）。"""
    years = sorted({m[:4] for m in months})
    year_denom = {y: sum(denom[m] for m in months if m[:4] == y) for y in years}
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    for month in months:
        by_year[month[:4]].update(by_month[month])

    def burst(term: str) -> float:
        rates = sorted(by_year[y][term] / year_denom[y] * 1000 for y in years)
        median = rates[len(rates) // 2]
        return (rates[-1] + 0.1) / (median + 0.1)

    eligible = [t for t in df_total
                if df_total[t] >= min_df and t not in excluded and keep(t) and not is_noise(t)]
    logger.info("下限 %s件とフィルタを通ったもの %s", f"{min_df:,}", f"{len(eligible):,}")

    ranked = sorted(eligible, key=burst, reverse=True)
    # 重複を落としてから切る（先に切ると、落とした数だけ一覧が短くなる）
    return drop_near_duplicates(ranked, df_total, dup_ratio, top * 3)[:top], burst


def write_report(path: Path, terms: list[str], df_total: Counter[str], burst,
                 by_month: dict[str, Counter[str]], months: list[str],
                 denom: Counter[str], topic_id: dict[str, int]) -> None:
    """採用した語の一覧。**運営が目で見て、事故があれば denylist に足すため。**"""
    years = sorted({m[:4] for m in months})
    year_denom = {y: sum(denom[m] for m in months if m[:4] == y) for y in years}
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    for month in months:
        by_year[month[:4]].update(by_month[month])

    lines = [
        "# 頻出語レイヤーに採用した語",
        "",
        f"- {len(terms):,}語（burst 順）。数字は**発言数**（部分文字列で数えたもの）",
        f"- 年別は1,000発言あたり: {' / '.join(years)}",
        "",
        "**おかしな語が混ざっていたら `data/topic_denylist.json` に1行足す。**",
        "議員名・役職・年号は機械的に落としているが、語の途中に平仮名が入るものは",
        "断片として残る（`延防止`←まん延防止、`目詰`←目詰まり。どちらも denylist に入れてある）。",
        "",
        "| # | 語 | 発言数 | 山 | 年別 | 争点語 |",
        "|---:|---|---:|---:|---|---|",
    ]
    for i, term in enumerate(terms, 1):
        profile = " / ".join(f"{by_year[y][term] / year_denom[y] * 1000:.0f}" for y in years)
        lines.append(f"| {i} | {term} | {df_total[term]:,} | {burst(term):.1f}倍 | "
                     f"{profile} | {'✅' if term in topic_id else ''} |")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--words", type=Path, default=WORDS_PATH)
    parser.add_argument("--topics", type=Path, default=TOPICS_PATH)
    parser.add_argument("--denylist", type=Path, default=DENYLIST_PATH,
                        help="一覧から外す語。事故ったらここに1行足す")
    parser.add_argument("--out", type=Path, default=DIST_DIR / "frequent.json")
    parser.add_argument("--top", type=int, default=500,
                        help="一覧に載せる語数（既定500 ＝ 約90KB）")
    parser.add_argument("--min-df", type=int, default=300,
                        help="この発言数に満たない語は載せない")
    parser.add_argument("--min-standalone", type=int, default=50,
                        help="3〜4文字が自立して出てくる発言数の下限（候補プール）")
    parser.add_argument("--dup-ratio", type=float, default=0.75,
                        help="短い語の出現の何割が長い語の中なら落とすか")
    parser.add_argument("--latin-ratio", type=float, default=0.5,
                        help="2文字の全角ラテンが自立して出る割合の下限（略語の断片よけ）")
    parser.add_argument("--min-session-speeches", type=int, default=3000,
                        help="この議員発言数に満たない会期は絞り込みに出さない")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"{args.db} が無い。先に build_db.py を実行すること")

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    pool, standalone_df = build_pool(con, args.words, args.min_standalone)
    counted = count(con, pool)
    months, denom, by_month, df_total = (
        counted.months, counted.denom, counted.by_month, counted.df_total)
    keep = make_word_filter(con, args.denylist)
    fragments = latin_fragments(standalone_df, df_total, args.latin_ratio)
    logger.info("略語の断片を除外 %d件（%s）", len(fragments),
                "、".join(sorted(fragments, key=lambda t: -df_total[t])[:8]))
    terms, burst = select(df_total, by_month, months, denom, keep, fragments,
                          min_df=args.min_df, top=args.top, dup_ratio=args.dup_ratio)

    # 争点語に載っている語は、そちらのページへ寄せる（一覧に同じ語が2つ出ないように）
    topics = json.loads(args.topics.read_text(encoding="utf-8"))["topics"] \
        if args.topics.exists() else []
    topic_id: dict[str, int] = {}
    for i, topic in enumerate(topics, 1):
        topic_id.setdefault(topic["term"], i)
        for variant in topic.get("variants", []):
            topic_id.setdefault(variant, i)

    def peak_month(term: str) -> str:
        return max(months, key=lambda m: (by_month[m][term] / denom[m]) if denom[m] else 0)

    # 会期。**発言の少ない会期は載せない。** 第220回は議員発言が191件しかなく、
    # そこで語を並べても中身は特別国会の手続きだけになる（首班指名・議長選出）
    sessions = sorted(s for s, n in counted.session_denom.items()
                      if n >= args.min_session_speeches)
    dropped = sorted(set(counted.session_denom) - set(sessions))
    logger.info("会期 %d件を載せる（%s件未満は除外: %s）", len(sessions),
                f"{args.min_session_speeches:,}",
                "、".join(f"第{s}回" for s in dropped) or "なし")

    data = {
        "_comment": [
            "機械抽出の頻出語。**争点語（topics.json）とは別物で、運営の編集方針ではない。**",
            "数は「その語を含む発言の数」。部分文字列で数えてあり、検索結果と一致する。",
            "作り直すには scripts/build_frequent.py を実行する。",
        ],
        "months": months,
        # ★ 分母。**グラフはこれで割ってから描くこと**（割らないと開催日数の多い月が争点に見える）
        "speech_totals": [denom[m] for m in months],
        # 会期。**月の合算では代用できない**（会期は月境界で始まらない。count() の注記）。
        # 順序は words[].sessions の並びと一致させてある
        "sessions": [{
            "session": s,
            "from": counted.session_span[s][0],
            "until": counted.session_span[s][1],
            "n_speeches": counted.session_denom[s],
        } for s in sessions],
        "params": {"top": args.top, "min_df": args.min_df,
                   "min_standalone": args.min_standalone, "dup_ratio": args.dup_ratio,
                   "min_session_speeches": args.min_session_speeches},
        "words": [{
            "term": term,
            "n": df_total[term],
            "burst": round(burst(term), 1),
            "peak": peak_month(term),
            "topic_id": topic_id.get(term),
            "series": [by_month[m][term] for m in months],
            "sessions": [counted.by_session[s][term] for s in sessions],
        } for term in terms],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
                        encoding="utf-8")
    write_report(ROOT / "reports" / "frequent_words.md", terms, df_total, burst,
                 by_month, months, denom, topic_id)

    logger.info("頻出語 %s件を保存: %s（%.0f KB）",
                f"{len(terms):,}", args.out, args.out.stat().st_size / 1024)
    n_topic = sum(1 for t in terms if t in topic_id)
    logger.info("うち争点語にもあるもの %d件（そちらのページへ寄せる）", n_topic)


if __name__ == "__main__":
    main()
