"""證交所 OpenAPI（上市公司）。

**重要限制（已實測確認）**：TWSE OpenAPI 的每個端點都只回傳「最新一期」，
不接受日期參數、不提供歷史查詢。因此本模組的角色是
「最新一期的官方權威值」，多年度歷史由 FinMind 回補（見 ``finmind.py``）。

**重要語意**：台灣財報揭露為**累計數**——Q2 的營收是上半年累計，
Q3 是前三季累計，Q4 累計即全年。本模組原樣保留累計數並標記 ``cumulative=True``，
由 ``normalize.py`` 負責換算單季數。把累計數當單季用會讓 QoQ 完全失真。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..models import (
    BalanceSheet,
    CashFlowStatement,
    DataPoint,
    FiscalPeriod,
    IncomeStatement,
    MarketData,
    MonthlyRevenue,
    utcnow,
)
from .base import FetchError, HttpClient, SchemaWatch, SourceUnavailable, make_point, parse_number

SOURCE_PREFIX = "TWSE"

_ID_FIELDS = ("Code", "公司代號", "SecuritiesCompanyCode")
"""行情類端點的股號欄位。

證交所用 ``Code``，櫃買用 ``SecuritiesCompanyCode``。兩邊都列，
這個 client 才能同時服務上市與上櫃——認不出股號的話，
整批資料抓回來卻一筆也對不上，而且不會報錯，只會全部標示資料不足。
"""


@dataclass
class TwseClient:
    """證交所 OpenAPI 客戶端。

    每個端點回傳全市場所有公司的資料，因此一次抓取後建立 stock_id 索引，
    避免對每家公司重複請求。
    """

    http: HttpClient
    config: dict
    source_prefix: str = SOURCE_PREFIX
    """``TWSE``（上市）或 ``TPEx``（上櫃）。00929 兩個市場都有成分股。"""
    _index_cache: dict[str, dict[str, dict]] = field(default_factory=dict)
    schema_watch: SchemaWatch = field(default_factory=lambda: SchemaWatch(SOURCE_PREFIX))

    def __post_init__(self) -> None:
        self.schema_watch = SchemaWatch(self.source_prefix)

    @property
    def base_url(self) -> str:
        return self.config["base_url"].rstrip("/")

    def _source_id(self, key: str) -> str:
        """來源標記直接由端點路徑推導，確保報表上的出處永遠與實際端點一致。"""
        endpoints = self.config.get("endpoints") or {}
        path = str(endpoints.get(key, key)).strip("/")
        return f"{self.source_prefix}:{path.split('/')[-1]}"

    @property
    def multiplier(self) -> float:
        """TWSE opendata 財報金額單位為新台幣仟元，轉換為元。"""
        return float(self.config.get("amount_multiplier", 1000))

    def _endpoint(self, key: str) -> str:
        endpoints = self.config.get("endpoints") or {}
        if key not in endpoints:
            raise SourceUnavailable(f"sources.yaml 的 twse.endpoints 缺少 {key}")
        return f"{self.base_url}{endpoints[key]}"

    def _fetch_indexed(self, key: str, id_fields: tuple[str, ...] = ("公司代號", "Code")) -> dict[str, dict]:
        """抓取整份端點資料並以股票代號建索引（含快取，避免重複請求）。"""
        if key in self._index_cache:
            return self._index_cache[key]

        url = self._endpoint(key)
        try:
            payload = self.http.get_json(url)
        except FetchError as exc:
            raise SourceUnavailable(f"TWSE {key} 無法取得：{exc}") from exc

        if not isinstance(payload, list):
            raise SourceUnavailable(f"TWSE {key} 回傳格式非預期（應為陣列）")

        index: dict[str, dict] = {}
        for record in payload:
            if not isinstance(record, dict):
                continue
            stock_id = None
            for id_field in id_fields:
                if record.get(id_field):
                    stock_id = str(record[id_field]).strip()
                    break
            if stock_id:
                index[stock_id] = record

        self._index_cache[key] = index
        return index

    # ------------------------------------------------------------------
    # 綜合損益表
    # ------------------------------------------------------------------

    def income_statement(self, stock_id: str) -> IncomeStatement | None:
        index = self._fetch_indexed("income_statement")
        record = index.get(stock_id)
        if record is None:
            return None

        period = _period_from_record(record)
        if period is None:
            return None
        source = self._source_id("income_statement")
        url = self._endpoint("income_statement")
        common = dict(
            source=source,
            period=str(period),
            fetched_at=utcnow(),
            multiplier=self.multiplier,
            url=url,
            watch=self.schema_watch,
        )
        return IncomeStatement(
            period=period,
            revenue=make_point(record, ("營業收入", "營業收入合計", "收入合計"), **common),
            cost_of_revenue=make_point(record, ("營業成本", "營業成本合計"), **common),
            gross_profit=make_point(
                record, ("營業毛利（毛損）", "營業毛利(毛損)", "營業毛利（毛損）淨額"), **common
            ),
            operating_expenses=make_point(record, ("營業費用", "營業費用合計"), **common),
            operating_income=make_point(
                record, ("營業利益（損失）", "營業利益(損失)", "營業利益"), **common
            ),
            non_operating_income=make_point(
                record, ("營業外收入及支出", "營業外收入及支出合計"), **common
            ),
            pretax_income=make_point(
                record, ("稅前淨利（淨損）", "稅前淨利(淨損)", "繼續營業單位稅前淨利（淨損）"), **common
            ),
            net_income=make_point(
                record,
                ("本期淨利（淨損）", "本期淨利(淨損)", "母公司業主（淨利／損）", "本期綜合損益總額"),
                **common,
            ),
            # EPS 是「元／股」，不適用仟元換算。
            eps=make_point(
                record,
                ("基本每股盈餘（元）", "基本每股盈餘(元)", "每股盈餘"),
                **{**common, "multiplier": 1.0},
            ),
        )

    # ------------------------------------------------------------------
    # 資產負債表
    # ------------------------------------------------------------------

    def balance_sheet(self, stock_id: str) -> BalanceSheet | None:
        index = self._fetch_indexed("balance_sheet")
        record = index.get(stock_id)
        if record is None:
            return None

        period = _period_from_record(record)
        if period is None:
            return None
        source = self._source_id("balance_sheet")
        url = self._endpoint("balance_sheet")
        common = dict(
            source=source,
            period=str(period),
            fetched_at=utcnow(),
            multiplier=self.multiplier,
            url=url,
            watch=self.schema_watch,
        )
        return BalanceSheet(
            period=period,
            total_assets=make_point(record, ("資產總額", "資產總計"), **common),
            total_liabilities=make_point(record, ("負債總額", "負債總計"), **common),
            total_equity=make_point(
                record, ("權益總額", "權益總計", "歸屬於母公司業主之權益合計"), **common
            ),
            current_assets=make_point(record, ("流動資產", "流動資產合計"), **common),
            current_liabilities=make_point(record, ("流動負債", "流動負債合計"), **common),
        )

    # ------------------------------------------------------------------
    # 月營收
    # ------------------------------------------------------------------

    def monthly_revenue(self, stock_id: str) -> MonthlyRevenue | None:
        index = self._fetch_indexed("monthly_revenue")
        record = index.get(stock_id)
        if record is None:
            return None

        raw_ym = record.get("資料年月") or record.get("年月")
        year, month = _parse_year_month(raw_ym)
        if year is None or month is None:
            return None

        source = self._source_id("monthly_revenue")
        common = dict(
            source=source,
            period=f"{year}-{month:02d}",
            fetched_at=utcnow(),
            url=self._endpoint("monthly_revenue"),
            watch=self.schema_watch,
        )
        return MonthlyRevenue(
            year=year,
            month=month,
            revenue=make_point(
                record,
                ("營業收入-當月營收", "當月營收"),
                **{**common, "multiplier": self.multiplier},
            ),
            yoy=make_point(
                record,
                ("營業收入-去年同月增減(%)", "去年同月增減(%)"),
                **{**common, "multiplier": 0.01},
            ),
        )

    # ------------------------------------------------------------------
    # 市場資料
    # ------------------------------------------------------------------

    def market_data(self, stock_id: str) -> MarketData:
        market = MarketData()
        today = date.today()

        try:
            valuation = self._fetch_indexed("valuation", id_fields=_ID_FIELDS)
        except SourceUnavailable:
            valuation = {}
        record = valuation.get(stock_id)
        if record:
            common = dict(
                source=self._source_id("valuation"),
                as_of=today,
                fetched_at=utcnow(),
                url=self._endpoint("valuation"),
                watch=self.schema_watch,
            )
            # 兩個交易所給同樣的三個指標，欄位名稱卻不同：
            # 證交所 BWIBBU_ALL 用 PEratio / PBratio / DividendYield，
            # 櫃買 tpex_mainboard_peratio_analysis 用
            # PriceEarningRatio / PriceBookRatio / YieldRatio。
            # 兩邊的別名都列進來，這個 client 才能同時服務上市與上櫃。
            market.pe_ratio = make_point(
                record, ("PEratio", "PriceEarningRatio", "本益比"), **common
            )
            market.pb_ratio = make_point(
                record, ("PBratio", "PriceBookRatio", "股價淨值比"), **common
            )
            # 兩邊都以百分比表示（櫃買樣本 YieldRatio="11.11"），同樣乘 0.01 轉成比率。
            market.dividend_yield = make_point(
                record,
                ("DividendYield", "YieldRatio", "殖利率(%)", "殖利率"),
                **{**common, "multiplier": 0.01},
            )

        try:
            prices = self._fetch_indexed("daily_price", id_fields=_ID_FIELDS)
        except SourceUnavailable:
            prices = {}
        price_record = prices.get(stock_id)
        if price_record:
            market.price = make_point(
                price_record,
                ("ClosingPrice", "收盤價"),
                source=self._source_id("daily_price"),
                as_of=today,
                fetched_at=utcnow(),
                url=self._endpoint("daily_price"),
                watch=self.schema_watch,
            )

        return market

    def cash_flow(self, stock_id: str) -> CashFlowStatement | None:
        """TWSE OpenAPI 未提供現金流量表；一律由 FinMind 供應。"""
        return None


# --------------------------------------------------------------------------
# 解析輔助
# --------------------------------------------------------------------------


def _period_from_record(record: dict) -> FiscalPeriod | None:
    """從 TWSE 記錄取出期別。年度欄位為民國年。"""
    raw_year = record.get("年度") or record.get("年")
    raw_quarter = record.get("季別") or record.get("季")
    year = parse_number(raw_year)
    quarter = parse_number(raw_quarter)
    if year is None:
        return None
    year_int = int(year)
    # 民國年（三位數）轉西元。
    if year_int < 1911:
        year_int += 1911
    quarter_int = int(quarter) if quarter is not None else 4
    if not 1 <= quarter_int <= 4:
        quarter_int = 4
    return FiscalPeriod(year_int, quarter_int)


def _parse_year_month(raw: object) -> tuple[int | None, int | None]:
    """解析 ``11507``（民國）或 ``202607``（西元）格式的資料年月。"""
    if raw is None:
        return None, None
    text = str(raw).strip().replace("/", "").replace("-", "")
    if not text.isdigit():
        return None, None
    if len(text) == 5:  # 民國 YYYMM
        return int(text[:3]) + 1911, int(text[3:])
    if len(text) == 6:  # 西元 YYYYMM
        return int(text[:4]), int(text[4:])
    return None, None


__all__ = ["TwseClient"]
