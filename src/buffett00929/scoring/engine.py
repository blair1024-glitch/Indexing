"""100 分聚合、分級與投資判斷（規格第九、十、十一、十五、十七、十八節）。

七個評分項目共用同一個通用評分函式——它們的差異全在
``config/scoring.yaml`` 的門檻帶，不在程式邏輯，所以不需要為每項各寫一個模組。

**規格第十一節「好公司 ≠ 好價格」** 由三個分離的分數落實：
* Business Quality Score — 企業品質（Management + Moat + ROE + 穩定性 + FCF）
* Financial Safety Score — 財務安全
* Valuation Score — 目前價格合理程度

Buffett Score 是加權後的 100 分總分，作為主排名；三個子分數則讓
「好公司買太貴」與「便宜但體質差」兩種情況都能被一眼看出來。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..config import (
    ComponentScore,
    Config,
    CriterionResult,
    QUALITATIVE_BONUS_KEY,
    apply_override,
    score_criterion,
)
from ..metrics import CompanyMetrics
from ..models import Company, DataPoint
from ..redflags import RedFlagReport
from .valuation import Valuation

COMPONENT_LABELS = {
    "management": "Management 管理能力",
    "moat": "Moat 經濟護城河",
    "roe": "ROE 與資本效率",
    "stability": "獲利穩定性",
    "financial_safety": "財務安全",
    "free_cash_flow": "自由現金流",
    "margin_of_safety": "Margin of Safety 安全邊際",
}

BUSINESS_QUALITY_COMPONENTS = ("management", "moat", "roe", "stability", "free_cash_flow")

VERDICT_BUY = "🟢 BUY / 加碼研究"
VERDICT_HOLD = "🟡 HOLD / 持續觀察"
VERDICT_REDUCE = "🟠 REDUCE / 降低曝險"
VERDICT_AVOID = "🔴 AVOID / 避免"
VERDICT_INSUFFICIENT = "⚪ 資料不足，不做判斷"

HOLD_YES = "🟢 YES"
HOLD_MAYBE = "🟡 MAYBE"
HOLD_NO = "🔴 NO"
HOLD_UNKNOWN = "⚪ 資料不足"

OVERRIDABLE_COMPONENTS = ("management", "moat")


@dataclass
class CompanyScore:
    """一家公司的完整評分結果。"""

    company: Company
    metrics: CompanyMetrics
    valuation: Valuation
    red_flags: RedFlagReport
    components: dict[str, ComponentScore] = field(default_factory=dict)
    weight_scale: float = 1.0
    """權重合計不等於 100 時的換算係數（讓使用者可自行改權重仍保持 100 分制）。"""

    # -- 分數 ---------------------------------------------------------------

    @property
    def raw_score(self) -> float:
        """扣分前的加權總分。"""
        return sum(
            (c.normalized or 0.0) * self.weight_scale
            for c in self.components.values()
            if c.has_any_data
        )

    @property
    def scorable_max(self) -> float:
        """可評分的滿分——只計入有資料的評分項目。"""
        return sum(
            c.max_points * self.weight_scale
            for c in self.components.values()
            if c.has_any_data
        )

    @property
    def penalty(self) -> float:
        return self._penalty

    @property
    def total_score(self) -> float:
        """Buffett Score：扣除紅旗懲罰後的總分，下限 0。"""
        return max(0.0, self.raw_score - self._penalty)

    _penalty: float = 0.0

    @property
    def coverage(self) -> float:
        """資料覆蓋率。100 分制下，可評分滿分佔 100 的比例。"""
        total = sum(c.max_points * self.weight_scale for c in self.components.values())
        return self.scorable_max / total if total else 0.0

    @property
    def is_rankable(self) -> bool:
        """資料覆蓋率過低的公司不進入排行榜。

        用殘缺資料算出的分數去和資料完整的公司比名次，比不排名更誤導。
        """
        return self.coverage >= self._min_coverage

    _min_coverage: float = 0.6

    @property
    def grade(self) -> tuple[str, str]:
        return grade_for(self.total_score, self._grades)

    _grades: list = field(default_factory=list)

    def component(self, key: str) -> ComponentScore | None:
        return self.components.get(key)

    def _subscore(self, keys: tuple[str, ...]) -> float | None:
        """把指定評分項目換算成 0~100 的子分數。"""
        earned = 0.0
        maximum = 0.0
        for key in keys:
            comp = self.components.get(key)
            if comp is None or not comp.has_any_data:
                continue
            earned += comp.normalized or 0.0
            maximum += comp.max_points
        if maximum <= 0:
            return None
        return earned / maximum * 100

    @property
    def business_quality_score(self) -> float | None:
        return self._subscore(BUSINESS_QUALITY_COMPONENTS)

    @property
    def financial_safety_score(self) -> float | None:
        return self._subscore(("financial_safety",))

    @property
    def valuation_score(self) -> float | None:
        return self._subscore(("margin_of_safety",))

    @property
    def moat_ratio(self) -> float | None:
        comp = self.components.get("moat")
        return comp.ratio if comp else None

    @property
    def moat_grade(self) -> str:
        ratio = self.moat_ratio
        if ratio is None:
            return "資料不足"
        for entry in self._moat_grades:
            if ratio >= float(entry["min_ratio"]):
                return str(entry["label"])
        return "無"

    _moat_grades: list = field(default_factory=list)

    def to_dict(self) -> dict:
        grade, grade_label = self.grade
        return {
            "stock_id": self.company.stock_id,
            "name": self.company.name,
            "industry": self.company.industry,
            "market": self.company.market,
            "etf_weight": self.company.etf_weight.to_dict(),
            "total_score": round(self.total_score, 2),
            "raw_score": round(self.raw_score, 2),
            "penalty": round(self._penalty, 2),
            "scorable_max": round(self.scorable_max, 2),
            "coverage": round(self.coverage, 4),
            "is_rankable": self.is_rankable,
            "grade": grade,
            "grade_label": grade_label,
            "business_quality_score": _round_or_none(self.business_quality_score),
            "financial_safety_score": _round_or_none(self.financial_safety_score),
            "valuation_score": _round_or_none(self.valuation_score),
            "moat_grade": self.moat_grade,
            "moat_ratio": _round_or_none(self.moat_ratio, 4),
            "components": {k: v.to_dict() for k, v in self.components.items()},
            "valuation": self.valuation.to_dict(),
            "red_flags": self.red_flags.to_dict(),
            "earnings_profile": self.metrics.earnings_profile,
            "capital_efficiency": self.metrics.capital_efficiency,
            "high_roe_via_leverage": self.metrics.high_roe_via_leverage,
            "high_roe_note": self.metrics.high_roe_note,
            "yield_trap": self.metrics.yield_trap.to_dict() if self.metrics.yield_trap else None,
            "data_gaps": self.company.data_gaps,
        }


def _round_or_none(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


# --------------------------------------------------------------------------
# 評分
# --------------------------------------------------------------------------


def score_component(
    component: str,
    metrics: CompanyMetrics,
    config: Config,
    *,
    stock_id: str,
    today: date,
    allow_override: bool = False,
) -> ComponentScore:
    """為單一評分項目計分。"""
    criteria = config.criteria(component)
    result = ComponentScore(
        key=component,
        label=COMPONENT_LABELS.get(component, component),
        max_points=config.weight(component),
    )

    overrides = config.overrides_for(stock_id, component) if allow_override else {}

    for key, criterion in criteria.items():
        scored = score_criterion(key, metrics.value(component, key), criterion)
        if allow_override:
            scored = apply_override(
                scored,
                overrides.get(key),
                today=today,
                max_age_days=config.override_max_age_days,
            )
        result.criteria.append(scored)

    # 質化加減分：讓財報數字看不見的面向（品牌、客戶黏著度、網路效應）
    # 有一個正式的落點，而不是被迫塞進某個量化指標裡。
    bonus = overrides.get(QUALITATIVE_BONUS_KEY) if allow_override else None
    if bonus:
        max_points = config.criterion_max_points(component, QUALITATIVE_BONUS_KEY) or 0.0
        entry = CriterionResult(
            key=QUALITATIVE_BONUS_KEY,
            label="質化加減分（人工）",
            value=DataPoint.missing("質化判斷，無對應量化指標"),
            max_points=max_points,
            points=0.0,
        )
        result.criteria.append(
            apply_override(
                entry, bonus, today=today, max_age_days=config.override_max_age_days
            )
        )

    return result


def score_company(
    company: Company,
    metrics: CompanyMetrics,
    valuation: Valuation,
    red_flags: RedFlagReport,
    config: Config,
    *,
    today: date | None = None,
) -> CompanyScore:
    """彙總為 100 分 Buffett Score。"""
    today = today or date.today()

    # 安全邊際由估值模組算出後回填給指標層，再照一般流程評分。
    metrics.set_value("margin_of_safety", "margin_of_safety", valuation.margin_of_safety)

    weights = config.weights
    weight_total = sum(weights.values())
    # 使用者若把 MoS 改成 25 分，權重合計會變成 115；換算回 100 分制以維持可比性。
    weight_scale = 100 / weight_total if weight_total else 1.0

    score = CompanyScore(
        company=company,
        metrics=metrics,
        valuation=valuation,
        red_flags=red_flags,
        weight_scale=weight_scale,
    )
    score._grades = config.scoring.get("grades") or []
    score._moat_grades = (config.scoring.get("moat") or {}).get("grades") or []
    score._min_coverage = float(
        (config.scoring.get("ranking") or {}).get("min_coverage", 0.6)
    )

    for component in weights:
        score.components[component] = score_component(
            component,
            metrics,
            config,
            stock_id=company.stock_id,
            today=today,
            allow_override=component in OVERRIDABLE_COMPONENTS,
        )

    score._penalty = red_flags.penalty(config)
    return score


def grade_for(total: float, grades: list) -> tuple[str, str]:
    """依規格第十節把總分轉為 A+/A/B+/B/C/D。"""
    for entry in grades:
        if total >= float(entry["min_score"]):
            return str(entry["grade"]), str(entry["label"])
    return "D", "不符合長期價值投資條件"


# --------------------------------------------------------------------------
# 投資判斷（規格第十五節）
# --------------------------------------------------------------------------


def investment_verdict(score: CompanyScore, config: Config) -> tuple[str, str]:
    """產生 BUY / HOLD / REDUCE / AVOID 與理由。

    規格明訂「不得只根據單季 EPS 判斷」。這裡的輸入全部是多年期指標
    （總分由 5 年 ROE、CAGR、毛利率趨勢等構成）、估值與紅旗，
    **沒有任何一條規則讀取單季 EPS**——單季數字只出現在財報變化表，
    用來提示重新檢查，不會單獨改變判斷。
    """
    rules = config.scoring.get("verdict") or {}

    if not score.is_rankable:
        return (
            VERDICT_INSUFFICIENT,
            f"資料覆蓋率僅 {score.coverage:.0%}，不足以做出綜合判斷",
        )

    mos = score.valuation.margin_of_safety
    mos_value = mos.value if mos.is_available else None
    critical = score.red_flags.critical_count
    total = score.total_score

    buy = rules.get("buy") or {}
    if (
        total >= float(buy.get("min_total_score", 75))
        and mos_value is not None
        and mos_value >= float(buy.get("min_margin_of_safety", 0.20))
        and critical <= int(buy.get("max_critical_flags", 0))
    ):
        return (
            VERDICT_BUY,
            f"總分 {total:.0f}、安全邊際 {mos_value:.0%}、無重大紅旗，"
            "企業品質與價格同時到位",
        )

    hold = rules.get("hold") or {}
    if (
        total >= float(hold.get("min_total_score", 60))
        and (mos_value is None or mos_value >= float(hold.get("min_margin_of_safety", -0.10)))
        and critical <= int(hold.get("max_critical_flags", 1))
    ):
        reason = f"總分 {total:.0f}"
        if mos_value is not None:
            reason += f"、安全邊際 {mos_value:.0%}"
        else:
            reason += "、安全邊際資料不足"
        if critical:
            reason += f"、{critical} 項重大紅旗"
        return VERDICT_HOLD, reason + "，體質可接受但未達加碼條件"

    reduce = rules.get("reduce") or {}
    if total >= float(reduce.get("min_total_score", 50)) and critical <= 2:
        return (
            VERDICT_REDUCE,
            f"總分 {total:.0f}、{critical} 項重大紅旗，體質或估值已轉弱",
        )

    return (
        VERDICT_AVOID,
        f"總分 {total:.0f}、{critical} 項重大紅旗，不符合長期持有條件",
    )


# --------------------------------------------------------------------------
# 7 年長期持有測試（規格第十七節）
# --------------------------------------------------------------------------


def seven_year_test(score: CompanyScore, config: Config) -> tuple[str, str]:
    """「如果我今天買進，持有 7 年，願不願意完全不看股價？」

    刻意**不看估值**——這個問題問的是企業本身能不能放心持有 7 年，
    價格便宜與否是另一個問題（由安全邊際回答）。
    """
    rules = config.scoring.get("seven_year_test") or {}
    quality = score.business_quality_score
    moat_ratio = score.moat_ratio
    critical = score.red_flags.critical_count

    if quality is None or moat_ratio is None:
        return HOLD_UNKNOWN, "企業品質或護城河資料不足，無法回答 7 年持有問題"

    yes = rules.get("yes") or {}
    if (
        quality >= float(yes.get("min_business_quality", 70))
        and moat_ratio >= float(yes.get("min_moat_ratio", 0.60))
        and critical <= int(yes.get("max_critical_flags", 0))
    ):
        return (
            HOLD_YES,
            f"企業品質 {quality:.0f} 分、護城河得分率 {moat_ratio:.0%}、無重大紅旗，"
            "獲利模式穩定到可以不看股價",
        )

    maybe = rules.get("maybe") or {}
    if (
        quality >= float(maybe.get("min_business_quality", 55))
        and moat_ratio >= float(maybe.get("min_moat_ratio", 0.40))
        and critical <= int(maybe.get("max_critical_flags", 1))
    ):
        return (
            HOLD_MAYBE,
            f"企業品質 {quality:.0f} 分、護城河得分率 {moat_ratio:.0%}"
            + (f"、{critical} 項重大紅旗" if critical else "")
            + "，可持有但需每季追蹤護城河是否鬆動",
        )

    return (
        HOLD_NO,
        f"企業品質 {quality:.0f} 分、護城河得分率 {moat_ratio:.0%}、{critical} 項重大紅旗，"
        "不具備不看股價持有 7 年的條件",
    )


# --------------------------------------------------------------------------
# Buffett 式一句話結論（規格第十八節）
# --------------------------------------------------------------------------


def one_line_conclusion(score: CompanyScore, config: Config) -> str:
    """依規格第十八節的固定句型產生結論。

    每一個空格都由已計算的數據填入，不是自由發揮的評論。
    """
    verdict, _ = investment_verdict(score, config)

    character = score.metrics.earnings_profile
    if score.metrics.capital_efficiency and "資料不足" not in score.metrics.capital_efficiency:
        character += f"、{score.metrics.capital_efficiency.split(' ')[-1]}"

    moat_component = score.components.get("moat")
    strongest = "護城河資料不足"
    if moat_component:
        scored = [c for c in moat_component.criteria if c.is_scorable and c.max_points > 0]
        if scored:
            best = max(scored, key=lambda c: (c.points or 0) / c.max_points)
            strongest = f"{best.label}（{best.band_label or ''}）"

    risk = "尚未發現重大風險"
    if score.red_flags.triggered:
        worst = max(
            score.red_flags.triggered,
            key=lambda f: {"critical": 2, "warning": 1}.get(f.severity, 0),
        )
        risk = f"{worst.label}——{worst.evidence}"
    elif score.metrics.high_roe_via_leverage:
        risk = f"高 ROE 依賴財務槓桿（{score.metrics.high_roe_note}）"

    price_view = score.valuation.classification
    mos = score.valuation.margin_of_safety
    if mos.is_available and mos.value is not None:
        price_view += f"（安全邊際 {mos.value:+.0%}）"

    return (
        f"這是一家「{character}」的公司，"
        f"最大的護城河是{strongest}，"
        f"最大的風險是{risk}，"
        f"目前股價相對內在價值為{price_view}，"
        f"因此我會 {verdict}。"
    )


__all__ = [
    "BUSINESS_QUALITY_COMPONENTS",
    "COMPONENT_LABELS",
    "CompanyScore",
    "grade_for",
    "investment_verdict",
    "one_line_conclusion",
    "score_company",
    "score_component",
    "seven_year_test",
]
