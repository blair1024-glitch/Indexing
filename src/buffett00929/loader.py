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
from datetime import date
from pathlib import Path

from .config import Config
from .models import STAGE_ONE_GAP, Company, DataPoint, MarketData
from .normalize import (
    CumulativeDetection,
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
from .sources.mops import MopsClient, MopsHistory
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
    mops: MopsClient = field(init=False)
    history: MopsHistory | None = field(default=None, init=False)
    """MOPS 全市場歷史索引。整批抓一次，所有公司共用。"""
    warnings: list[str] = field(default_factory=list)
    _directory: dict[str, dict[str, str]] | None = field(default=None, init=False)
    """FinMind 全市場總覽（代號 → 產業別、市場別）。一次請求涵蓋整個市場。"""
    _directory_tried: bool = field(default=False, init=False)

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

        # MOPS 的回應是 1.5 MB 的 HTML，而且要連打近百次回補十年，
        # 因此給它獨立的 client：較長的逾時與自己的節流間隔，
        # 不影響 OpenAPI 那些小而快的請求。
        mops_cfg = sources.get("mops") or {}
        self.mops = MopsClient(
            http=HttpClient(
                timeout=float(mops_cfg.get("timeout_seconds", 90)),
                max_retries=int(http_cfg.get("max_retries", 3)),
                backoff=tuple(http_cfg.get("backoff_seconds", (2, 4, 8))),
                user_agent=str(http_cfg.get("user_agent", "buffett00929/0.1")),
                cache=cache,
                min_interval_seconds=float(mops_cfg.get("min_interval_seconds", 1.0)),
            ),
            config=mops_cfg,
        )

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

    def load_company(
        self, constituent: Constituent, *, history_only: bool = False
    ) -> LoadedCompany:
        company = Company(
            stock_id=constituent.stock_id,
            name=constituent.name,
            market=constituent.market,
            etf_weight=constituent.weight,
        )

        # --- 1. 多年度歷史（MOPS 彙總報表，官方）------------------------------
        from_mops = self._load_from_mops(company)

        # ``history_only`` 給全市場掃描的第一階段用。守的是 **FinMind 額度**：
        # 它逐檔查詢且有速率上限，1,975 家全部補齊要近萬次請求，
        # 漏一個逐檔呼叫進來額度就見底。
        #
        # 但**整批端點不是同一回事**。TWSE/TPEx 的每個端點都是抓整份再以代號
        # 建索引（見 twse._fetch_indexed 的 _index_cache），第一檔觸發抓取、
        # 後面 1,974 檔全是快取命中；FinMind 的股票總覽也是一次涵蓋全市場。
        # 兩者加起來約 5 次請求，就能讓全市場都有股價、本益比、淨值比、
        # 殖利率與產業別——把它們一起擋掉是白白丟資料，不是省額度。
        if history_only:
            self._apply_directory(company)
            self._overlay_market_only(company)
            company.note_gap(
                f"{STAGE_ONE_GAP}：資本支出、股利與歷史本益比未載入"
                "（這三項只能逐檔向 FinMind 查詢，僅對品質前段班補齊）。"
                "FCF、盈再率與安全邊際將標示資料不足，可評分分母因此小於 100"
            )
            return self._normalise(company, from_mops)

        # --- 2. FinMind 補 MOPS 沒有的欄位 ------------------------------------
        # 彙總報表沒有資本支出、股利、月營收與本益比序列。
        # 有 token 時由 FinMind 補；**只填空缺，不覆蓋官方數字**（規格第十九節）。
        if self.finmind.is_available:
            self._load_history(company, fill_only=from_mops)
        elif not from_mops:
            company.note_gap(
                f"多年度歷史未載入：{self.finmind.unavailable_reason}"
                "（CAGR、5/10 年平均 ROE、毛利率趨勢、歷史本益比分位將標示資料不足）"
            )
        else:
            company.note_gap(
                "資本支出、股利與歷史本益比未載入："
                f"{self.finmind.unavailable_reason}"
                "（FCF、盈再率、殖利率與歷史本益比分位將標示資料不足）"
            )

        # --- 3. 官方最新一期覆蓋同期資料（規格第十九節：官方優先）--------------
        # 必須在年度彙總之前做，否則官方數字不會進到最終結果。
        self._overlay_official(company)

        return self._normalise(company, from_mops)

    def _normalise(self, company: Company, from_mops: bool) -> LoadedCompany:
        """期別正規化（累計 → 單季 → 年度）。

        台灣財報一律累計揭露。MOPS 的年度數（season 04）本身就是年度，
        直接以年度期別存放，不會被誤當成單季。
        """
        detection = (
            CumulativeDetection(True, "high", "台灣官方財報為累計揭露（MOPS 彙總報表）")
            if from_mops
            else detect_cumulative(company.income_statements)
        )
        quarterly_incomes = to_single_quarter(company.income_statements, detection)
        company.income_statements = to_annual_incomes(company.income_statements, detection)
        company.balance_sheets = to_annual_balances(company.balance_sheets)
        company.cash_flows = to_annual_cash_flows(company.cash_flows, detection.is_cumulative)

        return LoadedCompany(
            company=company,
            detection=detection,
            quarterly_incomes=quarterly_incomes,
        )

    def _load_from_mops(self, company: Company) -> bool:
        """從已回補的 MOPS 索引取出這家公司的歷史。回傳是否真的取到資料。"""
        if self.history is None:
            return False

        incomes = self.history.income_statements(company.stock_id)
        balances = self.history.balance_sheets(company.stock_id)
        flows = self.history.cash_flow_statements(company.stock_id)

        if not (incomes or balances or flows):
            company.note_gap(
                "MOPS 彙總報表查無此公司代號"
                "（可能為興櫃或公開發行公司，未涵蓋於上市櫃彙總表）"
            )
            return False

        company.income_statements = list(incomes)
        company.balance_sheets = list(balances)
        company.cash_flows = list(flows)

        # 一般業的彙總資產負債表沒有現金欄，但現金流量表有期末餘額——
        # 同一個時點的同一個數字，回填即可，不需要額外請求。
        ending = {f.period: f.ending_cash for f in company.cash_flows}
        for sheet in company.balance_sheets:
            if sheet.cash.is_available:
                continue
            cash = ending.get(sheet.period)
            if cash is not None and cash.is_available:
                sheet.cash = cash

        return True

    def _load_history(self, company: Company, *, fill_only: bool = False) -> None:
        """由 FinMind 載入歷史。

        ``fill_only=True`` 時只補 MOPS 沒給的欄位，不覆蓋既有的官方數字——
        規格第十九節的官方優先指的是同一欄位誰說了算，
        FinMind 的角色是補上彙總報表沒有的細項（資本支出、股利、月營收）。

        合併前必須先把 FinMind 的季度序列**彙總成年度**，否則資本支出會被靜默丟棄：
        MOPS 的歷史是年度期別（來自 season 04），FinMind 是 Q1–Q4，
        兩者以期別配對永遠對不上，季度資料只會被 append；
        接著 ``to_annual_cash_flows`` 看到該年度已有年度數就整組跳過
        （見 ``normalize.py``），資本支出就此消失，而且不會有任何錯誤訊息。

        因此年度與季度**兩個層級都合併**：年度的補進 MOPS 的年度報表，
        季度的與 MOPS 近兩年的 Q1–Q3 對齊。兩者都是只填空缺。
        """
        stock_id = company.stock_id
        statements = (
            ("綜合損益表", lambda: self.finmind.income_statements(stock_id), "income_statements"),
            ("資產負債表", lambda: self.finmind.balance_sheets(stock_id), "balance_sheets"),
            ("現金流量表", lambda: self.finmind.cash_flows(stock_id), "cash_flows"),
        )
        for label, fetch, attribute in statements:
            try:
                fetched = fetch()
            except (SourceUnavailable, FetchError) as exc:
                company.note_gap(f"{label}歷史未載入：{exc}")
                continue

            if not fill_only:
                setattr(company, attribute, fetched)
                continue

            existing = getattr(company, attribute)
            for statement in list(fetched) + _annualise(attribute, fetched):
                _merge_statement(existing, statement, company, label, overwrite=False)

        # 以下三項彙總報表都沒有，無論如何都是整批取用。
        extras = (
            ("股利", lambda: self.finmind.dividends(stock_id), "dividends"),
            ("月營收", lambda: self.finmind.monthly_revenues(stock_id), "monthly_revenues"),
        )
        for label, fetch, attribute in extras:
            try:
                setattr(company, attribute, fetch())
            except (SourceUnavailable, FetchError) as exc:
                company.note_gap(f"{label}歷史未載入：{exc}")

        try:
            company.market_data.pe_history = self.finmind.pe_history(stock_id)
        except (SourceUnavailable, FetchError) as exc:
            company.note_gap(f"歷史本益比未載入：{exc}（合理本益比將改用設定值上下限）")

    def _stock_directory(self) -> dict[str, dict[str, str]]:
        """FinMind 全市場總覽，整個執行只抓一次。

        一次請求就涵蓋所有上市櫃公司的產業別與市場別，**不是逐檔查詢**，
        所以第一階段用它不會動到額度預算。
        """
        if self._directory_tried:
            return self._directory or {}
        self._directory_tried = True

        if not self.finmind.is_available:
            return {}
        try:
            self._directory = self.finmind.stock_directory()
        except (SourceUnavailable, FetchError) as exc:
            self.warnings.append(f"FinMind 股票總覽未載入，產業別將標示未分類：{exc}")
            self._directory = {}
        return self._directory or {}

    def _apply_directory(self, company: Company) -> None:
        """補上產業別與市場別。

        市場別要在抓市場資料**之前**定好：財報與行情端點分屬證交所與
        櫃買中心，選錯就整批對不到。``build_lookup_constituent`` 造出來的
        constituent 沒有市場別，掃描與個股查詢都走這條路。
        """
        entry = self._stock_directory().get(company.stock_id)
        if not entry:
            return
        if entry.get("market"):
            company.market = "TPEx" if "tpex" in entry["market"].lower() else "TWSE"
        if entry.get("industry"):
            company.industry = entry["industry"]

    def _overlay_market_only(self, company: Company) -> None:
        """只取整批端點的行情（股價、本益比、淨值比、殖利率）。

        與 ``_overlay_official`` 的差別是**刻意不做**兩件事：不合併最新一期
        財報（第一階段的排序只用彙總報表的年度數，混入最新一期沒有意義），
        也不在缺股價時退回 ``finmind.latest_price``——那是逐檔查詢，
        1,975 家跑下來就是 1,975 次請求。
        """
        client = self.tpex if company.market.upper() == "TPEX" else self.twse
        try:
            market = client.market_data(company.stock_id)
        except SourceUnavailable as exc:
            company.note_gap(f"官方市場資料未載入：{exc}")
            return
        pe_history = company.market_data.pe_history
        company.market_data = market
        company.market_data.pe_history = pe_history

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

    def classify(self, constituents: ConstituentSet) -> dict[str, str]:
        """判定每檔成分股的市場別，並回傳產業別對照表。

        持股 API 只給代號、名稱與權重——沒有市場別也沒有產業別。
        市場別非知道不可：財報端點分屬證交所與櫃買中心，選錯就整批抓不到。

        優先用 FinMind 的股票總覽（同時給市場別與產業別）；
        沒有 token 時退回「有沒有出現在證交所收盤行情清單裡」來判斷市場別，
        產業別則留白（報表顯示「未分類」，不會編一個出來）。
        """
        industries: dict[str, str] = {}

        # 走共用快取：掃描的第一階段也要這份總覽，整個執行只該抓一次。
        directory = self._stock_directory()
        if directory:
            for constituent in constituents.constituents:
                entry = directory.get(constituent.stock_id)
                if not entry:
                    continue
                if entry["market"]:
                    constituent.market = "TPEx" if "tpex" in entry["market"].lower() else "TWSE"
                if entry["industry"]:
                    industries[constituent.stock_id] = entry["industry"]
            if industries:
                return industries

        try:
            listed = self.twse._fetch_indexed("daily_price", id_fields=("Code", "公司代號"))
        except SourceUnavailable as exc:
            self.warnings.append(
                f"無法取得上市股票清單，成分股市場別一律以上市處理：{exc}"
            )
            return industries

        if listed:
            for constituent in constituents.constituents:
                constituent.market = "TWSE" if constituent.stock_id in listed else "TPEx"
        return industries

    def prefetch_history(self, today: date | None = None) -> None:
        """整批回補 MOPS 歷史，供所有公司共用。

        必須在 ``load_all`` 之前呼叫一次。以期別為單位抓取，
        一次請求涵蓋全市場——這是唯一撐得住的量級（見 ``sources/mops.py``）。
        """
        if not self.mops.is_available:
            self.warnings.append("MOPS 歷史來源已停用，多年度指標將依賴 FinMind")
            return

        try:
            self.history = self.mops.backfill(today)
        except (SourceUnavailable, FetchError) as exc:
            self.warnings.append(
                f"MOPS 歷史回補失敗：{exc}（多年度指標將標示資料不足）"
            )
            return

        self.warnings.extend(self.mops.warnings)
        if self.mops.schema_watch.has_issues:
            # 只報數量等於沒報：讀者無從判斷是業別差異還是對應寫錯，
            # 而後者曾經真的發生過（全形括號害整批欄位對不上）。
            names = sorted(
                {issue.split(":", 1)[-1].split("（", 1)[0].strip()
                 for issue in self.mops.schema_watch.unknown_fields}
            )
            self.warnings.append(
                f"MOPS 彙總報表有 {len(names)} 個欄位在部分業別版面上對不到："
                + "、".join(names)
                + "。彙總報表依業別分表，銀行業沒有營業成本、金融業不分流動與非流動，"
                "屬版面差異；是否真的影響到成分股，見下一則。"
            )


    def _report_constituent_coverage(self, loaded: list[LoadedCompany]) -> None:
        """回答「欄位對不上有沒有影響到成分股」——警告只講版面差異是不夠的。

        判斷方式是直接看核心欄位在成分股身上到底有沒有值，
        而不是從版面推論：推論會漏掉「這一檔剛好落在缺欄位的版面」那種情況。
        """
        if self.history is None:
            return

        affected: list[str] = []
        for item in loaded:
            company = item.company
            income = company.latest_annual_income
            balance = company.latest_annual_balance
            missing = (
                income is None
                or balance is None
                or not income.revenue.is_available
                or not income.net_income.is_available
                or not balance.total_assets.is_available
                or not balance.total_equity.is_available
            )
            if missing:
                affected.append(f"{company.stock_id} {company.name}")

        if affected:
            self.warnings.append(
                f"其中 {len(affected)} 檔成分股的核心欄位（營收／稅後淨利／總資產／權益）"
                f"確實缺漏：{'、'.join(affected)}"
            )
        else:
            self.warnings.append(
                "全部成分股的核心欄位（營收／稅後淨利／總資產／權益）皆完整，"
                "上述欄位差異未影響本次分析"
            )

    def load_all(self, constituents: ConstituentSet) -> list[LoadedCompany]:
        industries = self.classify(constituents)

        loaded: list[LoadedCompany] = []
        for constituent in constituents.constituents:
            try:
                item = self.load_company(constituent)
                industry = industries.get(constituent.stock_id)
                if industry:
                    item.company.industry = industry
                loaded.append(item)
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

        self._report_constituent_coverage(loaded)

        if self.finmind.unmapped_types:
            for dataset, names in self.finmind.unmapped_types.items():
                self.warnings.append(
                    f"FinMind {dataset} 有 {len(names)} 個科目未對應到欄位；"
                    "執行 `buffett00929 verify-sources` 可列出完整名稱"
                )
        return loaded


_CUMULATIVE = CumulativeDetection(True, "high", "台灣財報為累計揭露")


def _annualise(attribute: str, statements: list) -> list:
    """把季度序列彙總成年度期別，供與 MOPS 的年度報表對齊。

    直接沿用 ``normalize`` 既有的彙總規則，包含它拒絕用不完整年度冒充全年的行為
    （缺 Q4 就不產生該年度數）。台灣財報一律累計揭露，故不做偵測。
    """
    if not statements:
        return []
    if attribute == "income_statements":
        return to_annual_incomes(statements, _CUMULATIVE)
    if attribute == "balance_sheets":
        return to_annual_balances(statements)
    if attribute == "cash_flows":
        return to_annual_cash_flows(statements, True)
    return []


def build_lookup_constituent(stock_id: str, name: str | None = None) -> "Constituent":
    """把一個股號包成 ``Constituent``，供任意個股查詢使用。

    MOPS 彙總報表本來就是全市場的（本次執行涵蓋 1,975 家），
    所以分析流程對非成分股完全適用——缺的只是持股 API 才有的那三個欄位。

    權重刻意標成**缺料**而不是 0：這家公司不在 ETF 裡，
    和「在 ETF 裡但權重為零」是兩件事，報表不該把兩者顯示成同一個樣子。
    """
    from .sources.constituents import Constituent

    return Constituent(
        stock_id=stock_id,
        name=name or stock_id,
        weight=DataPoint.missing("非 00929 成分股，無 ETF 權重"),
    )



def _merge_statement(
    statements: list, incoming, company: Company, label: str, *, overwrite: bool
) -> list[str]:
    """把一筆報表逐欄位併入既有序列，回傳實際寫入的欄位名。

    逐欄位而非整筆替換：整筆替換會讓來源缺的欄位平白消失，反而讓資料變少。
    優先序指的是**同一欄位**誰說了算，不是誰整筆覆蓋誰。

    ``overwrite=True``：來源優先，有值就蓋過去（官方最新一期用）。
    ``overwrite=False``：只填空缺，不動既有值（FinMind 補 MOPS 的細項用）。
    """
    if incoming is None:
        return []

    existing = next((s for s in statements if s.period == incoming.period), None)
    if existing is None:
        statements.append(incoming)
        statements.sort(key=lambda s: s.period)
        return ["（整期新增）"]

    written: list[str] = []
    for field_name, value in vars(incoming).items():
        if field_name == "period" or not hasattr(value, "is_available"):
            continue
        if not value.is_available:
            continue
        if not overwrite and getattr(existing, field_name).is_available:
            continue
        setattr(existing, field_name, value)
        written.append(field_name)
    return written


def _merge_official(statements: list, official, company: Company, label: str) -> None:
    """把官方最新一期併入既有序列（官方優先，逐欄位覆蓋）。"""
    replaced = _merge_statement(statements, official, company, label, overwrite=True)
    if replaced and replaced != ["（整期新增）"]:
        company.note_gap(
            f"{label} {official.period} 已以官方數字覆蓋 {len(replaced)} 個欄位（官方優先）"
        )


__all__ = ["DataLoader", "LoadedCompany"]
