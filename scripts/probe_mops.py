#!/usr/bin/env python3
"""探測公開資訊觀測站（MOPS）的多年度財報取得方式。

**一次性診斷工具，不是正式流程的一部分。**

## 前四輪的結論

**第一輪：** ``mops.twse.com.tw`` 的所有傳統端點只回 65 bytes 的轉址殘骸，
``/server-java/*`` 一律 404。

**第二輪：** 新版是 SPA，資料走 ``POST /mops/api/<group>/<code>`` 且 body 是 JSON。

**第三輪：** 從 ``/mops/assets/index.js`` 挖出 397 個報表代碼與中文名稱，
確認本專案要的三張表；同時發現 **``mopsov.twse.com.tw`` 舊網域還活著**。

**第四輪：找到了。** 報表 chunk（``/mops/assets/t163sb04.js``）裡有完整的查詢定義::

    inputCode: [
      {id:"TYPEK",  name:"市場別", value:"sii",
       selectOption:[sii=上市, otc=上櫃, rotc=興櫃, pub=公開發行]},
      {id:"year",   name:"年度", placeholder:"請輸入民國年"},
      {id:"season", name:"季別", selectOption:[01,02,03,04]}
    ],
    btnCode: [{id:"searchBtn", action:{apiName:"ajax_t163sb04", method:"POST"}}]

實測 ``POST https://mopsov.twse.com.tw/mops/web/ajax_t163sb04``
帶 ``TYPEK=sii&year=114&season=01`` 回傳 **1,578,302 bytes、1077 個 ``<tr>``**，
內容為「上市公司第一季資料」。三張表都可用::

    ajax_t163sb04  綜合損益表   1,578,302 B
    ajax_t163sb05  資產負債表   1,328,344 B
    ajax_t163sb20  現金流量表     519,290 B

**一次請求＝該期別全市場**，正是規劃需要的量級（10 年約 120 次請求）。

## 第五輪（本輪）：把表格結構挖清楚，才能寫解析器

彙總報表一頁包含**多個表格**（一般業／金融業／證券業／保險業的科目不同），
欄位隨業別而異。解析器不能用猜的——本輪把每個表格的表頭與首筆資料列印出來，
並實測：``season=04`` 是否給年度數、``TYPEK=otc`` 是否給上櫃、以及連續請求的節流需求。

用法（須在網路可通的環境執行，例如 GitHub Actions）：

    python scripts/probe_mops.py
"""

from __future__ import annotations

import re
import sys
import time
from html import unescape

import requests

LEGACY = "https://mopsov.twse.com.tw"
TIMEOUT = 60

REPORTS = {
    "t163sb04": "綜合損益表",
    "t163sb05": "資產負債表",
    "t163sb20": "現金流量表",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": f"{LEGACY}/mops/web/t163sb04",
    "Origin": LEGACY,
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

TAG = re.compile(r"<[^>]+>")


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def cells(row_html: str) -> list[str]:
    """把一列 HTML 拆成純文字儲存格。"""
    raw = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)
    return [" ".join(unescape(TAG.sub("", c)).split()) for c in raw]


def fetch(code: str, typek: str, year: str, season: str) -> tuple[str, float]:
    url = f"{LEGACY}/mops/web/ajax_{code}"
    form = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "TYPEK": typek,
        "year": year,
        "season": season,
    }
    started = time.monotonic()
    response = SESSION.post(url, data=form, timeout=TIMEOUT)
    elapsed = time.monotonic() - started
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text, elapsed


def describe_tables(html: str, *, max_tables: int = 8) -> None:
    """列出頁面裡每個表格的表頭與首筆資料——這就是寫解析器的依據。"""
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.S | re.I)
    print(f"  表格數：{len(tables)}")

    shown = 0
    for index, table in enumerate(tables):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S | re.I)
        if len(rows) < 3:
            continue
        header_index = next(
            (i for i, r in enumerate(rows) if "公司代號" in r),
            None,
        )
        if header_index is None:
            continue

        shown += 1
        if shown > max_tables:
            print(f"  （其餘 {len(tables) - index} 個表格省略）")
            break

        header = cells(rows[header_index])
        print(f"\n  ── 表格 #{index}：{len(rows)} 列，表頭 {len(header)} 欄 ──")
        print(f"     表頭：{header}")
        for row in rows[header_index + 1 : header_index + 3]:
            data = cells(row)
            if data and data[0].strip().isdigit():
                print(f"     資料：{data}")


def probe_structure() -> None:
    rule("階段 1：三張表的欄位結構（民國 114 年第一季，上市）")
    for code, label in REPORTS.items():
        html, elapsed = fetch(code, "sii", "114", "01")
        title = re.search(r"<h2>(.*?)</h2>", html, re.S)
        print(f"\n### {code} {label} — {len(html):,} 字元，{elapsed:.1f}s")
        print(f"  頁面標題：{title.group(1).strip() if title else '（無）'}")
        describe_tables(html)


def probe_annual_and_otc() -> None:
    rule("階段 2：season=04 是否為年度數，TYPEK=otc 是否為上櫃")

    for season, expect in (("04", "第四季/年度"), ("01", "第一季")):
        html, _ = fetch("t163sb04", "sii", "113", season)
        title = re.search(r"<h2>(.*?)</h2>", html, re.S)
        rows = html.count("<tr")
        print(
            f"  season={season}（預期 {expect}）→ {len(html):,} 字元，{rows} 列，"
            f"標題：{title.group(1).strip() if title else '（無）'}"
        )

    html, _ = fetch("t163sb04", "otc", "114", "01")
    title = re.search(r"<h2>(.*?)</h2>", html, re.S)
    print(
        f"  TYPEK=otc → {len(html):,} 字元，{html.count('<tr')} 列，"
        f"標題：{title.group(1).strip() if title else '（無）'}"
    )


def probe_history_depth() -> None:
    rule("階段 3：歷史深度——十年前的期別是否還查得到")
    for year in ("105", "110", "114"):
        html, elapsed = fetch("t163sb04", "sii", year, "01")
        title = re.search(r"<h2>(.*?)</h2>", html, re.S)
        has_data = "公司代號" in html
        print(
            f"  民國 {year} 年 Q1 → {len(html):,} 字元，{html.count('<tr')} 列，"
            f"有資料={has_data}，{elapsed:.1f}s，"
            f"標題：{title.group(1).strip() if title else '（無）'}"
        )


def probe_rate_limit() -> None:
    rule("階段 4：連續請求的節流需求")
    print("  連打 6 次（無間隔），觀察回應大小與耗時是否劣化：")
    for i in range(6):
        try:
            html, elapsed = fetch("t163sb04", "sii", "114", "01")
        except Exception as exc:  # noqa: BLE001
            print(f"    第 {i + 1} 次 ✗ {type(exc).__name__}: {exc}")
            break
        blocked = "公司代號" not in html
        print(
            f"    第 {i + 1} 次：{len(html):,} 字元，{elapsed:.1f}s"
            f"{'  ← 被擋' if blocked else ''}"
        )


def main() -> int:
    probe_structure()
    probe_annual_and_otc()
    probe_history_depth()
    probe_rate_limit()

    rule("結論")
    print("· 表頭與資料列 → 直接寫成 sources/mops.py 的欄位對應")
    print("· 表格數 > 1 表示分業別，解析器要逐表處理並以公司代號為鍵")
    print("· 節流間隔依階段 4 的實測結果設定於 sources.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
