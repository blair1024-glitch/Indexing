"""資料來源層。

所有對外部 API 的存取都集中在這裡，並且負責把回傳值轉成帶 provenance 的
``DataPoint``。上層（metrics / scoring）不知道資料從哪來，只知道每個數字
都帶著出處，或明確標示為缺料。
"""

from .base import FetchError, HttpClient, SourceUnavailable
from .cache import DiskCache

__all__ = ["DiskCache", "FetchError", "HttpClient", "SourceUnavailable"]
