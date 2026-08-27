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
        """CSP 與離線可用性：不得有任何外部**資源請求**。

        擋的是會發出請求的東西（``src=``、``<link href=``），不是 ``<a href>``。
        個股清單頁底下連到 GitHub Actions 當退路——那是**導覽**，
        不載入任何資源、不影響離線開啟、不違反 CSP。
        原本寫成 `'href="http' not in html`，會連導覽連結一起擋掉，
        而且只擋雙引號（本檔案的 HTML 屬性用單引號），等於兩頭都不準。
        """
        html = dashboard.render_dashboard(run)
        assert "src='http" not in html and 'src="http' not in html
        assert "<link" not in html
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
        """同 ``test_is_self_contained``：擋資源請求，不擋導覽連結。"""
        html = dashboard.render_company_dashboard(self._company_result(run))
        assert "src='http" not in html and 'src="http' not in html
        assert "<link" not in html
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


class TestNavigation:
    """三種頁面要能互相到達。

    這是使用者實際踩到的問題：Pages 站台打開之後，沒有任何界面可以進到
    全市場掃描或個股頁面——得自己手打 ``screen.html``、``lookup/6523.html``。
    """

    def _company_result(self, run):
        return run.results[0]

    def _screen_result(self, run):
        from buffett00929.screen import ScreenResult

        return ScreenResult(
            quality_ranked=run.results, valued=run.results[:2], universe_size=1975
        )

    def test_every_page_carries_the_nav(self, run):
        pages = (
            dashboard.render_dashboard(run),
            dashboard.render_screen_dashboard(self._screen_result(run)),
            dashboard.render_company_dashboard(self._company_result(run)),
        )
        for html in pages:
            assert "<nav class='nav'>" in html
            for label in ("00929 每日", "全市場掃描", "查詢個股"):
                assert label in html

    def test_company_page_nav_climbs_out_of_lookup(self, run):
        """個股頁在 ``docs/lookup/`` 底下，比首頁深一層。

        少了 ``../`` 就會連到 ``lookup/index.html``（自己）與
        ``lookup/screen.html``（不存在）。
        """
        html = dashboard.render_company_dashboard(self._company_result(run))
        assert "href='../index.html'" in html
        assert "href='../screen.html'" in html

    def test_company_page_links_back_to_the_lookup_list(self, run):
        """個股頁**在** lookup 區裡，但它不是那個列表頁。

        標成 current 會讓「個股頁面」變成不能點的字，從個股頁就回不去清單。
        """
        html = dashboard.render_company_dashboard(self._company_result(run))
        nav = html.split("</nav>")[0]
        assert "href='../lookup/index.html'" in nav
        assert "class='here'" not in nav

    def test_root_level_pages_do_not_use_a_prefix(self, run):
        html = dashboard.render_dashboard(run)
        assert "href='screen.html'" in html
        assert "../" not in html.split("</nav>")[0]

    def test_current_page_is_not_a_link(self, run):
        """連到自己是雜訊。"""
        html = dashboard.render_dashboard(run)
        nav = html.split("</nav>")[0]
        assert "<span class='here'>🏠 00929 每日</span>" in nav
        assert "href='index.html'" not in nav

    def test_the_nav_keeps_stock_lookup_on_site(self, run):
        """使用者明講過：把人丟去 Actions 按 Run workflow「不是我要的」。

        全市場的個股頁都預先產生好了，站內就查得到，導覽列不該再連出去。
        """
        html = dashboard.render_dashboard(run)
        nav = html.split("</nav>")[0]
        assert "href='lookup/index.html'" in nav
        assert dashboard.LOOKUP_WORKFLOW_URL not in nav

    def test_the_workflow_stays_available_as_a_fallback(self, run, tmp_path):
        """沒產生過的股票（新上市、興櫃）還是得有辦法查。"""
        dashboard.write_company_dashboards(run.results[:1], tmp_path)
        index = (tmp_path / "docs" / "lookup" / "index.html").read_text(encoding="utf-8")
        assert dashboard.LOOKUP_WORKFLOW_URL in index
        assert dashboard.LOOKUP_WORKFLOW_URL.endswith("/analyse-stock.yml")


class TestLookupIndex:
    def _screen_result(self, run):
        from buffett00929.screen import ScreenResult

        return ScreenResult(
            quality_ranked=run.results, valued=run.results[:3], universe_size=1975
        )

    def test_index_lists_the_companies_written(self, run, tmp_path):
        results = self._screen_result(run).valued
        dashboard.write_company_dashboards(results, tmp_path)

        index = (tmp_path / "docs" / "lookup" / "index.html").read_text(encoding="utf-8")
        for result in results:
            assert result.company.stock_id in index
            assert result.company.name in index
            assert f"href='{result.company.stock_id}.html'" in index

    def test_a_single_lookup_does_not_wipe_the_scan(self, run, tmp_path):
        """兩個呼叫點寫的份數不同，清單必須合併而不是整份重建。

        掃描一次寫 50 檔、個股查詢一次只寫 1 檔。若從 ``results`` 整份重建，
        查一檔就會把掃描寫的另外 49 筆洗掉。
        """
        import json

        scanned = run.results[:3]
        dashboard.write_company_dashboards(scanned, tmp_path)

        # 個股查詢：只寫一檔，而且是不在掃描名單裡的那一檔。
        single = [r for r in run.results if r not in scanned][:1]
        assert single, "測試資料需要至少 4 檔才問得出這件事"
        dashboard.write_company_dashboards(single, tmp_path)

        manifest = json.loads(
            (tmp_path / "docs" / "lookup" / "index.json").read_text(encoding="utf-8")
        )
        assert len(manifest) == len(scanned) + 1
        for result in scanned + single:
            assert manifest[result.company.stock_id] == result.company.name

    def test_pages_written_before_the_manifest_existed_are_picked_up(self, run, tmp_path):
        """清單檔是後來才加的，之前產生的 50 份頁面不在裡面。

        沒有這一層，導覽列上的「個股頁面」會連到一份只有新頁面的清單，
        舊的 50 份等於消失——而要補回來就得重跑一次掃描（花 FinMind 額度）。
        """
        lookup = tmp_path / "docs" / "lookup"
        lookup.mkdir(parents=True)
        # 模擬舊版產生的頁面：只有 HTML，沒有清單檔。
        legacy = run.results[0]
        (lookup / f"{legacy.company.stock_id}.html").write_text(
            dashboard.render_company_dashboard(legacy), encoding="utf-8"
        )

        other = [r for r in run.results if r is not legacy][:1]
        dashboard.write_company_dashboards(other, tmp_path)

        index = (lookup / "index.html").read_text(encoding="utf-8")
        assert legacy.company.stock_id in index
        assert legacy.company.name in index, "舊頁面的公司名要從標題救回來"
        assert other[0].company.stock_id in index

    def test_scan_falls_back_to_the_stock_id_when_the_title_is_unreadable(self, tmp_path):
        """救援層寧可少一個名字，也不要讓那一頁從清單上消失。"""
        lookup = tmp_path / "docs" / "lookup"
        lookup.mkdir(parents=True)
        (lookup / "9999.html").write_text("<html>沒有標題</html>", encoding="utf-8")

        dashboard.write_company_dashboards([], tmp_path)

        index = (lookup / "index.html").read_text(encoding="utf-8")
        assert "9999" in index

    def test_a_broken_manifest_does_not_fail_the_run(self, run, tmp_path):
        """清單是導覽用的便利品，不是資料——壞掉不該中斷整次執行。"""
        lookup = tmp_path / "docs" / "lookup"
        lookup.mkdir(parents=True)
        (lookup / "index.json").write_text("{ not json", encoding="utf-8")

        results = run.results[:2]
        dashboard.write_company_dashboards(results, tmp_path)

        index = (lookup / "index.html").read_text(encoding="utf-8")
        for result in results:
            assert result.company.stock_id in index

    def test_index_is_reachable_from_the_nav(self, run, tmp_path):
        dashboard.write_company_dashboards(run.results[:1], tmp_path)
        index = (tmp_path / "docs" / "lookup" / "index.html").read_text(encoding="utf-8")
        # 自己這一頁不給連結，其他三項要連得出去。
        assert "<span class='here'>🔎 查詢個股</span>" in index
        assert "href='../index.html'" in index
        assert "href='../screen.html'" in index


class TestStageOnePagesSayWhatIsMissing:
    """「還沒算」和「算不出來」長得一模一樣，結論卻相反。

    第一階段不補股利與資本支出，估值因此顯示「僅 0 種方法可用」——
    那句話讀起來像試過但失敗，實際上是根本沒去拿資料。讀者若照字面理解，
    會以為這家公司估不出價，而不是我們還沒估。
    """

    def _stage_one(self, run):
        from buffett00929.models import STAGE_ONE_GAP

        result = copy.deepcopy(run.results[0])
        result.company.data_gaps.append(f"{STAGE_ONE_GAP}：資本支出、股利與歷史本益比未載入")
        return result

    def test_stage_one_page_says_valuation_was_not_attempted(self, run):
        html = dashboard.render_company_dashboard(self._stage_one(run))
        assert "只做了第一階段" in html
        assert "還沒算" in html

    def test_a_fully_valued_page_carries_no_such_banner(self, run):
        html = dashboard.render_company_dashboard(run.results[0])
        assert "只做了第一階段" not in html


class TestFundamentalsCard:
    def test_the_card_names_the_industry_and_market(self, run):
        result = copy.deepcopy(run.results[0])
        result.company.industry = "半導體業"
        result.company.market = "TWSE"
        html = dashboard.render_company_dashboard(result)
        assert "基本面" in html
        assert "半導體業" in html
        assert "上市" in html

    def test_over_the_counter_is_not_reported_as_listed(self, run):
        result = copy.deepcopy(run.results[0])
        result.company.market = "TPEx"
        html = dashboard.render_company_dashboard(result)
        assert "上櫃" in html

    def test_a_missing_field_reads_as_missing_not_as_zero(self, run):
        """規格核心：查不到不能長得像「表現很差」。"""
        from buffett00929.models import DataPoint, MarketData

        result = copy.deepcopy(run.results[0])
        result.company.market_data = MarketData(
            price=DataPoint.missing("缺股價")
        )
        html = dashboard.render_company_dashboard(result)
        assert "<span class='missing'>資料不足</span>" in html


class TestEveryCompanyGetsAPage:
    """第一階段本來就對全市場算完分數了，只寫前 50 名等於算完丟掉。"""

    def _screen(self, run):
        from buffett00929.screen import ScreenResult

        return ScreenResult(
            quality_ranked=run.results, valued=run.results[:2], universe_size=1975
        )

    def test_all_companies_covers_the_whole_ranking(self, run):
        result = self._screen(run)
        assert len(result.all_companies) == len(run.results)

    def test_the_valued_version_wins(self, run):
        """同一家會出現兩次：第一階段 77 分制、第二階段 100 分制。

        取到第一階段那份等於把已經算出來的安全邊際丟掉。
        """
        from buffett00929.models import STAGE_ONE_GAP
        from buffett00929.screen import ScreenResult

        stage_one = copy.deepcopy(run.results[0])
        stage_one.company.data_gaps.append(f"{STAGE_ONE_GAP}：未載入")
        valued = run.results[0]

        result = ScreenResult(
            quality_ranked=[stage_one], valued=[valued], universe_size=1975
        )
        picked = result.all_companies
        assert len(picked) == 1
        assert picked[0] is valued

    def test_the_writer_receives_every_company(self, run, tmp_path):
        written = dashboard.write_company_dashboards(
            self._screen(run).all_companies, tmp_path
        )
        assert len(written) == len(run.results)


class TestLookupSearch:
    def test_the_index_carries_a_search_box(self, run, tmp_path):
        dashboard.write_company_dashboards(run.results[:3], tmp_path)
        index = (tmp_path / "docs" / "lookup" / "index.html").read_text(encoding="utf-8")
        assert "id='q'" in index
        assert "<script>" in index

    def test_rows_carry_searchable_text_for_id_and_name(self, run, tmp_path):
        dashboard.write_company_dashboards(run.results[:3], tmp_path)
        index = (tmp_path / "docs" / "lookup" / "index.html").read_text(encoding="utf-8")
        for result in run.results[:3]:
            needle = f"data-q='{result.company.stock_id} {result.company.name}'"
            assert needle in index

    def test_the_table_still_lists_everyone_without_js(self, run, tmp_path):
        """搜尋是便利品，不是進入頁面的必要條件。

        用 JS 產生列表的話，關掉 JS 就整頁空白——所以是過濾既有的列。
        """
        dashboard.write_company_dashboards(run.results[:3], tmp_path)
        index = (tmp_path / "docs" / "lookup" / "index.html").read_text(encoding="utf-8")
        table = index.split("<script>")[0]
        for result in run.results[:3]:
            assert f"href='{result.company.stock_id}.html'" in table

    def test_the_search_stays_self_contained(self, run, tmp_path):
        dashboard.write_company_dashboards(run.results[:3], tmp_path)
        index = (tmp_path / "docs" / "lookup" / "index.html").read_text(encoding="utf-8")
        assert "src='http" not in index and 'src="http' not in index
        assert "<link" not in index
