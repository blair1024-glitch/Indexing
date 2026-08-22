"""全市場掃描：把同一套邏輯的母體從 50 檔換成全市場。

MOPS 彙總報表以期別為單位抓取，一次涵蓋全市場（實測 1,975 家），
所以第一階段排企業品質不需要任何逐檔請求。FinMind 是逐檔的，
有速率上限，只能留給第二階段的前 N 名。
"""

from __future__ import annotations

from datetime import date

import pytest

from buffett00929.config import Config
from buffett00929.loader import DataLoader
from buffett00929.models import (
    BalanceSheet,
    CashFlowStatement,
    DataPoint,
    FiscalPeriod,
    IncomeStatement,
)
from buffett00929.sources.mops import MopsHistory

TODAY = date(2026, 8, 22)


def _history(specs: dict[str, float]) -> MopsHistory:
    """給每家公司五年年度報表，用毛利率高低製造品質差異。"""
    history = MopsHistory()
    for stock_id, margin in specs.items():
        history.names[stock_id] = f"公司{stock_id}"
        for year in range(2021, 2026):
            period = FiscalPeriod(year, 0)
            revenue = 100e8
            history.incomes.setdefault(stock_id, {})[period] = IncomeStatement(
                period=period,
                revenue=DataPoint.of(revenue, "MOPS 彙總報表 t163sb04"),
                gross_profit=DataPoint.of(revenue * margin, "MOPS 彙總報表 t163sb04"),
                operating_income=DataPoint.of(revenue * margin * 0.6, "MOPS 彙總報表 t163sb04"),
                net_income=DataPoint.of(revenue * margin * 0.5, "MOPS 彙總報表 t163sb04"),
                eps=DataPoint.of(margin * 50, "MOPS 彙總報表 t163sb04"),
            )
            history.balances.setdefault(stock_id, {})[period] = BalanceSheet(
                period=period,
                total_assets=DataPoint.of(200e8, "MOPS 彙總報表 t163sb05"),
                total_liabilities=DataPoint.of(80e8, "MOPS 彙總報表 t163sb05"),
                total_equity=DataPoint.of(120e8, "MOPS 彙總報表 t163sb05"),
                current_assets=DataPoint.of(90e8, "MOPS 彙總報表 t163sb05"),
                current_liabilities=DataPoint.of(40e8, "MOPS 彙總報表 t163sb05"),
                shares_outstanding=DataPoint.of(10e8, "MOPS 彙總報表 t163sb05"),
            )
            history.cash_flows.setdefault(stock_id, {})[period] = CashFlowStatement(
                period=period,
                operating_cash_flow=DataPoint.of(revenue * margin * 0.6, "MOPS 彙總報表 t163sb20"),
            )
    return history


@pytest.fixture
def loader(tmp_path):
    loader = DataLoader(config=Config.load(), repo_root=tmp_path)
    loader.history = _history({"1111": 0.55, "2222": 0.40, "3333": 0.12})
    return loader


class TestStageOneMakesNoPerCompanyRequests:
    """第一階段跑 1,975 家。只要有一次逐檔請求溜進去，額度就會瞬間見底。"""

    def test_history_only_skips_finmind_and_market_data(self, loader, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("第一階段不得發出逐檔請求")

        monkeypatch.setattr(loader, "_load_history", explode)
        monkeypatch.setattr(loader, "_overlay_official", explode)

        from buffett00929.loader import build_lookup_constituent

        loaded = loader.load_company(build_lookup_constituent("1111"), history_only=True)
        assert loaded.company.income_statements

    def test_the_full_path_still_calls_them(self, loader, monkeypatch):
        """這道開關不能把主流程一起關掉。"""
        calls = []
        monkeypatch.setattr(loader, "_load_history", lambda *a, **k: calls.append("history"))
        monkeypatch.setattr(loader, "_overlay_official", lambda *a, **k: calls.append("official"))

        from buffett00929.loader import build_lookup_constituent

        loader.load_company(build_lookup_constituent("1111"))
        assert "official" in calls


class TestQualityRanking:
    def test_companies_rank_by_business_quality(self, loader):
        from buffett00929.screen import rank_by_quality

        ranked = rank_by_quality(loader, Config.load(), today=TODAY)
        assert [r.company.stock_id for r in ranked][:2] == ["1111", "2222"]

    def test_names_come_from_the_bulk_reports(self, loader):
        from buffett00929.screen import rank_by_quality

        ranked = rank_by_quality(loader, Config.load(), today=TODAY)
        assert ranked[0].company.name == "公司1111"

    def test_the_scorable_denominator_reflects_what_mops_can_cover(self, loader):
        """第一階段沒有股利、資本支出、股價，分母必須小於 100 且要看得見。

        77 分制與 100 分制不可直接比較——報表要分開呈現，
        理由與 snapshots.py 處理分母位移相同。
        """
        from buffett00929.screen import rank_by_quality

        ranked = rank_by_quality(loader, Config.load(), today=TODAY)
        assert 0 < ranked[0].score.scorable_max < 100

    def test_a_company_with_no_financials_is_dropped_not_ranked_last(self, loader):
        """查無財報和「體質差」是兩件事，不能混在同一個排序裡。"""
        from buffett00929.screen import rank_by_quality

        loader.history.names["9999"] = "空殼"
        ranked = rank_by_quality(loader, Config.load(), today=TODAY)
        assert "9999" not in [r.company.stock_id for r in ranked]


class TestADegradedRunSaysSo:
    """來源被限流的執行看起來和「這些公司本來就估不出價」一模一樣。

    實測：連續跑三次掃描把 FinMind 額度用光之後，50 家裡 48 家變成
    「僅 0 種估值方法可用」，兩檔 BUY 候選整個消失，而報表照樣自信地
    列出結果，只在最下面的警告區塊提了一句。讀者會以為市場上沒有便宜的好公司，
    實際上是我們那次沒拿到股利與歷史本益比。

    覆蓋率低於門檻時必須在**最上面**講明白，而不是留給讀者自己推理。
    """

    def _result(self, usable: int, total: int = 50):
        from buffett00929.models import DataPoint
        from buffett00929.pipeline import CompanyResult
        from buffett00929.screen import ScreenResult
        from buffett00929.redflags import RedFlagReport
        from buffett00929.scoring.engine import CompanyScore
        from buffett00929.scoring.valuation import Valuation

        valued = []
        for i in range(total):
            valuation = Valuation()
            valuation.margin_of_safety = (
                DataPoint.of(0.1, "derived") if i < usable
                else DataPoint.missing("僅 0 種估值方法可用")
            )
            score = CompanyScore(
                company=None, metrics=None, valuation=valuation,  # type: ignore[arg-type]
                red_flags=RedFlagReport(),
            )
            valued.append(
                CompanyResult(score=score, verdict="", verdict_reason="",
                              seven_year="", seven_year_reason="", conclusion="")
            )
        return ScreenResult(valued=valued, universe_size=1975)

    def test_a_mostly_unvalued_batch_is_flagged(self):
        result = self._result(usable=2)
        assert result.is_degraded
        assert result.valuation_coverage < 0.10

    def test_a_healthy_batch_is_not_flagged(self):
        assert not self._result(usable=40).is_degraded

    def test_the_banner_appears_at_the_top_of_the_report(self):
        from buffett00929.report.markdown import render_screen

        text = render_screen(self._result(usable=0))
        head = text.split("## ")[0]
        assert "資料不完整" in head
        assert "FinMind" in head
