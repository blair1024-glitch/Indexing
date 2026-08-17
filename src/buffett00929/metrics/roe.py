"""ROE 與資本效率（規格第四節）。

規格明訂：「ROE是重要指標，但不能單獨使用」、「優先尋找長期 ROE >10~15%，
**且不是靠高槓桿支撐**」、「特別標示『高ROE但高負債』避免誤判」。

因此本模組除了算 ROE，還做杜邦拆解，把 ROE 分解為
淨利率 × 資產週轉率 × 權益乘數，讓「這個高 ROE 是怎麼來的」變成可檢查的事實。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import (
    Company,
    DataPoint,
    safe_div,
    safe_mean,
    safe_sub,
    stdev,
)


@dataclass
class DuPont:
    """杜邦拆解：ROE = 淨利率 × 資產週轉率 × 權益乘數。"""

    net_margin: DataPoint
    asset_turnover: DataPoint
    equity_multiplier: DataPoint
    roe: DataPoint

    def to_dict(self) -> dict:
        return {
            "net_margin": self.net_margin.to_dict(),
            "asset_turnover": self.asset_turnover.to_dict(),
            "equity_multiplier": self.equity_multiplier.to_dict(),
            "roe": self.roe.to_dict(),
        }


def annual_roe_series(company: Company) -> list[tuple[int, DataPoint]]:
    """逐年 ROE。

    分母採**期初期末平均股東權益**（有前一年資料時），這比只用期末權益更能
    反映當年度實際動用的資本；缺前一年時退回期末權益並照常標示來源。
    """
    balances = {b.period.year: b for b in company.annual_balances()}
    series: list[tuple[int, DataPoint]] = []

    for income in company.annual_incomes():
        year = income.period.year
        balance = balances.get(year)
        if balance is None:
            series.append((year, DataPoint.missing(f"{year} 年缺資產負債表，無法計算 ROE")))
            continue

        prior = balances.get(year - 1)
        if prior is not None and prior.total_equity.is_available and balance.total_equity.is_available:
            avg_equity = DataPoint.derived(
                (prior.total_equity.value + balance.total_equity.value) / 2,  # type: ignore[operator]
                inputs=[prior.total_equity, balance.total_equity],
            )
        else:
            avg_equity = balance.total_equity

        # 權益為負時 ROE 無意義（負負得正會產生假性高 ROE）。
        if avg_equity.is_available and avg_equity.value is not None and avg_equity.value <= 0:
            series.append((year, DataPoint.missing(f"{year} 年股東權益 ≤ 0，ROE 無意義")))
            continue

        series.append((year, safe_div(income.net_income, avg_equity, note=f"{year} 年 ROE 資料不足")))

    return series


def roe_average(company: Company, years: int) -> DataPoint:
    """近 N 年平均 ROE。資料不足 N 年則標示缺料，不用較短期間冒充。"""
    series = annual_roe_series(company)
    if len(series) < years:
        return DataPoint.missing(f"年度資料不足 {years} 年（僅 {len(series)} 年），無法計算 {years} 年平均 ROE")
    recent = [dp for _, dp in series[-years:]]
    return safe_mean(recent, note=f"近 {years} 年中有年度 ROE 無法計算")


def roe_latest(company: Company) -> DataPoint:
    series = annual_roe_series(company)
    if not series:
        return DataPoint.missing("無年度資料，無法計算 ROE")
    return series[-1][1]


def roe_stdev(company: Company, years: int = 5) -> DataPoint:
    """ROE 標準差，衡量管理階層維持穩定報酬的能力。"""
    series = annual_roe_series(company)
    if len(series) < years:
        return DataPoint.missing(f"年度資料不足 {years} 年，無法計算 ROE 波動度")
    return stdev([dp for _, dp in series[-years:]], note="部分年度 ROE 無法計算")


def roe_trend(company: Company, window: int = 3) -> DataPoint:
    """ROE 趨勢：近 N 年平均 − 前 N 年平均。正值代表資本報酬正在改善。"""
    series = annual_roe_series(company)
    if len(series) < window * 2:
        return DataPoint.missing(
            f"年度資料不足 {window * 2} 年，無法比較近 {window} 年與前 {window} 年 ROE"
        )
    recent = safe_mean([dp for _, dp in series[-window:]], note="近期 ROE 資料不足")
    earlier = safe_mean([dp for _, dp in series[-window * 2 : -window]], note="前期 ROE 資料不足")
    return safe_sub(recent, earlier, note="ROE 趨勢資料不足")


def latest_equity_multiplier(company: Company) -> DataPoint:
    """最新權益乘數。用來辨識高 ROE 是否靠槓桿堆出來的。"""
    balance = company.latest_balance
    if balance is None:
        return DataPoint.missing("無資產負債表，無法計算權益乘數")
    return balance.equity_multiplier


def dupont(company: Company) -> DuPont:
    """最新年度的杜邦拆解。"""
    income = company.latest_income
    balance = company.latest_balance
    if income is None or balance is None:
        missing = DataPoint.missing("缺損益表或資產負債表，無法進行杜邦拆解")
        return DuPont(missing, missing, missing, missing)

    net_margin = income.net_margin
    asset_turnover = safe_div(income.revenue, balance.total_assets, note="缺營收或總資產")
    equity_multiplier = balance.equity_multiplier

    if all(p.is_available for p in (net_margin, asset_turnover, equity_multiplier)):
        roe = DataPoint.derived(
            net_margin.value * asset_turnover.value * equity_multiplier.value,  # type: ignore[operator]
            inputs=[net_margin, asset_turnover, equity_multiplier],
        )
    else:
        roe = DataPoint.missing("杜邦三項有缺，無法還原 ROE")

    return DuPont(net_margin, asset_turnover, equity_multiplier, roe)


def is_high_roe_via_leverage(
    company: Company, *, roe_threshold: float, leverage_threshold: float
) -> tuple[bool, str]:
    """判定「高 ROE 但高負債」——規格第四、七節要求特別標示的情形。

    回傳 (是否觸發, 說明)。資料不足時回傳 False 並說明，
    不會因為算不出來就當作沒問題。
    """
    roe = roe_latest(company)
    multiplier = latest_equity_multiplier(company)

    if not roe.is_available or not multiplier.is_available:
        return False, "資料不足，無法判定 ROE 是否依靠高槓桿"

    if roe.value >= roe_threshold and multiplier.value >= leverage_threshold:  # type: ignore[operator]
        return True, (
            f"ROE {roe.value:.1%} 達高標，但權益乘數 {multiplier.value:.2f}x "
            f"（≥{leverage_threshold}x），報酬有相當部分來自財務槓桿而非本業獲利能力"
        )
    return False, (
        f"ROE {roe.value:.1%}、權益乘數 {multiplier.value:.2f}x，未同時觸發高 ROE 與高槓桿"
    )


__all__ = [
    "DuPont",
    "annual_roe_series",
    "dupont",
    "is_high_roe_via_leverage",
    "latest_equity_multiplier",
    "roe_average",
    "roe_latest",
    "roe_stdev",
    "roe_trend",
]
