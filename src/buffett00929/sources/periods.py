"""期別排程：哪幾期該抓、哪幾期已經定案。

回補十年歷史時有兩個問題非答對不可，而且兩個都不能用猜的：

1. **這一期公告了嗎？** 還沒到申報期限就去抓，只會拿到空結果，
   然後被誤記成「這家公司缺料」。台灣的申報期限是法定的，可以直接算。
2. **這一期還會變嗎？** 已經定案的期別可以永久快取（見 ``cache.py``），
   每日排程就不必重複回補十年；還可能被更正的期別則必須照常重抓。

第 2 點刻意保守。季報數字在年度財報查核完成時會被追溯調整，
所以「公告了」不等於「定案了」——本模組把定案時點壓到會計年度結束後
``settle_months``（預設 15 個月），也就是年報申報期限再加一段緩衝。
判斷錯的代價是不對稱的：多抓幾次只是慢一點，把會變的數字永久快取
則會讓過時資料被當成最新資料，違反規格第二節。
"""

from __future__ import annotations

from datetime import date

from ..models import FiscalPeriod

# 證券交易法規定的財報申報期限（一般行業）。
# 金融、保險與部分特殊產業另有規定，這裡取一般情況——
# 抓早了頂多是空結果，不會產生錯誤數字。
_QUARTER_DEADLINES = {
    1: (5, 15),   # 第一季：5/15
    2: (8, 14),   # 第二季：8/14
    3: (11, 14),  # 第三季：11/14
}
_ANNUAL_DEADLINE = (3, 31)  # 年報：次年 3/31

DEFAULT_SETTLE_MONTHS = 15


def filing_deadline(period: FiscalPeriod) -> date:
    """該期別的法定申報期限。"""
    if period.quarter in _QUARTER_DEADLINES:
        month, day = _QUARTER_DEADLINES[period.quarter]
        return date(period.year, month, day)
    # 第四季不單獨申報，併入年報；年度數同樣以年報期限為準。
    month, day = _ANNUAL_DEADLINE
    return date(period.year + 1, month, day)


def is_published(period: FiscalPeriod, today: date) -> bool:
    """申報期限是否已過。未過就別去抓——拿到的空結果會被誤判為缺料。"""
    return today >= filing_deadline(period)


def is_settled(
    period: FiscalPeriod, today: date, settle_months: int = DEFAULT_SETTLE_MONTHS
) -> bool:
    """該期別的數字是否已不會再變動（可永久快取）。

    以會計年度結束日往後推 ``settle_months``。預設 15 個月＝年報申報期限
    （次年 3/31）再加三個月緩衝，涵蓋更正重編的實務時程。
    """
    year_end = date(period.year, 12, 31)
    months = (today.year - year_end.year) * 12 + (today.month - year_end.month)
    if months == settle_months:
        return today.day >= year_end.day
    return months > settle_months


DEFAULT_QUARTERLY_YEARS = 2


def periods_to_fetch(
    today: date,
    years: int = 10,
    quarterly_years: int = DEFAULT_QUARTERLY_YEARS,
) -> list[FiscalPeriod]:
    """列出應該抓取的期別，由舊到新。

    **年度數抓滿 ``years`` 年，季度數只抓最近 ``quarterly_years`` 年。**
    十年的 ROE、CAGR 與獲利穩定度都是年度指標，用不到十年前的單季數；
    近幾季則要留著做 QoQ/YoY 與財報事件追蹤。這個取捨把請求數從
    每年 4 期壓到「10 個年度 + 6 個季度」，直接反映在被擋的風險上。

    只包含申報期限已過的期別（見 ``is_published``）。順序由舊到新，
    讓回補即使中途中斷，已取得的部分仍是連續的歷史。
    """
    periods: list[FiscalPeriod] = []
    for year in range(today.year - years, today.year + 1):
        annual = FiscalPeriod(year, 0)
        if is_published(annual, today):
            periods.append(annual)
        if year < today.year - quarterly_years + 1:
            continue
        for quarter in (1, 2, 3):
            period = FiscalPeriod(year, quarter)
            if is_published(period, today):
                periods.append(period)
    periods.sort(key=lambda p: (p.year, 4 if p.is_annual else p.quarter))
    return periods


def to_roc(year: int) -> int:
    """西元轉民國。MOPS 的查詢參數一律用民國年。"""
    return year - 1911


def from_roc(year: int | str) -> int:
    """民國轉西元。"""
    return int(year) + 1911


__all__ = [
    "DEFAULT_SETTLE_MONTHS",
    "filing_deadline",
    "from_roc",
    "is_published",
    "is_settled",
    "periods_to_fetch",
    "to_roc",
]
