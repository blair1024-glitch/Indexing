"""DataPoint 與缺失傳播——整個系統「絕不編造數字」的基礎。"""

from __future__ import annotations

import pytest

from buffett00929.models import (
    DataPoint,
    FiscalPeriod,
    cagr,
    safe_add,
    safe_div,
    safe_mean,
    safe_sub,
    stdev,
)


def dp(value):
    return DataPoint.of(value, "test")


class TestMissingPropagation:
    """缺料必須一路傳播，任何一步都不能悄悄變成 0。"""

    def test_missing_numerator_stays_missing(self):
        result = safe_div(DataPoint.missing("沒有營收"), dp(100))
        assert not result.is_available
        assert result.value is None
        assert "沒有營收" in result.unavailable_reason

    def test_missing_denominator_stays_missing(self):
        assert not safe_div(dp(100), DataPoint.missing("沒有股東權益")).is_available

    def test_zero_denominator_is_missing_not_infinity(self):
        result = safe_div(dp(100), dp(0))
        assert not result.is_available
        assert "分母為 0" in result.unavailable_reason

    @pytest.mark.parametrize(
        "operation",
        [
            lambda a, b: safe_sub(a, b),
            lambda a, b: safe_add(a, b),
            lambda a, b: safe_div(a, b),
        ],
    )
    def test_all_operations_propagate(self, operation):
        assert not operation(DataPoint.missing("缺"), dp(5)).is_available
        assert not operation(dp(5), DataPoint.missing("缺")).is_available

    def test_mean_requires_every_input(self):
        """3 年平均只有 2 年資料時不該回傳「2 年平均」冒充。"""
        result = safe_mean([dp(1), DataPoint.missing("缺 2024 年"), dp(3)])
        assert not result.is_available

    def test_available_values_compute_normally(self):
        assert safe_div(dp(50), dp(200)).value == pytest.approx(0.25)
        assert safe_mean([dp(1), dp(2), dp(3)]).value == pytest.approx(2.0)


class TestCagr:
    def test_normal_growth(self):
        assert cagr(dp(100), dp(200), 4).value == pytest.approx(2 ** 0.25 - 1)

    def test_negative_start_has_no_cagr(self):
        """由虧損轉盈沒有有意義的複合成長率，必須拒絕計算。"""
        result = cagr(dp(-10), dp(50), 4)
        assert not result.is_available
        assert "起始值" in result.unavailable_reason

    def test_negative_end_has_no_cagr(self):
        result = cagr(dp(50), dp(-10), 4)
        assert not result.is_available
        assert "期末值" in result.unavailable_reason

    def test_zero_years_rejected(self):
        assert not cagr(dp(100), dp(200), 0).is_available


class TestStdev:
    def test_needs_at_least_two_points(self):
        assert not stdev([dp(1)]).is_available

    def test_population_stdev(self):
        assert stdev([dp(2), dp(4), dp(4), dp(4), dp(5), dp(5), dp(7), dp(9)]).value == pytest.approx(2.0)


class TestProvenance:
    def test_value_carries_source(self):
        point = DataPoint.of(42, "TWSE:t187ap06_L_ci", period="2026Q2")
        assert point.provenance.source == "TWSE:t187ap06_L_ci"
        assert point.provenance.period == "2026Q2"

    def test_derived_inherits_oldest_as_of(self):
        """衍生值不能看起來比它最舊的輸入還新。"""
        from datetime import date

        old = DataPoint.of(1, "a", as_of=date(2024, 1, 1))
        new = DataPoint.of(2, "b", as_of=date(2026, 1, 1))
        assert safe_add(old, new).provenance.as_of == date(2024, 1, 1)

    def test_missing_describes_reason(self):
        assert "資料不足" in DataPoint.missing("來源未提供").describe()

    def test_require_raises_on_missing(self):
        with pytest.raises(ValueError, match="資料不足"):
            DataPoint.missing("沒有").require()


class TestFiscalPeriod:
    @pytest.mark.parametrize(
        "text,year,quarter",
        [("2026Q2", 2026, 2), ("2026 Q4", 2026, 4), ("2025", 2025, 0), ("2025FY", 2025, 0)],
    )
    def test_parse(self, text, year, quarter):
        period = FiscalPeriod.parse(text)
        assert (period.year, period.quarter) == (year, quarter)

    @pytest.mark.parametrize("text", ["2026Q5", "abc", "26Q1"])
    def test_parse_rejects_invalid(self, text):
        with pytest.raises(ValueError):
            FiscalPeriod.parse(text)

    def test_prev_quarter_crosses_year(self):
        assert FiscalPeriod(2026, 1).prev_quarter() == FiscalPeriod(2025, 4)

    def test_prev_year(self):
        assert FiscalPeriod(2026, 2).prev_year() == FiscalPeriod(2025, 2)

    def test_ordering(self):
        assert FiscalPeriod(2025, 4) < FiscalPeriod(2026, 1)
