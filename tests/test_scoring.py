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


class TestDispersionGate:
    """兩種方法差 7 倍不叫交叉驗證。

    實例：長華電材（8070）殖利率法 49.1 元、DCF 368.1 元，中位數 208.6 元，
    對上 49.5 元的股價得出 +76% 安全邊際、滿分 10/10，並登上「最便宜」榜首。
    分歧 76% 只被寫成一句註解，完全沒有影響評分。
    """

    def _diverging(self):
        # EPS 衰退擋掉 normalized_pe 與 PEG，只剩殖利率法與 DCF；
        # 股數偏小讓 DCF 每股價值遠高於殖利率法——正是 8070 的形狀。
        return demo.build_company(eps_growth=-0.20, shares=0.5e8)

    def test_wild_disagreement_refuses_to_score(self, config):
        result = valuation_module.estimate_valuation(self._diverging(), config.scoring)
        assert result.dispersion.value > 0.60
        assert not result.margin_of_safety.is_available
        assert not result.intrinsic_value.is_available
        assert "分歧" in (result.margin_of_safety.unavailable_reason or "")

    def test_dispersion_is_still_reported(self, config):
        """拒絕計分不等於藏起來——讀者要看得到分歧多少。"""
        result = valuation_module.estimate_valuation(self._diverging(), config.scoring)
        assert result.dispersion.is_available
        assert result.dispersion_warning

    def test_ordinary_disagreement_still_scores(self, config):
        """四種方法齊備時分歧約 48%，屬正常範圍，不得被這道閘門波及。"""
        result = valuation_module.estimate_valuation(demo.build_company(), config.scoring)
        assert 0.40 < result.dispersion.value <= 0.60
        assert result.dispersion_warning
        assert result.margin_of_safety.is_available


class TestShareCountCrossCheck:
    """股數與現金流不在同一基準上時，DCF 會給出漂亮但錯誤的每股價值。

    8070 的 DCF 隱含每股自由現金流 29.3 元、股價 49.5 元——自由現金流殖利率
    59%，而同一份報表的現金股利只佔那個 FCF 的 8%。兩者不可能同時為真。
    """

    def _company(self, shares: float, bvps: float | None = None, year: int = 2025):
        from buffett00929.models import (
            BalanceSheet, Company, DataPoint, FiscalPeriod,
        )

        company = Company(stock_id="8070", name="長華電材")
        sheet = BalanceSheet(
            period=FiscalPeriod(year, 0),
            total_assets=DataPoint.of(4e10, "MOPS"),
            total_equity=DataPoint.of(2.66e10, "MOPS"),
            shares_outstanding=DataPoint.of(shares, "derived(FinMind:TaiwanStockBalanceSheet)"),
        )
        if bvps is not None:
            sheet.book_value_per_share = DataPoint.of(bvps, "MOPS 彙總報表 t163sb05")
        company.balance_sheets = [sheet]
        return company

    def test_share_count_contradicted_by_book_value_is_refused(self):
        # 權益 266 億 ÷ 每股淨值 96.7 元 ≈ 2.75 億股，但股數欄位只有 0.724 億股。
        company = self._company(shares=7.24e7, bvps=96.7)
        result = valuation_module.share_count(company)
        assert not result.is_available
        reason = result.unavailable_reason or ""
        assert "每股淨值" in reason and "72,400,000" in reason

    def test_agreeing_share_count_passes(self):
        company = self._company(shares=2.75e8, bvps=96.7)
        assert valuation_module.share_count(company).value == pytest.approx(2.75e8)

    def test_no_book_value_means_no_verdict(self):
        """缺每股淨值時不能反過來把好的股數也擋掉。"""
        company = self._company(shares=7.24e7, bvps=None)
        assert valuation_module.share_count(company).is_available

    def test_dcf_names_the_share_count_it_used(self, config):
        """這次查不出問題，就是因為假設欄只印基期 FCF，不印股數。"""
        result = valuation_module.estimate_valuation(
            demo.build_company(shares=3.06e8), config.scoring
        )
        dcf = next(m for m in result.methods if m.key == "dcf")
        assert "股數" in dcf.assumptions
        assert "3.06" in dcf.assumptions or "306,000,000" in dcf.assumptions


class TestPegOnlySpeaksInsideItsBand:
    """PEG 的主張是「合理本益比 ≈ 成長率」。區間夾擠一旦接管，它就不再在講這件事。

    實測（2026-08-21，00929 五十檔）：PEG 可用 27 檔，其中 **15 檔被 8 倍下限
    夾住**、2 檔被 30 倍上限夾住，真正落在區間內的只有 10 檔。而在有 3 種以上
    方法的公司裡，離中位數最遠的方法**有 67% 是 PEG**——中華電、台灣大、遠傳
    三檔電信股的分歧度都卡在 62~63%，拿掉 PEG 之後掉到 6~10%。

    對成長 2% 的公司，PEG 給的「合理本益比 8 倍」與這家公司無關，
    那是區間下限的值。讓它進中位數與分歧度投票，等於讓一個固定倍數
    冒充成獨立的第三意見。
    """

    def _company(self, eps_growth: float):
        return demo.build_company(eps_growth=eps_growth)

    def _config(self):
        from buffett00929.config import Config

        return (Config.load().scoring.get("margin_of_safety") or {}).get("valuation") or {}

    def test_a_low_grower_gets_no_peg_opinion(self):
        from buffett00929.scoring.valuation import _method_peg

        method = _method_peg(self._company(0.02), self._config())
        assert not method.value_per_share.is_available
        reason = method.value_per_share.unavailable_reason or ""
        assert "區間" in reason and "2" in reason

    def test_a_grower_inside_the_band_still_gets_one(self):
        from buffett00929.scoring.valuation import _method_peg

        method = _method_peg(self._company(0.14), self._config())
        assert method.value_per_share.is_available
        assert "14" in method.assumptions

    def test_a_hypergrower_above_the_cap_gets_no_opinion(self):
        """成長 40% 卻用 30 倍，算的是 PEG=0.75 不是 PEG=1——名字會說謊。"""
        from buffett00929.scoring.valuation import _method_peg

        method = _method_peg(self._company(0.40), self._config())
        assert not method.value_per_share.is_available

    def test_the_existing_zero_growth_rule_is_unchanged(self):
        from buffett00929.scoring.valuation import _method_peg

        method = _method_peg(self._company(-0.05), self._config())
        assert not method.value_per_share.is_available
        assert "PEG 法不適用" in (method.value_per_share.unavailable_reason or "")
