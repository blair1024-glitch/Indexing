#!/usr/bin/env python3
"""探測公開資訊觀測站（MOPS）的多年度財報取得方式。

**一次性診斷工具，不是正式流程的一部分。**

## 第一輪結果：舊系統已不存在

實測（2026-08-17）確認所有傳統端點都只回 65 bytes 的轉址殘骸::

    <script> location.href = location.origin + "/mops"; </script>

`/mops/web/t163sb04`、`/mops/api/t163sb04`、`t57sb01_q1` 皆同；
`/server-java/*`（含 FileDownLoad）一律 404——舊的 Java servlet 層已拆除。

結論：**form POST 抓彙總報表的做法在 2026 年已死**，不能實作。

## 第二輪：新版是 SPA，側錄它自己呼叫的 API

新版 MOPS 既然是單頁應用，資料就一定來自某組 JSON 端點。
這和先前找復華持股 API 是同一個情況，用同一招：
載入頁面、側錄 XHR/fetch、找出真正的資料端點。

找到端點後直接打它，不需要瀏覽器——**這才是可維護的實作方式**。

用法（須在網路可通的環境執行，例如 GitHub Actions）：

    python scripts/probe_mops.py
"""

from __future__ import annotations

import json
import sys
from typing import Any

BASE = "https://mops.twse.com.tw"
PREVIEW = 420

# 新版 MOPS 的進入點。財報查詢的實際路由未知——先載入首頁側錄，
# 再嘗試幾個可能的 hash 路由，看哪一個會觸發財報相關的 XHR。
ENTRY_POINTS = [
    ("首頁", f"{BASE}/mops/"),
    ("財務報表（猜測路由 A）", f"{BASE}/mops/#/web/t163sb04"),
    ("財務報表（猜測路由 B）", f"{BASE}/mops/#/t163sb04"),
]

# 財報相關的訊號字詞。用來從一堆追蹤與版面請求裡挑出真正有用的回應。
SIGNALS = (
    "公司代號", "營業收入", "資產總", "權益總", "每股盈餘",
    "stockNo", "companyId", "revenue", "季別", "年度",
)


def preview(text: str, limit: int = PREVIEW) -> str:
    return " ".join(text.split())[:limit]


def probe_spa() -> list[dict[str, Any]]:
    """載入新版 MOPS 並側錄所有 XHR/fetch 回應。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("✗ 未安裝 playwright，無法側錄 SPA 流量")
        return []

    captured: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(locale="zh-TW")
        page = context.new_page()

        def on_response(response) -> None:
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            entry: dict[str, Any] = {
                "url": response.url,
                "method": response.request.method,
                "status": response.status,
                "content_type": response.headers.get("content-type", "?"),
                "post_data": response.request.post_data,
                "body": "",
            }
            try:
                entry["body"] = response.text()[:3000]
            except Exception as exc:  # noqa: BLE001
                entry["body"] = f"(無法讀取：{exc})"
            captured.append(entry)

        page.on("response", on_response)

        for label, url in ENTRY_POINTS:
            print(f"\n{'=' * 78}\n載入：{label}\n{url}\n{'=' * 78}")
            before = len(captured)
            try:
                page.goto(url, wait_until="networkidle", timeout=60_000)
            except Exception as exc:  # noqa: BLE001
                print(f"  載入警告：{type(exc).__name__}: {exc}（仍輸出已側錄的請求）")
            page.wait_for_timeout(4000)
            print(f"  新增側錄 {len(captured) - before} 個 XHR/fetch")

        # 頁面上有什麼可點的——新版路由未知時，選單文字是最好的線索。
        print(f"\n{'=' * 78}\n頁面連結與選單文字（用來找財報查詢的實際路由）\n{'=' * 78}")
        try:
            links = page.eval_on_selector_all(
                "a[href]",
                "els => els.slice(0, 60).map(e => (e.textContent||'').trim() + ' -> ' + e.getAttribute('href'))",
            )
            for link in links:
                if link.strip(" ->"):
                    print(f"  {link[:160]}")
        except Exception as exc:  # noqa: BLE001
            print(f"  無法讀取連結：{exc}")

        browser.close()

    return captured


def report(captured: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 78}\n側錄結果：共 {len(captured)} 個 XHR/fetch\n{'=' * 78}")

    interesting = []
    for index, entry in enumerate(captured, start=1):
        body = entry["body"] or ""
        hits = [s for s in SIGNALS if s in body or s in entry["url"]]
        marker = f"  ★ 命中 {hits[:4]}" if hits else ""
        if hits:
            interesting.append(entry)
        print(f"\n[{index}] {entry['method']} HTTP {entry['status']} | {entry['content_type']}")
        print(f"    {entry['url'][:200]}")
        if entry["post_data"]:
            print(f"    POST body：{preview(str(entry['post_data']), 200)}")
        print(f"    回應開頭：{preview(body, 260)}{marker}")

    print(f"\n{'=' * 78}\n疑似財報資料端點：{len(interesting)} 個\n{'=' * 78}")
    for entry in interesting:
        print(f"\n{entry['method']} {entry['url']}")
        if entry["post_data"]:
            print(f"  POST body：{entry['post_data']}")
        print(f"  完整回應（前 1500 字）：\n{entry['body'][:1500]}")

    if not interesting:
        print("（無）——需要實際操作查詢表單才會觸發資料請求，或路由猜錯了。")
        print("請看上方「頁面連結與選單文字」找出財報查詢的真實路由，下一輪再試。")


def main() -> int:
    captured = probe_spa()
    report(captured)
    print(f"\n{'=' * 78}\n結論指引\n{'=' * 78}")
    print("· 有標記 ★ 的 JSON 端點 → 直接打它，依實際欄位寫解析器，不需瀏覽器")
    print("· 只有版面/追蹤請求 → 依選單連結找出財報查詢路由，下一輪針對它側錄")
    print("· 若查詢必須互動觸發 → 下一輪用 Playwright 實際填表送出再側錄")
    return 0


if __name__ == "__main__":
    sys.exit(main())
