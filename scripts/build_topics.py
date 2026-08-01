"""Phase 1 §3.2: 争点語の集計を作る。

これは2つの問題を同時に解く。

1. **2文字以下の語が全文検索で引けない**（`docs/PHASE0_FINDINGS.md` §3）。
   FTS5 の trigram は3文字未満のトークンを作れないので、
   「増税」「憲法」「年金」「原発」が0件になる。
2. **任意語のFTS検索は1.5〜6秒かかる**（`docs/PHASE1_PROTOTYPE.md` §4）。
   よく引かれる語を事前に集計しておけば、主要な導線はFTSを通らずに済む。

運営が管理する有限個の争点語について、発言との対応を先に作っておく。
語のリストは `data/topics.json`（**手で管理する。コミットする**）。

## 出力

    data/dist/topics.json      全期間の月次集計。1リクエストで読める大きさに保つ
                               → 頻度推移ページはDBを引かずにこれだけで描ける
    data/dist/trending.json    直近の国会で急に増えた語。検索の入口に使う（10KB）
    reports/trending_new_terms.md  トレンドに出たが topics.json に無い語（運営用）

年ごとDBの `topic` / `topic_hit` は `build_db.py` が作る（本文を持っているのはあちら）。

## 週次トレンドが実際に拾うもの

実測すると、上位に来るのは**その週に審議された法案の専門用語**（`育成者権` `事理弁識能力`
`二次使用料`）で、いわゆる「争点」ではない。争点らしい語（`副首都` `皇室典範`）も出るが、
`data/topics.json` に載っている語は**160語中6語しかなかった**。

つまりこれは「**いま国会で議論されていること**」であって「今週の争点」ではない。
表示するときの言葉を誇張しないこと。検索の入口としては有用で、
そうでなければ辿り着けない審議に導線ができる。

## 語の候補出し

`--propose` を付けると、会議録から候補語を抽出して `reports/topic_candidates.md` に出す。
外部ソース（報道・SNS）は使わない — 会議録だけで十分に機能することは
`docs/PHASE0_FINDINGS.md` §10 で実証済み。

**候補はあくまで候補。何を争点語にするかは運営の判断**なので、自動では採用しない。

使い方:
    python scripts/build_topics.py --propose     # 候補を出す（リストは自分で決める）
    python scripts/build_topics.py               # data/topics.json から集計を作る
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "kokkai.db"
TOPICS_PATH = ROOT / "data" / "topics.json"
DENYLIST_PATH = ROOT / "data" / "topic_denylist.json"
DIST_DIR = ROOT / "data" / "dist"

logger = logging.getLogger("topics")

# 漢字の並びとカタカナの並びを語の候補として拾う。形態素解析は入れない
# （依存パッケージを増やさない方針。候補出しは人がレビューする前提なので精度は要らない）
RUN_PATTERN = re.compile(r"[一-鿿々]{2,10}|[ァ-ヴー]{2,10}")

# 会議録の定型。候補語として上位に来るが争点ではない
STOPWORDS = {
    "委員長", "委員会", "理事会", "本会議", "質疑", "答弁", "政府参考人", "国務大臣",
    "内閣総理大臣", "参考人", "衆議院", "参議院", "国会", "議員", "議長", "先生",
    "質問", "発言", "審議", "採決", "起立", "異議", "動議", "速記", "休憩", "散会",
    "予算委員会", "理事", "委員", "大臣", "政府", "総理", "皆様", "皆さん", "先ほど",
    "今回", "今後", "現在", "一つ", "二つ", "非常", "本当", "自分", "我々", "以上",
    "以下", "場合", "必要", "実際", "内容", "状況", "問題", "課題", "対応", "検討",
    "議案", "法案", "提出", "可決", "修正", "附帯決議", "報告", "説明", "資料",
    # 会期の終わりに跳ねる運営の語。週次トレンドで上位を占めるが中身が無い
    "閉会中", "閉会中審査", "委員派遣", "全体会議", "連合審査", "分科会", "継続審査",
}

# 「高市総理」「坂本委員長」のような 人名+役職 が候補の上位を埋めるので落とす。
# 発言者名の部分一致だけでは拾えない（名前の後ろに役職が付いた形は別の語になる）
ROLE_SUFFIXES = (
    "内閣総理大臣", "国務大臣", "副大臣", "政務官", "委員長", "参考人",
    "総理", "内閣", "政権", "大臣", "委員", "議員", "先生", "知事", "長官",
    "局長", "次官",
)
# ★ 姓が1文字だと上の「姓＋役職」判定をすり抜ける（「簗議員」が実際に漏れた）。
#   争点語がこれらで終わることはまず無いので、姓の照合を待たずに落とす。
#   「地方議員」「都道府県知事」のような語も巻き添えで落ちるが、
#   **議員名がトップページに出るリスクのほうが高くつく**ので、こちらを優先する。
ROLE_ENDINGS = ("議員", "委員", "大臣", "総理", "知事", "長官", "局長", "次官",
                "先生", "参考人", "政務官", "会長", "議長")
# 語のどこかにこれを含むなら役職名。争点語にはならない
# 「菅内閣総理大臣」「枝野会長」のように、姓が1文字だったり役職が珍しかったりして
# 上の判定をすり抜けるものがある。役職名を含む語は争点語にならないので丸ごと落とす
ROLE_CONTAINS = ("委員長", "国務大臣", "副大臣", "政務官", "政府参考人", "内閣官房長官",
                 "内閣総理大臣", "会長", "副議長")
# 「令和八年度予算」のような年度もの。中身は毎年変わるので争点語として持たない
ERA_PATTERN = re.compile(r"(令和|平成)[一二三四五六七八九十元\d]+年")


# --- 語の候補出し -----------------------------------------------------------

def make_word_filter(con: sqlite3.Connection, denylist: Path | None = None):
    """語として採用してよいかの判定を作る。候補出しと週次トレンドで共有する。

    発言者名は**議員に限らず全員**を材料にする（参考人・政府参考人の名前も落とすため）。
    """
    speakers = {row[0] for row in con.execute("SELECT DISTINCT speaker FROM speech")}
    # 議員名の断片が語として混ざるので弾く（PHASE0_FINDINGS §10 の後処理2）
    name_parts = {n[i:j] for n in speakers
                  for i in range(len(n)) for j in range(i + 2, len(n) + 1)}
    # 姓（名前の先頭2〜4文字）。「高市」+「総理」のような合成を落とすのに使う
    surnames = {n[:k] for n in speakers for k in (2, 3, 4) if len(n) >= k}

    denied: set[str] = set()
    if denylist and denylist.exists():
        denied = set(json.loads(denylist.read_text(encoding="utf-8"))["terms"])

    def is_person_with_role(term: str) -> bool:
        for role in ROLE_SUFFIXES:
            head = term[:-len(role)]
            if term.endswith(role) and head and (head in surnames or head in name_parts):
                return True
        return False

    def keep(term: str) -> bool:
        return (term not in STOPWORDS
                and term not in denied
                and term not in name_parts
                and not is_person_with_role(term)
                and not any(role in term for role in ROLE_CONTAINS)
                # 会議録は発言者を「〜君」と呼ぶ。政策語がこれで終わることはない
                and not term.endswith(("君", "氏"))
                and not term.endswith(ROLE_ENDINGS)
                and not ERA_PATTERN.search(term))

    return keep


def propose(con: sqlite3.Connection, out: Path, limit: int, denylist: Path) -> None:
    """会議録から候補語を抽出する。判断材料を出すだけで、採用はしない。"""
    keep_word = make_word_filter(con, denylist)

    total: Counter[str] = Counter()
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    n_rows = 0
    for date, body in con.execute(
            "SELECT date, body FROM speech WHERE speaker_kind='議員'"):
        n_rows += 1
        year = date[:4]
        for term in RUN_PATTERN.findall(body):
            total[term] += 1
            by_year[year][term] += 1
    logger.info("走査 %s件 / 異なり語 %s", f"{n_rows:,}", f"{len(total):,}")

    candidates = [t for t in total if total[t] >= 200 and keep_word(t)]
    years = sorted(by_year)
    recent = years[-1]
    # 年ごとに発言数が違う（2026年は7月まで）ので、割合に直さないと比較にならない
    volume = {y: sum(by_year[y].values()) for y in years}

    def rate(term: str, year: str) -> float:
        return by_year[year][term] / volume[year] * 1e6  # 100万語あたり

    def profile(term: str) -> list[float]:
        return [rate(term, y) for y in years]

    def peak_year(term: str) -> str:
        return max(years, key=lambda y: rate(term, y))

    # ★ 両端（最初の年と最後の年）の比較にしない。
    #   途中で山を作って収束した争点が丸ごと見えなくなる。実測（全期間）:
    #     裏金       両端比 2.4 / ピーク比 12.4（2024年）
    #     マイナンバー  両端比 0.2 / ピーク比  4.6（2023年）
    #     暫定税率    両端比 7.4 / ピーク比 15.2（2025年）
    #   サイトは2021年以降を通して引けるので、どの時期の争点も拾える必要がある。
    def burst(term: str) -> float:
        rates = sorted(profile(term))
        median = rates[len(rates) // 2]
        return (max(rates) + 0.1) / (median + 0.1)

    # 「今の争点」は直近年が平常時からどれだけ跳ねているかで見る。
    # 最初の年を基準にすると、その年がたまたま高いか低いかに引きずられる
    def recency(term: str) -> float:
        rates = sorted(profile(term))
        median = rates[len(rates) // 2]
        return (rate(term, recent) + 0.1) / (median + 0.1)

    def table(terms: list[str], header: str, note: str, key) -> list[str]:
        rows = [header, "", note, "",
                "| 語 | " + " | ".join(f"{y}年" for y in years) + " | 倍率 | ピーク | 文字数 | FTS |",
                "|---|" + "---:|" * len(years) + "---:|---|---:|---|"]
        for term in terms:
            fts = "✅" if len(term) >= 3 else "**❌**"
            cells = " | ".join(f"{r:,.0f}" for r in profile(term))
            rows.append(f"| {term} | {cells} | {key(term):.1f}倍 | {peak_year(term)} | "
                        f"{len(term)} | {fts} |")
        return rows + [""]

    # 争点は「頻度が高い語」ではなく「跳ねた語」に出る（PHASE0_FINDINGS §10）。
    # 頻度上位は「日本」「議論」「重要」のような一般語で埋まって使えない。
    # ピーク年の実数にも下限を置く。総数だけだと薄く広がった語が混ざる
    burstable = [t for t in candidates if by_year[peak_year(t)][t] >= 100]
    bursty = sorted(burstable, key=burst, reverse=True)[:100]
    current = sorted([t for t in burstable if peak_year(t) == recent],
                     key=recency, reverse=True)[:60]
    frequent = sorted(candidates, key=lambda t: -total[t])[:limit]

    lines = [
        "# 争点語の候補",
        "",
        f"- 走査: 議員の発言 {n_rows:,}件 / 異なり語 {len(total):,} / 候補 {len(candidates):,}",
        f"- 除外: 定型語 {len(STOPWORDS)}件、議員名の断片、出現200回未満、ピーク年100回未満",
        "- 表の数値は**100万語あたりの出現率**。年ごとに発言数が違うので実数では比べられない",
        "",
        "**これは候補であって争点語リストではない。** 何を争点として扱うかは運営の判断なので、",
        "ここから選んで `data/topics.json` に書くこと。自動では採用しない。",
        "",
        "> 抽出は漢字の並び・カタカナの並びを拾っているだけなので、送り仮名が落ちる",
        "> （`賃上げ` → `賃上`、`見直し` → `見直`）。`data/topics.json` には正しい形で書くこと。",
        "",
    ]
    lines += table(
        bursty, "## どこかの時期に突出した語 ★ここが争点",
        "ピーク年の出現率 ÷ 中央値の年の出現率。**山がいつ立ったかを問わない**ので、"
        "2022年の統一教会や2024年の裏金のように、途中で山を作って収束した争点も拾える。",
        burst)
    lines += table(
        current, f"## いま争点になっている語（ピークが{recent}年）",
        f"{recent}年の出現率 ÷ 中央値の年の出現率。上の表のうち山が直近に立っているもの。",
        recency)
    lines += [
        f"## 出現の多い語（上位{len(frequent)}・参考）",
        "",
        "一般語で埋まるので争点探しには向かない。表記の確認用。",
        "",
        "| 語 | 出現 | 文字数 | FTS |",
        "|---|---:|---:|---|",
    ]
    for term in frequent:
        lines.append(f"| {term} | {total[term]:,} | {len(term)} | "
                     f"{'✅' if len(term) >= 3 else '**❌**'} |")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("候補を保存: %s（突出した語 %d / 直近の争点 %d）", out, len(bursty), len(current))


# --- 週次トレンド -----------------------------------------------------------

def build_trending(con: sqlite3.Connection, topics: list[dict], *,
                   sitting_days: int, windows: int, top: int,
                   min_hits: int, denylist: Path) -> dict:
    """直近の国会で急に増えた語を出す。検索の入口に使う。

    **カレンダー週で区切らない。** 国会は通年で開いていないので、実測では直近8週のうち
    2週が「開催2日・2,273語」「開催4日・6,854語」で集計にならなかった。
    代わりに**発言のあった日をN日ずつ**まとめる（会期中はほぼ1週間に相当する）。

    急上昇の判定は「その窓での出現率 ÷ 全期間の出現率」。
    件数そのものだと「医療」「教育」のような常時多い語が並んで意味が無い。
    """
    keep_word = make_word_filter(con, denylist)
    topic_id = {t["term"]: t["id"] for t in topics}

    base: Counter[str] = Counter()
    by_date: dict[str, Counter[str]] = defaultdict(Counter)
    speeches_by_date: Counter[str] = Counter()
    for date, body in con.execute(
            "SELECT date, body FROM speech WHERE speaker_kind='議員'"):
        terms = RUN_PATTERN.findall(body)
        base.update(terms)
        by_date[date].update(terms)
        speeches_by_date[date] += 1

    base_total = sum(base.values())
    dates = sorted(by_date, reverse=True)
    out_windows = []

    for w in range(windows):
        chunk = dates[w * sitting_days:(w + 1) * sitting_days]
        if not chunk:
            break
        counts: Counter[str] = Counter()
        for d in chunk:
            counts.update(by_date[d])
        total = sum(counts.values())
        if not total:
            continue

        def lift(term: str) -> float:
            return (counts[term] / total) / ((base[term] + 1) / base_total)

        ranked = sorted((t for t, n in counts.items() if n >= min_hits and keep_word(t)),
                        key=lift, reverse=True)[:top]
        out_windows.append({
            "from": min(chunk), "until": max(chunk),
            "sitting_days": len(chunk),
            "n_speeches": sum(speeches_by_date[d] for d in chunk),
            "terms": [{
                "term": t, "n": counts[t], "lift": round(lift(t), 1),
                # 争点語リストに載っている語は頻度推移ページへ直接つなげる
                "topic_id": topic_id.get(t),
            } for t in ranked],
        })

    return {"through": dates[0] if dates else None,
            "sitting_days_per_window": sitting_days,
            "windows": out_windows}


def write_new_terms(trending: dict, topics: list[dict], out: Path) -> int:
    """トレンドに出たが `data/topics.json` に無い語。運営が採用を判断するための一覧。"""
    known = {t["term"] for t in topics}
    seen: dict[str, dict] = {}
    for window in trending["windows"]:
        for term in window["terms"]:
            if term["term"] in known:
                continue
            entry = seen.setdefault(term["term"], {"n": 0, "lift": 0, "weeks": []})
            entry["n"] += term["n"]
            entry["lift"] = max(entry["lift"], term["lift"])
            entry["weeks"].append(window["from"])

    lines = [
        "# 直近のトレンドに出た新語",
        "",
        f"- 対象: 直近 {len(trending['windows'])}窓 "
        f"（{trending['sitting_days_per_window']}開催日ずつ / 〜{trending['through']}）",
        f"- `data/topics.json` に無い語: **{len(seen)}件**",
        "",
        "争点として扱うなら `data/topics.json` に足す。足せば頻度推移も検索も速くなる。",
        "**足さなくても壊れない**（トレンドには出るが、検索はFTS経由になる）。",
        "",
        "| 語 | 出現 | 最大の急上昇度 | 出た窓 |",
        "|---|---:|---:|---|",
    ]
    for term, e in sorted(seen.items(), key=lambda kv: -kv[1]["lift"]):
        lines.append(f"| {term} | {e['n']:,} | {e['lift']:.0f}倍 | {' / '.join(e['weeks'])} |")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(seen)


# --- 集計 -------------------------------------------------------------------

def load_topics(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(
            f"{path} が無い。先に `python scripts/build_topics.py --propose` で候補を出し、"
            f"採用する語を書くこと")
    topics = json.loads(path.read_text(encoding="utf-8"))["topics"]
    for i, topic in enumerate(topics, 1):
        topic.setdefault("id", i)
        topic.setdefault("variants", [])
        topic.setdefault("category", None)
    warn_overlaps(topics)
    return topics


def warn_overlaps(topics: list[dict]) -> None:
    """同じ語の中で表記ゆれが互いに部分文字列だと二重に数えてしまう。"""
    for topic in topics:
        forms = [topic["term"], *topic["variants"]]
        for a in forms:
            for b in forms:
                if a != b and a in b:
                    logger.warning("「%s」の表記ゆれ %r が %r に含まれる。二重に数える",
                                   topic["term"], a, b)


def count_hits(body: str, forms: list[str]) -> int:
    """本文に含まれる出現回数。表記ゆれは合算する。"""
    return sum(body.count(form) for form in forms)


def aggregate(con: sqlite3.Connection, topics: list[dict],
              min_kaiha_speeches: int) -> dict:
    """全期間の月次集計。頻度推移ページはこれだけで描ける。

    政党ではなく**会派**で集計する。`affiliation.party` は統一会派に欠損が偏るので、
    政党別に出すと特定の政党だけ少なく見える（`scripts/build_politicians.py`）。
    """
    forms = {t["id"]: [t["term"], *t["variants"]] for t in topics}

    months: set[str] = set()
    kaiha_volume: Counter[str] = Counter()
    # (topic_id, month, kaiha) → [その語を含む発言数, 延べ出現回数]
    cells: dict[tuple[int, str, str], list[int]] = defaultdict(lambda: [0, 0])
    # 分母。国会は通年で開いていないので、月によって発言数が桁で違う。
    # これを見せずに件数だけグラフにすると「開催日数が多い月」が争点に見える
    denom: Counter[tuple[str, str]] = Counter()

    n_rows = 0
    for date, kaiha, body in con.execute(
            "SELECT date, COALESCE(speaker_group, ''), body FROM speech"
            " WHERE speaker_kind='議員'"):
        n_rows += 1
        month = date[:7]
        months.add(month)
        kaiha_volume[kaiha] += 1
        denom[(month, "")] += 1
        denom[(month, kaiha)] += 1
        for topic_id, form_list in forms.items():
            n = count_hits(body, form_list)
            if not n:
                continue
            for key in ((topic_id, month, ""), (topic_id, month, kaiha)):
                cell = cells[key]
                cell[0] += 1
                cell[1] += n

    # 発言数の少ない会派まで持つとファイルが膨らむ割に読めるグラフにならない
    kept = {k for k, v in kaiha_volume.items() if k and v >= min_kaiha_speeches}
    month_list = sorted(months)
    index = {m: i for i, m in enumerate(month_list)}

    series: dict[str, dict[str, list[int]]] = {}
    for (topic_id, month, kaiha), (n_speeches, _) in cells.items():
        if kaiha and kaiha not in kept:
            continue
        row = series.setdefault(str(topic_id), {}).setdefault(
            kaiha or "*", [0] * len(month_list))
        row[index[month]] = n_speeches

    totals = {t["id"]: [0, 0] for t in topics}
    for (topic_id, _, kaiha), (n_speeches, occurrences) in cells.items():
        if kaiha:
            continue
        totals[topic_id][0] += n_speeches
        totals[topic_id][1] += occurrences

    logger.info("走査 %s件 / 月 %s / 会派 %s（%s件以上）",
                f"{n_rows:,}", len(month_list), len(kept), f"{min_kaiha_speeches:,}")

    return {
        "months": month_list,
        # "*" は全会派の合計
        "kaiha": sorted(kept, key=lambda k: -kaiha_volume[k]),
        # 分母（その月の議員の発言数）。**グラフはこれで割ってから描くこと。**
        # 割らないと、国会が長く開かれた月ほど争点に見える
        "speech_totals": {
            key: [denom[(m, "" if key == "*" else key)] for m in month_list]
            for key in ["*", *kept]
        },
        "topics": [{
            "id": t["id"], "term": t["term"], "category": t["category"],
            "variants": t["variants"],
            "n_speeches": totals[t["id"]][0], "n_occurrences": totals[t["id"]][1],
        } for t in topics],
        "series": series,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--topics", type=Path, default=TOPICS_PATH)
    parser.add_argument("--out", type=Path, default=DIST_DIR / "topics.json")
    parser.add_argument("--propose", action="store_true", help="候補語を抽出して終了する")
    parser.add_argument("--propose-limit", type=int, default=400)
    parser.add_argument("--min-kaiha-speeches", type=int, default=2000,
                        help="この件数未満の会派は月次の内訳に含めない")
    parser.add_argument("--denylist", type=Path, default=DENYLIST_PATH,
                        help="トレンド・候補から外す語。事故ったらここに1行足す")
    parser.add_argument("--sitting-days", type=int, default=5,
                        help="トレンドの1窓に入れる開催日数（会期中のほぼ1週間）")
    parser.add_argument("--trend-windows", type=int, default=8)
    parser.add_argument("--trend-top", type=int, default=20)
    parser.add_argument("--trend-min-hits", type=int, default=15)
    args = parser.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    if args.propose:
        propose(con, ROOT / "reports" / "topic_candidates.md", args.propose_limit,
                args.denylist)
        return

    topics = load_topics(args.topics)
    logger.info("争点語 %s件", len(topics))

    trending = build_trending(con, topics, sitting_days=args.sitting_days,
                              windows=args.trend_windows, top=args.trend_top,
                              min_hits=args.trend_min_hits, denylist=args.denylist)
    trend_path = args.out.with_name("trending.json")
    trend_path.parent.mkdir(parents=True, exist_ok=True)
    trend_path.write_text(json.dumps(trending, ensure_ascii=False, separators=(",", ":")) + "\n",
                          encoding="utf-8")
    n_new = write_new_terms(trending, topics, ROOT / "reports" / "trending_new_terms.md")
    logger.info("トレンドを保存: %s（%d窓 / %.0f KB / 未登録の新語 %d件）",
                trend_path.name, len(trending["windows"]),
                trend_path.stat().st_size / 1024, n_new)

    data = aggregate(con, topics, args.min_kaiha_speeches)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n",
                        encoding="utf-8")
    size = args.out.stat().st_size
    logger.info("集計を保存: %s（%.0f KB）", args.out, size / 1024)

    print(f"\n{'語':<16}{'含む発言':>10}{'延べ出現':>10}  文字数")
    for t in sorted(data["topics"], key=lambda x: -x["n_speeches"]):
        mark = "" if len(t["term"]) >= 3 else "  ← FTSでは引けない"
        print(f"{t['term']:<16}{t['n_speeches']:>10,}{t['n_occurrences']:>10,}"
              f"{len(t['term']):>6}{mark}")


if __name__ == "__main__":
    main()
