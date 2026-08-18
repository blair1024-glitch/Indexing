"""磁碟快取。

用途是避免每日排程重複打相同端點，不是為了在來源掛掉時拿舊資料頂替。
規格第二節明訂「禁止使用過時資料當作最新數據」，因此：

* 快取過期就是過期，不會被當成有效回應回傳；
* 每筆快取都記錄寫入時間，上層可據此判斷資料新鮮度。

## 不變期別的永久快取

上面那條規則針對的是**當期**資料。歷史財報是另一回事：
2020Q1 的綜合損益表在 2020Q1 結束、財報公告並完成更正期之後就固定了，
再抓一百次也是同一份數字。對這種資料設 TTL 只會讓每日排程重複回補十年歷史，
既慢又容易被來源端擋。

因此快取分兩種，由呼叫端在請求時明確宣告：

* ``immutable=False``（預設）——當期資料，套用 TTL，過期即失效。
* ``immutable=True``——已完結期別的歷史資料，不過期。

判斷「這一期是否已完結」是呼叫端的責任（見 ``sources/periods.py``），
快取層只負責忠實執行。宣告錯誤會讓過時資料被當成最新資料，
所以 ``immutable`` 只能用在期別已經結束、且該期報告已公告的請求上。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SAFE_NAMESPACE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass
class CacheEntry:
    payload: Any
    stored_at: float
    url: str
    immutable: bool = False

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

    def _path(self, url: str, params: dict | None, namespace: str | None = None) -> Path:
        directory = self.directory
        if namespace:
            # 分目錄存放，讓「歷史財報」這種值得 commit 進 repo 的快取
            # 能和每日就丟掉的當期快取分開管理。
            directory = directory / _SAFE_NAMESPACE.sub("_", namespace)
        return directory / f"{self._key(url, params)}.json"

    def get(
        self,
        url: str,
        params: dict | None = None,
        *,
        namespace: str | None = None,
        immutable: bool = False,
    ) -> CacheEntry | None:
        """回傳可用的快取。

        當期資料過期或不存在一律回傳 None；
        ``immutable=True`` 的請求則不看 TTL——那一期的數字不會再變。
        """
        if not self.enabled:
            return None
        path = self._path(url, params, namespace)
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
            immutable=bool(raw.get("immutable", False)),
        )
        if immutable and entry.immutable:
            return entry
        if entry.age_seconds > self.ttl_seconds:
            return None
        return entry

    def set(
        self,
        url: str,
        params: dict | None,
        payload: Any,
        *,
        namespace: str | None = None,
        immutable: bool = False,
    ) -> None:
        if not self.enabled:
            return
        path = self._path(url, params, namespace)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(
                {
                    "payload": payload,
                    "stored_at": time.time(),
                    "url": url,
                    "immutable": immutable,
                },
                fh,
                ensure_ascii=False,
            )
        tmp.replace(path)


__all__ = ["CacheEntry", "DiskCache"]
