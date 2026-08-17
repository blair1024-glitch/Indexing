"""設定載入與門檻帶評分機制。

評分全部由 ``config/scoring.yaml`` 驅動，程式碼不寫死任何門檻。
這讓每個分數都能在報表上回溯到「哪個指標、實際值多少、命中哪一帶、得幾分」。

核心規則（規格第二十一節）：**輸入資料不足的 criterion 不計分，也不計入分母。**
所以一家只拿得到部分資料的公司會顯示「52 / 68 可評分」，
而不是被當成「52 / 100」——後者會讓缺料看起來像體質差。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .models import DataPoint

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


class ConfigError(Exception):
    """設定檔格式錯誤。刻意讓它中止執行，而不是靜默用預設值帶過。"""


# --------------------------------------------------------------------------
# 載入
# --------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"找不到設定檔：{path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"設定檔格式錯誤（頂層必須是物件）：{path}")
    return data


@dataclass
class Config:
    scoring: dict
    sources: dict
    overrides: dict
    config_dir: Path

    @classmethod
    def load(cls, config_dir: Path | str | None = None) -> Config:
        directory = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        cfg = cls(
            scoring=_load_yaml(directory / "scoring.yaml"),
            sources=_load_yaml(directory / "sources.yaml"),
            overrides=_load_yaml(directory / "overrides.yaml"),
            config_dir=directory,
        )
        cfg.validate()
        return cfg

    # -- 驗證 ---------------------------------------------------------------

    def validate(self) -> None:
        weights = self.scoring.get("weights")
        if not isinstance(weights, dict) or not weights:
            raise ConfigError("scoring.yaml 缺少 weights 區塊")

        for component in weights:
            if component in COMPONENTS_WITHOUT_CRITERIA:
                continue
            block = self.scoring.get(component)
            if not isinstance(block, dict) or "criteria" not in block:
                raise ConfigError(f"scoring.yaml 缺少 {component}.criteria 區塊")
            for key, crit in block["criteria"].items():
                _validate_criterion(f"{component}.{key}", crit)

        self._validate_overrides()

    def _validate_overrides(self) -> None:
        """覆寫必須完整。缺理由或日期的覆寫會被拒絕——可追溯性不能妥協。"""
        entries = self.overrides.get("overrides") or {}
        if not isinstance(entries, dict):
            raise ConfigError("overrides.yaml 的 overrides 必須是物件")
        for stock_id, components in entries.items():
            if not isinstance(components, dict):
                raise ConfigError(f"overrides.yaml：{stock_id} 的內容必須是物件")
            for component, criteria in components.items():
                if component not in ("management", "moat"):
                    raise ConfigError(
                        f"overrides.yaml：{stock_id}.{component} 不可覆寫"
                        "（僅開放 management 與 moat 這兩個質化項目）"
                    )
                for crit_key, payload in criteria.items():
                    self._validate_override_entry(stock_id, component, crit_key, payload)

    def _validate_override_entry(
        self, stock_id: str, component: str, crit_key: str, payload: Any
    ) -> None:
        where = f"overrides.yaml：{stock_id}.{component}.{crit_key}"
        if not isinstance(payload, dict):
            raise ConfigError(f"{where} 必須是物件")
        for required in ("points", "rationale", "reviewed_on"):
            if payload.get(required) in (None, ""):
                raise ConfigError(f"{where} 缺少必填欄位 {required}（覆寫必須留下理由與日期）")
        if not isinstance(payload["points"], (int, float)):
            raise ConfigError(f"{where} 的 points 必須是數字")
        if not isinstance(payload["reviewed_on"], date):
            raise ConfigError(f"{where} 的 reviewed_on 必須是日期（YYYY-MM-DD）")

        max_points = self.criterion_max_points(component, crit_key)
        if max_points is not None and payload["points"] > max_points:
            raise ConfigError(
                f"{where} 的 points={payload['points']} 超過該項上限 {max_points}"
            )

    # -- 查詢 ---------------------------------------------------------------

    def weight(self, component: str) -> float:
        return float(self.scoring["weights"][component])

    @property
    def weights(self) -> dict[str, float]:
        return {k: float(v) for k, v in self.scoring["weights"].items()}

    def criteria(self, component: str) -> dict[str, dict]:
        block = self.scoring.get(component) or {}
        return block.get("criteria") or {}

    def criterion_max_points(self, component: str, key: str) -> float | None:
        crit = self.criteria(component).get(key)
        if crit is None:
            # qualitative_bonus 不對應任何 criterion，上限採該 component 權重的 10%。
            if key == QUALITATIVE_BONUS_KEY:
                return self.weight(component) * 0.1
            return None
        return float(crit["max_points"])

    def overrides_for(self, stock_id: str, component: str) -> dict[str, dict]:
        entries = self.overrides.get("overrides") or {}
        return (entries.get(stock_id) or {}).get(component) or {}

    @property
    def override_max_age_days(self) -> int:
        return int(self.overrides.get("review_max_age_days", 365))

    def red_flag_threshold(self, key: str, default: float) -> float:
        thresholds = (self.scoring.get("red_flags") or {}).get("thresholds") or {}
        return float(thresholds.get(key, default))

    def red_flag_penalty(self, severity: str) -> float:
        penalties = (self.scoring.get("red_flags") or {}).get("penalties") or {}
        return float(penalties.get(severity, 0))

    @property
    def max_total_penalty(self) -> float:
        return float((self.scoring.get("red_flags") or {}).get("max_total_penalty", 25))


COMPONENTS_WITHOUT_CRITERIA: set[str] = set()
"""目前所有評分項目都有 criteria；保留此集合以便未來加入純衍生項目。"""

QUALITATIVE_BONUS_KEY = "qualitative_bonus"
"""覆寫檔可用的特殊鍵，對整個 M1/M2 額外加減分。"""


def _validate_criterion(where: str, crit: Any) -> None:
    if not isinstance(crit, dict):
        raise ConfigError(f"{where} 必須是物件")
    for required in ("label", "max_points", "direction", "bands"):
        if required not in crit:
            raise ConfigError(f"{where} 缺少必填欄位 {required}")
    if crit["direction"] not in ("higher_better", "lower_better"):
        raise ConfigError(f"{where}.direction 必須是 higher_better 或 lower_better")
    bands = crit["bands"]
    if not isinstance(bands, list) or not bands:
        raise ConfigError(f"{where}.bands 必須是非空陣列")
    if bands[-1].get("threshold") is not None:
        raise ConfigError(f"{where}.bands 最後一帶的 threshold 必須是 null（保底帶）")
    for band in bands:
        if "points" not in band:
            raise ConfigError(f"{where}.bands 每一帶都必須有 points")
        if band["points"] > crit["max_points"]:
            raise ConfigError(
                f"{where}.bands 有一帶的 points={band['points']} 超過 max_points={crit['max_points']}"
            )


# --------------------------------------------------------------------------
# 評分結果
# --------------------------------------------------------------------------


@dataclass
class CriterionResult:
    """單一評分指標的結果，帶完整的可稽核軌跡。"""

    key: str
    label: str
    value: DataPoint
    max_points: float
    points: float | None = None
    """None 表示資料不足、未計分（也不計入分母）。"""
    band_label: str | None = None
    rationale: str | None = None
    overridden: bool = False
    override_rationale: str | None = None
    override_reviewed_on: date | None = None
    override_stale: bool = False
    computed_points: float | None = None
    """被覆寫時保留原始計算值，供報表對照。"""

    @property
    def is_scorable(self) -> bool:
        return self.points is not None

    @property
    def display_value(self) -> str:
        if not self.value.is_available:
            return "資料不足"
        return f"{self.value.value:,.4g}"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value.to_dict(),
            "points": self.points,
            "max_points": self.max_points,
            "band_label": self.band_label,
            "overridden": self.overridden,
            "override_rationale": self.override_rationale,
            "override_reviewed_on": (
                self.override_reviewed_on.isoformat() if self.override_reviewed_on else None
            ),
            "override_stale": self.override_stale,
            "computed_points": self.computed_points,
        }


@dataclass
class ScoreAdjustment:
    """對整個評分項目的加減分，附理由。

    用於門檻帶無法表達的跨指標規則，例如「高 ROE 但高負債時不得給高分」——
    這件事看的是 ROE 與槓桿的**組合**，單一 criterion 的門檻帶表達不了。
    每筆調整都會出現在報表上，不會是暗中改分數。
    """

    label: str
    delta: float
    reason: str

    def to_dict(self) -> dict:
        return {"label": self.label, "delta": self.delta, "reason": self.reason}


@dataclass
class ComponentScore:
    """一個評分項目（如 Management 20 分）的彙總結果。"""

    key: str
    label: str
    max_points: float
    criteria: list[CriterionResult] = field(default_factory=list)
    adjustments: list[ScoreAdjustment] = field(default_factory=list)

    @property
    def criteria_points(self) -> float:
        return sum(c.points for c in self.criteria if c.points is not None)

    @property
    def earned(self) -> float:
        # 下限為 0：調整可以扣掉得分，但不會讓某一項變成負分去拉低其他項。
        return max(0.0, self.criteria_points + sum(a.delta for a in self.adjustments))

    @property
    def scorable_max(self) -> float:
        """可評分的分母——只計入資料齊全的指標。"""
        return sum(c.max_points for c in self.criteria if c.is_scorable)

    @property
    def has_any_data(self) -> bool:
        return self.scorable_max > 0

    @property
    def coverage(self) -> float:
        """資料覆蓋率：可評分上限佔滿分的比例。"""
        total = sum(c.max_points for c in self.criteria)
        return self.scorable_max / total if total else 0.0

    @property
    def normalized(self) -> float | None:
        """換算回滿分制。資料完全缺失時回傳 None，不回傳 0。"""
        if not self.has_any_data:
            return None
        return self.earned / self.scorable_max * self.max_points

    @property
    def ratio(self) -> float | None:
        """得分率（0~1），用於護城河分級等比例判斷。"""
        if not self.has_any_data:
            return None
        return self.earned / self.scorable_max

    @property
    def missing_criteria(self) -> list[CriterionResult]:
        return [c for c in self.criteria if not c.is_scorable]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "max_points": self.max_points,
            "earned": self.earned,
            "criteria_points": self.criteria_points,
            "adjustments": [a.to_dict() for a in self.adjustments],
            "scorable_max": self.scorable_max,
            "normalized": self.normalized,
            "coverage": self.coverage,
            "criteria": [c.to_dict() for c in self.criteria],
        }


# --------------------------------------------------------------------------
# 門檻帶評分
# --------------------------------------------------------------------------


def score_criterion(
    key: str,
    value: DataPoint,
    criterion: dict,
) -> CriterionResult:
    """依門檻帶為單一指標評分。

    資料不足時回傳 ``points=None``（不計分、不計入分母），
    絕不退化成 0 分——那會讓「查不到資料」和「表現很差」混為一談。
    """
    label = criterion["label"]
    max_points = float(criterion["max_points"])
    rationale = criterion.get("rationale")

    if not value.is_available:
        return CriterionResult(
            key=key,
            label=label,
            value=value,
            max_points=max_points,
            points=None,
            band_label=None,
            rationale=rationale,
        )

    direction = criterion["direction"]
    actual = value.value
    assert actual is not None

    for band in criterion["bands"]:
        threshold = band.get("threshold")
        if threshold is None:
            hit = True
        elif direction == "higher_better":
            hit = actual >= threshold
        else:
            hit = actual <= threshold
        if hit:
            return CriterionResult(
                key=key,
                label=label,
                value=value,
                max_points=max_points,
                points=float(band["points"]),
                band_label=band.get("label"),
                rationale=rationale,
            )

    # _validate_criterion 保證有保底帶，理論上不會走到這裡。
    raise ConfigError(f"{key} 的 bands 沒有保底帶，無法評分")


def apply_override(
    result: CriterionResult,
    override: dict | None,
    *,
    today: date,
    max_age_days: int,
) -> CriterionResult:
    """套用人工覆寫，並保留原始計算值與理由供報表對照。"""
    if not override:
        return result

    reviewed_on = override["reviewed_on"]
    age_days = (today - reviewed_on).days
    result.computed_points = result.points
    result.points = float(override["points"])
    result.overridden = True
    result.override_rationale = str(override["rationale"]).strip()
    result.override_reviewed_on = reviewed_on
    result.override_stale = age_days > max_age_days
    return result


__all__ = [
    "ComponentScore",
    "ScoreAdjustment",
    "Config",
    "ConfigError",
    "CriterionResult",
    "QUALITATIVE_BONUS_KEY",
    "apply_override",
    "score_criterion",
]
