"""快照儲存與變化追蹤（規格第十三、十四、二十節）。

兩種「變化」，用途不同：

1. **評分變化**（``diff_snapshots``）——本次執行 vs 上次執行，
   產生「台積電：88 → 92 ↑4」並**歸因到實際變動的評分項目**。
   只說分數變了不夠，要說清楚是護城河改善還是紅旗解除。

2. **財報變化**（``financial_changes``）——本期 vs 上期／去年同期，
   產生規格第十四節要求的 YoY／QoQ 趨勢表，並回答
   「這次財報到底變好了還是變差了」。

快照以每日一檔 JSON 存於 ``data/snapshots/``，並 commit 進 repo，
所以 ``git log`` 本身就是完整的評分歷史。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import Company, DataPoint, FiscalPeriod, IncomeStatement, safe_div

SNAPSHOT_VERSION = 1


# --------------------------------------------------------------------------
# 快照
# --------------------------------------------------------------------------


@dataclass
class Snapshot:
    run_date: date
    constituents: dict
    companies: list[dict]
    warnings: list[str] = field(default_factory=list)
    version: int = SNAPSHOT_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "run_date": self.run_date.isoformat(),
            "constituents": self.constituents,
            "companies": self.companies,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Snapshot:
        return cls(
            run_date=date.fromisoformat(raw["run_date"]),
            constituents=raw.get("constituents") or {},
            companies=raw.get("companies") or [],
            warnings=raw.get("warnings") or [],
            version=int(raw.get("version", 1)),
        )

    def by_stock_id(self) -> dict[str, dict]:
        return {c["stock_id"]: c for c in self.companies if c.get("stock_id")}


def snapshot_dir(repo_root: Path) -> Path:
    return repo_root / "data" / "snapshots"


def save_snapshot(repo_root: Path, snapshot: Snapshot) -> Path:
    directory = snapshot_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot.run_date.isoformat()}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(snapshot.to_dict(), fh, ensure_ascii=False, indent=2, sort_keys=True)
    return path


def load_previous_snapshot(repo_root: Path, before: date) -> Snapshot | None:
    """載入 ``before`` 之前最近的一份快照，作為比較基準。"""
    directory = snapshot_dir(repo_root)
    if not directory.exists():
        return None

    candidates: list[tuple[date, Path]] = []
    for path in directory.glob("*.json"):
        try:
            stamp = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if stamp < before:
            candidates.append((stamp, path))

    if not candidates:
        return None

    _, latest = max(candidates, key=lambda item: item[0])
    try:
        with latest.open("r", encoding="utf-8") as fh:
            return Snapshot.from_dict(json.load(fh))
    except (json.JSONDecodeError, OSError, KeyError):
        return None


# --------------------------------------------------------------------------
# 評分變化
# --------------------------------------------------------------------------


@dataclass
class ComponentDelta:
    key: str
    label: str
    previous: float | None
    current: float | None

    @property
    def delta(self) -> float | None:
        if self.previous is None or self.current is None:
            return None
        return self.current - self.previous

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "previous": self.previous,
            "current": self.current,
            "delta": self.delta,
        }


@dataclass
class ScoreChange:
    stock_id: str
    name: str
    previous_score: float | None
    current_score: float | None
    component_deltas: list[ComponentDelta] = field(default_factory=list)
    new_flags: list[str] = field(default_factory=list)
    resolved_flags: list[str] = field(default_factory=list)
    previous_grade: str | None = None
    current_grade: str | None = None

    @property
    def delta(self) -> float | None:
        if self.previous_score is None or self.current_score is None:
            return None
        return self.current_score - self.previous_score

    @property
    def arrow(self) -> str:
        delta = self.delta
        if delta is None:
            return ""
        if delta > 0.05:
            return "↑"
        if delta < -0.05:
            return "↓"
        return "→"

    @property
    def headline(self) -> str:
        """規格第二十節的格式，例如「台積電：88 → 92 ↑4」。"""
        if self.previous_score is None:
            return f"{self.name}：新增追蹤，{self.current_score:.0f} 分"
        if self.current_score is None:
            return f"{self.name}：本次無法評分"
        return (
            f"{self.name}：{self.previous_score:.0f} → {self.current_score:.0f} "
            f"{self.arrow}{abs(self.delta or 0):.0f}"
        )

    @property
    def explanation(self) -> str:
        """說明分數為什麼改變——歸因到實際變動的項目。"""
        reasons: list[str] = []

        moved = [d for d in self.component_deltas if d.delta and abs(d.delta) >= 0.5]
        moved.sort(key=lambda d: abs(d.delta or 0), reverse=True)
        for item in moved[:3]:
            reasons.append(f"{item.label} {item.delta:+.1f} 分")

        if self.new_flags:
            reasons.append(f"新增紅旗：{'、'.join(self.new_flags)}")
        if self.resolved_flags:
            reasons.append(f"紅旗解除：{'、'.join(self.resolved_flags)}")
        if self.previous_grade and self.current_grade and self.previous_grade != self.current_grade:
            reasons.append(f"等級 {self.previous_grade} → {self.current_grade}")

        return "；".join(reasons) if reasons else "各評分項目與紅旗狀態均無顯著變動"

    @property
    def is_significant(self) -> bool:
        delta = self.delta
        return bool(
            (delta is not None and abs(delta) >= 1.0) or self.new_flags or self.resolved_flags
        )

    def to_dict(self) -> dict:
        return {
            "stock_id": self.stock_id,
            "name": self.name,
            "previous_score": self.previous_score,
            "current_score": self.current_score,
            "delta": self.delta,
            "headline": self.headline,
            "explanation": self.explanation,
            "component_deltas": [d.to_dict() for d in self.component_deltas],
            "new_flags": self.new_flags,
            "resolved_flags": self.resolved_flags,
            "previous_grade": self.previous_grade,
            "current_grade": self.current_grade,
        }


def diff_snapshots(current: Snapshot, previous: Snapshot | None) -> list[ScoreChange]:
    """比較兩次執行，產生帶歸因的評分變化清單。"""
    previous_map = previous.by_stock_id() if previous else {}
    changes: list[ScoreChange] = []

    for entry in current.companies:
        stock_id = entry.get("stock_id")
        if not stock_id:
            continue
        before = previous_map.get(stock_id)

        change = ScoreChange(
            stock_id=stock_id,
            name=entry.get("name", stock_id),
            previous_score=(before or {}).get("total_score"),
            current_score=entry.get("total_score"),
            previous_grade=(before or {}).get("grade"),
            current_grade=entry.get("grade"),
        )

        if before:
            change.component_deltas = _component_deltas(entry, before)
            change.new_flags, change.resolved_flags = _flag_changes(entry, before)

        changes.append(change)

    changes.sort(key=lambda c: abs(c.delta or 0), reverse=True)
    return changes


def _component_deltas(current: dict, previous: dict) -> list[ComponentDelta]:
    deltas: list[ComponentDelta] = []
    current_components = current.get("components") or {}
    previous_components = previous.get("components") or {}

    for key, comp in current_components.items():
        before = previous_components.get(key)
        if not before:
            continue
        deltas.append(
            ComponentDelta(
                key=key,
                label=comp.get("label", key),
                previous=before.get("normalized"),
                current=comp.get("normalized"),
            )
        )
    return deltas


def _flag_changes(current: dict, previous: dict) -> tuple[list[str], list[str]]:
    def labels(entry: dict) -> dict[str, str]:
        flags = (entry.get("red_flags") or {}).get("triggered") or []
        return {f["code"]: f.get("label", f["code"]) for f in flags}

    now, before = labels(current), labels(previous)
    new = [label for code, label in now.items() if code not in before]
    resolved = [label for code, label in before.items() if code not in now]
    return new, resolved


# --------------------------------------------------------------------------
# 財報變化表（規格第十四節）
# --------------------------------------------------------------------------


@dataclass
class MetricChange:
    label: str
    previous: DataPoint
    current: DataPoint
    yoy: DataPoint
    qoq: DataPoint
    is_percentage: bool = False
    higher_is_better: bool = True

    @property
    def trend(self) -> str:
        if not self.previous.is_available or not self.current.is_available:
            return "—"
        if self.current.value is None or self.previous.value is None:
            return "—"
        if self.current.value > self.previous.value:
            return "↑"
        if self.current.value < self.previous.value:
            return "↓"
        return "→"

    @property
    def improved(self) -> bool | None:
        """這一項變好還是變壞（已考慮「越低越好」的指標）。"""
        if not self.previous.is_available or not self.current.is_available:
            return None
        if self.current.value is None or self.previous.value is None:
            return None
        if self.current.value == self.previous.value:
            return None
        rising = self.current.value > self.previous.value
        return rising if self.higher_is_better else not rising

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "previous": self.previous.to_dict(),
            "current": self.current.to_dict(),
            "yoy": self.yoy.to_dict(),
            "qoq": self.qoq.to_dict(),
            "trend": self.trend,
            "improved": self.improved,
            "is_percentage": self.is_percentage,
        }


def _growth(current: DataPoint, base: DataPoint) -> DataPoint:
    if not current.is_available or not base.is_available:
        return DataPoint.missing("缺比較基期資料")
    if base.value in (None, 0):
        return DataPoint.missing("比較基期為 0，成長率無定義")
    if base.value < 0:
        # 基期為負時成長率的正負號會反轉，容易誤讀，直接標示不適用。
        return DataPoint.missing("比較基期為負，成長率不具意義")
    return DataPoint.derived(current.value / base.value - 1, inputs=[current, base])  # type: ignore[operator]


def financial_changes(
    company: Company,
    quarterly_incomes: list[IncomeStatement],
) -> list[MetricChange]:
    """產生規格第十四節的本期 vs 上期／去年同期比較表。

    使用**單季**損益表——台灣財報揭露為累計數，直接拿累計數比 QoQ
    會讓每一季都看起來在成長。
    """
    if not quarterly_incomes:
        return []

    ordered = sorted(quarterly_incomes, key=lambda s: s.period)
    current = ordered[-1]
    by_period: dict[FiscalPeriod, IncomeStatement] = {s.period: s for s in ordered}
    prev_q = by_period.get(current.period.prev_quarter())
    prev_y = by_period.get(current.period.prev_year())

    def build(
        label: str,
        extract,
        *,
        is_percentage: bool = False,
        higher_is_better: bool = True,
    ) -> MetricChange:
        curr_value = extract(current)
        prev_value = extract(prev_q) if prev_q else DataPoint.missing("無上一季資料")
        yoy_base = extract(prev_y) if prev_y else DataPoint.missing("無去年同期資料")
        return MetricChange(
            label=label,
            previous=prev_value,
            current=curr_value,
            # 比率型指標比較的是「變動幾個百分點」，不是「成長幾 %」。
            yoy=_delta(curr_value, yoy_base) if is_percentage else _growth(curr_value, yoy_base),
            qoq=_delta(curr_value, prev_value) if is_percentage else _growth(curr_value, prev_value),
            is_percentage=is_percentage,
            higher_is_better=higher_is_better,
        )

    changes = [
        build("營收", lambda s: s.revenue),
        build("毛利率", lambda s: s.gross_margin, is_percentage=True),
        build("營益率", lambda s: s.operating_margin, is_percentage=True),
        build("EPS", lambda s: s.eps),
    ]

    # ROE / FCF / 負債來自年度資料，季度序列不一定完整，缺就明白標示。
    changes.extend(_annual_changes(company))
    return changes


def _delta(current: DataPoint, base: DataPoint) -> DataPoint:
    """比率型指標的變動（百分點差），例如毛利率 45% → 47% 為 +2pp。"""
    if not current.is_available or not base.is_available:
        return DataPoint.missing("缺比較基期資料")
    return DataPoint.derived(current.value - base.value, inputs=[current, base])  # type: ignore[operator]


def _annual_changes(company: Company) -> list[MetricChange]:
    from .metrics import roe as roe_metrics

    result: list[MetricChange] = []

    roe_series = roe_metrics.annual_roe_series(company)
    if len(roe_series) >= 2:
        prev, curr = roe_series[-2][1], roe_series[-1][1]
        result.append(
            MetricChange(
                label="ROE（年度）",
                previous=prev,
                current=curr,
                yoy=_delta(curr, prev),
                qoq=DataPoint.missing("年度指標無季度比較"),
                is_percentage=True,
            )
        )

    flows = company.annual_cash_flows()
    if len(flows) >= 2:
        prev_fcf, curr_fcf = flows[-2].free_cash_flow, flows[-1].free_cash_flow
        result.append(
            MetricChange(
                label="自由現金流（年度）",
                previous=prev_fcf,
                current=curr_fcf,
                yoy=_growth(curr_fcf, prev_fcf),
                qoq=DataPoint.missing("年度指標無季度比較"),
            )
        )

    balances = company.annual_balances()
    if len(balances) >= 2:
        prev_debt, curr_debt = balances[-2].debt_ratio, balances[-1].debt_ratio
        result.append(
            MetricChange(
                label="負債比（年度）",
                previous=prev_debt,
                current=curr_debt,
                yoy=_delta(curr_debt, prev_debt),
                qoq=DataPoint.missing("年度指標無季度比較"),
                is_percentage=True,
                higher_is_better=False,
            )
        )

    return result


def summarize_report(changes: list[MetricChange]) -> str:
    """回答規格第十四節的提問：「這次財報到底變好了還是變差了？」"""
    assessed = [c for c in changes if c.improved is not None]
    if not assessed:
        return "本期與上期可比較的指標不足，無法判斷財報變好或變差"

    better = sum(1 for c in assessed if c.improved)
    worse = len(assessed) - better
    improved_labels = [c.label for c in assessed if c.improved]
    worse_labels = [c.label for c in assessed if not c.improved]

    if better and not worse:
        return f"**全面轉好**：{len(assessed)} 項可比較指標全數改善（{'、'.join(improved_labels)}）"
    if worse and not better:
        return f"**全面轉弱**：{len(assessed)} 項可比較指標全數惡化（{'、'.join(worse_labels)}）"

    verdict = "偏正面" if better > worse else ("偏負面" if worse > better else "好壞參半")
    return (
        f"**{verdict}**：{better} 項改善（{'、'.join(improved_labels)}）、"
        f"{worse} 項惡化（{'、'.join(worse_labels)}）"
    )


__all__ = [
    "ComponentDelta",
    "MetricChange",
    "SNAPSHOT_VERSION",
    "ScoreChange",
    "Snapshot",
    "diff_snapshots",
    "financial_changes",
    "load_previous_snapshot",
    "save_snapshot",
    "snapshot_dir",
    "summarize_report",
]
