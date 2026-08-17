"""櫃買中心 OpenAPI（上櫃公司）。

00929 追蹤的是「特選臺灣**上市上櫃**科技優息指數」，成分股包含上櫃公司，
只接證交所會漏掉這一段。櫃買的財報端點同樣源自公開資訊觀測站，
欄位命名與上市版本一致，因此直接沿用 ``TwseClient`` 的解析邏輯，
只換 base_url、端點路徑與來源標記。
"""

from __future__ import annotations

from .base import HttpClient
from .twse import TwseClient

SOURCE_PREFIX = "TPEx"


def make_tpex_client(http: HttpClient, config: dict) -> TwseClient:
    """建立上櫃資料客戶端。

    回傳的仍是 ``TwseClient``——兩者的差異純粹是設定，不是行為，
    硬做一個空的子類別只會增加閱讀成本。
    """
    return TwseClient(http=http, config=config, source_prefix=SOURCE_PREFIX)


__all__ = ["SOURCE_PREFIX", "make_tpex_client"]
