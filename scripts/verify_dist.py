"""配信物（期間DBと目録）を**公開する前に**検証する。

日次更新は R2 と Pages に配ってしまうと取り消せない。ここを通らなかったものは
配らない、という関門をひとつ置く（docs/ROADMAP.md「日次更新を公開前に検証する」）。

## 見るもの

    1. 目録        分割規則・期間の連続・databases と periods の一致・
                   手元にあるDBとの size / version / from / to の突き合わせ
    2. 分割規則    scripts/build_db.py の period_of() と
                   site/src/lib/query.ts の periodOf() が同じ写像か
                   （**片方だけ変えると存在しないファイルを引きに行く**）
    3. 期間DB      PRAGMA quick_check・page_size・journal_mode・
                   meta（period / period_rule / from / to）・
                   目録の `topics`（その期間が持つ争点語）が実物と一致するか
    4. 骨格        speech_fts があるか・争点語が入っているか・
                   **両方にある争点語の term が一致するか**
                   （**発言の量に関係なく成り立つもの。4と5を混ぜないこと**）
    5. スモーク    FTS・2文字語・争点語の**3経路を1件ずつ実際に引く**

## スモークテストが3経路あるのはなぜか

検索の引き先は3つに分かれていて（docs/PITFALLS.md）、**壊れ方も別々**:

    FTS      3文字以上。trigram の索引。--no-fts で作られていないことがある
    word     2文字。FTS では**原理的に引けない**ので、ここが空だと `年金` が0件になる
    topic    争点語。id を使い回して語を書き換えると、**別の争点の発言が黙って出る**
             （id は `topics.json` に書いてある不変の識別子。並び順から採らない）

どれも「引けない」ではなく「**0件**」や「**中身が入れ替わる**」形で壊れる。
エラーにならないので、実際に引いてみる以外に気づく手が無い。

争点語の経路では、引けた発言の本文にその語が**本当に入っているか**まで見る。
`topic_id` のずれはこれでしか捕まらない（件数は正しいまま中身だけ入れ替わる）。

使い方:
    python scripts/verify_dist.py                 # data/dist にあるDB全部
    python scripts/verify_dist.py --id 2026H2     # 触った期間だけ（日次更新はこれ）
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

# 出力先が UTF-8 でないことがある（Windows のコンソールは既定で cp932）。
# **検証そのものが文字化けで落ちないように**、出せない文字はエスケープに逃がす
sys.stdout.reconfigure(errors="backslashreplace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_db import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    DEFAULT_PERIOD,
    DIST_DIR,
    TOPICS_PATH,
    file_version,
    format_id_ranges,
    load_topics,
    parse_id_ranges,
    period_of,
    read_topics,
    topics_fingerprint,
)

ROOT = Path(__file__).resolve().parent.parent
QUERY_TS = ROOT / "site" / "src" / "lib" / "query.ts"

# 代表語。**3経路に1語ずつ。**「国会の会議録なら必ず出てくる語」を選んである。
# 0件なら語が無いのではなく索引が壊れている。
#
# ★ 全角に畳まない。どちらも漢字で、`splitTerms()` の全角化の対象外
#   （英数字を全角にする変換であって、漢字は素通りする）。
SMOKE_FTS = "委員会"      # 3文字。FTS5(trigram) の経路
SMOKE_WORD = "国会"       # 2文字。word / word_hit の経路
# 争点語の代表はDBから採る（この期間でいちばん出ている語）。
# `topics.json` の中身は運営が変えるので、ここに固定で書くと更新のたびに嘘になる。

# これ未満の発言しか無い期間では代表語のスモークテストを飛ばす。
# **半期の開始直後（1月1日・7月1日）は会議録が1件も入っていないDBができる。**
# 飛ばしたことは必ず表示する（黙って通さない）。
SMOKE_MIN_SPEECHES = 1_000

# CDN のキャッシュ上限は 512MB。**超えたファイルは黙ってキャッシュから外れる**
# （RTT 8ms → 77ms）。手前で落とす（docs/DECISIONS.md）。
CACHE_LIMIT = 512e6
DEFAULT_MAX_SIZE = 480e6


class Report:
    """○ / × を並べて、最後に落ちたものだけまとめて返す。

    **最初の失敗で止めない。** 1回の実行で直せるものは全部出したい
    （日次更新のログを見返す回数を減らす）。
    """

    def __init__(self) -> None:
        self.failures: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n--- {title} ---")

    def check(self, ok: bool, message: str) -> bool:
        print(f"  {'○' if ok else '×'} {message}")
        if not ok:
            self.failures.append(message)
        return ok

    def note(self, message: str) -> None:
        print(f"  ・ {message}")

    def fail(self, message: str) -> None:
        self.check(False, message)


def period_index(period: str) -> int | None:
    """期間IDを時系列の整数に。連続しているかを見るためだけに使う。"""
    if re.fullmatch(r"\d{4}", period):
        return int(period) * 2
    m = re.fullmatch(r"(\d{4})H([12])", period)
    return int(m[1]) * 2 + int(m[2]) - 1 if m else None


def check_split_rule(report: Report, rule: str) -> None:
    """分割規則が Python と TypeScript で同じか。

    **規則は2か所にある**（`build_db.py` の `period_of()` と `query.ts` の `periodOf()`）。
    片方だけ変えると、サイトが存在しないファイル名を組み立てて検索が丸ごと止まる。
    ここで見るのは「既定の単位」と「半期の境界」の2つ。写像そのものの検証は
    `site/test/query.test.ts`（`npm run check`）にある。
    """
    report.section("分割規則（build_db.py と query.ts）")
    if not QUERY_TS.exists():
        report.fail(f"{QUERY_TS} が無い")
        return
    src = QUERY_TS.read_text(encoding="utf-8")
    m = re.search(r"function\s+periodOf\s*\([^)]*rule\s*:\s*PeriodRule\s*=\s*\"(\w+)\"", src)
    if not m:
        report.fail("query.ts の periodOf() を読み取れない。"
                    "署名を変えたならこの検査（verify_dist.py の check_split_rule）も直すこと")
        return
    report.check(m[1] == rule, f"既定の分割単位: build_db.py={rule} / query.ts={m[1]}")
    body = src[m.end():src.index("\n}", m.end())]
    report.check('<= "06"' in body, "半期の境界が7月1日（query.ts の periodOf に `<= \"06\"`）")


def check_manifest(report: Report, dist_dir: Path, rule: str, max_size: float) -> dict | None:
    """目録そのものの整合。**DBを1つも開かずに分かるものだけ**をここで見る。"""
    report.section("目録（manifest.json）")
    path = dist_dir / "manifest.json"
    if not path.exists():
        report.fail(f"{path} が無い。先に build_db.py --split を回すこと")
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        report.fail(f"目録を読めない: {e}")
        return None

    report.check(manifest.get("period") == rule,
                 f"分割規則: {manifest.get('period')!r}（期待 {rule!r}）")

    entries = manifest.get("databases") or []
    ids = [e["id"] for e in entries]
    report.check(manifest.get("periods") == ids,
                 "periods と databases の期間IDが一致している")

    indices = [period_index(i) for i in ids]
    report.check(all(i is not None for i in indices) and indices == sorted(indices),
                 f"期間IDが時系列に並んでいる: {' '.join(ids)}")
    if all(i is not None for i in indices) and len(indices) > 1:
        gaps = [f"{ids[k]}→{ids[k + 1]}" for k in range(len(indices) - 1)
                if indices[k + 1] - indices[k] != 1]
        # **痩せた目録は「検索が特定の期間だけ0件」になって出る。**
        # 日次更新は触った期間しか手元に置かないので、前回の目録の引き継ぎが
        # 効かないとここが欠ける（docs/PIPELINE.md「初回実行で特に見るところ」）
        report.check(not gaps, f"期間に抜けが無い（{len(ids)}期間）"
                               + (f" - 抜け: {' '.join(gaps)}" if gaps else ""))

    for e in entries:
        if not e.get("version"):
            report.fail(f"{e['id']}: 目録に version が無い。"
                        "URLの `?v=` が付かず、差し替えたDBが古いキャッシュと混ざる")
        if not (e.get("from") and e.get("to")):
            report.note(f"{e['id']}: 収録範囲が空（発言がまだ無い期間）")

    if entries:
        worst = max(entries, key=lambda e: e["size"])
        report.check(
            worst["size"] <= max_size,
            f"最大 {worst['file']} {worst['size'] / 1e6:.0f} MB"
            f"（上限 {CACHE_LIMIT / 1e6:.0f} MB / 手前で落とす閾値 {max_size / 1e6:.0f} MB）"
            + ("" if worst["size"] <= max_size
               else " - 分割を細かくすること（build_db.py の --period）"))
    return manifest


def check_schema(report: Report, con: sqlite3.Connection, topics: list[dict]) -> bool:
    """**中身の量に関係なく成り立っていなければならないもの。**

    ★ スモークテストのしきい値（`--min-smoke`）の外に置いてある。
      発言が少ない期間でも、`speech_fts` の無いDB（`--no-fts` で作ってしまったもの）や
      争点語の入っていないDBを配ってはいけない。前者は検索が
      `no such table: speech_fts` で落ち、後者は `/topic/<id>` が全部0件になる。
      **どちらも「発言が少ないから引けない」とは別の壊れ方。**

    **件数の一致は求めない**（2026-08-21）。`topics.json` に語を足しても古い期間DBは
    作り直さなくてよい ＝ **DBのほうが少ないのが正常**で、足りない語はサイトが
    普通の検索経路（word / FTS）で出す（`build_db.stamp_indexed`）。
    代わりに**両方にある id は term も variants も一致していること**を見る。
    ここがずれると `/topic/<id>` が古い語のヒットを新しい語の名前で出す。

    戻り値は speech_fts があるかどうか（無ければ FTS のスモークを飛ばす）。
    """
    has_fts = bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'speech_fts'").fetchone())
    report.check(has_fts, "speech_fts がある")

    held = {i: (term, variants) for i, term, variants in read_topics(con)}
    report.check(bool(held), "争点語が入っている")
    wanted = {t["id"]: (t["term"], t["variants"]) for t in topics}

    mismatched = [f"id={i}: DB `{held[i][0]}` ≠ topics.json `{wanted[i][0]}`"
                  for i in sorted(held.keys() & wanted.keys()) if held[i] != wanted[i]]
    report.check(not mismatched,
                 f"両方にある争点語 {len(held.keys() & wanted.keys())}件の term/variants が一致"
                 + ("" if not mismatched else
                    " - " + " / ".join(mismatched[:3])
                    + "。**id を使い回して語を書き換えている。**"
                      "別の争点なら新しい id を振る。書き直しなら全期間を作り直す"))

    # 足りない語・余った語は事故ではない。**印を付けて配るだけ**（黙って通さない）
    if missing := sorted(wanted.keys() - held.keys()):
        report.note(f"このDBに無い争点語 {len(missing)}件（id={format_id_ranges(missing)}）"
                    " - 検索経路で出る。全期間を作り直すと topic_hit に載って速くなる")
    if stale := sorted(held.keys() - wanted.keys()):
        report.note(f"topics.json から消えた争点語 {len(stale)}件"
                    f"（id={format_id_ranges(stale)}）が残っている"
                    " - 引かれないので害は無い。作り直すと消える")
    return has_fts


def smoke_fts(report: Report, con: sqlite3.Connection) -> None:
    """3文字以上の経路。表があることは `check_schema()` が先に確かめている。"""
    row = con.execute(
        "SELECT s.rowid, s.body FROM speech_fts f JOIN speech s ON s.rowid = f.rowid"
        " WHERE speech_fts MATCH ? LIMIT 1", (f'"{SMOKE_FTS}"',)).fetchone()
    if not report.check(row is not None, f"FTS: `{SMOKE_FTS}` が引ける"):
        return
    report.check(SMOKE_FTS in row[1],
                 f"FTS: 引けた発言 rowid={row[0]} の本文に `{SMOKE_FTS}` が入っている")


def smoke_word(report: Report, con: sqlite3.Connection) -> None:
    """2文字の経路。ここが空だと `年金` `増税` が**原理的に引けない**（FTS が届かない）。"""
    row = con.execute("SELECT id, n_speeches FROM word WHERE term = ?",
                      (SMOKE_WORD,)).fetchone()
    if not report.check(row is not None, f"2文字語: `{SMOKE_WORD}` が索引にある"):
        return
    word_id, n_speeches = row
    hit = con.execute(
        "SELECT s.rowid, s.body FROM word_hit h JOIN speech s ON s.rowid = h.speech_rowid"
        " WHERE h.word_id = ? ORDER BY h.speech_rowid DESC LIMIT 1", (word_id,)).fetchone()
    if not report.check(hit is not None, f"2文字語: `{SMOKE_WORD}` から発言を引ける"):
        return
    report.check(SMOKE_WORD in hit[1],
                 f"2文字語: 引けた発言 rowid={hit[0]} の本文に `{SMOKE_WORD}` が入っている")
    # `word.n_speeches` は複数語検索の起点を選ぶのに使う。ずれると重いほうから引く
    actual = con.execute("SELECT COUNT(*) FROM word_hit WHERE word_id = ?",
                         (word_id,)).fetchone()[0]
    report.check(n_speeches == actual,
                 f"2文字語: `{SMOKE_WORD}` の n_speeches {n_speeches:,} = word_hit {actual:,}")


def smoke_topic(report: Report, con: sqlite3.Connection) -> None:
    """争点語の経路。**`topic_hit` が本当にその語の発言を指しているかを見る。**

    件数は正しいまま中身だけ入れ替わる壊れ方をするので、引けた発言の本文に
    その語が入っているかまで見る。**代表語はDBから採る**ので、`topics.json` が
    先に進んでいてもここは成立する（両者の照合は `check_schema()` の仕事）。
    """
    row = con.execute("SELECT id, term, variants, n_speeches FROM topic"
                      " ORDER BY n_speeches DESC, id LIMIT 1").fetchone()
    if not report.check(row is not None and row[3] > 0,
                        "争点語: この期間で引ける語がある"):
        return
    topic_id, term, variants_json, n_speeches = row
    forms = [term, *json.loads(variants_json or "[]")]
    hit = con.execute(
        "SELECT s.rowid, s.body FROM topic_hit h JOIN speech s ON s.rowid = h.speech_rowid"
        " WHERE h.topic_id = ? ORDER BY h.speech_rowid DESC LIMIT 1", (topic_id,)).fetchone()
    if not report.check(hit is not None, f"争点語: `{term}`（id={topic_id}）から発言を引ける"):
        return
    report.check(
        any(form in hit[1] for form in forms),
        f"争点語: 引けた発言 rowid={hit[0]} の本文に `{term}` が入っている"
        " - 入っていなければ topic_hit と topic の対応が壊れている（作り直しが要る）")
    actual = con.execute("SELECT COUNT(*) FROM topic_hit WHERE topic_id = ?",
                         (topic_id,)).fetchone()[0]
    report.check(n_speeches == actual,
                 f"争点語: `{term}` の n_speeches {n_speeches:,} = topic_hit {actual:,}")


def check_monthly(report: Report, con: sqlite3.Connection) -> None:
    """月別の集計（検索結果のグラフ）が乗っている前提を見る。

    ブラウザは**日付では GROUP BY せず、`rowid` の範囲で月に割る**
    （`site/src/lib/query.ts` の `monthlyQuery`）。日付でまとめると当たった発言の
    行を1件ずつ読みに行くことになり、HTTP Range 越しでは桁が変わるため。

    その代わり、成り立っていないと**黙って別の月に足される**前提が2つある:

      1. `speech.rowid` が日付の昇順であること（`build_db.py` の `load()`）
      2. 月の先頭 rowid を `idx_speech_date` の seek で採れること

    件数の合計は正しいまま月の割り当てだけがずれるので、
    **利用者にも運営にも気づく手が無い。** ここで実物と突き合わせておく。
    """
    has_index = bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'idx_speech_date'").fetchone())
    if not report.check(has_index, "idx_speech_date がある（月境界の seek に要る）"):
        return

    # 1. rowid の昇順 = 日付の昇順
    out_of_order = con.execute(
        "SELECT COUNT(*) FROM (SELECT date, LAG(date) OVER (ORDER BY rowid) AS prev"
        " FROM speech) WHERE prev > date").fetchone()[0]
    report.check(out_of_order == 0,
                 f"rowid の昇順が日付の昇順（逆転 {out_of_order:,}件）"
                 " - 逆転すると月別の集計が別の月に混ざる")

    # 2. seek で採った境界 == 実際の最小 rowid
    truth = con.execute(
        "SELECT substr(date, 1, 7) AS m, MIN(rowid) FROM speech GROUP BY m ORDER BY m").fetchall()
    if not truth:
        return
    seek = [con.execute("SELECT rowid FROM speech WHERE date >= ? ORDER BY date LIMIT 1",
                        (f"{month}-01",)).fetchone()[0] for month, _ in truth]
    report.check(seek == [at for _, at in truth],
                 f"月の先頭 rowid が seek と一致（{len(truth)}か月）")


def check_db(report: Report, path: Path, period: str, entry: dict | None,
             rule: str, topics: list[dict],
             page_size: int, min_smoke: int) -> None:
    report.section(f"{path.name}（{path.stat().st_size / 1e6:.0f} MB）")

    # 目録との突き合わせ。**目録を先に書いてからDBを作り直す**とここで落ちる
    # （サイトは目録の version を `?v=` に付けるので、ずれると古い世代を握り続ける）
    if entry is None:
        report.fail(f"{period}: 手元にDBがあるのに目録に載っていない")
    else:
        size, version = path.stat().st_size, file_version(path)
        report.check(entry["size"] == size,
                     f"目録のサイズ {entry['size']:,} = 実物 {size:,} バイト")
        report.check(entry["version"] == version,
                     f"目録の version {entry['version']} = 実物の指紋 {version}")

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        # quick_check はページの構造を見る（integrity_check と違って索引と表の
        # 突き合わせはしないぶん速い）。転送が切れたDBはここで落ちる
        rows = con.execute("PRAGMA quick_check").fetchall()
        report.check(rows == [("ok",)], f"PRAGMA quick_check: {rows[0][0] if rows else '無応答'}")

        actual_page = con.execute("PRAGMA page_size").fetchone()[0]
        report.check(actual_page == page_size, f"page_size = {actual_page}（期待 {page_size}）")
        # WAL のままだと本体の外に -wal が要る。**HTTP Range で1ファイルしか
        # 読まない sql.js-httpvfs から開けない**（finalize() が畳んでいるはず）
        journal = con.execute("PRAGMA journal_mode").fetchone()[0]
        report.check(journal.lower() != "wal", f"journal_mode = {journal}（WAL でない）")

        meta = dict(con.execute("SELECT key, value FROM meta"))
        report.check(meta.get("period") == period,
                     f"meta.period = {meta.get('period')!r}（ファイル名は {period!r}）")
        report.check(meta.get("period_rule") == rule,
                     f"meta.period_rule = {meta.get('period_rule')!r}（目録は {rule!r}）")

        # **目録の `topics` が実物と合っていること。** サイトはこれを見て
        # 「この語を topic_hit で引いてよいか」を決める（`build_db.stamp_indexed`）。
        # ずれると、持っていない語を topic_hit で引いて**0件**になる
        if entry is not None:
            held = read_topics(con)
            listed = entry.get("topics") or {}
            report.check(
                parse_id_ranges(listed.get("ids", "")) == [i for i, _, _ in held]
                and listed.get("fp") == topics_fingerprint(held),
                f"目録の争点語の記載が実物と一致（{len(held)}件）"
                " - ずれていたら build_db.py --manifest-only で目録を作り直す")

        covers = (meta.get("from", ""), meta.get("to", ""))
        for key, value in zip(("from", "to"), covers):
            if value:
                report.check(period_of(value, rule) == period,
                             f"meta.{key} = {value} がこの期間に収まっている")
        if entry is not None:
            report.check((entry.get("from", ""), entry.get("to", "")) == covers,
                         f"目録の収録範囲が meta と一致（{covers[0] or 'なし'} 〜 {covers[1] or 'なし'}）")

        # --- 骨格（発言の量に関係なく成り立つもの） ---
        has_fts = check_schema(report, con, topics)
        check_monthly(report, con)

        # --- スモークテスト（3経路） ---
        indexed = con.execute(
            "SELECT COUNT(*) FROM speech WHERE speaker_kind = '議員'").fetchone()[0]
        if indexed < min_smoke:
            # 半期の開始直後はここに来る（1月1日・7月1日）。**黙って飛ばさない**。
            # 飛ばすのは「代表語を引く」ところだけで、表の有無は上で見ている
            report.note(f"議員の発言 {indexed:,}件 < {min_smoke:,}件。"
                        "代表語のスモークテストは飛ばす（半期の開始直後はここに来る）")
            return
        report.note(f"議員の発言 {indexed:,}件。3経路を1件ずつ引く")
        if has_fts:
            smoke_fts(report, con)
        smoke_word(report, con)
        smoke_topic(report, con)
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dist", type=Path, default=DIST_DIR, help="期間DBと目録の場所")
    parser.add_argument("--topics", type=Path, default=TOPICS_PATH, help="争点語のリスト")
    parser.add_argument("--id", action="append", metavar="YYYYH1",
                        help="検証する期間ID。複数指定可。既定は手元にあるDB全部")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--max-size", type=float, default=DEFAULT_MAX_SIZE,
                        help=f"この大きさを超えたら落とす（既定 {DEFAULT_MAX_SIZE / 1e6:.0f} MB）")
    parser.add_argument("--min-smoke", type=int, default=SMOKE_MIN_SPEECHES,
                        help="この件数未満の期間では代表語のスモークテストを飛ばす")
    args = parser.parse_args()

    report = Report()
    check_split_rule(report, DEFAULT_PERIOD)
    manifest = check_manifest(report, args.dist, DEFAULT_PERIOD, args.max_size)
    if manifest is None:
        sys.exit("★ 目録を読めないので検証できない。配らないこと")

    rule = manifest.get("period", DEFAULT_PERIOD)
    entries = {e["id"]: e for e in manifest.get("databases", [])}

    topics = load_topics(args.topics)
    if not topics:
        sys.exit(f"★ {args.topics} を読めない。争点語を照合できないので配らないこと")

    found = {p.stem.removeprefix("kokkai-"): p
             for p in sorted(args.dist.glob("kokkai-*.db"))
             if re.fullmatch(r"\d{4}(H[12])?", p.stem.removeprefix("kokkai-"))}
    targets = sorted(set(args.id) & set(found)) if args.id else sorted(found)
    if args.id:
        missing = sorted(set(args.id) - set(found))
        if missing:
            sys.exit(f"★ 指定された期間のDBが手元に無い: {' '.join(missing)}")
    if not targets:
        sys.exit(f"★ {args.dist} に期間DBが無い。先に build_db.py --split を回すこと")

    # **手元に無い期間は検証していない**ことを明示する。日次更新は触った期間しか
    # 置かないので、ここに出ない期間は前回の目録の記載を引き継いだだけ
    inherited = sorted(set(entries) - set(targets))
    if inherited:
        print(f"\n（検証しない期間: {' '.join(inherited)} - 手元にDBが無い。"
              "目録の記載を引き継いだもの）")

    for period in targets:
        check_db(report, found[period], period, entries.get(period), rule,
                 topics, args.page_size, args.min_smoke)

    print()
    if report.failures:
        for message in report.failures:
            print(f"× {message}")
        sys.exit(f"\n★ {len(report.failures)}件の検証に失敗した。**配らないこと**")
    print(f"○ {len(targets)}期間（{' '.join(targets)}）すべて検証を通った")


if __name__ == "__main__":
    main()
