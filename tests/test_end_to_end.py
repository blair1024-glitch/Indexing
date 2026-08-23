"""端到端：demo 執行 → 排行榜 → Markdown 與 HTML 報表 → CLI。"""

from __future__ import annotations

from datetime import date

import copy

import pytest

from buffett00929 import demo
from buffett00929.cli import main
from buffett00929.report import dashboard, markdown


@pytest.fixture(scope="module")
def run(request):
    from buffett00929.config import Config

    return demo.build_demo_run(Config.load(), today=date(2026, 8, 17))


class TestDemoRun:
    def test_every_constituent_is_analysed(self, run):
        assert len(run.results) == len(run.constituents)

    def test_flagged_as_demo(self, run):
        assert run.is_demo

    def test_company_without_financials_is_excluded_from_ranking(self, run):
        empty = next(r for r in run.results if r.company.stock_id == "8808")
        assert not empty.score.is_rankable
        assert empty in run.unrankable

    def test_weights_come_from_the_constituent_list(self, run):
        weights = {c.stock_id: c.weight.value for c in run.constituents.constituents}
        for result in run.results:
            assert result.company.etf_weight.value == pytest.approx(
                weights[result.company.stock_id]
            )

    def test_etf_averages_report_their_sample_size(self, run):
        _, count = run.average_roe
        assert 0 < count <= len(run.results)


class TestLeaderboards:
    def test_six_boards_produced(self, run):
        assert len(run.leaderboards()) == 6

    def test_boards_never_include_unrankable_companies(self, run):
        unrankable = {r.company.stock_id for r in run.unrankable}
        for board in run.leaderboards():
            assert not {r.company.stock_id for r in board.entries} & unrankable

    def test_buffett_top_is_sorted_by_total_score(self, run):
        entries = run.leaderboards()[0].entries
        scores = [r.score.total_score for r in entries]
        assert scores == sorted(scores, reverse=True)

    def test_cheapest_board_sorted_by_margin_of_safety(self, run):
        board = next(b for b in run.leaderboards() if b.key == "cheapest")
        values = [r.score.valuation.margin_of_safety.value for r in board.entries]
        assert values == sorted(values, reverse=True)

    def test_yield_trap_list_only_contains_traps(self, run):
        for result in run.yield_traps:
            assert result.metrics.yield_trap.is_trap
            assert result.metrics.yield_trap.reasons


class TestMarkdownReport:
    def test_summary_marks_synthetic_data(self, run):
        assert "合成範例資料" in markdown.render_summary(run)

    def test_summary_contains_all_required_sections(self, run):
        text = markdown.render_summary(run)
        for heading in ["ETF 資訊", "評分變化", "紅旗警報", "Buffett Top 10",
                        "最便宜 Top 10", "最強護城河 Top 10", "ROE Top 10",
                        "最穩定獲利 Top 10", "高殖利率陷阱", "全部成分股", "資料來源"]:
            assert heading in text, heading

    def test_company_report_has_conclusion_and_scorecard(self, run):
        result = run.rankable[0]
        text = markdown.render_company(result, run)
        assert "這是一家" in text
        assert "評分明細" in text
        assert "估值與安全邊際" in text
        assert "7 年長期持有測試" in text

    def test_missing_data_renders_as_text_not_zero(self, run):
        empty = next(r for r in run.results if r.company.stock_id == "8808")
        text = markdown.render_company(empty, run)
        assert "資料不足" in text

    def test_write_reports_creates_one_file_per_company(self, run, tmp_path):
        paths = markdown.write_reports(run, tmp_path)
        assert len(paths) == len(run.results) + 1
        assert (tmp_path / "reports" / "README.md").exists()
        for result in run.results:
            assert (tmp_path / "reports" / "companies" / f"{result.company.stock_id}.md").exists()


class TestHtmlDashboard:
    def test_is_self_contained(self, run):
        """CSP 與離線可用性：不得有任何外部資源請求。"""
        html = dashboard.render_dashboard(run)
        assert 'src="http' not in html
        assert 'href="http' not in html
        assert "<style>" in html

    def test_declares_both_themes(self, run):
        html = dashboard.render_dashboard(run)
        assert "prefers-color-scheme: dark" in html
        assert '[data-theme="dark"]' in html

    def test_marks_synthetic_data(self, run):
        assert "合成範例資料" in dashboard.render_dashboard(run)

    def test_escapes_company_names(self, run):
        import copy

        hostile = copy.deepcopy(run)
        hostile.results[0].score.company.name = '<script>alert("x")</script>'
        html = dashboard.render_dashboard(hostile)
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_write_dashboard_creates_file(self, run, tmp_path):
        path = dashboard.write_dashboard(run, tmp_path)
        assert path.exists()
        assert path.read_text(encoding="utf-8").startswith("<!doctype html>")


class TestCli:
    def test_check_config_succeeds(self, capsys):
        assert main(["check-config"]) == 0
        assert "設定檔驗證通過" in capsys.readouterr().out

    def test_demo_writes_outputs(self, tmp_path, capsys):
        assert main(["--repo-root", str(tmp_path), "demo"]) == 0
        output = capsys.readouterr().out
        assert "合成範例資料" in output
        assert (tmp_path / "docs" / "index.html").exists()
        assert (tmp_path / "reports" / "README.md").exists()

    def test_demo_does_not_write_snapshots(self, tmp_path):
        """合成分數不得混入真實的評分變化歷史。"""
        main(["--repo-root", str(tmp_path), "demo"])
        assert not (tmp_path / "data" / "snapshots").exists()

    def test_update_aborts_when_no_constituent_source_is_available(self, tmp_path, capsys):
        """規格第二節：拿不到最新名單就中止，絕不產生空報表或沿用舊名單。

        用「全部來源停用」的設定來製造這個情境，而不是依賴測試機器沒有網路——
        後者在 CI 上會反過來成立，讓這個測試變成偵測網路的工具而不是偵測行為。
        """
        import yaml

        from buffett00929.config import Config

        base = Config.load()
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        sources = yaml.safe_load(yaml.safe_dump(base.sources))
        for provider in sources["constituents"]["providers"]:
            provider["enabled"] = False

        (config_dir / "scoring.yaml").write_text(
            yaml.safe_dump(base.scoring, allow_unicode=True), encoding="utf-8"
        )
        (config_dir / "sources.yaml").write_text(
            yaml.safe_dump(sources, allow_unicode=True), encoding="utf-8"
        )
        (config_dir / "overrides.yaml").write_text(
            yaml.safe_dump(base.overrides, allow_unicode=True), encoding="utf-8"
        )

        exit_code = main(
            ["--config-dir", str(config_dir), "--repo-root", str(tmp_path), "update"]
        )
        assert exit_code == 3
        assert "無法取得成分股名單" in capsys.readouterr().err
        assert not (tmp_path / "docs").exists()
        assert not (tmp_path / "data" / "snapshots").exists()


class TestStaleReportCleanup:
    """成分股被剔除後，它的舊報告必須消失。

    留著會很危險：那份報告看起來仍是最新分析的一部分，
    但那家公司其實已經不在 ETF 裡了。
    """

    def test_removes_reports_for_dropped_constituents(self, run, tmp_path):
        companies_dir = tmp_path / "reports" / "companies"
        companies_dir.mkdir(parents=True)
        stale = companies_dir / "1111.md"
        stale.write_text("# 已被剔除的成分股\n", encoding="utf-8")

        markdown.write_reports(run, tmp_path)

        assert not stale.exists()
        for result in run.results:
            assert (companies_dir / f"{result.company.stock_id}.md").exists()

    def test_keeps_the_summary_readme(self, run, tmp_path):
        markdown.write_reports(run, tmp_path)
        assert (tmp_path / "reports" / "README.md").exists()


class TestSourceAttributionTracksReality:
    """資料來源段落必須反映**實際命中**的來源。

    先前這段是寫死的字串，宣稱多年度歷史來自 FinMind。改接 MOPS 之後
    它沒跟著改，於是總表與自己的明細互相矛盾——明細標 MOPS、總表寫 FinMind；
    連合成資料的 demo 也照樣宣稱資料來自 FinMind。
    """

    def test_demo_reports_synthetic_not_a_real_provider(self, run):
        text = markdown.render_summary(run)
        section = text.split("## 資料來源", 1)[1]
        assert "fixture:synthetic" in section

    def test_no_provider_is_named_unless_it_was_actually_used(self, run):
        section = markdown.render_summary(run).split("## 資料來源", 1)[1]
        for provider in ("FinMind", "MOPS", "OpenAPI"):
            assert provider not in section, f"{provider} 沒被用到卻出現在資料來源"

    def test_counts_come_from_real_data_points(self, run):
        sources = run.data_sources
        assert sources, "應至少有一個來源"
        assert all(count > 0 for _source, count in sources)
        assert sources == sorted(sources, key=lambda item: (-item[1], item[0]))

    def test_dashboard_footer_agrees_with_the_markdown_summary(self, run):
        html = dashboard.render_dashboard(run)
        for source, _count in run.data_sources:
            assert source in html


class TestScanAndLookupDashboards:
    """掃描與個股也要有 dashboard——評比要看得出來，不是只有 Markdown 表格。

    既有的 dashboard 綁在 ``AnalysisRun`` 上（六張榜單、評分變化、ETF 權重），
    那些對「一檔股票」或「全市場掃描」都不適用：一檔沒有排行榜，
    掃描的公司沒有 ETF 權重。共用的是版面與配色，不是內容。
    """

    def _company_result(self, run):
        return run.results[0]

    def test_company_dashboard_is_self_contained(self, run):
        html = dashboard.render_company_dashboard(self._company_result(run))
        assert 'src="http' not in html and 'href="http' not in html
        assert "<style>" in html

    def test_company_dashboard_shows_the_component_breakdown(self, run):
        """使用者要的是「看得出評比」——各項得分必須逐項顯示，不能只有總分。"""
        html = dashboard.render_company_dashboard(self._company_result(run))
        for label in ("Management", "Moat", "ROE"):
            assert label in html

    def test_company_dashboard_names_the_scorable_denominator(self, run):
        """總分要配著分母看。85 分制與 100 分制的 70 分不是同一件事。"""
        result = self._company_result(run)
        html = dashboard.render_company_dashboard(result)
        assert f"{result.score.scorable_max:.0f}" in html

    def test_company_dashboard_escapes_names(self, run):
        import copy

        hostile = copy.deepcopy(self._company_result(run))
        hostile.score.company.name = '<script>alert("x")</script>'
        html = dashboard.render_company_dashboard(hostile)
        assert "<script>alert" not in html

    def test_company_dashboard_renders_triggered_red_flags(self, run):
        """紅旗那段分支原本沒有任何測試走過，於是它帶著錯的屬性名上線。

        demo 公司剛好不觸發紅旗，所以整份測試都繞過了 `if flags.triggered:`，
        而正式執行第一檔有紅旗的公司就炸掉：
        `AttributeError: 'RedFlag' object has no attribute 'severity_label'`。
        分析全部跑完（1,975 家、2 檔 BUY 候選），只有最後寫 HTML 那步失敗。
        """
        from buffett00929.redflags import RedFlag

        result = copy.deepcopy(run.results[0])
        result.score.red_flags.triggered = [
            RedFlag(code="fcf_negative", label="自由現金流轉負",
                    severity="critical", evidence="最新年度自由現金流為 -1.01 億元"),
            RedFlag(code="receivables", label="應收帳款成長遠快於營收",
                    severity="warning", evidence="營收 -7.5% 但應收帳款 +43.6%"),
        ]
        html = dashboard.render_company_dashboard(result)
        assert "自由現金流轉負" in html
        assert "-1.01 億元" in html
        assert "應收帳款成長遠快於營收" in html

    def test_screen_dashboard_renders_without_an_analysis_run(self, run):
        """掃描沒有 AnalysisRun，dashboard 不能依賴它。"""
        from buffett00929.screen import ScreenResult

        result = ScreenResult(
            quality_ranked=run.results, valued=run.results, universe_size=1975
        )
        html = dashboard.render_screen_dashboard(result)
        assert "1,975" in html or "1975" in html
        assert "<style>" in html

    def test_screen_dashboard_carries_the_degraded_banner(self, run):
        from buffett00929.models import DataPoint
        from buffett00929.screen import ScreenResult

        thin = copy_results = [run.results[0]]
        thin[0].score.valuation.margin_of_safety = DataPoint.missing("僅 0 種估值方法可用")
        html = dashboard.render_screen_dashboard(
            ScreenResult(quality_ranked=thin, valued=thin, universe_size=1975)
        )
        assert "資料不完整" in html

    def test_writers_create_the_files(self, run, tmp_path):
        from buffett00929.screen import ScreenResult

        result = ScreenResult(quality_ranked=run.results, valued=run.results[:2],
                              universe_size=1975)
        assert dashboard.write_screen_dashboard(result, tmp_path).exists()
        written = dashboard.write_company_dashboards(result.valued, tmp_path)
        assert len(written) == 2
        assert all(p.exists() for p in written)
