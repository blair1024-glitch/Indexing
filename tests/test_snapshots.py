"""快照儲存、評分變化歸因與財報變化表。"""

from __future__ import annotations

from datetime import date

import pytest

from buffett00929 import demo
from buffett00929.models import DataPoint, FiscalPeriod, IncomeStatement
from buffett00929.snapshots import (
    Snapshot,
    diff_snapshots,
    financial_changes,
    load_previous_snapshot,
    save_snapshot,
    summarize_report,
)


def snapshot_for(score, run_date):
    return Snapshot(run_date=run_date, constituents={}, companies=[score.to_dict()])


class TestPersistence:
    def test_round_trip(self, tmp_path, score_of):
        original = snapshot_for(score_of(demo.build_company()), date(2026, 8, 10))
        save_snapshot(tmp_path, original)
        loaded = load_previous_snapshot(tmp_path, before=date(2026, 8, 17))
        assert loaded is not None
        assert loaded.run_date == date(2026, 8, 10)
        assert loaded.companies[0]["stock_id"] == original.companies[0]["stock_id"]

    def test_only_earlier_snapshots_are_loaded(self, tmp_path, score_of):
        score = score_of(demo.build_company())
        save_snapshot(tmp_path, snapshot_for(score, date(2026, 8, 20)))
        assert load_previous_snapshot(tmp_path, before=date(2026, 8, 17)) is None

    def test_most_recent_earlier_snapshot_wins(self, tmp_path, score_of):
        score = score_of(demo.build_company())
        for day in (1, 5, 12):
            save_snapshot(tmp_path, snapshot_for(score, date(2026, 8, day)))
        loaded = load_previous_snapshot(tmp_path, before=date(2026, 8, 17))
        assert loaded.run_date == date(2026, 8, 12)

    def test_missing_directory_returns_none(self, tmp_path):
        assert load_previous_snapshot(tmp_path / "nope", before=date(2026, 8, 17)) is None


class TestScoreChanges:
    def test_headline_matches_specified_format(self, score_of):
        before = score_of(demo.build_company(stock_id="2330", name="台積電"))
        after = score_of(
            demo.build_company(stock_id="2330", name="台積電", gross_margin=0.28,
                               eps_growth=-0.10, gross_margin_drift=-0.02)
        )
        change = diff_snapshots(
            snapshot_for(after, date(2026, 8, 17)), snapshot_for(before, date(2026, 8, 10))
        )[0]
        assert change.headline.startswith("台積電：")
        assert "→" in change.headline
        assert change.arrow == "↓"
        assert change.delta < 0

    def test_explanation_attributes_to_components(self, score_of):
        before = score_of(demo.build_company(stock_id="1", name="A"))
        after = score_of(
            demo.build_company(stock_id="1", name="A", gross_margin=0.20,
                               operating_margin=0.06, gross_margin_drift=-0.02)
        )
        change = diff_snapshots(
            snapshot_for(after, date(2026, 8, 17)), snapshot_for(before, date(2026, 8, 10))
        )[0]
        assert "Moat" in change.explanation or "護城河" in change.explanation
        assert change.is_significant

    def test_new_flags_listed_in_explanation(self, score_of):
        before = score_of(demo.build_company(stock_id="1", name="A"))
        after = score_of(demo.build_company(stock_id="1", name="A", eps_growth=-0.15))
        change = diff_snapshots(
            snapshot_for(after, date(2026, 8, 17)), snapshot_for(before, date(2026, 8, 10))
        )[0]
        assert "新增紅旗" in change.explanation

    def test_resolved_flags_listed(self, score_of):
        before = score_of(demo.build_company(stock_id="1", name="A", eps_growth=-0.15))
        after = score_of(demo.build_company(stock_id="1", name="A"))
        change = diff_snapshots(
            snapshot_for(after, date(2026, 8, 17)), snapshot_for(before, date(2026, 8, 10))
        )[0]
        assert "紅旗解除" in change.explanation

    def test_unchanged_company_is_not_significant(self, score_of):
        score = score_of(demo.build_company(stock_id="1", name="A"))
        change = diff_snapshots(
            snapshot_for(score, date(2026, 8, 17)), snapshot_for(score, date(2026, 8, 10))
        )[0]
        assert change.delta == pytest.approx(0)
        assert not change.is_significant
        assert "無顯著變動" in change.explanation

    def test_first_run_marks_new_coverage(self, score_of):
        change = diff_snapshots(
            snapshot_for(score_of(demo.build_company()), date(2026, 8, 17)), None
        )[0]
        assert "新增追蹤" in change.headline


class TestFinancialChanges:
    def _quarters(self):
        def q(year, quarter, revenue, gross, operating, eps):
            def dp(v):
                return DataPoint.of(v, "t")

            return IncomeStatement(
                period=FiscalPeriod(year, quarter),
                revenue=dp(revenue),
                gross_profit=dp(gross),
                operating_income=dp(operating),
                eps=dp(eps),
            )

        return [
            q(2025, 2, 100, 45, 25, 2.0),
            q(2026, 1, 110, 52, 29, 2.3),
            q(2026, 2, 120, 50, 26, 2.5),
        ]

    def test_yoy_and_qoq_computed(self):
        changes = financial_changes(demo.build_company(), self._quarters())
        revenue = next(c for c in changes if c.label == "營收")
        assert revenue.qoq.value == pytest.approx(120 / 110 - 1)
        assert revenue.yoy.value == pytest.approx(120 / 100 - 1)

    def test_ratio_metrics_use_percentage_points(self):
        """毛利率 47.3% → 41.7% 是 −5.6pp，不是 −5.6% 的相對成長。"""
        changes = financial_changes(demo.build_company(), self._quarters())
        margin = next(c for c in changes if c.label == "毛利率")
        assert margin.is_percentage
        assert margin.qoq.value == pytest.approx(50 / 120 - 52 / 110)

    def test_trend_arrows(self):
        changes = financial_changes(demo.build_company(), self._quarters())
        assert next(c for c in changes if c.label == "營收").trend == "↑"
        assert next(c for c in changes if c.label == "毛利率").trend == "↓"

    def test_lower_is_better_metric_inverts_improvement(self):
        """負債比上升是變差，不是變好。"""
        changes = financial_changes(demo.build_company(debt_growth_final=0.8), [])
        debt = next((c for c in changes if "負債比" in c.label), None)
        if debt and debt.improved is not None:
            assert debt.improved is (debt.current.value < debt.previous.value)

    def test_summary_answers_better_or_worse(self):
        summary = summarize_report(financial_changes(demo.build_company(), self._quarters()))
        assert any(word in summary for word in ["轉好", "轉弱", "偏正面", "偏負面", "好壞參半"])

    def test_summary_reports_insufficient_data(self):
        assert "不足" in summarize_report([])

    def test_no_quarterly_data_yields_annual_only(self):
        changes = financial_changes(demo.build_company(), [])
        assert all("年度" in c.label for c in changes)


class TestMeasurementBasisChange:
    """可評分分母會因為資料可得性而變動——補上一個來源、或某來源當天被擋。

    分母一動，每一檔的總分都會整體位移。把那當成基本面變化來歸因，
    等於憑空製造一整批假訊號，而且是最有說服力的那種假訊號：
    數字真的變了、歸因也言之成理，只是講的不是同一件事。
    """

    def _entry(self, *, score: float, scorable: float) -> dict:
        return {
            "stock_id": "5269",
            "name": "祥碩科技",
            "total_score": score,
            "scorable_max": scorable,
            "grade": "B",
            "components": {
                "management": {"label": "Management", "normalized": 15.0},
            },
            "red_flags": {"triggered": []},
        }

    def _diff(self, before: dict, after: dict):
        from buffett00929.snapshots import Snapshot, diff_snapshots

        previous = Snapshot(run_date=date(2026, 8, 16), constituents={}, companies=[before])
        current = Snapshot(run_date=date(2026, 8, 17), constituents={}, companies=[after])
        return diff_snapshots(current, previous)[0]

    def test_a_denominator_shift_is_not_reported_as_a_finding(self):
        change = self._diff(
            self._entry(score=66.9, scorable=85.0),
            self._entry(score=72.4, scorable=100.0),
        )
        assert change.basis_changed
        assert not change.is_significant

    def test_the_explanation_says_the_basis_moved(self):
        change = self._diff(
            self._entry(score=66.9, scorable=85.0),
            self._entry(score=72.4, scorable=100.0),
        )
        assert "總分不可直接比較" in change.explanation
        assert "85" in change.explanation and "100" in change.explanation

    def test_the_headline_is_marked(self):
        change = self._diff(
            self._entry(score=66.9, scorable=85.0),
            self._entry(score=72.4, scorable=100.0),
        )
        assert "基準改變" in change.headline

    def test_same_basis_keeps_the_normal_attribution(self):
        """分母沒動時行為完全不變——這個修正不能把真實變化也一起壓掉。"""
        change = self._diff(
            self._entry(score=60.0, scorable=100.0),
            self._entry(score=72.4, scorable=100.0),
        )
        assert not change.basis_changed
        assert change.is_significant
        assert "不可直接比較" not in change.explanation

    def test_red_flags_are_reported_even_when_the_basis_moved(self):
        """紅旗是狀態不是分數，不受分母影響，絕不能被一起壓掉。"""
        before = self._entry(score=66.9, scorable=85.0)
        after = self._entry(score=72.4, scorable=100.0)
        after["red_flags"] = {
            "triggered": [{"code": "roe_decline", "label": "ROE 連續下降"}]
        }
        change = self._diff(before, after)
        assert change.basis_changed
        assert change.is_significant
        assert change.new_flags
        # 只講「基準改變」會把新增的重大紅旗說成「非基本面變動」，
        # 比原本的過度歸因更糟。實際執行時 3702 就是這樣被誤標的。
        assert "ROE 連續下降" in change.explanation


class TestVerdictChangeIsReported:
    """判斷是報表裡最可操作的一行，而它的變化原本完全看不見。

    停止衡量估值會讓判斷從 REDUCE 升到 HOLD——不是因為公司變好，
    而是因為安全邊際變成資料不足。等級變化會被報出來，判斷變化不會，
    所以讀者只看得到「升級」，看不到升級的原因。
    """

    def _entry(self, *, verdict: str, scorable: float = 100.0) -> dict:
        return {
            "stock_id": "4904",
            "name": "遠傳電信",
            "total_score": 70.2,
            "scorable_max": scorable,
            "grade": "B+",
            "verdict": verdict,
            "components": {"management": {"label": "Management", "normalized": 15.0}},
            "red_flags": {"triggered": []},
        }

    def _diff(self, before: dict, after: dict):
        from buffett00929.snapshots import Snapshot, diff_snapshots

        previous = Snapshot(run_date=date(2026, 8, 17), constituents={}, companies=[before])
        current = Snapshot(run_date=date(2026, 8, 18), constituents={}, companies=[after])
        return diff_snapshots(current, previous)[0]

    def test_a_verdict_change_is_explained(self):
        change = self._diff(
            self._entry(verdict="🟠 REDUCE / 降低曝險"),
            self._entry(verdict="🟡 HOLD / 持續觀察"),
        )
        assert "REDUCE" in change.explanation and "HOLD" in change.explanation

    def test_a_verdict_change_survives_a_basis_shift(self):
        """分母同時動了也一樣要報——這正是判斷會翻動的場合。"""
        change = self._diff(
            self._entry(verdict="🟠 REDUCE / 降低曝險", scorable=100.0),
            self._entry(verdict="🟡 HOLD / 持續觀察", scorable=90.0),
        )
        assert change.basis_changed
        assert change.is_significant
        assert "REDUCE" in change.explanation and "HOLD" in change.explanation

    def test_an_unchanged_verdict_is_not_mentioned(self):
        change = self._diff(
            self._entry(verdict="🟠 REDUCE / 降低曝險"),
            self._entry(verdict="🟠 REDUCE / 降低曝險"),
        )
        assert "判斷" not in change.explanation
