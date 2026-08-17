"""設定驗證、來源解析與數值解析。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import yaml

from buffett00929.config import Config, ConfigError, score_criterion
from buffett00929.metrics import verify_criteria_coverage
from buffett00929.models import DataPoint
from buffett00929.sources.base import parse_number, roc_to_ad
from buffett00929.sources.constituents import (
    ConstituentResolver,
    ConstituentsUnavailable,
    diff_constituents,
)


class TestShippedConfig:
    def test_weights_sum_to_100(self, config):
        assert sum(config.weights.values()) == pytest.approx(100)

    def test_every_criterion_has_an_implementation(self, config):
        assert verify_criteria_coverage(config.scoring) == []

    def test_every_criterion_has_a_catch_all_band(self, config):
        for component in config.weights:
            for key, criterion in config.criteria(component).items():
                assert criterion["bands"][-1]["threshold"] is None, f"{component}.{key}"


class TestBandScoring:
    CRITERION = {
        "label": "測試",
        "max_points": 10,
        "direction": "higher_better",
        "bands": [
            {"threshold": 0.5, "points": 10, "label": "高"},
            {"threshold": 0.2, "points": 5, "label": "中"},
            {"threshold": None, "points": 0, "label": "低"},
        ],
    }

    @pytest.mark.parametrize("value,points", [(0.9, 10), (0.5, 10), (0.3, 5), (0.05, 0)])
    def test_higher_better_bands(self, value, points):
        result = score_criterion("t", DataPoint.of(value, "s"), self.CRITERION)
        assert result.points == points

    def test_lower_better_bands(self):
        criterion = {**self.CRITERION, "direction": "lower_better"}
        assert score_criterion("t", DataPoint.of(0.1, "s"), criterion).points == 10
        assert score_criterion("t", DataPoint.of(0.9, "s"), criterion).points == 0

    def test_missing_value_is_unscored_not_zero(self):
        result = score_criterion("t", DataPoint.missing("沒有資料"), self.CRITERION)
        assert result.points is None
        assert not result.is_scorable
        assert result.display_value == "資料不足"


class TestConfigValidation:
    def _write(self, tmp_path, scoring=None, overrides=None):
        base = Config.load()
        (tmp_path / "scoring.yaml").write_text(
            yaml.safe_dump(scoring or base.scoring, allow_unicode=True), encoding="utf-8"
        )
        (tmp_path / "sources.yaml").write_text(
            yaml.safe_dump(base.sources, allow_unicode=True), encoding="utf-8"
        )
        (tmp_path / "overrides.yaml").write_text(
            yaml.safe_dump(overrides if overrides is not None else base.overrides,
                           allow_unicode=True),
            encoding="utf-8",
        )
        return tmp_path

    def test_override_without_rationale_is_rejected(self, tmp_path):
        path = self._write(
            tmp_path,
            overrides={"overrides": {"2330": {"moat": {"scale": {"points": 3}}}}},
        )
        with pytest.raises(ConfigError, match="rationale"):
            Config.load(path)

    def test_override_without_date_is_rejected(self, tmp_path):
        path = self._write(
            tmp_path,
            overrides={
                "overrides": {"2330": {"moat": {"scale": {"points": 3, "rationale": "x"}}}}
            },
        )
        with pytest.raises(ConfigError, match="reviewed_on"):
            Config.load(path)

    def test_override_above_max_points_is_rejected(self, tmp_path):
        path = self._write(
            tmp_path,
            overrides={
                "overrides": {
                    "2330": {
                        "moat": {
                            "scale": {
                                "points": 99,
                                "rationale": "x",
                                "reviewed_on": date(2026, 1, 1),
                            }
                        }
                    }
                }
            },
        )
        with pytest.raises(ConfigError, match="超過"):
            Config.load(path)

    def test_only_qualitative_components_are_overridable(self, tmp_path):
        path = self._write(
            tmp_path,
            overrides={
                "overrides": {
                    "2330": {
                        "roe": {
                            "roe_latest": {
                                "points": 4,
                                "rationale": "x",
                                "reviewed_on": date(2026, 1, 1),
                            }
                        }
                    }
                }
            },
        )
        with pytest.raises(ConfigError, match="不可覆寫"):
            Config.load(path)

    def test_valid_override_is_accepted(self, tmp_path):
        path = self._write(
            tmp_path,
            overrides={
                "review_max_age_days": 365,
                "overrides": {
                    "2330": {
                        "moat": {
                            "scale": {
                                "points": 3,
                                "rationale": "先進製程市佔超過九成",
                                "reviewed_on": date(2026, 8, 1),
                            }
                        }
                    }
                },
            },
        )
        loaded = Config.load(path)
        assert loaded.overrides_for("2330", "moat")["scale"]["points"] == 3


class TestOverrideApplication:
    def test_override_replaces_points_and_keeps_original(self, config):
        from buffett00929.config import apply_override

        result = score_criterion(
            "scale", DataPoint.of(50, "s"), config.criteria("moat")["scale"]
        )
        original = result.points
        apply_override(
            result,
            {"points": 3, "rationale": "市佔領先", "reviewed_on": date(2026, 8, 1)},
            today=date(2026, 8, 17),
            max_age_days=365,
        )
        assert result.points == 3
        assert result.computed_points == original
        assert result.overridden

    def test_stale_override_is_marked(self, config):
        from buffett00929.config import apply_override

        result = score_criterion(
            "scale", DataPoint.of(50, "s"), config.criteria("moat")["scale"]
        )
        apply_override(
            result,
            {"points": 3, "rationale": "x", "reviewed_on": date(2024, 1, 1)},
            today=date(2026, 8, 17),
            max_age_days=365,
        )
        assert result.override_stale


class TestNumberParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1,234,567", 1234567.0),
            ("(1,234)", -1234.0),
            ("12.5%", 12.5),
            (42, 42.0),
            ("-", None),
            ("", None),
            (None, None),
            ("N/A", None),
            ("不適用", None),
            ("abc", None),
        ],
    )
    def test_parse_number(self, raw, expected):
        assert parse_number(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [("1150817", date(2026, 8, 17)), ("115/08/17", date(2026, 8, 17)), ("bad", None)],
    )
    def test_roc_dates(self, raw, expected):
        assert roc_to_ad(raw) == expected


class TestConstituentResolution:
    def test_all_sources_failing_aborts(self, config, tmp_path):
        """規格第二節：拿不到最新名單就中止，不使用過期資料。"""
        settings = dict(config.sources["constituents"])
        settings["providers"] = [
            {"name": "manual", "enabled": True, "path": "data/manual/constituents.yaml"}
        ]
        resolver = ConstituentResolver(http=None, config=settings, repo_root=tmp_path)
        with pytest.raises(ConstituentsUnavailable, match="中止"):
            resolver.resolve()

    def test_stale_manual_list_is_rejected(self, config, tmp_path):
        manual = tmp_path / "data" / "manual"
        manual.mkdir(parents=True)
        (manual / "constituents.yaml").write_text(
            yaml.safe_dump(
                {
                    "as_of": date.today() - timedelta(days=120),
                    "source_note": "測試",
                    "constituents": [{"stock_id": "2330", "name": "台積電", "weight": 0.05}],
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        settings = dict(config.sources["constituents"])
        settings["providers"] = [
            {"name": "manual", "enabled": True, "path": "data/manual/constituents.yaml"}
        ]
        settings["manual_max_age_days"] = 45
        resolver = ConstituentResolver(http=None, config=settings, repo_root=tmp_path)
        with pytest.raises(ConstituentsUnavailable) as excinfo:
            resolver.resolve()
        assert "過期" in str(excinfo.value)

    def test_fresh_manual_list_is_accepted(self, config, tmp_path):
        manual = tmp_path / "data" / "manual"
        manual.mkdir(parents=True)
        (manual / "constituents.yaml").write_text(
            yaml.safe_dump(
                {
                    "as_of": date.today(),
                    "source_note": "官網 2026-08-17",
                    "constituents": [
                        {"stock_id": "2330", "name": "台積電", "weight": 0.05},
                        {"stock_id": "5347", "name": "世界先進", "market": "TPEx",
                         "weight": 0.03},
                    ],
                },
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        settings = dict(config.sources["constituents"])
        settings["providers"] = [
            {"name": "manual", "enabled": True, "path": "data/manual/constituents.yaml"}
        ]
        resolver = ConstituentResolver(http=None, config=settings, repo_root=tmp_path)
        result = resolver.resolve()
        assert len(result) == 2
        assert result.total_weight == pytest.approx(0.08)
        assert result.by_id()["5347"].market == "TPEx"


class TestConstituentDiff:
    def test_detects_add_remove_and_weight_change(self):
        from buffett00929.sources.constituents import Constituent, ConstituentSet

        current = ConstituentSet(
            constituents=[
                Constituent("2330", "台積電", DataPoint.of(0.09, "s")),
                Constituent("2454", "聯發科", DataPoint.of(0.05, "s")),
            ],
            source="test",
            as_of=date(2026, 8, 17),
        )
        previous = {
            "constituents": [
                {"stock_id": "2330", "name": "台積電", "weight": {"value": 0.05}},
                {"stock_id": "2303", "name": "聯電", "weight": {"value": 0.04}},
            ]
        }
        changes = diff_constituents(current, previous)
        assert [c.stock_id for c in changes.added] == ["2454"]
        assert [c["stock_id"] for c in changes.removed] == ["2303"]
        assert changes.weight_changes[0]["stock_id"] == "2330"
        assert changes.has_changes

    def test_no_previous_snapshot_reports_no_changes(self):
        from buffett00929.sources.constituents import ConstituentSet

        empty = ConstituentSet(constituents=[], source="t", as_of=date(2026, 8, 17))
        assert not diff_constituents(empty, None).has_changes
