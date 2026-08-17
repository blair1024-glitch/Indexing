"""核心資料模型。

設計原則（對應使用者規格第十九、二十一節）：

1. 每一個數值都必須帶著來源（source）、資料日期（as_of）、財報期別（period）
   與抓取時間（fetched_at）。沒有出處的數字不允許進入系統。
2. 缺資料就是缺資料。`DataPoint.missing()` 帶著「為什麼沒有」的理由，
   在報表上渲染為「資料不足」，**永遠不會被當成 0 參與計算**。
3. 所有衍生計算都必須用本模組的 `safe_*` 系列函式，
   任一輸入缺失時結果自動變成 missing 並保留缺失原因，
   避免「缺料被算成 0 分」這種最危險的誤判。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Iterable, Iterator, Sequence

# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Provenance:
    """一筆數據的出處。規格第十九節要求所有重要數據標示來源與日期。"""

    source: str
    """來源識別，例如 ``TWSE:t187ap06_L_ci`` 或 ``FinMind:TaiwanStockBalanceSheet``。"""

    as_of: date | None = None
    """資料本身的日期（不是抓取日期）。"""

    period: str | None = None
    """財報期別，例如 ``2026Q2`` 或月營收的 ``2026-07``。"""

    fetched_at: datetime | None = None
    """抓取時間，UTC。"""

    url: str | None = None
    """可回溯的原始 URL。"""

    def describe(self) -> str:
        bits = [self.source]
        if self.period:
            bits.append(self.period)
        if self.as_of:
            bits.append(self.as_of.isoformat())
        return " | ".join(bits)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "period": self.period,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "url": self.url,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> Provenance | None:
        if not raw:
            return None
        return cls(
            source=raw.get("source", "unknown"),
            as_of=_parse_date(raw.get("as_of")),
            period=raw.get("period"),
            fetched_at=_parse_datetime(raw.get("fetched_at")),
            url=raw.get("url"),
        )


DERIVED = "derived"
"""由其他 DataPoint 計算而得的衍生值使用的來源標記。"""


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# DataPoint
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DataPoint:
    """一個帶出處的數值，或一個帶理由的缺值。

    絕不要直接讀 ``.value`` 做算術——它可能是 ``None``。
    請使用 ``safe_div`` / ``safe_sub`` / ``safe_mul`` 等函式，
    它們會把缺失狀態正確地傳播下去。
    """

    value: float | None
    provenance: Provenance | None = None
    unavailable_reason: str | None = None
    """僅在 value 為 None 時有意義：說明為什麼拿不到這個數字。"""

    # -- 建構 ---------------------------------------------------------------

    @classmethod
    def of(
        cls,
        value: float | int | None,
        source: str,
        *,
        as_of: date | None = None,
        period: str | None = None,
        fetched_at: datetime | None = None,
        url: str | None = None,
    ) -> DataPoint:
        if value is None:
            return cls.missing(f"{source} 未提供此欄位")
        return cls(
            value=float(value),
            provenance=Provenance(
                source=source,
                as_of=as_of,
                period=period,
                fetched_at=fetched_at,
                url=url,
            ),
        )

    @classmethod
    def missing(cls, reason: str) -> DataPoint:
        return cls(value=None, provenance=None, unavailable_reason=reason)

    @classmethod
    def derived(
        cls,
        value: float | None,
        *,
        inputs: Iterable[DataPoint] = (),
        note: str = "",
    ) -> DataPoint:
        """由其他 DataPoint 算出的值，繼承最舊的 as_of 以免高估資料新鮮度。"""
        if value is None:
            return cls.missing(note or "衍生計算輸入不足")
        prov_list = [dp.provenance for dp in inputs if dp.provenance]
        as_of_values = [p.as_of for p in prov_list if p.as_of]
        periods = [p.period for p in prov_list if p.period]
        sources = sorted({p.source for p in prov_list})
        return cls(
            value=float(value),
            provenance=Provenance(
                source=f"{DERIVED}({'+'.join(sources)})" if sources else DERIVED,
                # 衍生值只能和它最舊的輸入一樣新。
                as_of=min(as_of_values) if as_of_values else None,
                period=periods[0] if periods else None,
                fetched_at=utcnow(),
            ),
        )

    # -- 查詢 ---------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return self.value is not None

    def get(self, default: float | None = None) -> float | None:
        return self.value if self.value is not None else default

    def require(self) -> float:
        if self.value is None:
            raise ValueError(f"資料不足：{self.unavailable_reason}")
        return self.value

    def describe(self) -> str:
        if self.value is None:
            return f"資料不足（{self.unavailable_reason}）"
        prov = self.provenance.describe() if self.provenance else "來源未標示"
        return f"{self.value:,.4g}（{prov}）"

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "provenance": self.provenance.to_dict() if self.provenance else None,
            "unavailable_reason": self.unavailable_reason,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> DataPoint:
        if raw is None:
            return cls.missing("快照中無此欄位")
        return cls(
            value=raw.get("value"),
            provenance=Provenance.from_dict(raw.get("provenance")),
            unavailable_reason=raw.get("unavailable_reason"),
        )


MISSING = DataPoint.missing("未提供")


# --------------------------------------------------------------------------
# 缺失傳播的安全算術
# --------------------------------------------------------------------------


def _collect_missing(points: Sequence[DataPoint]) -> str | None:
    reasons = [p.unavailable_reason or "資料不足" for p in points if not p.is_available]
    if not reasons:
        return None
    # 去重但保留順序，避免報表出現一長串重複理由。
    seen: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return "；".join(seen)


def safe_div(numerator: DataPoint, denominator: DataPoint, *, note: str = "") -> DataPoint:
    """除法。分母為 0 或缺失時回傳 missing，而不是 0 或 inf。"""
    missing = _collect_missing([numerator, denominator])
    if missing:
        return DataPoint.missing(note or missing)
    denom = denominator.value
    if denom is None or denom == 0:
        return DataPoint.missing(note or "分母為 0，無法計算")
    result = numerator.value / denom  # type: ignore[operator]
    if not math.isfinite(result):
        return DataPoint.missing(note or "計算結果非有限數")
    return DataPoint.derived(result, inputs=[numerator, denominator])


def safe_sub(left: DataPoint, right: DataPoint, *, note: str = "") -> DataPoint:
    missing = _collect_missing([left, right])
    if missing:
        return DataPoint.missing(note or missing)
    return DataPoint.derived(left.value - right.value, inputs=[left, right])  # type: ignore[operator]


def safe_add(*points: DataPoint, note: str = "") -> DataPoint:
    missing = _collect_missing(points)
    if missing:
        return DataPoint.missing(note or missing)
    return DataPoint.derived(sum(p.value for p in points), inputs=points)  # type: ignore[misc]


def safe_mul(left: DataPoint, right: DataPoint, *, note: str = "") -> DataPoint:
    missing = _collect_missing([left, right])
    if missing:
        return DataPoint.missing(note or missing)
    return DataPoint.derived(left.value * right.value, inputs=[left, right])  # type: ignore[operator]


def safe_scale(point: DataPoint, factor: float, *, note: str = "") -> DataPoint:
    if not point.is_available:
        return DataPoint.missing(note or point.unavailable_reason or "資料不足")
    return DataPoint.derived(point.value * factor, inputs=[point])  # type: ignore[operator]


def safe_mean(points: Sequence[DataPoint], *, note: str = "") -> DataPoint:
    """僅在**全部**輸入都可得時才計算平均。

    刻意不採「有幾筆算幾筆」：3 年平均 ROE 若只有 1 年資料，
    算出來的東西不是 3 年平均，標成 3 年平均會誤導。
    """
    if not points:
        return DataPoint.missing(note or "無資料可計算平均")
    missing = _collect_missing(points)
    if missing:
        return DataPoint.missing(note or missing)
    values = [p.value for p in points]
    return DataPoint.derived(sum(values) / len(values), inputs=points)  # type: ignore[arg-type]


def cagr(first: DataPoint, last: DataPoint, years: float, *, note: str = "") -> DataPoint:
    """年複合成長率。

    起始值 <= 0 時無法定義 CAGR（負轉正沒有有意義的複合成長率），
    這種情況回傳 missing 並說明原因，而不是給一個假的數字。
    """
    missing = _collect_missing([first, last])
    if missing:
        return DataPoint.missing(note or missing)
    if years <= 0:
        return DataPoint.missing("期間長度無效，無法計算 CAGR")
    start, end = first.value, last.value
    if start is None or end is None:
        return DataPoint.missing("資料不足")
    if start <= 0:
        return DataPoint.missing("起始值 ≤ 0，CAGR 無定義（由虧損轉盈無複合成長率）")
    if end <= 0:
        return DataPoint.missing("期末值 ≤ 0，CAGR 無定義（由盈轉虧無複合成長率）")
    return DataPoint.derived((end / start) ** (1 / years) - 1, inputs=[first, last])


def stdev(points: Sequence[DataPoint], *, note: str = "") -> DataPoint:
    """母體標準差，用於衡量穩定度（毛利率波動、ROE 波動）。"""
    missing = _collect_missing(points)
    if missing:
        return DataPoint.missing(note or missing)
    if len(points) < 2:
        return DataPoint.missing("樣本數不足 2 期，無法計算波動度")
    values = [p.value for p in points]
    mean = sum(values) / len(values)  # type: ignore[arg-type]
    var = sum((v - mean) ** 2 for v in values) / len(values)  # type: ignore[operator]
    return DataPoint.derived(math.sqrt(var), inputs=points)


# --------------------------------------------------------------------------
# 財報期別
# --------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class FiscalPeriod:
    """財報期別。可排序，方便取最新期與計算 YoY/QoQ。"""

    year: int
    quarter: int
    """1-4；0 表示年度合併數（FY）。"""

    _QUARTER_RE = re.compile(r"^(\d{4})\s*[Qq](\d)$")
    _YEAR_RE = re.compile(r"^(\d{4})(?:\s*FY)?$", re.IGNORECASE)

    def __str__(self) -> str:
        return f"{self.year}Q{self.quarter}" if self.quarter else f"{self.year}FY"

    @property
    def is_annual(self) -> bool:
        return self.quarter == 0

    @classmethod
    def parse(cls, text: str) -> FiscalPeriod:
        text = text.strip()
        m = cls._QUARTER_RE.match(text)
        if m:
            quarter = int(m.group(2))
            if not 1 <= quarter <= 4:
                raise ValueError(f"季別必須是 1-4：{text}")
            return cls(int(m.group(1)), quarter)
        m = cls._YEAR_RE.match(text)
        if m:
            return cls(int(m.group(1)), 0)
        raise ValueError(f"無法解析財報期別：{text!r}")

    def prev_quarter(self) -> FiscalPeriod:
        """上一季（QoQ 比較基準）。"""
        if self.is_annual:
            return FiscalPeriod(self.year - 1, 0)
        if self.quarter == 1:
            return FiscalPeriod(self.year - 1, 4)
        return FiscalPeriod(self.year, self.quarter - 1)

    def prev_year(self) -> FiscalPeriod:
        """去年同期（YoY 比較基準）。"""
        return FiscalPeriod(self.year - 1, self.quarter)


# --------------------------------------------------------------------------
# 財報資料容器
# --------------------------------------------------------------------------


def _mp(reason: str = "未載入") -> DataPoint:
    return DataPoint.missing(reason)


@dataclass
class IncomeStatement:
    """綜合損益表。單位一律為新台幣元（來源若為千元，在 source 層換算完畢）。"""

    period: FiscalPeriod
    revenue: DataPoint = field(default_factory=_mp)
    cost_of_revenue: DataPoint = field(default_factory=_mp)
    gross_profit: DataPoint = field(default_factory=_mp)
    operating_expenses: DataPoint = field(default_factory=_mp)
    operating_income: DataPoint = field(default_factory=_mp)
    non_operating_income: DataPoint = field(default_factory=_mp)
    pretax_income: DataPoint = field(default_factory=_mp)
    net_income: DataPoint = field(default_factory=_mp)
    eps: DataPoint = field(default_factory=_mp)
    rnd_expense: DataPoint = field(default_factory=_mp)
    finance_cost: DataPoint = field(default_factory=_mp)
    """財務成本（利息費用）。用於利息保障倍數。"""

    @property
    def gross_margin(self) -> DataPoint:
        gp = self.gross_profit
        if not gp.is_available:
            gp = safe_sub(self.revenue, self.cost_of_revenue, note="缺毛利與成本，無法推算毛利率")
        return safe_div(gp, self.revenue, note="缺營收或毛利，無法計算毛利率")

    @property
    def operating_margin(self) -> DataPoint:
        return safe_div(self.operating_income, self.revenue, note="缺營收或營業利益，無法計算營益率")

    @property
    def net_margin(self) -> DataPoint:
        return safe_div(self.net_income, self.revenue, note="缺營收或稅後淨利，無法計算淨利率")

    @property
    def non_operating_ratio(self) -> DataPoint:
        """業外損益佔稅前淨利比重。過高是盈餘品質警訊（規格第五節）。"""
        return safe_div(
            self.non_operating_income,
            self.pretax_income,
            note="缺業外損益或稅前淨利，無法計算業外佔比",
        )

    @property
    def rnd_intensity(self) -> DataPoint:
        return safe_div(self.rnd_expense, self.revenue, note="缺研發費用或營收")

    @property
    def interest_coverage(self) -> DataPoint:
        """利息保障倍數＝營業利益／利息費用（規格第七節）。"""
        return safe_div(
            self.operating_income, self.finance_cost, note="缺營業利益或利息費用"
        )


@dataclass
class BalanceSheet:
    period: FiscalPeriod
    total_assets: DataPoint = field(default_factory=_mp)
    total_liabilities: DataPoint = field(default_factory=_mp)
    total_equity: DataPoint = field(default_factory=_mp)
    current_assets: DataPoint = field(default_factory=_mp)
    current_liabilities: DataPoint = field(default_factory=_mp)
    cash: DataPoint = field(default_factory=_mp)
    inventory: DataPoint = field(default_factory=_mp)
    receivables: DataPoint = field(default_factory=_mp)
    short_term_debt: DataPoint = field(default_factory=_mp)
    long_term_debt: DataPoint = field(default_factory=_mp)
    shares_outstanding: DataPoint = field(default_factory=_mp)

    @property
    def debt_ratio(self) -> DataPoint:
        return safe_div(self.total_liabilities, self.total_assets, note="缺負債或資產，無法計算負債比")

    @property
    def interest_bearing_debt(self) -> DataPoint:
        """有息負債＝短期借款＋長期借款。與總負債不同，後者含應付帳款等營運負債。"""
        return safe_add(self.short_term_debt, self.long_term_debt, note="缺短期或長期借款")

    @property
    def net_debt(self) -> DataPoint:
        return safe_sub(self.interest_bearing_debt, self.cash, note="缺有息負債或現金")

    @property
    def debt_to_equity(self) -> DataPoint:
        return safe_div(self.interest_bearing_debt, self.total_equity, note="缺有息負債或股東權益")

    @property
    def current_ratio(self) -> DataPoint:
        return safe_div(self.current_assets, self.current_liabilities, note="缺流動資產或流動負債")

    @property
    def equity_multiplier(self) -> DataPoint:
        """權益乘數（杜邦拆解用）。用來辨識「高 ROE 但靠高負債」。"""
        return safe_div(self.total_assets, self.total_equity, note="缺資產或股東權益")


@dataclass
class CashFlowStatement:
    period: FiscalPeriod
    operating_cash_flow: DataPoint = field(default_factory=_mp)
    capex: DataPoint = field(default_factory=_mp)
    """資本支出，一律存為正數（流出金額）。"""
    dividends_paid: DataPoint = field(default_factory=_mp)
    depreciation: DataPoint = field(default_factory=_mp)

    @property
    def free_cash_flow(self) -> DataPoint:
        return safe_sub(self.operating_cash_flow, self.capex, note="缺營業現金流或資本支出")


@dataclass
class MarketData:
    """市場面資料。皆為最新交易日快照。"""

    price: DataPoint = field(default_factory=_mp)
    pe_ratio: DataPoint = field(default_factory=_mp)
    pb_ratio: DataPoint = field(default_factory=_mp)
    dividend_yield: DataPoint = field(default_factory=_mp)
    market_cap: DataPoint = field(default_factory=_mp)
    pe_history: list[float] = field(default_factory=list)
    """歷史本益比序列，用於估算合理本益比分位帶。"""


@dataclass
class DividendRecord:
    year: int
    cash_dividend: DataPoint = field(default_factory=_mp)
    stock_dividend: DataPoint = field(default_factory=_mp)


@dataclass
class MonthlyRevenue:
    year: int
    month: int
    revenue: DataPoint = field(default_factory=_mp)
    yoy: DataPoint = field(default_factory=_mp)

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"


# --------------------------------------------------------------------------
# 公司
# --------------------------------------------------------------------------


@dataclass
class Company:
    """一檔 00929 成分股的完整資料集。

    所有序列一律**由舊到新**排序，方便計算趨勢與 CAGR。
    """

    stock_id: str
    name: str
    industry: str = "未分類"
    market: str = "TWSE"
    """TWSE（上市）或 TPEx（上櫃）。00929 兩者皆含。"""
    etf_weight: DataPoint = field(default_factory=_mp)

    income_statements: list[IncomeStatement] = field(default_factory=list)
    balance_sheets: list[BalanceSheet] = field(default_factory=list)
    cash_flows: list[CashFlowStatement] = field(default_factory=list)
    dividends: list[DividendRecord] = field(default_factory=list)
    monthly_revenues: list[MonthlyRevenue] = field(default_factory=list)
    market_data: MarketData = field(default_factory=MarketData)

    data_gaps: list[str] = field(default_factory=list)
    """抓取過程中記錄的缺料項目，會原樣呈現在報表上。"""

    def iter_data_points(self) -> Iterator[DataPoint]:
        """走訪這家公司所有帶 provenance 的數據點。

        報表的「資料來源」段落據此產生。列舉實際命中的來源，
        而不是把設定檔裡的優先序抄一遍——來源換掉時，
        寫死的說明會變成錯的，而且錯得很安靜（明細標 MOPS、總表卻說 FinMind）。
        """
        containers = (
            *self.income_statements,
            *self.balance_sheets,
            *self.cash_flows,
            *self.dividends,
            *self.monthly_revenues,
            self.market_data,
            self,
        )
        for container in containers:
            for value in vars(container).values():
                if isinstance(value, DataPoint):
                    yield value

    def __post_init__(self) -> None:
        self.income_statements.sort(key=lambda s: s.period)
        self.balance_sheets.sort(key=lambda s: s.period)
        self.cash_flows.sort(key=lambda s: s.period)
        self.dividends.sort(key=lambda d: d.year)
        self.monthly_revenues.sort(key=lambda m: (m.year, m.month))

    # -- 取單期 -------------------------------------------------------------

    @property
    def latest_income(self) -> IncomeStatement | None:
        return self.income_statements[-1] if self.income_statements else None

    @property
    def latest_balance(self) -> BalanceSheet | None:
        return self.balance_sheets[-1] if self.balance_sheets else None

    @property
    def latest_cash_flow(self) -> CashFlowStatement | None:
        return self.cash_flows[-1] if self.cash_flows else None

    @property
    def latest_period(self) -> FiscalPeriod | None:
        latest = self.latest_income
        return latest.period if latest else None

    def income_at(self, period: FiscalPeriod) -> IncomeStatement | None:
        return next((s for s in self.income_statements if s.period == period), None)

    def balance_at(self, period: FiscalPeriod) -> BalanceSheet | None:
        return next((s for s in self.balance_sheets if s.period == period), None)

    def cash_flow_at(self, period: FiscalPeriod) -> CashFlowStatement | None:
        return next((s for s in self.cash_flows if s.period == period), None)

    # -- 取序列 -------------------------------------------------------------

    def annual_incomes(self) -> list[IncomeStatement]:
        return [s for s in self.income_statements if s.period.is_annual]

    def annual_balances(self) -> list[BalanceSheet]:
        return [s for s in self.balance_sheets if s.period.is_annual]

    def annual_cash_flows(self) -> list[CashFlowStatement]:
        return [s for s in self.cash_flows if s.period.is_annual]

    def trailing_annual_incomes(self, years: int) -> list[IncomeStatement]:
        return self.annual_incomes()[-years:]

    # 年度指標一律用這組取數。``latest_income`` 取的是序列最後一筆，
    # 若序列同時含季報就會拿到季度數字——年度 ROE 配上單季淨利會嚴重失真。
    @property
    def latest_annual_income(self) -> IncomeStatement | None:
        annual = self.annual_incomes()
        return annual[-1] if annual else None

    @property
    def latest_annual_balance(self) -> BalanceSheet | None:
        annual = self.annual_balances()
        return annual[-1] if annual else None

    @property
    def latest_annual_cash_flow(self) -> CashFlowStatement | None:
        annual = self.annual_cash_flows()
        return annual[-1] if annual else None

    def note_gap(self, message: str) -> None:
        if message not in self.data_gaps:
            self.data_gaps.append(message)


__all__ = [
    "BalanceSheet",
    "CashFlowStatement",
    "Company",
    "DERIVED",
    "DataPoint",
    "DividendRecord",
    "FiscalPeriod",
    "IncomeStatement",
    "MISSING",
    "MarketData",
    "MonthlyRevenue",
    "Provenance",
    "cagr",
    "replace",
    "safe_add",
    "safe_div",
    "safe_mean",
    "safe_mul",
    "safe_scale",
    "safe_sub",
    "stdev",
    "utcnow",
]
