"""內在價值估算與安全邊際（規格第三節 M3）。

規格的核心要求：**好公司 ≠ 好股票價格**，且「至少使用 2 種以上估值方法交叉驗證」。
本模組實作四種方法，取**可得方法的中位數**為內在價值（中位數比平均值更耐離群值），
並回報各方法的離散度。

**可用方法少於 2 種時不給安全邊際分數**——寧可標示資料不足，
也不要用單一方法算出的價格當成「交叉驗證過」的結論。

台股特有細節：財報的「股本」是**金額**不是股數，兩者差一個面額。
換算集中在來源層（``sources/base.shares_from_share_capital``），
``BalanceSheet.shares_outstanding`` 拿到手時**已經是股數**。
這裡若再除一次面額，每股價值會憑空變成十倍——實際發生過：
DCF 一度算出 13,921 元，而同一家公司的其他三種估值法都在 577～1,443 之間。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..metrics import stability
from ..models import Company, DataPoint

"""台股普通股面額，元。用於由股本換算股數。"""


@dataclass
class ValuationMethod:
    """單一估值方法的結果。"""

    key: str
    label: str
    value_per_share: DataPoint
    assumptions: str = ""

    @property
    def is_available(self) -> bool:
        return self.value_per_share.is_available

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value_per_share": self.value_per_share.to_dict(),
            "assumptions": self.assumptions,
        }


@dataclass
class Valuation:
    """交叉驗證後的估值結論。"""

    methods: list[ValuationMethod] = field(default_factory=list)
    intrinsic_value: DataPoint = field(default_factory=lambda: DataPoint.missing("未估算"))
    price: DataPoint = field(default_factory=lambda: DataPoint.missing("未取得股價"))
    margin_of_safety: DataPoint = field(default_factory=lambda: DataPoint.missing("未計算"))
    dispersion: DataPoint = field(default_factory=lambda: DataPoint.missing("未計算"))
    dispersion_warning: bool = False
    note: str = ""

    @property
    def available_methods(self) -> list[ValuationMethod]:
        return [m for m in self.methods if m.is_available]

    @property
    def classification(self) -> str:
        """規格第三節的安全邊際分級。"""
        if not self.margin_of_safety.is_available or self.margin_of_safety.value is None:
            return "資料不足"
        mos = self.margin_of_safety.value
        if mos > 0.30:
            return "🟢 非常便宜"
        if mos >= 0.20:
            return "🟢 便宜"
        if mos >= 0.10:
            return "🟡 合理偏便宜"
        if mos >= 0:
            return "🟡 合理"
        return "🔴 高估"

    def to_dict(self) -> dict:
        return {
            "methods": [m.to_dict() for m in self.methods],
            "intrinsic_value": self.intrinsic_value.to_dict(),
            "price": self.price.to_dict(),
            "margin_of_safety": self.margin_of_safety.to_dict(),
            "classification": self.classification,
            "dispersion": self.dispersion.to_dict(),
            "dispersion_warning": self.dispersion_warning,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# 輔助
# --------------------------------------------------------------------------


SHARE_COUNT_TOLERANCE = 0.05
"""股數與「權益 ÷ 每股淨值」的容忍差距。每股淨值本身有四捨五入，不能抓太緊。"""


def share_count(company: Company, config: dict | None = None) -> DataPoint:
    """最新年度的在外流通股數，並以每股淨值交叉驗算。

    股本→股數的換算已在來源層完成（見模組說明），這裡直接取用。

    但**取用前必須驗算**。``sources/mops.py`` 對 MOPS 的股本做了
    「股本 ÷ 面額」對「權益 ÷ 每股淨值」的交叉檢查，FinMind 那條路徑卻沒有；
    而 FinMind 缺料或數字對不上時，這裡拿到的就是一個沒人驗過的股數。
    股數錯不會讓任何一步報錯，只會讓 DCF 的每股價值等比例放大——
    長華電材（8070）因此算出每股 368 元、股價 49.5 元、安全邊際 +76%，
    登上「最便宜」榜首；反推它隱含的每股自由現金流是 29.3 元，
    也就是 59% 的自由現金流殖利率，而同一份報表的現金股利只有 2.46 元。
    兩者不可能同時為真——差的就是股數。

    ``權益 ÷ 每股淨值``這條路徑與面額、與資料來源都無關，是獨立的第二意見。
    兩者對不上時回缺料：寧可讓 DCF 消失，也不要發布一個等比例錯誤的內在價值。
    """
    balance = company.latest_annual_balance
    if balance is None or not balance.shares_outstanding.is_available:
        return DataPoint.missing("缺在外流通股數，無法計算每股價值")
    shares = balance.shares_outstanding
    if shares.value is None or shares.value <= 0:
        return DataPoint.missing("在外流通股數 ≤ 0，無法計算每股價值")

    tolerance = float((config or {}).get("share_count_tolerance", SHARE_COUNT_TOLERANCE))
    equity = balance.total_equity
    bvps = balance.book_value_per_share
    if (
        equity.is_available
        and equity.value is not None
        and bvps.is_available
        and bvps.value not in (None, 0)
    ):
        by_book = equity.value / bvps.value  # type: ignore[operator]
        if by_book > 0 and abs(shares.value - by_book) / by_book > tolerance:
            return DataPoint.missing(
                f"在外流通股數 {shares.value:,.0f} 股，"
                f"但權益 ÷ 每股淨值推得 {by_book:,.0f} 股，"
                f"差距 {abs(shares.value - by_book) / by_book:.0%} 超過容忍值 "
                f"{tolerance:.0%}，股數與財報不在同一基準，不用於每股估值"
            )

    return shares


def normalized_eps(company: Company, years: int) -> DataPoint:
    """正常化 EPS：近 N 年 EPS 平均，抹平景氣循環的高低點。"""
    incomes = company.trailing_annual_incomes(years)
    if len(incomes) < years:
        return DataPoint.missing(
            f"年度資料不足 {years} 年（僅 {len(incomes)} 年），無法計算正常化 EPS"
        )
    eps_points = [s.eps for s in incomes]
    missing = [p for p in eps_points if not p.is_available]
    if missing:
        return DataPoint.missing("部分年度 EPS 缺漏，無法計算正常化 EPS")
    values = [p.value for p in eps_points]
    return DataPoint.derived(sum(values) / len(values), inputs=eps_points)  # type: ignore[arg-type]


def justified_pe(company: Company, config: dict) -> DataPoint:
    """合理本益比：歷史本益比的指定分位數，並夾在設定的上下限之間。

    夾住上下限是為了避免兩種失真：泡沫期的歷史高本益比被當成合理值，
    以及長期低估的公司被永遠鎖在低本益比。
    """
    history = company.market_data.pe_history
    floor = float(config.get("pe_band_floor", 8.0))
    cap = float(config.get("pe_band_cap", 30.0))
    percentile = float(config.get("justified_pe_percentile", 0.5))

    if not history:
        return DataPoint.missing("無歷史本益比資料，無法推估合理本益比")

    ordered = sorted(history)
    index = min(int(len(ordered) * percentile), len(ordered) - 1)
    raw = ordered[index]
    clamped = max(floor, min(cap, raw))
    return DataPoint.of(
        clamped,
        "derived(FinMind:TaiwanStockPER)",
        period=f"{len(ordered)} 筆歷史本益比之第 {percentile:.0%} 分位"
        + ("" if raw == clamped else f"（原值 {raw:.1f} 倍，已夾至 {floor:.0f}~{cap:.0f} 倍）"),
    )


# --------------------------------------------------------------------------
# 四種估值方法
# --------------------------------------------------------------------------



def _normalization_is_contradicted(
    latest: float | None, average: float, config: dict, *, eps_cagr: DataPoint | None = None
) -> str | None:
    """正常化的前提被趨勢否定時，回傳原因；否則回傳 None。

    取平均是為了抹平景氣循環——前提是會回到平均。結構性衰退不是循環，
    平均值代表的是公司**已經失去**的獲利能力，用它估值會得出「非常便宜」，
    而那個便宜是拿不回來的東西換算出來的。

    寧可標示不可靠，也不要給一個看起來很有信心的內在價值：
    規格第二節要的是「資料不足就說資料不足」，不是硬算一個數字。
    """
    guard = config.get("normalization_guard") or {}
    min_ratio = float(guard.get("min_latest_to_average", 0.5))
    min_cagr = float(guard.get("min_eps_cagr", -0.15))

    if latest is not None and average > 0 and latest / average < min_ratio:
        return (
            f"最新一年僅為所用平均的 {latest / average:.0%}"
            f"（低於 {min_ratio:.0%} 門檻），平均值已不代表當前獲利能力"
        )

    if eps_cagr is not None and eps_cagr.is_available and eps_cagr.value is not None:
        if eps_cagr.value < min_cagr:
            return (
                f"EPS 年複合成長率 {eps_cagr.value:.1%}"
                f"（低於 {min_cagr:.0%} 門檻），屬結構性衰退而非景氣循環"
            )
    return None


def _method_normalized_pe(company: Company, config: dict) -> ValuationMethod:
    years = int(config.get("normalized_eps_years", 5))
    eps = normalized_eps(company, years)
    pe = justified_pe(company, config)

    if not eps.is_available or not pe.is_available:
        reason = eps.unavailable_reason if not eps.is_available else pe.unavailable_reason
        return ValuationMethod(
            key="normalized_pe",
            label="正常化 EPS × 合理本益比",
            value_per_share=DataPoint.missing(reason or "資料不足"),
        )

    incomes = company.trailing_annual_incomes(years)
    latest_eps = incomes[-1].eps.value if incomes and incomes[-1].eps.is_available else None
    contradiction = _normalization_is_contradicted(
        latest_eps,
        eps.value,  # type: ignore[arg-type]
        config,
        eps_cagr=stability.eps_cagr(company, years),
    )
    if contradiction:
        return ValuationMethod(
            key="normalized_pe",
            label="正常化 EPS × 合理本益比",
            value_per_share=DataPoint.missing(f"正常化前提不成立：{contradiction}"),
        )

    value = eps.value * pe.value  # type: ignore[operator]
    return ValuationMethod(
        key="normalized_pe",
        label="正常化 EPS × 合理本益比",
        value_per_share=DataPoint.derived(value, inputs=[eps, pe]),
        assumptions=f"{years} 年平均 EPS {eps.value:.2f} 元 × 合理本益比 {pe.value:.1f} 倍",
    )


def _method_dividend_yield(company: Company, config: dict) -> ValuationMethod:
    """殖利率法：內在價值 = 每股現金股利 ÷ 要求殖利率。

    00929 成分股皆為配息穩定的公司，此法最貼近這類標的的實際定價邏輯。
    """
    required = float(config.get("required_yield", 0.05))
    records = [d for d in company.dividends if d.cash_dividend.is_available]
    if not records:
        return ValuationMethod(
            key="dividend_yield",
            label="殖利率法",
            value_per_share=DataPoint.missing("無現金股利資料，無法以殖利率法估值"),
        )

    # 取近三年平均股利，避免單一年度的特別股利把估值墊高。
    window = records[-3:]
    values = [d.cash_dividend.value for d in window if d.cash_dividend.value is not None]
    if not values:
        return ValuationMethod(
            key="dividend_yield",
            label="殖利率法",
            value_per_share=DataPoint.missing("現金股利資料缺漏"),
        )

    avg_dividend = sum(values) / len(values)
    if avg_dividend <= 0:
        return ValuationMethod(
            key="dividend_yield",
            label="殖利率法",
            value_per_share=DataPoint.missing("平均現金股利為 0，不適用殖利率法"),
        )

    latest_dividend = values[-1]
    contradiction = _normalization_is_contradicted(latest_dividend, avg_dividend, config)
    if contradiction:
        return ValuationMethod(
            key="dividend_yield",
            label="殖利率法",
            value_per_share=DataPoint.missing(f"正常化前提不成立：{contradiction}"),
        )

    value = avg_dividend / required
    return ValuationMethod(
        key="dividend_yield",
        label="殖利率法",
        value_per_share=DataPoint.derived(value, inputs=[d.cash_dividend for d in window]),
        assumptions=(
            f"近 {len(window)} 年平均現金股利 {avg_dividend:.2f} 元 ÷ 要求殖利率 {required:.1%}"
        ),
    )


def _method_dcf(company: Company, config: dict) -> ValuationMethod:
    """自由現金流折現。"""
    dcf_config = config.get("dcf") or {}
    years = int(dcf_config.get("projection_years", 10))
    discount = float(dcf_config.get("discount_rate", 0.09))
    terminal_growth = float(dcf_config.get("terminal_growth", 0.02))
    max_growth = float(dcf_config.get("max_growth_rate", 0.15))

    flows = company.annual_cash_flows()
    if not flows:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing("無現金流量表，無法進行 DCF"),
        )

    # 用近三年平均 FCF 當基期，單一年度的資本支出高峰不該決定整個估值。
    recent = [f.free_cash_flow for f in flows[-3:] if f.free_cash_flow.is_available]
    if not recent:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing("近年自由現金流缺漏，無法進行 DCF"),
        )

    base_fcf = sum(f.value for f in recent) / len(recent)  # type: ignore[misc]
    if base_fcf <= 0:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing("基期自由現金流 ≤ 0，DCF 無意義"),
        )

    shares = share_count(company, config)
    if not shares.is_available or shares.value is None:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing(shares.unavailable_reason or "缺股數，無法計算每股價值"),
        )
    balance = company.latest_annual_balance
    share_period = str(balance.period) if balance is not None else "期別不明"

    growth_point = stability.eps_cagr(company)
    growth = 0.0
    if growth_point.is_available and growth_point.value is not None:
        growth = max(0.0, min(growth_point.value, max_growth))

    if discount <= terminal_growth:
        return ValuationMethod(
            key="dcf",
            label="自由現金流折現（DCF）",
            value_per_share=DataPoint.missing("折現率須大於永續成長率，設定有誤"),
        )

    present_value = 0.0
    projected = base_fcf
    for year in range(1, years + 1):
        projected *= 1 + growth
        present_value += projected / ((1 + discount) ** year)

    terminal_value = projected * (1 + terminal_growth) / (discount - terminal_growth)
    present_value += terminal_value / ((1 + discount) ** years)

    return ValuationMethod(
        key="dcf",
        label="自由現金流折現（DCF）",
        value_per_share=DataPoint.derived(present_value / shares.value, inputs=[*recent, shares]),
        assumptions=(
            f"基期 FCF {base_fcf / 1e8:.1f} 億元、成長 {growth:.1%}／年（{years} 年）、"
            f"折現率 {discount:.1%}、永續成長 {terminal_growth:.1%}、"
            f"股數 {shares.value / 1e8:.2f} 億股（{share_period}）"
        ),
    )


def _method_peg(company: Company, config: dict) -> ValuationMethod:
    """PEG＝1 隱含的合理本益比（合理 PE ≈ 成長率百分比）。"""
    years = int(config.get("normalized_eps_years", 5))
    eps = normalized_eps(company, years)
    growth = stability.eps_cagr(company)

    if not eps.is_available or not growth.is_available or growth.value is None:
        reason = (
            eps.unavailable_reason
            if not eps.is_available
            else growth.unavailable_reason or "缺成長率"
        )
        return ValuationMethod(
            key="peg",
            label="PEG＝1 隱含合理本益比",
            value_per_share=DataPoint.missing(reason or "資料不足"),
        )

    if growth.value <= 0:
        return ValuationMethod(
            key="peg",
            label="PEG＝1 隱含合理本益比",
            value_per_share=DataPoint.missing("EPS 成長率 ≤ 0，PEG 法不適用"),
        )

    floor = float(config.get("pe_band_floor", 8.0))
    cap = float(config.get("pe_band_cap", 30.0))
    implied_pe = growth.value * 100

    # 這個方法的主張是「合理本益比 ≈ 成長率」。區間夾擠一旦接管，
    # 算出來的就不是 PEG＝1 隱含的本益比，而是區間端點本身——
    # 對成長 2% 的公司，「合理本益比 8 倍」與這家公司無關。
    #
    # 讓它照樣投票的代價實測過（2026-08-21，00929 五十檔）：PEG 可用 27 檔，
    # 其中 15 檔被下限夾住、2 檔被上限夾住，真正落在區間內只有 10 檔；
    # 而有 3 種以上方法的公司裡，離中位數最遠的**有 67% 是 PEG**。
    # 中華電、台灣大、遠傳的分歧度都卡在 62~63%，拿掉 PEG 之後掉到 6~10%——
    # 一個固定倍數冒充獨立的第三意見，把三檔體質最好的公司推出了估值範圍。
    #
    # 上下限一視同仁：成長 40% 卻用 30 倍，算的是 PEG＝0.75 不是 PEG＝1，
    # 那個名字會說謊。寧可讓這個方法沉默，也不要它假裝有意見。
    if implied_pe < floor or implied_pe > cap:
        return ValuationMethod(
            key="peg",
            label="PEG＝1 隱含合理本益比",
            value_per_share=DataPoint.missing(
                f"EPS 成長 {growth.value:.1%} 推得合理本益比 {implied_pe:.1f} 倍，"
                f"落在 {floor:.0f}~{cap:.0f} 倍區間之外；"
                "夾擠後的倍數由區間端點決定而非由這家公司決定，PEG 法不表態"
            ),
        )

    fair_pe = implied_pe
    return ValuationMethod(
        key="peg",
        label="PEG＝1 隱含合理本益比",
        value_per_share=DataPoint.derived(eps.value * fair_pe, inputs=[eps, growth]),  # type: ignore[operator]
        assumptions=(
            f"EPS 成長 {growth.value:.1%} → 合理本益比 {fair_pe:.1f} 倍"
            f"（落於 {floor:.0f}~{cap:.0f} 倍區間內），× 正常化 EPS {eps.value:.2f} 元"
        ),
    )


# --------------------------------------------------------------------------
# 交叉驗證
# --------------------------------------------------------------------------


def estimate_valuation(company: Company, scoring_config: dict) -> Valuation:
    """執行四種估值方法並交叉驗證。"""
    mos_config = scoring_config.get("margin_of_safety") or {}
    config = mos_config.get("valuation") or {}
    min_methods = int(mos_config.get("min_methods_required", 2))

    methods = [
        _method_normalized_pe(company, config),
        _method_dividend_yield(company, config),
        _method_dcf(company, config),
        _method_peg(company, config),
    ]
    valuation = Valuation(methods=methods, price=company.market_data.price)

    available = [m for m in methods if m.is_available]
    if len(available) < min_methods:
        valuation.note = (
            f"僅 {len(available)} 種估值方法可用，未達交叉驗證所需的 {min_methods} 種，"
            "不計安全邊際分數（規格第三節要求至少 2 種方法交叉驗證）"
        )
        valuation.intrinsic_value = DataPoint.missing(valuation.note)
        valuation.margin_of_safety = DataPoint.missing(valuation.note)
        return valuation

    values = [m.value_per_share.value for m in available]
    median_value = statistics.median(values)  # type: ignore[arg-type]

    # 離散度：各方法相對中位數的最大偏離幅度。分歧過大代表估值本身不可靠。
    spread: float | None = None
    if median_value > 0:
        spread = max(abs(v - median_value) / median_value for v in values)  # type: ignore[operator]
        valuation.dispersion = DataPoint.derived(spread, inputs=[])
        warn_at = float(config.get("dispersion_warning", 0.40))
        valuation.dispersion_warning = spread > warn_at

    # 規格第三節要的是「至少 2 種方法**交叉驗證**」。兩種方法差 7 倍不是交叉驗證，
    # 是把一個答案和一個非答案取平均——中位數落在兩者之間，看起來很有信心，
    # 實際上沒有任何一種方法支持它。實例：長華電材（8070）殖利率法 49.1 元、
    # DCF 368.1 元，中位數 208.6 元對上 49.5 元股價，得出 +76% 安全邊際、
    # 滿分 10/10、「最便宜」榜首；分歧 76% 當時只是一句沒人讀的註解。
    reject_at = float(config.get("dispersion_reject", 0.60))
    if spread is not None and spread > reject_at:
        valuation.note = (
            f"{len(available)} 種估值方法彼此分歧達 {spread:.0%}"
            f"（超過 {reject_at:.0%} 門檻），未構成交叉驗證，"
            "不計安全邊際分數（規格第三節要求 2 種方法交叉驗證）"
        )
        valuation.intrinsic_value = DataPoint.missing(valuation.note)
        valuation.margin_of_safety = DataPoint.missing(valuation.note)
        return valuation

    valuation.intrinsic_value = DataPoint.derived(
        median_value, inputs=[m.value_per_share for m in available]
    )

    price = company.market_data.price
    # 股價 ≤ 0 不是股價，是缺料。實測（2026-08-22 全市場掃描）雙美（4728）
    # 回傳 0.0 元，內在價值 300.1 元，安全邊際就成了（300.1−0）÷300.1＝100%，
    # 直接登上安全邊際榜首。把零當數據而不是當缺漏，是這個專案一路在防的錯誤。
    if not price.is_available or price.value is None or price.value <= 0:
        reason = (
            "股價為 0 或負值，視同未取得，無法計算安全邊際"
            if price.is_available and price.value is not None
            else "缺股價，無法計算安全邊際"
        )
        valuation.margin_of_safety = DataPoint.missing(reason)
        valuation.note = f"已估算內在價值，但{reason}"
        return valuation

    if median_value <= 0:
        valuation.margin_of_safety = DataPoint.missing("內在價值 ≤ 0，安全邊際無意義")
        return valuation

    mos = (median_value - price.value) / median_value
    valuation.margin_of_safety = DataPoint.derived(
        mos, inputs=[valuation.intrinsic_value, price]
    )
    valuation.note = (
        f"{len(available)} 種方法交叉驗證，中位數內在價值 {median_value:.1f} 元"
        f"，現價 {price.value:.1f} 元"
    )
    if valuation.dispersion_warning and valuation.dispersion.value is not None:
        valuation.note += f"；⚠ 各方法估值分歧達 {valuation.dispersion.value:.0%}，估值可靠度偏低"

    return valuation


__all__ = [
    "Valuation",
    "ValuationMethod",
    "estimate_valuation",
    "justified_pe",
    "normalized_eps",
    "share_count",
]
