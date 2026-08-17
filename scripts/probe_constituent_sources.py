#!/usr/bin/env python3
"""探測 00929 成分股名單的可用來源。

**這是一次性的診斷工具，不是正式流程的一部分。**

第一輪探測（側錄復華頁面的 XHR 流量）已找到目標端點：

    https://www.fhtrust.com.tw/api/assets?fundID=ETF21&qDate=YYYY/MM/DD

回應含 ``etf002: "00929"``、``ec038``（追蹤指數）、``pcf_FundNav``
與一個巢狀 ``result`` 陣列（``ftype: 股票``）。

第二輪（本檔現在的內容）要回答三個決定實作方式的問題：

1. 這個端點用**純 HTTP**（不開瀏覽器）打得通嗎？
   若可以，正式流程就不需要 Playwright——少一個沉重且易碎的相依。
2. 巢狀結構與欄位名稱到底長什麼樣？（決定解析器怎麼寫）
3. ``qDate`` 的行為：假日或未來日期會回什麼？（決定要不要往前找交易日）

用法（須在網路可通的環境執行，例如 GitHub Actions）：

    python scripts/probe_constituent_sources.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from typing import Any

BASE = "https://www.fhtrust.com.tw/api"
FUND_ID = "ETF21"  # 復華內部代號，對應 00929

HEADERS = {
    "User-Agent": "Mozilla/5.0 (probe) buffett00929/0.1",
    "Accept": "application/json",
}


def fetch(label: str, path: str, params: dict | None = None) -> Any:
    import requests

    url = f"{BASE}{path}"
    print(f"\n--- {label} ---\n{url}  params={params}")
    try:
        response = requests.get(url, params=params, headers=HEADERS, timeout=30)
    except Exception as exc:  # noqa: BLE001 - 探測工具，任何錯誤都要看到
        print(f"  ✗ 請求失敗：{type(exc).__name__}: {exc}")
        return None

    print(f"  HTTP {response.status_code} | {response.headers.get('content-type', '?')}")
    if response.status_code != 200:
        print(f"  內容開頭：{response.text[:300]}")
        return None

    try:
        return response.json()
    except ValueError:
        print(f"  ✗ 非 JSON。開頭：{response.text[:300]}")
        return None


def describe(value: Any, indent: int = 4, depth: int = 0, max_depth: int = 4) -> None:
    """遞迴描述 JSON 結構，只印型別與少量樣本，避免把 log 灌爆。"""
    pad = " " * indent
    if depth > max_depth:
        print(f"{pad}…（超過深度上限）")
        return

    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                size = len(item)
                print(f"{pad}{key}: {type(item).__name__}[{size}]")
                describe(item, indent + 2, depth + 1, max_depth)
            else:
                preview = str(item)
                if len(preview) > 60:
                    preview = preview[:60] + "…"
                print(f"{pad}{key}: {preview!r}")
    elif isinstance(value, list):
        if not value:
            print(f"{pad}（空陣列）")
            return
        print(f"{pad}[0] 為 {type(value[0]).__name__}：")
        describe(value[0], indent + 2, depth + 1, max_depth)
        if len(value) > 1:
            print(f"{pad}…另有 {len(value) - 1} 筆同型元素")


def probe_assets() -> None:
    """主目標：持股明細。"""
    print("=" * 78)
    print("一、持股明細 /api/assets —— 純 HTTP，未開瀏覽器")
    print("=" * 78)

    today = date.today()
    payload = fetch(
        "assets（今日）", "/assets", {"fundID": FUND_ID, "qDate": today.strftime("%Y/%m/%d")}
    )

    if payload is None:
        print("\n✗ 純 HTTP 取不到——正式流程仍需 Playwright")
        return

    print("\n✓ 純 HTTP 可取得，不需瀏覽器")
    print("\n【完整結構】")
    describe(payload)

    # 把最外層那筆的巢狀 result 挖出來——那應該就是持股明細。
    outer = (payload.get("result") or [{}])[0] if isinstance(payload, dict) else {}
    holdings = outer.get("result")
    if isinstance(holdings, list) and holdings:
        print(f"\n【巢狀 result：{len(holdings)} 個資產類別】")
        for group in holdings:
            if not isinstance(group, dict):
                continue
            ftype = group.get("ftype") or group.get("itemName")
            # 找出這個類別下的明細陣列（欄位名未知，逐一找 list）。
            for key, item in group.items():
                if isinstance(item, list) and item:
                    print(f"\n  ftype={ftype!r} → 明細欄位 {key!r}，{len(item)} 筆")
                    print("  第一筆完整內容：")
                    print(
                        "    "
                        + json.dumps(item[0], ensure_ascii=False, indent=2).replace("\n", "\n    ")
                    )
                    if len(item) > 1:
                        print("  第二筆：")
                        print(
                            "    "
                            + json.dumps(item[1], ensure_ascii=False, indent=2).replace(
                                "\n", "\n    "
                            )
                        )


def probe_date_behaviour() -> None:
    """qDate 的行為決定要不要往前回溯交易日。"""
    print("\n" + "=" * 78)
    print("二、qDate 行為：假日／未來日期會回什麼？")
    print("=" * 78)

    today = date.today()
    for label, when in (
        ("未來日期（+7 天）", today + timedelta(days=7)),
        ("一週前", today - timedelta(days=7)),
        ("一個月前", today - timedelta(days=30)),
    ):
        payload = fetch(label, "/assets", {"fundID": FUND_ID, "qDate": when.strftime("%Y/%m/%d")})
        if not isinstance(payload, dict):
            continue
        outer = (payload.get("result") or [{}])
        if not outer:
            print("  → result 為空陣列")
            continue
        entry = outer[0] if isinstance(outer[0], dict) else {}
        inner = entry.get("result")
        count = len(inner) if isinstance(inner, list) else 0
        print(f"  → dDate={entry.get('dDate')!r}，巢狀 result {count} 個類別")

    # 省略 qDate 時的行為——若能自動給最新，實作可以更簡單。
    payload = fetch("省略 qDate", "/assets", {"fundID": FUND_ID})
    if isinstance(payload, dict):
        outer = payload.get("result") or []
        entry = outer[0] if outer and isinstance(outer[0], dict) else {}
        print(f"  → dDate={entry.get('dDate')!r}")


def probe_companion_endpoints() -> None:
    """同時側錄到的其他端點，可能取代目前缺料的欄位。"""
    print("\n" + "=" * 78)
    print("三、其他可用端點")
    print("=" * 78)

    today = date.today().strftime("%Y/%m/%d")
    for label, path, params in (
        ("基金基本資料", "/fund", {"fundID": FUND_ID}),
        ("PCF 申購買回清單", "/ETFPcf", {"fundID": FUND_ID, "pcfDate": today}),
        ("配息紀錄", "/fundDividend", {"m": "fund", "fundID": FUND_ID,
                                        "sDate": "2023/01/01", "eDate": today,
                                        "dateType": "divDate", "ec001": "3"}),
    ):
        payload = fetch(label, path, params)
        if isinstance(payload, dict):
            outer = payload.get("result") or []
            print(f"  result 長度：{len(outer)}")
            if outer and isinstance(outer[0], dict):
                print(f"  第一筆 keys：{list(outer[0])[:25]}")


def main() -> int:
    probe_assets()
    probe_date_behaviour()
    probe_companion_endpoints()
    print("\n" + "=" * 78)
    print("結論指引")
    print("=" * 78)
    print("· /api/assets 若純 HTTP 可取得 → 實作直接用 requests，不需 Playwright")
    print("· 依上方「第一筆完整內容」的欄位名寫解析器，不再猜測")
    print("· 依 qDate 行為決定是否需要往前回溯交易日")
    return 0


if __name__ == "__main__":
    sys.exit(main())
