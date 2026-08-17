"""內在價值估算與安全邊際（規格第三節 M3）。

規格的核心要求：**好公司 ≠ 好股票價格**，且「至少使用 2 種以上估值方法交叉驗證」。
本模組實作四種方法，取**可得方法的中位數**為內在價值（中位數比平均值更耐離群值），
並回報各方法的離散度。

**可用方法少於 2 種時不給安全邊際分數**——寧可標示資料不足，
也不要用單一方法算出的價格當成「交叉驗證過」的結論。

台股特有細節：``shares_outstanding`` 取自財報的「普通股股本」，單位是**金額**
而非股數。台股面額通常為 10 元，故股數 = 股本 ÷ 面額。這個換算若省略，
每股數字會差 10 倍。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..metrics import stability
from ..models import Company, DataPoint

DEFAULT_PAR_VALUE = 10.0
"""台股普通股面額，元。用於由股本換算股數。"""


@dataclass
class ValuationMethod:
    """單一估值方法的結果。"""

    key: str
    label: str
    value_per_share: DataPoint
    assumptions: str = ""

    @property
    def is_available(self) -> bool:
        return self.value_per_share.is_available

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value_per_share": self.value_per_share.to_dict(),
            "assumptions": self.assumptions,
        }


@dataclass
class Valuation:
    """交叉驗證後的估值結論。"""

    methods: list[ValuationMethod] = field(default_factory=list)
    intrinsic_value: DataPoint = field(default_factory=lambda: DataPoint.missing("未估算"))
    price: DataPoint = field(default_factory=lambda: DataPoint.missing("未取得股價"))
    margin_of_safety: DataPoint = field(default_factory=lambda: DataPoint.missing("未計算"))
    dispersion: DataPoint = field(default_factory=lambda: DataPoint.missing("未計算"))
    dispersion_warning: bool = False
    note: str = ""

    @property
    def available_methods(self) -> list[ValuationMethod]:
        return [m for m in self.methods if m.is_available]

    @property
    def classification(self) -> str:
        """規格第三節的安全邊際分級。"""
        if not self.margin_of_safety.is_available or self.margin_of_safety.value is None:
            return "資料不足"
        mos = self.margin_of_safety.value
        if mos > 0.30:
            return "🟢 非常便宜"
        if mos >= 0.20:
            return "🟢 便宜"
        if mos >= 0.10:
            return "🟡 合理偏便宜"
        if mos >= 0:
            return "🟡 合理"
        return "🔴 高估"

    def to_dict(self) -> dict:
        return {
            "methods": [m.to_dict() for m in self.methods],
            "intrinsic_value": self.intrinsic_value.to_dict(),
            "price": self.price.to_dict(),
            "margin_of_safety": self.margin_of_safety.to_dict(),
            "classification": self.classification,
            "dispersion": self.dispersion.to_dict(),
            "dispersion_warning": self.dispersion_warning,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# 輔助
# --------------------------------------------------------------------------


def share_count(company: Company, par_value: float = DEFAULT_PAR_VALUE) -> DataPoint:
    """由普通股股本換算流通股數：股數 = 股本 ÷ 面額。"""
    balance = company.latest_balance
    if balance is None or not balance.shares_outstanding.is_available:
        return DataPoint.missing("缺普通股股本，無法換算流通股數")
    capital = balance.shares_outstanding
    if capital.value is None or capital.value <= 0:
        return DataPoint.missing("普通股股本 ≤ 0，無法換算流通股數")
    return DataPoint.derived(capital.value / par_value, inputs=[capital])


def normalized_eps(company: Company, years: int) -> DataPoint:
    """正常化 EPS：近 N 年 EPS 平均，抹平景氣循環的高低點。"""
    incomes = company.trailing_annual_incomes(years)
    if len(incomes) < years:
        return DataPoint.missing(
            f"年度資料不足 {years} 年（僅 {len(incomes)} 年），無法計算正常化 EPS"
        )
    eps_points = [s.eps for s in incomes]
    missing = [p for p in eps_points if not p.is_available]
    if missing:
        return DataPoint.missing("部分年度 EPS 缺漏，無法計算正常化 EPS")
    values = [p.value for p in eps_points]
    return DataPoint.derived(sum(values) / len(values), inputs=eps_points)  # type: ignore[arg-type]


def justified_pe(company: Company, config: dict) -> DataPoint:
    """合理本益比：歷史本益比的指定分位數，並夾在設定的上下限之間。

    夾住上下限是為了避免兩種失真：泡沫期的歷史高本益比被當成合理值，
    以及長期低估的公司被永遠鎖在低本益比。
    """
    history = company.market_data.pe_history
    floor = float(config.get("pe_band_floor", 8.0))
    cap = float(config.get("pe_band_cap", 30.0))
    percentile = float(config.get("justified_pe_percentile", 0.5))

    if not history:
        return DataPoint.missing("無歷史本益比資料，無法推估合理本益比")

    ordered = sorted(history)
    index = min(int(len(ordered) * percentile), len(ordered) - 1)
    raw = ordered[index]
    clamped = max(floor, min(cap, raw))
    return DataPoint.of(
        clamped,
        "derived(FinMind:TaiwanStockPER)",
        period=f"{len(ordered)} 筆歷史本益比之第 {percentile:.0%} 分位"
        + ("" if raw == clamped else f"（原值 {raw:.1f} 倍，已夾至 {floor:.0f}~{cap:.0f} 倍）"),
    )


# --------------------------------------------------------------------------
# 四種估值方法
# --------------------------------------------------------------------------


def _method_normalized_pe(company: Company, config: dict) -> ValuationMethod:
    years = int(config.get("normalized_eps_years", 5))
    eps = normalized_eps(company, years)
    pe = justified_pe(company, config)

    if not eps.is_available or not pe.is_available:
        reason = eps.unavailable_reason if not eps.is_available else pe.unavailable_reason
        return ValuationMethod(
            key="normalized_pe",
            label="正常化 EPS × 合理本益比",
            value_per_share=DataPoint.missing(reason or "資料不足"),
        )

    value = eps.value * pe.value  # type: ignore[operator]
    return ValuationMethod(
        key="normalized_pe",
        label="正常化 EPS × 合理本益比",
        value_per_share=DataPoint.derived(value, inputs=[eps, pe]),
        assumptions=f"{years} 年平均 EPS {eps.value:.2f} 元 × 合理本益比 {pe.value:.1f} 倍",
    )


def _method_dividend_yield(company: Company, config: dict) -> ValuationMethod:
    """殖利率法：內在價值 = 每股現金股利 ÷ 要求殖利率。

    00929 成分股皆為配息穩定的公司，此法最貼近這類標的的實際定價邏輯。
    """
    required = float(config.get("required_yield", 0.05))
    records = [d for d in company.dividends if d.cash_dividend.is_available]
    if not records:
        return ValuationMethod(
            key="dividend_yield",
            label="殖利率法",
            value_per_share=DataPoint.missing("無現金股利資料，無法以殖利率法估值"),
        )

    # 取近三年平均股利，避免單一年度的特別股利把估值墊高。
    window = records[-3:]
    values = [d.cash_dividend.value for d in window if d.cash_dividend.value is not None]
    if not values:
        return ValuationMethod(
            key="dividend_yield",
            label="殖利率法",
            value_per_share=DataPoint.missing("現金股利資料缺漏"),
        )

    avg_dividend = sum(values) / len(values)
    if avg_dividend <= 0:
        return ValuationMethod(
            key="dividend_yield",
            label="殖利率法",
            value_per_share=DataPoint.missing("平均現金股利為 0，不適用殖利率法"),
        )

    value = avg_dividend / required
    return ValuationMethod(
        key="dividend_yield",
        label="殖利率法",
        value_per_share=DataPoint.derived(value, inputs=[d.cash_dividend for d in window]),
        assumptions=(
            f"近 {len(window)} 年平均現金股利 {avg_dividend:.2f} 元 ÷ 要求殖利率 {required:.1%}"
        ),
    )


def _method_dcf(company: Company, config: dict) -> ValuationMethod:
    """自由現金流折現。"""
    dcf_config = config.get("dcf") or {}
    years = int(dcf_config.get("projection_years", 10))
    discount = float(dcf_config.get("discount_rate", 0.09))
    terminal_growth = float(dcf_config.get("terminal_growth", 0.02))
    max_growth = float(dcf_config.get("max_growth_rate", 0.15))

    flows = company.annual_cash_flows()
    if not flows:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing("無現金流量表，無法進行 DCF"),
        )

    # 用近三年平均 FCF 當基期，單一年度的資本支出高峰不該決定整個估值。
    recent = [f.free_cash_flow for f in flows[-3:] if f.free_cash_flow.is_available]
    if not recent:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing("近年自由現金流缺漏，無法進行 DCF"),
        )

    base_fcf = sum(f.value for f in recent) / len(recent)  # type: ignore[misc]
    if base_fcf <= 0:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing("基期自由現金流 ≤ 0，DCF 無意義"),
        )

    shares = share_count(company)
    if not shares.is_available or shares.value is None:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing(shares.unavailable_reason or "缺股數，無法計算每股價值"),
        )

    growth_point = stability.eps_cagr(company)
    growth = 0.0
    if growth_point.is_available and growth_point.value is not None:
        growth = max(0.0, min(growth_point.value, max_growth))

    if discount <= terminal_growth:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing("折現率須大於永續成長率，設定有誤"),
        )

    present_value = 0.0
    projected = base_fcf
    for year in range(1, years + 1):
        projected *= 1 + growth
        present_value += projected / ((1 + discount) ** year)

    terminal_value = projected * (1 + terminal_growth) / (discount - terminal_growth)
    present_value += terminal_value / ((1 + discount) ** years)

    return ValuationMethod(
        key="dcf",
        label="自由現金流折現（DCF）",
        value_per_share=DataPoint.derived(present_value / shares.value, inputs=[*recent, shares]),
        assumptions=(
            f"基期 FCF {base_fcf / 1e8:.1f} 億元、成長 {growth:.1%}／年（{years} 年）、"
            f"折現率 {discount:.1%}、永續成長 {terminal_growth:.1%}"
        ),
    )


def _method_peg(company: Company, config: dict) -> ValuationMethod:
    """PEG＝1 隱含的合理本益比（合理 PE ≈ 成長率百分比）。"""
    years = int(config.get("normalized_eps_years", 5))
    eps = normalized_eps(company, years)
    growth = stability.eps_cagr(company)

    if not eps.is_available or not growth.is_available or growth.value is None:
        reason = (
            eps.unavailable_reason
            if not eps.is_available
            else growth.unavailable_reason or "缺成長率"
        )
        return ValuationMethod(
            key="peg",
            label="PEG＝1 隱含合理本益比",
            value_per_share=DataPoint.missing(reason or "資料不足"),
        )

    if growth.value <= 0:
        return ValuationMethod(
            key="peg",
            label="PEG＝1 隱含合理本益比",
            value_per_share=DataPoint.missing("EPS 成長率 ≤ 0，PEG 法不適用"),
        )

    floor = float(config.get("pe_band_floor", 8.0))
    cap = float(config.get("pe_band_cap", 30.0))
    fair_pe = max(floor, min(cap, growth.value * 100))

    return ValuationMethod(
        key="peg",
        label="PEG＝1 隱含合理本益比",
        value_per_share=DataPoint.derived(eps.value * fair_pe, inputs=[eps, growth]),  # type: ignore[operator]
        assumptions=(
            f"EPS 成長 {growth.value:.1%} → 合理本益比 {fair_pe:.1f} 倍"
            f"（夾於 {floor:.0f}~{cap:.0f} 倍），× 正常化 EPS {eps.value:.2f} 元"
        ),
    )


# --------------------------------------------------------------------------
# 交叉驗證
# --------------------------------------------------------------------------


def estimate_valuation(company: Company, scoring_config: dict) -> Valuation:
    """執行四種估值方法並交叉驗證。"""
    mos_config = scoring_config.get("margin_of_safety") or {}
    config = mos_config.get("valuation") or {}
    min_methods = int(mos_config.get("min_methods_required", 2))

    methods = [
        _method_normalized_pe(company, config),
        _method_dividend_yield(company, config),
        _method_dcf(company, config),
        _method_peg(company, config),
    ]
    valuation = Valuation(methods=methods, price=company.market_data.price)

    available = [m for m in methods if m.is_available]
    if len(available) < min_methods:
        valuation.note = (
            f"僅 {len(available)} 種估值方法可用，未達交叉驗證所需的 {min_methods} 種，"
            "不計安全邊際分數（規格第三節要求至少 2 種方法交叉驗證）"
        )
        valuation.intrinsic_value = DataPoint.missing(valuation.note)
        valuation.margin_of_safety = DataPoint.missing(valuation.note)
        return valuation

    values = [m.value_per_share.value for m in available]
    median_value = statistics.median(values)  # type: ignore[arg-type]
    valuation.intrinsic_value = DataPoint.derived(
        median_value, inputs=[m.value_per_share for m in available]
    )

    # 離散度：各方法相對中位數的最大偏離幅度。分歧過大代表估值本身不可靠。
    if median_value > 0:
        spread = max(abs(v - median_value) / median_value for v in values)  # type: ignore[operator]
        valuation.dispersion = DataPoint.derived(spread, inputs=[])
        threshold = float(mos_config.get("valuation", {}).get("dispersion_warning", 0.40))
        valuation.dispersion_warning = spread > threshold

    price = company.market_data.price
    if not price.is_available or price.value is None:
        valuation.margin_of_safety = DataPoint.missing("缺股價，無法計算安全邊際")
        valuation.note = "已估算內在價值，但缺最新股價，無法計算安全邊際"
        return valuation

    if median_value <= 0:
        valuation.margin_of_safety = DataPoint.missing("內在價值 ≤ 0，安全邊際無意義")
        return valuation

    mos = (median_value - price.value) / median_value
    valuation.margin_of_safety = DataPoint.derived(
        mos, inputs=[valuation.intrinsic_value, price]
    )
    valuation.note = (
        f"{len(available)} 種方法交叉驗證，中位數內在價值 {median_value:.1f} 元"
        f"，現價 {price.value:.1f} 元"
    )
    if valuation.dispersion_warning and valuation.dispersion.value is not None:
        valuation.note += f"；⚠ 各方法估值分歧達 {valuation.dispersion.value:.0%}，估值可靠度偏低"

    return valuation


__all__ = [
    "DEFAULT_PAR_VALUE",
    "Valuation",
    "ValuationMethod",
    "estimate_valuation",
    "justified_pe",
    "normalized_eps",
    "share_count",
]
