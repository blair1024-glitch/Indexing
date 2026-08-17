"""期別正規化。

台灣財報以累計數揭露，這一層算錯會讓所有 QoQ 與年度數字失真，
而且錯得很隱蔽——數字仍然「看起來合理」。
"""

from __future__ import annotations

import pytest

from buffett00929.models import DataPoint, FiscalPeriod, IncomeStatement
from buffett00929.normalize import (
    detect_cumulative,
    to_annual_balances,
    to_annual_incomes,
    to_single_quarter,
)


def income(year, quarter, revenue, eps=None):
    return IncomeStatement(
        period=FiscalPeriod(year, quarter),
        revenue=DataPoint.of(revenue, "test"),
        eps=DataPoint.of(eps if eps is not None else revenue / 100, "test"),
    )


CUMULATIVE = [income(2025, 1, 100), income(2025, 2, 220), income(2025, 3, 340), income(2025, 4, 470)]
SINGLE = [income(2025, 1, 100), income(2025, 2, 90), income(2025, 3, 120), income(2025, 4, 160)]


class TestDetection:
    def test_monotonic_series_detected_as_cumulative(self):
        result = detect_cumulative(CUMULATIVE)
        assert result.is_cumulative
        assert result.confidence == "high"

    def test_fluctuating_series_detected_as_single_quarter(self):
        result = detect_cumulative(SINGLE)
        assert not result.is_cumulative

    def test_insufficient_data_defaults_to_taiwan_convention(self):
        """判斷不了時採台灣揭露慣例，但必須標示低信心。"""
        result = detect_cumulative([income(2025, 1, 100)])
        assert result.is_cumulative
        assert result.confidence == "low"
        assert "慣例" in result.evidence


class TestCumulativeToQuarterly:
    def test_subtracts_prior_cumulative(self):
        detection = detect_cumulative(CUMULATIVE)
        values = [s.revenue.value for s in to_single_quarter(CUMULATIVE, detection)]
        assert values == [100, 120, 120, 130]

    def test_single_quarter_data_passes_through(self):
        detection = detect_cumulative(SINGLE)
        values = [s.revenue.value for s in to_single_quarter(SINGLE, detection)]
        assert values == [100, 90, 120, 160]

    def test_missing_prior_quarter_yields_missing_not_cumulative(self):
        """缺 Q1 時 Q2 不能拿累計數冒充單季數。"""
        partial = [income(2025, 2, 220), income(2025, 3, 340)]
        detection = detect_cumulative(partial)
        result = to_single_quarter(partial, detection)
        q2 = next(s for s in result if s.period.quarter == 2)
        assert not q2.revenue.is_available
        assert "無法還原單季數" in q2.revenue.unavailable_reason


class TestAnnualAggregation:
    def test_cumulative_annual_is_q4(self):
        detection = detect_cumulative(CUMULATIVE)
        annual = to_annual_incomes(CUMULATIVE, detection)
        assert len(annual) == 1
        assert annual[0].revenue.value == 470

    def test_single_quarter_annual_is_sum(self):
        detection = detect_cumulative(SINGLE)
        annual = to_annual_incomes(SINGLE, detection)
        assert annual[0].revenue.value == pytest.approx(470)

    def test_partial_year_is_not_aggregated(self):
        """只有三季不能當成全年——那會讓 EPS CAGR 出現假性衰退。"""
        partial = SINGLE[:3]
        annual = to_annual_incomes(partial, detect_cumulative(partial))
        assert annual == []

    def test_cumulative_partial_year_without_q4_is_skipped(self):
        partial = CUMULATIVE[:3]
        annual = to_annual_incomes(partial, detect_cumulative(partial))
        assert annual == []

    def test_existing_annual_rows_preserved(self):
        annual_row = income(2024, 0, 900)
        result = to_annual_incomes([annual_row, *CUMULATIVE], detect_cumulative(CUMULATIVE))
        assert {s.period.year: s.revenue.value for s in result} == {2024: 900, 2025: 470}


class TestAnnualBalances:
    def test_uses_latest_quarter_not_sum(self):
        """資產負債表是時點數，年度值取最後一期，不能相加。"""
        from buffett00929.models import BalanceSheet

        sheets = [
            BalanceSheet(period=FiscalPeriod(2025, q), total_assets=DataPoint.of(1000 + q, "t"))
            for q in (1, 2, 3, 4)
        ]
        annual = to_annual_balances(sheets)
        assert len(annual) == 1
        assert annual[0].total_assets.value == 1004
