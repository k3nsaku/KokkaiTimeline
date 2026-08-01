"""国会会議録検索システム API クライアント。

標準ライブラリのみで動く。依存を持たないのは GitHub Actions 側で
pip install のステップを不要にするため（運用工数を増やさない）。

API 仕様: https://kokkai.ndl.go.jp/api.html
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

BASE_URL = "https://kokkai.ndl.go.jp/api"

# 仕様書が「数秒間隔を空ける」ことを求めているため、機械的に必ず待つ。
# 短縮しないこと。ブロックされると Phase 0 どころではなくなる。
REQUEST_INTERVAL_SEC = 3.0

# speech / meeting_list は最大 100、meeting は最大 10
MAX_RECORDS = {"speech": 100, "meeting_list": 100, "meeting": 10}

USER_AGENT = "KokkaiTimeline/0.1 (personal research project; contact via GitHub)"

logger = logging.getLogger(__name__)


class NDLAPIError(RuntimeError):
    """API がエラーレスポンスを返した場合。"""


def _build_url(endpoint: str, params: dict[str, Any]) -> str:
    query = {k: v for k, v in params.items() if v is not None and v != ""}
    query.setdefault("recordPacking", "json")
    encoded = urllib.parse.urlencode(query, encoding="utf-8")
    if len(encoded) > 2000:
        raise ValueError(f"クエリ文字列が仕様上限の2000バイトを超えている: {len(encoded)}")
    return f"{BASE_URL}/{endpoint}?{encoded}"


def fetch(
    endpoint: str,
    params: dict[str, Any],
    *,
    retries: int = 4,
    interval: float = REQUEST_INTERVAL_SEC,
) -> dict[str, Any]:
    """API を1回叩いて JSON を返す。呼び出し後に必ず interval 秒待つ。"""
    url = _build_url(endpoint, params)
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            backoff = interval * (2 ** (attempt - 1))
            logger.warning(
                "リクエスト失敗 (%d/%d): %s — %.0f秒待って再試行", attempt, retries, exc, backoff
            )
            time.sleep(backoff)
    else:
        raise NDLAPIError(f"{retries}回試行して失敗した: {url}") from last_error

    time.sleep(interval)

    # エラーは HTTP 200 で JSON 本体に入ってくることがある
    if "message" in payload and "numberOfRecords" not in payload:
        raise NDLAPIError(f"API エラー {payload.get('details', '')}: {payload['message']}")

    return payload


def iter_speeches(
    *,
    limit: int | None = None,
    start_record: int = 1,
    interval: float = REQUEST_INTERVAL_SEC,
    **search_params: Any,
) -> Iterator[dict[str, Any]]:
    """発言単位 API をページングしながら発言レコードを順に返す。

    search_params には from / until / nameOfHouse / any / speaker などを渡す。
    仕様上、検索条件がひとつも無いと 19007 エラーになる。

    start_record は中断からの再開に使う。取得済みの件数 + 1 を渡すと続きから流れる。
    """
    if not any(v for v in search_params.values()):
        raise ValueError("検索条件を最低ひとつ指定すること（API仕様 19007）")

    per_page = MAX_RECORDS["speech"]
    yielded = 0
    total: int | None = None
    # start_record はループ内で next_position に更新されるので、
    # 再開位置は最初に控えておく（進捗表示に使う）
    offset = start_record - 1

    while True:
        if limit is not None:
            per_page = min(MAX_RECORDS["speech"], limit - yielded)
            if per_page <= 0:
                return

        payload = fetch(
            "speech",
            {**search_params, "startRecord": start_record, "maximumRecords": per_page},
            interval=interval,
        )

        if total is None:
            total = int(payload.get("numberOfRecords", 0))
            logger.info("該当件数: %s件", f"{total:,}")

        records = payload.get("speechRecord") or []
        for record in records:
            yield record
            yielded += 1

        # 再開時も進捗が分かるよう、この回で取った件数ではなく通算の位置を出す
        logger.info("取得済み %s / %s", f"{offset + yielded:,}", f"{total:,}")

        next_position = payload.get("nextRecordPosition")
        if not next_position or not records:
            return
        start_record = int(next_position)


def count_speeches(*, interval: float = REQUEST_INTERVAL_SEC, **search_params: Any) -> int:
    """該当件数だけを取得する（1リクエストで済ませる）。"""
    payload = fetch(
        "speech",
        {**search_params, "startRecord": 1, "maximumRecords": 1},
        interval=interval,
    )
    return int(payload.get("numberOfRecords", 0))
