"""公開資訊觀測站（MOPS）彙總報表——多年度財報的主要來源。

## 為什麼是彙總報表

逐檔查詢是 50 檔 × 約 40 期 × 3 張表 ≈ 6000 次請求，必定被擋。
彙總報表**一次請求就涵蓋該期別全市場**，10 年 3 張表約 120 次請求，
而且抓一次就把所有成分股都涵蓋了——換股後新成分股的歷史已在快取內。

## 實測確認的存取方式（2026-08-17）

新版 MOPS（``mops.twse.com.tw``）是 SPA，舊路徑只回轉址殘骸；
但**舊網域 ``mopsov.twse.com.tw`` 仍供應彙總報表**，查詢條件由
新版前端的 chunk 定義反推而得::

    POST https://mopsov.twse.com.tw/mops/web/ajax_t163sb04
    TYPEK=sii&year=114&season=01        （民國年；season 01-04）

    t163sb04 綜合損益表    t163sb05 資產負債表    t163sb20 現金流量表

回應是一整頁 HTML，內含 **7 個表格**——依業別分開（一般業、金融業、
證券業、保險業……），欄位不同。

## 解析策略：掃描所有列，而不是切表格

舊版 MOPS 的 HTML 是巢狀表格，用 ``<table>…</table>`` 去切會在內層表格
提早截斷。因此改成**依序掃描整份文件的 ``<tr>``**：
遇到含「公司代號」的列就當作表頭、重新定義後續欄位，直到下一個表頭為止。
這對巢狀結構免疫，也剛好對應「一頁多個業別表格」的實際版面。

## 單位

彙總報表以**新台幣仟元**揭露，本模組在回傳前一律換算成元，
與 ``models`` 的約定一致（每股盈餘、每股淨值等每股數值不換算）。

## 這個來源給不了什麼

彙總報表的現金流量表只有三大活動淨額，**沒有資本支出明細**，
因此 FCF 與盈再率無法由此推得——這些欄位會保持 missing 並記錄原因，
不會用投資活動淨額之類的近似值冒充（那會把金融資產買賣算成資本支出）。
有 FinMind token 時由 FinMind 補；沒有就照規格標示「資料不足」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from typing import Iterable

from ..models import (
    BalanceSheet,
    CashFlowStatement,
    DataPoint,
    FiscalPeriod,
    IncomeStatement,
    Provenance,
    utcnow,
)
from .base import (
    PAR_VALUE,
    FetchError,
    HttpClient,
    SchemaWatch,
    SourceUnavailable,
    parse_number,
)
from .periods import is_published, is_settled, periods_to_fetch, to_roc

THOUSAND = 1000.0

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")

ID_COLUMN = "公司代號"
NAME_COLUMN = "公司名稱"

# 全形／半形正規化表。
#
# 實測（2026-08-17）確認 MOPS 一律用**全形括號**：「營業利益（損失）」。
# 用半形括號寫候選名會一個都對不上——而且失敗方式很隱蔽：
# 「營業收入」沒有括號所以照樣命中，於是看起來只有部分欄位缺料，
# 像是業別差異而不是對應寫錯。因此比對前兩邊都先正規化，
# 而不是把每個欄名的全形與半形版本都列一遍。
_WIDTH = str.maketrans({"（": "(", "）": ")", "－": "-", "─": "-", "　": ""})


def normalize_column(name: str) -> str:
    return name.translate(_WIDTH).replace(" ", "")


# 欄位對應。每個邏輯欄位給一組候選中文欄名——彙總報表分 6 種業別版面，
# 欄名不一致（銀行業叫「資產總額」，一般業叫「資產總計」），
# 對不上的欄位保持 missing 並由 SchemaWatch 記錄。
INCOME_COLUMNS: dict[str, tuple[str, ...]] = {
    # 銀行業沒有單一的「營業收入」（只有利息淨收益與利息以外淨損益），
    # 這裡不硬湊——湊出來的營收會讓毛利率與淨利率全部失真。
    "revenue": ("營業收入", "淨收益", "收益", "收入"),
    "cost_of_revenue": ("營業成本", "支出及費用", "支出"),
    # 「淨額」是加計未實現／已實現銷貨損益後的數字，與營收同基礎，優先採用。
    "gross_profit": ("營業毛利(毛損)淨額", "營業毛利(毛損)", "營業毛利"),
    "operating_expenses": ("營業費用",),
    "operating_income": ("營業利益(損失)", "營業利益", "營業損益"),
    "non_operating_income": ("營業外收入及支出", "營業外損益"),
    "pretax_income": (
        "稅前淨利(淨損)",
        "繼續營業單位稅前淨利(淨損)",
        "繼續營業單位稅前損益",
        "繼續營業單位稅前純益(純損)",
    ),
    "net_income": ("本期淨利(淨損)", "本期稅後淨利(淨損)"),
    "eps": ("基本每股盈餘(元)", "基本每股盈餘"),
}

# 稅後淨利優先取「歸屬於母公司業主」——ROE 的分子必須與分母（母公司權益）一致。
# 兩種寫法都存在：一般業用「淨利（淨損）」，銀行業用「淨利（損）」。
NET_INCOME_PARENT = (
    "淨利(淨損)歸屬於母公司業主",
    "淨利(損)歸屬於母公司業主",
)

BALANCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "total_assets": ("資產總計", "資產總額"),
    "total_liabilities": ("負債總計", "負債總額"),
    "total_equity": ("權益總計", "權益總額"),
    "current_assets": ("流動資產",),
    "current_liabilities": ("流動負債",),
    # 一般業的彙總資產負債表沒有現金欄，改由現金流量表的期末餘額回填。
    "cash": ("現金及約當現金",),
    "retained_earnings": ("保留盈餘", "保留盈餘（或累積虧損）"),
    "book_value_per_share": ("每股參考淨值",),
}

# 「股本」是**金額**不是股數，換算需要面額。
SHARE_CAPITAL = ("股本",)
"""面額由 base.PAR_VALUE 提供。並非所有公司都是 10 元，故換算後必須驗算。"""

SHARE_COUNT_TOLERANCE = 0.05
"""兩路股數估計的容許差距。超過就代表面額假設不成立。"""

PER_SHARE_FIELDS = frozenset({"book_value_per_share"})
"""每股數值不換算仟元——與 income 的 eps 同一個規則。"""

# 權益優先取歸屬於母公司業主的部分，理由同上。三種業別寫法都不一樣。
EQUITY_PARENT = (
    "歸屬於母公司業主之權益合計",
    "歸屬於母公司業主權益合計",
    "歸屬於母公司業主之權益",
)

CASHFLOW_COLUMNS: dict[str, tuple[str, ...]] = {
    "operating_cash_flow": ("營業活動之淨現金流入(流出)", "營業活動之現金流量"),
}

# 一般業的資產負債表沒有現金欄，但現金流量表有期末餘額——同一個數字。
ENDING_CASH = ("期末現金及約當現金餘額",)

REPORTS = {
    "income": ("t163sb04", "綜合損益表"),
    "balance": ("t163sb05", "資產負債表"),
    "cashflow": ("t163sb20", "現金流量表"),
}

MARKETS = {"TWSE": "sii", "TPEx": "otc"}


def _text(html: str) -> str:
    return " ".join(unescape(_TAG.sub("", html)).split())


def scan_rows(html: str) -> Iterable[tuple[list[str], list[str]]]:
    """依序掃描整份文件，產出 ``(表頭, 資料列)``。

    含「公司代號」的列視為表頭，重新定義後續資料列的欄位；
    直到下一個表頭出現為止。巢狀表格不影響判讀。
    """
    header: list[str] = []
    for match in _ROW.finditer(html):
        cells = [_text(c) for c in _CELL.findall(match.group(1))]
        if not cells:
            continue
        if ID_COLUMN in cells:
            header = cells
            continue
        if header and cells[0].isdigit() and len(cells) == len(header):
            yield header, cells


def _column_index(header: list[str], candidates: Iterable[str]) -> int | None:
    normalized = [normalize_column(name) for name in header]
    for name in candidates:
        target = normalize_column(name)
        if target in normalized:
            return normalized.index(target)
    return None



def _share_count(
    share_capital: DataPoint, equity: DataPoint, book_value_per_share: DataPoint
) -> DataPoint:
    """由股本推算在外流通股數，並以每股淨值交叉驗算。

    台灣的「股本」是**金額**不是股數，換算要除以面額。面額多為 10 元，
    但並非一律如此——直接假設會算出錯的股數，而錯的股數會讓「股本稀釋」
    這個指標無聲地給出錯誤結論，比缺料更糟。

    因此另外用 ``權益 ÷ 每股淨值`` 算第二個估計值：這條路徑與面額無關。
    兩者相差超過容忍值就代表面額假設不成立，回傳缺料。
    每股淨值本身有四捨五入，所以容忍值不能太緊。
    """
    if not share_capital.is_available or share_capital.value in (None, 0):
        return DataPoint.missing("MOPS 彙總報表未提供股本，無法推算在外流通股數")

    by_par = share_capital.value / PAR_VALUE  # type: ignore[operator]

    if (
        equity.is_available
        and book_value_per_share.is_available
        and book_value_per_share.value not in (None, 0)
    ):
        by_book = equity.value / book_value_per_share.value  # type: ignore[operator]
        if by_book > 0 and abs(by_par - by_book) / by_book > SHARE_COUNT_TOLERANCE:
            return DataPoint.missing(
                f"股本 ÷ 面額 {PAR_VALUE:g} 元推得 {by_par:,.0f} 股，"
                f"但權益 ÷ 每股淨值推得 {by_book:,.0f} 股，"
                "差距過大（面額可能非 10 元），不採用推估股數"
            )

    return DataPoint.derived(by_par, inputs=[share_capital])

@dataclass
class MopsClient:
    """彙總報表存取。以**期別**為單位抓取，不是以公司為單位。"""

    http: HttpClient
    config: dict = field(default_factory=dict)
    schema_watch: SchemaWatch = field(default_factory=lambda: SchemaWatch("MOPS 彙總報表"))
    warnings: list[str] = field(default_factory=list)
    share_count_rejections: dict[str, str] = field(default_factory=dict)
    """交叉驗算不通過而被退掉的股數，``{股號: 原因}``。

    被退掉是對的——但**退掉之後會沉默地退回 FinMind 的股本**，而那條路徑沒有
    任何驗算。8070／6548 的 DCF 因此用了一個沒人驗過的股數。所以退件本身
    必須留下痕跡，讓下一次執行看得到是哪幾檔、差多少。
    """

    @property
    def base_url(self) -> str:
        return str(self.config.get("base_url", "https://mopsov.twse.com.tw"))

    @property
    def is_available(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def years(self) -> int:
        return int(self.config.get("years", 10))

    def _url(self, code: str) -> str:
        return f"{self.base_url}/mops/web/ajax_{code}"

    def fetch_report(
        self, code: str, market: str, period: FiscalPeriod, *, today: date
    ) -> str:
        """抓取單一期別、單一市場的彙總報表 HTML。

        已定案的期別走永久快取（見 ``cache.py``）——歷史財報不會變，
        每日排程重抓十年只會被擋。
        """
        season = f"{4 if period.is_annual else period.quarter:02d}"
        body = {
            "encodeURIComponent": "1",
            "step": "1",
            "firstin": "1",
            "off": "1",
            "TYPEK": market,
            "year": str(to_roc(period.year)),
            "season": season,
        }
        return self.http.post_form(
            self._url(code),
            body,
            namespace="mops",
            immutable=is_settled(period, today),
        )

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def _provenance(self, code: str, period: FiscalPeriod, as_of: date) -> Provenance:
        return Provenance(
            source=f"MOPS 彙總報表 {code}",
            as_of=as_of,
            period=str(period),
            fetched_at=utcnow(),
            url=self._url(code),
        )

    def _point(
        self,
        header: list[str],
        row: list[str],
        candidates: Iterable[str],
        provenance: Provenance,
        *,
        scale: float,
        label: str,
    ) -> DataPoint:
        index = _column_index(header, candidates)
        if index is None:
            self.schema_watch.note_missing(f"{label}（候選：{' / '.join(candidates)}）")
            return DataPoint.missing(f"MOPS 彙總報表未提供{label}")
        value = parse_number(row[index])
        if value is None:
            return DataPoint.missing(f"MOPS 彙總報表 {label} 為空值")
        return DataPoint(value=value * scale, provenance=provenance)

    def parse_income(
        self, html: str, period: FiscalPeriod, as_of: date
    ) -> dict[str, IncomeStatement]:
        provenance = self._provenance(REPORTS["income"][0], period, as_of)
        out: dict[str, IncomeStatement] = {}
        for header, row in scan_rows(html):
            statement = IncomeStatement(period=period)
            for field_name, candidates in INCOME_COLUMNS.items():
                scale = 1.0 if field_name == "eps" else THOUSAND
                setattr(
                    statement,
                    field_name,
                    self._point(
                        header, row, candidates, provenance, scale=scale, label=field_name
                    ),
                )
            # ROE 的分子要與分母一致：優先用歸屬於母公司業主的淨利。
            parent = self._point(
                header, row, NET_INCOME_PARENT, provenance, scale=THOUSAND,
                label="淨利歸屬於母公司業主",
            )
            if parent.is_available:
                statement.net_income = parent
            out[row[0]] = statement
        return out

    def parse_balance(
        self, html: str, period: FiscalPeriod, as_of: date
    ) -> dict[str, BalanceSheet]:
        provenance = self._provenance(REPORTS["balance"][0], period, as_of)
        out: dict[str, BalanceSheet] = {}
        for header, row in scan_rows(html):
            sheet = BalanceSheet(period=period)
            for field_name, candidates in BALANCE_COLUMNS.items():
                # 每股數值本來就是元，跟著乘一千會變成荒謬的每股淨值，
                # 而且會讓股數的交叉驗算永遠不通過（同 EPS 的處理）。
                scale = 1.0 if field_name in PER_SHARE_FIELDS else THOUSAND
                setattr(
                    sheet,
                    field_name,
                    self._point(
                        header, row, candidates, provenance, scale=scale, label=field_name
                    ),
                )
            parent_equity = self._point(
                header, row, EQUITY_PARENT, provenance, scale=THOUSAND,
                label="歸屬於母公司業主之權益",
            )
            if parent_equity.is_available:
                sheet.total_equity = parent_equity

            share_capital = self._point(
                header, row, SHARE_CAPITAL, provenance, scale=THOUSAND, label="股本",
            )
            sheet.shares_outstanding = _share_count(
                share_capital, sheet.total_equity, sheet.book_value_per_share
            )
            if share_capital.is_available and not sheet.shares_outstanding.is_available:
                self.share_count_rejections[row[0]] = (
                    f"{period}：{sheet.shares_outstanding.unavailable_reason}"
                )
            out[row[0]] = sheet
        return out

    def parse_cashflow(
        self, html: str, period: FiscalPeriod, as_of: date
    ) -> dict[str, CashFlowStatement]:
        provenance = self._provenance(REPORTS["cashflow"][0], period, as_of)
        out: dict[str, CashFlowStatement] = {}
        for header, row in scan_rows(html):
            statement = CashFlowStatement(period=period)
            statement.operating_cash_flow = self._point(
                header, row, CASHFLOW_COLUMNS["operating_cash_flow"], provenance,
                scale=THOUSAND, label="營業活動淨現金流",
            )
            statement.ending_cash = self._point(
                header, row, ENDING_CASH, provenance,
                scale=THOUSAND, label="期末現金及約當現金餘額",
            )
            # 彙總報表沒有資本支出明細——標示缺料，不用投資活動淨額冒充。
            statement.capex = DataPoint.missing(
                "MOPS 彙總報表僅揭露投資活動淨額，無資本支出明細"
                "（FCF 與盈再率需 FinMind 或 XBRL 補充）"
            )
            out[row[0]] = statement
        return out

    # ------------------------------------------------------------------
    # 批次回補
    # ------------------------------------------------------------------

    def backfill(self, today: date | None = None) -> "MopsHistory":
        """把 N 年份的三張表抓齊，建成 {股號: {期別: 報表}} 索引。"""
        if not self.is_available:
            raise SourceUnavailable("MOPS 來源已停用")

        today = today or date.today()
        history = MopsHistory()
        periods = periods_to_fetch(today, years=self.years)

        for period in periods:
            for market in MARKETS.values():
                for kind, (code, label) in REPORTS.items():
                    try:
                        html = self.fetch_report(code, market, period, today=today)
                    except (FetchError, SourceUnavailable) as exc:
                        self.warnings.append(
                            f"MOPS {label} {period} {market} 未取得：{exc}"
                        )
                        continue
                    parser = {
                        "income": self.parse_income,
                        "balance": self.parse_balance,
                        "cashflow": self.parse_cashflow,
                    }[kind]
                    parsed = parser(html, period, as_of=today)
                    if not parsed:
                        self.warnings.append(
                            f"MOPS {label} {period} {market} 解析不到任何公司"
                            "（版面可能已變更）"
                        )
                    history.add(kind, parsed)

        if self.share_count_rejections:
            codes = sorted(self.share_count_rejections)
            sample = self.share_count_rejections[codes[0]]
            self.warnings.append(
                f"MOPS 股數交叉驗算未通過 {len(codes)} 檔："
                f"{'、'.join(codes)}。這些公司的股數改由 FinMind 股本推得，"
                f"而該路徑無驗算，每股估值須留意。範例（{codes[0]}）：{sample}"
            )
        return history


@dataclass
class MopsHistory:
    """全市場的歷史財報索引：``{股號: [報表, …]}``，依期別排序。"""

    incomes: dict[str, dict[FiscalPeriod, IncomeStatement]] = field(default_factory=dict)
    balances: dict[str, dict[FiscalPeriod, BalanceSheet]] = field(default_factory=dict)
    cash_flows: dict[str, dict[FiscalPeriod, CashFlowStatement]] = field(default_factory=dict)

    def add(self, kind: str, parsed: dict) -> None:
        target = {
            "income": self.incomes,
            "balance": self.balances,
            "cashflow": self.cash_flows,
        }[kind]
        for stock_id, statement in parsed.items():
            target.setdefault(stock_id, {})[statement.period] = statement

    def _sorted(self, index: dict, stock_id: str) -> list:
        by_period = index.get(stock_id) or {}
        return [by_period[p] for p in sorted(by_period)]

    def income_statements(self, stock_id: str) -> list[IncomeStatement]:
        return self._sorted(self.incomes, stock_id)

    def balance_sheets(self, stock_id: str) -> list[BalanceSheet]:
        return self._sorted(self.balances, stock_id)

    def cash_flow_statements(self, stock_id: str) -> list[CashFlowStatement]:
        return self._sorted(self.cash_flows, stock_id)

    @property
    def company_count(self) -> int:
        return len(set(self.incomes) | set(self.balances) | set(self.cash_flows))


__all__ = [
    "MopsClient",
    "MopsHistory",
    "REPORTS",
    "is_published",
    "scan_rows",
]
