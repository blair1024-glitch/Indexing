"""指標層：ROE、穩定性、現金流、財務結構、股利。"""

from __future__ import annotations

import pytest

from buffett00929 import demo
from buffett00929.metrics import balance, cashflow, dividend, roe, stability
from buffett00929.models import BalanceSheet, Company, DataPoint, FiscalPeriod, IncomeStatement


class TestRoe:
    def test_uses_average_equity(self):
        company = demo.build_company(years=3)
        series = roe.annual_roe_series(company)
        assert all(point.is_available for _, point in series[1:])

    def test_negative_equity_yields_missing_not_inflated_roe(self):
        """權益為負時負負得正會產生假性高 ROE，必須拒絕計算。"""
        company = Company(
            stock_id="1",
            name="負權益",
            income_statements=[
                IncomeStatement(
                    period=FiscalPeriod(2025, 0), net_income=DataPoint.of(-100, "t")
                )
            ],
            balance_sheets=[
                BalanceSheet(
                    period=FiscalPeriod(2025, 0), total_equity=DataPoint.of(-50, "t")
                )
            ],
        )
        result = roe.roe_latest(company)
        assert not result.is_available
        assert "≤ 0" in result.unavailable_reason

    def test_average_requires_full_window(self):
        company = demo.build_company(years=3)
        assert not roe.roe_average(company, 5).is_available
        assert roe.roe_average(company, 3).is_available

    def test_high_roe_via_leverage_detected(self):
        company = demo.build_company(equity_ratio=0.2, net_margin=0.12)
        triggered, note = roe.is_high_roe_via_leverage(
            company, roe_threshold=0.15, leverage_threshold=2.5
        )
        assert triggered
        assert "槓桿" in note

    def test_low_leverage_high_roe_not_flagged(self):
        company = demo.build_company(equity_ratio=0.85, net_margin=0.25)
        triggered, _ = roe.is_high_roe_via_leverage(
            company, roe_threshold=0.15, leverage_threshold=2.5
        )
        assert not triggered

    def test_insufficient_data_does_not_claim_safe(self):
        triggered, note = roe.is_high_roe_via_leverage(
            Company(stock_id="1", name="空"), roe_threshold=0.15, leverage_threshold=2.5
        )
        assert not triggered
        assert "資料不足" in note

    def test_dupont_components_multiply_to_roe(self):
        company = demo.build_company()
        result = roe.dupont(company)
        product = (
            result.net_margin.value * result.asset_turnover.value * result.equity_multiplier.value
        )
        assert result.roe.value == pytest.approx(product)


class TestStability:
    def test_eps_cagr_matches_input_growth(self):
        company = demo.build_company(years=6, eps_growth=0.12)
        assert stability.eps_cagr(company, 6).value == pytest.approx(0.12, abs=1e-9)

    def test_declining_company_profiled_as_declining(self):
        company = demo.build_company(eps_growth=-0.12, revenue_growth=-0.06)
        assert stability.earnings_profile(company) == stability.PROFILE_DECLINING

    def test_steady_growth_profile(self):
        assert stability.earnings_profile(demo.build_company()) == stability.PROFILE_STEADY_GROWTH

    def test_loss_years_counted(self):
        company = demo.build_company(years=5, net_margin=-0.05)
        assert stability.loss_years(company, 5).value == 5

    def test_margin_drift_shows_in_trend(self):
        company = demo.build_company(years=6, gross_margin_drift=-0.02)
        assert stability.gross_margin_trend(company).value < 0

    def test_receivable_spike_flagged(self):
        company = demo.build_company(receivable_ratio=0.15, receivable_ratio_final=0.40)
        codes = {w.code for w in stability.earnings_quality_warnings(company)}
        assert "receivables_vs_revenue" in codes

    def test_healthy_company_has_no_quality_warnings(self):
        assert stability.earnings_quality_warnings(demo.build_company()) == []


class TestCashflow:
    def test_capital_efficient_company_classified(self):
        company = demo.build_company(capex_ratio=0.2, ocf_ratio=1.2)
        label, _ = cashflow.capital_efficiency(company)
        assert label == cashflow.EFFICIENCY_HIGH

    def test_capital_intensive_company_classified(self):
        company = demo.build_company(capex_ratio=0.8)
        label, _ = cashflow.capital_efficiency(company)
        assert label == cashflow.EFFICIENCY_CAPITAL_INTENSIVE

    def test_poor_cash_conversion_is_quality_warning(self):
        """營業現金流遠低於淨利＝獲利收不回現金，屬盈餘品質問題。"""
        company = demo.build_company(ocf_ratio=0.25, capex_ratio=0.1)
        label, note = cashflow.capital_efficiency(company)
        assert label == cashflow.EFFICIENCY_QUALITY_WARNING
        assert "營業現金流" in note

    def test_capital_intensive_is_not_mislabelled_as_quality_warning(self):
        """高資本支出讓 FCF 變薄，但營業現金流健康——這是資本密集，不是盈餘品質問題。"""
        company = demo.build_company(capex_ratio=0.8, ocf_ratio=1.15)
        assert cashflow.ocf_to_net_income_average(company).value > 1.0
        label, _ = cashflow.capital_efficiency(company)
        assert label == cashflow.EFFICIENCY_CAPITAL_INTENSIVE

    def test_unknown_when_no_data(self):
        label, _ = cashflow.capital_efficiency(Company(stock_id="1", name="空"))
        assert label == cashflow.EFFICIENCY_UNKNOWN

    def test_negative_net_income_excluded_from_conversion_ratio(self):
        """淨利為負時 FCF/淨利會變成誤導性的正數，該年度必須排除。"""
        company = demo.build_company(years=5, net_margin=-0.10)
        assert not cashflow.fcf_to_net_income_average(company).is_available


class TestBalance:
    def test_debt_to_fcf_missing_when_fcf_negative(self):
        company = demo.build_company(capex_ratio=2.0)
        result = balance.debt_to_fcf(company)
        assert not result.is_available
        assert "≤ 0" in result.unavailable_reason

    def test_share_dilution_annualized(self):
        company = demo.build_company(years=6, share_growth=0.08)
        assert balance.share_count_growth(company).value == pytest.approx(0.08, abs=1e-9)

    def test_no_dilution_reads_zero(self):
        company = demo.build_company(share_growth=0.0)
        assert balance.share_count_growth(company).value == pytest.approx(0.0, abs=1e-9)

    def test_debt_growth_undefined_from_zero_base(self):
        company = demo.build_company(debt_ratio_of_equity=0.0)
        assert not balance.debt_growth(company).is_available


class TestDividend:
    def test_payout_ratio_matches_input(self):
        company = demo.build_company(dividend_payout=0.6)
        assert dividend.payout_ratio(company).value == pytest.approx(0.6, abs=1e-6)

    def test_continuity_counts_streak(self):
        company = demo.build_company(years=6)
        assert dividend.dividend_continuity(company).value == 6

    def test_low_yield_not_evaluated_as_trap(self):
        """低殖利率公司的 EPS 下滑是別的問題，不該掛在殖利率陷阱下。"""
        company = demo.build_company(dividend_yield=0.02, eps_growth=-0.15)
        result = dividend.high_yield_trap(company, yield_threshold=0.06)
        assert not result.is_trap
        assert "未達" in result.note

    def test_high_yield_with_declining_eps_is_trap(self):
        company = demo.build_company(dividend_yield=0.09, eps_growth=-0.15)
        result = dividend.high_yield_trap(company, yield_threshold=0.06)
        assert result.is_trap
        assert any("EPS" in reason for reason in result.reasons)

    def test_high_yield_with_healthy_fundamentals_is_not_trap(self):
        company = demo.build_company(dividend_yield=0.075, eps_growth=0.10, dividend_payout=0.55)
        result = dividend.high_yield_trap(company, yield_threshold=0.06)
        assert not result.is_trap
        assert result.reasons == []

    def test_excessive_payout_is_trap(self):
        company = demo.build_company(dividend_yield=0.08, dividend_payout=1.3)
        result = dividend.high_yield_trap(company, yield_threshold=0.06, payout_max=1.0)
        assert result.is_trap
        assert any("配息率" in reason for reason in result.reasons)

    def test_missing_yield_does_not_claim_safe(self):
        company = demo.build_company(dividend_yield=None)
        result = dividend.high_yield_trap(company)
        assert not result.is_trap
        assert "缺殖利率" in result.note


class TestDuPontAgreesWithTheReportedRoe:
    """同一份報表不能出現兩個對不起來的 ROE。

    杜邦拆解的用途是解釋「這個 ROE 是怎麼來的」，所以它必須拆解
    **報表上那個 ROE**。先前它取「最新」資產負債表，而年中的資產負債表
    會被 normalize 標成當年度的年度數，於是拿今年年中的權益去配去年的損益，
    乘出來的 ROE 和明細表的年度 ROE 差了 0.5 個百分點。
    """

    def _company(self, *, with_partial_year: bool):
        from buffett00929.metrics import roe as roe_metrics
        from buffett00929.models import (
            BalanceSheet,
            Company,
            DataPoint,
            FiscalPeriod,
            IncomeStatement,
        )

        def dp(value):
            return DataPoint.of(value, "MOPS 彙總報表")

        company = Company(stock_id="5269", name="祥碩科技")
        company.income_statements = [
            IncomeStatement(
                period=FiscalPeriod(year, 0),
                revenue=dp(revenue),
                net_income=dp(net_income),
            )
            for year, revenue, net_income in ((2024, 900.0, 340.0), (2025, 1000.0, 400.0))
        ]
        company.balance_sheets = [
            BalanceSheet(
                period=FiscalPeriod(year, 0),
                total_assets=dp(assets),
                total_equity=dp(equity),
            )
            for year, assets, equity in ((2024, 2800.0, 2300.0), (2025, 3200.0, 2500.0))
        ]
        if with_partial_year:
            # 2026 只過了半年，normalize 會把年中的資產負債表標成 2026 年度數。
            company.balance_sheets.append(
                BalanceSheet(
                    period=FiscalPeriod(2026, 0),
                    total_assets=dp(3600.0),
                    total_equity=dp(2000.0),
                )
            )
        return company, roe_metrics

    def test_the_three_factors_multiply_back_to_the_reported_roe(self):
        company, roe_metrics = self._company(with_partial_year=False)
        decomposition = roe_metrics.dupont(company)
        assert decomposition.roe.value == pytest.approx(roe_metrics.roe_latest(company).value)

    def test_a_mid_year_balance_sheet_does_not_hijack_the_decomposition(self):
        """關鍵回歸：多了一筆年中的 2026 資產負債表，答案不該改變。"""
        company, roe_metrics = self._company(with_partial_year=True)
        decomposition = roe_metrics.dupont(company)
        assert decomposition.roe.value == pytest.approx(roe_metrics.roe_latest(company).value)

    def test_equity_multiplier_uses_the_same_averaged_basis(self):
        company, roe_metrics = self._company(with_partial_year=True)
        decomposition = roe_metrics.dupont(company)
        # 平均資產 (2800+3200)/2 = 3000，平均權益 (2300+2500)/2 = 2400
        assert decomposition.equity_multiplier.value == pytest.approx(3000 / 2400)

    def test_missing_same_year_balance_reports_insufficient_data(self):
        """寧可標示資料不足，也不拿別的年度的資產負債表硬湊。"""
        company, roe_metrics = self._company(with_partial_year=False)
        company.balance_sheets = [b for b in company.balance_sheets if b.period.year != 2025]
        decomposition = roe_metrics.dupont(company)
        assert not decomposition.roe.is_available
        assert "2025" in (decomposition.roe.unavailable_reason or "")


class TestShareCountGrowthNeedsOneRuler:
    """跨基準的成長率不是成長率，是換尺的幅度。

    股數有四條推算路徑（股本÷面額、權益÷每股淨值、淨利÷EPS、FinMind 股本），
    彼此對同一家公司可能給出差 20% 的數字。逐期各自挑一條，序列就會在中途換尺，
    而年化成長率會忠實地把換尺算成稀釋：實測 8422 +79.5%、6548 −55.1%、
    8070 −42.9%，全都足以觸發「股本快速膨脹」重大紅旗。公司什麼都沒做。

    這與 snapshots.py 對總分的處理是同一條原則：分母變了就不能直接相比。
    """

    def _company(self, series: list[tuple[int, float, str]]):
        from buffett00929.models import BalanceSheet, Company, DataPoint, FiscalPeriod

        company = Company(stock_id="8422", name="可寧衛股")
        company.balance_sheets = [
            BalanceSheet(
                period=FiscalPeriod(year, 0),
                shares_outstanding=DataPoint.of(shares, source),
            )
            for year, shares, source in series
        ]
        return company

    def test_a_basis_switch_is_not_reported_as_dilution(self):
        from buffett00929.metrics import balance

        company = self._company([
            (2021, 1.0e8, "MOPS 彙總報表 t163sb05"),
            (2025, 1.8e8, "MOPS 彙總報表 t163sb04"),
        ])
        result = balance.share_count_growth(company)
        assert not result.is_available
        assert "基準" in (result.unavailable_reason or "")

    def test_one_ruler_still_measures_real_dilution(self):
        from buffett00929.metrics import balance

        company = self._company([
            (2021, 1.0e8, "MOPS 彙總報表 t163sb05"),
            (2025, 1.8e8, "MOPS 彙總報表 t163sb05"),
        ])
        result = balance.share_count_growth(company)
        assert result.is_available
        assert result.value == pytest.approx(0.158, abs=0.005)

    def test_the_longest_single_basis_run_is_used(self):
        """換尺不該讓整項變成資料不足——同基準的區段還能用就用。"""
        from buffett00929.metrics import balance

        company = self._company([
            (2020, 5.0e8, "FinMind:TaiwanStockBalanceSheet"),
            (2022, 1.0e8, "MOPS 彙總報表 t163sb05"),
            (2023, 1.05e8, "MOPS 彙總報表 t163sb05"),
            (2025, 1.10e8, "MOPS 彙總報表 t163sb05"),
        ])
        result = balance.share_count_growth(company)
        assert result.is_available
        assert result.value == pytest.approx(0.0323, abs=0.005)


class TestTheTwoShareCountGuardsTogether:
    """移除合併層的封殺之後，序列會恢復成「MOPS 多年 + FinMind 最新一年」。

    這正是實際的資料形狀，也是我上次沒驗到的地方：只確認稀釋率分佈改善，
    沒檢查別的東西有沒有跟著動——結果 DCF 在 50 檔全數消失。
    兩道守門必須在這個形狀下同時成立。
    """

    def _company(self, series):
        from buffett00929.models import BalanceSheet, Company, DataPoint, FiscalPeriod

        company = Company(stock_id="8070", name="長華電材")
        company.balance_sheets = [
            BalanceSheet(
                period=FiscalPeriod(year, 0),
                shares_outstanding=DataPoint.of(shares, source),
            )
            for year, shares, source in series
        ]
        return company

    def _realistic(self):
        mops = "MOPS 彙總報表 t163sb05"
        return self._company([
            (2021, 1.00e8, mops),
            (2022, 1.02e8, mops),
            (2023, 1.04e8, mops),
            (2024, 1.06e8, mops),
            (2025, 1.08e8, mops),
            (2026, 7.30e7, "FinMind:TaiwanStockBalanceSheet"),
        ])

    def test_dilution_ignores_the_finmind_tail(self):
        """成長率在最長的同基準區段內衡量，換尺的那一年不參與。"""
        from buffett00929.metrics import balance

        result = balance.share_count_growth(self._realistic())
        assert result.is_available
        assert result.value == pytest.approx(0.0194, abs=0.003)

    def test_the_dcf_still_gets_a_share_count(self):
        """DCF 讀的是**最新年度**——那一期只有 FinMind，擋掉它 DCF 就整個不見。"""
        from buffett00929.scoring.valuation import share_count

        result = share_count(self._realistic())
        assert result.is_available
        assert result.value == pytest.approx(7.30e7)

    def test_a_contradicted_latest_count_is_still_refused(self):
        """恢復 FinMind 不等於放行錯誤股數——使用前仍要與每股淨值對帳。"""
        from buffett00929.models import DataPoint
        from buffett00929.scoring.valuation import share_count

        company = self._realistic()
        latest = company.balance_sheets[-1]
        latest.total_equity = DataPoint.of(2.66e10, "MOPS")
        latest.book_value_per_share = DataPoint.of(96.7, "MOPS")
        assert not share_count(company).is_available
