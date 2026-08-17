"""分析流程編排。

一次完整執行的順序：

    成分股 → 逐檔載入財報 → 指標 → 估值 → 紅旗 → 評分 → 快照 → 報表

``AnalysisRun`` 是報表層唯一需要的輸入，內含所有評分結果、排行榜、
評分變化與資料缺口清單。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config
from .loader import DataLoader, LoadedCompany
from .metrics import CompanyMetrics
from .metrics import compute as compute_metrics
from .models import Company, DataPoint
from .redflags import detect as detect_red_flags
from .scoring.engine import (
    CompanyScore,
    investment_verdict,
    one_line_conclusion,
    score_company,
    seven_year_test,
)
from .scoring.valuation import estimate_valuation
from .snapshots import (
    MetricChange,
    ScoreChange,
    Snapshot,
    diff_snapshots,
    financial_changes,
    load_previous_snapshot,
    summarize_report,
)
from .sources.constituents import ConstituentChanges, ConstituentSet, diff_constituents


@dataclass
class CompanyResult:
    """單一公司的完整分析結果。"""

    score: CompanyScore
    verdict: str
    verdict_reason: str
    seven_year: str
    seven_year_reason: str
    conclusion: str
    changes: list[MetricChange] = field(default_factory=list)
    change_summary: str = ""

    @property
    def company(self) -> Company:
        return self.score.company

    @property
    def metrics(self) -> CompanyMetrics:
        return self.score.metrics

    def to_dict(self) -> dict:
        payload = self.score.to_dict()
        payload.update(
            {
                "verdict": self.verdict,
                "verdict_reason": self.verdict_reason,
                "seven_year": self.seven_year,
                "seven_year_reason": self.seven_year_reason,
                "conclusion": self.conclusion,
                "financial_changes": [c.to_dict() for c in self.changes],
                "change_summary": self.change_summary,
            }
        )
        return payload


@dataclass
class Leaderboard:
    key: str
    title: str
    description: str
    entries: list[CompanyResult] = field(default_factory=list)


@dataclass
class AnalysisRun:
    run_date: date
    constituents: ConstituentSet
    results: list[CompanyResult]
    score_changes: list[ScoreChange] = field(default_factory=list)
    constituent_changes: ConstituentChanges = field(default_factory=ConstituentChanges)
    warnings: list[str] = field(default_factory=list)
    is_demo: bool = False
    """True 表示使用合成資料產生，報表會顯著標示，避免被誤當真實分析。"""

    # -- 彙總 ---------------------------------------------------------------

    @property
    def rankable(self) -> list[CompanyResult]:
        return [r for r in self.results if r.score.is_rankable]

    @property
    def unrankable(self) -> list[CompanyResult]:
        return [r for r in self.results if not r.score.is_rankable]

    @property
    def average_roe(self) -> tuple[DataPoint, int]:
        """回傳 (平均值, 納入計算的檔數)。檔數必須揭露，否則平均值無法解讀。"""
        return _average(
            [r.metrics.value("roe", "roe_latest") for r in self.results],
            "成分股皆無可用 ROE",
        )

    @property
    def average_pe(self) -> tuple[DataPoint, int]:
        return _average(
            [r.company.market_data.pe_ratio for r in self.results],
            "成分股皆無可用本益比",
        )

    @property
    def average_dividend_yield(self) -> tuple[DataPoint, int]:
        return _average(
            [r.company.market_data.dividend_yield for r in self.results],
            "成分股皆無可用殖利率",
        )

    @property
    def red_flag_results(self) -> list[CompanyResult]:
        return sorted(
            [r for r in self.results if r.score.red_flags.triggered],
            key=lambda r: (r.score.red_flags.critical_count, r.score.red_flags.warning_count),
            reverse=True,
        )

    @property
    def significant_changes(self) -> list[ScoreChange]:
        return [c for c in self.score_changes if c.is_significant]

    @property
    def data_gap_results(self) -> list[CompanyResult]:
        return [r for r in self.results if r.company.data_gaps or not r.score.is_rankable]

    # -- 排行榜（規格第十二節）-------------------------------------------------

    def leaderboards(self, limit: int = 10) -> list[Leaderboard]:
        """六張榜單。全部只從 ``rankable`` 挑選——資料不足的公司不參與排名。"""
        pool = self.rankable

        def top(key, reverse=True):
            scored = [(r, key(r)) for r in pool]
            scored = [(r, v) for r, v in scored if v is not None]
            scored.sort(key=lambda item: item[1], reverse=reverse)
            return [r for r, _ in scored[:limit]]

        return [
            Leaderboard(
                "buffett_top",
                "🏆 Buffett Top 10",
                "綜合 Management、Moat、ROE、穩定性、財務、FCF 與安全邊際的 100 分總分",
                top(lambda r: r.score.total_score),
            ),
            Leaderboard(
                "research",
                "🔍 最值得研究 Top 10",
                "企業品質高但目前估值尚未到位——體質好、等價格的觀察名單",
                top(
                    lambda r: r.score.business_quality_score
                    if r.score.business_quality_score is not None
                    and (r.score.valuation_score or 0) < 60
                    else None
                ),
            ),
            Leaderboard(
                "cheapest",
                "💰 最便宜 Top 10",
                "依安全邊際排序（內在價值由至少 2 種估值法交叉驗證）",
                top(
                    lambda r: r.score.valuation.margin_of_safety.value
                    if r.score.valuation.margin_of_safety.is_available
                    else None
                ),
            ),
            Leaderboard(
                "moat",
                "🏰 最強護城河 Top 10",
                "毛利率水準與穩定度、營益率韌性、研發投入與規模的綜合得分率",
                top(lambda r: r.score.moat_ratio),
            ),
            Leaderboard(
                "roe",
                "📊 ROE Top 10",
                "最新年度 ROE。⚠ 標記者為高 ROE 但依賴高財務槓桿",
                top(
                    lambda r: r.metrics.value("roe", "roe_latest").value
                    if r.metrics.value("roe", "roe_latest").is_available
                    else None
                ),
            ),
            Leaderboard(
                "stability",
                "🛡 最穩定獲利 Top 10",
                "獲利穩定性得分——EPS/營收 CAGR、虧損年數與營益率趨勢",
                top(
                    lambda r: r.score.components["stability"].ratio
                    if "stability" in r.score.components
                    else None
                ),
            ),
        ]

    @property
    def yield_traps(self) -> list[CompanyResult]:
        """高殖利率陷阱清單（規格第十二節第六張榜單）。"""
        return sorted(
            [r for r in self.results if r.metrics.yield_trap and r.metrics.yield_trap.is_trap],
            key=lambda r: r.metrics.yield_trap.yield_value.value or 0,
            reverse=True,
        )

    def to_snapshot(self) -> Snapshot:
        return Snapshot(
            run_date=self.run_date,
            constituents=self.constituents.to_dict(),
            companies=[r.to_dict() for r in self.results],
            warnings=self.warnings,
        )


def _average(points: list[DataPoint], empty_reason: str) -> tuple[DataPoint, int]:
    """平均值與納入檔數。與 ``safe_mean`` 不同，這裡刻意「有幾筆算幾筆」。

    ETF 層級的平均 ROE 本來就是對「有資料的成分股」取平均，
    因為少數成分股缺料不該讓整個 ETF 的統計變成無法計算。
    但分母一定要一起回傳並顯示——「8 檔中有 2 檔算出來的平均」和
    「8 檔全部算出來的平均」意義完全不同。
    """
    usable = [p for p in points if p.is_available and p.value is not None]
    if not usable:
        return DataPoint.missing(empty_reason), 0
    return (
        DataPoint.derived(sum(p.value for p in usable) / len(usable), inputs=usable),
        len(usable),
    )


# --------------------------------------------------------------------------
# 執行
# --------------------------------------------------------------------------


def analyse_company(
    loaded: LoadedCompany,
    config: Config,
    *,
    today: date,
    repo_root: Path | None = None,
) -> CompanyResult:
    """單一公司：指標 → 估值 → 紅旗 → 評分 → 判斷。"""
    company = loaded.company
    metrics = compute_metrics(company, config.scoring)
    valuation = estimate_valuation(company, config.scoring)
    red_flags = detect_red_flags(company, metrics, config, repo_root=repo_root)
    score = score_company(company, metrics, valuation, red_flags, config, today=today)

    verdict, verdict_reason = investment_verdict(score, config)
    seven_year, seven_year_reason = seven_year_test(score, config)
    changes = financial_changes(company, loaded.quarterly_incomes)

    return CompanyResult(
        score=score,
        verdict=verdict,
        verdict_reason=verdict_reason,
        seven_year=seven_year,
        seven_year_reason=seven_year_reason,
        conclusion=one_line_conclusion(score, config),
        changes=changes,
        change_summary=summarize_report(changes),
    )


def run_analysis(
    config: Config,
    repo_root: Path,
    *,
    today: date | None = None,
) -> AnalysisRun:
    """完整執行一次分析（需要網路可連線至資料來源）。"""
    today = today or date.today()
    loader = DataLoader(config=config, repo_root=repo_root)

    # 成分股拿不到就直接中止——不用過期名單產生看起來很新的報表。
    constituents = loader.load_constituents()
    loaded = loader.load_all(constituents)

    results = [
        analyse_company(item, config, today=today, repo_root=repo_root) for item in loaded
    ]

    previous = load_previous_snapshot(repo_root, before=today)
    run = AnalysisRun(
        run_date=today,
        constituents=constituents,
        results=results,
        warnings=list(loader.warnings),
    )
    run.score_changes = diff_snapshots(run.to_snapshot(), previous)
    run.constituent_changes = diff_constituents(
        constituents, (previous.constituents if previous else None)
    )
    return run


__all__ = [
    "AnalysisRun",
    "CompanyResult",
    "Leaderboard",
    "analyse_company",
    "run_analysis",
]
