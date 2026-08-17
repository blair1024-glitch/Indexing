"""紅旗警報：每一項都需要「會觸發」與「不會誤觸發」兩面驗證。"""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from buffett00929 import demo, redflags
from buffett00929.metrics import compute as compute_metrics
from buffett00929.models import Company


def detect(company, config, repo_root=None):
    metrics = compute_metrics(company, config.scoring)
    return redflags.detect(company, metrics, config, repo_root=repo_root)


def codes(report):
    return {flag.code for flag in report.triggered}


class TestHealthyCompany:
    def test_no_flags_triggered(self, config):
        assert codes(detect(demo.build_company(), config)) == set()

    def test_two_manual_flags_remain_unchecked(self, config):
        """公司治理與競爭市占無法由財報判定，必須標示未檢查而非安全。"""
        unchecked = {flag.code for flag in detect(demo.build_company(), config).unchecked}
        assert "governance" in unchecked
        assert "competitive_share_loss" in unchecked


class TestDeclineFlags:
    def test_eps_decline(self, config):
        assert "eps_decline" in codes(detect(demo.build_company(eps_growth=-0.12), config))

    def test_gross_margin_decline(self, config):
        report = detect(demo.build_company(gross_margin_drift=-0.03), config)
        assert "gross_margin_decline" in codes(report)

    def test_moat_erosion_from_margin_trend(self, config):
        report = detect(demo.build_company(gross_margin_drift=-0.03), config)
        assert "moat_erosion" in codes(report)

    def test_stable_margins_do_not_trigger_erosion(self, config):
        assert "moat_erosion" not in codes(detect(demo.build_company(), config))

    def test_roe_decline_flagged(self, config):
        company = demo.build_company(net_margin=0.25, years=6)
        # 逐年壓低淨利，製造 ROE 連續下降。
        for index, statement in enumerate(company.income_statements):
            if statement.net_income.is_available:
                from buffett00929.models import DataPoint

                statement.net_income = DataPoint.of(
                    statement.net_income.value * (0.75**index), "t"
                )
        assert "roe_decline" in codes(detect(company, config))


class TestBalanceSheetFlags:
    def test_fcf_negative(self, config):
        assert "fcf_negative" in codes(detect(demo.build_company(capex_ratio=1.8), config))

    def test_positive_fcf_not_flagged(self, config):
        assert "fcf_negative" not in codes(detect(demo.build_company(), config))

    def test_debt_surge(self, config):
        assert "debt_surge" in codes(detect(demo.build_company(debt_growth_final=0.9), config))

    def test_receivables_surge(self, config):
        company = demo.build_company(receivable_ratio=0.15, receivable_ratio_final=0.45)
        assert "receivables_surge" in codes(detect(company, config))

    def test_share_expansion_and_large_issuance(self, config):
        result = codes(detect(demo.build_company(share_growth=0.15), config))
        assert "share_expansion" in result
        assert "large_issuance" in result

    def test_modest_dilution_is_not_large_issuance(self, config):
        result = codes(detect(demo.build_company(share_growth=0.03), config))
        assert "share_expansion" not in result
        assert "large_issuance" not in result


class TestEarningsQualityFlags:
    def test_excessive_payout(self, config):
        assert "payout_spike" in codes(detect(demo.build_company(dividend_payout=1.4), config))

    def test_high_non_operating_income(self, config):
        company = demo.build_company(non_operating_ratio=0.60)
        assert "non_operating_high" in codes(detect(company, config))

    def test_normal_non_operating_not_flagged(self, config):
        assert "non_operating_high" not in codes(detect(demo.build_company(), config))


class TestUncheckedVersusSafe:
    def test_empty_company_triggers_nothing_but_checks_nothing(self, config):
        """沒有資料時不能報「無紅旗」，必須全部落在未檢查。"""
        report = detect(Company(stock_id="0", name="空"), config)
        assert report.triggered == []
        assert len(report.unchecked) >= 10

    def test_unchecked_flags_carry_a_reason(self, config):
        for flag in detect(Company(stock_id="0", name="空"), config).unchecked:
            assert flag.evidence
            assert flag.checked is False


class TestPenalty:
    def test_penalty_scales_with_severity(self, config):
        clean = detect(demo.build_company(), config)
        broken = detect(
            demo.build_company(eps_growth=-0.15, gross_margin_drift=-0.03, capex_ratio=1.8),
            config,
        )
        assert clean.penalty(config) == 0
        assert broken.penalty(config) > 0

    def test_penalty_is_capped(self, config):
        broken = detect(
            demo.build_company(
                eps_growth=-0.2, gross_margin_drift=-0.04, capex_ratio=2.0,
                share_growth=0.2, dividend_payout=1.6, debt_growth_final=1.0,
                non_operating_ratio=0.7,
            ),
            config,
        )
        assert broken.penalty(config) <= config.max_total_penalty


class TestManualFlags:
    def test_logged_governance_issue_triggers(self, config, tmp_path):
        manual = tmp_path / "data" / "manual"
        manual.mkdir(parents=True)
        (manual / "governance.yaml").write_text(
            yaml.safe_dump(
                {
                    "issues": {
                        "9999": {
                            "governance": {
                                "note": "獨立董事集體請辭",
                                "reported_on": date(2026, 7, 22),
                            }
                        }
                    }
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        report = detect(demo.build_company(stock_id="9999"), config, repo_root=tmp_path)
        assert "governance" in codes(report)
        triggered = next(f for f in report.triggered if f.code == "governance")
        assert "獨立董事" in triggered.evidence

    def test_other_company_unaffected(self, config, tmp_path):
        manual = tmp_path / "data" / "manual"
        manual.mkdir(parents=True)
        (manual / "governance.yaml").write_text(
            yaml.safe_dump({"issues": {"1111": {"governance": {"note": "x",
                                                               "reported_on": date(2026, 1, 1)}}}}),
            encoding="utf-8",
        )
        report = detect(demo.build_company(stock_id="9999"), config, repo_root=tmp_path)
        assert "governance" not in codes(report)
