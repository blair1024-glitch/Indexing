"""HTTP 存取的共用基礎設施。

刻意的設計選擇：

* **失敗要吵。** 抓不到就拋 ``SourceUnavailable``，不回傳空資料讓上層以為
  「這家公司沒有負債」。缺料與零值必須能被區分。
* **重試只針對暫時性錯誤。** 4xx（除了 429）不重試——那是請求本身有問題，
  重試只是浪費時間並可能觸發封鎖。
* **schema 變動要被發現。** 解析欄位時若關鍵欄位不存在，記錄下來並回報，
  而不是預設 0。台灣的財報 API 欄位名稱偶爾會調整。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Sequence

import requests

from ..models import DataPoint, utcnow
from .cache import DiskCache


class FetchError(Exception):
    """單次請求失敗。"""


class SourceUnavailable(Exception):
    """整個來源不可用（網路封鎖、缺 token、端點下線）。"""


@dataclass
class HttpClient:
    """帶重試、退避與快取的 HTTP 客戶端。"""

    timeout: float = 30.0
    max_retries: int = 3
    backoff: Sequence[float] = (2, 4, 8)
    user_agent: str = "buffett00929/0.1"
    cache: DiskCache | None = None
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.session.headers.update({"User-Agent": self.user_agent})

    def get_json(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        *,
        use_cache: bool = True,
    ) -> Any:
        if use_cache and self.cache:
            entry = self.cache.get(url, params)
            if entry is not None:
                return entry.payload

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.get(
                    url, params=params, headers=headers, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise FetchError(f"{url} 回傳非 JSON 內容：{exc}") from exc
                    if self.cache:
                        self.cache.set(url, params, payload)
                    return payload

                # 4xx（除 429）是請求本身的問題，重試沒有意義。
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise FetchError(
                        f"{url} 回傳 HTTP {response.status_code}"
                        f"（不重試：用戶端錯誤）{response.text[:200]}"
                    )
                last_error = FetchError(f"{url} 回傳 HTTP {response.status_code}")

            if attempt < self.max_retries - 1:
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                time.sleep(delay)

        raise FetchError(f"{url} 重試 {self.max_retries} 次後仍失敗：{last_error}")


# --------------------------------------------------------------------------
# 解析輔助
# --------------------------------------------------------------------------


@dataclass
class SchemaWatch:
    """記錄解析過程中遇到的欄位缺失，讓 API schema 變動能被看見而非被吞掉。"""

    source: str
    unknown_fields: list[str] = field(default_factory=list)

    def note_missing(self, field_name: str) -> None:
        entry = f"{self.source}: 找不到欄位 {field_name}"
        if entry not in self.unknown_fields:
            self.unknown_fields.append(entry)

    @property
    def has_issues(self) -> bool:
        return bool(self.unknown_fields)


def parse_number(raw: Any) -> float | None:
    """把 API 回傳的字串數字轉成 float。

    台灣的財報 API 常見格式：``"1,234,567"``、``"-"``、``""``、``"(1,234)"``（負數）。
    無法解析時回傳 None——**不是 0**。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text or text in {"-", "--", "N/A", "n/a", "NA", "無", "不適用"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = text.replace(",", "").replace("%", "").replace("元", "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def pick(record: dict, candidates: Iterable[str], watch: SchemaWatch | None = None) -> Any:
    """從多個候選欄位名稱中取第一個存在的值。

    台灣的 OpenAPI 欄位名稱在不同端點/年度間不完全一致，
    所以每個邏輯欄位都給一組候選名稱。全部落空時記錄到 SchemaWatch。
    """
    for name in candidates:
        if name in record and record[name] not in (None, ""):
            return record[name]
    if watch is not None:
        watch.note_missing(" / ".join(candidates))
    return None


def make_point(
    record: dict,
    candidates: Iterable[str],
    *,
    source: str,
    period: str | None = None,
    as_of: date | None = None,
    fetched_at: datetime | None = None,
    multiplier: float = 1.0,
    url: str | None = None,
    watch: SchemaWatch | None = None,
) -> DataPoint:
    """從一筆 API 記錄取出欄位並包成帶 provenance 的 DataPoint。"""
    raw = pick(record, candidates, watch)
    value = parse_number(raw)
    if value is None:
        names = " / ".join(candidates)
        return DataPoint.missing(f"{source} 未提供 {names}")
    return DataPoint.of(
        value * multiplier,
        source,
        as_of=as_of,
        period=period,
        fetched_at=fetched_at or utcnow(),
        url=url,
    )


def roc_to_ad(roc_date: str) -> date | None:
    """民國年轉西元。例：``"1150817"`` 或 ``"115/08/17"`` → 2026-08-17。"""
    if not roc_date:
        return None
    text = str(roc_date).strip().replace("/", "").replace("-", "")
    if not text.isdigit() or len(text) not in (6, 7):
        return None
    # 6 碼為 YYMMDD（民國百年前），7 碼為 YYYMMDD。
    split = 3 if len(text) == 7 else 2
    try:
        year = int(text[:split]) + 1911
        month = int(text[split : split + 2])
        day = int(text[split + 2 :])
        return date(year, month, day)
    except ValueError:
        return None


__all__ = [
    "FetchError",
    "HttpClient",
    "SchemaWatch",
    "SourceUnavailable",
    "make_point",
    "parse_number",
    "pick",
    "roc_to_ad",
]
