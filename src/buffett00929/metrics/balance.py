"""財務結構（規格第七節）。

規格特別強調：「ROE高，但是因為負債很高而造成的公司，這類公司不能直接給高分。」
高 ROE 的成因判定在 ``roe.py``；本模組負責負債水準與償債能力本身。
"""

from __future__ import annotations

from ..models import Company, DataPoint, safe_div


def debt_ratio(company: Company) -> DataPoint:
    balance = company.latest_annual_balance
    if balance is None:
        return DataPoint.missing("無資產負債表，無法計算負債比")
    return balance.debt_ratio


def debt_to_equity(company: Company) -> DataPoint:
    balance = company.latest_annual_balance
    if balance is None:
        return DataPoint.missing("無資產負債表，無法計算負債權益比")
    return balance.debt_to_equity


def current_ratio(company: Company) -> DataPoint:
    balance = company.latest_annual_balance
    if balance is None:
        return DataPoint.missing("無資產負債表，無法計算流動比率")
    return balance.current_ratio


def net_debt(company: Company) -> DataPoint:
    balance = company.latest_annual_balance
    if balance is None:
        return DataPoint.missing("無資產負債表，無法計算淨負債")
    return balance.net_debt


def interest_coverage(company: Company) -> DataPoint:
    income = company.latest_annual_income
    if income is None:
        return DataPoint.missing("無損益表，無法計算利息保障倍數")
    return income.interest_coverage


def debt_to_fcf(company: Company) -> DataPoint:
    """有息負債 ÷ 自由現金流：大約幾年的自由現金流可以還清有息負債。

    FCF ≤ 0 時「幾年可還清」沒有意義，回傳缺料並說明；
    評分時該指標不計分，另由紅旗系統標記 FCF 為負的問題。
    """
    balance = company.latest_annual_balance
    flow = company.latest_annual_cash_flow
    if balance is None or flow is None:
        return DataPoint.missing("缺資產負債表或現金流量表，無法計算負債／FCF")

    fcf = flow.free_cash_flow
    if fcf.is_available and fcf.value is not None and fcf.value <= 0:
        return DataPoint.missing("自由現金流 ≤ 0，無法以 FCF 衡量償債年限")
    return safe_div(balance.interest_bearing_debt, fcf, note="缺有息負債或自由現金流")


def debt_growth(company: Company) -> DataPoint:
    """有息負債年增率，用於偵測「負債快速增加」紅旗。"""
    balances = company.annual_balances()
    if len(balances) < 2:
        return DataPoint.missing("年度資料不足 2 年，無法計算負債成長率")

    prev = balances[-2].interest_bearing_debt
    curr = balances[-1].interest_bearing_debt
    if not prev.is_available or not curr.is_available:
        return DataPoint.missing("缺有息負債資料，無法計算負債成長率")
    if prev.value in (None, 0):
        # 由零負債轉為有負債，用成長率表達會變成無限大，改以缺料標示並讓紅旗系統另行判斷。
        return DataPoint.missing("前期有息負債為 0，成長率無定義")
    return DataPoint.derived(curr.value / prev.value - 1, inputs=[prev, curr])  # type: ignore[operator]


def _share_count_basis(point: DataPoint) -> str:
    """股數是用哪一條路徑算出來的。跨路徑的兩個數字不能相除。"""
    if point.provenance is None:
        return "?"
    return point.provenance.source


def _longest_single_basis_run(balances: list) -> list:
    """取序列中最長的同基準連續區段。

    股數有四條推算路徑（股本÷面額、權益÷每股淨值、淨利÷EPS、FinMind 股本），
    對同一家公司可能差 20% 以上。序列中途換路徑，年化成長率就會把換尺
    算成稀釋——實測 8422 +79.5%、6548 −55.1%、8070 −42.9%，
    每一個都足以觸發「股本快速膨脹」重大紅旗，而公司什麼都沒做。

    與 ``snapshots.py`` 對總分的處理同一條原則：衡量基準變了就不能直接比。
    但也不必整項放棄——同基準的區段仍然是可信的，取最長的那一段。
    """
    best: list = []
    current: list = []
    for sheet in balances:
        basis = _share_count_basis(sheet.shares_outstanding)
        if current and _share_count_basis(current[-1].shares_outstanding) != basis:
            current = []
        current.append(sheet)
        if len(current) > len(best):
            best = list(current)
    return best


def share_count_growth(company: Company, years: int = 5) -> DataPoint:
    """在外流通股數年化成長率——衡量股本稀釋（規格第八節、第十六節）。"""
    available = [b for b in company.annual_balances() if b.shares_outstanding.is_available]
    if len(available) < 2:
        return DataPoint.missing("股數資料不足 2 年，無法計算股本稀釋率")

    balances = _longest_single_basis_run(available)
    if len(balances) < 2:
        return DataPoint.missing(
            "股數序列每一期的推算基準都不同（股本÷面額、權益÷每股淨值、淨利÷EPS "
            "各有不同結果），跨基準相除只會量到換尺的幅度，不計股本稀釋率"
        )

    window = balances[-years:] if len(balances) >= years else balances
    first, last = window[0].shares_outstanding, window[-1].shares_outstanding
    span = window[-1].period.year - window[0].period.year
    if span <= 0:
        return DataPoint.missing("期間長度為 0，無法年化股本變動")
    if first.value in (None, 0) or last.value is None:
        return DataPoint.missing("股數為 0 或缺漏，無法計算稀釋率")

    annualized = (last.value / first.value) ** (1 / span) - 1  # type: ignore[operator]
    return DataPoint.derived(annualized, inputs=[first, last])


def inventory_growth_vs_revenue(company: Company) -> tuple[DataPoint, DataPoint]:
    """回傳 (存貨成長率, 營收成長率)，供紅旗系統比較。"""
    balances = company.annual_balances()
    incomes = company.annual_incomes()
    if len(balances) < 2 or len(incomes) < 2:
        missing = DataPoint.missing("年度資料不足 2 年，無法比較存貨與營收成長")
        return missing, missing
    return (
        _growth(balances[-2].inventory, balances[-1].inventory, "存貨"),
        _growth(incomes[-2].revenue, incomes[-1].revenue, "營收"),
    )


def receivables_growth_vs_revenue(company: Company) -> tuple[DataPoint, DataPoint]:
    balances = company.annual_balances()
    incomes = company.annual_incomes()
    if len(balances) < 2 or len(incomes) < 2:
        missing = DataPoint.missing("年度資料不足 2 年，無法比較應收帳款與營收成長")
        return missing, missing
    return (
        _growth(balances[-2].receivables, balances[-1].receivables, "應收帳款"),
        _growth(incomes[-2].revenue, incomes[-1].revenue, "營收"),
    )


def _growth(previous: DataPoint, current: DataPoint, label: str) -> DataPoint:
    if not previous.is_available or not current.is_available:
        return DataPoint.missing(f"缺{label}資料，無法計算成長率")
    if previous.value in (None, 0):
        return DataPoint.missing(f"前期{label}為 0，成長率無定義")
    return DataPoint.derived(current.value / previous.value - 1, inputs=[previous, current])  # type: ignore[operator]


__all__ = [
    "current_ratio",
    "debt_growth",
    "debt_ratio",
    "debt_to_equity",
    "debt_to_fcf",
    "interest_coverage",
    "inventory_growth_vs_revenue",
    "net_debt",
    "receivables_growth_vs_revenue",
    "share_count_growth",
]
