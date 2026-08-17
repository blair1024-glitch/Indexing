"""評分層。

``engine.py`` 以單一的通用評分函式處理全部七個評分項目
（Management / Moat / ROE / 穩定性 / 財務安全 / FCF / 安全邊際）——
它們的差異全在 ``config/scoring.yaml`` 的門檻帶，不在程式邏輯，
因此沒有為每個項目各寫一個模組。Management 與 Moat 額外支援人工覆寫，
這部分由 ``engine.score_component`` 的 ``allow_override`` 參數處理。

``valuation.py`` 負責內在價值估算與安全邊際。
"""

from .engine import (
    CompanyScore,
    grade_for,
    score_company,
)
from .valuation import Valuation, ValuationMethod, estimate_valuation

__all__ = [
    "CompanyScore",
    "Valuation",
    "ValuationMethod",
    "estimate_valuation",
    "grade_for",
    "score_company",
]
