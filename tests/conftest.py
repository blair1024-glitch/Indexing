"""共用測試設定。"""

from __future__ import annotations

from datetime import date

import pytest

from buffett00929 import metrics as metrics_module
from buffett00929 import redflags
from buffett00929.config import Config
from buffett00929.scoring import engine, valuation as valuation_module

TODAY = date(2026, 8, 17)


@pytest.fixture(scope="session")
def config() -> Config:
    return Config.load()


@pytest.fixture
def score_of(config):
    """把一家公司跑完整條評分流程，回傳 CompanyScore。"""

    def _score(company, *, repo_root=None):
        metrics = metrics_module.compute(company, config.scoring)
        valuation = valuation_module.estimate_valuation(company, config.scoring)
        flags = redflags.detect(company, metrics, config, repo_root=repo_root)
        return engine.score_company(
            company, metrics, valuation, flags, config, today=TODAY
        )

    return _score
