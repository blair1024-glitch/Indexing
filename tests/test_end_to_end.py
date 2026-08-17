"""端到端：demo 執行 → 排行榜 → Markdown 與 HTML 報表 → CLI。"""

from __future__ import annotations

from datetime import date

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
