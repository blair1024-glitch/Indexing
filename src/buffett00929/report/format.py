"""報表數值格式化。

統一規則：

* 缺料一律顯示「資料不足」，**永遠不顯示 0 或 N/A**——
  0 會被讀成「這家公司真的是 0」，N/A 讀不出原因。
* 比率型指標的變動用 **pp（百分點）**，不用 %。
  毛利率從 45% 變 47% 是 +2pp，寫成 +2% 會被誤解為相對成長。
* 金額一律換算為「億元」，台股財報原始單位（元）的位數難以閱讀。
"""

from __future__ import annotations

from ..models import DataPoint

MISSING_TEXT = "資料不足"


def pct(point: DataPoint, digits: int = 1) -> str:
    """比率 → 百分比字串。"""
    if not point.is_available or point.value is None:
        return MISSING_TEXT
    return f"{point.value:.{digits}%}"


def pp(point: DataPoint, digits: int = 1) -> str:
    """比率的變動 → 百分點字串（帶正負號）。"""
    if not point.is_available or point.value is None:
        return MISSING_TEXT
    return f"{point.value * 100:+.{digits}f}pp"


def growth(point: DataPoint, digits: int = 1) -> str:
    """成長率 → 帶正負號的百分比。"""
    if not point.is_available or point.value is None:
        return MISSING_TEXT
    return f"{point.value:+.{digits}%}"


def money(point: DataPoint, digits: int = 1) -> str:
    """金額（元）→ 億元。"""
    if not point.is_available or point.value is None:
        return MISSING_TEXT
    return f"{point.value / 1e8:,.{digits}f} 億"


def num(point: DataPoint, digits: int = 2, suffix: str = "") -> str:
    if not point.is_available or point.value is None:
        return MISSING_TEXT
    return f"{point.value:,.{digits}f}{suffix}"


def multiple(point: DataPoint, digits: int = 2) -> str:
    return num(point, digits, suffix="x")


def score_text(value: float | None, digits: int = 1) -> str:
    if value is None:
        return MISSING_TEXT
    return f"{value:.{digits}f}"


def change_cell(point: DataPoint, is_percentage: bool) -> str:
    """財報變化表的 YoY／QoQ 欄位。"""
    return pp(point) if is_percentage else growth(point)


def value_cell(point: DataPoint, is_percentage: bool) -> str:
    """財報變化表的數值欄位。"""
    if not point.is_available or point.value is None:
        return MISSING_TEXT
    if is_percentage:
        return pct(point)
    # 大額數字換算為億元，小數字（EPS 等）維持原樣。
    if abs(point.value) >= 1e8:
        return money(point)
    return f"{point.value:,.2f}"


def provenance_text(point: DataPoint) -> str:
    """數據出處，供報表附註（規格第十九節：標示資料來源與日期）。"""
    if point.provenance is None:
        return point.unavailable_reason or MISSING_TEXT
    return point.provenance.describe()


def coverage_badge(coverage: float) -> str:
    if coverage >= 0.95:
        return "完整"
    if coverage >= 0.8:
        return "大致完整"
    if coverage >= 0.6:
        return "部分缺漏"
    return "嚴重缺漏"


__all__ = [
    "MISSING_TEXT",
    "change_cell",
    "coverage_badge",
    "growth",
    "money",
    "multiple",
    "num",
    "pct",
    "pp",
    "provenance_text",
    "score_text",
    "value_cell",
]
