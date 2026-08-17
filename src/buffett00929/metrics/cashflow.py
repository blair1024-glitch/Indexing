"""自由現金流與盈餘再投資能力（規格第六節）。

核心提問：「公司賺 100 元，需要投入多少錢才能維持競爭力？」

分類：
* 獲利高 ＋ 資本支出低 ＋ FCF 高 → 🟢 高資本效率
* 獲利高但需要大量 CapEx        → 🟡 資本密集型
* 淨利很好但現金流很差          → 🔴 盈餘品質警告
"""

from __future__ import annotations

from ..models import Company, DataPoint, safe_div, safe_mean

EFFICIENCY_HIGH = "🟢 高資本效率"
EFFICIENCY_CAPITAL_INTENSIVE = "🟡 資本密集型"
EFFICIENCY_QUALITY_WARNING = "🔴 盈餘品質警告"
EFFICIENCY_UNKNOWN = "資料不足，無法分類"


def fcf_series(company: Company) -> list[tuple[int, DataPoint]]:
    return [(cf.period.year, cf.free_cash_flow) for cf in company.annual_cash_flows()]


def fcf_margin_average(company: Company, years: int = 5) -> DataPoint:
    """FCF Margin＝自由現金流／營收（近 N 年平均）。"""
    incomes = {s.period.year: s for s in company.annual_incomes()}
    flows = company.annual_cash_flows()[-years:]
    if not flows:
        return DataPoint.missing("無現金流量表，無法計算 FCF Margin")

    ratios: list[DataPoint] = []
    for flow in flows:
        income = incomes.get(flow.period.year)
        if income is None:
            ratios.append(DataPoint.missing(f"{flow.period.year} 年缺營收，無法計算 FCF Margin"))
            continue
        ratios.append(safe_div(flow.free_cash_flow, income.revenue, note="缺 FCF 或營收"))
    return safe_mean(ratios, note="部分年度 FCF Margin 無法計算")


def fcf_to_net_income_average(company: Company, years: int = 5) -> DataPoint:
    """FCF／稅後淨利——盈餘含金量。

    淨利為負的年度會讓比值失去意義（負負得正），故該年度標示缺料排除。
    """
    incomes = {s.period.year: s for s in company.annual_incomes()}
    flows = company.annual_cash_flows()[-years:]
    if not flows:
        return DataPoint.missing("無現金流量表，無法計算 FCF／淨利")

    ratios: list[DataPoint] = []
    for flow in flows:
        income = incomes.get(flow.period.year)
        if income is None:
            ratios.append(DataPoint.missing(f"{flow.period.year} 年缺淨利，無法計算 FCF／淨利"))
            continue
        net_income = income.net_income
        if net_income.is_available and net_income.value is not None and net_income.value <= 0:
            ratios.append(
                DataPoint.missing(f"{flow.period.year} 年淨利 ≤ 0，FCF／淨利無意義")
            )
            continue
        ratios.append(safe_div(flow.free_cash_flow, net_income, note="缺 FCF 或淨利"))
    return safe_mean(ratios, note="部分年度 FCF／淨利無法計算")


def ocf_to_net_income_average(company: Company, years: int = 5) -> DataPoint:
    """營業現金流／稅後淨利——真正的盈餘品質指標。

    這和 ``fcf_to_net_income_average`` 衡量的是不同的事，不可混用：
    營業現金流低於淨利，代表帳上獲利收不回現金（應收暴增、存貨堆積、
    認列過於寬鬆），屬於**盈餘品質**問題；
    而自由現金流低只是因為資本支出大，那是**資本密集**，不是品質問題。
    """
    incomes = {s.period.year: s for s in company.annual_incomes()}
    flows = company.annual_cash_flows()[-years:]
    if not flows:
        return DataPoint.missing("無現金流量表，無法計算營業現金流／淨利")

    ratios: list[DataPoint] = []
    for flow in flows:
        income = incomes.get(flow.period.year)
        if income is None:
            ratios.append(DataPoint.missing(f"{flow.period.year} 年缺淨利"))
            continue
        net_income = income.net_income
        if net_income.is_available and net_income.value is not None and net_income.value <= 0:
            ratios.append(
                DataPoint.missing(f"{flow.period.year} 年淨利 ≤ 0，營業現金流／淨利無意義")
            )
            continue
        ratios.append(
            safe_div(flow.operating_cash_flow, net_income, note="缺營業現金流或淨利")
        )
    return safe_mean(ratios, note="部分年度營業現金流／淨利無法計算")


def reinvestment_rate_average(company: Company, years: int = 5) -> DataPoint:
    """盈再率＝資本支出／營業現金流（近 N 年平均）。

    衡量「維持競爭力需要吃掉多少營運現金」。營業現金流為負的年度排除，
    否則比率的正負號會反轉，讓資本密集的公司看起來像高效率。
    """
    flows = company.annual_cash_flows()[-years:]
    if not flows:
        return DataPoint.missing("無現金流量表，無法計算盈再率")

    ratios: list[DataPoint] = []
    for flow in flows:
        ocf = flow.operating_cash_flow
        if ocf.is_available and ocf.value is not None and ocf.value <= 0:
            ratios.append(DataPoint.missing(f"{flow.period.year} 年營業現金流 ≤ 0，盈再率無意義"))
            continue
        ratios.append(safe_div(flow.capex, ocf, note="缺資本支出或營業現金流"))
    return safe_mean(ratios, note="部分年度盈再率無法計算")


def latest_fcf(company: Company) -> DataPoint:
    flow = company.latest_annual_cash_flow
    if flow is None:
        return DataPoint.missing("無現金流量表，無法取得自由現金流")
    return flow.free_cash_flow


def capital_efficiency(
    company: Company,
    *,
    high_fcf_margin: float = 0.10,
    low_reinvestment: float = 0.40,
    weak_conversion: float = 0.30,
) -> tuple[str, str]:
    """依規格第六節分類資本效率。回傳 (分類, 依據說明)。"""
    fcf_margin = fcf_margin_average(company)
    reinvestment = reinvestment_rate_average(company)
    # 盈餘品質看的是**營業**現金流：自由現金流低可能只是資本支出大，
    # 那是資本密集，不是獲利收不回現金。
    conversion = ocf_to_net_income_average(company)

    if not fcf_margin.is_available and not conversion.is_available:
        return EFFICIENCY_UNKNOWN, "缺現金流量表或損益表，無法評估資本效率"

    # 先判斷最嚴重的情形：帳上有獲利但現金收不回來。
    if conversion.is_available and conversion.value is not None and conversion.value < weak_conversion:
        return (
            EFFICIENCY_QUALITY_WARNING,
            f"營業現金流僅為淨利的 {conversion.value:.0%}，帳面獲利未有效轉化為現金",
        )

    if (
        fcf_margin.is_available
        and reinvestment.is_available
        and fcf_margin.value is not None
        and reinvestment.value is not None
    ):
        if fcf_margin.value >= high_fcf_margin and reinvestment.value <= low_reinvestment:
            return (
                EFFICIENCY_HIGH,
                f"FCF Margin {fcf_margin.value:.1%}、盈再率 {reinvestment.value:.0%}，"
                "獲利無需大量資本支出即可維持",
            )
        if reinvestment.value > low_reinvestment:
            return (
                EFFICIENCY_CAPITAL_INTENSIVE,
                f"盈再率 {reinvestment.value:.0%}，維持競爭力需持續投入大量資本支出",
            )

    if fcf_margin.is_available and fcf_margin.value is not None and fcf_margin.value < 0:
        return EFFICIENCY_QUALITY_WARNING, f"FCF Margin 為負（{fcf_margin.value:.1%}），自由現金流長期流出"

    return EFFICIENCY_UNKNOWN, "現金流指標不足，無法明確分類"


__all__ = [
    "EFFICIENCY_CAPITAL_INTENSIVE",
    "EFFICIENCY_HIGH",
    "EFFICIENCY_QUALITY_WARNING",
    "EFFICIENCY_UNKNOWN",
    "capital_efficiency",
    "fcf_margin_average",
    "fcf_series",
    "fcf_to_net_income_average",
    "ocf_to_net_income_average",
    "latest_fcf",
    "reinvestment_rate_average",
]
