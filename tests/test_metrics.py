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
