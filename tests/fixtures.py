"""合成測試資料。

**這些數字全部是為了測試計算邏輯而編造的，不對應任何真實公司。**
真實數據一律由 ``sources/`` 從官方來源抓取。

``build_company()`` 產生一家可調整體質的公司，讓測試能針對
「高毛利穩定成長」「獲利衰退」「高槓桿高 ROE」「配息陷阱」等情境
各自構造出對應的財報序列。
"""

from __future__ import annotations

from datetime import date

from buffett00929.models import (
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
