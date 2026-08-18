#!/usr/bin/env python3
"""驗證 ``sources/mops.py`` 的解析器對得上 MOPS 彙總報表的真實版面。

**一次性診斷工具，不是正式流程的一部分。**

## 前五輪找到的路

1. ``mops.twse.com.tw`` 的舊路徑只回 65 bytes 轉址殘骸，``/server-java/*`` 全 404。
2. 新版是 SPA，資料走 ``POST /mops/api/<group>/<code>``、body 為 JSON。
3. 從 ``/mops/assets/index.js`` 挖出 397 個報表代碼與中文名稱，
   並發現舊網域 ``mopsov.twse.com.tw`` 還活著。
4. 報表 chunk（``/mops/assets/t163sb04.js``）裡有完整查詢定義：
   ``TYPEK`` / ``year``（民國年）/ ``season``（01-04），``POST ajax_t163sb04``。
   實測回傳 1,578,302 bytes 的「上市公司第一季資料」。
5. 結構與範圍實測：一頁 **7 個表格**（分業別）；``season=04`` 給第四季/年度數；
   ``TYPEK=otc`` 給上櫃；民國 105 年（十年前）仍查得到；
   連打 6 次沒有被擋或降速（1.7–15.9s，回應大小一致）。

## 本輪的任務

第五輪印不出表頭，是探測程式自己的兩個錯：用 ``<table>…</table>`` 切巢狀表格
會提早截斷，而 ``apparent_encoding`` 猜錯編碼讓中文變亂碼（所以「有資料=False」）。
兩者都已在 ``sources/mops.py`` 修正——改為掃描整份文件的 ``<tr>``、編碼固定 UTF-8。

因此本輪不再是拋棄式腳本，而是**直接跑正式解析器**，檢查：

* 真實表頭長什麼樣（逐表印出，這是欄位對應的依據）
* 對應失敗的欄位有哪些（``SchemaWatch`` 會吵）
* 00929 成分股實際解析出來的數字對不對
* 揭露單位是否確為仟元
* 快取大小，決定歷史要不要進版控

用法（須在網路可通的環境執行，例如 GitHub Actions）：

    python scripts/probe_mops.py
"""

from __future__ import annotations

import re
import sys
from datetime import date

from buffett00929.models import FiscalPeriod
from buffett00929.sources.base import HttpClient
from buffett00929.sources.cache import DiskCache
from buffett00929.sources.mops import REPORTS, MopsClient, scan_rows

TODAY = date(2026, 8, 17)
PERIOD = FiscalPeriod(2025, 1)

# 00929 的實際成分股，用來檢查解析結果是否合理。
SAMPLE = ["2330", "2317", "2454", "3711", "2382"]


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def show_headers(html: str) -> None:
    """列出頁面裡所有不同的表頭——這就是欄位對應的依據。"""
    seen: list[tuple[str, ...]] = []
    counts: dict[tuple[str, ...], int] = {}
    for header, _row in scan_rows(html):
        key = tuple(header)
        if key not in counts:
            seen.append(key)
        counts[key] = counts.get(key, 0) + 1

    print(f"  不同表頭：{len(seen)} 種")
    for index, header in enumerate(seen, start=1):
        print(f"\n  ── 表頭 {index}（{counts[header]} 家公司，{len(header)} 欄）──")
        for position, name in enumerate(header):
            print(f"     [{position:>2}] {name}")


def show_units(html: str) -> None:
    hits = set(re.findall(r"單位[：:][^<，,。]{0,20}", html))
    print(f"  單位宣告：{sorted(hits) if hits else '（頁面未宣告）'}")


def money(point) -> str:
    if not point.is_available:
        return f"資料不足（{point.unavailable_reason}）"
    value = point.value
    if abs(value) >= 1e8:
        return f"{value / 1e8:,.2f} 億"
    return f"{value:,.2f}"


def main() -> int:
    cache = DiskCache("data/raw/probe", ttl_hours=12)
    http = HttpClient(timeout=90, cache=cache, min_interval_seconds=1.0)
    client = MopsClient(http=http, config={})

    parsed_by_kind = {}

    for kind, (code, label) in REPORTS.items():
        rule(f"{code} {label} — 民國 114 年第一季，上市")
        try:
            html = client.fetch_report(code, "sii", PERIOD, today=TODAY)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ 抓取失敗：{type(exc).__name__}: {exc}")
            continue

        print(f"  回應 {len(html):,} 字元")
        show_units(html)
        show_headers(html)

        parser = {
            "income": client.parse_income,
            "balance": client.parse_balance,
            "cashflow": client.parse_cashflow,
        }[kind]
        parsed = parser(html, PERIOD, as_of=TODAY)
        parsed_by_kind[kind] = parsed
        print(f"\n  解析出 {len(parsed)} 家公司")

    rule("00929 成分股的解析結果（抽樣驗算）")
    for stock_id in SAMPLE:
        print(f"\n── {stock_id} ──")
        income = (parsed_by_kind.get("income") or {}).get(stock_id)
        balance = (parsed_by_kind.get("balance") or {}).get(stock_id)
        cashflow = (parsed_by_kind.get("cashflow") or {}).get(stock_id)

        if income:
            print(f"  營業收入    {money(income.revenue)}")
            print(f"  營業利益    {money(income.operating_income)}")
            print(f"  稅後淨利    {money(income.net_income)}")
            print(f"  每股盈餘    {money(income.eps)}")
            print(f"  毛利率      {income.gross_margin.value if income.gross_margin.is_available else '資料不足'}")
        else:
            print("  綜合損益表：未解析到這家公司")

        if balance:
            print(f"  資產總額    {money(balance.total_assets)}")
            print(f"  權益總額    {money(balance.total_equity)}")
            print(f"  負債比      {balance.debt_ratio.value if balance.debt_ratio.is_available else '資料不足'}")
        else:
            print("  資產負債表：未解析到這家公司")

        if cashflow:
            print(f"  營業現金流  {money(cashflow.operating_cash_flow)}")
        else:
            print("  現金流量表：未解析到這家公司")

    rule("欄位對應狀況（SchemaWatch）")
    if client.schema_watch.has_issues:
        print(f"  對應不到的欄位共 {len(client.schema_watch.unknown_fields)} 項：")
        for issue in client.schema_watch.unknown_fields:
            print(f"    · {issue}")
        print("\n  註：分業別的表格本來就缺某些欄位（金融業沒有營業成本），")
        print("      要對照上方表頭判斷是「業別本來就沒有」還是「對應寫錯了」。")
    else:
        print("  全部欄位都對得上。")

    rule("快取大小（決定歷史要不要進版控）")
    import os

    total = 0
    count = 0
    for root, _dirs, files in os.walk("data/raw/probe"):
        for name in files:
            total += os.path.getsize(os.path.join(root, name))
            count += 1
    print(f"  {count} 個檔案，共 {total / 1e6:.1f} MB（3 次請求）")
    print(f"  推估十年份（約 16 期 × 3 表 × 2 市場 = 96 次）：{total / max(count, 1) * 96 / 1e6:.0f} MB")

    return 0


if __name__ == "__main__":
    sys.exit(main())
