"""紅旗警報系統（規格第十六節）。

規格列了 16 種警訊。其中 14 種可由財報數字客觀判定，本模組全部實作。

**另外 2 種無法從財報推導**：「公司治理重大問題」與「競爭對手市占率快速增加」。
這兩件事需要重大訊息、法說會與產業資料，硬用財報數字近似只會產生
看起來很客觀、實際上沒有根據的判斷。因此它們的偵測器會回報
「無法自動偵測」，並讀取 ``data/manual/governance.yaml`` 讓你手動登錄
——有登錄才觸發，沒登錄就明白說沒有檢查，不會假裝檢查過了。

每個偵測器回傳 ``RedFlag(triggered, severity, evidence)``，
觸發後依 ``config/scoring.yaml`` 的 ``red_flags.penalties`` 扣分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Sequence

import yaml

from .metrics import CompanyMetrics, balance, cashflow, dividend, roe, stability
from .models import Company, DataPoint

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


@dataclass
class RedFlag:
    code: str
    label: str
    severity: str
    evidence: str
    checked: bool = True
    """False 代表這一項沒有被檢查（資料不足或無法自動偵測），而非「檢查過沒問題」。"""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "label": self.label,
            "severity": self.severity,
            "evidence": self.evidence,
            "checked": self.checked,
        }


@dataclass
class RedFlagReport:
    triggered: list[RedFlag] = field(default_factory=list)
    unchecked: list[RedFlag] = field(default_factory=list)
    """無法判定的項目。呈現在報表上，避免「沒有紅旗」被誤讀為「已全面確認安全」。"""

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.triggered if f.severity == SEVERITY_CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.triggered if f.severity == SEVERITY_WARNING)

    def penalty(self, config) -> float:
        """依設定計算總扣分，並套用上限。"""
        total = sum(config.red_flag_penalty(f.severity) for f in self.triggered)
        return min(total, config.max_total_penalty)

    def to_dict(self) -> dict:
        return {
            "triggered": [f.to_dict() for f in self.triggered],
            "unchecked": [f.to_dict() for f in self.unchecked],
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
        }


# --------------------------------------------------------------------------
# 序列輔助
# --------------------------------------------------------------------------


def _consecutive_decline(points: Sequence[DataPoint], years: int) -> tuple[bool, str]:
    """判斷序列是否連續下降 N 年。

    需要 N+1 個資料點才能構成 N 次比較；資料不足時回傳 (False, 原因)，
    由呼叫端歸類為「未檢查」而非「沒問題」。
    """
    usable = [p for p in points if p.is_available and p.value is not None]
    if len(usable) < years + 1:
        return False, f"可用年度僅 {len(usable)} 年，需 {years + 1} 年才能判斷連續 {years} 年下降"

    window = usable[-(years + 1) :]
    values = [p.value for p in window]
    declining = all(values[i + 1] < values[i] for i in range(len(values) - 1))  # type: ignore[operator]
    trend = " → ".join(f"{v:.4g}" for v in values)  # type: ignore[arg-type]
    return declining, trend


# --------------------------------------------------------------------------
# 偵測器
# --------------------------------------------------------------------------


def detect(
    company: Company,
    metrics: CompanyMetrics,
    config,
    *,
    repo_root: Path | None = None,
) -> RedFlagReport:
    """執行全部 16 項紅旗偵測。"""
    report = RedFlagReport()
    thresholds = (config.scoring.get("red_flags") or {}).get("thresholds") or {}

    incomes = company.annual_incomes()

    # 1. ROE 連續下降
    _add_decline_flag(
        report,
        code="roe_decline",
        label="ROE 連續下降",
        points=[dp for _, dp in roe.annual_roe_series(company)],
        years=int(thresholds.get("roe_decline_years", 3)),
        severity=SEVERITY_CRITICAL,
        unit="ROE",
    )

    # 2. 毛利率連續下降
    _add_decline_flag(
        report,
        code="gross_margin_decline",
        label="毛利率連續下降",
        points=[s.gross_margin for s in incomes],
        years=int(thresholds.get("margin_decline_years", 3)),
        severity=SEVERITY_CRITICAL,
        unit="毛利率",
    )

    # 3. 營業利益率連續下降
    _add_decline_flag(
        report,
        code="operating_margin_decline",
        label="營業利益率連續下降",
        points=[s.operating_margin for s in incomes],
        years=int(thresholds.get("margin_decline_years", 3)),
        severity=SEVERITY_WARNING,
        unit="營益率",
    )

    # 4. EPS 連續下降
    _add_decline_flag(
        report,
        code="eps_decline",
        label="EPS 連續下降",
        points=[s.eps for s in incomes],
        years=int(thresholds.get("eps_decline_years", 3)),
        severity=SEVERITY_CRITICAL,
        unit="EPS",
    )

    # 5. FCF 轉負
    report_add(report, _flag_fcf_negative(company))

    # 6. 負債快速增加
    report_add(
        report,
        _flag_growth(
            code="debt_surge",
            label="有息負債快速增加",
            growth=balance.debt_growth(company),
            threshold=float(thresholds.get("debt_growth_annual", 0.30)),
            severity=SEVERITY_WARNING,
            description="有息負債年增",
        ),
    )

    # 7. 應收帳款快速增加（相對營收）
    recv_growth, rev_growth = balance.receivables_growth_vs_revenue(company)
    report_add(
        report,
        _flag_relative_growth(
            code="receivables_surge",
            label="應收帳款成長遠快於營收",
            item_growth=recv_growth,
            revenue_growth=rev_growth,
            multiple=float(thresholds.get("receivables_growth_vs_revenue", 1.5)),
            item_label="應收帳款",
        ),
    )

    # 8. 存貨快速增加（相對營收）
    inv_growth, rev_growth2 = balance.inventory_growth_vs_revenue(company)
    report_add(
        report,
        _flag_relative_growth(
            code="inventory_surge",
            label="存貨成長遠快於營收",
            item_growth=inv_growth,
            revenue_growth=rev_growth2,
            multiple=float(thresholds.get("inventory_growth_vs_revenue", 1.5)),
            item_label="存貨",
        ),
    )

    # 9. 股本快速膨脹 + 10. 大量增資
    share_growth = balance.share_count_growth(company)
    threshold = float(thresholds.get("share_expansion_annual", 0.05))
    report_add(
        report,
        _flag_growth(
            code="share_expansion",
            label="股本快速膨脹",
            growth=share_growth,
            threshold=threshold,
            severity=SEVERITY_WARNING,
            description="在外流通股數年化成長",
        ),
    )
    report_add(report, _flag_large_issuance(company, share_growth, threshold))

    # 11. 配息率異常升高
    report_add(
        report,
        _flag_payout_ratio(company, float(thresholds.get("payout_ratio_max", 1.0))),
    )

    # 12. 業外收益佔比過高
    report_add(
        report,
        _flag_non_operating(company, float(thresholds.get("non_operating_ratio_max", 0.40))),
    )

    # 13. 大量一次性收益
    report_add(report, _flag_one_off_gains(metrics))

    # 14. 公司治理重大問題（需人工登錄）
    # 15. 競爭對手市占率快速增加（需人工登錄）
    for flag in _manual_flags(company, repo_root):
        report_add(report, flag)

    # 16. 護城河弱化
    report_add(report, _flag_moat_erosion(company))

    return report


def report_add(report: RedFlagReport, flag: RedFlag | None) -> None:
    if flag is None:
        return
    if flag.checked:
        report.triggered.append(flag)
    else:
        report.unchecked.append(flag)


def _add_decline_flag(
    report: RedFlagReport,
    *,
    code: str,
    label: str,
    points: Sequence[DataPoint],
    years: int,
    severity: str,
    unit: str,
) -> None:
    declining, detail = _consecutive_decline(points, years)
    if declining:
        report.triggered.append(
            RedFlag(
                code=code,
                label=label,
                severity=severity,
                evidence=f"{unit} 連續 {years} 年下降：{detail}",
            )
        )
    elif detail.startswith("可用年度"):
        report.unchecked.append(
            RedFlag(code=code, label=label, severity=severity, evidence=detail, checked=False)
        )


def _flag_fcf_negative(company: Company) -> RedFlag | None:
    fcf = cashflow.latest_fcf(company)
    if not fcf.is_available or fcf.value is None:
        return RedFlag(
            code="fcf_negative",
            label="自由現金流轉負",
            severity=SEVERITY_CRITICAL,
            evidence=fcf.unavailable_reason or "缺自由現金流資料",
            checked=False,
        )
    if fcf.value < 0:
        return RedFlag(
            code="fcf_negative",
            label="自由現金流轉負",
            severity=SEVERITY_CRITICAL,
            evidence=f"最新年度自由現金流為 {fcf.value / 1e8:.2f} 億元",
        )
    return None


def _flag_growth(
    *,
    code: str,
    label: str,
    growth: DataPoint,
    threshold: float,
    severity: str,
    description: str,
) -> RedFlag | None:
    if not growth.is_available or growth.value is None:
        return RedFlag(
            code=code,
            label=label,
            severity=severity,
            evidence=growth.unavailable_reason or "資料不足",
            checked=False,
        )
    if growth.value > threshold:
        return RedFlag(
            code=code,
            label=label,
            severity=severity,
            evidence=f"{description} {growth.value:+.1%}，超過 {threshold:.0%} 門檻",
        )
    return None


def _flag_relative_growth(
    *,
    code: str,
    label: str,
    item_growth: DataPoint,
    revenue_growth: DataPoint,
    multiple: float,
    item_label: str,
) -> RedFlag | None:
    if not item_growth.is_available or not revenue_growth.is_available:
        reason = item_growth.unavailable_reason or revenue_growth.unavailable_reason
        return RedFlag(
            code=code,
            label=label,
            severity=SEVERITY_WARNING,
            evidence=reason or "資料不足",
            checked=False,
        )

    item_value = item_growth.value
    rev_value = revenue_growth.value
    if item_value is None or rev_value is None:
        return None

    # 營收衰退時倍數比較會失真，改以「營收縮、該項增」直接判定。
    if rev_value <= 0:
        if item_value > 0.10:
            return RedFlag(
                code=code,
                label=label,
                severity=SEVERITY_WARNING,
                evidence=f"營收 {rev_value:+.1%} 但{item_label} {item_value:+.1%}，方向背離",
            )
        return None

    if item_value > rev_value * multiple and item_value > 0.10:
        return RedFlag(
            code=code,
            label=label,
            severity=SEVERITY_WARNING,
            evidence=(
                f"{item_label}成長 {item_value:+.1%}，營收成長 {rev_value:+.1%}"
                f"（超過 {multiple} 倍）"
            ),
        )
    return None


def _flag_large_issuance(
    company: Company, share_growth: DataPoint, threshold: float
) -> RedFlag | None:
    """大量增資：股數年化成長顯著超標（門檻的兩倍）視為大規模稀釋。"""
    if not share_growth.is_available or share_growth.value is None:
        return None
    if share_growth.value > threshold * 2:
        return RedFlag(
            code="large_issuance",
            label="大量增資稀釋股東",
            severity=SEVERITY_CRITICAL,
            evidence=(
                f"在外流通股數年化成長 {share_growth.value:+.1%}，"
                f"為稀釋門檻 {threshold:.0%} 的兩倍以上"
            ),
        )
    return None


def _flag_payout_ratio(company: Company, maximum: float) -> RedFlag | None:
    ratio = dividend.payout_ratio(company)
    if not ratio.is_available or ratio.value is None:
        return RedFlag(
            code="payout_spike",
            label="配息率異常升高",
            severity=SEVERITY_WARNING,
            evidence=ratio.unavailable_reason or "資料不足",
            checked=False,
        )
    if ratio.value > maximum:
        return RedFlag(
            code="payout_spike",
            label="配息率異常升高",
            severity=SEVERITY_WARNING,
            evidence=f"配息率 {ratio.value:.0%} 超過 {maximum:.0%}，配息高於盈餘",
        )
    return None


def _flag_non_operating(company: Company, maximum: float) -> RedFlag | None:
    ratio = stability.non_operating_ratio_average(company, years=3)
    if not ratio.is_available or ratio.value is None:
        return RedFlag(
            code="non_operating_high",
            label="業外收益佔比過高",
            severity=SEVERITY_WARNING,
            evidence=ratio.unavailable_reason or "資料不足",
            checked=False,
        )
    if ratio.value > maximum:
        return RedFlag(
            code="non_operating_high",
            label="業外收益佔比過高",
            severity=SEVERITY_WARNING,
            evidence=f"近 3 年業外損益佔稅前淨利 {ratio.value:.0%}，超過 {maximum:.0%}",
        )
    return None


def _flag_one_off_gains(metrics: CompanyMetrics) -> RedFlag | None:
    """大量一次性收益：由盈餘品質警訊中的 EPS 暴增與毛利率跳動推導。"""
    relevant = [
        w for w in metrics.earnings_quality_warnings if w.code in ("eps_spike", "margin_jump")
    ]
    if not relevant:
        return None
    return RedFlag(
        code="one_off_gains",
        label="疑似大量一次性收益",
        severity=SEVERITY_WARNING,
        evidence="；".join(f"{w.message}（{w.evidence}）" for w in relevant),
    )


def _flag_moat_erosion(company: Company) -> RedFlag | None:
    """護城河弱化：以毛利率長期趨勢作為代理指標。"""
    trend = stability.gross_margin_trend(company)
    if not trend.is_available or trend.value is None:
        return RedFlag(
            code="moat_erosion",
            label="護城河弱化",
            severity=SEVERITY_CRITICAL,
            evidence=trend.unavailable_reason or "資料不足",
            checked=False,
        )
    if trend.value < -0.03:
        return RedFlag(
            code="moat_erosion",
            label="護城河弱化",
            severity=SEVERITY_CRITICAL,
            evidence=(
                f"近 3 年平均毛利率較前 3 年下降 {abs(trend.value):.1%}，"
                "定價權可能正在流失"
            ),
        )
    return None


# --------------------------------------------------------------------------
# 需人工登錄的兩項
# --------------------------------------------------------------------------

_MANUAL_FLAG_SPECS = (
    (
        "governance",
        "公司治理重大問題",
        SEVERITY_CRITICAL,
        "無法由財報數字判定，需重大訊息／法說會資料；請於 data/manual/governance.yaml 登錄",
    ),
    (
        "competitive_share_loss",
        "競爭對手市占率快速增加",
        SEVERITY_CRITICAL,
        "無法由單一公司財報判定，需產業市占資料；請於 data/manual/governance.yaml 登錄",
    ),
)


def _manual_flags(company: Company, repo_root: Path | None) -> list[RedFlag]:
    """讀取人工登錄的治理與競爭警訊。

    沒有登錄檔就明確標示「未檢查」——這比預設「沒問題」誠實得多，
    因為財報數字本來就看不出董事會爭議或市占流失。
    """
    entries: dict = {}
    if repo_root is not None:
        path = repo_root / "data" / "manual" / "governance.yaml"
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
                entries = (raw.get("issues") or {}).get(company.stock_id) or {}
            except (yaml.YAMLError, OSError):
                entries = {}

    flags: list[RedFlag] = []
    for code, label, severity, unchecked_reason in _MANUAL_FLAG_SPECS:
        entry = entries.get(code)
        if not entry:
            flags.append(
                RedFlag(code=code, label=label, severity=severity, evidence=unchecked_reason, checked=False)
            )
            continue
        note = str(entry.get("note", "")).strip()
        reported = entry.get("reported_on")
        reported_text = reported.isoformat() if isinstance(reported, date) else str(reported or "未填日期")
        flags.append(
            RedFlag(
                code=code,
                label=label,
                severity=severity,
                evidence=f"{note}（人工登錄，{reported_text}）",
            )
        )
    return flags


__all__ = [
    "RedFlag",
    "RedFlagReport",
    "SEVERITY_CRITICAL",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "detect",
]
