#!/usr/bin/env python3
"""探測公開資訊觀測站（MOPS）的多年度財報取得方式。

**一次性診斷工具，不是正式流程的一部分。**

背景：沒有 FinMind token 時，唯一的財報來源是證交所 OpenAPI 的最新一期，
目前為 2026Q2＝**半年累計數**。系統正確地拒絕把半年當成一年，
因此連「最近一年 ROE」都算不出來，0 / 50 檔可排名。
改由 MOPS 取得歷史財報——官方來源，且不需第三方憑證。

**關鍵限制：請求量。** 逐檔查詢是 50 檔 × 40 季 × 3 表 ≈ 6000 次請求，必被擋。
所以只有「彙總報表」（一次一期、全市場所有公司）這條路可行：
10 年 × 4 季 × 3 表 ≈ 120 次請求。本探測的首要目的就是確認這條路存在。

本檔全部是**問題**，不是假設。要回答：

1. 網域是 mops.twse.com.tw 還是 mopsov.twse.com.tw？（近年有遷移）
2. 彙總報表端點存在嗎？GET 還是 POST？參數實際叫什麼？
3. 回應是 HTML／CSV／JSON？欄位名稱實際長什麼樣？
4. XBRL 批次下載可用嗎？（可用的話比解析 HTML 穩定得多）
5. 連續請求多少次會被擋？

用法（須在網路可通的環境執行，例如 GitHub Actions）：

    python scripts/probe_mops.py
"""

from __future__ import annotations

import sys
import time
from typing import Any

import requests

PREVIEW = 500
TIMEOUT = 30

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

# 民國年。2026 年＝民國 115 年。用 2025Q1（民國 114 年第 1 季）當測試期別——
# 夠舊確定已公告，又夠新確定還在系統裡。
TEST_ROC_YEAR = "114"
TEST_SEASON = "01"

DOMAINS = [
    "https://mops.twse.com.tw",
    "https://mopsov.twse.com.tw",
    "https://mops.twse.com.tw/mops",
]


def preview(text: str, limit: int = PREVIEW) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:limit] + ("…" if len(collapsed) > limit else "")


def describe_response(response: requests.Response) -> None:
    content_type = response.headers.get("content-type", "?")
    print(f"  HTTP {response.status_code} | {content_type} | {len(response.content)} bytes")

    body = response.text
    # 判斷是不是真的拿到報表，還是拿到錯誤頁／查無資料。
    markers = {
        "含 <table>": "<table" in body.lower(),
        "含 公司代號": "公司代號" in body,
        "含 查詢無資料": "查詢無資料" in body or "查無資料" in body,
        "含 系統忙碌/請稍後": ("系統" in body and "忙碌" in body) or "請稍後" in body,
        "含 驗證碼": "驗證碼" in body or "captcha" in body.lower(),
    }
    hits = [name for name, hit in markers.items() if hit]
    print(f"  訊號：{', '.join(hits) if hits else '（無明顯訊號）'}")
    print(f"  開頭：{preview(body, 300)}")


def try_request(
    label: str,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    data: dict | None = None,
) -> requests.Response | None:
    print(f"\n--- {label} ---")
    print(f"  {method} {url}")
    if params:
        print(f"  params={params}")
    if data:
        print(f"  data={data}")
    try:
        response = requests.request(
            method, url, params=params, data=data, headers=HEADERS, timeout=TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 - 探測工具，任何錯誤都要看到
        print(f"  ✗ {type(exc).__name__}: {exc}")
        return None
    describe_response(response)
    return response


# --------------------------------------------------------------------------
# 一、網域是否還在
# --------------------------------------------------------------------------


def probe_domains() -> list[str]:
    print("=" * 78)
    print("一、網域可達性")
    print("=" * 78)

    alive = []
    for domain in DOMAINS:
        response = try_request(domain, "GET", f"{domain}/")
        if response is not None and response.status_code < 400:
            alive.append(domain)
    print(f"\n可達網域：{alive or '（無）'}")
    return alive


# --------------------------------------------------------------------------
# 二、彙總報表（首要目標）
# --------------------------------------------------------------------------

# MOPS 的彙總報表代號（依歷來慣例，實際是否可用由本探測回答）：
#   t163sb04  綜合損益表（一般業）
#   t163sb05  資產負債表（一般業）
#   t163sb20  現金流量表
# 舊路徑為 /mops/web/<code>，新版可能改為 /mops/api/<code> 或其他。
SUMMARY_REPORTS = [
    ("綜合損益表 t163sb04", "t163sb04"),
    ("資產負債表 t163sb05", "t163sb05"),
    ("現金流量表 t163sb20", "t163sb20"),
]

PATH_SHAPES = ["/mops/web/{code}", "/mops/api/{code}", "/server-java/{code}", "/{code}"]


def probe_summary_reports(domain: str) -> None:
    print("\n" + "=" * 78)
    print(f"二、彙總報表（一次一期全市場）—— {domain}")
    print("=" * 78)
    print(f"測試期別：民國 {TEST_ROC_YEAR} 年第 {TEST_SEASON} 季\n")

    # MOPS 傳統上用 form POST，參數名稱歷來為 encodeURIComponent/step/firstin/
    # TYPEK（市場別）/year/season。這裡把最可能的組合實際打出去看回應。
    payload = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": "sii",  # sii=上市, otc=上櫃
        "year": TEST_ROC_YEAR,
        "season": TEST_SEASON,
    }

    for label, code in SUMMARY_REPORTS:
        for shape in PATH_SHAPES:
            url = domain + shape.format(code=code)
            response = try_request(f"{label} POST {shape}", "POST", url, data=payload)
            if response is not None and response.status_code == 200 and "<table" in response.text.lower():
                print("  ★ 疑似取得報表——列印表頭：")
                dump_table_headers(response.text)
                return  # 找到一個可用形狀就夠了，其餘同理
            time.sleep(1.5)


def dump_table_headers(html: str, limit: int = 3) -> None:
    """把前幾個表格的表頭列出來，用來確認欄位名稱。"""
    import re

    tables = re.findall(r"<table.*?</table>", html, flags=re.S | re.I)
    print(f"    找到 {len(tables)} 個 <table>")
    for index, table in enumerate(tables[:limit], start=1):
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", table, flags=re.S | re.I)
        cleaned = [" ".join(re.sub(r"<[^>]+>", "", c).split()) for c in cells[:20]]
        cleaned = [c for c in cleaned if c]
        print(f"    [表 {index}] 前 20 格：{cleaned}")


# --------------------------------------------------------------------------
# 三、XBRL 批次下載
# --------------------------------------------------------------------------


def probe_xbrl(domain: str) -> None:
    print("\n" + "=" * 78)
    print("三、XBRL 批次下載（若可用會比解析 HTML 穩定得多）")
    print("=" * 78)

    for label, path, payload in (
        (
            "FileDownLoad 批次",
            "/server-java/FileDownLoad",
            {
                "step": "9",
                "functionName": "t187ap14_L",
                "filePath": "/t187ap14_L/",
                "fileName": f"{TEST_ROC_YEAR}{TEST_SEASON}.zip",
            },
        ),
        (
            "t57sb01 XBRL 查詢",
            "/mops/web/t57sb01_q1",
            {"encodeURIComponent": "1", "step": "1", "firstin": "1",
             "TYPEK": "sii", "year": TEST_ROC_YEAR, "season": TEST_SEASON},
        ),
    ):
        try_request(label, "POST", domain + path, data=payload)
        time.sleep(1.5)


# --------------------------------------------------------------------------
# 四、速率限制
# --------------------------------------------------------------------------


def probe_rate_limit(domain: str) -> None:
    """連續請求，看第幾次開始被擋。

    這決定實作的間隔設定：120 次請求若每次要等 10 秒，一輪就要 20 分鐘，
    對每日排程來說仍可接受；若要等 60 秒就不可行，得改用快取回補策略。
    """
    print("\n" + "=" * 78)
    print("四、速率限制：連續 6 次請求，間隔 1 秒")
    print("=" * 78)

    url = f"{domain}/mops/web/t163sb04"
    payload = {
        "encodeURIComponent": "1", "step": "1", "firstin": "1", "off": "1",
        "TYPEK": "sii", "year": TEST_ROC_YEAR, "season": TEST_SEASON,
    }
    for attempt in range(1, 7):
        started = time.time()
        try:
            response = requests.post(url, data=payload, headers=HEADERS, timeout=TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            print(f"  第 {attempt} 次：✗ {type(exc).__name__}: {exc}")
        else:
            blocked = "忙碌" in response.text or response.status_code == 429
            print(
                f"  第 {attempt} 次：HTTP {response.status_code}，"
                f"{len(response.content)} bytes，{time.time() - started:.1f}s"
                f"{'　★ 疑似被擋' if blocked else ''}"
            )
        time.sleep(1)


def main() -> int:
    alive = probe_domains()
    if not alive:
        print("\n✗ 所有候選網域都不可達，MOPS 路線需重新評估")
        return 0

    domain = alive[0]
    probe_summary_reports(domain)
    probe_xbrl(domain)
    probe_rate_limit(domain)

    print("\n" + "=" * 78)
    print("結論指引")
    print("=" * 78)
    print("· 有取到含「公司代號」的 <table> → 彙總報表可用，依表頭寫解析器")
    print("· 只拿到錯誤頁或驗證碼 → 該路徑不可用，換路徑形狀或改走 XBRL")
    print("· 被擋很快 → 需拉長間隔並把歷史永久快取，避免每次重抓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
