"""股東回饋與高殖利率陷阱（規格第八節）。

00929 是高股息 ETF，成分股全部以「配得多」入選，因此**高殖利率陷阱**
是本系統最需要防守的誤判：殖利率高不是買進理由，如果它同時伴隨

* EPS 下降
* FCF 下降或轉負
* 配息率過高（超過 100%＝吃老本配息）
* 負債增加

那高殖利率往往只是股價下跌的結果，或是不可持續的配息政策。
規格明訂這種情況必須標示 🔴「高殖利率可能是價值陷阱」。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Company, DataPoint, cagr, safe_div, safe_mean


def latest_dividend_yield(company: Company) -> DataPoint:
    return company.market_data.dividend_yield


def cash_dividend_series(company: Company) -> list[tuple[int, DataPoint]]:
    return [(d.year, d.cash_dividend) for d in company.dividends]


def payout_ratio(company: Company, years: int = 3) -> DataPoint:
    """配息率＝每股現金股利／EPS（近 N 年平均）。

    EPS ≤ 0 的年度排除——虧損還配息的配息率是負數，會讓平均值失去意義；
    這種情況改由 ``high_yield_trap`` 直接判定為警訊。
    """
    incomes = {s.period.year: s for s in company.annual_incomes()}
    records = company.dividends[-years:] if company.dividends else []
    if not records:
        return DataPoint.missing("無股利資料，無法計算配息率")

    ratios: list[DataPoint] = []
    for record in records:
        income = incomes.get(record.year)
        if income is None:
            ratios.append(DataPoint.missing(f"{record.year} 年缺 EPS，無法計算配息率"))
            continue
        eps = income.eps
        if eps.is_available and eps.value is not None and eps.value <= 0:
            ratios.append(DataPoint.missing(f"{record.year} 年 EPS ≤ 0，配息率無意義"))
            continue
        ratios.append(safe_div(record.cash_dividend, eps, note="缺股利或 EPS"))
    return safe_mean(ratios, note="部分年度配息率無法計算")


def dividend_growth(company: Company, years: int = 5) -> DataPoint:
    """現金股利年複合成長率。"""
    records = [d for d in company.dividends if d.cash_dividend.is_available]
    if len(records) < 2:
        return DataPoint.missing("股利資料不足 2 年，無法計算股利成長率")
    window = records[-years:]
    span = window[-1].year - window[0].year
    if span <= 0:
        return DataPoint.missing("股利期間長度為 0，無法年化")
    return cagr(
        window[0].cash_dividend,
        window[-1].cash_dividend,
        span,
        note="股利資料不足，無法計算成長率",
    )


def dividend_continuity(company: Company) -> DataPoint:
    """連續配息年數（自最近年度往回數）。"""
    records = company.dividends
    if not records:
        return DataPoint.missing("無股利資料，無法計算連續配息年數")

    streak = 0
    inputs: list[DataPoint] = []
    for record in reversed(records):
        dividend = record.cash_dividend
        if not dividend.is_available or dividend.value is None or dividend.value <= 0:
            break
        streak += 1
        inputs.append(dividend)
    return DataPoint.derived(float(streak), inputs=inputs, note="無連續配息紀錄")


def stock_dividend_dilution(company: Company, years: int = 5) -> DataPoint:
    """近 N 年股票股利合計（股本稀釋來源之一）。"""
    records = [d for d in company.dividends[-years:] if d.stock_dividend.is_available]
    if not records:
        return DataPoint.missing("無股票股利資料")
    total = sum(d.stock_dividend.value or 0.0 for d in records)
    return DataPoint.derived(total, inputs=[d.stock_dividend for d in records])


# --------------------------------------------------------------------------
# 高殖利率陷阱
# --------------------------------------------------------------------------


@dataclass
class YieldTrapAssessment:
    is_trap: bool
    yield_value: DataPoint
    reasons: list[str] = field(default_factory=list)
    """觸發陷阱判定的具體證據。空 list 代表高殖利率但體質無警訊。"""
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "is_trap": self.is_trap,
            "yield": self.yield_value.to_dict(),
            "reasons": self.reasons,
            "note": self.note,
        }


def high_yield_trap(
    company: Company,
    *,
    yield_threshold: float = 0.06,
    payout_max: float = 1.0,
) -> YieldTrapAssessment:
    """規格第八節的高殖利率陷阱判定。

    只有在殖利率**確實偏高**時才進行陷阱判定——低殖利率公司的 EPS 下滑
    是另一回事（由紅旗系統處理），不該掛在「殖利率陷阱」這個標籤下。
    """
    from . import cashflow as cf_metrics  # 延後匯入避免循環相依
    from . import stability as stab_metrics

    dividend_yield = latest_dividend_yield(company)
    if not dividend_yield.is_available or dividend_yield.value is None:
        return YieldTrapAssessment(
            is_trap=False,
            yield_value=dividend_yield,
            note="缺殖利率資料，無法進行高殖利率陷阱判定",
        )

    if dividend_yield.value < yield_threshold:
        return YieldTrapAssessment(
            is_trap=False,
            yield_value=dividend_yield,
            note=f"殖利率 {dividend_yield.value:.2%} 未達 {yield_threshold:.0%} 門檻，不適用陷阱判定",
        )

    reasons: list[str] = []

    eps_growth = stab_metrics.eps_cagr(company)
    if eps_growth.is_available and eps_growth.value is not None and eps_growth.value < 0:
        reasons.append(f"EPS 5 年 CAGR 為 {eps_growth.value:+.1%}，獲利呈長期下滑")

    incomes = company.annual_incomes()
    if len(incomes) >= 2:
        prev, curr = incomes[-2].eps, incomes[-1].eps
        if (
            prev.is_available
            and curr.is_available
            and prev.value is not None
            and curr.value is not None
            and curr.value < prev.value
        ):
            reasons.append(f"最新年度 EPS 自 {prev.value:.2f} 降至 {curr.value:.2f} 元")

    fcf_margin = cf_metrics.fcf_margin_average(company)
    if fcf_margin.is_available and fcf_margin.value is not None and fcf_margin.value < 0:
        reasons.append(f"5 年平均 FCF Margin 為 {fcf_margin.value:.1%}，自由現金流長期為負")

    flows = company.annual_cash_flows()
    if len(flows) >= 2:
        prev_fcf, curr_fcf = flows[-2].free_cash_flow, flows[-1].free_cash_flow
        if (
            prev_fcf.is_available
            and curr_fcf.is_available
            and prev_fcf.value is not None
            and curr_fcf.value is not None
            and curr_fcf.value < prev_fcf.value
            and curr_fcf.value < 0
        ):
            reasons.append("最新年度自由現金流較前期下滑且已轉為負值")

    ratio = payout_ratio(company)
    if ratio.is_available and ratio.value is not None and ratio.value > payout_max:
        reasons.append(f"配息率 {ratio.value:.0%} 超過 100%，配息高於盈餘（吃老本）")

    from . import balance as bal_metrics

    debt_change = bal_metrics.debt_growth(company)
    if debt_change.is_available and debt_change.value is not None and debt_change.value > 0.30:
        reasons.append(f"有息負債年增 {debt_change.value:+.0%}，配息同時債務快速墊高")

    return YieldTrapAssessment(
        is_trap=bool(reasons),
        yield_value=dividend_yield,
        reasons=reasons,
        note=(
            f"殖利率 {dividend_yield.value:.2%} 偏高且出現 {len(reasons)} 項體質警訊"
            if reasons
            else f"殖利率 {dividend_yield.value:.2%} 偏高，但未發現獲利／現金流／配息率／負債警訊"
        ),
    )


__all__ = [
    "YieldTrapAssessment",
    "cash_dividend_series",
    "dividend_continuity",
    "dividend_growth",
    "high_yield_trap",
    "latest_dividend_yield",
    "payout_ratio",
    "stock_dividend_dilution",
]
