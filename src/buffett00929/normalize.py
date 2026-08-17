"""期別正規化：累計數 ↔ 單季數 ↔ 年度數。

台灣的損益表與現金流量表以**累計數**揭露：Q1 是第一季，Q2 是上半年累計，
Q3 是前三季累計，Q4 累計即全年。資產負債表則是**時點數**，不累計。

搞錯這件事的後果很嚴重：把累計數當單季用，QoQ 會憑空多出一倍成長；
把單季數當累計用，全年獲利會被低估四倍。因此本模組不假設格式，
而是**從資料本身偵測**，並把判斷依據記錄下來供報表查核。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import (
    BalanceSheet,
    CashFlowStatement,
    DataPoint,
    FiscalPeriod,
    IncomeStatement,
    safe_sub,
)


@dataclass
class CumulativeDetection:
    is_cumulative: bool
    confidence: str
    evidence: str

    def to_dict(self) -> dict:
        return {
            "is_cumulative": self.is_cumulative,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


def detect_cumulative(statements: Sequence[IncomeStatement]) -> CumulativeDetection:
    """判斷損益表序列是累計數還是單季數。

    判準：累計數的營收在同一年度內必然**單調不減**（Q1≤Q2≤Q3≤Q4）。
    單季數則會隨季節波動，出現下降。檢查所有完整年度後，
    只要有任一年度出現下降，就判定為單季數。
    """
    by_year: dict[int, list[IncomeStatement]] = {}
    for stmt in statements:
        if stmt.period.is_annual or not stmt.revenue.is_available:
            continue
        by_year.setdefault(stmt.period.year, []).append(stmt)

    checked_years = 0
    decreasing_years = 0
    for year, group in by_year.items():
        if len(group) < 3:
            continue
        group.sort(key=lambda s: s.period.quarter)
        values = [s.revenue.value for s in group]
        checked_years += 1
        if any(values[i + 1] < values[i] for i in range(len(values) - 1)):  # type: ignore[operator]
            decreasing_years += 1

    if checked_years == 0:
        # 沒有足夠資料判斷時，採台灣揭露慣例（累計）並標示低信心。
        return CumulativeDetection(
            is_cumulative=True,
            confidence="low",
            evidence="可比較的完整年度不足，採台灣財報揭露慣例（累計數）為預設",
        )

    if decreasing_years == 0:
        return CumulativeDetection(
            is_cumulative=True,
            confidence="high",
            evidence=f"檢查 {checked_years} 個年度，季度營收皆單調不減，符合累計數特徵",
        )

    return CumulativeDetection(
        is_cumulative=False,
        confidence="high" if decreasing_years >= max(1, checked_years // 2) else "medium",
        evidence=(
            f"檢查 {checked_years} 個年度，其中 {decreasing_years} 個年度出現季度營收下降，"
            "符合單季數特徵"
        ),
    )


# --------------------------------------------------------------------------
# 累計 → 單季
# --------------------------------------------------------------------------

_INCOME_FLOW_FIELDS = (
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "operating_expenses",
    "operating_income",
    "non_operating_income",
    "pretax_income",
    "net_income",
    "eps",
    "rnd_expense",
    "finance_cost",
)

_CASHFLOW_FLOW_FIELDS = ("operating_cash_flow", "capex", "depreciation", "dividends_paid")


def to_single_quarter(
    statements: Sequence[IncomeStatement], detection: CumulativeDetection
) -> list[IncomeStatement]:
    """把累計損益表換算為單季損益表。已是單季則原樣回傳。"""
    if not detection.is_cumulative:
        return list(statements)

    by_period = {s.period: s for s in statements}
    result: list[IncomeStatement] = []
    for stmt in statements:
        if stmt.period.is_annual or stmt.period.quarter == 1:
            # Q1 的累計數就是單季數。
            result.append(stmt)
            continue
        previous = by_period.get(FiscalPeriod(stmt.period.year, stmt.period.quarter - 1))
        if previous is None:
            # 缺前一季就無法還原單季數——標示缺料，不用累計數冒充。
            result.append(
                IncomeStatement(
                    period=stmt.period,
                    **{
                        name: DataPoint.missing(
                            f"缺 {stmt.period.year}Q{stmt.period.quarter - 1} 累計數，無法還原單季數"
                        )
                        for name in _INCOME_FLOW_FIELDS
                    },
                )
            )
            continue
        result.append(
            IncomeStatement(
                period=stmt.period,
                **{
                    name: safe_sub(getattr(stmt, name), getattr(previous, name))
                    for name in _INCOME_FLOW_FIELDS
                },
            )
        )
    return result


def cash_flow_to_single_quarter(
    flows: Sequence[CashFlowStatement], is_cumulative: bool
) -> list[CashFlowStatement]:
    """現金流量表的累計 → 單季換算。"""
    if not is_cumulative:
        return list(flows)

    by_period = {f.period: f for f in flows}
    result: list[CashFlowStatement] = []
    for flow in flows:
        if flow.period.is_annual or flow.period.quarter == 1:
            result.append(flow)
            continue
        previous = by_period.get(FiscalPeriod(flow.period.year, flow.period.quarter - 1))
        if previous is None:
            result.append(
                CashFlowStatement(
                    period=flow.period,
                    **{
                        name: DataPoint.missing("缺前一季累計數，無法還原單季現金流")
                        for name in _CASHFLOW_FLOW_FIELDS
                    },
                )
            )
            continue
        result.append(
            CashFlowStatement(
                period=flow.period,
                **{
                    name: safe_sub(getattr(flow, name), getattr(previous, name))
                    for name in _CASHFLOW_FLOW_FIELDS
                },
            )
        )
    return result


# --------------------------------------------------------------------------
# → 年度
# --------------------------------------------------------------------------


def to_annual_incomes(
    statements: Sequence[IncomeStatement], detection: CumulativeDetection
) -> list[IncomeStatement]:
    """彙總為年度損益表。

    累計數：直接取該年度 Q4（即全年累計）。
    單季數：四季相加，**且必須四季齊全**——只有三季就標示缺料，
    不用三季冒充全年（那會讓 EPS CAGR 出現假性衰退）。
    """
    annual_existing = [s for s in statements if s.period.is_annual]
    by_year: dict[int, list[IncomeStatement]] = {}
    for stmt in statements:
        if not stmt.period.is_annual:
            by_year.setdefault(stmt.period.year, []).append(stmt)

    result: list[IncomeStatement] = list(annual_existing)
    existing_years = {s.period.year for s in annual_existing}

    for year, group in sorted(by_year.items()):
        if year in existing_years:
            continue
        group.sort(key=lambda s: s.period.quarter)
        if detection.is_cumulative:
            q4 = next((s for s in group if s.period.quarter == 4), None)
            if q4 is None:
                continue
            result.append(
                IncomeStatement(
                    period=FiscalPeriod(year, 0),
                    **{name: getattr(q4, name) for name in _INCOME_FLOW_FIELDS},
                )
            )
        else:
            if len(group) != 4:
                continue
            result.append(
                IncomeStatement(
                    period=FiscalPeriod(year, 0),
                    **{name: _sum_points([getattr(s, name) for s in group]) for name in _INCOME_FLOW_FIELDS},
                )
            )

    result.sort(key=lambda s: s.period)
    return result


def to_annual_balances(sheets: Sequence[BalanceSheet]) -> list[BalanceSheet]:
    """年度資產負債表＝該年度最後一期（時點數，不累加）。"""
    annual_existing = {s.period.year: s for s in sheets if s.period.is_annual}
    by_year: dict[int, BalanceSheet] = {}
    for sheet in sheets:
        if sheet.period.is_annual:
            continue
        current = by_year.get(sheet.period.year)
        if current is None or sheet.period.quarter > current.period.quarter:
            by_year[sheet.period.year] = sheet

    result: list[BalanceSheet] = []
    for year in sorted(set(annual_existing) | set(by_year)):
        if year in annual_existing:
            result.append(annual_existing[year])
            continue
        latest = by_year[year]
        result.append(
            BalanceSheet(
                period=FiscalPeriod(year, 0),
                total_assets=latest.total_assets,
                total_liabilities=latest.total_liabilities,
                total_equity=latest.total_equity,
                current_assets=latest.current_assets,
                current_liabilities=latest.current_liabilities,
                cash=latest.cash,
                inventory=latest.inventory,
                receivables=latest.receivables,
                short_term_debt=latest.short_term_debt,
                long_term_debt=latest.long_term_debt,
                shares_outstanding=latest.shares_outstanding,
            )
        )
    return result


def to_annual_cash_flows(
    flows: Sequence[CashFlowStatement], is_cumulative: bool
) -> list[CashFlowStatement]:
    annual_existing = [f for f in flows if f.period.is_annual]
    existing_years = {f.period.year for f in annual_existing}
    by_year: dict[int, list[CashFlowStatement]] = {}
    for flow in flows:
        if not flow.period.is_annual:
            by_year.setdefault(flow.period.year, []).append(flow)

    result: list[CashFlowStatement] = list(annual_existing)
    for year, group in sorted(by_year.items()):
        if year in existing_years:
            continue
        group.sort(key=lambda f: f.period.quarter)
        if is_cumulative:
            q4 = next((f for f in group if f.period.quarter == 4), None)
            if q4 is None:
                continue
            result.append(
                CashFlowStatement(
                    period=FiscalPeriod(year, 0),
                    **{name: getattr(q4, name) for name in _CASHFLOW_FLOW_FIELDS},
                )
            )
        else:
            if len(group) != 4:
                continue
            result.append(
                CashFlowStatement(
                    period=FiscalPeriod(year, 0),
                    **{
                        name: _sum_points([getattr(f, name) for f in group])
                        for name in _CASHFLOW_FLOW_FIELDS
                    },
                )
            )

    result.sort(key=lambda f: f.period)
    return result


def _sum_points(points: Sequence[DataPoint]) -> DataPoint:
    """加總。任一期缺料則整體標示缺料——不容許用部分季度冒充全年。"""
    missing = [p for p in points if not p.is_available]
    if missing:
        return DataPoint.missing(missing[0].unavailable_reason or "部分期別資料不足，無法彙總全年")
    return DataPoint.derived(sum(p.value for p in points), inputs=list(points))  # type: ignore[misc]


__all__ = [
    "CumulativeDetection",
    "cash_flow_to_single_quarter",
    "detect_cumulative",
    "to_annual_balances",
    "to_annual_cash_flows",
    "to_annual_incomes",
    "to_single_quarter",
]
