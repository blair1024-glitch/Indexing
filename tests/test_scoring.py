"""評分聚合、估值與投資判斷。"""

from __future__ import annotations

import pytest

from buffett00929 import demo
from buffett00929.models import Company
from buffett00929.scoring import engine, valuation as valuation_module


class TestMissingDataNeverScoresZero:
    """整個系統最重要的安全性質：缺料不等於低分。"""

    def test_empty_company_has_no_scorable_max(self, score_of):
        score = score_of(Company(stock_id="0000", name="無資料"))
        assert score.scorable_max == 0
        assert score.coverage == 0

    def test_empty_company_is_not_rankable(self, score_of):
        assert not score_of(Company(stock_id="0000", name="無資料")).is_rankable

    def test_empty_company_gets_no_verdict(self, config, score_of):
        verdict, reason = engine.investment_verdict(
            score_of(Company(stock_id="0000", name="無資料")), config
        )
        assert verdict == engine.VERDICT_INSUFFICIENT
        assert "資料" in reason

    def test_unavailable_criteria_are_excluded_from_denominator(self, score_of):
        """缺料項目不計分也不計入分母，否則會被讀成「表現差」。"""
        score = score_of(demo.build_company(years=2))
        for component in score.components.values():
            for criterion in component.criteria:
                if not criterion.value.is_available:
                    assert criterion.points is None
            assert component.scorable_max == sum(
                c.max_points for c in component.criteria if c.is_scorable
            )

    def test_partial_data_scores_below_full_denominator(self, score_of):
        partial = score_of(demo.build_company(years=2))
        full = score_of(demo.build_company(years=6))
        assert partial.scorable_max < full.scorable_max
        assert partial.coverage < 1.0


class TestGrading:
    @pytest.mark.parametrize(
        "total,expected",
        [(95, "A+"), (85, "A"), (75, "B+"), (65, "B"), (55, "C"), (30, "D")],
    )
    def test_grade_boundaries(self, config, total, expected):
        assert engine.grade_for(total, config.scoring["grades"])[0] == expected


class TestSeparatedScores:
    def test_three_scores_are_reported_separately(self, score_of):
        """規格第十一節：好公司 ≠ 好價格，三個分數必須分開。"""
        score = score_of(demo.build_company())
        assert score.business_quality_score is not None
        assert score.financial_safety_score is not None
        assert score.valuation_score is not None

    def test_expensive_good_company_keeps_quality_but_loses_valuation(self, score_of):
        cheap = score_of(demo.build_company(price=60))
        expensive = score_of(demo.build_company(price=400))
        assert cheap.business_quality_score == pytest.approx(expensive.business_quality_score)
        assert expensive.valuation_score < cheap.valuation_score


class TestLeverageAdjustment:
    def test_leverage_driven_roe_is_capped(self, score_of):
        """規格第四、七節：靠負債撐出來的高 ROE 不得直接給高分。"""
        score = score_of(demo.build_company(equity_ratio=0.2, net_margin=0.12,
                                            debt_ratio_of_equity=1.6))
        assert score.metrics.high_roe_via_leverage
        roe_component = score.components["roe"]
        assert roe_component.adjustments
        assert roe_component.earned < roe_component.criteria_points

    def test_adjustment_records_its_reason(self, score_of):
        score = score_of(demo.build_company(equity_ratio=0.2, net_margin=0.12))
        adjustment = score.components["roe"].adjustments[0]
        assert adjustment.delta < 0
        assert "槓桿" in adjustment.reason

    def test_low_leverage_company_is_not_adjusted(self, score_of):
        score = score_of(demo.build_company(equity_ratio=0.8))
        assert score.components["roe"].adjustments == []


class TestValuation:
    def test_requires_two_methods_for_margin_of_safety(self, config):
        """規格第三節明訂至少 2 種方法交叉驗證，否則不給安全邊際分數。"""
        company = demo.build_company(years=2, pe_history=[], dividend_payout=0.0)
        company.dividends = []
        result = valuation_module.estimate_valuation(company, config.scoring)
        assert len(result.available_methods) < 2
        assert not result.margin_of_safety.is_available
        assert "交叉驗證" in result.note

    def test_healthy_company_has_all_four_methods(self, config):
        result = valuation_module.estimate_valuation(demo.build_company(), config.scoring)
        assert len(result.available_methods) == 4

    def test_intrinsic_value_is_median_of_methods(self, config):
        import statistics

        result = valuation_module.estimate_valuation(demo.build_company(), config.scoring)
        values = [m.value_per_share.value for m in result.available_methods]
        assert result.intrinsic_value.value == pytest.approx(statistics.median(values))

    def test_margin_of_safety_formula(self, config):
        result = valuation_module.estimate_valuation(demo.build_company(price=100), config.scoring)
        expected = (result.intrinsic_value.value - 100) / result.intrinsic_value.value
        assert result.margin_of_safety.value == pytest.approx(expected)

    @pytest.mark.parametrize(
        "mos,expected",
        [(0.35, "🟢 非常便宜"), (0.25, "🟢 便宜"), (0.15, "🟡 合理偏便宜"),
         (0.05, "🟡 合理"), (-0.2, "🔴 高估")],
    )
    def test_classification_bands(self, config, mos, expected):
        from buffett00929.models import DataPoint

        result = valuation_module.Valuation()
        result.margin_of_safety = DataPoint.of(mos, "t")
        assert result.classification == expected

    def test_share_count_converts_capital_at_par(self):
        """台股財報的「股本」是金額不是股數，面額 10 元。"""
        company = demo.build_company(shares=5e8)
        assert valuation_module.share_count(company).value == pytest.approx(5e8)

    def test_dispersion_warning_when_methods_disagree(self, config):
        result = valuation_module.estimate_valuation(
            demo.build_company(dividend_payout=0.95, eps_growth=0.14), config.scoring
        )
        assert result.dispersion.is_available


class TestVerdict:
    def test_declining_company_avoided(self, config, score_of):
        score = score_of(demo.build_company(eps_growth=-0.15, revenue_growth=-0.08,
                                            gross_margin_drift=-0.02))
        verdict, _ = engine.investment_verdict(score, config)
        assert verdict in (engine.VERDICT_AVOID, engine.VERDICT_REDUCE)

    def test_weak_balance_sheet_blocks_buy(self, config, score_of):
        """財務結構脆弱時，總分再高也不該是 BUY。"""
        score = score_of(demo.build_company(equity_ratio=0.2, net_margin=0.12,
                                            debt_ratio_of_equity=1.6, price=40))
        verdict, reason = engine.investment_verdict(score, config)
        assert verdict != engine.VERDICT_BUY
        if score.total_score >= 75:
            assert "財務安全" in reason

    def test_cheap_quality_company_is_buy(self, config, score_of):
        score = score_of(demo.build_company(price=45))
        verdict, _ = engine.investment_verdict(score, config)
        assert verdict == engine.VERDICT_BUY


class TestSingleQuarterCannotDriveVerdict:
    """規格第十五節：不得只根據單季 EPS 判斷。"""

    def test_verdict_ignores_quarterly_statements(self, config, score_of):
        from buffett00929.models import DataPoint, FiscalPeriod, IncomeStatement

        company = demo.build_company()
        before = engine.investment_verdict(score_of(company), config)

        # 塞進一個極端的單季損益表——年度序列完全不變。
        company.income_statements.append(
            IncomeStatement(
                period=FiscalPeriod(2026, 1),
                revenue=DataPoint.of(1, "t"),
                net_income=DataPoint.of(-1e12, "t"),
                eps=DataPoint.of(-500.0, "t"),
            )
        )
        company.income_statements.sort(key=lambda s: s.period)

        after = engine.investment_verdict(score_of(company), config)
        assert before == after


class TestSevenYearTest:
    def test_quality_company_answers_yes(self, config, score_of):
        answer, _ = engine.seven_year_test(score_of(demo.build_company()), config)
        assert answer == engine.HOLD_YES

    def test_declining_company_answers_no(self, config, score_of):
        score = score_of(demo.build_company(gross_margin=0.12, operating_margin=0.03,
                                            net_margin=0.02, eps_growth=-0.15,
                                            gross_margin_drift=-0.02))
        answer, _ = engine.seven_year_test(score, config)
        assert answer == engine.HOLD_NO

    def test_no_data_answers_unknown(self, config, score_of):
        answer, _ = engine.seven_year_test(score_of(Company(stock_id="0", name="空")), config)
        assert answer == engine.HOLD_UNKNOWN

    def test_price_does_not_affect_the_answer(self, config, score_of):
        """7 年持有問的是企業本身，不是價格。"""
        cheap, _ = engine.seven_year_test(score_of(demo.build_company(price=30)), config)
        pricey, _ = engine.seven_year_test(score_of(demo.build_company(price=900)), config)
        assert cheap == pricey


class TestOneLineConclusion:
    def test_matches_specified_sentence_shape(self, config, score_of):
        text = engine.one_line_conclusion(score_of(demo.build_company()), config)
        for fragment in ["這是一家", "最大的護城河是", "最大的風險是",
                         "目前股價相對內在價值", "因此我會"]:
            assert fragment in text

    def test_names_the_worst_flag_as_the_risk(self, config, score_of):
        score = score_of(demo.build_company(eps_growth=-0.15, gross_margin_drift=-0.02))
        text = engine.one_line_conclusion(score, config)
        assert any(f.label in text for f in score.red_flags.triggered)


class TestPerShareValuesUseAShareCount:
    """股本→股數的換算只能做一次。

    「股本」是金額，`shares_outstanding` 是股數，中間差一個面額。
    來源層換算完之後，估值層若再除一次，每股價值就會變成十倍——
    實際發生過：DCF 一度算出 13,921 元，而同一家公司的其他三種估值法
    都落在 577～1,443。四種方法的中位數因此被整個抬高。
    """

    def _company(self, shares: float):
        from buffett00929.models import BalanceSheet, Company, DataPoint, FiscalPeriod

        company = Company(stock_id="2458", name="義隆電子")
        company.balance_sheets = [
            BalanceSheet(
                period=FiscalPeriod(2025, 0),
                total_assets=DataPoint.of(1e10, "MOPS"),
                total_equity=DataPoint.of(8e9, "MOPS"),
                shares_outstanding=DataPoint.of(shares, "MOPS"),
            )
        ]
        return company

    def test_share_count_is_returned_unchanged(self):
        from buffett00929.scoring.valuation import share_count

        company = self._company(306_000_000.0)
        assert share_count(company).value == pytest.approx(306_000_000.0)

    def test_a_second_par_division_would_be_caught(self):
        """若有人再除一次面額，這個值會掉到 30,600,000。"""
        from buffett00929.scoring.valuation import share_count

        company = self._company(306_000_000.0)
        assert share_count(company).value > 100_000_000

    def test_missing_share_count_is_reported_not_guessed(self):
        from buffett00929.models import DataPoint
        from buffett00929.scoring.valuation import share_count

        company = self._company(306_000_000.0)
        company.balance_sheets[0].shares_outstanding = DataPoint.missing("無")
        result = share_count(company)
        assert not result.is_available
        assert "股數" in (result.unavailable_reason or "")


class TestNormalizationGuard:
    """正常化假設均值回歸。結構性衰退不是循環，平均值代表的是
    公司**已經失去**的獲利能力——用它估值會得出拿不回來的「便宜」。

    實例：可寧衛（8422）EPS 五年 CAGR −41%、目前每股盈餘約 1.3 元，
    但 5 年平均 8.81 元把它估到 133 元，+81% 安全邊際、「最便宜」榜首。
    """

    def _company(self, eps_by_year: list[float], dividends: list[float] | None = None):
        from buffett00929.models import (
            BalanceSheet, Company, DataPoint, DividendRecord, FiscalPeriod, IncomeStatement,
        )

        company = Company(stock_id="8422", name="可寧衛股")
        start = 2026 - len(eps_by_year)
        company.income_statements = [
            IncomeStatement(
                period=FiscalPeriod(start + i, 0),
                revenue=DataPoint.of(1e9, "MOPS"),
                net_income=DataPoint.of(eps * 1e8, "MOPS"),
                eps=DataPoint.of(eps, "MOPS"),
            )
            for i, eps in enumerate(eps_by_year)
        ]
        company.balance_sheets = [
            BalanceSheet(
                period=FiscalPeriod(start + i, 0),
                total_assets=DataPoint.of(1e10, "MOPS"),
                total_equity=DataPoint.of(8e9, "MOPS"),
                shares_outstanding=DataPoint.of(1e8, "MOPS"),
            )
            for i in range(len(eps_by_year))
        ]
        # 合理本益比取自歷史 PE 分位，沒有它 normalized_pe 會在守門之前就退出
        company.market_data.pe_history = [15.0] * 8
        for i, d in enumerate(dividends or []):
            company.dividends.append(
                DividendRecord(year=start + i, cash_dividend=DataPoint.of(d, "FinMind"))
            )
        return company

    def _config(self):
        from buffett00929.config import Config

        return (Config.load().scoring.get("valuation") or {})

    def test_collapsed_earnings_disable_the_normalised_pe(self):
        from buffett00929.scoring.valuation import _method_normalized_pe

        company = self._company([9.0, 9.0, 8.0, 7.0, 1.3])
        method = _method_normalized_pe(company, self._config())
        assert not method.value_per_share.is_available
        assert "正常化前提不成立" in (method.value_per_share.unavailable_reason or "")

    def test_a_steady_earner_is_untouched(self):
        """守門不能把正常運作的公司也一起關掉。"""
        from buffett00929.scoring.valuation import _method_normalized_pe

        company = self._company([9.0, 9.5, 10.0, 10.2, 10.5])
        method = _method_normalized_pe(company, self._config())
        assert method.value_per_share.is_available

    def test_a_collapsed_dividend_disables_the_yield_method(self):
        from buffett00929.scoring.valuation import _method_dividend_yield

        company = self._company([9.0] * 5, dividends=[10.0, 10.0, 1.2])
        method = _method_dividend_yield(company, self._config())
        assert not method.value_per_share.is_available
        assert "正常化前提不成立" in (method.value_per_share.unavailable_reason or "")

    def test_a_steady_dividend_is_untouched(self):
        from buffett00929.scoring.valuation import _method_dividend_yield

        company = self._company([9.0] * 5, dividends=[7.0, 7.2, 7.5])
        method = _method_dividend_yield(company, self._config())
        assert method.value_per_share.is_available
