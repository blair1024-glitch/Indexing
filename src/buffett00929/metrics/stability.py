"""獲利穩定性與成長（規格第五節）。

規格要求分析至少 5 年的營收、毛利、毛利率、營業利益、營益率、稅後淨利、EPS，
計算 CAGR 與趨勢，並判斷屬於「穩定成長／穩定低成長／高度週期性／衰退／大起大落」。

另需標示「盈餘品質警告」：EPS 突然暴增、一次性收益、業外收益過高、
毛利率突然異常、應收帳款快速增加。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from ..models import (
    Company,
    DataPoint,
    IncomeStatement,
    cagr,
    safe_div,
    safe_mean,
    safe_sub,
    stdev,
)

PROFILE_STEADY_GROWTH = "獲利穩定成長"
PROFILE_STEADY_LOW = "穩定但低成長"
PROFILE_CYCLICAL = "高度週期性"
PROFILE_DECLINING = "獲利衰退"
PROFILE_VOLATILE = "大起大落"
PROFILE_UNKNOWN = "資料不足，無法判定"


def _series(
    company: Company, extract: Callable[[IncomeStatement], DataPoint], years: int
) -> list[DataPoint]:
    incomes = company.trailing_annual_incomes(years)
    return [extract(stmt) for stmt in incomes]


def eps_cagr(company: Company, years: int = 5) -> DataPoint:
    """EPS 年複合成長率。

    起點或終點為負（虧損）時 CAGR 無定義，`cagr()` 會回傳缺料並說明原因，
    而不是產生一個看似成長的假數字。
    """
    incomes = company.trailing_annual_incomes(years)
    if len(incomes) < years:
        return DataPoint.missing(f"年度資料不足 {years} 年（僅 {len(incomes)} 年），無法計算 EPS CAGR")
    return cagr(incomes[0].eps, incomes[-1].eps, years - 1, note="EPS 資料不足，無法計算 CAGR")


def revenue_cagr(company: Company, years: int = 5) -> DataPoint:
    incomes = company.trailing_annual_incomes(years)
    if len(incomes) < years:
        return DataPoint.missing(f"年度資料不足 {years} 年，無法計算營收 CAGR")
    return cagr(incomes[0].revenue, incomes[-1].revenue, years - 1, note="營收資料不足，無法計算 CAGR")


def gross_margin_average(company: Company, years: int = 5) -> DataPoint:
    values = _series(company, lambda s: s.gross_margin, years)
    if len(values) < years:
        return DataPoint.missing(f"年度資料不足 {years} 年，無法計算平均毛利率")
    return safe_mean(values, note="部分年度毛利率無法計算")


def gross_margin_stdev(company: Company, years: int = 5) -> DataPoint:
    values = _series(company, lambda s: s.gross_margin, years)
    if len(values) < years:
        return DataPoint.missing(f"年度資料不足 {years} 年，無法計算毛利率波動度")
    return stdev(values, note="部分年度毛利率無法計算")


def operating_margin_average(company: Company, years: int = 5) -> DataPoint:
    values = _series(company, lambda s: s.operating_margin, years)
    if len(values) < years:
        return DataPoint.missing(f"年度資料不足 {years} 年，無法計算平均營益率")
    return safe_mean(values, note="部分年度營益率無法計算")


def margin_trend(company: Company, window: int = 3) -> DataPoint:
    """營益率趨勢：近 N 年平均 − 前 N 年平均。"""
    values = _series(company, lambda s: s.operating_margin, window * 2)
    if len(values) < window * 2:
        return DataPoint.missing(f"年度資料不足 {window * 2} 年，無法比較營益率趨勢")
    recent = safe_mean(values[-window:], note="近期營益率資料不足")
    earlier = safe_mean(values[:window], note="前期營益率資料不足")
    return safe_sub(recent, earlier, note="營益率趨勢資料不足")


def gross_margin_trend(company: Company, window: int = 3) -> DataPoint:
    values = _series(company, lambda s: s.gross_margin, window * 2)
    if len(values) < window * 2:
        return DataPoint.missing(f"年度資料不足 {window * 2} 年，無法比較毛利率趨勢")
    recent = safe_mean(values[-window:], note="近期毛利率資料不足")
    earlier = safe_mean(values[:window], note="前期毛利率資料不足")
    return safe_sub(recent, earlier, note="毛利率趨勢資料不足")


def loss_years(company: Company, years: int = 5) -> DataPoint:
    """近 N 年的虧損年數。"""
    incomes = company.trailing_annual_incomes(years)
    if len(incomes) < years:
        return DataPoint.missing(f"年度資料不足 {years} 年，無法統計虧損年數")
    available = [s.net_income for s in incomes if s.net_income.is_available]
    if len(available) < years:
        return DataPoint.missing("部分年度稅後淨利缺漏，無法統計虧損年數")
    count = sum(1 for dp in available if dp.value is not None and dp.value < 0)
    return DataPoint.derived(float(count), inputs=available)


def latest_revenue_in_100m(company: Company) -> DataPoint:
    """最新年度營收，換算為「億元」——規模門檻用億元設定較直觀。"""
    income = company.latest_annual_income
    if income is None or not income.revenue.is_available:
        return DataPoint.missing("無最新年度營收，無法評估規模")
    return DataPoint.derived(income.revenue.value / 1e8, inputs=[income.revenue])  # type: ignore[operator]


def rnd_intensity_average(company: Company, years: int = 5) -> DataPoint:
    values = _series(company, lambda s: s.rnd_intensity, years)
    if not values:
        return DataPoint.missing("無年度資料，無法計算研發費用率")
    return safe_mean(values, note="部分年度研發費用或營收缺漏")


def non_operating_ratio_average(company: Company, years: int = 3) -> DataPoint:
    """業外損益佔稅前淨利比重（近 N 年平均）。

    取平均而非單年，避免某一年的一次性處分利益就把長期判斷帶偏。
    """
    values = _series(company, lambda s: s.non_operating_ratio, years)
    if not values:
        return DataPoint.missing("無年度資料，無法計算業外佔比")
    return safe_mean(values, note="部分年度業外損益或稅前淨利缺漏")


# --------------------------------------------------------------------------
# 獲利型態判定
# --------------------------------------------------------------------------


def earnings_profile(company: Company, years: int = 5) -> str:
    """把獲利軌跡歸類為規格第五節的五種型態之一。"""
    incomes = company.trailing_annual_incomes(years)
    if len(incomes) < years:
        return PROFILE_UNKNOWN

    eps_values = [s.eps for s in incomes]
    if any(not dp.is_available for dp in eps_values):
        return PROFILE_UNKNOWN

    values = [dp.value for dp in eps_values]
    growth = eps_cagr(company, years)
    volatility = stdev(eps_values)
    mean_eps = safe_mean(eps_values)

    # 變異係數：用相對波動衡量，避免高 EPS 公司天生看起來比較波動。
    cv = None
    if volatility.is_available and mean_eps.is_available and mean_eps.value:
        cv = abs(volatility.value / mean_eps.value)  # type: ignore[operator]

    if any(v is not None and v < 0 for v in values):
        return PROFILE_VOLATILE if cv is not None and cv > 0.5 else PROFILE_CYCLICAL

    if growth.is_available and growth.value is not None:
        if growth.value < -0.03:
            return PROFILE_DECLINING
        if cv is not None and cv > 0.6:
            return PROFILE_VOLATILE
        if cv is not None and cv > 0.35:
            return PROFILE_CYCLICAL
        if growth.value >= 0.07:
            return PROFILE_STEADY_GROWTH
        return PROFILE_STEADY_LOW

    return PROFILE_UNKNOWN


# --------------------------------------------------------------------------
# 盈餘品質警告
# --------------------------------------------------------------------------


@dataclass
class EarningsQualityWarning:
    code: str
    message: str
    evidence: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "evidence": self.evidence}


def earnings_quality_warnings(
    company: Company,
    *,
    non_operating_max: float = 0.40,
    eps_spike_multiple: float = 2.0,
    margin_jump: float = 0.10,
    receivable_growth_multiple: float = 1.5,
) -> list[EarningsQualityWarning]:
    """規格第五節列舉的盈餘品質警訊偵測。"""
    warnings: list[EarningsQualityWarning] = []
    incomes = company.annual_incomes()

    # 業外收益佔比過高
    ratio = non_operating_ratio_average(company, years=min(3, len(incomes)) or 1)
    if ratio.is_available and ratio.value is not None and ratio.value > non_operating_max:
        warnings.append(
            EarningsQualityWarning(
                code="non_operating_high",
                message="業外收益佔比過高，獲利可持續性存疑",
                evidence=f"近年業外損益佔稅前淨利平均 {ratio.value:.1%}，超過 {non_operating_max:.0%} 門檻",
            )
        )

    # EPS 突然暴增
    if len(incomes) >= 2:
        prev, curr = incomes[-2].eps, incomes[-1].eps
        if (
            prev.is_available
            and curr.is_available
            and prev.value is not None
            and curr.value is not None
            and prev.value > 0
            and curr.value > prev.value * eps_spike_multiple
        ):
            warnings.append(
                EarningsQualityWarning(
                    code="eps_spike",
                    message="EPS 單年暴增，須確認是否來自一次性收益",
                    evidence=(
                        f"EPS 自 {prev.value:.2f} 元跳升至 {curr.value:.2f} 元"
                        f"（{curr.value / prev.value:.1f} 倍）"
                    ),
                )
            )

    # 毛利率異常跳動
    if len(incomes) >= 2:
        prev_gm, curr_gm = incomes[-2].gross_margin, incomes[-1].gross_margin
        if prev_gm.is_available and curr_gm.is_available:
            delta = curr_gm.value - prev_gm.value  # type: ignore[operator]
            if abs(delta) > margin_jump:
                direction = "跳升" if delta > 0 else "驟降"
                warnings.append(
                    EarningsQualityWarning(
                        code="margin_jump",
                        message=f"毛利率單年{direction}，須確認會計處理或產品組合是否改變",
                        evidence=f"毛利率自 {prev_gm.value:.1%} 變為 {curr_gm.value:.1%}（{delta:+.1%}）",
                    )
                )

    # 應收帳款成長遠快於營收
    balances = company.annual_balances()
    if len(balances) >= 2 and len(incomes) >= 2:
        warning = _receivable_warning(
            balances[-2].receivables,
            balances[-1].receivables,
            incomes[-2].revenue,
            incomes[-1].revenue,
            receivable_growth_multiple,
        )
        if warning:
            warnings.append(warning)

    return warnings


def _receivable_warning(
    prev_recv: DataPoint,
    curr_recv: DataPoint,
    prev_rev: DataPoint,
    curr_rev: DataPoint,
    multiple: float,
) -> EarningsQualityWarning | None:
    """應收帳款成長速度遠超營收，常見於認列過於寬鬆或收款惡化。"""
    points = (prev_recv, curr_recv, prev_rev, curr_rev)
    if not all(p.is_available for p in points):
        return None
    if prev_recv.value in (None, 0) or prev_rev.value in (None, 0):
        return None

    recv_growth = curr_recv.value / prev_recv.value - 1  # type: ignore[operator]
    rev_growth = curr_rev.value / prev_rev.value - 1  # type: ignore[operator]

    # 營收本身衰退時，用倍數比較會失真，改以「應收成長 vs 營收衰退」直接判定。
    if rev_growth <= 0:
        if recv_growth > 0.10:
            return EarningsQualityWarning(
                code="receivables_vs_revenue",
                message="營收衰退但應收帳款仍增加，收款品質須留意",
                evidence=f"應收帳款 {recv_growth:+.1%}、營收 {rev_growth:+.1%}",
            )
        return None

    if recv_growth > rev_growth * multiple and recv_growth > 0.10:
        return EarningsQualityWarning(
            code="receivables_vs_revenue",
            message="應收帳款成長顯著快於營收，須留意認列與收款品質",
            evidence=(
                f"應收帳款成長 {recv_growth:+.1%}，營收成長 {rev_growth:+.1%}"
                f"（超過 {multiple} 倍門檻）"
            ),
        )
    return None


__all__ = [
    "EarningsQualityWarning",
    "PROFILE_CYCLICAL",
    "PROFILE_DECLINING",
    "PROFILE_STEADY_GROWTH",
    "PROFILE_STEADY_LOW",
    "PROFILE_UNKNOWN",
    "PROFILE_VOLATILE",
    "earnings_profile",
    "earnings_quality_warnings",
    "eps_cagr",
    "gross_margin_average",
    "gross_margin_stdev",
    "gross_margin_trend",
    "latest_revenue_in_100m",
    "loss_years",
    "margin_trend",
    "non_operating_ratio_average",
    "operating_margin_average",
    "revenue_cagr",
    "rnd_intensity_average",
]
