"""名寄せ結果を確定させ、議員マスタと所属の時系列を作る。

`match_politicians.py` は突合の**レポート**を出すだけで、結果を残さない。
このスクリプトは同じ突合ロジックを使って、DBに投入できる形に落とす。

    data/politicians.json     議員マスタ + 所属 + 突合単位→議員IDの対応
    data/politician_ids.json  ID の台帳（**コミットする**。生成物ではない）

`build_db.py` がこの2つを読んで `politician` / `affiliation` テーブルと
`speech.politician_id` を埋める。

## 議員IDを別ファイルで管理する理由

議員IDは **URLに出る**（`/politician/123`）。日次で作り直すたびに番号が振り直されると
リンクが全部壊れるので、一度振ったIDは台帳に残して使い回す。

台帳のキーは2種類あり、**同じ人が複数のキーを持つ**:

    Q7827692                    Wikidata ID（突合できた場合）
    local:阿部俊子:あべとしこ    会議録側の情報だけで作るキー

Wikidata に項目が無い議員がいる（2026年の新人が中心）。
その人には `local:` キーでIDを振っておき、**あとから Wikidata に項目ができたら
同じIDに QID キーを追加する**。こうすればURLが変わらない。

## 所属政党（affiliation）

会派は政党と1対1ではないので、**特定できたときだけ**政党名を入れる。

    1. `data/party_overrides.json` に手入力があれば、それを最優先
    2. 会派の候補（`party_map.json`）が1つ → それ
    3. 候補と Wikidata の政党(P102) の交差が1つ → それ
    4. それ以外 → **NULL のまま**（推測で埋めない）

**Wikidata の P102 は所属政党の履歴の羅列で、現在の所属を表さない。**
「立憲民主・社民」のような統一会派はこれだけでは割れないので、
残りは `data/party_overrides.json` に人力で入れる。このスクリプトが
未解決の (議員, 会派) を `null` 付きで自動追記するので、値を埋めて再実行すればよい。

期間は「その会派で発言した最初と最後の日」であって、在籍期間そのものではない。

使い方:
    python scripts/build_politicians.py
    python scripts/build_politicians.py --db data/kokkai.db --report reports/politicians.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from match_politicians import (  # noqa: E402
    MEMBERS_PATH, PARTY_MAP_PATH, TERMS_PATH,
    build_index, load_units, match, norm_kana, norm_name, norm_party,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "kokkai.db"
OUT_PATH = ROOT / "data" / "politicians.json"
IDS_PATH = ROOT / "data" / "politician_ids.json"
OVERRIDES_PATH = ROOT / "data" / "party_overrides.json"

logger = logging.getLogger("politicians")

# 「政党に所属していない」ことが分かっている、という値。
# `party IS NULL`（＝政党を特定できていない）とは意味が違うので混ぜないこと。
# 政党別の集計では政党のひとつとして扱わず、別の区分として出す。
NO_PARTY = "無所属"


# --- ID台帳 ---------------------------------------------------------------

class IdLedger:
    """議員IDの台帳。一度振ったIDは変えない。

    1人が2種類のキーを持つ:

        Q7827692                  Wikidata ID
        local:阿部俊子:あべとしこ  会議録側の情報だけで作るキー

    Wikidata ID があるときは**そちらが本人の identity**。local キーは
    「まだ Wikidata に項目が無い人」を追うための補助で、**同姓同名で衝突しうる**
    （鬼木誠は衆院自民と参院立憲の2人いる）。そのため local キーは
    **持ち主がいなければ登録する**だけにして、他人のIDを横取りさせない。
    """

    def __init__(self, path: Path):
        self.path = path
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.keys: dict[str, int] = dict(data.get("keys", {}))
        self.next_id: int = data.get("next_id", 1)
        self.dirty = False
        # そのIDに Wikidata ID が結び付いているか（local キーの横取りを防ぐ判定に使う）
        self.qid_of: dict[int, str] = {v: k for k, v in self.keys.items() if k.startswith("Q")}

    def _bind(self, key: str, politician_id: int, *, only_if_free: bool = False) -> None:
        owner = self.keys.get(key)
        if owner == politician_id:
            return
        if owner is not None and only_if_free:
            return  # 他人が持っているキーは奪わない
        self.keys[key] = politician_id
        if key.startswith("Q"):
            self.qid_of[politician_id] = key
        self.dirty = True

    def _new(self) -> int:
        politician_id, self.next_id = self.next_id, self.next_id + 1
        self.dirty = True
        return politician_id

    def resolve(self, qid: str | None, local: str) -> int:
        if qid and (known := self.keys.get(qid)) is not None:
            self._bind(local, known, only_if_free=True)
            return known

        owner = self.keys.get(local)
        # local キーの持ち主が「まだ QID が無い人」なら、それは同じ人。
        # 既に別の QID が付いているなら同姓同名の別人なので新しく振る
        if owner is not None and self.qid_of.get(owner) in (None, qid):
            if qid:
                self._bind(qid, owner)
            return owner

        politician_id = self._new()
        if qid:
            self._bind(qid, politician_id)
        self._bind(local, politician_id, only_if_free=True)
        return politician_id

    def save(self) -> None:
        if not self.dirty:
            logger.info("ID台帳に変更なし（%s件）", f"{len(set(self.keys.values())):,}")
            return
        self.path.write_text(
            json.dumps({
                "_comment": [
                    "議員IDの台帳。scripts/build_politicians.py が追記する。",
                    "**生成物ではないのでコミットすること。** 失うとURLが全部変わる。",
                    "1人が複数のキーを持つ: Wikidata ID と local:表記:読み。",
                    "手で消さないこと。追記だけが安全な操作。",
                ],
                "next_id": self.next_id,
                "keys": dict(sorted(self.keys.items(), key=lambda kv: (kv[1], kv[0]))),
            }, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        logger.info("ID台帳を更新: %s（%s人）", self.path.name,
                    f"{len(set(self.keys.values())):,}")


def local_key(speaker: str, yomi: str | None) -> str:
    return f"local:{norm_name(speaker)}:{norm_kana(yomi)}"


# --- 集約 -----------------------------------------------------------------

def group_people(results: list[dict]) -> dict[str, dict]:
    """突合結果を人単位にまとめる。

    確定した単位は Wikidata ID で、それ以外は会議録側のキーでまとめる。
    同姓同名は Wikidata ID で自然に分かれる（鬼木誠が2人になる）が、
    **未マッチの同姓同名は分離できない**ので警告を出す。
    """
    people: dict[str, dict] = {}
    for r in results:
        matched = r["matched"] if r["n_candidates"] == 1 else None
        key = matched["wikidata_id"] if matched else local_key(r["speaker"], r["speaker_yomi"])
        person = people.setdefault(key, {
            "key": key, "matched": matched, "units": [],
            "names": defaultdict(int), "yomi": defaultdict(int),
        })
        # 同じ人が別の単位でだけ確定していることがある（会派が変わった等）
        if matched and not person["matched"]:
            person["matched"] = matched
        person["units"].append(r)
        person["names"][r["speaker"]] += r["n_speeches"]
        if r["speaker_yomi"]:
            person["yomi"][r["speaker_yomi"]] += r["n_speeches"]
    return people


#: 公式サイトのURLとして受ける scheme。**これ以外は落とす。**
SAFE_URL_SCHEMES = ("http://", "https://")

#: 落とした公式サイトURL（`--report` に出す）。件数だけでなく中身を残す
dropped_urls: list[tuple[str, str]] = []


def safe_url(url: object, who: str) -> str | None:
    """Wikidata の公式サイト（P856）を、リンクにしてよい形だけ通す。

    ★ **ここが「誰でも編集できるもの」が公開ページの `href` に出る唯一の経路。**
      氏名は会議録から採っていて Wikidata で上書きしない（`build_records`）。
      政党は `party_map` の候補内からしか選ばれない（`resolve_party`）。
      院は固定文字列2つ。**自由文字列のまま外に出るのはこのURLだけ**なので、
      検査もここ1か所で足りる。

    `javascript:` は配信側の CSP（`site/public/_headers`）も止めるが、
    **CSP を最後の砦にしない**。CSP を緩めた瞬間に静かに効かなくなる。

    ★ **`http://` は落とさない**（実測で 437件ある。消すと大量のリンクが黙って
      消える）。平文であることと、掲載元が Wikidata で未検証であることは、
      サイト側の表示（`politician/[id].astro`）で断ってある。
    """
    if not isinstance(url, str):
        return None
    text = url.strip()
    if not text.lower().startswith(SAFE_URL_SCHEMES):
        dropped_urls.append((who, text[:120]))
        return None
    # 制御文字・空白が混ざったものも落とす（URLとして壊れているか、細工されている）
    if any(c.isspace() or ord(c) < 0x20 for c in text):
        dropped_urls.append((who, text[:120]))
        return None
    return text


def resolve_party(kaiha: str | None, party_map: dict, member_parties: list[str],
                  matched: bool, override: str | None) -> tuple[str | None, str | None]:
    """会派から政党を決める。決められなければ (None, 理由)。**推測はしない。**

    優先順位は 議員ごとの手入力 → 機械的に決まる分 → NULL。

    「この会派の所属議員はたいていこの政党」という当てはめは**入れない**。
    誤った所属は同姓同名の誤分離と同じで、無記入より有害になる（docs/SCOPE.md）。
    割り切れない分は人力で1件ずつ決める。

    理由を返すのは、欠損がどこに偏っているかをレポートで示すため。
    偏りを知らずに政党別の集計を出すと、特定の政党だけ発言数が少なく出る。
    """
    if override:
        return override, None
    candidates = party_map.get(kaiha or "", [])
    if not candidates:
        return None, "会派の候補が空（政党を特定できない会派。設計どおり）"
    if len(candidates) == 1:
        return candidates[0], None
    if not matched:
        return None, "要手入力: Wikidata と未突合"
    if not member_parties:
        return None, "要手入力: Wikidata に政党(P102)が無い"
    overlap = [p for p in candidates
               if norm_party(p) in {norm_party(q) for q in member_parties}]
    if len(overlap) == 1:
        return overlap[0], None
    # P102 は履歴の羅列で、現在の所属が入っていないことがある
    return None, ("要手入力: Wikidata の政党と交差しない" if not overlap else
                  "要手入力: Wikidata の政党と複数交差（在籍時期が分からない）")


class PartyOverrides:
    """会派から機械的に政党を決められない分の手入力。

    キーは Wikidata ID（無ければ `local:` キー）→ 会派名 → 中身。中身は3通り書ける:

        "立憲民主党"                          その会派にいた全期間で同じ政党
        {"party": "立憲民主党"}               同上（`_期間` を併記できる形）
        {"periods": [                         **会派名が同じまま政党が変わった場合**
            {"until": "2023-03-31", "party": "社会民主党"},
            {"party": "立憲民主党"}
        ]}

    `periods` を書くと所属レコードが期間で分割される。統一会派は名前が変わらないまま
    構成政党が入れ替わることがあるので、1会派＝1政党とは限らない。
    未決定は `null`。このクラスが未解決分を `null` で自動追記する。
    """

    def __init__(self, path: Path):
        self.path = path
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.data: dict[str, dict] = {k: v for k, v in raw.items() if not k.startswith("_")}

    def validate(self, party_map: dict) -> int:
        """手入力の政党名を会派の候補と突き合わせる。

        誤った所属は同姓同名の誤分離と同じで、無記入より有害になる。
        候補外の値は打ち間違いか、`party_map.json` の更新漏れのどちらか。
        """
        known = {p for v in party_map.values() for p in v}
        problems = 0
        for person_key, entry in self.data.items():
            name = entry.get("_name", person_key)
            for kaiha, value in entry.items():
                if kaiha.startswith("_"):
                    continue
                for period in self.get(person_key, kaiha) or []:
                    party = period.get("party")
                    # 区間の一部を未決定のまま置くのは正当な状態なので警告しない
                    if not party or party == NO_PARTY:
                        continue
                    candidates = party_map.get(kaiha, [])
                    if candidates and party not in candidates:
                        logger.warning("%s / %s: 「%s」は会派の候補に無い（候補: %s）",
                                       name, kaiha, party, " / ".join(candidates))
                        problems += 1
                    elif party not in known:
                        logger.warning("%s / %s: 「%s」は party_map.json のどこにも無い。"
                                       "打ち間違いか、対応表の更新漏れ", name, kaiha, party)
                        problems += 1
        return problems

    def get(self, person_key: str, kaiha: str | None) -> list[dict] | None:
        """[{from, until, party}, ...] を返す。未記入なら None。

        期間の指定が無ければ1区間として返す。`from` / `until` が省略された端は
        呼び出し側が所属レコードの実際の期間で埋める。
        """
        value = self.data.get(person_key, {}).get(kaiha or "")
        if not value:
            return None
        if isinstance(value, str):
            return [{"party": value}]
        if periods := value.get("periods"):
            # party が null の区間も**落とさない**。落とすと後ろの区間の開始日がずれて、
            # 「一部だけ分かっている」状態を書けなくなる
            return periods if any(p.get("party") for p in periods) else None
        return [{"party": value["party"]}] if value.get("party") else None

    def sync(self, politicians: list[dict], party_map: dict) -> tuple[int, int]:
        """未解決の (議員, 会派) を null で追記し、解決済みになった null を掃除する。

        影響の大きい議員から埋められるよう、**未確定の発言数が多い順**に並べる。
        戻り値は (未記入の件数, 記入済みの件数)。
        """
        # (議員, 会派) ごとに期間も持つ。同じ会派名でも在籍時期によって政党が違いうるので、
        # 期間が見えないと判断できない
        needed: dict[str, dict[str, dict]] = {}
        weight: dict[str, int] = defaultdict(int)
        kaiha_seen: set[str] = set()
        for p in politicians:
            for a in p["affiliations"]:
                if not (a["party_unresolved"] or "").startswith("要手入力"):
                    continue
                entry = needed.setdefault(p["person_key"], {})
                entry[a["kaiha"] or ""] = {
                    "_期間": f"{a['start_date']}〜{a['end_date']} / {a['n_speeches']:,}発言",
                    "party": None,
                }
                entry["_name"] = f"{p['name']}（{p['name_kana'] or '—'}）"
                weight[p["person_key"]] += a["n_speeches"]
                kaiha_seen.add(a["kaiha"] or "")

        def is_filled(slot) -> bool:
            if isinstance(slot, str):
                return bool(slot)
            return bool(slot) and bool(slot.get("party") or slot.get("periods"))

        merged: dict[str, dict] = {}
        for key in sorted(needed, key=lambda k: (-weight[k], k)):
            slots = needed[key]
            existing = self.data.get(key, {})
            merged[key] = {"_name": f"{slots['_name']} {weight[key]:,}発言"}
            # 訂正の経緯など、人が書いた注記は消さない（_期間 だけは毎回作り直す）
            merged[key].update({k: v for k, v in existing.items()
                                if k.startswith("_") and k not in ("_name", "_期間")})
            # ★ 同じ議員の**記入済みの会派**は、今回 needed に出てこなくても残す。
            #   1会派だけ未確定になった議員がいると、隣の会派の手入力が消えて
            #   次の実行で元に戻ってしまう（訂正作業でまさに起きる）
            for kaiha, old in existing.items():
                if not kaiha.startswith("_") and kaiha not in slots and is_filled(old):
                    merged[key][kaiha] = old
            for kaiha in sorted(k for k in slots if k != "_name"):
                slot = dict(slots[kaiha])
                old = existing.get(kaiha)
                # 期間は毎回作り直す（データが増えれば伸びる）。記入した値だけ引き継ぐ
                if isinstance(old, str):
                    slot["party"] = old
                elif isinstance(old, dict):
                    slot.update({k: v for k, v in old.items()
                                 if k.startswith("_") and k != "_期間"})
                    if old.get("periods"):
                        slot.pop("party", None)
                        slot["periods"] = old["periods"]
                    elif old.get("party"):
                        slot["party"] = old["party"]
                merged[key][kaiha] = slot
        # 手で入れたが今回は未解決に出てこなかった分（自動確定した所属の訂正など）は残す。
        # 訂正した項目がここで消えると、次の実行で元に戻ってしまう
        for key, slots in self.data.items():
            if key in merged:
                continue
            kept = {k: v for k, v in slots.items() if k.startswith("_") or is_filled(v)}
            if any(not k.startswith("_") for k in kept):
                merged[key] = kept

        blank = sum(1 for slots in merged.values()
                    for k, v in slots.items() if k != "_name" and not is_filled(v))
        filled = sum(1 for slots in merged.values()
                     for k, v in slots.items() if k != "_name" and is_filled(v))

        self.path.write_text(json.dumps({
            "_comment": [
                "会派から機械的に政党を決められない分の手入力。**コミットすること。**",
                "",
                "形式: Wikidata ID（無ければ local:表記:読み） → 会派名 → 中身。",
                "未確定の発言数が多い議員から並べてある。上から埋めるほど効く。",
                "",
                "■ その会派にいた全期間で同じ政党なら party を埋める",
                '    "立憲民主・社民": { "_期間": "…", "party": "立憲民主党" }',
                "",
                "■ 会派名が同じまま政党が変わったなら periods で分ける",
                "  統一会派は名前が変わらないまま構成政党が入れ替わることがある。",
                "  until は「その日まで」（その日を含む）。最後の区間の until は書かない。",
                '    "立憲民主・社民": { "_期間": "…", "periods": [',
                '        { "until": "2023-03-31", "party": "社会民主党" },',
                '        { "party": "立憲民主党" } ] }',
                "",
                "■ 政党に所属していないと分かっているなら \"無所属\"",
                "  null（＝まだ決めていない・特定できない）とは意味が違う。",
                "  会派名の「無所属」は『会派に属さない』であって政党の話ではないので混同しない",
                "  （議長・副議長は自党の会派を抜けるが政党には属したまま）。",
                "",
                "_期間 と _name は目印。**毎回上書きされる**ので編集しても意味がない",
                "（データが増えれば期間も伸びる）。party / periods だけが引き継がれる。",
                "null は「まだ決めていない」。null のままでも壊れない（政党不明として扱う）。",
                "候補に無い政党名を書くと build_politicians.py が警告する（\"無所属\" は除く）。",
                "",
                "会派ごとにまとめて読み替える仕組みは**あえて用意していない**。",
                "誤った所属は同姓同名の誤分離と同じで、無記入より有害だから",
                "（`docs/SCOPE.md`）。1件ずつ根拠を確認して決めること。",
                "",
                "埋めたら python scripts/build_politicians.py を再実行する。",
                "議員ごとの詳細（発言数・Wikidataリンク）は reports/party_todo.md。",
            ],
            "_会派ごとの候補": {
                kaiha: party_map.get(kaiha, []) for kaiha in sorted(kaiha_seen)
            },
            **merged,
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return blank, filled


def write_todo(politicians: list[dict], party_map: dict, members_by_id: dict,
               out: Path) -> int:
    """手入力が要る (議員, 会派) の一覧。発言数の多い順に並べる。

    `data/party_overrides.json` を埋めるための作業リスト。
    上から順に埋めれば、少ない件数で影響の大きいところから潰せる。
    """
    rows = []
    for p in politicians:
        for a in p["affiliations"]:
            if not (a["party_unresolved"] or "").startswith("要手入力"):
                continue
            rows.append((p, a))
    rows.sort(key=lambda pa: -pa[1]["n_speeches"])

    total = sum(a["n_speeches"] for _, a in rows)
    by_kaiha: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for _, a in rows:
        entry = by_kaiha[a["kaiha"] or "(空)"]
        entry[0] += 1
        entry[1] += a["n_speeches"]

    lines = [
        "# 政党の手入力リスト",
        "",
        f"- 埋める箇所: **{len(rows)}件** / 影響する発言 **{total:,}件**",
        "- 埋め先: `data/party_overrides.json`",
        "- 埋めたら `python scripts/build_politicians.py` を再実行する",
        "",
        "## 対象の会派",
        "",
        "残っているのはこの会派だけ。どれも統一会派で、会派名からは政党を一意に決められない。",
        "",
        "| 会派 | 残っている件数 | 発言数 | 候補（この中から選ぶ） |",
        "|---|---:|---:|---|",
    ]
    for kaiha, (n, sp) in sorted(by_kaiha.items(), key=lambda kv: -kv[1][1]):
        cands = " / ".join(party_map.get(kaiha, [])) or "—"
        lines.append(f"| {kaiha} | {n} | {sp:,} | {cands} |")
    lines += [
        "",
        "> 会派ごとにまとめて読み替える仕組みは**あえて用意していない**。",
        "> 誤った所属は同姓同名の誤分離と同じで、無記入より有害になる（`docs/SCOPE.md`）。",
        "",
        "## 議員ごとの一覧",
        "",
        "**上から埋めるほど効く。** 発言数の多い順に並べてある。",
        "",
        "**期間が長いものは要注意。** 会派名が同じまま政党が変わっていることがある。",
        "その場合は `party` ではなく `periods` で区間に分けること。",
        "",
        "| # | 議員 | 会派 | 期間 | 発言数 | 会派の候補（この中から選ぶ） | Wikidataの政党(P102) | キー |",
        "|---:|---|---|---|---:|---|---|---|",
    ]
    for i, (p, a) in enumerate(rows, 1):
        cands = " / ".join(party_map.get(a["kaiha"] or "", [])) or "—"
        p102 = " / ".join(members_by_id.get(p["wikidata_id"], {}).get("parties", [])) or "—"
        qid = p["wikidata_id"]
        who = f"[{p['name']}](https://www.wikidata.org/wiki/{qid})" if qid else p["name"]
        span = f"{a['start_date']}〜{a['end_date']}"
        lines.append(f"| {i} | {who} | {a['kaiha']} | {span} | {a['n_speeches']:,} | {cands} | "
                     f"{p102} | `{p['person_key']}` |")
    lines += ["", f"※ 上位20件で {sum(a['n_speeches'] for _, a in rows[:20]):,}件"
                  f"（未確定分の {100*sum(a['n_speeches'] for _, a in rows[:20])/total:.0f}%）"
                  f"をカバーする。" if rows else ""]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def _why_unresolved(politicians: list[dict]) -> dict[str, tuple[int, int]]:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for p in politicians:
        for a in p["affiliations"]:
            if a["party"] is None:
                entry = counts[a.get("party_unresolved") or "不明"]
                entry[0] += 1
                entry[1] += a["n_speeches"]
    return {k: (v[0], v[1]) for k, v in counts.items()}


def split_by_periods(unit: dict, periods: list[dict], count: Callable) -> list[dict]:
    """所属レコードを手入力の期間で分割する。

    区間は書かれた順に並んでいるものとして扱う。`until` の翌日から次の区間が始まり、
    最後の区間はその会派の最終発言日で終わる。`from` を書けば明示もできる。
    発言数は実データから数え直す（区間の切り方で件数が変わるため）。
    """
    rows: list[dict] = []
    cursor = unit["first_date"]
    for i, period in enumerate(periods):
        start = period.get("from") or cursor
        end = period.get("until") or (unit["last_date"] if i == len(periods) - 1 else None)
        if end is None:
            logger.warning("%s / %s: 最後でない区間に until が無い。無視した",
                           unit["speaker"], unit["speaker_group"])
            continue
        n, first, last = count(unit, start, end)
        if not n:
            logger.warning("%s / %s: 区間 %s〜%s に発言が無い。無視した",
                           unit["speaker"], unit["speaker_group"], start, end)
            continue
        rows.append({
            "party": period.get("party"),
            # 一部の区間だけ未記入のまま置ける。作業リストに戻ってくるよう理由を付ける
            "party_unresolved": None if period.get("party") else "要手入力: 期間の一部が未決定",
            "start_date": first, "end_date": last, "n_speeches": n,
        })
        cursor = _next_day(end)

    counted = sum(r["n_speeches"] for r in rows)
    if rows and counted != unit["n_speeches"]:
        # 区間の間に隙間があると発言が落ちる。埋めたつもりで落ちているのが一番まずい
        logger.warning("%s / %s: 区間の合計 %s件が全体 %s件と合わない。期間の指定を見直すこと",
                       unit["speaker"], unit["speaker_group"],
                       f"{counted:,}", f"{unit['n_speeches']:,}")
    return rows


def _next_day(date: str) -> str:
    return (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()


def make_counter(db_path: Path) -> Callable:
    """(突合単位, 開始日, 終了日) → (発言数, 実際の最初の日, 実際の最後の日)。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def count(unit: dict, start: str, end: str) -> tuple[int, str | None, str | None]:
        return con.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM speech"
            " WHERE speaker = ? AND speaker_yomi IS ? AND speaker_group IS ?"
            "   AND speaker_kind = '議員' AND date BETWEEN ? AND ?",
            (unit["speaker"], unit["speaker_yomi"], unit["speaker_group"], start, end),
        ).fetchone()

    return count


def build_records(people: dict[str, dict], ledger: IdLedger, party_map: dict,
                  overrides: PartyOverrides,
                  count: Callable) -> tuple[list[dict], dict[str, int]]:
    """議員マスタと「突合単位 → 議員ID」の対応を作る。"""
    politicians: list[dict] = []
    unit_map: dict[str, int] = {}

    # 発言数の多い順にIDを振る。初回実行の採番を再現可能にするため
    ordered = sorted(people.values(),
                     key=lambda p: (-sum(u["n_speeches"] for u in p["units"]), p["key"]))

    for person in ordered:
        matched = person["matched"]
        # 代表表記は「最も多く使われた表記」。通称（あべ俊子）で通っている議員がいるので
        # Wikidata の表記で上書きしない
        name = max(person["names"].items(), key=lambda kv: kv[1])[0]
        yomi = max(person["yomi"].items(), key=lambda kv: kv[1])[0] if person["yomi"] else None

        qid = matched["wikidata_id"] if matched else None
        politician_id = ledger.resolve(qid, local_key(name, yomi))

        # 手入力のキーは Wikidata ID を優先する（QIDが付いても手入力が生き続ける）
        person_key = qid or local_key(name, yomi)

        affiliations = []
        for unit in person["units"]:
            unit_map[unit_key(unit)] = politician_id
            periods = overrides.get(person_key, unit["speaker_group"])

            # 同じ会派名のまま政党が変わった分は、期間で複数レコードに割る
            if periods and len(periods) > 1:
                for row in split_by_periods(unit, periods, count):
                    affiliations.append({"kaiha": unit["speaker_group"], **row})
                continue

            party, why = resolve_party(unit["speaker_group"], party_map,
                                       matched.get("parties", []) if matched else [],
                                       matched is not None,
                                       periods[0]["party"] if periods else None)
            affiliations.append({
                "kaiha": unit["speaker_group"],
                "party": party,
                "party_unresolved": why,
                "start_date": unit["first_date"],
                "end_date": unit["last_date"],
                "n_speeches": unit["n_speeches"],
            })
        affiliations.sort(key=lambda a: (a["start_date"], a["kaiha"] or ""))

        politicians.append({
            "id": politician_id,
            "person_key": person_key,
            "name": name,
            "name_kana": yomi,
            "wikidata_id": matched["wikidata_id"] if matched else None,
            "wikidata_name": matched["name"] if matched else None,
            "house": "・".join(matched.get("houses") or []) if matched else None,
            "official_url": safe_url(matched.get("website"), name) if matched else None,
            "n_speeches": sum(u["n_speeches"] for u in person["units"]),
            "first_date": min(u["first_date"] for u in person["units"]),
            "last_date": max(u["last_date"] for u in person["units"]),
            "affiliations": affiliations,
        })

    politicians.sort(key=lambda p: p["id"])
    return politicians, unit_map


def unit_key(unit: dict) -> str:
    """突合単位のキー。build_db.py が speech から同じキーを組み立てて引く。"""
    return "\t".join([unit["speaker"], unit["speaker_yomi"] or "", unit["speaker_group"] or ""])


# --- 訂正 -------------------------------------------------------------------

def find_politicians(data: dict, query: str) -> list[dict]:
    """名前・ID・Wikidata ID・読みのどれでも引ける。訂正依頼は表記がまちまちなので。"""
    exact = [p for p in data["politicians"]
             if query in (p["name"], str(p["id"]), p["wikidata_id"], p["name_kana"])]
    if exact:
        return exact
    key = norm_name(query) or norm_kana(query)
    return [p for p in data["politicians"]
            if key and (key in norm_name(p["name"]) or key in norm_kana(p["name_kana"]))]


def fix_politician(query: str, out_path: Path, overrides_path: Path,
                   party_map: dict) -> int:
    """指定の議員の所属を `party_overrides.json` に編集できる形で書き出す。

    自動確定した所属は手入力ファイルに載らないので、訂正依頼が来ても
    そのままでは直せない。このコマンドが**現在の値ごと**雛形を作るので、
    値を書き換えて `build_politicians.py` を回せば直る。
    """
    if not out_path.exists():
        raise SystemExit(f"{out_path} が無い。先に build_politicians.py を実行すること")
    data = json.loads(out_path.read_text(encoding="utf-8"))
    found = find_politicians(data, query)
    if not found:
        raise SystemExit(f"「{query}」に該当する議員がいない")
    if len(found) > 1:
        print(f"「{query}」に {len(found)}人が該当する。ID か Wikidata ID で指定し直すこと:")
        for p in found:
            print(f"  id={p['id']:<6} {p['name']}（{p['name_kana']}） "
                  f"{p['wikidata_id'] or '—'} {p['first_date']}〜{p['last_date']}")
        return 1

    person = found[0]
    doc = json.loads(overrides_path.read_text(encoding="utf-8")) \
        if overrides_path.exists() else {}
    existing = doc.get(person["person_key"], {})

    print(f"{person['name']}（{person['name_kana']}）  id={person['id']}  "
          f"{person['wikidata_id'] or '独自ID'}")
    print(f"  URL に出る ID は {person['id']}。**この値は変えないこと**\n")
    print(f"  {'会派':<26}{'期間':<26}{'発言':>7}  現在の政党")
    for a in person["affiliations"]:
        print(f"  {(a['kaiha'] or '(空)'):<26}"
              f"{a['start_date'] + '〜' + a['end_date']:<26}{a['n_speeches']:>7,}  "
              f"{a['party'] or 'NULL'}")

    entry = {"_name": f"{person['name']}（{person['name_kana'] or '—'}）"}
    entry.update({k: v for k, v in existing.items() if k.startswith("_") and k != "_name"})
    for kaiha in dict.fromkeys(a["kaiha"] or "" for a in person["affiliations"]):
        rows = [a for a in person["affiliations"] if (a["kaiha"] or "") == kaiha]
        old = existing.get(kaiha)
        slot = {
            "_期間": f"{rows[0]['start_date']}〜{rows[-1]['end_date']} / "
                     f"{sum(r['n_speeches'] for r in rows):,}発言",
            "_候補": party_map.get(kaiha, []),
        }
        if isinstance(old, dict) and old.get("periods"):
            slot["periods"] = old["periods"]
        elif len(rows) > 1:
            # すでに期間で割れている分は、そのまま編集できる形で出す
            slot["periods"] = [
                {"until": r["end_date"], "party": r["party"]} for r in rows[:-1]
            ] + [{"party": rows[-1]["party"]}]
        else:
            slot["party"] = rows[0]["party"]
        entry[kaiha] = slot

    doc[person["person_key"]] = entry
    overrides_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")

    print(f"\n{overrides_path} に書き出した。キー: {person['person_key']}")
    print(json.dumps({person["person_key"]: entry}, ensure_ascii=False, indent=1))
    print("\n次の手順:")
    print("  1. 上の party を直す。期間で分かれるなら periods に書き換える:")
    print('       "periods": [ {"until": "2023-03-31", "party": "A党"}, {"party": "B党"} ]')
    print("     さらに割るときは区間を足すだけでよい。発言数は数え直される")
    print('  2. 訂正の経緯を残すなら "_note" に書く（自由記述。上書きされない）')
    print("  3. python scripts/build_politicians.py")
    print("  4. python scripts/build_db.py --split   ← 期間DBに反映")
    return 0


# --- レポート -------------------------------------------------------------

def write_report(politicians: list[dict], results: list[dict], out: Path | None) -> list[str]:
    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text)
        lines.append(text)

    total_speeches = sum(p["n_speeches"] for p in politicians)
    with_qid = [p for p in politicians if p["wikidata_id"]]
    without = [p for p in politicians if not p["wikidata_id"]]
    all_aff = [a for p in politicians for a in p["affiliations"]]
    with_party = [a for a in all_aff if a["party"]]

    emit("# 議員マスタ")
    emit()
    emit(f"- 議員: **{len(politicians):,}人** / 発言 {total_speeches:,}件")
    emit(f"- Wikidata と紐づいた: {len(with_qid):,}人 "
         f"（{100*len(with_qid)/len(politicians):.1f}%）")
    emit(f"- 独自IDのみ: **{len(without):,}人** / 発言 "
         f"{sum(p['n_speeches'] for p in without):,}件 "
         f"（{100*sum(p['n_speeches'] for p in without)/total_speeches:.1f}%）")
    emit(f"- 所属レコード: {len(all_aff):,}件 / うち政党を特定できた: "
         f"**{len(with_party):,}件（{100*len(with_party)/len(all_aff):.1f}%）**")
    emit()

    if without:
        emit("## Wikidata に項目が無い議員")
        emit()
        emit("恒久的な欠落ではない。項目ができれば日次の再突合で自動的に紐づく")
        emit("（IDは台帳で維持されるのでURLは変わらない）。")
        emit()
        emit("| ID | 発言者 | 読み | 発言数 | 会派 | 期間 |")
        emit("|---:|---|---|---:|---|---|")
        for p in sorted(without, key=lambda x: -x["n_speeches"]):
            kaiha = " / ".join(sorted({a["kaiha"] or "—" for a in p["affiliations"]}))
            emit(f"| {p['id']} | {p['name']} | {p['name_kana'] or '—'} | {p['n_speeches']:,} | "
                 f"{kaiha} | {p['first_date']}〜{p['last_date']} |")
        emit()

    # 政党を特定できなかった会派＝分析で「政党別」に集計できない部分
    unresolved: dict[str, int] = defaultdict(int)
    for a in all_aff:
        if not a["party"]:
            unresolved[a["kaiha"] or "(空)"] += a["n_speeches"]
    if unresolved:
        missing = sum(unresolved.values())
        emit("## 政党を特定できなかった会派")
        emit()
        emit(f"発言 **{missing:,}件（{100*missing/total_speeches:.1f}%）**が政党不明。")
        emit("**推測で埋めない**方針なので NULL のままにしてある。")
        emit()
        emit("> ### ⚠️ 政党別の集計をそのまま出さないこと")
        emit(">")
        emit("> 欠損は**特定の会派に偏っている**（統一会派に集中する）。")
        emit("> 政党別に集計すると、その政党の発言数だけが系統的に少なく出る。")
        emit(">")
        emit("> **集計の既定単位は会派にする。** 会派は会議録に書かれている事実で推測がゼロ。")
        emit("> 政党は「特定できた分だけの補助表示」に留める。")
        emit()
        emit("| 会派 | 発言数 |")
        emit("|---|---:|")
        for kaiha, n in sorted(unresolved.items(), key=lambda kv: -kv[1]):
            emit(f"| {kaiha} | {n:,} |")
        emit()
        emit("原因の内訳（Wikidata の政党 P102 は履歴の羅列で、"
             "**現在の所属を表さない**のが主因）:")
        emit()
        emit("| 原因 | 所属レコード | 発言数 |")
        emit("|---|---:|---:|")
        for why, (n, sp) in sorted(_why_unresolved(politicians).items(),
                                   key=lambda kv: -kv[1][1]):
            emit(f"| {why} | {n:,} | {sp:,} |")
        emit()

    # 未マッチの同姓同名は分離できない。件数がゼロでないなら手当てが要る
    collisions = defaultdict(set)
    for p in politicians:
        if not p["wikidata_id"]:
            collisions[(p["name"], p["name_kana"])].add(p["id"])
    dupes = {k: v for k, v in collisions.items() if len(v) > 1}
    if dupes:
        emit("> ⚠️ 未マッチの同姓同名を検出。会議録側の情報だけでは分離できない:")
        for (name, yomi), ids in dupes.items():
            emit(f"> - {name}（{yomi}） → ID {sorted(ids)}")
        emit()

    ambiguous = [r for r in results if r["n_candidates"] > 1]
    if ambiguous:
        emit(f"> ⚠️ 候補を絞り込めなかった突合単位が {len(ambiguous)}件ある。"
             f"独自IDに落としているので、同一人物が2つのIDに割れている可能性がある。")
        emit()

    emit("## 発言数の多い議員")
    emit()
    emit("| ID | 議員 | Wikidata | 所属の変遷 | 発言数 |")
    emit("|---:|---|---|---|---:|")
    for p in sorted(politicians, key=lambda x: -x["n_speeches"])[:20]:
        moves = " → ".join(dict.fromkeys(a["party"] or a["kaiha"] or "—"
                                         for a in p["affiliations"]))
        emit(f"| {p['id']} | {p['name']} | {p['wikidata_id'] or '—'} | {moves} | "
             f"{p['n_speeches']:,} |")
    emit()

    # ★ Wikidata は誰でも編集できる。**公開ページの href に出るのは公式サイトURLだけ**
    #   なので、落としたものはここに残す（黙って消すと、消えたことに気づけない）
    linked = [x for x in politicians if x["official_url"]]
    http_urls = [x for x in linked if x["official_url"].startswith("http://")]
    emit("## 公式サイトのURL（Wikidata P856）")
    emit()
    emit(f"- リンクにした: {len(linked):,}件（うち平文 http: **{len(http_urls):,}件**）")
    emit(f"- 受け付けなかった: **{len(dropped_urls):,}件**（http/https 以外）")
    for who, url in dropped_urls[:20]:
        emit(f"  - {who}: `{url}`")
    emit()

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nレポートを保存: {out}")
    return lines


# --- 実行 -----------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help="突合の元にするDB（全期間を含むもの）")
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    parser.add_argument("--ids", type=Path, default=IDS_PATH)
    parser.add_argument("--overrides", type=Path, default=OVERRIDES_PATH,
                        help="政党の手入力（未解決分を null で自動追記する）")
    parser.add_argument("--fix", metavar="議員",
                        help="所属の訂正用。名前・ID・Wikidata ID のどれでも可。"
                             "現在の値ごと編集できる雛形を party_overrides.json に書き出す")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "politicians.md")
    args = parser.parse_args()

    party_map_all = {k: v for k, v in json.loads(
        PARTY_MAP_PATH.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    if args.fix:
        raise SystemExit(fix_politician(args.fix, args.out, args.overrides, party_map_all))

    if not MEMBERS_PATH.exists():
        raise SystemExit(f"{MEMBERS_PATH} が無い。先に scripts/fetch_wikidata.py を実行すること")

    members: list[dict[str, Any]] = json.loads(MEMBERS_PATH.read_text(encoding="utf-8"))
    terms = json.loads(TERMS_PATH.read_text(encoding="utf-8")) if TERMS_PATH.exists() else []
    party_map = party_map_all

    terms_by_id: dict[str, list[dict]] = defaultdict(list)
    for t in terms:
        terms_by_id[t["wikidata_id"]].append(t)

    units = load_units(args.db)
    by_kana, by_name = build_index(members)
    results = match(units, by_kana, by_name, party_map, terms_by_id)
    logger.info("突合単位 %s件 → 一意に確定 %s件",
                f"{len(results):,}", f"{sum(1 for r in results if r['n_candidates'] == 1):,}")

    people = group_people(results)
    ledger = IdLedger(args.ids)
    overrides = PartyOverrides(args.overrides)
    if problems := overrides.validate(party_map):
        logger.warning("手入力に %d件の疑わしい値がある。上の警告を確認すること", problems)
    politicians, unit_map = build_records(people, ledger, party_map, overrides,
                                          make_counter(args.db))
    ledger.save()

    blank, filled = overrides.sync(politicians, party_map)
    todo_path = args.report.with_name("party_todo.md")
    n_todo = write_todo(politicians, party_map, {m["wikidata_id"]: m for m in members},
                        todo_path)
    logger.info("政党の手入力: 記入済み %s件 / **未記入 %s件** → %s（作業リスト: %s）",
                f"{filled:,}", f"{blank:,}", args.overrides.name, todo_path)
    if n_todo:
        logger.info("  → %s を上から埋めて再実行すること", todo_path.name)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(
        {"politicians": politicians, "units": unit_map},
        ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    logger.info("議員マスタを保存: %s（%s人 / 単位 %s件）", args.out.name,
                f"{len(politicians):,}", f"{len(unit_map):,}")

    print()
    write_report(politicians, results, args.report)


if __name__ == "__main__":
    main()
