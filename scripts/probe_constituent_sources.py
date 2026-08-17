#!/usr/bin/env python3
"""探測 00929 成分股名單的可用來源。

**這是一次性的診斷工具，不是正式流程的一部分。**

背景：證交所 OpenAPI 的 143 個端點裡沒有任何 ETF 成分股資料
（已比對 `成分` / `PCF` / `申購買回` / `holding` / `composition` 皆 0 筆），
而復華官網是 JS 動態渲染，純 HTTP 拿不到持股表。

因此需要在**網路可通的環境**（GitHub Actions）實際探測，回答兩個問題：

1. 復華頁面背後是否有一個回傳 JSON 的 API？
   若有，直接打那個端點遠比用瀏覽器爬 DOM 穩定。
2. 若沒有，DOM 裡的持股表格長什麼樣？

用法（在 GitHub Actions 上執行）：

    python scripts/probe_constituent_sources.py

輸出會列出頁面載入期間的所有 XHR/fetch 回應，
以及各候選 HTTP 端點的實際回應開頭。
"""

from __future__ import annotations

import json
import sys
from typing import Any

FUHWA_URL = "https://www.fhtrust.com.tw/ETF/etf_detail/ETF21"

# 純 HTTP 候選端點。逐一實際請求並印出回應開頭，不猜測。
HTTP_CANDIDATES = [
    ("TWSE ETF 專區頁", "https://www.twse.com.tw/zh/ETFortune/etfInfo/00929", None),
    ("TWSE rwd etfPCF", "https://www.twse.com.tw/rwd/zh/ETF/etfPCF", {"stkNo": "00929", "response": "json"}),
    ("TWSE exchangeReport etfPCF", "https://www.twse.com.tw/exchangeReport/etfPCF", {"stkNo": "00929", "response": "json"}),
    ("TWSE 基金基本資料彙總表", "https://openapi.twse.com.tw/v1/opendata/t187ap47_L", None),
    ("TPEx openapi 首頁", "https://www.tpex.org.tw/openapi/v1/", None),
]

PREVIEW = 400
"""每個回應印出的字元數。夠判斷格式，又不會把 log 灌爆。"""


def _preview(text: str, limit: int = PREVIEW) -> str:
    text = text.strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def probe_http() -> None:
    import requests

    print("=" * 78)
    print("一、純 HTTP 候選端點")
    print("=" * 78)

    for label, url, params in HTTP_CANDIDATES:
        print(f"\n--- {label} ---\n{url}")
        try:
            response = requests.get(
                url,
                params=params,
                timeout=25,
                headers={"User-Agent": "Mozilla/5.0 (probe) buffett00929/0.1"},
            )
        except Exception as exc:  # noqa: BLE001 - 探測工具，任何錯誤都要看到
            print(f"  ✗ 請求失敗：{type(exc).__name__}: {exc}")
            continue

        content_type = response.headers.get("content-type", "?")
        print(f"  HTTP {response.status_code} | {content_type} | {len(response.content)} bytes")
        try:
            payload = response.json()
        except ValueError:
            print(f"  非 JSON。內容開頭：{_preview(response.text)}")
            continue

        print(f"  ✓ JSON，頂層型別 {type(payload).__name__}")
        if isinstance(payload, dict):
            print(f"    keys: {list(payload)[:15]}")
        elif isinstance(payload, list) and payload:
            print(f"    {len(payload)} 筆，第一筆 keys: {list(payload[0])[:15] if isinstance(payload[0], dict) else payload[0]}")


def probe_fuhwa_network() -> None:
    """載入復華頁面並側錄所有 XHR/fetch 回應。

    JS 頁面的持股表一定是從某個端點抓來的。找到那個端點就能直接打，
    比在 DOM 上爬表格穩定得多——版面改了 DOM 就變，API 通常不會。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\n✗ 未安裝 playwright，略過瀏覽器探測")
        return

    print("\n" + "=" * 78)
    print("二、復華官網載入期間的 XHR / fetch 回應")
    print("=" * 78)
    print(f"目標：{FUHWA_URL}\n")

    captured: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()

        def on_response(response) -> None:
            resource = response.request.resource_type
            if resource not in ("xhr", "fetch"):
                return
            entry = {
                "url": response.url,
                "status": response.status,
                "content_type": response.headers.get("content-type", "?"),
                "body": "",
            }
            try:
                entry["body"] = response.text()[: PREVIEW * 3]
            except Exception as exc:  # noqa: BLE001
                entry["body"] = f"(無法讀取內容：{exc})"
            captured.append(entry)

        page.on("response", on_response)

        try:
            page.goto(FUHWA_URL, wait_until="networkidle", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            print(f"頁面載入警告：{type(exc).__name__}: {exc}（仍會輸出已側錄的請求）")

        page.wait_for_timeout(4000)

        print(f"側錄到 {len(captured)} 個 XHR/fetch 回應：\n")
        for index, entry in enumerate(captured, start=1):
            print(f"[{index}] HTTP {entry['status']} | {entry['content_type']}")
            print(f"    {entry['url']}")
            body = entry["body"]
            # 只有可能含成分股的回應才值得看內容。
            looks_relevant = any(
                token in body for token in ("股票", "代號", "權重", "持股", "成分", "stock", "weight")
            )
            marker = "  ★ 可能含成分股" if looks_relevant else ""
            print(f"    內容開頭：{_preview(body, 300)}{marker}\n")

        # DOM 後備：若沒有可用 API，就得知道表格長什麼樣。
        print("-" * 78)
        print("三、DOM 中的表格結構")
        print("-" * 78)
        tables = page.query_selector_all("table")
        print(f"找到 {len(tables)} 個 <table>\n")
        for index, table in enumerate(tables[:6], start=1):
            text = (table.inner_text() or "").strip()
            rows = [r for r in text.split("\n") if r.strip()]
            print(f"[表格 {index}] {len(rows)} 列")
            for row in rows[:6]:
                print(f"    {row[:150]}")
            print()

        browser.close()

    print("=" * 78)
    print("結論指引")
    print("=" * 78)
    print("· 若上方有標記「★ 可能含成分股」的 JSON 回應 → 直接接那個端點，不需瀏覽器")
    print("· 若沒有，但 DOM 表格含代號與權重 → 用 Playwright 抓 DOM")
    print("· 兩者皆無 → 官方自動化不可行，改以人工名單為主要來源")


def main() -> int:
    probe_http()
    probe_fuhwa_network()
    return 0


if __name__ == "__main__":
    sys.exit(main())
