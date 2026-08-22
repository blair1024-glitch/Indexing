"""全市場掃描：把同一套巴菲特邏輯的母體從 50 檔換成全市場。

分析邏輯從來不綁 00929，綁的只是「選誰」。MOPS 彙總報表以**期別**為單位抓取，
一次請求涵蓋全市場（實測 1,975 家上市櫃公司），所以換母體不需要換資料層。

## 為什麼分兩階段

FinMind 是**逐檔查詢**且有速率上限（冷啟動約 300 次請求）。1,975 家全部補齊
需要近萬次請求，不可能。但彙總報表本身就足以評估企業品質：

    Management 20 分、ROE 15 分、獲利穩定性 15 分   ← 全部可算
    Moat 25 分中的 22 分                          ← 缺研發費用率
    財務安全 10 分中的 5 分                        ← 缺有息負債
    自由現金流 5 分、安全邊際 10 分                 ← 缺資本支出、股利、股價

第一階段因此有約 77 分可評分，足以排序品質，而且**不花任何 FinMind 額度**。
這剛好就是巴菲特的順序：先問是不是好公司，再問價格。

第二階段只對品質最高的前 N 名補估值，N 預設保守取 50（每檔約 5 次請求）。

## 兩個分母不可直接比較

第一階段是 77 分制、第二階段是 100 分制。混在同一張表裡排序會讓讀者
以為前者比後者差，實際上只是量得比較少——與 ``snapshots.py`` 處理
分母位移是同一條原則，所以報表分開呈現。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config
from .loader import DataLoader, build_lookup_constituent
from .pipeline import CompanyResult, analyse_company


@dataclass
class ScreenResult:
    """一次全市場掃描的結果。"""

    quality_ranked: list[CompanyResult] = field(default_factory=list)
    """第一階段：全市場依企業品質排序（約 77 分制）。"""
    valued: list[CompanyResult] = field(default_factory=list)
    """第二階段：前 N 名補齊估值後的完整結果（100 分制）。"""
    universe_size: int = 0
    """彙總報表裡有財報可用的公司數。"""
    run_date: date = field(default_factory=date.today)
    warnings: list[str] = field(default_factory=list)

    @property
    def buy_candidates(self) -> list[CompanyResult]:
        from .scoring.engine import VERDICT_BUY

        return [r for r in self.valued if r.verdict == VERDICT_BUY]

    @property
    def with_margin_of_safety(self) -> list[CompanyResult]:
        return sorted(
            [r for r in self.valued if r.score.valuation.margin_of_safety.is_available],
            key=lambda r: r.score.valuation.margin_of_safety.value or 0,
            reverse=True,
        )


def rank_by_quality(
    loader: DataLoader,
    config: Config,
    *,
    today: date | None = None,
    repo_root: Path | None = None,
) -> list[CompanyResult]:
    """第一階段：對彙總報表裡的每一家公司評分並依企業品質排序。

    **不發出任何逐檔請求**——歷史已由 ``prefetch_history`` 整批回補。
    """
    today = today or date.today()
    history = loader.history
    if history is None:
        return []

    stock_ids = sorted(set(history.incomes) | set(history.balances) | set(history.cash_flows))

    results: list[CompanyResult] = []
    for stock_id in stock_ids:
        constituent = build_lookup_constituent(stock_id, history.names.get(stock_id))
        loaded = loader.load_company(constituent, history_only=True)
        # 查無財報與「體質差」是兩件事。沒有任何可評分項目的公司要被排除，
        # 而不是以 0 分排在最後——後者會讓讀者以為我們評估過它。
        result = analyse_company(loaded, config, today=today, repo_root=repo_root)
        if result.score.scorable_max <= 0 or result.score.business_quality_score is None:
            continue
        results.append(result)

    results.sort(key=lambda r: r.score.business_quality_score or 0, reverse=True)
    return results


def screen_market(
    config: Config,
    repo_root: Path,
    *,
    today: date | None = None,
    top_n: int | None = None,
) -> ScreenResult:
    """兩段式全市場掃描（需要網路）。"""
    today = today or date.today()
    settings = config.sources.get("screen") or {}
    top_n = top_n or int(settings.get("top_n", 50))

    loader = DataLoader(config=config, repo_root=repo_root)
    loader.prefetch_history(today)

    ranked = rank_by_quality(loader, config, today=today, repo_root=repo_root)

    # --- 第二階段：只對前 N 名補估值 -----------------------------------------
    valued: list[CompanyResult] = []
    for result in ranked[:top_n]:
        company = result.company
        constituent = build_lookup_constituent(company.stock_id, company.name)
        try:
            loaded = loader.load_company(constituent)
        except Exception as exc:  # noqa: BLE001 - 單檔失敗不該中斷整份掃描
            loader.warnings.append(f"{company.stock_id} 補齊估值失敗：{exc}")
            continue
        valued.append(analyse_company(loaded, config, today=today, repo_root=repo_root))

    valued.sort(key=lambda r: r.score.total_score, reverse=True)

    return ScreenResult(
        quality_ranked=ranked,
        valued=valued,
        universe_size=len(ranked),
        run_date=today,
        warnings=list(loader.warnings),
    )


__all__ = ["ScreenResult", "rank_by_quality", "screen_market"]
