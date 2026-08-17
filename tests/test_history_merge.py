"""MOPS 與 FinMind 的歷史合併。

這裡釘住的兩個行為都是**靜默**失效的——不會拋錯、不會警告，
只會讓報表繼續顯示「資料不足」或給出低估的數字。沒有測試就會再犯。
"""

from __future__ import annotations

from datetime import date

import pytest

from buffett00929.config import Config
from buffett00929.loader import DataLoader
from buffett00929.models import (
    BalanceSheet,
    CashFlowStatement,
    Company,
    DataPoint,
    FiscalPeriod,
    IncomeStatement,
)
from buffett00929.normalize import to_annual_cash_flows

FY2020 = FiscalPeriod(2020, 0)


def point(value: float, source: str) -> DataPoint:
    return DataPoint.of(value, source)


class FakeFinMind:
    """只回傳現金流量表的假 FinMind，其餘資料集回空。"""

    is_available = True
    unavailable_reason = ""

    def __init__(self, cash_flows: list[CashFlowStatement]):
        self._cash_flows = cash_flows

    def income_statements(self, _stock_id): return []
    def balance_sheets(self, _stock_id): return []
    def cash_flows(self, _stock_id): return list(self._cash_flows)
    def dividends(self, _stock_id): return []
    def monthly_revenues(self, _stock_id): return []
    def pe_history(self, _stock_id): return []


def cumulative_2020_cash_flows() -> list[CashFlowStatement]:
    """FinMind 的原始形狀：Q1–Q4 **累計數**（台灣財報一律累計揭露）。

    全年營業現金流 400、資本支出 120，都在 Q4 的累計數上。
    """
    return [
        CashFlowStatement(
            period=FiscalPeriod(2020, quarter),
            operating_cash_flow=point(ocf, "FinMind"),
            capex=point(capex, "FinMind"),
        )
        for quarter, ocf, capex in (
            (1, 100.0, 30.0),
            (2, 200.0, 60.0),
            (3, 300.0, 90.0),
            (4, 400.0, 120.0),
        )
    ]


@pytest.fixture
def loader(tmp_path):
    return DataLoader(config=Config.load(), repo_root=tmp_path)


@pytest.fixture
def company_with_mops_history() -> Company:
    """MOPS 給的形狀：年度期別，有營業現金流、**沒有資本支出**。"""
    company = Company(stock_id="2330", name="台積電")
    company.cash_flows = [
        CashFlowStatement(
            period=FY2020,
            operating_cash_flow=point(400.0, "MOPS 彙總報表 t163sb20"),
            capex=DataPoint.missing("MOPS 彙總報表僅揭露投資活動淨額，無資本支出明細"),
        )
    ]
    return company


class TestCapexReachesTheAnnualStatement:
    """FinMind 補資本支出是使用者申請 token 的唯一理由，不能默默接不上。

    兩個來源的期別形狀不同：MOPS 是年度（season 04），FinMind 是 Q1–Q4。
    若不先彙總成年度就合併，季度資料只會被 append，
    接著 ``to_annual_cash_flows`` 因為該年度已有年度數而整組跳過，
    資本支出就此消失——沒有任何錯誤訊息。
    """

    def test_capex_fills_the_gap_mops_left(self, loader, company_with_mops_history):
        loader.finmind = FakeFinMind(cumulative_2020_cash_flows())
        loader._load_history(company_with_mops_history, fill_only=True)

        annual = next(f for f in company_with_mops_history.cash_flows if f.period == FY2020)
        assert annual.capex.is_available
        assert annual.capex.value == pytest.approx(120.0)

    def test_free_cash_flow_becomes_computable(self, loader, company_with_mops_history):
        loader.finmind = FakeFinMind(cumulative_2020_cash_flows())
        loader._load_history(company_with_mops_history, fill_only=True)

        annual = next(f for f in company_with_mops_history.cash_flows if f.period == FY2020)
        assert annual.free_cash_flow.value == pytest.approx(400.0 - 120.0)

    def test_official_operating_cash_flow_is_not_overwritten(
        self, loader, company_with_mops_history
    ):
        """FinMind 只補空缺。營業現金流兩邊都有，必須維持官方那一份。"""
        loader.finmind = FakeFinMind(cumulative_2020_cash_flows())
        loader._load_history(company_with_mops_history, fill_only=True)

        annual = next(f for f in company_with_mops_history.cash_flows if f.period == FY2020)
        assert "MOPS" in (annual.operating_cash_flow.provenance.source or "")


class TestAnnualCashFlowIsNotUnderstated:
    """年度現金流必須是全年，不是第四季。

    先前 ``_load_history`` 會先把序列轉成單季，之後 ``to_annual_cash_flows``
    又在 cumulative 模式下直接取 Q4 當全年——兩次轉換疊在一起，
    全年數字會被壓成大約四分之一。數字看起來仍然合理，所以不會被發現。
    """

    def test_annual_equals_the_fourth_quarter_cumulative_figure(self):
        annual = to_annual_cash_flows(cumulative_2020_cash_flows(), True)
        year = next(f for f in annual if f.period == FY2020)
        assert year.operating_cash_flow.value == pytest.approx(400.0)
        assert year.capex.value == pytest.approx(120.0)

    def test_merged_annual_figure_is_the_full_year(self, loader):
        """走完整合併路徑，年度數仍須是 400 而非 Q4 單季的 100。"""
        company = Company(stock_id="2330", name="台積電")
        loader.finmind = FakeFinMind(cumulative_2020_cash_flows())
        loader._load_history(company, fill_only=True)

        annual = next(f for f in company.cash_flows if f.period == FY2020)
        assert annual.operating_cash_flow.value == pytest.approx(400.0)

    def test_an_incomplete_year_produces_no_annual_figure(self, loader):
        """只有三季就不該產生年度數——那會讓 FCF 與盈再率出現假性衰退。"""
        company = Company(stock_id="2330", name="台積電")
        loader.finmind = FakeFinMind(cumulative_2020_cash_flows()[:3])
        loader._load_history(company, fill_only=True)

        assert not any(f.period == FY2020 for f in company.cash_flows)


class TestQuarterlyLevelStillAligns:
    """近兩年 MOPS 也有 Q1–Q3，季度層級要照樣對得上。"""

    def test_quarterly_gap_is_filled_without_touching_official_values(self, loader):
        company = Company(stock_id="2330", name="台積電")
        company.cash_flows = [
            CashFlowStatement(
                period=FiscalPeriod(2020, 2),
                operating_cash_flow=point(200.0, "MOPS 彙總報表 t163sb20"),
                capex=DataPoint.missing("MOPS 無資本支出明細"),
            )
        ]
        loader.finmind = FakeFinMind(cumulative_2020_cash_flows())
        loader._load_history(company, fill_only=True)

        q2 = next(f for f in company.cash_flows if f.period == FiscalPeriod(2020, 2))
        assert q2.capex.value == pytest.approx(60.0)
        assert "MOPS" in (q2.operating_cash_flow.provenance.source or "")
