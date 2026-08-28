#!/usr/bin/env python3
"""探測櫃買中心有沒有本益比／股價淨值比／殖利率的端點。

**這是一次性的診斷工具，不是正式流程的一部分。**

## 為什麼需要它

`config/sources.yaml` 裡證交所有 valuation 端點：

    twse.endpoints.valuation: "/exchangeReport/BWIBBU_ALL"   本益比／淨值比／殖利率

**櫃買中心沒有對應的設定**，所以所有上櫃公司這三個指標一律「資料不足」。

實測（2026-08-28 全市場掃描）：達爾膚 6523 是上櫃，修好交易所路由之後
產業別與市場別都對了，本益比、股價淨值比、殖利率仍然是資料不足——
因為根本沒有來源可問，不是問錯地方。

## 要回答的問題

1. 櫃買 OpenAPI 有沒有提供這三個欄位的端點？叫什麼名字？
2. 欄位名稱是什麼？（決定 `twse.py` 的對應表要不要加別名）
3. 回應是不是**全市場一次給完**？（決定是否符合整批取用的成本模型；
   逐檔查詢就不能用在 1,975 家的掃描上）

## 用法

開發環境連不上台股任何來源（一律 403），必須在網路可通的環境執行：

    Actions → Probe constituent sources → target: tpex-valuation
"""

from __future__ import annotations

import json
import sys
from typing import Any

BASE = "https://www.tpex.org.tw/openapi/v1"

HEADERS = {
    "User-Agent": "buffett00929-probe/0.1 (+https://github.com/blair1024-glitch/Indexing)",
    "Accept": "application/json",
}

# 這三個是目標。中英文都列，因為櫃買的欄位命名兩種都出現過。
WANTED = ("本益比", "PERatio", "PER", "淨值比", "PBRatio", "PBR", "殖利率", "DividendYield")

# 端點名稱／描述裡出現這些字就值得看一眼。
HINTS = ("peratio", "per", "pbr", "yield", "本益比", "殖利率", "淨值比", "價值", "valuation")

SAMPLE_STOCK = "6523"
"""達爾膚。上櫃，且是目前掃描的 BUY 候選之一，拿它驗證最直接。"""


def fetch(label: str, url: str) -> Any:
    import requests

    print(f"\n--- {label}\n    {url}")
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
    except Exception as exc:  # noqa: BLE001 - 探測腳本，任何失敗都只是一筆結果
        print(f"    ✗ 連線失敗：{exc}")
        return None
    print(f"    HTTP {response.status_code}　{len(response.content):,} bytes")
    if response.status_code != 200:
        print(f"    ✗ 非 200：{response.text[:200]}")
        return None
    try:
        return response.json()
    except ValueError:
        print(f"    ✗ 非 JSON，前 200 字元：{response.text[:200]}")
        return None


def find_spec() -> dict | None:
    """找 OpenAPI/Swagger 規格。櫃買沒有公開文件說它在哪，所以逐一試。"""
    for path in (
        "https://www.tpex.org.tw/openapi/swagger.json",
        "https://www.tpex.org.tw/openapi/v1/swagger.json",
        "https://www.tpex.org.tw/openapi/doc/swagger.json",
        "https://www.tpex.org.tw/openapi/v1/openapi.json",
    ):
        spec = fetch("尋找 OpenAPI 規格", path)
        if isinstance(spec, dict) and spec.get("paths"):
            return spec
    return None


def describe(rows: list, endpoint: str) -> None:
    """列出欄位名稱、筆數，以及樣本股是否在裡面。

    筆數是關鍵：全市場一次給完才符合整批取用的成本模型。
    只回單一檔的端點對 1,975 家的掃描沒有用。
    """
    print(f"    筆數 {len(rows):,}")
    if not rows or not isinstance(rows[0], dict):
        print("    ✗ 不是物件陣列，略過")
        return

    fields = list(rows[0].keys())
    print(f"    欄位（{len(fields)}）：{'、'.join(fields)}")

    hits = [f for f in fields if any(w.lower() in f.lower() for w in WANTED)]
    print(f"    命中目標欄位：{'、'.join(hits) if hits else '（無）'}")

    sample = next(
        (
            r
            for r in rows
            if SAMPLE_STOCK in str(r.get("SecuritiesCompanyCode") or r.get("Code") or
                                   r.get("公司代號") or r.get("股票代號") or "")
        ),
        None,
    )
    if sample:
        print(f"    {SAMPLE_STOCK} 樣本：{json.dumps(sample, ensure_ascii=False)[:400]}")
    else:
        print(f"    （{SAMPLE_STOCK} 不在這份資料裡）")


def main() -> int:
    print("=" * 72)
    print("櫃買中心估值指標端點探測")
    print("=" * 72)

    spec = find_spec()
    candidates: list[tuple[str, str]] = []

    if spec:
        paths = spec.get("paths") or {}
        print(f"\n✓ 找到規格，共 {len(paths)} 個端點")
        for path, item in paths.items():
            text = json.dumps(item, ensure_ascii=False).lower() + path.lower()
            if any(h in text for h in HINTS):
                summary = ""
                for method in item.values():
                    if isinstance(method, dict) and method.get("summary"):
                        summary = method["summary"]
                        break
                candidates.append((path, summary))
        print(f"  其中 {len(candidates)} 個名稱或描述與估值指標相關：")
        for path, summary in candidates:
            print(f"    {path}　{summary}")
    else:
        print("\n✗ 找不到 OpenAPI 規格，改用候選名稱盲試")
        # 依證交所命名習慣與櫃買既有端點推測。猜不中不代表沒有，
        # 只代表要換個方式找（例如從櫃買網站的資料開放頁面抄）。
        candidates = [
            (f"/{name}", "（盲試）")
            for name in (
                "tpex_mainboard_peratio_analysis",
                "tpex_mainboard_daily_close_quotes",
                "mopsfin_t187ap14_O",
                "tpex_esb_latest_statistics",
                "peratio_analysis",
            )
        ]

    if not candidates:
        print("\n沒有候選端點可試。")
        return 1

    print("\n" + "=" * 72)
    print("逐一取樣")
    print("=" * 72)

    for path, summary in candidates:
        url = path if path.startswith("http") else f"{BASE}{path}"
        data = fetch(f"{path}　{summary}", url)
        if isinstance(data, list):
            describe(data, path)
        elif isinstance(data, dict):
            print(f"    物件回應，鍵：{list(data)[:20]}")

    print("\n" + "=" * 72)
    print("判讀方式")
    print("=" * 72)
    print("· 有端點同時給本益比／淨值比／殖利率，且筆數是全市場等級（數百～上千）")
    print("  → 把它填進 config/sources.yaml 的 tpex.endpoints.valuation，")
    print("     並確認 twse.py 的欄位對應表認得它的欄位名稱")
    print("· 只有部分欄位 → 也值得接，缺的那項照常標示資料不足")
    print("· 筆數只有個位數或需要帶股號 → 逐檔查詢，不能用在 1,975 家的掃描上")
    print("· 全部落空 → 上櫃公司這三個指標確實無官方免金鑰來源，")
    print("     維持標示資料不足即可，不要拿證交所的數字冒充")
    return 0


if __name__ == "__main__":
    sys.exit(main())
