"""Phase 0 ステップ4: Wikidata から国会議員リストを SPARQL で取得する。

名寄せの軸を Wikidata の議員IDに置くのが企画書の方針。

役職QIDは wbsearchentities で特定した実測値：
    Q17506823 = 衆議院議員 (member of the House of Representatives of Japan)
    Q14552828 = 参議院議員 (member of the House of Councillors)

ラベル全走査でQIDを発見する方式も試したが、SPARQL エンドポイントが
502/504 を返すほど重かったため採用しなかった。--verify で妥当性だけ確認できる。

クエリは「人物属性」と「任期」に分けている。1本にまとめると
OPTIONAL の直積で行数が爆発してタイムアウトするため。

使い方:
    python scripts/fetch_wikidata.py
    python scripts/fetch_wikidata.py --verify   # 役職QIDが妥当か確認するだけ
"""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ENDPOINT = "https://query.wikidata.org/sparql"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

# Wikidata は User-Agent の明示を求めている（無いと 403）
USER_AGENT = "KokkaiTimeline/0.1 (personal research project; contact via GitHub)"

POSITIONS = {
    "Q17506823": "衆議院",
    "Q14552828": "参議院",
}

# 連続クエリの間隔。エンドポイントは共用資源なので余裕を持たせる。
QUERY_INTERVAL_SEC = 5.0

logger = logging.getLogger("wikidata")

VERIFY = """
SELECT ?position ?positionLabel (COUNT(DISTINCT ?person) AS ?holders) WHERE {
  VALUES ?position { %(positions)s }
  ?person wdt:P39 ?position ; wdt:P31 wd:Q5 .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "ja,en". }
}
GROUP BY ?position ?positionLabel
"""

# 人物属性。政党は複数持ちうるので person あたり複数行になる。
PERSONS = """
SELECT ?person ?personLabel ?kana ?birth ?death ?partyLabel ?website ?ndlAuth WHERE {
  ?person wdt:P39 wd:%(position)s ; wdt:P31 wd:Q5 .
  OPTIONAL { ?person wdt:P1814 ?kana . }
  OPTIONAL { ?person wdt:P569 ?birth . }
  OPTIONAL { ?person wdt:P570 ?death . }
  OPTIONAL { ?person wdt:P102 ?party . }
  OPTIONAL { ?person wdt:P856 ?website . }
  OPTIONAL { ?person wdt:P349 ?ndlAuth . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "ja,en". }
}
"""

# 任期。P39 の修飾子から開始/終了と選挙区を取る。
TERMS = """
SELECT ?person ?start ?end ?districtLabel WHERE {
  ?person p:P39 ?statement ; wdt:P31 wd:Q5 .
  ?statement ps:P39 wd:%(position)s .
  OPTIONAL { ?statement pq:P580 ?start . }
  OPTIONAL { ?statement pq:P582 ?end . }
  OPTIONAL { ?statement pq:P768 ?district . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "ja,en". }
}
"""


def run_sparql(query: str, *, retries: int = 4) -> list[dict[str, Any]]:
    """SPARQL を投げて bindings をフラットな dict のリストで返す。"""
    data = urllib.parse.urlencode({"query": query, "format": "json"}).encode("utf-8")

    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            ENDPOINT,
            data=data,
            headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError) as exc:
            wait = 15 * attempt
            logger.warning("SPARQL 失敗 (%d/%d): %s — %d秒待って再試行", attempt, retries, exc, wait)
            time.sleep(wait)
    else:
        raise RuntimeError("Wikidata SPARQL に接続できなかった")

    time.sleep(QUERY_INTERVAL_SEC)
    return [{k: v["value"] for k, v in b.items()} for b in payload["results"]["bindings"]]


def qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def verify() -> None:
    query = VERIFY % {"positions": " ".join(f"wd:{q}" for q in POSITIONS)}
    print(f"\n{'QID':<12} {'保持者数':>8}  {'想定':<6}  Wikidataのラベル")
    print("-" * 70)
    for row in run_sparql(query):
        q = qid(row["position"])
        print(f"{q:<12} {int(row['holders']):>8,}  {POSITIONS.get(q, '?'):<6}  {row['positionLabel']}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="役職QIDの妥当性確認だけ行う")
    args = parser.parse_args()

    if args.verify:
        verify()
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    persons: dict[str, dict[str, Any]] = {}
    terms: list[dict[str, Any]] = []

    for position, house in POSITIONS.items():
        logger.info("[%s] 人物属性を取得中… (%s)", house, position)
        for row in run_sparql(PERSONS % {"position": position}):
            pid = qid(row["person"])
            entry = persons.setdefault(pid, {
                "wikidata_id": pid,
                "name": row.get("personLabel"),
                "kana": row.get("kana"),
                "birth": row.get("birth"),
                "death": row.get("death"),
                "website": row.get("website"),
                "ndl_auth_id": row.get("ndlAuth"),
                "houses": [],
                "parties": [],
            })
            if house not in entry["houses"]:
                entry["houses"].append(house)
            party = row.get("partyLabel")
            if party and party not in entry["parties"]:
                entry["parties"].append(party)
            # かな等は行によって欠けることがあるので、来たものを優先して埋める
            for key, src in (("kana", "kana"), ("website", "website"), ("ndl_auth_id", "ndlAuth")):
                if not entry[key] and row.get(src):
                    entry[key] = row[src]

        logger.info("[%s] 任期を取得中…", house)
        for row in run_sparql(TERMS % {"position": position}):
            terms.append({
                "wikidata_id": qid(row["person"]),
                "house": house,
                "start": row.get("start"),
                "end": row.get("end"),
                "district": row.get("districtLabel"),
            })

    (OUT_DIR / "wikidata_members.json").write_text(
        json.dumps(list(persons.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "wikidata_terms.json").write_text(
        json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")

    with_kana = sum(1 for p in persons.values() if p["kana"])
    with_ndl = sum(1 for p in persons.values() if p["ndl_auth_id"])
    print(f"\n議員: {len(persons):,}人 / 任期レコード: {len(terms):,}件")
    print(f"  かな(P1814)あり     : {with_kana:>6,}人 ({100*with_kana/len(persons):5.1f}%)  ★名寄せの第一キー")
    print(f"  NDL典拠ID(P349)あり : {with_ndl:>6,}人 ({100*with_ndl/len(persons):5.1f}%)")
    for house in POSITIONS.values():
        n = sum(1 for p in persons.values() if house in p["houses"])
        print(f"  {house:<6}            : {n:>6,}人")
    print(f"\n保存: {OUT_DIR}")


if __name__ == "__main__":
    main()
