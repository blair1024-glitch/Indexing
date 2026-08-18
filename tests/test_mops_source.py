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
Q3 = FiscalPeriod(2025, 3)
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
  <tr>
    <td>1103</td><td>非控制權益</td><td>3,000,000</td><td>1,000,000</td>
    <td>10,000</td><td>500,000</td>
    <td>1,684,000</td><td>2,000,000</td><td>2000.00</td>
  </tr>
  <tr>
    <td>1104</td><td>股本落單</td><td>30,000,000</td><td>10,000,000</td>
    <td>8,328,746</td><td>3,000,000</td>
    <td>7,715,235</td><td>7,750,000</td><td>11.00</td>
  </tr>
</table></td></tr></table>
"""

# 對應 BALANCE_WITH_CAPITAL 的損益表：淨利 ÷ EPS 是第三條獨立的股數推算路徑，
# 而且用的正是市場計算每股數字時的股數基準。
INCOME_FOR_SHARE_COUNT = """
<table><tr><td><table>
  <tr>
    <td>公司代號</td><td>公司名稱</td><td>營業收入</td>
    <td>淨利（淨損）歸屬於母公司業主</td><td>基本每股盈餘（元）</td>
  </tr>
  <tr>
    <td>2330</td><td>台積電</td><td>5,000,000</td><td>2,000</td><td>2.00</td>
  </tr>
  <tr>
    <td>1103</td><td>非控制權益</td><td>5,000,000</td><td>2,000</td><td>2.00</td>
  </tr>
  <tr>
    <td>1104</td><td>股本落單</td><td>30,000,000</td><td>1,402,770</td><td>2.00</td>
  </tr>
</table></td></tr></table>
"""

# 同一家公司、另一期，但**沒有**「淨利歸屬於母公司業主」欄。
# 本期淨利含非控制權益，EPS 只算母公司——相除會高估股數。
INCOME_WITHOUT_PARENT_COLUMN = """
<table><tr><td><table>
  <tr>
    <td>公司代號</td><td>公司名稱</td><td>營業收入</td>
    <td>本期淨利（淨損）</td><td>基本每股盈餘（元）</td>
  </tr>
  <tr>
    <td>1104</td><td>股本落單</td><td>30,000,000</td><td>1,700,000</td><td>2.00</td>
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

    def test_a_minority_interest_does_not_look_like_a_wrong_par_value(self, client):
        """MOPS 的每股參考淨值是用**權益總計**算的，我們拿的是母公司權益。

        兩者差的就是非控制權益，所以只要公司有子公司少數股東，驗算就過不了——
        實測退掉了 348 檔，約佔全市場 18%，遠超過「面額不是 10 元」的合理比例。
        範例（1103 嘉泥）：股本推得 832,874,600 股，母公司權益推得 701,385,039 股，
        比值 0.842 正好是母公司權益佔權益總計的比重。
        """
        parsed = client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY)
        shares = parsed["1103"].shares_outstanding
        assert shares.is_available
        assert shares.value == pytest.approx(1_000_000)
        assert "1103" not in client.share_count_rejections

    def test_the_basis_that_matched_is_recorded(self, client):
        """一次執行就要能回答「是哪一種基準相符」，否則只能再猜一輪。"""
        client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY)
        assert client.share_count_bases["1103"] == "權益總計"
        assert client.share_count_bases["2330"] == "母公司權益"

    def test_neither_basis_matching_is_still_refused(self, client):
        """兩種基準都對不上才是真的面額異常——那個退件必須保留。"""
        parsed = client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY)
        shares = parsed["9999"].shares_outstanding
        assert not shares.is_available
        reason = shares.unavailable_reason or ""
        assert "母公司權益" in reason and "權益總計" in reason

    def test_earnings_break_the_tie_against_the_par_value_path(self, client):
        """淨利 ÷ EPS 是第三條獨立路徑，而且是市場實際用的股數基準。

        1103 嘉泥的實測數字否定了非控制權益的說法：母公司權益推得
        701,385,039 股、權益總計推得 704,559,430 股——兩者只差 0.45%，
        這家公司幾乎沒有少數股東。真正的落差是股本推得的 832,874,600 股，
        比另外兩條路徑高 19%，也就是「股本 ÷ 10」這個假設本身不成立
        （特別股、庫藏股，或面額不是 10 元）。

        兩條路徑互相印證時就該採用它們，而不是採用落單的那一條。
        """
        history = MopsHistory()
        history.add("balance", client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY))
        history.add("income", client.parse_income(INCOME_FOR_SHARE_COUNT, Q1, TODAY))
        client.reconcile_share_counts(history)

        shares = history.balances["1104"][Q1].shares_outstanding
        assert shares.is_available
        # 母公司權益 77.15 億 ÷ 每股淨值 11.00 元 = 701,385,000 股
        # 淨利 14.03 億 ÷ EPS 2.00 元          = 701,385,000 股   兩條印證
        # 股本 83.29 億 ÷ 面額 10 元           = 832,874,600 股   落單，不採用
        assert shares.value == pytest.approx(701_385_000, rel=0.01)
        assert client.share_count_bases["1104"] == "淨利÷EPS＝權益÷每股淨值"

    def test_all_three_agreeing_is_left_alone(self, client):
        parsed_b = client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY)
        history = MopsHistory()
        history.add("balance", parsed_b)
        history.add("income", client.parse_income(INCOME_FOR_SHARE_COUNT, Q1, TODAY))
        client.reconcile_share_counts(history)
        assert history.balances["2330"][Q1].shares_outstanding.value == pytest.approx(1_000_000)

    def test_a_company_needing_correction_gets_it_in_every_period(self, client):
        """基準必須整條序列一致，不能逐期各自挑一個。

        修正只在「股本路徑與盈餘路徑不合」時觸發，於是盈餘路徑不可用的期別
        （EPS 太小、或缺損益表）就留著未修正的股本值，同一條序列裡兩種基準
        交錯。實測結果：8422 的股本年化成長率變成 +79.5%、5536 +21.4%，
        來源標示同時出現 t163sb04 與 t163sb05——公司什麼都沒做，
        只是我們在中途換了尺。

        需要修正的公司，其股本基準是結構性錯誤（特別股、庫藏股、面額非 10 元），
        不會只錯一期。驗不到的期別要標資料不足，不能沿用已知錯誤的那把尺。
        """
        history = MopsHistory()
        history.add("balance", client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY))
        history.add("income", client.parse_income(INCOME_FOR_SHARE_COUNT, Q1, TODAY))
        # 第二期的損益表沒有母公司欄，盈餘路徑不可信——不得沿用股本值。
        history.add("balance", client.parse_balance(BALANCE_WITH_CAPITAL, Q3, TODAY))
        history.add("income", client.parse_income(INCOME_WITHOUT_PARENT_COLUMN, Q3, TODAY))
        client.reconcile_share_counts(history)

        corrected = history.balances["1104"][Q1].shares_outstanding
        unverifiable = history.balances["1104"][Q3].shares_outstanding
        assert corrected.is_available
        assert not unverifiable.is_available, "驗不到的期別不得沿用股本推估值"
        assert "基準" in (unverifiable.unavailable_reason or "")

    def test_a_company_that_never_needed_correction_keeps_every_period(self, client):
        """沒問題的公司不受影響——這道規則不能把正常序列打出洞來。"""
        history = MopsHistory()
        history.add("balance", client.parse_balance(BALANCE_WITH_CAPITAL, Q1, TODAY))
        history.add("income", client.parse_income(INCOME_FOR_SHARE_COUNT, Q1, TODAY))
        history.add("balance", client.parse_balance(BALANCE_WITH_CAPITAL, Q3, TODAY))
        client.reconcile_share_counts(history)

        for period in (Q1, Q3):
            assert history.balances["2330"][period].shares_outstanding.is_available

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
