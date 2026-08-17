"""資料載入編排。

把各來源組裝成 ``Company`` 物件，並執行規格第十九節的來源優先序：

* **官方優先**：TWSE / TPEx OpenAPI 的最新一期，覆蓋 FinMind 的同期資料。
* **FinMind 補歷史**：官方端點沒有歷史，5–10 年序列由 FinMind 提供。
* **缺料就標示**：任何來源都拿不到的欄位保持 missing 並記錄原因，
  絕不以其他期別或其他公司的數字填補。

單一公司抓取失敗不會中斷整批分析——記錄下來、標示缺料、繼續處理其他公司，
但成分股名單本身抓不到則整批中止（見 ``sources/constituents.py``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .models import Company, MarketData
from .normalize import (
    CumulativeDetection,
    cash_flow_to_single_quarter,
    detect_cumulative,
    to_annual_balances,
    to_annual_cash_flows,
    to_annual_incomes,
    to_single_quarter,
)
from .sources.base import FetchError, HttpClient, SourceUnavailable
from .sources.cache import DiskCache
from .sources.constituents import Constituent, ConstituentResolver, ConstituentSet
from .sources.finmind import FinMindClient
from .sources.tpex import make_tpex_client
from .sources.twse import TwseClient


@dataclass
class LoadedCompany:
    """公司資料加上載入過程的診斷資訊。"""

    company: Company
    detection: CumulativeDetection
    quarterly_incomes: list = field(default_factory=list)
    """單季化後的損益表，供 QoQ 比較使用（年度指標一律用年度數）。"""


@dataclass
class DataLoader:
    config: Config
    repo_root: Path
    http: HttpClient = field(init=False)
    twse: TwseClient = field(init=False)
    tpex: TwseClient = field(init=False)
    finmind: FinMindClient = field(init=False)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        sources = self.config.sources
        http_cfg = sources.get("http") or {}
        cache_cfg = sources.get("cache") or {}
        cache = DiskCache(
            directory=self.repo_root / cache_cfg.get("dir", "data/raw"),
            ttl_hours=float(cache_cfg.get("ttl_hours", 12)),
            enabled=bool(cache_cfg.get("enabled", True)),
        )
        self.http = HttpClient(
            timeout=float(http_cfg.get("timeout_seconds", 30)),
            max_retries=int(http_cfg.get("max_retries", 3)),
            backoff=tuple(http_cfg.get("backoff_seconds", (2, 4, 8))),
            user_agent=str(http_cfg.get("user_agent", "buffett00929/0.1")),
            cache=cache,
        )
        self.twse = TwseClient(http=self.http, config=sources.get("twse") or {})
        self.tpex = make_tpex_client(self.http, sources.get("tpex") or {})
        self.finmind = FinMindClient(http=self.http, config=sources.get("finmind") or {})

    # ------------------------------------------------------------------
    # 成分股
    # ------------------------------------------------------------------

    def load_constituents(self) -> ConstituentSet:
        resolver = ConstituentResolver(
            http=self.http,
            config=self.config.sources.get("constituents") or {},
            repo_root=self.repo_root,
        )
        return resolver.resolve()

    # ------------------------------------------------------------------
    # 單一公司
    # ------------------------------------------------------------------

    def load_company(self, constituent: Constituent) -> LoadedCompany:
        company = Company(
            stock_id=constituent.stock_id,
            name=constituent.name,
            market=constituent.market,
            etf_weight=constituent.weight,
        )

        # --- 1. 歷史（FinMind），取得季度序列 ---------------------------------
        if self.finmind.is_available:
            self._load_history(company)
        else:
            company.note_gap(
                f"多年度歷史未載入：{self.finmind.unavailable_reason}"
                "（CAGR、5/10 年平均 ROE、毛利率趨勢、歷史本益比分位將標示資料不足）"
            )

        # --- 2. 官方最新一期覆蓋同期 FinMind 值（規格第十九節：官方優先）--------
        # 必須在年度彙總之前做，否則官方數字不會進到最終結果。
        self._overlay_official(company)

        # --- 3. 期別正規化（累計 → 單季 → 年度）-------------------------------
        detection = detect_cumulative(company.income_statements)
        quarterly_incomes = to_single_quarter(company.income_statements, detection)
        company.income_statements = to_annual_incomes(company.income_statements, detection)
        company.balance_sheets = to_annual_balances(company.balance_sheets)
        company.cash_flows = to_annual_cash_flows(company.cash_flows, detection.is_cumulative)

        return LoadedCompany(
            company=company,
            detection=detection,
            quarterly_incomes=quarterly_incomes,
        )

    def _load_history(self, company: Company) -> None:
        stock_id = company.stock_id
        loaders = (
            ("綜合損益表", lambda: self.finmind.income_statements(stock_id), "income_statements"),
            ("資產負債表", lambda: self.finmind.balance_sheets(stock_id), "balance_sheets"),
            ("現金流量表", lambda: self.finmind.cash_flows(stock_id), "cash_flows"),
            ("股利", lambda: self.finmind.dividends(stock_id), "dividends"),
            ("月營收", lambda: self.finmind.monthly_revenues(stock_id), "monthly_revenues"),
        )
        for label, fetch, attribute in loaders:
            try:
                setattr(company, attribute, fetch())
            except (SourceUnavailable, FetchError) as exc:
                company.note_gap(f"{label}歷史未載入：{exc}")

        try:
            company.market_data.pe_history = self.finmind.pe_history(stock_id)
        except (SourceUnavailable, FetchError) as exc:
            company.note_gap(f"歷史本益比未載入：{exc}（合理本益比將改用設定值上下限）")

        # 現金流量表同為累計揭露，先換算單季再彙總年度。
        if company.cash_flows:
            company.cash_flows = cash_flow_to_single_quarter(company.cash_flows, is_cumulative=True)

    def _overlay_official(self, company: Company) -> None:
        """以官方最新一期覆蓋 FinMind 同期資料（規格第十九節：官方優先）。"""
        client = self.tpex if company.market.upper() == "TPEX" else self.twse
        stock_id = company.stock_id

        try:
            market = client.market_data(stock_id)
        except SourceUnavailable as exc:
            company.note_gap(f"官方市場資料未載入：{exc}")
            market = MarketData()

        # 保留 FinMind 的歷史本益比序列，其餘欄位以官方值為準。
        pe_history = company.market_data.pe_history
        company.market_data = market
        company.market_data.pe_history = pe_history

        if not company.market_data.price.is_available and self.finmind.is_available:
            try:
                company.market_data.price = self.finmind.latest_price(stock_id)
            except (SourceUnavailable, FetchError):
                pass

        try:
            official_income = client.income_statement(stock_id)
        except SourceUnavailable as exc:
            company.note_gap(f"最新綜合損益表未載入：{exc}")
        else:
            _merge_official(company.income_statements, official_income, company, "綜合損益表")

        try:
            official_balance = client.balance_sheet(stock_id)
        except SourceUnavailable as exc:
            company.note_gap(f"最新資產負債表未載入：{exc}")
        else:
            _merge_official(company.balance_sheets, official_balance, company, "資產負債表")

        try:
            official_revenue = client.monthly_revenue(stock_id)
        except SourceUnavailable as exc:
            company.note_gap(f"最新月營收未載入：{exc}")
        else:
            if official_revenue is not None:
                company.monthly_revenues = [
                    m
                    for m in company.monthly_revenues
                    if (m.year, m.month) != (official_revenue.year, official_revenue.month)
                ] + [official_revenue]
                company.monthly_revenues.sort(key=lambda m: (m.year, m.month))

        if client.schema_watch.has_issues:
            for issue in client.schema_watch.unknown_fields:
                company.note_gap(f"欄位對應異常：{issue}")

    # ------------------------------------------------------------------
    # 批次
    # ------------------------------------------------------------------

    def classify_markets(self, constituents: ConstituentSet) -> None:
        """判定每檔成分股屬於上市（TWSE）或上櫃（TPEx）。

        持股 API 只給代號與名稱，沒有市場別，但財報端點分屬兩個交易所，
        搞錯就整批抓不到資料。以證交所的每日收盤行情清單為準：
        在清單內即為上市，否則歸為上櫃。

        清單抓不到時保持預設（TWSE）並記錄警告——猜錯市場別會導致缺料，
        缺料會被正確標示，不會變成錯誤數字。
        """
        try:
            listed = self.twse._fetch_indexed("daily_price", id_fields=("Code", "公司代號"))
        except SourceUnavailable as exc:
            self.warnings.append(
                f"無法取得上市股票清單，成分股市場別一律以上市處理：{exc}"
            )
            return

        if not listed:
            return

        for constituent in constituents.constituents:
            constituent.market = "TWSE" if constituent.stock_id in listed else "TPEx"

    def load_all(self, constituents: ConstituentSet) -> list[LoadedCompany]:
        self.classify_markets(constituents)

        loaded: list[LoadedCompany] = []
        for constituent in constituents.constituents:
            try:
                loaded.append(self.load_company(constituent))
            except Exception as exc:  # noqa: BLE001 - 單一公司失敗不應中斷整批
                self.warnings.append(
                    f"{constituent.stock_id} {constituent.name} 載入失敗：{exc}"
                )
                fallback = Company(
                    stock_id=constituent.stock_id,
                    name=constituent.name,
                    market=constituent.market,
                    etf_weight=constituent.weight,
                )
                fallback.note_gap(f"資料載入失敗：{exc}")
                loaded.append(
                    LoadedCompany(
                        company=fallback,
                        detection=CumulativeDetection(True, "low", "資料載入失敗，未進行偵測"),
                    )
                )

        if self.finmind.unmapped_types:
            for dataset, names in self.finmind.unmapped_types.items():
                self.warnings.append(
                    f"FinMind {dataset} 有 {len(names)} 個科目未對應到欄位；"
                    "執行 `buffett00929 verify-sources` 可列出完整名稱"
                )
        return loaded


def _merge_official(statements: list, official, company: Company, label: str) -> None:
    """把官方最新一期併入既有序列。

    逐欄位合併而非整筆替換：官方值優先，但官方沒給的欄位保留 FinMind 的值。
    整筆替換會讓官方端點缺的欄位（例如研發費用）平白消失，
    反而讓資料變少——官方優先指的是同一欄位誰說了算，不是誰整筆覆蓋誰。
    """
    if official is None:
        return

    existing = next((s for s in statements if s.period == official.period), None)
    if existing is None:
        statements.append(official)
        statements.sort(key=lambda s: s.period)
        return

    replaced: list[str] = []
    for field_name, official_value in vars(official).items():
        if field_name == "period" or not hasattr(official_value, "is_available"):
            continue
        if official_value.is_available:
            setattr(existing, field_name, official_value)
            replaced.append(field_name)

    if replaced:
        company.note_gap(
            f"{label} {official.period} 已以官方數字覆蓋 {len(replaced)} 個欄位（官方優先）"
        )


__all__ = ["DataLoader", "LoadedCompany"]
