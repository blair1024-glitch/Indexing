#!/usr/bin/env python3
"""探測公開資訊觀測站（MOPS）的多年度財報取得方式。

**一次性診斷工具，不是正式流程的一部分。**

## 前三輪的結論

**第一輪：舊系統在新網域上已不存在。** ``mops.twse.com.tw`` 的所有傳統端點
都只回 65 bytes 的轉址殘骸 ``<script> location.href = ... "/mops"; </script>``；
``/server-java/*`` 一律 404。

**第二輪：新版是 SPA。** 側錄首頁得知資料走 ``POST /mops/api/<group>/<code>``
且 body 是 **JSON**（第一輪送 form-encoded 才會全軍覆沒）::

    POST /mops/api/home_page/t146sb10   body {"count": 8, "marketKind": "sii"}
    POST /mops/api/home_page/t108sb31new  body {"yymm": "1158"}

注意路徑中間那段 ``home_page``——這是第三輪失敗的原因。

**第三輪：從 bundle 挖出完整路由表，並發現舊網域還活著。**
``/mops/assets/index.js`` 裡有 397 個報表代碼與它們的中文名稱，
本專案要的三張表確認為::

    t163sb04  綜合損益表      t163sb05  資產負債表      t163sb20  現金流量表

同一輪也證實 ``mopsov.twse.com.tw/mops/web/t163sb04`` 仍回傳 45,714 bytes
的真實 HTML（不是轉址殘骸）——**舊網域還在供應彙總報表**。
而直接打 ``/mops/api/t163sb04`` 全部落空，因為少了中間的 group 區段。

## 第四輪（本輪）：讀 chunk，拿到確切的端點與參數

路由表顯示每個報表都是獨立的 lazy chunk（``import("./t163sb04.js")``），
**發送請求的程式碼就在那個 chunk 裡**。把 chunk 抓下來讀，
就能得到確切的 API 路徑與 payload 欄位名——不必再猜，也不必開瀏覽器。

同時並行測試舊網域的 ``ajax_t163sb04`` 表單查詢：
第三輪已證明舊網域會回真實內容，這是同樣值得一試的路徑。

用法（須在網路可通的環境執行，例如 GitHub Actions）：

    python scripts/probe_mops.py
"""

from __future__ import annotations

import re
import sys
from typing import Any

import requests

BASE = "https://mops.twse.com.tw"
LEGACY = "https://mopsov.twse.com.tw"
ASSETS = f"{BASE}/mops/assets"
TIMEOUT = 40

# 本專案要的三張表，代碼由第三輪的路由表確認。
TARGETS = {
    "t163sb04": "綜合損益表",
    "t163sb05": "資產負債表",
    "t163sb20": "現金流量表",
}

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

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def preview(text: Any, limit: int = 400) -> str:
    return " ".join(str(text).split())[:limit]


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ----------------------------------------------------------------------
# 階段 1：抓下報表 chunk，讀出它真正呼叫的端點
# ----------------------------------------------------------------------


def mine_chunks() -> tuple[set[str], set[str]]:
    """回傳 (API 路徑, payload 欄位名候選)。"""
    rule("階段 1：讀取報表 chunk，找出確切的 API 路徑與參數")

    api_paths: set[str] = set()
    param_names: set[str] = set()

    for code, label in TARGETS.items():
        url = f"{ASSETS}/{code}.js"
        print(f"\n── {code}（{label}）──\n{url}")
        try:
            response = SESSION.get(url, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {type(exc).__name__}: {exc}")
            continue

        if response.status_code != 200:
            print(f"  ✗ HTTP {response.status_code}")
            continue

        text = response.text
        print(f"  ✓ {len(text):,} bytes")

        found = set(re.findall(r'["\'`]([^"\'`]*api/[A-Za-z0-9_\-/]{3,80})', text))
        found |= set(re.findall(r'["\'`](/[A-Za-z0-9_\-/]*t\d{2,3}s[a-z]\w*)["\'`]', text))
        api_paths |= found
        print(f"  API 字串：{sorted(found) if found else '（無）'}")

        # 送出請求的那一段程式碼——payload 欄位名就在裡面。
        for match in re.finditer(r"(post|get|request|fetch|axios)\s*[(<]", text):
            start = max(0, match.start() - 400)
            snippet = text[start : match.end() + 600]
            if "api" in snippet or "t163" in snippet:
                print(f"\n  ── 送出請求處 ──\n  …{preview(snippet, 900)}…")

        # 表單欄位：民國年、季別、市場別的實際參數名。
        for key in re.findall(r"\b(year|season|quarter|market\w*|type\w*|TYPEK|"
                              r"companyId|dataType|date|yymm|isQuery|encode\w*)\b", text):
            param_names.add(key)

        for term in ("民國", "季別", "年度", "市場別", "全部", "上市", "上櫃"):
            for match in re.finditer(re.escape(term), text):
                start = max(0, match.start() - 200)
                print(f"  「{term}」附近：…{preview(text[start : match.end() + 260], 460)}…")
                break  # 每個詞只看第一次出現

    print(f"\n-- 彙整：API 路徑 --")
    for path in sorted(api_paths):
        print(f"  {path}")
    print(f"\n-- 彙整：可能的參數名 --\n  {sorted(param_names)}")
    return api_paths, param_names


# ----------------------------------------------------------------------
# 階段 2：打新版 API
# ----------------------------------------------------------------------

PAYLOADS: list[tuple[str, dict[str, Any]]] = [
    ("市場別 + 民國年 + 季", {"marketKind": "sii", "year": "114", "season": "01"}),
    ("市場別 + 民國年 + 季（無前導零）", {"marketKind": "sii", "year": "114", "season": "1"}),
    ("加 isQuery", {"marketKind": "sii", "year": "114", "season": "01", "isQuery": "Y"}),
    ("舊參數名 TYPEK", {"TYPEK": "sii", "year": "114", "season": "01"}),
]


def call(url: str, payload: dict[str, Any], label: str) -> bool:
    try:
        response = SESSION.post(url, json=payload, timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        print(f"    {label:<28} ✗ {type(exc).__name__}: {exc}")
        return False

    body = response.text or ""
    note = ""
    hit = False
    try:
        parsed = response.json()
    except ValueError:
        pass
    else:
        if isinstance(parsed, dict):
            note = f" keys={list(parsed)[:8]}"
            payload_data = parsed.get("data") or parsed.get("result")
            if payload_data:
                hit = True
                note += "  ★ 有 data"

    print(f"    {label:<28} HTTP {response.status_code} {len(body):>8,}B{note}")
    print(f"      {preview(body, 260)}")
    if hit:
        print(f"\n      ── 完整回應（前 3000 字）──\n      {preview(body, 3000)}\n")
    return hit


def probe_new_api(discovered: set[str]) -> list[str]:
    rule("階段 2：呼叫新版 API")

    # 第二輪確認 group 區段存在（home_page）。財報頁的 group 未知，
    # 所以把 chunk 挖到的路徑排最前面，其餘為推測。
    candidates: list[str] = []
    for path in sorted(discovered):
        cleaned = path.lstrip("/")
        for code in TARGETS:
            if code in cleaned:
                candidates.append(f"{BASE}/{cleaned.lstrip('/')}")
    for code in TARGETS:
        for group in ("", "t163/", "quer_summary/", "query/", "summary/", "web/"):
            candidates.append(f"{BASE}/mops/api/{group}{code}")

    seen: set[str] = set()
    hits: list[str] = []
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        print(f"\n  ── {url} ──")
        for label, payload in PAYLOADS:
            if call(url, payload, label):
                hits.append(url)
                break
    return hits


# ----------------------------------------------------------------------
# 階段 3：舊網域的表單查詢（第三輪已證實舊網域會回真實內容）
# ----------------------------------------------------------------------


def probe_legacy_forms() -> list[str]:
    rule("階段 3：舊網域 mopsov 的彙總報表表單查詢")

    form = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": "sii",
        "year": "114",
        "season": "01",
    }
    legacy_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"{LEGACY}/mops/web/t163sb04",
        "Origin": LEGACY,
    }

    hits: list[str] = []
    for code, label in TARGETS.items():
        for prefix in ("ajax_", ""):
            url = f"{LEGACY}/mops/web/{prefix}{code}"
            try:
                response = SESSION.post(
                    url, data=form, headers=legacy_headers, timeout=TIMEOUT
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {url} → {type(exc).__name__}: {exc}")
                continue

            body = response.text or ""
            # 彙總報表的特徵：整頁表格，含公司代號欄與大量 <tr>。
            rows = body.count("<tr")
            has_header = "公司代號" in body or "公司名稱" in body
            marker = "  ★ 疑似彙總表" if rows > 50 and has_header else ""
            print(
                f"\n  {url}（{label}）→ HTTP {response.status_code}, "
                f"{len(body):,}B, {rows} 個 <tr>, 含公司代號={has_header}{marker}"
            )
            print(f"    {preview(body, 300)}")

            if marker:
                hits.append(url)
                # 表頭是寫解析器的依據，完整印出來。
                header = re.search(r"<tr[^>]*>(.{0,2500}?)</tr>", body, re.S)
                if header:
                    print(f"\n    ── 表頭 ──\n    {preview(header.group(1), 1600)}")
                first_data = re.findall(r"<tr[^>]*>(.{0,2500}?)</tr>", body, re.S)
                for row in first_data[1:4]:
                    print(f"\n    ── 資料列 ──\n    {preview(row, 900)}")
    return hits


# ----------------------------------------------------------------------


def main() -> int:
    discovered, _params = mine_chunks()
    api_hits = probe_new_api(discovered)
    legacy_hits = probe_legacy_forms()

    rule("結論")
    if api_hits:
        print(f"✓ 新版 API 可用：{api_hits}")
        print("  → 首選。JSON 回應最好解析，依實際欄位寫 sources/mops.py")
    if legacy_hits:
        print(f"✓ 舊網域彙總報表可用：{legacy_hits}")
        print("  → 備案。HTML 表格，解析器要對表頭做 SchemaWatch 檢查")
    if not api_hits and not legacy_hits:
        print("✗ 兩條路都沒拿到資料。")
        print("  → 看階段 1 印出的「送出請求處」找出正確的 group 與參數名；")
        print("     若 chunk 讀不到，改用 Playwright 實際操作查詢表單並側錄")
        print("     （一個 context 只載一頁，任何導航前先讀完 response body）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
