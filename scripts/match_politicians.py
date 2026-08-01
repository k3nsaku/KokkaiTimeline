"""Phase 0 ステップ5: 会議録の発言者と Wikidata の議員を突合し、名寄せレポートを出す。

Phase 0 の関門。ここでマッチ率と失敗パターンが見えれば、Phase 1 に進むか判断できる。

## 名寄せの単位

`(発言者名, 読み, 会派)` を1単位として突合する。`(発言者名, 読み)` だけだと
**同姓同名が1行にまとまってしまい、分離できなくなる**（例: 鬼木誠は衆院自民と参院立憲の2人）。
会派を単位に含めておけば、同一人物の政党移籍も自然に複数行として現れ、
そのまま `Affiliation` テーブルの材料になる。

## 突合の方針

  第一キー : speakerYomi（読み）を正規化して一致
  第二キー : 漢字表記を正規化して一致
  絞り込み : 候補が複数のとき、会派 → 院 → 任期 の順に信号を使う

会議録は「あべ俊子」のようなひらがな交じりの通称を使うため、
漢字表記だけでは突合できない。読みを軸にするのはそのため。

## 絞り込み信号の強さ（実測に基づく）

  会派 : ◎ 最も強い。会派↔政党(P102)の対応で同姓同名が分離できる
  院   : △ 弱い。大臣・副大臣は他院の委員会にも出るし「両院」の会議もある
         （役職なし発言から院を推定する案は 368人中11人が食い違い、不採用）
  任期 : △ Wikidata の P39 修飾子 P580 の付与率が 13.7% しかなく、ほとんど効かない

使い方:
    python scripts/match_politicians.py
    python scripts/match_politicians.py --report reports/name_matching.md
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "kokkai.db"
MEMBERS_PATH = ROOT / "data" / "raw" / "wikidata_members.json"
TERMS_PATH = ROOT / "data" / "raw" / "wikidata_terms.json"
PARTY_MAP_PATH = ROOT / "data" / "party_map.json"

logger = logging.getLogger("match")

# 読みの区切りに使われうる文字。会議録側は区切り無しで来る。
SEPARATORS = re.compile(r"[\s　・･,、.。]+")


def to_hiragana(text: str) -> str:
    """カタカナをひらがなに寄せる。Wikidata 側は表記が混在している。"""
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in text)


def norm_kana(text: str | None) -> str:
    """読みの正規化: NFKC → カタカナ→ひらがな → 区切り文字の除去。

    Wikidata の P1814 は 7,844人が 'みき ぶきち' のように空白入りで、
    29人がカタカナ表記。正規化しないと1件もマッチしない。
    """
    if not text:
        return ""
    return SEPARATORS.sub("", to_hiragana(unicodedata.normalize("NFKC", text))).strip()


def norm_name(text: str | None) -> str:
    """漢字表記の正規化: NFKC → 空白・区切り除去。"""
    if not text:
        return ""
    return SEPARATORS.sub("", unicodedata.normalize("NFKC", text)).strip()


def norm_party(text: str | None) -> str:
    """政党名の正規化。全角英字や表記ゆれを吸収する。"""
    if not text:
        return ""
    return SEPARATORS.sub("", unicodedata.normalize("NFKC", text)).strip()


def is_japanese(text: str | None) -> bool:
    """Wikidata のラベルが日本語か（ja ラベルが無いと英語で返る）。"""
    return any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" for c in text or "")


def load_units(db_path: Path) -> list[dict[str, Any]]:
    """(発言者, 読み, 会派) を1単位として集計する。"""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT s.speaker, s.speaker_yomi, s.speaker_group,
               COUNT(*)   AS n_speeches,
               MIN(s.date) AS first_date,
               MAX(s.date) AS last_date,
               GROUP_CONCAT(DISTINCT m.house) AS houses
        FROM speech s
        JOIN meeting m ON m.issue_id = s.issue_id
        WHERE s.speaker_kind = '議員'
        GROUP BY s.speaker, s.speaker_yomi, s.speaker_group
        ORDER BY n_speeches DESC
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]


def build_index(members: list[dict]) -> tuple[dict, dict]:
    by_kana: dict[str, list[dict]] = defaultdict(list)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for m in members:
        if kana := norm_kana(m.get("kana")):
            by_kana[kana].append(m)
        if is_japanese(m.get("name")) and (key := norm_name(m.get("name"))):
            by_name[key].append(m)
    return by_kana, by_name


def narrow(unit: dict, candidates: list[dict], party_map: dict,
           terms_by_id: dict) -> tuple[list[dict], str]:
    """候補が複数のとき、信号を強い順に当てて絞り込む。"""
    # 1. 漢字表記の完全一致
    exact = [c for c in candidates if norm_name(c.get("name")) == norm_name(unit["speaker"])]
    if len(exact) == 1:
        return exact, "漢字表記"
    pool = exact or candidates

    # 2. 会派 ↔ 政党（最も強い信号）
    allowed = {norm_party(p) for p in party_map.get(unit["speaker_group"] or "", [])}
    if allowed:
        by_party = [c for c in pool
                    if allowed & {norm_party(p) for p in c.get("parties", [])}]
        if len(by_party) == 1:
            return by_party, "会派↔政党"
        if by_party:
            pool = by_party

    # 3. 院（弱い信号。大臣は他院にも出るので、絞れたときだけ採用する）
    houses = {h for h in (unit["houses"] or "").split(",") if h and h != "両院"}
    if houses:
        by_house = [c for c in pool if houses & set(c.get("houses", []))]
        if len(by_house) == 1:
            return by_house, "院"
        if by_house:
            pool = by_house

    # 4. 任期（付与率が低いので最後）
    with_terms = []
    for c in pool:
        for t in terms_by_id.get(c["wikidata_id"], []):
            start, end = (t.get("start") or "")[:10], (t.get("end") or "9999-12-31")[:10]
            if start and start <= unit["last_date"] and unit["first_date"] <= end:
                with_terms.append(c)
                break
    if len(with_terms) == 1:
        return with_terms, "任期"

    return pool, "未確定"


def match(units: list[dict], by_kana: dict, by_name: dict,
          party_map: dict, terms_by_id: dict) -> list[dict]:
    results = []
    for unit in units:
        candidates = by_kana.get(norm_kana(unit["speaker_yomi"]), [])
        route = "読み"
        if not candidates:
            candidates = by_name.get(norm_name(unit["speaker"]), [])
            route = "漢字表記"
        if not candidates:
            results.append({**unit, "route": "未マッチ", "narrowed_by": None,
                            "n_candidates": 0, "matched": None, "candidates": []})
            continue

        if len(candidates) == 1:
            results.append({**unit, "route": route, "narrowed_by": None,
                            "n_candidates": 1, "matched": candidates[0], "candidates": []})
            continue

        pool, how = narrow(unit, candidates, party_map, terms_by_id)
        results.append({
            **unit, "route": route, "narrowed_by": how,
            "n_candidates": len(pool),
            "matched": pool[0] if len(pool) == 1 else None,
            "candidates": pool if len(pool) > 1 else [],
        })
    return results


def report(results: list[dict], members: list[dict], party_map: dict, out: Path | None) -> None:
    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text)
        lines.append(text)

    total_units = len(results)
    total_speeches = sum(r["n_speeches"] for r in results)
    resolved = [r for r in results if r["n_candidates"] == 1]
    ambiguous = [r for r in results if r["n_candidates"] > 1]
    unmatched = [r for r in results if r["n_candidates"] == 0]
    people = {r["matched"]["wikidata_id"] for r in resolved}

    emit("# 名寄せレポート")
    emit()
    emit(f"- 突合の単位 `(発言者, 読み, 会派)`: **{total_units:,}件** / 発言 {total_speeches:,}件")
    emit(f"- 確定した実人数（Wikidata ID のユニーク数）: **{len(people):,}人**")
    emit(f"- Wikidata の国会議員: {len(members):,}人")
    emit()
    emit("## マッチ率")
    emit()
    emit("| 区分 | 単位数 | 単位比 | 発言数 | 発言数比 |")
    emit("|---|---:|---:|---:|---:|")
    for label, group in (("一意に確定", resolved), ("候補が複数", ambiguous), ("未マッチ", unmatched)):
        n, sp = len(group), sum(r["n_speeches"] for r in group)
        emit(f"| {label} | {n:,} | {100*n/total_units:.1f}% | {sp:,} | {100*sp/total_speeches:.1f}% |")
    emit()
    emit(f"**発言数ベースの確定率: {100*sum(r['n_speeches'] for r in resolved)/total_speeches:.1f}%**")
    emit()

    routes: dict[str, int] = defaultdict(int)
    for r in resolved:
        routes[r["route"]] += 1
    emit("### 突合の経路")
    emit()
    emit("| 経路 | 単位数 |")
    emit("|---|---:|")
    for k, v in sorted(routes.items(), key=lambda x: -x[1]):
        emit(f"| {k}で一致 | {v:,} |")
    emit()

    narrowed = [r for r in resolved if r["narrowed_by"]]
    if narrowed:
        counts: dict[str, int] = defaultdict(int)
        for r in narrowed:
            counts[r["narrowed_by"]] += 1
        emit(f"### 候補が複数あったが絞り込めた: {len(narrowed):,}件")
        emit()
        emit("| 決め手 | 件数 |")
        emit("|---|---:|")
        for k, v in sorted(counts.items(), key=lambda x: -x[1]):
            emit(f"| {k} | {v:,} |")
        emit()
        emit("| 発言者 | 会派 | 決め手 | 確定先 |")
        emit("|---|---|---|---|")
        for r in sorted(narrowed, key=lambda x: -x["n_speeches"])[:20]:
            m = r["matched"]
            emit(f"| {r['speaker']} | {r['speaker_group'] or '—'} | {r['narrowed_by']} | "
                 f"{m['name']} ({m['wikidata_id']}, {'・'.join(m['houses'])}, "
                 f"{'・'.join(m['parties']) or '政党不明'}) |")
        emit()

    if ambiguous:
        emit("## 絞り込めなかった候補（要手当て）")
        emit()
        emit("| 発言者 | 読み | 会派 | 発言数 | 候補 |")
        emit("|---|---|---|---:|---|")
        for r in sorted(ambiguous, key=lambda x: -x["n_speeches"])[:30]:
            cands = " / ".join(f"{c.get('name')}({c['wikidata_id']})" for c in r["candidates"][:4])
            emit(f"| {r['speaker']} | {r['speaker_yomi']} | {r['speaker_group'] or '—'} | "
                 f"{r['n_speeches']:,} | {cands} |")
        emit()

    if unmatched:
        by_person: dict[tuple, dict] = {}
        for r in unmatched:
            key = (r["speaker"], r["speaker_yomi"])
            e = by_person.setdefault(key, {**r, "n_speeches": 0, "groups": set()})
            e["n_speeches"] += r["n_speeches"]
            if r["speaker_group"]:
                e["groups"].add(r["speaker_group"])
        emit(f"## 未マッチ（{len(by_person)}人 / Wikidata に項目が無い可能性）")
        emit()
        emit("| 発言者 | 読み | 発言数 | 会派 | 期間 |")
        emit("|---|---|---:|---|---|")
        for r in sorted(by_person.values(), key=lambda x: -x["n_speeches"])[:40]:
            # 会派名自体が「・」を含むので、区切りには「 / 」を使う
            emit(f"| {r['speaker']} | {r['speaker_yomi']} | {r['n_speeches']:,} | "
                 f"{' / '.join(sorted(r['groups']))} | {r['first_date']}〜{r['last_date']} |")
        emit()
        kana_mixed = [r for r in by_person.values() if re.search(r"[ぁ-ゖ]", r["speaker"])]
        emit(f"- うち発言者名にひらがなを含む（通称表記）: **{len(kana_mixed)}人**")
        emit()

    # 会派の網羅チェック
    emit("## 会派の一覧")
    emit()
    groups: dict[str, int] = defaultdict(int)
    for r in results:
        groups[r["speaker_group"] or "(空)"] += r["n_speeches"]
    unknown = [g for g in groups if g not in party_map and g != "(空)"]
    emit("| 会派名 | 発言数 | party_map.json |")
    emit("|---|---:|---|")
    for g, n in sorted(groups.items(), key=lambda x: -x[1]):
        status = "—" if g == "(空)" else ("✅" if g in party_map else "**⚠️ 未登録**")
        emit(f"| {g} | {n:,} | {status} |")
    emit()
    if unknown:
        emit(f"> ⚠️ `data/party_map.json` に未登録の会派が {len(unknown)}件あります。追記してください:")
        emit("> " + " / ".join(unknown))
        emit()

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nレポートを保存: {out}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "name_matching.md")
    args = parser.parse_args()

    if not MEMBERS_PATH.exists():
        raise SystemExit(f"{MEMBERS_PATH} が無い。先に scripts/fetch_wikidata.py を実行すること")

    members = json.loads(MEMBERS_PATH.read_text(encoding="utf-8"))
    terms = json.loads(TERMS_PATH.read_text(encoding="utf-8")) if TERMS_PATH.exists() else []
    party_map = {k: v for k, v in json.loads(
        PARTY_MAP_PATH.read_text(encoding="utf-8")).items() if not k.startswith("_")}

    terms_by_id: dict[str, list[dict]] = defaultdict(list)
    for t in terms:
        terms_by_id[t["wikidata_id"]].append(t)

    units = load_units(args.db)
    logger.info("突合単位 %s件 / Wikidata議員 %s人 / 会派マップ %s件",
                f"{len(units):,}", f"{len(members):,}", f"{len(party_map):,}")

    by_kana, by_name = build_index(members)
    results = match(units, by_kana, by_name, party_map, terms_by_id)
    report(results, members, party_map, args.report)


if __name__ == "__main__":
    main()
