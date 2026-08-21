"""運営コンソール。**人手が要る作業を1か所に集めた、手元だけの道具。**

    python scripts/admin.py        → http://127.0.0.1:8790 を開く

## 何のためにあるか

このプロジェクトの運用は月1時間以下と決めてある（CLAUDE.md）。それでも
**判断か手入力が要る作業**が残っていて、いずれも材料が別々の場所にあり、
書き戻す先も別々のJSONだった。**材料を探す時間のほうが、決める時間より長い。**
ここはその**材料と書き戻し先を並べて出すだけ**の道具。

| タブ | 触るファイル | 材料 | あとで回すもの |
|---|---|---|---|
| 状態 | （読むだけ） | `OPERATIONS.local.md` / 目録 | — |
| 争点語 | `data/topics.json` | 週次トレンド・頻出語・**その場で数えた件数** | `build_topics.py` |
| 政党 | `data/party_overrides.json` | `_name` / `_期間` と会派ごとの候補 | `build_politicians.py` |
| 除外語 | `data/topic_denylist.json` | 頻出語500件 | `build_topics.py` `build_frequent.py` |

## これは公開サイトではない

`127.0.0.1` にしか listen せず、`site/` にも `public/` にも入らないので
**間違って配られることが原理的に無い**。使うときだけ立ち上げる（`npm run dev` や
`dbserve` と同じ位置づけで、CLAUDE.md の「常時稼働プロセスを持たない」は配信の話）。
**外部依存パッケージ無し**（stdlib の `http.server` と `sqlite3` だけ）。

## 集計はしない

**このツールはJSONを書き換えるだけ。** DBも集計も作り直さない。
保存すると「次に打つコマンド」を出すので、それは手で回す。
黙って重い処理を始めない、というのがこのプロジェクトの流儀
（`docs/PIPELINE.md`）。

## その場で件数を数えられる理由

配信済みの期間DB（`data/dist/kokkai-*.db`）を**検索と同じ経路**で引くだけ。
3文字以上は FTS5(trigram)、2文字は `word` の索引。サイトが出す件数と同じ数が出る
（実測: `安全保障` 16,969件 / `憲法` 7,924件 ＝ `dist/topics.json` と一致。
全12本を引いて 4〜17ms）。**採否の判断に一番効くのがこの数字**で、
いままでは `build_topics.py` を回すまで分からなかった。

## 書き戻すときの約束

- **形式を変えない。** `_comment`（運営の申し送り）はそのまま、争点語は1行1語。
  差分が読めなくなるとレビューできない
- **事故だけ止める。** 半角英数・引けない語・id の使い回し・二重計上は拒否する。
  何を争点と呼ぶか、どの政党を当てるかは**運営の判断**なので口を出さない
- **落ちたら1バイトも書かない**（検証を通ってから書く）
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import threading
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_db import (  # noqa: E402
    DIST_DIR,
    TOPICS_PATH,
    check_topic_ids,
    parse_id_ranges,
)
from build_topics import CHAR_CLASSES  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = Path(__file__).resolve().parent / "admin.html"
OVERRIDES_PATH = ROOT / "data" / "party_overrides.json"
PARTY_MAP_PATH = ROOT / "data" / "party_map.json"
DENYLIST_PATH = ROOT / "data" / "topic_denylist.json"
OPERATIONS_PATH = ROOT / "OPERATIONS.local.md"

#: 候補として出す数。頻出語は500件あるので、全部出すと読めない
CANDIDATE_LIMIT = 60

#: 配信DB1本の上限。**超えると黙って CDN キャッシュから外れる**（docs/DECISIONS.md）
CACHE_LIMIT = 512_000_000
SIZE_WARN = 480_000_000

#: 推奨間隔の表記 → 日数。`OPERATIONS.local.md` の「推奨間隔」列に出る言葉だけ
INTERVAL_DAYS = {"1か月": 30, "3か月": 90, "6か月": 180, "1年": 365}


# --- 語の判定（サイトと同じ規則）--------------------------------------------

def to_full_width(text: str) -> str:
    """英数字を全角に寄せる。**会議録は半角を1文字も使っていない。**

    `site/src/lib/query.ts` の `toFullWidth()` と同じ写像（`A-Za-z0-9` を +0xFEE0）。
    ★ NFKC は全角→半角の**逆方向**なので使わない。
    """
    return "".join(chr(ord(c) + 0xFEE0) if ("A" <= c <= "Z" or "a" <= c <= "z"
                                            or "0" <= c <= "9") else c
                   for c in text)


def class_of(ch: str) -> int:
    return next((i for i, r in enumerate(CHAR_CLASSES) if r.fullmatch(ch)), -1)


def unsearchable_reason(term: str) -> str:
    """**引きようがない語**なら理由を返す。引けるなら空文字。

    `site/src/lib/query.ts` の `unsearchableTerms()` と同じ判定。索引は漢字・
    カタカナ・全角英数の**連続の中**しか2文字窓を切らないので、文字種をまたぐ
    2文字（`お金`）は入りようがない。
    """
    if len(term) >= 3:
        return ""
    if not term:
        return "空"
    if len(term) == 1:
        return "1文字（索引の項はちょうど2文字）"
    if class_of(term[0]) < 0 or class_of(term[1]) < 0:
        return "ひらがな（索引は漢字・カタカナ・全角英数の連続しか見ない）"
    if class_of(term[0]) != class_of(term[1]):
        return "文字種をまたぐ2文字（索引は同じ文字種の連続の中しか切らない）"
    return ""


def count_term(term: str) -> dict:
    """配信済みの期間DBを**検索と同じ経路で**引いて件数を出す。

    3文字以上は FTS5(trigram)、2文字は `word` の索引。**`topic_hit` は見ない**
    （まだ載っていない語を数えるための道具なので）。別表記は合算しない。
    """
    if reason := unsearchable_reason(term):
        return {"term": term, "unsearchable": reason, "total": 0, "periods": {}}

    fold = "".join(chr(ord(c) - 0x20) if "ａ" <= c <= "ｚ" else c for c in term)
    periods: dict[str, int] = {}
    for path in sorted(DIST_DIR.glob("kokkai-*.db")):
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if len(term) >= 3:
                n = con.execute("SELECT COUNT(*) FROM speech_fts WHERE speech_fts MATCH ?",
                                (f'"{term}"',)).fetchone()[0]
            else:
                row = con.execute("SELECT n_speeches FROM word WHERE term = ?",
                                  (fold,)).fetchone()
                n = row[0] if row else 0
        except sqlite3.OperationalError as e:
            return {"term": term, "unsearchable": f"期間DBを引けない（{e}）",
                    "total": 0, "periods": {}}
        finally:
            con.close()
        periods[path.stem.removeprefix("kokkai-")] = n
    return {"term": term, "unsearchable": "", "total": sum(periods.values()),
            "periods": periods}


# --- 読み書き ---------------------------------------------------------------

def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    """`indent=1`。**この2ファイルは実測でこの書式**（往復して一致を確認済み）。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def dump_topics(data: dict) -> str:
    """`data/topics.json` の書式を**そのまま**保って出す。

    `_comment` は1行1要素、争点語は**1行1語**。`json.dumps(indent=…)` に任せると
    全部が縦に伸びて差分が読めなくなるので、ここだけ手で組む。
    **空の `variants` は書かない**（元の書式に合わせる）。

    ★ **分類の変わり目に空行を入れる。** 元のファイルがそうなっていて、無くすと
      「1語足しただけ」の差分に**空行10か所の削除が混ざる**（実際に踏んだ）。
    """
    lines = ["{", ' "_comment": [']
    comment = data.get("_comment", [])
    for i, row in enumerate(comment):
        lines.append(f"  {json.dumps(row, ensure_ascii=False)}"
                     f"{'' if i == len(comment) - 1 else ','}")
    lines += [" ],", ' "topics": [']
    topics = data["topics"]
    previous: object = None
    for i, topic in enumerate(topics):
        if i and topic.get("category") != previous:
            lines.append("")
        previous = topic.get("category")
        parts = [f'"id": {topic["id"]}',
                 f'"term": {json.dumps(topic["term"], ensure_ascii=False)}',
                 f'"category": {json.dumps(topic.get("category"), ensure_ascii=False)}']
        if topic.get("variants"):
            parts.append(f'"variants": {json.dumps(topic["variants"], ensure_ascii=False)}')
        lines.append("  {" + ", ".join(parts) + "}"
                     + ("" if i == len(topics) - 1 else ","))
    lines += [" ]", "}"]
    return "\n".join(lines) + "\n"


# --- 状態（何が期限切れか）---------------------------------------------------

def read_operations() -> list[dict]:
    """`OPERATIONS.local.md` の実績表を読む。**推奨間隔を過ぎたものを出すため。**

    仕様（作業と間隔）は CLAUDE.md、実績はあちら。**ここは実績の表だけを読む**
    （見出しの `作業 | 推奨間隔 | 最終実施 | メモ` の並びに依存する）。
    読めなければ空を返す ＝ 画面には「読めない」と出て、勝手に何かを決めない。
    """
    if not OPERATIONS_PATH.exists():
        return []
    rows: list[dict] = []
    today = date.today()
    for line in OPERATIONS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip().replace("**", "") for c in line.strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("作業", "") or set(cells[0]) <= {"-", ":"}:
            continue
        task, interval, last = cells[0], cells[1], cells[2]
        note = cells[3] if len(cells) > 3 else ""
        days = INTERVAL_DAYS.get(interval)
        elapsed = None
        if match := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", last):
            elapsed = (today - date(*(int(g) for g in match.groups()))).days
        rows.append({
            "task": task, "interval": interval, "last": last, "note": note,
            "elapsed": elapsed,
            "overdue": bool(days and (elapsed is None or elapsed > days)),
            "due_in": None if not days or elapsed is None else days - elapsed,
        })
    return rows


def delivery_status() -> dict:
    """配信物の大きさ。**512MB を超えたファイルは黙ってキャッシュされなくなる。**"""
    path = DIST_DIR / "manifest.json"
    if not path.exists():
        return {"available": False}
    databases = load_json(path).get("databases", [])
    if not databases:
        return {"available": False}
    largest = max(databases, key=lambda d: d.get("size", 0))
    return {
        "available": True,
        "periods": len(databases),
        "total": sum(d.get("size", 0) for d in databases),
        "largest": {"id": largest["id"], "size": largest.get("size", 0)},
        "limit": CACHE_LIMIT, "warn": SIZE_WARN,
        "over": largest.get("size", 0) > SIZE_WARN,
        "unrecorded": [d["id"] for d in databases if not d.get("topics")],
    }


# --- 争点語 -----------------------------------------------------------------

def retired_ids(current: list[dict]) -> list[int]:
    """**もう使ってはいけない id。** 目録に残っている ＝ 配信済みDBが持っている。

    `topics.json` から消しても `topic_hit` は配信済みDBに残るので、使い回すと
    **消したはずの語のヒットを新しい語の名前で出す**。
    """
    path = DIST_DIR / "manifest.json"
    if not path.exists():
        return []
    seen: set[int] = set()
    for entry in load_json(path).get("databases", []):
        seen |= set(parse_id_ranges((entry.get("topics") or {}).get("ids", "")))
    return sorted(seen - {t["id"] for t in current})


def topics_state() -> dict:
    source = load_json(TOPICS_PATH)
    topics = source["topics"]

    stats: dict[int, dict] = {}
    series = DIST_DIR / "topics.json"
    if series.exists():
        stats = {t["id"]: t for t in load_json(series).get("topics", [])}

    listed = [{**t, "variants": t.get("variants", []),
               "n_speeches": stats.get(t["id"], {}).get("n_speeches"),
               "indexed": stats.get(t["id"], {}).get("indexed", False),
               "unsearchable": unsearchable_reason(t["term"])} for t in topics]

    known = {t["term"] for t in topics}
    for topic in topics:
        known |= set(topic.get("variants", []))

    candidates: list[dict] = []
    seen: set[str] = set()

    trending = DIST_DIR / "trending.json"
    if trending.exists():
        for window in reversed(load_json(trending).get("windows", [])):
            for term in window.get("terms", []):
                if term["term"] not in known and term["term"] not in seen:
                    seen.add(term["term"])
                    candidates.append({"term": term["term"], "n": term["n"],
                                       "note": f"直近の国会で {term['lift']:.0f}倍",
                                       "from": "トレンド"})
    frequent = DIST_DIR / "frequent.json"
    if frequent.exists():
        for word in load_json(frequent).get("words", []):
            if word["term"] not in known and word["term"] not in seen:
                seen.add(word["term"])
                candidates.append({"term": word["term"], "n": word["n"],
                                   "note": f"ピーク {word['peak']}・{word['burst']:.0f}倍",
                                   "from": "頻出語"})

    retired = retired_ids(topics)
    return {
        "topics": listed,
        "categories": sorted({t["category"] for t in topics if t.get("category")}),
        "candidates": candidates[:CANDIDATE_LIMIT],
        "retired": retired,
        "next_id": max([t["id"] for t in topics] + retired, default=0) + 1,
        "path": str(TOPICS_PATH.relative_to(ROOT)),
    }


def validate_topics(topics: list[dict], before: list[dict]) -> tuple[list[str], list[str]]:
    """`(止める理由, 警告)`。**止めるのは事故だけ**で、編集方針には口を出さない。"""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        check_topic_ids(topics, TOPICS_PATH)
    except SystemExit as e:
        errors.append(str(e))
        return errors, warnings

    known = {t["id"] for t in before} | set(retired_ids(before))
    for topic in topics:
        term = topic["term"]
        if not term:
            errors.append("空の争点語がある")
            continue
        if term != to_full_width(term):
            errors.append(f"`{term}` に半角英数が混じっている"
                          f"（`{to_full_width(term)}` にすること。半角は黙って0件になる）")
        if reason := unsearchable_reason(term):
            errors.append(f"`{term}` は検索経路で引けない: {reason}"
                          " - 載せると、全期間のDBを作り直すまで発言一覧が空になる")
        if any(c.isspace() for c in term):
            errors.append(f"`{term}` に空白が入っている")
        forms = [term, *topic.get("variants", [])]
        for a in forms:
            for b in forms:
                if a != b and a in b:
                    errors.append(f"`{term}` の表記 `{a}` が `{b}` に含まれている"
                                  "（同じ発言を二重に数える）")
        if topic["id"] in known and not any(t["id"] == topic["id"] for t in before):
            errors.append(f"id={topic['id']} は**消した id**。使い回すと配信済みDBの "
                          "topic_hit から別の争点の発言を引く。新しい id を振ること")

    terms = [t["term"] for t in topics]
    for term in sorted({t for t in terms if terms.count(t) > 1}):
        errors.append(f"`{term}` が2回出てくる")

    # 争点どうしの包含は**残すと決めたものがある**（憲法⊃憲法改正）ので止めない
    for a in topics:
        for b in topics:
            if a["id"] != b["id"] and a["term"] and a["term"] in b["term"]:
                warnings.append(f"`{a['term']}` は `{b['term']}` に含まれる"
                                "（粒度違いなら残してよい。同じ争点の別名なら variants へ）")
    return errors, warnings


def diff_topics(before: list[dict], after: list[dict]) -> dict:
    """**全期間の作り直しが要るか**の判定材料（`docs/PIPELINE.md` の表）。"""
    old = {t["id"]: t for t in before}
    new = {t["id"]: t for t in after}
    renamed = [f"{old[i]['term']} → {new[i]['term']}"
               for i in sorted(old.keys() & new.keys()) if old[i]["term"] != new[i]["term"]]
    revariant = [new[i]["term"] for i in sorted(old.keys() & new.keys())
                 if old[i].get("variants", []) != new[i].get("variants", [])]
    removed = [old[i]["term"] for i in sorted(old.keys() - new.keys())]
    return {"added": [new[i]["term"] for i in sorted(new.keys() - old.keys())],
            "removed": removed, "renamed": renamed, "revariant": revariant,
            # 追加だけなら作り直しは要らない（indexed が偽の語は検索経路で出る）
            "rebuild": bool(removed or renamed or revariant)}


def save_topics(payload: dict) -> dict:
    before = load_json(TOPICS_PATH)
    topics = []
    for topic in payload.get("topics", []):
        topics.append({
            "id": topic["id"],
            "term": to_full_width(str(topic.get("term", "")).strip()),
            "category": (topic.get("category") or "").strip() or None,
            "variants": [to_full_width(v.strip())
                         for v in topic.get("variants", []) if v and v.strip()],
        })

    errors, warnings = validate_topics(topics, before["topics"])
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}

    changed = diff_topics(before["topics"], topics)
    TOPICS_PATH.write_text(dump_topics({**before, "topics": topics}), encoding="utf-8")
    commands = ["python scripts/build_topics.py"]
    if changed["rebuild"]:
        commands += ["python scripts/build_db.py --split --page-size 8192",
                     "python scripts/verify_dist.py"]
    return {"ok": True, "warnings": warnings, "changed": changed, "commands": commands}


# --- 政党の手入力 ------------------------------------------------------------

def parties_state() -> dict:
    """`data/party_overrides.json` の枠を、埋まっていないものから順に出す。

    枠そのもの（誰のどの会派か・`_name` と `_期間`）は `build_politicians.py` が
    毎回書き直す。**ここで足せるのは中身（政党名）だけ。**
    """
    if not OVERRIDES_PATH.exists():
        return {"available": False}
    data = load_json(OVERRIDES_PATH)
    candidates = load_json(PARTY_MAP_PATH) if PARTY_MAP_PATH.exists() else {}

    known: set[str] = {"無所属"}
    for value in candidates.values():
        if isinstance(value, list):
            known |= set(value)

    people = []
    for key, person in data.items():
        if key.startswith("_") or not isinstance(person, dict):
            continue
        slots = []
        for kaiha, slot in person.items():
            if kaiha.startswith("_") or not isinstance(slot, dict):
                continue
            known |= {slot["party"]} if slot.get("party") else set()
            slots.append({
                "kaiha": kaiha,
                "span": slot.get("_期間", ""),
                "party": slot.get("party"),
                # 期間で分かれている枠は**ここでは触らない**（区間の編集はファイルで）
                "periods": slot.get("periods"),
                "candidates": [p for p in candidates.get(kaiha, [])] if isinstance(
                    candidates.get(kaiha), list) else [],
            })
        if slots:
            people.append({"key": key, "name": person.get("_name", key), "slots": slots})

    # **埋まっていないものを上に。** 次に同数なら発言数の多い順（`_name` の並び）を保つ
    people.sort(key=lambda p: (all(s["party"] or s["periods"] for s in p["slots"]),))
    empty = sum(1 for p in people for s in p["slots"] if not s["party"] and not s["periods"])
    return {"available": True, "people": people, "parties": sorted(known),
            "empty": empty,
            "slots": sum(len(p["slots"]) for p in people),
            "path": str(OVERRIDES_PATH.relative_to(ROOT))}


def save_parties(payload: dict) -> dict:
    """`party` だけを書き換える。**枠そのものと `periods` には触らない。**"""
    data = load_json(OVERRIDES_PATH)
    known: set[str] = {"無所属"}
    if PARTY_MAP_PATH.exists():
        for value in load_json(PARTY_MAP_PATH).values():
            if isinstance(value, list):
                known |= set(value)

    warnings: list[str] = []
    errors: list[str] = []
    changed: list[str] = []
    for update in payload.get("updates", []):
        key, kaiha = update.get("key"), update.get("kaiha")
        party = (update.get("party") or "").strip() or None
        person = data.get(key)
        if not isinstance(person, dict) or not isinstance(person.get(kaiha), dict):
            errors.append(f"{key} / {kaiha} という枠が無い（build_politicians.py を回した？）")
            continue
        slot = person[kaiha]
        if slot.get("periods"):
            errors.append(f"{person.get('_name', key)} の「{kaiha}」は期間で分かれている。"
                          "ここでは触らない（data/party_overrides.json を直接直すこと）")
            continue
        if slot.get("party") == party:
            continue
        if party and party not in known:
            # build_politicians.py も同じ警告を出す。**止めはしない**（新党はありうる）
            warnings.append(f"`{party}` は会派の候補にも既存の値にも無い"
                            "（新しい政党なら data/party_map.json も直すこと）")
        slot["party"] = party
        changed.append(f"{person.get('_name', key)} / {kaiha} → {party or '（未入力に戻した）'}")

    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings}
    if changed:
        write_json(OVERRIDES_PATH, data)
    return {"ok": True, "warnings": warnings, "changed": changed,
            "commands": ["python scripts/build_politicians.py"] if changed else []}


# --- 除外語 -----------------------------------------------------------------

def deny_state() -> dict:
    """`data/topic_denylist.json` と、判断の材料になる頻出語500件。"""
    if not DENYLIST_PATH.exists():
        return {"available": False}
    terms = load_json(DENYLIST_PATH).get("terms", [])
    words = []
    frequent = DIST_DIR / "frequent.json"
    if frequent.exists():
        words = [{"term": w["term"], "n": w["n"], "burst": w["burst"], "peak": w["peak"],
                  "denied": w["term"] in terms}
                 for w in load_json(frequent).get("words", [])]
    return {"available": True, "terms": terms, "words": words,
            "path": str(DENYLIST_PATH.relative_to(ROOT))}


def save_deny(payload: dict) -> dict:
    """**並べ替えない。** 足した語は末尾に付く（画面がそう並べて渡してくる）。

    五十音や辞書順に直すと、1語足しただけの差分が**全行の入れ替え**になって
    レビューできなくなる（実際に踏んだ）。
    """
    data = load_json(DENYLIST_PATH)
    before = list(data.get("terms", []))
    terms: list[str] = []
    for term in payload.get("terms", []):
        term = str(term).strip()
        if term and term not in terms:
            terms.append(term)
    data["terms"] = terms
    write_json(DENYLIST_PATH, data)
    added = sorted(set(terms) - set(before))
    removed = sorted(set(before) - set(terms))
    return {"ok": True, "warnings": [], "changed": {"added": added, "removed": removed},
            "commands": (["python scripts/build_topics.py",
                          "python scripts/build_frequent.py"]
                         if added or removed else [])}


# --- HTTP -------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        # 手元だけの道具。**キャッシュさせない**（編集中に古い画面を見ない）
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        routes = {
            "/api/status": lambda: {"operations": read_operations(),
                                    "delivery": delivery_status(),
                                    "root": str(ROOT), "today": date.today().isoformat()},
            "/api/topics": topics_state,
            "/api/parties": parties_state,
            "/api/deny": deny_state,
        }
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/term":
                term = to_full_width((parse_qs(url.query).get("q") or [""])[0].strip())
                self._json(200, count_term(term))
            elif url.path in routes:
                self._json(200, routes[url.path]())
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as e:                                   # noqa: BLE001
            self._json(500, {"ok": False, "errors": [f"{type(e).__name__}: {e}"]})

    def do_POST(self) -> None:  # noqa: N802
        savers = {"/api/topics": save_topics, "/api/parties": save_parties,
                  "/api/deny": save_deny}
        path = urlparse(self.path).path
        if path not in savers:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            payload = json.loads(self.rfile.read(
                int(self.headers.get("content-length") or 0)) or b"{}")
        except json.JSONDecodeError as e:
            self._json(400, {"ok": False, "errors": [f"JSON として読めない: {e}"]})
            return
        try:
            self._json(200, savers[path](payload))
        except Exception as e:                                   # noqa: BLE001
            # **書く前に落ちる**ようにしてあるので、ここに来てもファイルは無傷
            self._json(500, {"ok": False, "errors": [f"保存に失敗した: {type(e).__name__}: {e}"]})

    def log_message(self, fmt: str, *args) -> None:
        pass                     # 編集のたびに流れるだけなので出さない


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--no-open", action="store_true", help="ブラウザを開かない")
    args = parser.parse_args()

    missing = [p.name for p in (TOPICS_PATH, HTML_PATH) if not p.exists()]
    if missing:
        sys.exit(f"★ {' / '.join(missing)} が無い")

    # ★ 127.0.0.1 にだけ listen する。手元の道具であって、配るものではない
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"運営コンソール: {url}")
    print("  ★ JSONを書き換えるだけ。集計は保存後に出るコマンドを手で回すこと")
    print("  終了は Ctrl+C")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n終了")


if __name__ == "__main__":
    main()
