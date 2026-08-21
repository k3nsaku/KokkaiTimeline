"""**配ったあと**の配信物を、利用者が実際に引くURLごしに検算する。

`verify_dist.py` が見るのは「配る前の手元のファイル」。こちらが見るのは
「公開URLが返すもの」。**見ている対象が違うので両方いる。**
手元のDBが正しくても、上げ方とエッジの設定でいくらでも壊れる。

## なぜ要るか（2026-08-19 に踏んだ）

`kokkai-2026H2.db` だけ **Content-Type が落ちた**状態で配られていた。
原因は `aws s3 cp` に `--content-type` を渡しておらず、**ランナーの mime
データベース任せ**になっていたこと。8/12 に上げた11本は
`application/vnd.sqlite3` が付き、8/18 に日次が差し替えた1本だけ付かなかった。

手元のDBは正常なので `verify_dist.py` は通る。**配ったあとにURLを叩く以外に
気づく手が無い。** 同じ日に Chrome の1つの窓口だけが 2026H2 で
`SQLite: file is not a database` を出しており（[docs/ROADMAP.md]）、
因果は証明できていないが、**次に踏んだとき「サーバか、ブラウザか」を
推測せずに判定できる**ようにしておく。

## 見るもの（期間DB1本につき）

    HEAD          200 / Content-Length が目録の size と一致 /
                  Content-Type が application/vnd.sqlite3 /
                  Accept-Ranges: bytes / Access-Control-Allow-Origin
    Range 0-99    206 / Content-Range が size と一致 /
                  先頭16バイトが SQLite のマジック / page_size が 8192

`Accept-Ranges` は**ブラウザからは見えない**（R2 の CORS が公開していないので
`getAllResponseHeaders()` に出ない）。ここでしか確かめられない。

## ★ `Origin` を必ず付ける（2026-08-21 に踏んだ）

**この検算そのものが配信を壊していた。**

R2 は `Origin` の付いた要求にだけ CORS ヘッダを返し、応答に `Vary: Origin` を
付けない。日次は「配る → 検算する」の順なので、上げ直した直後に
**`Origin` 無しの応答がエッジに載り、そこから `max-age` の間（目録なら5分）
すべてのブラウザで目録が読めなくなる**（`Failed to fetch` ＝ サイトが丸ごと止まる）。

だから `Origin` を付けて引き、**返ってきた CORS ヘッダまで見る**。
ブラウザと同じ形で引くことが、発生源を消すことと検査を増やすことの両方になる。
サイト側にも保険がある（`site/src/lib/db.ts` は目録の取得に失敗したら
URLを変えて1度だけ引き直す）。

`--expect-local` を付けると、配信されている目録を手元の
`data/dist/manifest.json` とも突き合わせる。食い違っていたら、上げ損ねたか、
エッジが古い世代を握っている。**日次更新の直後だけ意味がある**
（手元の作業コピーは配信より古いのが普通なので、既定では見ない）。

## 使い方

    PUBLIC_DB_BASE=https://db.example.org python scripts/verify_published.py
    python scripts/verify_published.py --base https://db.example.org --id 2026H2

**日次更新では「配る」の直後に走らせている。** 配ってしまったあとなので
止められない — 落ちたら運営に知らせるための関門（翌朝これで気づく）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DIST_DIR = Path(__file__).resolve().parent.parent / "data" / "dist"

#: DBに期待する Content-Type。**上げるときに明示している値と揃える**
#: （.github/workflows/daily.yml の `--content-type`）。
EXPECT_CONTENT_TYPE = "application/vnd.sqlite3"

#: **ブラウザのふりをするための Origin。** サイトの正本は site/astro.config.mjs の `site`。
#:
#: ★ 飾りではない。R2 は **`Origin` の付いた要求にだけ** CORS ヘッダを返し、
#:   応答に `Vary: Origin` が付かない。だから **`Origin` 無しで引くと、CORS ヘッダの
#:   無い応答がエッジに載り、そこから5分間すべてのブラウザで目録が読めなくなる**
#:   （`max-age=300`）。検算そのものが配信を壊していた。2026-08-21 に踏んだ。
DEFAULT_ORIGIN = "https://kokkai-timeline.com"

#: 配信DBの page_size。sql.js-httpvfs の requestChunkSize と揃っている必要がある
#: （site/src/lib/db.ts の CHUNK）。
EXPECT_PAGE_SIZE = 8192

SQLITE_MAGIC = b"SQLite format 3\x00"

#: 一時的な失敗で日次を落とさないための再試行。**中身の誤りは再試行しない**
ATTEMPTS = 3
RETRY_WAIT = 3.0

#: ★ 名乗ること。**既定の `Python-urllib/3.x` は Cloudflare に 403 で弾かれる**
#: （2026-08-19 実測。curl や普通のブラウザのUAなら通る）。ここを消すと
#: 「配信が壊れている」ではなく「検算が届かない」で落ちる
USER_AGENT = "kokkai-timeline-verify/1 (+https://kokkai-timeline.com)"


class Report:
    """失敗を集めて最後にまとめて出す。**1件目で止めない**（全部見たい）。"""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, message: str) -> bool:
        if not ok:
            self.failures.append(message)
        return ok


def request(url: str, method: str = "GET", headers: dict[str, str] | None = None,
            origin: str = DEFAULT_ORIGIN):
    """`(status, headers, body)` を返す。206 も 200 も例外にしない。

    **`Origin` を必ず付ける。** ブラウザと同じ形で引くためで、外すと
    CORS ヘッダの無い応答をエッジに載せてしまう（`DEFAULT_ORIGIN` の注記）。
    """
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        req = urllib.request.Request(
            url, method=method,
            headers={"User-Agent": USER_AGENT, "Origin": origin, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read()
        except urllib.error.HTTPError as e:
            # 4xx/5xx は中身を見て判断したいので、そのまま返す
            return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt + 1 < ATTEMPTS:
                time.sleep(RETRY_WAIT)
    raise SystemExit(f"★ {url} に到達できない（{ATTEMPTS}回）: {last}")


def check_cors(report: Report, label: str, headers: dict[str, str], origin: str) -> None:
    """**ブラウザから読めるか。**

    R2 は `Origin` の付いた要求にだけ CORS ヘッダを返し、応答に `Vary: Origin` を
    付けない。**`Origin` 無しの要求が先にエッジのキャッシュを埋めると、
    CORS ヘッダの無い応答が全ブラウザに配られる。** 目録でこれが起きると
    サイトは丸ごと止まる（どの期間DBも引けない）。
    """
    allow = headers.get("access-control-allow-origin")
    report.check(
        allow in (origin, "*"),
        f"{label}: Access-Control-Allow-Origin が {allow or '（無し）'}（期待 {origin}）"
        " - 無いなら Origin 無しの要求がキャッシュを埋めている。"
        "max-age が切れるまでブラウザから読めない")


def check_database(report: Report, base: str, entry: dict, origin: str) -> None:
    period, size = entry["id"], entry["size"]
    url = f"{base}/{entry['file']}"
    if entry.get("version"):
        url += f"?v={entry['version']}"

    status, headers, _ = request(url, method="HEAD", origin=origin)
    if not report.check(status == 200, f"{period}: HEAD が {status}（200 でない）{url}"):
        return
    check_cors(report, period, headers, origin)

    length = headers.get("content-length")
    report.check(length == str(size),
                 f"{period}: Content-Length が目録と違う（配信 {length} / 目録 {size}）")
    report.check(headers.get("content-type") == EXPECT_CONTENT_TYPE,
                 f"{period}: Content-Type が {headers.get('content-type') or '（無し）'}。"
                 f"{EXPECT_CONTENT_TYPE} で上げ直すこと")
    report.check(headers.get("accept-ranges") == "bytes",
                 f"{period}: Accept-Ranges が {headers.get('accept-ranges') or '（無し）'}。"
                 "バイト単位で取れないとDB全体を1つとして読みに行く")

    status, headers, body = request(url, headers={"Range": "bytes=0-99"}, origin=origin)
    if not report.check(status == 206,
                        f"{period}: Range 要求に {status} で答えている（206 でない）"):
        return
    report.check(headers.get("content-range") == f"bytes 0-99/{size}",
                 f"{period}: Content-Range が {headers.get('content-range')}（size {size} と不一致）")
    if not report.check(body[:16] == SQLITE_MAGIC,
                        f"{period}: 先頭16バイトが SQLite ではない（{body[:16]!r}）"):
        return
    # ヘッダ16〜17バイト目が page_size。1 は 65536 の意味
    raw = int.from_bytes(body[16:18], "big")
    page_size = 65536 if raw == 1 else raw
    report.check(page_size == EXPECT_PAGE_SIZE,
                 f"{period}: page_size が {page_size}（期待 {EXPECT_PAGE_SIZE}）")


def check_manifest(report: Report, base: str, local: Path | None,
                   origin: str) -> dict | None:
    url = f"{base}/manifest.json"
    status, headers, body = request(url, origin=origin)
    if not report.check(status == 200, f"目録: {status} が返る {url}"):
        return None
    report.check((headers.get("content-type") or "").startswith("application/json"),
                 f"目録: Content-Type が {headers.get('content-type') or '（無し）'}")
    # **ここが落ちるとサイトは丸ごと止まる。** 目録を読めなければ期間DBに辿り着けない
    check_cors(report, "目録", headers, origin)

    try:
        served = json.loads(body)
    except json.JSONDecodeError as e:
        report.check(False, f"目録: JSON として読めない（{e}）")
        return None

    # 手元と違うなら、上げ損ねたかエッジが古い世代を握っている。
    # **どちらでも、サイトは存在しない世代を引きに行く**
    #
    # ★ 見るのは日次のときだけ（`--expect-local`）。手元の作業コピーは
    #   配信より古いのが普通なので、既定で見ると毎回 × が出て意味を失う
    if local is not None:
        if not local.exists():
            report.check(False, f"目録: 手元に {local} が無いので突き合わせられない")
        else:
            mine = json.loads(local.read_text(encoding="utf-8"))
            report.check(
                served == mine,
                "目録: 配信されているものが手元と違う（上げ損ねたか、エッジが古い）")
    return served


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=os.environ.get("PUBLIC_DB_BASE"),
                        help="配信元のURL（既定は環境変数 PUBLIC_DB_BASE）")
    parser.add_argument("--dist", type=Path, default=DIST_DIR, help="手元の配信物の場所")
    parser.add_argument("--expect-local", action="store_true",
                        help="配信されている目録が手元のものと同じか見る（日次更新の直後用）")
    parser.add_argument("--id", action="append", metavar="YYYYH1",
                        help="検算する期間ID。複数指定可。既定は目録にある全部")
    parser.add_argument("--origin", default=os.environ.get("PUBLIC_SITE_ORIGIN", DEFAULT_ORIGIN),
                        help="ブラウザのふりをするときの Origin"
                             f"（既定 {DEFAULT_ORIGIN}）。**外さないこと**")
    args = parser.parse_args()

    if not args.base:
        sys.exit("★ 配信元のURLが分からない（--base か PUBLIC_DB_BASE）")
    base = args.base.rstrip("/")

    report = Report()
    local = args.dist / "manifest.json" if args.expect_local else None
    manifest = check_manifest(report, base, local, args.origin)
    if manifest is None:
        for message in report.failures:
            print(f"× {message}")
        sys.exit("\n★ 目録を引けないので検算できない")

    entries = manifest.get("databases", [])
    if args.id:
        wanted = set(args.id)
        missing = sorted(wanted - {e["id"] for e in entries})
        if missing:
            sys.exit(f"★ 目録に無い期間を指定している: {' '.join(missing)}")
        entries = [e for e in entries if e["id"] in wanted]

    print(f"{base} を検算する（{len(entries)}期間・Origin {args.origin}）")
    for entry in entries:
        check_database(report, base, entry, args.origin)

    print()
    if report.failures:
        for message in report.failures:
            print(f"× {message}")
        sys.exit(f"\n★ {len(report.failures)}件の検算に失敗した。**もう配ってある**ので、"
                 "上げ直すかエッジをパージすること")
    print(f"○ {len(entries)}期間（{' '.join(e['id'] for e in entries)}）すべて検算を通った")


if __name__ == "__main__":
    main()
