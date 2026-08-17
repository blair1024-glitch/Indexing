"""指標層。

``CompanyMetrics`` 是 metrics 與 scoring 之間唯一的介面：一次算完所有指標，
並以 ``criterion_values`` 對外提供「評分項目 → 指標值」的對應表。
這張表的 key 必須與 ``config/scoring.yaml`` 的 criteria key 完全一致，
``verify_criteria_coverage()`` 會在啟動時檢查兩者是否對得上——
設定檔加了新指標卻忘了實作時，要立刻報錯，而不是安靜地少算一項。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Company, DataPoint
from . import balance, cashflow, dividend, roe, stability

__all__ = [
    "CompanyMetrics",
    "balance",
    "cashflow",
    "dividend",
    "roe",
    "stability",
]


def reinvestment_return(company: Company, years: int = 5) -> DataPoint:
    """盈餘再投資報酬（ROIIC）——巴菲特評估管理階層的核心提問。

    公式：（期末淨利 − 期初淨利）／ 期間內累計保留盈餘

    意義是「公司把沒發出去的錢再投資，每 1 元換到多少獲利成長」。
    累計保留盈餘 ≤ 0（幾乎全數配出或持續虧損）時比率無意義，回傳缺料。
    """
    incomes = company.trailing_annual_incomes(years)
    flows = {f.period.year: f for f in company.annual_cash_flows()}

    if len(incomes) < 2:
        return DataPoint.missing("年度資料不足 2 年，無法計算盈餘再投資報酬")

    first, last = incomes[0].net_income, incomes[-1].net_income
    if not first.is_available or not last.is_available:
        return DataPoint.missing("缺期初或期末淨利，無法計算盈餘再投資報酬")

    inputs: list[DataPoint] = [first, last]

    # 首選：直接用資產負債表的保留盈餘餘額差額。
    # 期間累計保留盈餘就是期末餘額 − 期初餘額，不必回推股利——
    # 官方彙總報表有保留盈餘，卻沒有股利支付金額，
    # 靠股利回推會讓整項指標在沒有第三方資料時永遠算不出來。
    balances = {b.period.year: b for b in company.annual_balances()}
    opening = balances.get(incomes[0].period.year)
    closing = balances.get(incomes[-1].period.year)
    if (
        opening is not None
        and closing is not None
        and opening.retained_earnings.is_available
        and closing.retained_earnings.is_available
    ):
        retained_total = closing.retained_earnings.value - opening.retained_earnings.value  # type: ignore[operator]
        if retained_total <= 0:
            return DataPoint.missing("期間累計保留盈餘 ≤ 0，盈餘再投資報酬無意義")
        delta = last.value - first.value  # type: ignore[operator]
        return DataPoint.derived(
            delta / retained_total,
            inputs=[*inputs, opening.retained_earnings, closing.retained_earnings],
        )

    # 後備：淨利 − 股利。需要現金流量表的配息數字。
    retained_total = 0.0
    # 最後一年的保留盈餘還來不及貢獻該年度獲利，故不計入分母。
    for income in incomes[:-1]:
        net_income = income.net_income
        flow = flows.get(income.period.year)
        if not net_income.is_available or net_income.value is None:
            return DataPoint.missing(
                f"{income.period.year} 年缺淨利，無法累計保留盈餘"
            )
        dividends = flow.dividends_paid if flow else DataPoint.missing("缺現金流量表")
        if not dividends.is_available or dividends.value is None:
            return DataPoint.missing(
                f"{income.period.year} 年缺股利支付金額，無法計算保留盈餘"
                "（盈餘再投資報酬需要現金流量表的配息數字）"
            )
        # 現金流量表的股利為流出（負數），取絕對值。
        retained_total += net_income.value - abs(dividends.value)
        inputs.extend([net_income, dividends])

    if retained_total <= 0:
        return DataPoint.missing("期間累計保留盈餘 ≤ 0，盈餘再投資報酬無意義")

    delta = last.value - first.value  # type: ignore[operator]
    return DataPoint.derived(delta / retained_total, inputs=inputs)


@dataclass
class CompanyMetrics:
    """一家公司的完整指標集，附帶質化判定結果。"""

    company: Company
    criterion_values: dict[str, dict[str, DataPoint]] = field(default_factory=dict)

    # 質化／分類結果（不直接參與門檻帶評分，但會出現在報表與投資判斷中）
    dupont: roe.DuPont | None = None
    high_roe_via_leverage: bool = False
    high_roe_note: str = ""
    earnings_profile: str = stability.PROFILE_UNKNOWN
    earnings_quality_warnings: list = field(default_factory=list)
    capital_efficiency: str = cashflow.EFFICIENCY_UNKNOWN
    capital_efficiency_note: str = ""
    yield_trap: dividend.YieldTrapAssessment | None = None
    interest_coverage: DataPoint = field(default_factory=lambda: DataPoint.missing("未計算"))
    net_debt: DataPoint = field(default_factory=lambda: DataPoint.missing("未計算"))
    dividend_continuity: DataPoint = field(default_factory=lambda: DataPoint.missing("未計算"))
    dividend_growth: DataPoint = field(default_factory=lambda: DataPoint.missing("未計算"))
    payout_ratio: DataPoint = field(default_factory=lambda: DataPoint.missing("未計算"))

    def value(self, component: str, criterion: str) -> DataPoint:
        return self.criterion_values.get(component, {}).get(
            criterion, DataPoint.missing(f"指標 {component}.{criterion} 未計算")
        )

    def set_value(self, component: str, criterion: str, point: DataPoint) -> None:
        self.criterion_values.setdefault(component, {})[criterion] = point


def compute(company: Company, scoring_config: dict) -> CompanyMetrics:
    """計算一家公司的所有指標。

    ``scoring_config`` 只用來讀取判定門檻（例如高 ROE／高槓桿的界線），
    評分本身由 ``scoring`` 套件負責——指標層不決定分數。
    """
    metrics = CompanyMetrics(company=company)

    # -- Management（規格 M1）-------------------------------------------------
    metrics.set_value("management", "share_dilution", balance.share_count_growth(company))
    metrics.set_value("management", "roe_consistency", roe.roe_stdev(company))
    metrics.set_value("management", "reinvestment_return", reinvestment_return(company))
    metrics.set_value(
        "management", "earnings_quality", stability.non_operating_ratio_average(company)
    )

    # -- Moat（規格 M2）------------------------------------------------------
    metrics.set_value("moat", "gross_margin_level", stability.gross_margin_average(company))
    metrics.set_value("moat", "gross_margin_stability", stability.gross_margin_stdev(company))
    metrics.set_value(
        "moat", "operating_margin_level", stability.operating_margin_average(company)
    )
    metrics.set_value("moat", "rnd_intensity", stability.rnd_intensity_average(company))
    metrics.set_value("moat", "scale", stability.latest_revenue_in_100m(company))

    # -- ROE 與資本效率（規格第四節）-------------------------------------------
    metrics.set_value("roe", "roe_5y_avg", roe.roe_average(company, 5))
    metrics.set_value("roe", "roe_latest", roe.roe_latest(company))
    metrics.set_value("roe", "roe_trend", roe.roe_trend(company))
    metrics.set_value("roe", "leverage_quality", roe.latest_equity_multiplier(company))

    # -- 獲利穩定性（規格第五節）----------------------------------------------
    metrics.set_value("stability", "eps_cagr", stability.eps_cagr(company))
    metrics.set_value("stability", "revenue_cagr", stability.revenue_cagr(company))
    metrics.set_value("stability", "profit_consistency", stability.loss_years(company))
    metrics.set_value("stability", "margin_trend", stability.margin_trend(company))

    # -- 財務安全（規格第七節）------------------------------------------------
    metrics.set_value("financial_safety", "debt_ratio", balance.debt_ratio(company))
    metrics.set_value("financial_safety", "debt_to_equity", balance.debt_to_equity(company))
    metrics.set_value("financial_safety", "current_ratio", balance.current_ratio(company))
    metrics.set_value("financial_safety", "debt_to_fcf", balance.debt_to_fcf(company))

    # -- 自由現金流（規格第六節）----------------------------------------------
    metrics.set_value("free_cash_flow", "fcf_margin", cashflow.fcf_margin_average(company))
    metrics.set_value(
        "free_cash_flow", "fcf_to_net_income", cashflow.fcf_to_net_income_average(company)
    )
    metrics.set_value(
        "free_cash_flow", "reinvestment_rate", cashflow.reinvestment_rate_average(company)
    )

    # 安全邊際由 scoring/valuation.py 算完後回填（需要估值結果）。

    # -- 質化與分類 ----------------------------------------------------------
    roe_config = scoring_config.get("roe") or {}
    metrics.dupont = roe.dupont(company)
    metrics.high_roe_via_leverage, metrics.high_roe_note = roe.is_high_roe_via_leverage(
        company,
        roe_threshold=float(roe_config.get("high_roe_threshold", 0.15)),
        leverage_threshold=float(roe_config.get("high_leverage_threshold", 2.5)),
    )
    metrics.earnings_profile = stability.earnings_profile(company)
    metrics.earnings_quality_warnings = stability.earnings_quality_warnings(company)
    metrics.capital_efficiency, metrics.capital_efficiency_note = cashflow.capital_efficiency(
        company
    )

    flags_config = (scoring_config.get("red_flags") or {}).get("thresholds") or {}
    metrics.yield_trap = dividend.high_yield_trap(
        company,
        yield_threshold=float(flags_config.get("dividend_yield_trap_min", 0.06)),
        payout_max=float(flags_config.get("payout_ratio_max", 1.0)),
    )

    metrics.interest_coverage = balance.interest_coverage(company)
    metrics.net_debt = balance.net_debt(company)
    metrics.dividend_continuity = dividend.dividend_continuity(company)
    metrics.dividend_growth = dividend.dividend_growth(company)
    metrics.payout_ratio = dividend.payout_ratio(company)

    return metrics


def verify_criteria_coverage(scoring_config: dict) -> list[str]:
    """檢查 scoring.yaml 的每個 criterion 都有對應的指標實作。

    回傳缺漏清單（空 list 代表全部對得上）。設定檔新增評分項目卻忘了實作時，
    這裡會把問題指出來，而不是讓那一項永遠算不出分數卻沒人發現。
    """
    from ..models import Company as _Company

    probe = compute(_Company(stock_id="0000", name="probe"), scoring_config)
    problems: list[str] = []

    for component in scoring_config.get("weights", {}):
        expected = set((scoring_config.get(component) or {}).get("criteria", {}))
        # 安全邊際由估值模組回填，指標層不預先計算。
        if component == "margin_of_safety":
            continue
        actual = set(probe.criterion_values.get(component, {}))
        for missing in sorted(expected - actual):
            problems.append(f"{component}.{missing}：scoring.yaml 有設定但指標層未計算")
        for extra in sorted(actual - expected):
            problems.append(f"{component}.{extra}：指標層有計算但 scoring.yaml 未設定")

    return problems
