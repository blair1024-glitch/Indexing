"""MOPS 彙總報表解析。

fixture 依實測回應的**結構**撰寫（巢狀表格、分業別多表、仟元單位），
數值簡化以便驗算。真實回應是 1.5 MB、7 個表格、上千列。
"""

from __future__ import annotations

from datetime import date

import pytest

from buffett00929.models import FiscalPeriod
from buffett00929.sources.base import HttpClient
from buffett00929.sources.mops import MopsClient, MopsHistory, scan_rows

# 舊版 MOPS 的版面：表格是**巢狀**的，而且一頁有多個業別表格。
# 用 <table>…</table> 去切會在內層表格提早截斷——這正是要防的情況。
NESTED_HTML = """
<html><body>
<table>
  <tr><td>
    <table>
      <tr><td colspan="9"><h2>上市公司第一季資料</h2></td></tr>
      <tr><td>註1：單位：新台幣仟元</td></tr>
      <tr>
        <td>公司代號</td><td>公司名稱</td><td>營業收入</td><td>營業成本</td>
        <td>營業毛利(毛損)</td><td>營業利益(損失)</td><td>稅前淨利(淨損)</td>
        <td>本期淨利(淨損)</td><td>淨利(淨損)歸屬於母公司業主</td>
        <td>基本每股盈餘(元)</td>
      </tr>
      <tr>
        <td>2330</td><td>台積電</td><td>1,000,000</td><td>400,000</td>
        <td>600,000</td><td>500,000</td><td>520,000</td>
        <td>430,000</td><td>420,000</td><td>16.10</td>
      </tr>
      <tr>
        <td>2317</td><td>鴻海</td><td>2,000,000</td><td>1,800,000</td>
        <td>200,000</td><td>120,000</td><td>130,000</td>
        <td>100,000</td><td>95,000</td><td>2.50</td>
      </tr>
    </table>
  </td></tr>
  <tr><td>
    <table>
      <tr><td colspan="5"><h2>金融保險業</h2></td></tr>
      <tr>
        <td>公司代號</td><td>公司名稱</td><td>淨收益</td>
        <td>本期淨利(淨損)</td><td>基本每股盈餘(元)</td>
      </tr>
      <tr>
        <td>2882</td><td>國泰金</td><td>300,000</td><td>90,000</td><td>5.20</td>
      </tr>
    </table>
  </td></tr>
</table>
</body></html>
"""

BALANCE_HTML = """
<table><tr><td><table>
  <tr>
    <td>公司代號</td><td>公司名稱</td><td>流動資產</td><td>資產總額</td>
    <td>流動負債</td><td>負債總額</td>
    <td>歸屬於母公司業主之權益合計</td><td>權益總額</td>
  </tr>
  <tr>
    <td>2330</td><td>台積電</td><td>900,000</td><td>3,000,000</td>
    <td>500,000</td><td>1,000,000</td><td>1,900,000</td><td>2,000,000</td>
  </tr>
</table></td></tr></table>
"""

CASHFLOW_HTML = """
<table><tr><td><table>
  <tr>
    <td>公司代號</td><td>公司名稱</td><td>營業活動之淨現金流入(流出)</td>
    <td>投資活動之淨現金流入(流出)</td><td>籌資活動之淨現金流入(流出)</td>
  </tr>
  <tr>
    <td>2330</td><td>台積電</td><td>700,000</td><td>(500,000)</td><td>(150,000)</td>
  </tr>
</table></td></tr></table>
"""

Q1 = FiscalPeriod(2025, 1)
TODAY = date(2026, 8, 17)


@pytest.fixture
def client():
    return MopsClient(http=HttpClient(), config={})


class TestRowScanner:
    def test_nested_tables_do_not_truncate_the_scan(self):
        rows = list(scan_rows(NESTED_HTML))
        assert [row[0] for _header, row in rows] == ["2330", "2317", "2882"]

    def test_each_industry_table_carries_its_own_header(self):
        rows = list(scan_rows(NESTED_HTML))
        general_header = rows[0][0]
        financial_header = rows[2][0]
        assert "營業收入" in general_header
        assert "營業收入" not in financial_header
        assert "淨收益" in financial_header

    def test_note_rows_are_not_mistaken_for_data(self):
        """說明文字列與標題列的欄數對不上，必須被略過而不是解析成公司。"""
        ids = [row[0] for _h, row in scan_rows(NESTED_HTML)]
        assert all(i.isdigit() for i in ids)


class TestIncomeParsing:
    def test_thousands_are_converted_to_dollars(self, client):
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        assert parsed["2330"].revenue.value == pytest.approx(1_000_000 * 1000)

    def test_eps_is_not_scaled(self, client):
        """每股數值本來就是元，跟著乘一千會變成荒謬的每股盈餘。"""
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        assert parsed["2330"].eps.value == pytest.approx(16.10)

    def test_net_income_prefers_the_parent_company_share(self, client):
        """ROE 的分子要與分母（母公司權益）一致。"""
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        assert parsed["2330"].net_income.value == pytest.approx(420_000 * 1000)

    def test_derived_margins_work_end_to_end(self, client):
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        assert parsed["2330"].gross_margin.value == pytest.approx(0.6)

    def test_columns_absent_for_an_industry_are_missing_not_zero(self, client):
        """金融業沒有「營業成本」。填 0 會讓它的毛利率變成 100%。"""
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        financial = parsed["2882"]
        assert not financial.cost_of_revenue.is_available
        assert financial.cost_of_revenue.value is None
        assert "未提供" in (financial.cost_of_revenue.unavailable_reason or "")

    def test_missing_columns_are_recorded_for_review(self, client):
        client.parse_income(NESTED_HTML, Q1, TODAY)
        assert client.schema_watch.has_issues

    def test_provenance_records_period_and_source(self, client):
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        provenance = parsed["2330"].revenue.provenance
        assert provenance is not None
        assert provenance.period == "2025Q1"
        assert "MOPS" in provenance.source
        assert "t163sb04" in (provenance.url or "")


class TestBalanceParsing:
    def test_equity_prefers_the_parent_company_share(self, client):
        parsed = client.parse_balance(BALANCE_HTML, Q1, TODAY)
        assert parsed["2330"].total_equity.value == pytest.approx(1_900_000 * 1000)

    def test_debt_ratio_is_derivable(self, client):
        parsed = client.parse_balance(BALANCE_HTML, Q1, TODAY)
        assert parsed["2330"].debt_ratio.value == pytest.approx(1 / 3, rel=1e-3)

    def test_interest_bearing_debt_stays_missing(self, client):
        """彙總報表只有負債總額，沒有借款明細——不能拿總負債冒充有息負債。"""
        parsed = client.parse_balance(BALANCE_HTML, Q1, TODAY)
        assert not parsed["2330"].interest_bearing_debt.is_available


class TestCashFlowParsing:
    def test_operating_cash_flow_is_parsed(self, client):
        parsed = client.parse_cashflow(CASHFLOW_HTML, Q1, TODAY)
        assert parsed["2330"].operating_cash_flow.value == pytest.approx(700_000 * 1000)

    def test_capex_is_explicitly_unavailable(self, client):
        """投資活動淨額含金融資產買賣，拿來當資本支出會讓 FCF 全錯。"""
        capex = client.parse_cashflow(CASHFLOW_HTML, Q1, TODAY)["2330"].capex
        assert not capex.is_available
        assert "資本支出" in (capex.unavailable_reason or "")

    def test_free_cash_flow_reports_missing_rather_than_guessing(self, client):
        fcf = client.parse_cashflow(CASHFLOW_HTML, Q1, TODAY)["2330"].free_cash_flow
        assert not fcf.is_available


class TestHistoryIndex:
    def test_statements_come_back_sorted_by_period(self, client):
        history = MopsHistory()
        for period in (FiscalPeriod(2025, 2), FiscalPeriod(2024, 0), FiscalPeriod(2025, 1)):
            history.add("income", client.parse_income(NESTED_HTML, period, TODAY))
        periods = [s.period for s in history.income_statements("2330")]
        assert periods == sorted(periods)

    def test_unknown_company_returns_empty_not_error(self):
        assert MopsHistory().income_statements("9999") == []

    def test_company_count_spans_all_three_statements(self, client):
        history = MopsHistory()
        history.add("income", client.parse_income(NESTED_HTML, Q1, TODAY))
        history.add("balance", client.parse_balance(BALANCE_HTML, Q1, TODAY))
        assert history.company_count == 3


class TestRequestShape:
    def test_annual_periods_query_season_04(self, client, monkeypatch):
        sent = {}

        def fake_post_form(url, data, **kwargs):
            sent.update({"url": url, **data, **kwargs})
            return ""

        monkeypatch.setattr(client.http, "post_form", fake_post_form)
        client.fetch_report("t163sb04", "sii", FiscalPeriod(2020, 0), today=TODAY)

        assert sent["season"] == "04"
        assert sent["year"] == "109"  # 民國年
        assert sent["TYPEK"] == "sii"
        assert sent["url"].endswith("/mops/web/ajax_t163sb04")

    def test_settled_periods_are_cached_permanently(self, client, monkeypatch):
        captured = {}

        def fake_post_form(url, data, **kwargs):
            captured.update(kwargs)
            return ""

        monkeypatch.setattr(client.http, "post_form", fake_post_form)

        client.fetch_report("t163sb04", "sii", FiscalPeriod(2020, 0), today=TODAY)
        assert captured["immutable"] is True

        client.fetch_report("t163sb04", "sii", FiscalPeriod(2026, 2), today=TODAY)
        assert captured["immutable"] is False
