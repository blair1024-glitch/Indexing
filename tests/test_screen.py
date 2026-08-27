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
    MarketData,
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
def loader(tmp_path, monkeypatch):
    loader = DataLoader(config=Config.load(), repo_root=tmp_path)
    loader.history = _history({"1111": 0.55, "2222": 0.40, "3333": 0.12})

    # 第一階段現在會取用 TWSE/TPEx 的**整批**行情端點（抓一次、全市場共用）。
    # 這裡把它們換成空資料，讓整份測試維持離線——測試不該依賴網路，
    # 否則在封鎖外網的建置環境裡會卡在重試退避上。
    # 產業別不必處理：沒有 token 時 _stock_directory 本來就直接短路。
    monkeypatch.setattr(loader.twse, "market_data", lambda stock_id: MarketData())
    monkeypatch.setattr(loader.tpex, "market_data", lambda stock_id: MarketData())
    return loader


class TestStageOneMakesNoPerCompanyRequests:
    """第一階段跑 1,975 家。只要有一次**逐檔**請求溜進去，額度就會瞬間見底。

    分界不是「有沒有發請求」，而是「**每家一次**還是**全市場一次**」：

    - TWSE/TPEx 的端點抓整份再以代號建索引（``twse._fetch_indexed`` 的
      ``_index_cache``），第一檔觸發抓取、後面全是快取命中
    - FinMind 的股票總覽（``stock_directory``）也是一次涵蓋全市場

    這兩者第一階段可以用，加起來約 5 次請求。真正要擋的是 FinMind 的
    **逐檔**查詢——股利、資本支出、歷史本益比、逐檔股價。
    """

    @pytest.fixture
    def bulk_only(self, loader, monkeypatch):
        """整批來源換成固定資料；逐檔來源一律引爆。"""

        def explode(*args, **kwargs):
            raise AssertionError("第一階段不得發出逐檔請求")

        # 沒有 token 時 _stock_directory 會直接短路，總覽根本不會被呼叫——
        # 那樣這個測試就驗不到東西了。所有請求都已 mock，token 只是開關。
        monkeypatch.setattr(loader.finmind, "token", "test-token")
        monkeypatch.setattr(loader, "_load_history", explode)
        monkeypatch.setattr(loader.finmind, "latest_price", explode)
        monkeypatch.setattr(
            loader.finmind,
            "stock_directory",
            lambda: {"1111": {"industry": "半導體業", "market": "twse"}},
        )
        monkeypatch.setattr(
            loader.twse,
            "market_data",
            lambda stock_id: MarketData(
                price=DataPoint.of(123.0, "TWSE STOCK_DAY_ALL"),
                pe_ratio=DataPoint.of(15.0, "TWSE BWIBBU_ALL"),
            ),
        )
        return loader

    def test_history_only_skips_per_company_sources(self, bulk_only):
        from buffett00929.loader import build_lookup_constituent

        loaded = bulk_only.load_company(
            build_lookup_constituent("1111"), history_only=True
        )
        assert loaded.company.income_statements

    def test_stage_one_does_not_merge_the_latest_statements(self, bulk_only, monkeypatch):
        """第一階段的排序只用彙總報表的年度數，混入最新一期沒有意義。"""

        def explode(*args, **kwargs):
            raise AssertionError("第一階段不該合併最新一期財報")

        monkeypatch.setattr(bulk_only, "_overlay_official", explode)

        from buffett00929.loader import build_lookup_constituent

        bulk_only.load_company(build_lookup_constituent("1111"), history_only=True)

    def test_stage_one_still_gets_industry_and_price(self, bulk_only):
        """擋逐檔請求不該連整批端點一起擋掉——那是白丟資料，不是省額度。"""
        from buffett00929.loader import build_lookup_constituent

        loaded = bulk_only.load_company(
            build_lookup_constituent("1111"), history_only=True
        )
        assert loaded.company.industry == "半導體業"
        assert loaded.company.market_data.price.value == 123.0
        assert loaded.company.market_data.pe_ratio.value == 15.0

    def test_the_directory_is_fetched_once_for_the_whole_market(self, loader, monkeypatch):
        """1,975 家共用一份總覽。每家各抓一次就是 1,975 次請求。"""
        calls = []

        def counted():
            calls.append(1)
            return {"1111": {"industry": "半導體業", "market": "twse"}}

        monkeypatch.setattr(loader.finmind, "token", "test-token")
        monkeypatch.setattr(loader, "_load_history", lambda *a, **k: None)
        monkeypatch.setattr(loader.finmind, "stock_directory", counted)
        monkeypatch.setattr(
            loader.twse, "market_data", lambda stock_id: MarketData()
        )

        from buffett00929.loader import build_lookup_constituent

        for stock_id in ("1111", "2222", "3333"):
            loader.load_company(build_lookup_constituent(stock_id), history_only=True)

        assert len(calls) == 1

    def test_a_directory_failure_does_not_retry_per_company(self, loader, monkeypatch):
        """總覽抓失敗就整批放棄，不能退化成逐檔重試。"""
        from buffett00929.sources.base import SourceUnavailable

        calls = []

        def failing():
            calls.append(1)
            raise SourceUnavailable("額度用盡")

        monkeypatch.setattr(loader.finmind, "token", "test-token")
        monkeypatch.setattr(loader, "_load_history", lambda *a, **k: None)
        monkeypatch.setattr(loader.finmind, "stock_directory", failing)
        monkeypatch.setattr(
            loader.twse, "market_data", lambda stock_id: MarketData()
        )

        from buffett00929.loader import build_lookup_constituent

        for stock_id in ("1111", "2222", "3333"):
            loaded = loader.load_company(
                build_lookup_constituent(stock_id), history_only=True
            )
            assert loaded.company.industry == "未分類"

        assert len(calls) == 1

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
        """實測的健康執行是 25/50——門檻必須明顯低於它，不能貼著它。

        兩次資料完整的執行（8cad755 與 4ea8155）都是覆蓋率剛好 50%：
        另外 25 家是真的估不出價（方法不足或分歧過大），不是被限流。
        門檻若設在 0.50，健康執行就是以毫釐之差通過，任何一家公司
        少算出一個估值都會誤觸降級橫幅。
        """
        assert not self._result(usable=25).is_degraded
        assert not self._result(usable=24).is_degraded

    def test_a_throttled_batch_is_far_below_the_healthy_baseline(self):
        """被限流那次是 2/50。健康 50%、限流 4%，門檻要落在中間而非邊緣。"""
        assert self._result(usable=2).is_degraded

    def test_the_banner_appears_at_the_top_of_the_report(self):
        from buffett00929.report.markdown import render_screen

        text = render_screen(self._result(usable=0))
        head = text.split("## ")[0]
        assert "資料不完整" in head
        assert "FinMind" in head
