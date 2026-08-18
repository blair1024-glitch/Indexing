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

# 版面依實測（2026-08-17）：表格**巢狀**、一頁 6 種業別表頭、
# 欄名一律用**全形括號**、業別之間用詞不一致（資產總計／資產總額）。
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
        <td>營業毛利（毛損）</td><td>營業毛利（毛損）淨額</td>
        <td>營業利益（損失）</td><td>稅前淨利（淨損）</td>
        <td>本期淨利（淨損）</td><td>淨利（淨損）歸屬於母公司業主</td>
        <td>基本每股盈餘（元）</td>
      </tr>
      <tr>
        <td>2330</td><td>台積電</td><td>1,000,000</td><td>400,000</td>
        <td>600,000</td><td>590,000</td><td>500,000</td><td>520,000</td>
        <td>430,000</td><td>420,000</td><td>16.10</td>
      </tr>
      <tr>
        <td>2317</td><td>鴻海</td><td>2,000,000</td><td>1,800,000</td>
        <td>200,000</td><td>200,000</td><td>120,000</td><td>130,000</td>
        <td>100,000</td><td>95,000</td><td>2.50</td>
      </tr>
    </table>
  </td></tr>
  <tr><td>
    <table>
      <tr><td colspan="6"><h2>金融保險業</h2></td></tr>
      <tr>
        <td>公司代號</td><td>公司名稱</td><td>利息淨收益</td><td>淨收益</td>
        <td>本期稅後淨利（淨損）</td><td>淨利（損）歸屬於母公司業主</td>
        <td>基本每股盈餘（元）</td>
      </tr>
      <tr>
        <td>2882</td><td>國泰金</td><td>120,000</td><td>300,000</td>
        <td>90,000</td><td>88,000</td><td>5.20</td>
      </tr>
    </table>
  </td></tr>
</table>
</body></html>
"""

BALANCE_HTML = """
<table><tr><td><table>
  <tr>
    <td>公司代號</td><td>公司名稱</td><td>流動資產</td><td>資產總計</td>
    <td>流動負債</td><td>負債總計</td>
    <td>歸屬於母公司業主之權益合計</td><td>權益總計</td>
  </tr>
  <tr>
    <td>2330</td><td>台積電</td><td>900,000</td><td>3,000,000</td>
    <td>500,000</td><td>1,000,000</td><td>1,900,000</td><td>2,000,000</td>
  </tr>
</table></td></tr>
<tr><td><table>
  <tr>
    <td>公司代號</td><td>公司名稱</td><td>現金及約當現金</td><td>資產總額</td>
    <td>負債總額</td><td>歸屬於母公司業主之權益</td><td>權益總額</td>
  </tr>
  <tr>
    <td>2882</td><td>國泰金</td><td>700,000</td><td>9,000,000</td>
    <td>8,400,000</td><td>580,000</td><td>600,000</td>
  </tr>
</table></td></tr></table>
"""

CASHFLOW_HTML = """
<table><tr><td><table>
  <tr>
    <td>公司代號</td><td>公司名稱</td><td>營業活動之淨現金流入（流出）</td>
    <td>投資活動之淨現金流入（流出）</td><td>籌資活動之淨現金流入（流出）</td>
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


class TestColumnNaming:
    """實測的欄名一律用全形括號，而且業別之間用詞不一致。

    這兩件事各自都會讓對應**部分**失敗——沒有括號的「營業收入」照樣命中，
    看起來像業別差異而不是對應寫錯，是最難察覺的一種失敗。
    """

    def test_full_width_parentheses_are_matched(self, client):
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        assert parsed["2330"].operating_income.value == pytest.approx(500_000 * 1000)
        assert parsed["2330"].eps.value == pytest.approx(16.10)

    def test_gross_profit_prefers_the_net_figure(self, client):
        """「淨額」已加計未實現／已實現銷貨損益，與營收同基礎。"""
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        assert parsed["2330"].gross_profit.value == pytest.approx(590_000 * 1000)

    def test_bank_wording_for_the_parent_share_is_matched(self, client):
        """銀行業寫「淨利（損）」，一般業寫「淨利（淨損）」。"""
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        assert parsed["2882"].net_income.value == pytest.approx(88_000 * 1000)

    def test_total_and_sum_wording_both_map(self, client):
        """一般業「資產總計」，金融業「資產總額」。"""
        parsed = client.parse_balance(BALANCE_HTML, Q1, TODAY)
        assert parsed["2330"].total_assets.value == pytest.approx(3_000_000 * 1000)
        assert parsed["2882"].total_assets.value == pytest.approx(9_000_000 * 1000)

    def test_equity_wording_variants_all_map(self, client):
        """「…之權益合計」與「…之權益」都要對得上，否則 ROE 分母會缺。"""
        parsed = client.parse_balance(BALANCE_HTML, Q1, TODAY)
        assert parsed["2330"].total_equity.value == pytest.approx(1_900_000 * 1000)
        assert parsed["2882"].total_equity.value == pytest.approx(580_000 * 1000)

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
        """毛利率用「營業毛利（毛損）淨額」590,000 ÷ 營收 1,000,000。"""
        parsed = client.parse_income(NESTED_HTML, Q1, TODAY)
        assert parsed["2330"].gross_margin.value == pytest.approx(0.59)

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


class TestSourcePriority:
    """規格第十九節：官方優先指的是**同一欄位**誰說了算，不是誰整筆覆蓋誰。

    整筆替換是最容易寫錯的地方，而且錯得很安靜——資料不會出錯，
    只會憑空變少（官方端點沒有的欄位被抹掉），看起來像來源缺料。
    """

    def _merge(self, existing, incoming, *, overwrite):
        from buffett00929.loader import _merge_statement
        from buffett00929.models import Company

        company = Company(stock_id="2330", name="台積電")
        _merge_statement(existing, incoming, company, "綜合損益表", overwrite=overwrite)
        return existing

    def test_official_value_wins_the_same_field(self, client):
        from buffett00929.models import DataPoint, IncomeStatement

        existing = [IncomeStatement(period=Q1, revenue=DataPoint.of(100.0, "MOPS"))]
        official = IncomeStatement(period=Q1, revenue=DataPoint.of(111.0, "TWSE"))

        merged = self._merge(existing, official, overwrite=True)
        assert merged[0].revenue.value == 111.0

    def test_fields_the_new_source_lacks_are_kept(self, client):
        """官方端點沒給研發費用時，不能把既有的值一起抹掉。"""
        from buffett00929.models import DataPoint, IncomeStatement

        existing = [
            IncomeStatement(
                period=Q1,
                revenue=DataPoint.of(100.0, "MOPS"),
                rnd_expense=DataPoint.of(9.0, "FinMind"),
            )
        ]
        official = IncomeStatement(period=Q1, revenue=DataPoint.of(111.0, "TWSE"))

        merged = self._merge(existing, official, overwrite=True)
        assert merged[0].rnd_expense.value == 9.0

    def test_fill_only_never_overwrites_official_numbers(self, client):
        """FinMind 只補空缺——它的角色是細項來源，不是官方數字的替代品。"""
        from buffett00929.models import DataPoint, IncomeStatement

        existing = [IncomeStatement(period=Q1, revenue=DataPoint.of(100.0, "MOPS"))]
        third_party = IncomeStatement(
            period=Q1,
            revenue=DataPoint.of(999.0, "FinMind"),
            rnd_expense=DataPoint.of(9.0, "FinMind"),
        )

        merged = self._merge(existing, third_party, overwrite=False)
        assert merged[0].revenue.value == 100.0
        assert merged[0].rnd_expense.value == 9.0

    def test_a_period_nobody_had_is_added_and_kept_sorted(self, client):
        from buffett00929.models import DataPoint, IncomeStatement

        existing = [IncomeStatement(period=FiscalPeriod(2025, 2))]
        incoming = IncomeStatement(period=Q1, revenue=DataPoint.of(1.0, "MOPS"))

        merged = self._merge(existing, incoming, overwrite=False)
        assert [s.period for s in merged] == sorted(s.period for s in merged)
        assert len(merged) == 2


BALANCE_WITH_CAPITAL = """
<table><tr><td><table>
  <tr>
    <td>公司代號</td><td>公司名稱</td><td>資產總計</td><td>負債總計</td>
    <td>股本</td><td>保留盈餘</td>
    <td>歸屬於母公司業主之權益合計</td><td>權益總計</td><td>每股參考淨值</td>
  </tr>
  <tr>
    <td>2330</td><td>台積電</td><td>3,000,000</td><td>1,000,000</td>
    <td>10,000</td><td>1,500,000</td>
    <td>2,000,000</td><td>2,000,000</td><td>2000.00</td>
  </tr>
  <tr>
    <td>9999</td><td>面額五元</td><td>3,000,000</td><td>1,000,000</td>
    <td>10,000</td><td>500,000</td>
    <td>2,000,000</td><td>2,000,000</td><td>1000.00</td>
  </tr>
</table></td></tr></table>
"""

CASHFLOW_WITH_ENDING = """
<table><tr><td><table>
  <tr>
    <td>公司代號</td><td>公司名稱</td><td>營業活動之淨現金流入（流出）</td>
    <td>期初現金及約當現金餘額</td><td>期末現金及約當現金餘額</td>
  </tr>
  <tr>
    <td>2330</td><td>台積電</td><td>700,000</td><td>400,000</td><td>650,000</td>
  </tr>
</table></td></tr></table>
"""


class TestShareCount:
    """「股本」是金額不是股數，換算要除以面額。面額並非一律 10 元，
    算錯的股數會讓「股本稀釋」無聲地給出錯誤結論——比缺料更糟。"""

    def test_derived_from_share_capital_at_par(self, client):
        # 股本 10,000 仟元 = 10,000,000 元 ÷ 面額 10 = 1,000,000 股
        # 驗算：權益 2,000,000 仟元 ÷ 每股淨值 2,000 = 1,000,000 股 ✓
        parsed = client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY)
        assert parsed["2330"].shares_outstanding.value == pytest.approx(1_000_000)

    def test_disagreeing_cross_check_reports_missing_not_a_wrong_count(self, client):
        """每股淨值推得 2,000,000 股，面額法推得 1,000,000 股——面額不是 10 元。"""
        parsed = client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY)
        shares = parsed["9999"].shares_outstanding
        assert not shares.is_available
        assert "面額" in (shares.unavailable_reason or "")

    def test_absent_share_capital_is_missing(self, client):
        parsed = client.parse_balance(BALANCE_HTML, Q1, TODAY)
        assert not parsed["2330"].shares_outstanding.is_available

    def test_a_rejected_share_count_is_recorded_not_silently_dropped(self, client):
        """退件是對的，但退完會沉默地回退到 FinMind 的股本——那條路徑沒有驗算。

        8070／6548 的 DCF 就是用了那個沒人驗過的股數，算出每股 368 元、
        股價 49.5 元、安全邊際 +76%。所以退件必須留下痕跡。
        """
        client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY)
        assert "9999" in client.share_count_rejections
        assert "2330" not in client.share_count_rejections
        assert str(Q1) in client.share_count_rejections["9999"]


class TestRetainedEarningsAndCash:
    def test_retained_earnings_balance_is_parsed(self, client):
        parsed = client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY)
        assert parsed["2330"].retained_earnings.value == pytest.approx(1_500_000 * 1000)

    def test_ending_cash_is_parsed_from_the_cash_flow_statement(self, client):
        parsed = client.parse_cashflow(CASHFLOW_WITH_ENDING, Q1, TODAY)
        assert parsed["2330"].ending_cash.value == pytest.approx(650_000 * 1000)
