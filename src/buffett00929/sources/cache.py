"""磁碟快取。

用途是避免每日排程重複打相同端點，不是為了在來源掛掉時拿舊資料頂替。
規格第二節明訂「禁止使用過時資料當作最新數據」，因此：

* 快取過期就是過期，不會被當成有效回應回傳；
* 每筆快取都記錄寫入時間，上層可據此判斷資料新鮮度。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class CacheEntry:
    payload: Any
    stored_at: float
    url: str

    @property
    def age_seconds(self) -> float:
        return time.time() - self.stored_at


class DiskCache:
    def __init__(self, directory: Path | str, ttl_hours: float = 12, enabled: bool = True):
        self.directory = Path(directory)
        self.ttl_seconds = ttl_hours * 3600
        self.enabled = enabled
        if self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str, params: dict | None) -> str:
        raw = url + "?" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _path(self, url: str, params: dict | None) -> Path:
        return self.directory / f"{self._key(url, params)}.json"

    def get(self, url: str, params: dict | None = None) -> CacheEntry | None:
        """回傳未過期的快取；過期或不存在一律回傳 None。"""
        if not self.enabled:
            return None
        path = self._path(url, params)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
        entry = CacheEntry(
            payload=raw.get("payload"),
            stored_at=float(raw.get("stored_at", 0)),
            url=raw.get("url", url),
        )
        if entry.age_seconds > self.ttl_seconds:
            return None
        return entry

    def set(self, url: str, params: dict | None, payload: Any) -> None:
        if not self.enabled:
            return
        path = self._path(url, params)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {"payload": payload, "stored_at": time.time(), "url": url},
                fh,
                ensure_ascii=False,
            )
        tmp.replace(path)


__all__ = ["CacheEntry", "DiskCache"]
