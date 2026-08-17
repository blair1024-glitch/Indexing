"""合成示範資料。

**這些數字全部是為了驗證計算邏輯而編造的，不對應任何真實公司。**
真實數據一律由 ``sources/`` 從官方來源抓取。

用途有二：

1. ``buffett00929 demo`` ——在還沒接上資料來源時先看到 Dashboard 版面，
   輸出會全程標示「合成範例資料」。
2. 測試——``build_company()`` 可調整體質，方便構造
   「高毛利穩定成長」「獲利衰退」「高槓桿高 ROE」「配息陷阱」等情境。
"""

from __future__ import annotations

from datetime import date

from .models import (
    BalanceSheet,
    CashFlowStatement,
    Company,
    DataPoint,
    DividendRecord,
    FiscalPeriod,
    IncomeStatement,
    MarketData,
)

SOURCE = "fixture:synthetic"
"""所有合成數據都用這個來源標記，報表上一眼可辨識非真實資料。"""


def dp(value: float | None, period: str = "", year: int = 2025) -> DataPoint:
    if value is None:
        return DataPoint.missing("fixture 未提供")
    return DataPoint.of(value, SOURCE, as_of=date(year, 12, 31), period=period)


def build_company(
    *,
    stock_id: str = "9999",
    name: str = "測試科技",
    years: int = 6,
    start_year: int = 2020,
    revenue_start: float = 100e8,
    revenue_growth: float = 0.10,
    gross_margin: float = 0.45,
    gross_margin_drift: float = 0.0,
    operating_margin: float = 0.25,
    net_margin: float = 0.20,
    equity_ratio: float = 0.65,
    shares: float = 10e8,
    share_growth: float = 0.0,
    eps_start: float = 8.0,
    eps_growth: float = 0.10,
    ocf_ratio: float = 1.10,
    capex_ratio: float = 0.25,
    dividend_payout: float = 0.60,
    price: float = 100.0,
    pe_history: list[float] | None = None,
    dividend_yield: float | None = 0.045,
    non_operating_ratio: float = 0.05,
    rnd_ratio: float = 0.08,
    receivable_ratio: float = 0.15,
    receivable_ratio_final: float | None = None,
    inventory_ratio: float = 0.12,
    debt_ratio_of_equity: float = 0.20,
    debt_growth_final: float | None = None,
) -> Company:
    """建構一家合成公司。

    所有參數都有合理的預設值（一家體質不錯的科技公司），
    測試只需覆寫想製造問題的那幾項。``*_final`` 參數用來只改變最後一年，
    方便構造「最新年度突然惡化」這類紅旗情境。
    """
    incomes: list[IncomeStatement] = []
    balances: list[BalanceSheet] = []
    flows: list[CashFlowStatement] = []
    dividends: list[DividendRecord] = []

    for index in range(years):
        year = start_year + index
        is_final = index == years - 1
        period = FiscalPeriod(year, 0)

        revenue = revenue_start * ((1 + revenue_growth) ** index)
        margin = gross_margin + gross_margin_drift * index
        gross_profit = revenue * margin
        op_income = revenue * operating_margin
        net_income = revenue * net_margin
        pretax = net_income / 0.8 if net_income else 0.0
        non_op = pretax * non_operating_ratio
        eps = eps_start * ((1 + eps_growth) ** index)
        share_count = shares * ((1 + share_growth) ** index)

        incomes.append(
            IncomeStatement(
                period=period,
                revenue=dp(revenue, str(period), year),
                cost_of_revenue=dp(revenue - gross_profit, str(period), year),
                gross_profit=dp(gross_profit, str(period), year),
                operating_expenses=dp(gross_profit - op_income, str(period), year),
                operating_income=dp(op_income, str(period), year),
                non_operating_income=dp(non_op, str(period), year),
                pretax_income=dp(pretax, str(period), year),
                net_income=dp(net_income, str(period), year),
                eps=dp(eps, str(period), year),
                rnd_expense=dp(revenue * rnd_ratio, str(period), year),
                finance_cost=dp(op_income * 0.03, str(period), year),
            )
        )

        equity = revenue * equity_ratio
        assets = equity / equity_ratio if equity_ratio else revenue
        debt = equity * debt_ratio_of_equity
        if is_final and debt_growth_final is not None:
            debt *= 1 + debt_growth_final
        recv_ratio = (
            receivable_ratio_final if (is_final and receivable_ratio_final is not None) else receivable_ratio
        )

        balances.append(
            BalanceSheet(
                period=period,
                total_assets=dp(assets, str(period), year),
                total_liabilities=dp(assets - equity, str(period), year),
                total_equity=dp(equity, str(period), year),
                current_assets=dp(assets * 0.55, str(period), year),
                current_liabilities=dp(assets * 0.22, str(period), year),
                cash=dp(assets * 0.20, str(period), year),
                inventory=dp(revenue * inventory_ratio, str(period), year),
                receivables=dp(revenue * recv_ratio, str(period), year),
                short_term_debt=dp(debt * 0.4, str(period), year),
                long_term_debt=dp(debt * 0.6, str(period), year),
                # 台股財報的「普通股股本」是金額，面額 10 元。
                shares_outstanding=dp(share_count * 10, str(period), year),
            )
        )

        ocf = net_income * ocf_ratio
        flows.append(
            CashFlowStatement(
                period=period,
                operating_cash_flow=dp(ocf, str(period), year),
                capex=dp(ocf * capex_ratio, str(period), year),
                depreciation=dp(revenue * 0.05, str(period), year),
                dividends_paid=dp(-net_income * dividend_payout, str(period), year),
            )
        )

        dividends.append(
            DividendRecord(
                year=year,
                cash_dividend=dp(eps * dividend_payout, str(year), year),
                stock_dividend=dp(0.0, str(year), year),
            )
        )

    market = MarketData(
        price=dp(price),
        pe_ratio=dp(price / eps_start if eps_start else None),
        dividend_yield=dp(dividend_yield) if dividend_yield is not None else DataPoint.missing("fixture 未提供殖利率"),
        pe_history=pe_history if pe_history is not None else [12, 14, 15, 16, 18, 20],
    )

    return Company(
        stock_id=stock_id,
        name=name,
        industry="半導體",
        market="TWSE",
        etf_weight=dp(0.035),
        income_statements=incomes,
        balance_sheets=balances,
        cash_flows=flows,
        dividends=dividends,
        market_data=market,
    )


def empty_company(stock_id: str = "0000", name: str = "無資料公司") -> Company:
    """完全沒有財報資料的公司，用來確認缺料不會被算成 0 分。"""
    return Company(stock_id=stock_id, name=name)


# --------------------------------------------------------------------------
# 完整示範執行
# --------------------------------------------------------------------------


def demo_constituents() -> "ConstituentSet":
    """合成成分股名單。

    **這不是 00929 的真實持股。** 代號與名稱皆為虛構，用來驗證版面與計算，
    真實名單一律由 ``sources/constituents.py`` 從官方來源抓取。
    """
    from .sources.constituents import Constituent, ConstituentSet

    specs = [
        ("8801", "示範半導體", 0.092),
        ("8802", "示範電子", 0.081),
        ("8803", "示範光電", 0.074),
        ("8804", "示範封測", 0.066),
        ("8805", "示範材料", 0.058),
        ("8806", "示範網通", 0.049),
        ("8807", "示範零組件", 0.041),
        ("8808", "示範資訊", 0.033),
    ]
    return ConstituentSet(
        constituents=[
            Constituent(
                stock_id=code,
                name=name,
                weight=DataPoint.of(weight, SOURCE, as_of=date(2026, 8, 15)),
            )
            for code, name, weight in specs
        ],
        source="合成範例資料（非真實成分股）",
        as_of=date(2026, 8, 15),
        attempts=["demo：使用合成資料，未連線任何外部來源"],
    )


def _demo_profiles() -> list[Company]:
    """八種體質各異的公司，讓每個榜單與警報都有東西可顯示。"""
    return [
        build_company(stock_id="8801", name="示範半導體", gross_margin=0.52,
                      operating_margin=0.34, net_margin=0.30, eps_growth=0.16, price=180),
        build_company(stock_id="8802", name="示範電子", gross_margin=0.28,
                      operating_margin=0.12, net_margin=0.09, eps_growth=0.05, price=62),
        build_company(stock_id="8803", name="示範光電", gross_margin=0.34,
                      gross_margin_drift=-0.025, eps_growth=-0.14, revenue_growth=-0.05,
                      price=45, dividend_yield=0.088, dividend_payout=1.20),
        build_company(stock_id="8804", name="示範封測", equity_ratio=0.24,
                      net_margin=0.13, debt_ratio_of_equity=1.5, price=95),
        build_company(stock_id="8805", name="示範材料", gross_margin=0.41,
                      capex_ratio=0.75, eps_growth=0.06, price=120),
        build_company(stock_id="8806", name="示範網通", gross_margin=0.38,
                      eps_growth=0.11, price=88, receivable_ratio_final=0.34),
        build_company(stock_id="8807", name="示範零組件", gross_margin=0.22,
                      operating_margin=0.08, net_margin=0.06, eps_growth=0.02,
                      share_growth=0.13, price=31),
        # 刻意只給市場資料，用來驗證「資料不足」的呈現與排除排名的行為。
        Company(stock_id="8808", name="示範資訊", industry="資訊服務",
                etf_weight=DataPoint.of(0.033, SOURCE, as_of=date(2026, 8, 15))),
    ]


def build_demo_run(config, today: date | None = None):
    """產生一次完整的合成分析，供 ``buffett00929 demo`` 使用。"""
    from .loader import LoadedCompany
    from .normalize import CumulativeDetection
    from .pipeline import AnalysisRun, analyse_company
    from .snapshots import diff_snapshots

    today = today or date.today()
    constituents = demo_constituents()

    weights = {c.stock_id: c.weight for c in constituents.constituents}

    results = []
    for company in _demo_profiles():
        # 權重以成分股名單為準，才會和「權重合計」對得起來。
        if company.stock_id in weights:
            company.etf_weight = weights[company.stock_id]
        company.note_gap("本檔為合成範例資料，非真實財報")
        loaded = LoadedCompany(
            company=company,
            detection=CumulativeDetection(True, "n/a", "合成資料，未進行累計數偵測"),
            quarterly_incomes=[],
        )
        results.append(analyse_company(loaded, config, today=today))

    run = AnalysisRun(
        run_date=today,
        constituents=constituents,
        results=results,
        warnings=["這是合成範例資料，所有數字均非真實財報"],
        is_demo=True,
    )
    run.score_changes = diff_snapshots(run.to_snapshot(), None)
    return run
