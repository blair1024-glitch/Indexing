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


class TestRetainedEarningsPath:
    """盈餘再投資報酬先前只能靠「淨利 − 現金流量表的股利」算，
    而官方彙總報表**有保留盈餘餘額、沒有股利支付金額**，
    所以沒有第三方資料時整項指標永遠算不出來。餘額差額就是答案。"""

    def _company(self):
        from buffett00929.models import BalanceSheet, Company, IncomeStatement

        company = Company(stock_id="2330", name="台積電")
        company.income_statements = [
            IncomeStatement(period=FiscalPeriod(y, 0), net_income=point(ni, "MOPS"))
            for y, ni in ((2021, 100.0), (2022, 120.0), (2023, 150.0))
        ]
        company.balance_sheets = [
            BalanceSheet(period=FiscalPeriod(y, 0), retained_earnings=point(re, "MOPS"))
            for y, re in ((2021, 1000.0), (2022, 1060.0), (2023, 1150.0))
        ]
        return company

    def test_computed_from_balances_without_any_dividend_data(self):
        from buffett00929.metrics import reinvestment_return

        company = self._company()
        assert not company.cash_flows  # 完全沒有現金流量表
        result = reinvestment_return(company, years=5)
        # 淨利成長 150 − 100 = 50；累計保留盈餘 1150 − 1000 = 150
        assert result.value == pytest.approx(50 / 150)

    def test_falls_back_when_balances_lack_retained_earnings(self):
        """餘額缺漏時仍走原本的「淨利 − 股利」路徑，而不是直接放棄。"""
        from buffett00929.metrics import reinvestment_return
        from buffett00929.models import CashFlowStatement, DataPoint

        company = self._company()
        for sheet in company.balance_sheets:
            sheet.retained_earnings = DataPoint.missing("測試：無保留盈餘")
        company.cash_flows = [
            CashFlowStatement(period=FiscalPeriod(y, 0), dividends_paid=point(-40.0, "FinMind"))
            for y in (2021, 2022, 2023)
        ]
        result = reinvestment_return(company, years=5)
        # 分母＝(100−40)+(120−40)=140，分子 50
        assert result.value == pytest.approx(50 / 140)

    def test_non_positive_retained_earnings_is_meaningless(self):
        from buffett00929.metrics import reinvestment_return

        company = self._company()
        company.balance_sheets[-1].retained_earnings = point(900.0, "MOPS")
        assert not reinvestment_return(company, years=5).is_available


class TestCashBackfill:
    """一般業的彙總資產負債表沒有現金欄，但現金流量表有期末餘額。"""

    def test_cash_is_backfilled_from_the_cash_flow_statement(self, loader):
        from buffett00929.models import BalanceSheet, CashFlowStatement, Company
        from buffett00929.sources.mops import MopsHistory

        history = MopsHistory()
        history.add("balance", {"2330": BalanceSheet(period=FY2020)})
        history.add(
            "cashflow",
            {"2330": CashFlowStatement(period=FY2020, ending_cash=point(650.0, "MOPS"))},
        )
        loader.history = history

        company = Company(stock_id="2330", name="台積電")
        assert loader._load_from_mops(company)
        assert company.balance_sheets[0].cash.value == pytest.approx(650.0)

    def test_an_existing_cash_figure_is_not_overwritten(self, loader):
        from buffett00929.models import BalanceSheet, CashFlowStatement, Company
        from buffett00929.sources.mops import MopsHistory

        history = MopsHistory()
        history.add(
            "balance",
            {"2330": BalanceSheet(period=FY2020, cash=point(111.0, "MOPS 資產負債表"))},
        )
        history.add(
            "cashflow",
            {"2330": CashFlowStatement(period=FY2020, ending_cash=point(650.0, "MOPS"))},
        )
        loader.history = history

        company = Company(stock_id="2330", name="台積電")
        loader._load_from_mops(company)
        assert company.balance_sheets[0].cash.value == pytest.approx(111.0)


class TestConstituentCoverageReport:
    """「12 個欄位對不上」本身不可行動——讀者要問的是：影響到成分股了嗎？

    而且答案不能用版面推論，得直接看核心欄位在成分股身上有沒有值。
    """

    def _loaded(self, *, complete: bool):
        from buffett00929.loader import LoadedCompany
        from buffett00929.models import BalanceSheet, Company, IncomeStatement
        from buffett00929.normalize import CumulativeDetection

        company = Company(stock_id="5269", name="祥碩科技")
        company.income_statements = [
            IncomeStatement(
                period=FY2020,
                revenue=point(1000.0, "MOPS") if complete else DataPoint.missing("缺"),
                net_income=point(100.0, "MOPS"),
            )
        ]
        company.balance_sheets = [
            BalanceSheet(
                period=FY2020,
                total_assets=point(3000.0, "MOPS"),
                total_equity=point(2000.0, "MOPS"),
            )
        ]
        return [LoadedCompany(company=company, detection=CumulativeDetection(True, "high", ""))]

    def test_says_so_plainly_when_nothing_is_affected(self, loader):
        from buffett00929.sources.mops import MopsHistory

        loader.history = MopsHistory()
        loader._report_constituent_coverage(self._loaded(complete=True))
        assert any("未影響本次分析" in w for w in loader.warnings)

    def test_names_the_affected_constituents(self, loader):
        from buffett00929.sources.mops import MopsHistory

        loader.history = MopsHistory()
        loader._report_constituent_coverage(self._loaded(complete=False))
        affected = [w for w in loader.warnings if "確實缺漏" in w]
        assert affected and "5269" in affected[0]

    def test_stays_quiet_when_mops_was_not_used(self, loader):
        loader.history = None
        loader._report_constituent_coverage(self._loaded(complete=False))
        assert not loader.warnings


class TestShareCountUnitsAgreeAcrossSources:
    """兩個來源都提供股數時，單位必須一致。

    FinMind 的 OrdinaryShare／股本合計是**金額**，名字卻很像股數。
    直接塞進 shares_outstanding，會讓同一條序列混入金額與股數，
    年化成長率憑空變成 +74%～+105%——實測 50 檔裡 45 檔因此被標上
    「大量增資稀釋股東」重大紅旗，整個 ETF 被誤判為 AVOID。
    """

    def test_finmind_share_capital_becomes_a_share_count(self):
        from buffett00929.sources.base import PAR_VALUE, shares_from_share_capital

        shares = shares_from_share_capital(point(10_000_000.0, "FinMind"))
        assert shares.value == pytest.approx(10_000_000.0 / PAR_VALUE)

    def test_both_sources_land_on_the_same_order_of_magnitude(self):
        """同一家公司、同一筆股本，兩條路徑算出來的股數必須相同。"""
        from buffett00929.sources.base import shares_from_share_capital
        from buffett00929.sources.mops import MopsClient
        from buffett00929.sources.base import HttpClient

        capital_html = """
        <table><tr><td><table>
          <tr><td>公司代號</td><td>公司名稱</td><td>股本</td>
              <td>權益總計</td><td>每股參考淨值</td></tr>
          <tr><td>3702</td><td>大聯大</td><td>10,000</td>
              <td>20,000</td><td>20.00</td></tr>
        </table></td></tr></table>
        """
        mops = MopsClient(http=HttpClient(), config={})
        by_mops = mops.parse_balance(capital_html, FY2020, date(2026, 8, 18))["3702"]
        # MOPS 的股本是仟元：10,000 仟元 = 10,000,000 元 → 1,000,000 股
        by_finmind = shares_from_share_capital(point(10_000_000.0, "FinMind"))

        assert by_mops.shares_outstanding.value == pytest.approx(by_finmind.value)

    def test_missing_share_capital_stays_missing(self):
        from buffett00929.sources.base import shares_from_share_capital

        assert not shares_from_share_capital(DataPoint.missing("無")).is_available

    def test_finmind_balance_sheets_report_shares_not_the_capital_amount(self, monkeypatch):
        """關鍵回歸：直接走 FinMind 的解析路徑。

        OrdinaryShare 是股本金額。若它被原樣當成股數，這個測試就會看到
        10,000,000 而不是 1,000,000——正是讓 45 檔被誤標稀釋的那個值。
        """
        from buffett00929.sources.base import HttpClient
        from buffett00929.sources.finmind import FinMindClient

        client = FinMindClient(
            http=HttpClient(), config=Config.load().sources["finmind"], token="x"
        )
        monkeypatch.setattr(
            client,
            "fetch",
            lambda *_a, **_k: [
                {"date": "2020-12-31", "type": "OrdinaryShare", "value": 10_000_000.0},
                {"date": "2020-12-31", "type": "TotalAssets", "value": 50_000_000.0},
            ],
        )

        sheet = client.balance_sheets("3702")[0]
        assert sheet.shares_outstanding.value == pytest.approx(1_000_000.0)


class TestShareCountStaysSingleSourced:
    """成長率序列不能混來源。混了就會憑空長出重大紅旗。

    MOPS 現在會用「淨利 ÷ EPS」修正股本路徑算錯的股數，FinMind 則仍是
    股本 ÷ 10。兩者對同一家公司給出的基準不同，填進同一條序列後，
    長華電材（8070）的股本年化成長率變成 −42.9%、長華科技（6548）−55.1%——
    公司什麼都沒做，只是我們把兩把尺量出來的數字接在一起。
    """

    def _sheet(self, year: int, shares: float | None, source: str):
        from buffett00929.models import BalanceSheet, DataPoint, FiscalPeriod

        sheet = BalanceSheet(
            period=FiscalPeriod(year, 0),
            total_assets=DataPoint.of(1e10, source),
            total_equity=DataPoint.of(8e9, source),
        )
        if shares is not None:
            sheet.shares_outstanding = DataPoint.of(shares, source)
        return sheet

    def test_finmind_does_not_fill_a_share_count_mops_already_verified(self):
        """MOPS 驗過這家公司的股數，FinMind 那把尺就不准進同一條序列。"""
        from buffett00929.loader import drop_conflicting_share_counts

        official = [self._sheet(2022, 7.0e8, "MOPS 彙總報表 t163sb04")]
        incoming = [
            self._sheet(2023, 7.3e7, "FinMind:TaiwanStockBalanceSheet"),
            self._sheet(2024, 7.3e7, "FinMind:TaiwanStockBalanceSheet"),
        ]
        drop_conflicting_share_counts(official, incoming)
        assert all(not s.shares_outstanding.is_available for s in incoming)
        reason = incoming[0].shares_outstanding.unavailable_reason or ""
        assert "MOPS" in reason

    def test_other_fields_are_untouched(self):
        """只擋股數。FinMind 補的流動資產、資本支出等細項照常進來。"""
        from buffett00929.loader import drop_conflicting_share_counts

        official = [self._sheet(2022, 7.0e8, "MOPS 彙總報表 t163sb04")]
        incoming = [self._sheet(2023, 7.3e7, "FinMind:TaiwanStockBalanceSheet")]
        drop_conflicting_share_counts(official, incoming)
        assert incoming[0].total_assets.is_available
        assert incoming[0].total_equity.is_available

    def test_finmind_still_fills_when_mops_never_had_a_count(self):
        """MOPS 完全沒給股數時，FinMind 是唯一來源，不能一起擋掉。"""
        from buffett00929.loader import drop_conflicting_share_counts

        official = [self._sheet(2022, None, "MOPS 彙總報表 t163sb05")]
        incoming = [self._sheet(2023, 7.3e7, "FinMind:TaiwanStockBalanceSheet")]
        drop_conflicting_share_counts(official, incoming)
        assert incoming[0].shares_outstanding.is_available
