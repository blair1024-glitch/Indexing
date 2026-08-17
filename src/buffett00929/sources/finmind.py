"""FinMind v4｜多年度歷史回補。

TWSE/TPEx OpenAPI 只給最新一期，但巴菲特式分析需要 5–10 年序列
（ROE 趨勢、EPS/營收 CAGR、毛利率波動、歷史 PE 分位），所以歷史由此供應。

**長格式轉寬格式**：FinMind 的三大報表是長格式，每列一個科目：
``{date, stock_id, type, value, origin_name}``。本模組把它們樞紐成
每期一筆的 ``IncomeStatement`` / ``BalanceSheet`` / ``CashFlowStatement``。

**欄位對應策略**：同時比對英文 ``type`` 與中文 ``origin_name``，兩者都給一組
候選名稱。任何對不上的科目都會被 ``unmapped_types`` 記錄下來——
API schema 變動時我們要立刻知道，而不是默默算出錯誤的毛利率。
執行 ``buffett00929 verify-sources`` 可列出實際回傳的所有科目名稱。

**無 token 時的行為**：不中斷執行，但所有需要歷史的指標會標示「資料不足」，
而不是用單期資料假裝成多年平均。
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from ..models import (
    BalanceSheet,
    CashFlowStatement,
    DataPoint,
    DividendRecord,
    FiscalPeriod,
    IncomeStatement,
    MonthlyRevenue,
    utcnow,
)
from .base import FetchError, HttpClient, SourceUnavailable, parse_number

SOURCE = "FinMind"

# --------------------------------------------------------------------------
# 科目對應表
# --------------------------------------------------------------------------
# 每個邏輯欄位對應 (英文 type 候選, 中文 origin_name 候選)。
# 中文名稱來自公開資訊觀測站的標準科目，比英文代碼穩定，因此兩者都比對。

INCOME_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "revenue": (
        ("Revenue", "TotalOperatingRevenue", "OperatingRevenue", "NetSales"),
        ("營業收入合計", "營業收入", "淨營業收入"),
    ),
    "cost_of_revenue": (
        ("CostOfGoodsSold", "TotalOperatingCosts", "OperatingCosts"),
        ("營業成本合計", "營業成本"),
    ),
    "gross_profit": (
        ("GrossProfit", "GrossProfitLoss", "GrossProfitLossFromOperations"),
        ("營業毛利（毛損）", "營業毛利(毛損)", "營業毛利（毛損）淨額"),
    ),
    "operating_expenses": (
        ("OperatingExpenses", "TotalOperatingExpenses"),
        ("營業費用合計", "營業費用"),
    ),
    "operating_income": (
        ("OperatingIncome", "OperatingProfitLoss", "NetOperatingIncome"),
        ("營業利益（損失）", "營業利益(損失)", "營業利益"),
    ),
    "non_operating_income": (
        ("TotalNonoperatingIncomeAndExpense", "NonoperatingIncomeAndExpense"),
        ("營業外收入及支出合計", "營業外收入及支出"),
    ),
    "pretax_income": (
        ("PreTaxIncome", "ProfitLossBeforeTax", "IncomeBeforeIncomeTax"),
        ("稅前淨利（淨損）", "稅前淨利(淨損)", "繼續營業單位稅前淨利（淨損）"),
    ),
    "net_income": (
        ("IncomeAfterTaxes", "ProfitLoss", "NetIncome", "TotalConsolidatedProfitForThePeriod"),
        ("本期淨利（淨損）", "本期淨利(淨損)", "合併總損益"),
    ),
    "eps": (("EPS", "BasicEarningsPerShare"), ("基本每股盈餘", "基本每股盈餘（元）")),
    "rnd_expense": (
        ("ResearchAndDevelopmentExpenses", "ResearchAndDevelopmentExpense"),
        ("研究發展費用",),
    ),
    "finance_cost": (
        ("FinancialCost", "FinanceCosts", "InterestExpense"),
        ("財務成本淨額", "財務成本", "利息費用"),
    ),
}

BALANCE_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "total_assets": (("TotalAssets",), ("資產總計", "資產總額")),
    "total_liabilities": (("TotalLiabilities",), ("負債總計", "負債總額")),
    "total_equity": (
        ("TotalEquity", "Equity", "EquityAttributableToOwnersOfParent", "StockholdersEquity"),
        ("權益總計", "權益總額", "歸屬於母公司業主之權益合計"),
    ),
    "current_assets": (("TotalCurrentAssets", "CurrentAssets"), ("流動資產合計", "流動資產")),
    "current_liabilities": (
        ("TotalCurrentLiabilities", "CurrentLiabilities"),
        ("流動負債合計", "流動負債"),
    ),
    "cash": (
        ("CashAndCashEquivalents",),
        ("現金及約當現金",),
    ),
    "inventory": (("Inventories", "Inventory"), ("存貨",)),
    "receivables": (
        ("AccountsReceivableNet", "AccountsReceivable", "NotesAndAccountsReceivable"),
        ("應收帳款淨額", "應收帳款"),
    ),
    "short_term_debt": (
        ("ShortTermBorrowings", "ShortTermLoans", "ShortTermBillsPayable"),
        ("短期借款", "應付短期票券"),
    ),
    "long_term_debt": (
        ("LongTermBorrowings", "LongTermLoans", "BondsPayable", "NoncurrentLongTermBorrowings"),
        ("長期借款", "應付公司債"),
    ),
    "shares_outstanding": (
        ("OrdinaryShare", "CommonStock", "ShareCapitalCommonStock", "CapitalStock"),
        ("普通股股本", "股本合計"),
    ),
}

CASHFLOW_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "operating_cash_flow": (
        (
            "CashFlowsProvidedFromOperatingActivities",
            "NetCashProvidedByOperatingActivities",
            "CashProvidedByOperatingActivities",
        ),
        ("營業活動之淨現金流入（流出）", "營業活動之淨現金流入(流出)"),
    ),
    "capex": (
        (
            "AcquisitionOfPropertyPlantAndEquipment",
            "PropertyAndPlantAndEquipment",
            "PaymentsToAcquirePropertyPlantAndEquipment",
        ),
        ("取得不動產、廠房及設備", "購置不動產、廠房及設備"),
    ),
    "depreciation": (("Depreciation", "DepreciationExpense"), ("折舊費用", "折舊")),
    "dividends_paid": (
        ("CashDividendsPaid", "PaymentOfDividends"),
        ("發放現金股利", "支付股利"),
    ),
}


@dataclass
class FinMindClient:
    http: HttpClient
    config: dict
    token: str | None = None
    unmapped_types: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _last_request_at: float = 0.0

    def __post_init__(self) -> None:
        if self.token is None:
            self.token = os.environ.get(self.config.get("token_env", "FINMIND_TOKEN"), "").strip()

    @property
    def is_available(self) -> bool:
        """無 token 時 FinMind 免費層對多數 dataset 會拒絕，視為不可用。"""
        return bool(self.token) and bool(self.config.get("enabled", True))

    @property
    def unavailable_reason(self) -> str:
        if not self.config.get("enabled", True):
            return "FinMind 於 sources.yaml 中停用"
        env = self.config.get("token_env", "FINMIND_TOKEN")
        return f"未設定 {env} 環境變數，無法取得多年度歷史資料"

    def _dataset(self, key: str) -> str:
        datasets = self.config.get("datasets") or {}
        if key not in datasets:
            raise SourceUnavailable(f"sources.yaml 的 finmind.datasets 缺少 {key}")
        return datasets[key]

    def _throttle(self) -> None:
        """免費方案有速率限制，主動間隔避免被擋。"""
        interval = float(self.config.get("request_interval_seconds", 0))
        if interval <= 0:
            return
        elapsed = time.time() - self._last_request_at
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_request_at = time.time()

    def fetch(self, dataset_key: str, stock_id: str, start_date: str | None = None) -> list[dict]:
        if not self.is_available:
            raise SourceUnavailable(self.unavailable_reason)

        self._throttle()
        params = {
            "dataset": self._dataset(dataset_key),
            "data_id": stock_id,
        }
        if start_date:
            params["start_date"] = start_date

        try:
            payload = self.http.get_json(
                self.config["base_url"],
                params=params,
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except FetchError as exc:
            raise SourceUnavailable(f"FinMind {dataset_key} 取得失敗：{exc}") from exc

        if not isinstance(payload, dict):
            raise SourceUnavailable(f"FinMind {dataset_key} 回傳格式非預期")
        if payload.get("status") not in (200, None):
            raise SourceUnavailable(
                f"FinMind {dataset_key} 回傳錯誤：{payload.get('msg', payload.get('status'))}"
            )
        data = payload.get("data")
        return data if isinstance(data, list) else []

    def start_date(self) -> str:
        years = int(self.config.get("history_years", 10))
        return f"{date.today().year - years}-01-01"

    # ------------------------------------------------------------------
    # 長格式 → 寬格式
    # ------------------------------------------------------------------

    def _pivot(
        self,
        rows: list[dict],
        field_map: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
        dataset_key: str,
    ) -> dict[str, dict[str, DataPoint]]:
        """把長格式列樞紐成 ``{報表日期: {邏輯欄位: DataPoint}}``。"""
        # 先依日期分組，再於組內對應科目。
        by_date: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            report_date = row.get("date")
            if report_date:
                by_date[str(report_date)].append(row)

        # 反查表：把候選名稱攤平成 名稱 -> 邏輯欄位。
        lookup: dict[str, str] = {}
        for logical, (en_names, zh_names) in field_map.items():
            for name in (*en_names, *zh_names):
                lookup[name] = logical

        result: dict[str, dict[str, DataPoint]] = {}
        for report_date, group in by_date.items():
            fields: dict[str, DataPoint] = {}
            for row in group:
                type_name = str(row.get("type", "")).strip()
                origin = str(row.get("origin_name", "")).strip()
                logical = lookup.get(type_name) or lookup.get(origin)
                if logical is None:
                    if type_name:
                        self.unmapped_types[dataset_key].add(f"{type_name} / {origin}")
                    continue
                # 同一邏輯欄位若有多個候選命中，以先出現者為準。
                if logical in fields:
                    continue
                value = parse_number(row.get("value"))
                if value is None:
                    continue
                fields[logical] = DataPoint.of(
                    value,
                    f"{SOURCE}:{self._dataset(dataset_key)}",
                    as_of=_parse_iso_date(report_date),
                    period=str(_period_from_date(report_date)),
                    fetched_at=utcnow(),
                    url=self.config["base_url"],
                )
            result[report_date] = fields
        return result

    # ------------------------------------------------------------------
    # 三大報表
    # ------------------------------------------------------------------

    def income_statements(self, stock_id: str) -> list[IncomeStatement]:
        rows = self.fetch("income_statement", stock_id, self.start_date())
        pivoted = self._pivot(rows, INCOME_FIELDS, "income_statement")
        statements = []
        for report_date, fields in sorted(pivoted.items()):
            period = _period_from_date(report_date)
            if period is None:
                continue
            statements.append(
                IncomeStatement(
                    period=period,
                    **{
                        key: fields.get(key, DataPoint.missing(f"FinMind 未提供 {key}"))
                        for key in INCOME_FIELDS
                    },
                )
            )
        return statements

    def balance_sheets(self, stock_id: str) -> list[BalanceSheet]:
        rows = self.fetch("balance_sheet", stock_id, self.start_date())
        pivoted = self._pivot(rows, BALANCE_FIELDS, "balance_sheet")
        sheets = []
        for report_date, fields in sorted(pivoted.items()):
            period = _period_from_date(report_date)
            if period is None:
                continue
            sheets.append(
                BalanceSheet(
                    period=period,
                    **{
                        key: fields.get(key, DataPoint.missing(f"FinMind 未提供 {key}"))
                        for key in BALANCE_FIELDS
                    },
                )
            )
        return sheets

    def cash_flows(self, stock_id: str) -> list[CashFlowStatement]:
        rows = self.fetch("cash_flow", stock_id, self.start_date())
        pivoted = self._pivot(rows, CASHFLOW_FIELDS, "cash_flow")
        flows = []
        for report_date, fields in sorted(pivoted.items()):
            period = _period_from_date(report_date)
            if period is None:
                continue
            capex = fields.get("capex", DataPoint.missing("FinMind 未提供資本支出"))
            # 現金流量表的投資支出為負數；模型統一存正數（流出金額）。
            if capex.is_available and capex.value is not None and capex.value < 0:
                capex = DataPoint.derived(abs(capex.value), inputs=[capex])
            flows.append(
                CashFlowStatement(
                    period=period,
                    operating_cash_flow=fields.get(
                        "operating_cash_flow", DataPoint.missing("FinMind 未提供營業現金流")
                    ),
                    capex=capex,
                    depreciation=fields.get("depreciation", DataPoint.missing("FinMind 未提供折舊")),
                    dividends_paid=fields.get(
                        "dividends_paid", DataPoint.missing("FinMind 未提供股利支付")
                    ),
                )
            )
        return flows

    # ------------------------------------------------------------------
    # 股利、月營收、歷史本益比
    # ------------------------------------------------------------------

    def dividends(self, stock_id: str) -> list[DividendRecord]:
        rows = self.fetch("dividend", stock_id, self.start_date())
        by_year: dict[int, DividendRecord] = {}
        for row in rows:
            raw_date = row.get("date") or row.get("CashExDividendTradingDate")
            parsed = _parse_iso_date(str(raw_date)) if raw_date else None
            if parsed is None:
                continue
            year = parsed.year
            cash = parse_number(row.get("CashEarningsDistribution")) or 0.0
            cash += parse_number(row.get("CashStatutorySurplus")) or 0.0
            stock = parse_number(row.get("StockEarningsDistribution")) or 0.0
            stock += parse_number(row.get("StockStatutorySurplus")) or 0.0

            source = f"{SOURCE}:{self._dataset('dividend')}"
            record = by_year.get(year)
            if record is None:
                by_year[year] = DividendRecord(
                    year=year,
                    cash_dividend=DataPoint.of(cash, source, as_of=parsed, period=str(year)),
                    stock_dividend=DataPoint.of(stock, source, as_of=parsed, period=str(year)),
                )
            else:
                # 同年度多次配息要累加（00929 成分股不少採季配或月配）。
                record.cash_dividend = DataPoint.of(
                    (record.cash_dividend.get(0.0) or 0.0) + cash, source, as_of=parsed, period=str(year)
                )
                record.stock_dividend = DataPoint.of(
                    (record.stock_dividend.get(0.0) or 0.0) + stock, source, as_of=parsed, period=str(year)
                )
        return [by_year[y] for y in sorted(by_year)]

    def monthly_revenues(self, stock_id: str) -> list[MonthlyRevenue]:
        rows = self.fetch("monthly_revenue", stock_id, self.start_date())
        source = f"{SOURCE}:{self._dataset('monthly_revenue')}"
        results = []
        for row in rows:
            year = parse_number(row.get("revenue_year"))
            month = parse_number(row.get("revenue_month"))
            revenue = parse_number(row.get("revenue"))
            if year is None or month is None:
                continue
            results.append(
                MonthlyRevenue(
                    year=int(year),
                    month=int(month),
                    revenue=DataPoint.of(
                        revenue,
                        source,
                        as_of=_parse_iso_date(str(row.get("date", ""))),
                        period=f"{int(year)}-{int(month):02d}",
                    ),
                )
            )
        return sorted(results, key=lambda m: (m.year, m.month))

    def pe_history(self, stock_id: str) -> list[float]:
        """歷史本益比序列，用於估算合理本益比分位帶。"""
        rows = self.fetch("per", stock_id, self.start_date())
        values = []
        for row in rows:
            pe = parse_number(row.get("PER"))
            # 本益比 <= 0（虧損）不納入分位帶計算，會扭曲合理本益比。
            if pe is not None and pe > 0:
                values.append(pe)
        return values

    def latest_price(self, stock_id: str) -> DataPoint:
        rows = self.fetch("price", stock_id, self.start_date())
        if not rows:
            return DataPoint.missing("FinMind 未提供股價")
        latest = max(rows, key=lambda r: str(r.get("date", "")))
        return DataPoint.of(
            parse_number(latest.get("close")),
            f"{SOURCE}:{self._dataset('price')}",
            as_of=_parse_iso_date(str(latest.get("date", ""))),
        )

    # ------------------------------------------------------------------
    # 診斷
    # ------------------------------------------------------------------

    def discover_types(self, stock_id: str, dataset_key: str) -> list[str]:
        """列出某 dataset 實際回傳的所有科目名稱。

        供 ``verify-sources`` 使用：當 FinMind 調整科目命名時，
        用真實資料一次看出哪些科目沒對應到，而不是逐一猜測。
        """
        rows = self.fetch(dataset_key, stock_id, self.start_date())
        seen = {
            f"{str(r.get('type', '')).strip()} / {str(r.get('origin_name', '')).strip()}"
            for r in rows
        }
        return sorted(name for name in seen if name.strip(" /"))


# --------------------------------------------------------------------------
# 日期輔助
# --------------------------------------------------------------------------


def _parse_iso_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text[:10])
    except (ValueError, TypeError):
        return None


def _period_from_date(text: str) -> FiscalPeriod | None:
    """財報日期轉期別。3/31→Q1，6/30→Q2，9/30→Q3，12/31→Q4。"""
    parsed = _parse_iso_date(str(text))
    if parsed is None:
        return None
    quarter = (parsed.month - 1) // 3 + 1
    return FiscalPeriod(parsed.year, quarter)


__all__ = [
    "BALANCE_FIELDS",
    "CASHFLOW_FIELDS",
    "INCOME_FIELDS",
    "FinMindClient",
]
