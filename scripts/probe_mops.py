#!/usr/bin/env python3
"""探測公開資訊觀測站（MOPS）的多年度財報取得方式。

**一次性診斷工具，不是正式流程的一部分。**

## 第一輪：舊系統已不存在

實測（2026-08-17）確認所有傳統端點都只回 65 bytes 的轉址殘骸::

    <script> location.href = location.origin + "/mops"; </script>

`/mops/web/t163sb04`、`/mops/api/t163sb04`、`t57sb01_q1` 皆同；
`/server-java/*`（含 FileDownLoad）一律 404——舊的 Java servlet 層已拆除。

結論：**form POST 抓彙總報表的做法在 2026 年已死**，不能實作。

## 第二輪：找到新版的 API 形狀，但沒拿到內容

側錄首頁得知新版是 SPA，資料來自 ``POST /mops/api/<code>``，
而且 body 是 **JSON**（不是 form-encoded，這正是第一輪失敗的原因）::

    GET  /mops/api/system/maintenance   → {"maintenance": false}
    POST /mops/api/home_page/t146sb10   body {"count": 8, "marketKind": "sii"}
    POST /mops/api/home_page/t108sb31new  body {"yymm": "1158"}

參數形狀也跟著露出來了：民國年月併成 ``1158``、市場別叫 ``marketKind``
（``sii`` = 上市）。但財報端點一個都沒側錄到，因為只有首頁被載入，
而且回應內容全部讀不到（探測程式在讀 body 前就跳去下一頁了）。

## 第三輪（本輪）：直接從 SPA 的 JS bundle 挖出它的端點表

不再猜路由。SPA 的前端程式碼裡一定有完整的路由與 API 對照表——
把 bundle 抓下來、用中文報表名稱與 ``api/`` 樣式反查，就能得到
**財報端點的真實代碼與參數**，不必等它自己發請求。

這和先前找復華持股 API 是同一個思路，只是往下一層：
與其側錄它打了什麼，不如直接讀它「打算打什麼」。

用法（須在網路可通的環境執行，例如 GitHub Actions）：

    python scripts/probe_mops.py
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import requests

BASE = "https://mops.twse.com.tw"
LEGACY = "https://mopsov.twse.com.tw"
TIMEOUT = 40

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": f"{BASE}/mops/",
    "Origin": BASE,
}

# 我們要的三張表。用中文名稱回頭找 bundle 裡的代碼，比猜代碼可靠得多。
WANTED = ["綜合損益表", "資產負債表", "現金流量表", "彙總報表", "財務報表"]

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def preview(text: str, limit: int = 400) -> str:
    return " ".join(str(text).split())[:limit]


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ----------------------------------------------------------------------
# 第一階段：把 SPA 的 JS bundle 抓下來
# ----------------------------------------------------------------------


def fetch_bundles() -> dict[str, str]:
    """取得首頁引用的所有 JS，回傳 {url: 內容}。"""
    rule("階段 1：抓取 SPA 的 JS bundle")

    try:
        page = SESSION.get(f"{BASE}/mops/", timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 首頁抓取失敗：{type(exc).__name__}: {exc}")
        return {}

    print(f"首頁 HTTP {page.status_code}，{len(page.text)} bytes")

    scripts = re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', page.text)
    links = re.findall(r'<link[^>]+href=["\']([^"\']+\.js)["\']', page.text)
    candidates = []
    for src in scripts + links:
        if src.startswith("http"):
            url = src
        elif src.startswith("/"):
            url = BASE + src
        else:
            url = f"{BASE}/mops/{src.lstrip('./')}"
        if url not in candidates:
            candidates.append(url)

    print(f"找到 {len(candidates)} 個 JS 檔：")
    for url in candidates:
        print(f"  · {url}")

    bundles: dict[str, str] = {}
    for url in candidates:
        try:
            response = SESSION.get(url, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {url} → {type(exc).__name__}: {exc}")
            continue
        if response.status_code == 200 and response.text:
            bundles[url] = response.text
            print(f"  ✓ {url.rsplit('/', 1)[-1]} → {len(response.text):,} bytes")
        else:
            print(f"  ✗ {url} → HTTP {response.status_code}")

    return bundles


# ----------------------------------------------------------------------
# 第二階段：從 bundle 裡挖端點與參數
# ----------------------------------------------------------------------


def mine_bundles(bundles: dict[str, str]) -> list[str]:
    """列出 bundle 裡出現的 API 路徑與報表代碼，回傳候選財報代碼。"""
    rule("階段 2：從 bundle 挖出 API 路徑與報表代碼")

    if not bundles:
        print("（沒有 bundle 可分析）")
        return []

    api_paths: set[str] = set()
    codes: set[str] = set()
    context_hits: list[str] = []

    for url, text in bundles.items():
        api_paths.update(re.findall(r'["\'`/]api/([A-Za-z0-9_\-/]{3,60})', text))
        codes.update(re.findall(r"\bt\d{2,3}s[a-z]\w{0,12}\b", text))

        # 中文報表名稱附近的程式碼——這裡通常就是「名稱 → 代碼」的對照。
        for term in WANTED:
            for match in re.finditer(re.escape(term), text):
                start = max(0, match.start() - 260)
                snippet = text[start : match.end() + 260]
                context_hits.append(f"[{url.rsplit('/', 1)[-1]}] …{preview(snippet, 520)}…")

    print(f"\n-- API 路徑（{len(api_paths)} 個）--")
    for path in sorted(api_paths):
        print(f"  api/{path}")

    print(f"\n-- 報表代碼（{len(codes)} 個）--")
    for code in sorted(codes):
        print(f"  {code}")

    print(f"\n-- 中文報表名稱的上下文（去重後最多 40 段）--")
    seen: set[str] = set()
    shown = 0
    for hit in context_hits:
        key = hit[:120]
        if key in seen:
            continue
        seen.add(key)
        print(f"\n  {hit}")
        shown += 1
        if shown >= 40:
            print("\n  （其餘省略）")
            break
    if not context_hits:
        print("  （bundle 內找不到中文報表名稱——可能是語系檔另外載入）")

    # 候選：所有看起來像報表代碼的東西，優先 t163 系列（歷來的財報彙總）。
    ranked = sorted(codes, key=lambda c: (not c.startswith("t163"), c))
    return ranked


# ----------------------------------------------------------------------
# 第三階段：直接打候選端點
# ----------------------------------------------------------------------

# 依第二輪側錄到的真實 body 推出的參數形狀。全部都要實測，不能假設。
PAYLOADS: list[tuple[str, dict[str, Any]]] = [
    ("空 body", {}),
    ("市場別 + 民國年 + 季", {"marketKind": "sii", "year": "115", "season": "01"}),
    ("市場別 + 民國年 + 季（數字）", {"marketKind": "sii", "year": 115, "season": 1}),
    ("舊參數名 TYPEK", {"TYPEK": "sii", "year": "115", "season": "01"}),
    ("含公司代號", {"marketKind": "sii", "year": "115", "season": "01", "companyId": "2330"}),
    ("年月併寫", {"marketKind": "sii", "yymm": "1152"}),
]


def try_endpoint(code: str, payloads: list[tuple[str, dict[str, Any]]]) -> bool:
    """對單一代碼試打各種 body，印出實際回應。回傳是否拿到疑似資料。"""
    got_data = False
    for label, payload in payloads:
        url = f"{BASE}/mops/api/{code}"
        try:
            response = SESSION.post(url, json=payload, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            print(f"    {label:<24} ✗ {type(exc).__name__}: {exc}")
            continue

        body = response.text or ""
        note = ""
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        else:
            if isinstance(parsed, dict):
                note = f" keys={list(parsed)[:8]}"
                data = parsed.get("data") or parsed.get("result")
                if data:
                    got_data = True
                    note += "  ★ 有 data"

        print(
            f"    {label:<24} HTTP {response.status_code} "
            f"{len(body):>7,}B{note}\n"
            f"      {preview(body, 300)}"
        )

        if got_data:
            print(f"\n      ── 完整回應（前 2500 字）──\n      {preview(body, 2500)}\n")
            return True
    return got_data


def probe_codes(codes: list[str]) -> list[str]:
    rule("階段 3：直接呼叫候選端點（JSON body）")

    # 先確認基礎連線與已知端點仍然可用，作為對照組。
    try:
        health = SESSION.get(f"{BASE}/mops/api/system/maintenance", timeout=TIMEOUT)
        print(f"對照組 maintenance：HTTP {health.status_code} {preview(health.text, 120)}")
    except Exception as exc:  # noqa: BLE001
        print(f"對照組 maintenance 失敗：{type(exc).__name__}: {exc}")

    try:
        known = SESSION.post(
            f"{BASE}/mops/api/home_page/t51sb10",
            json={"count": 3, "marketKind": "sii"},
            timeout=TIMEOUT,
        )
        print(
            f"對照組 home_page/t51sb10：HTTP {known.status_code} "
            f"{preview(known.text, 300)}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"對照組 home_page/t51sb10 失敗：{type(exc).__name__}: {exc}")

    if not codes:
        print("\n（bundle 沒挖到代碼，改試歷來已知的財報彙總代碼）")
        codes = ["t163sb04", "t163sb05", "t163sb06", "t163sb01", "t163sb02", "t163sb03"]

    hits: list[str] = []
    for code in codes[:24]:
        print(f"\n  ── {code} ──")
        if try_endpoint(code, PAYLOADS):
            hits.append(code)
    if len(codes) > 24:
        print(f"\n（代碼共 {len(codes)} 個，本輪只試前 24 個）")
    return hits


# ----------------------------------------------------------------------
# 第四階段：舊網域是否還活著
# ----------------------------------------------------------------------


def probe_legacy_host() -> None:
    """第二輪在頁面連結裡看到仍指向 mopsov 的舊網址，實測是否還能用。"""
    rule("階段 4：舊網域 mopsov.twse.com.tw 是否仍供應報表")

    targets = [
        f"{LEGACY}/mops/web/t141sb02",
        f"{LEGACY}/mops/web/t100sb07",
        f"{LEGACY}/mops/web/t163sb04",
        f"{LEGACY}/server-java/t164sb01",
    ]
    for url in targets:
        try:
            response = SESSION.get(url, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {url} → {type(exc).__name__}: {exc}")
            continue
        body = response.text or ""
        redirect = "location.href" in body
        print(
            f"  {'✗ 轉址殘骸' if redirect else '?'} {url} → "
            f"HTTP {response.status_code}, {len(body):,}B\n"
            f"      {preview(body, 240)}"
        )


# ----------------------------------------------------------------------


def main() -> int:
    bundles = fetch_bundles()
    codes = mine_bundles(bundles)
    hits = probe_codes(codes)
    probe_legacy_host()

    rule("結論")
    if hits:
        print(f"✓ 有回資料的端點：{hits}")
        print("  → 下一步：依實際欄位寫 sources/mops.py 的解析器，不需瀏覽器")
    else:
        print("✗ 本輪沒有任何端點回傳資料。")
        print("  → 看階段 2 的「中文報表名稱上下文」找出正確代碼與參數名，")
        print("     或改用 Playwright 實際操作查詢表單（一個 context 只載一頁，")
        print("     並在任何導航前讀完 response body）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
