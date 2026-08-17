"""00929 成分股名單解析。

規格第二節要求「每次執行分析時都必須確認最新成分股」，第二十一節要求
「資料不足必須明確標示，不得自行推測或編造」。因此這個模組的行為是：

1. 依 ``sources.yaml`` 設定的順序嘗試各來源，記錄實際命中的來源與 as-of 日期。
2. **全部失敗就中止整個流程並拋錯**，不會退回舊快取假裝成最新名單——
   用過期名單跑出來的 Dashboard 比沒有 Dashboard 更危險。
3. 人工名單超過 ``manual_max_age_days`` 天即視為過期並拒用。

**刻意不寫死檔數與名單**：公開資料對 00929 成分股數量的說法並不一致
（有 40 檔也有 50 檔的說法），任何寫死的名單都會在下次調整後變成錯誤資料。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import yaml

from ..models import DataPoint, utcnow
from .base import FetchError, HttpClient, SourceUnavailable, parse_number


class ConstituentsUnavailable(Exception):
    """所有成分股來源都失敗。刻意中止流程，不使用過期資料。"""


@dataclass
class Constituent:
    stock_id: str
    name: str
    weight: DataPoint
    market: str = "TWSE"
    shares: DataPoint | None = None

    def to_dict(self) -> dict:
        return {
            "stock_id": self.stock_id,
            "name": self.name,
            "weight": self.weight.to_dict(),
            "market": self.market,
        }


@dataclass
class ConstituentSet:
    """一次成功解析的成分股名單，附帶完整出處。"""

    constituents: list[Constituent]
    source: str
    as_of: date
    fetched_at: datetime = field(default_factory=utcnow)
    attempts: list[str] = field(default_factory=list)
    """依序記錄每個來源的嘗試結果，成功與失敗都留下，方便診斷。"""

    def __len__(self) -> int:
        return len(self.constituents)

    @property
    def stock_ids(self) -> list[str]:
        return [c.stock_id for c in self.constituents]

    def by_id(self) -> dict[str, Constituent]:
        return {c.stock_id: c for c in self.constituents}

    @property
    def total_weight(self) -> float | None:
        weights = [c.weight.value for c in self.constituents if c.weight.is_available]
        return sum(weights) if weights else None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "count": len(self.constituents),
            "total_weight": self.total_weight,
            "constituents": [c.to_dict() for c in self.constituents],
            "attempts": self.attempts,
        }


@dataclass
class ConstituentResolver:
    http: HttpClient
    config: dict
    repo_root: Path

    def resolve(self) -> ConstituentSet:
        attempts: list[str] = []
        providers = self.config.get("providers") or []

        handlers: dict[str, Callable[[dict], ConstituentSet]] = {
            "fuhwa_official": self._from_fuhwa,
            "manual": self._from_manual,
        }

        for provider in providers:
            name = provider.get("name")
            if not provider.get("enabled", True):
                attempts.append(f"{name}：已於設定中停用，略過")
                continue
            handler = handlers.get(name)
            if handler is None:
                attempts.append(f"{name}：無對應處理器，略過")
                continue
            try:
                result = handler(provider)
            except (SourceUnavailable, FetchError, ConstituentsUnavailable) as exc:
                attempts.append(f"{name}：失敗（{exc}）")
                continue
            if not result.constituents:
                attempts.append(f"{name}：回傳空名單，視為失敗")
                continue
            result.attempts = attempts + [f"{name}：成功，取得 {len(result)} 檔"]
            return result

        raise ConstituentsUnavailable(
            "無法取得 00929 成分股名單，已中止分析（不使用過期資料）。\n"
            + "\n".join(f"  - {a}" for a in attempts)
            + "\n\n請確認網路可連線至復華投信官網，"
            f"或在 {self.config.get('providers', [{}])[-1].get('path', 'data/manual/constituents.yaml')} "
            "填入最新名單與 as_of 日期。"
        )

    # ------------------------------------------------------------------
    # 官方來源
    # ------------------------------------------------------------------

    def _from_fuhwa(self, provider: dict) -> ConstituentSet:
        """復華投信官方持股 API——00929 成分股的權威來源。

        00929 的持股明細由**發行投信**（復華）公告，不是證交所。
        證交所 OpenAPI 的 143 個端點裡沒有任何 ETF 成分股資料
        （已比對 `成分`／`PCF`／`申購買回`／`holding`／`composition` 皆 0 筆）。

        官網頁面雖是 JS 渲染，但其前端呼叫的是一個純 JSON 端點，
        直接打這個端點即可，不需要瀏覽器自動化——少一個沉重相依，
        而且網站改版時 API 通常比 DOM 選擇器穩定。

        回應結構（實測確認）::

            result[0]
              etf002  "00929"          ← 用來確認抓到的是正確的基金
              ec038   追蹤指數名稱
              dDate   資料日期
              detail  [ {ftype, stockid, stockname, qshare,
                         mvalue, price, prate_addaccint}, … ]
              summary [ {ftype, totValue, totRatio}, … ]

        ``qDate`` 為必填（省略會回傳 HTML），且**非交易日回空結果**，
        因此需要往前回溯到最近一個有資料的日期。
        """
        url = provider["url"]
        fund_id = str(provider.get("fund_id", "ETF21"))
        expected_etf = str(provider.get("expected_etf_id", "00929"))
        lookback = int(provider.get("lookback_days", 10))

        tried: list[str] = []
        for offset in range(lookback + 1):
            as_of = date.today() - timedelta(days=offset)
            params = {"fundID": fund_id, "qDate": as_of.strftime("%Y/%m/%d")}

            payload = self.http.get_json(url, params=params)
            entry = _fuhwa_entry(payload)
            if entry is None:
                tried.append(as_of.isoformat())
                continue

            # 確認抓到的確實是 00929——fundID 是投信內部代號，
            # 萬一對應改變，這道檢查會擋下錯誤的基金。
            actual_etf = str(entry.get("etf002") or "").strip()
            if actual_etf and actual_etf != expected_etf:
                raise SourceUnavailable(
                    f"fundID={fund_id} 對應到的是 {actual_etf}，不是預期的 {expected_etf}；"
                    "請確認 sources.yaml 的 fund_id 設定"
                )

            return self._parse_fuhwa_entry(entry, as_of, url, provider)

        raise SourceUnavailable(
            f"往前回溯 {lookback} 天皆無持股資料（已試 {', '.join(tried[:5])}…）；"
            "可能是連續假期或端點結構已變更"
        )

    def _parse_fuhwa_entry(
        self, entry: dict, requested: date, url: str, provider: dict
    ) -> ConstituentSet:
        """把 detail 陣列轉成成分股清單。只取 ``ftype='股票'``。"""
        data_date = _parse_slash_date(entry.get("dDate")) or requested
        source = f"復華投信官方持股 API（fundID={provider.get('fund_id', 'ETF21')}）"

        constituents: list[Constituent] = []
        skipped: dict[str, int] = {}

        for row in entry.get("detail") or []:
            if not isinstance(row, dict):
                continue
            ftype = str(row.get("ftype") or "").strip()
            if ftype != "股票":
                # 期貨與現金部位也在 detail 裡，不是成分股。
                skipped[ftype or "未分類"] = skipped.get(ftype or "未分類", 0) + 1
                continue

            stock_id = str(row.get("stockid") or "").strip()
            if not _looks_like_stock_id(stock_id):
                skipped["代號格式不符"] = skipped.get("代號格式不符", 0) + 1
                continue

            # prate_addaccint 形如 "3.790%"，parse_number 會去掉 % 號。
            weight_pct = parse_number(row.get("prate_addaccint"))
            constituents.append(
                Constituent(
                    stock_id=stock_id,
                    name=str(row.get("stockname") or stock_id).strip(),
                    weight=DataPoint.of(
                        weight_pct / 100 if weight_pct is not None else None,
                        source,
                        as_of=data_date,
                        url=url,
                    ),
                    shares=DataPoint.of(
                        parse_number(row.get("qshare")), source, as_of=data_date
                    ),
                )
            )

        if not constituents:
            raise SourceUnavailable(
                f"{data_date} 的 detail 中沒有 ftype='股票' 的項目"
                + (f"（略過：{skipped}）" if skipped else "")
            )

        index_name = str(entry.get("ec038") or "").strip()
        note = f"，追蹤指數：{index_name}" if index_name else ""
        return ConstituentSet(
            constituents=constituents,
            source=f"{source}{note}",
            as_of=data_date,
        )

    # ------------------------------------------------------------------
    # 人工名單
    # ------------------------------------------------------------------

    def _from_manual(self, provider: dict) -> ConstituentSet:
        path = self.repo_root / provider.get("path", "data/manual/constituents.yaml")
        if not path.exists():
            raise SourceUnavailable(f"人工名單檔不存在：{path}")

        with path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        as_of = raw.get("as_of")
        if not isinstance(as_of, date):
            raise SourceUnavailable("人工名單缺少有效的 as_of 日期（格式 YYYY-MM-DD）")

        max_age = int(self.config.get("manual_max_age_days", 45))
        age_days = (date.today() - as_of).days
        if age_days > max_age:
            raise SourceUnavailable(
                f"人工名單已過期（as_of {as_of}，{age_days} 天前，上限 {max_age} 天）；"
                "請更新後再執行，避免以過時名單產生分析"
            )

        entries = raw.get("constituents") or []
        if not entries:
            raise SourceUnavailable("人工名單為空")

        constituents = []
        for entry in entries:
            stock_id = str(entry.get("stock_id", "")).strip()
            if not stock_id:
                continue
            weight = entry.get("weight")
            constituents.append(
                Constituent(
                    stock_id=stock_id,
                    name=str(entry.get("name", stock_id)),
                    market=str(entry.get("market", "TWSE")),
                    weight=DataPoint.of(
                        float(weight) if weight is not None else None,
                        f"manual:{path.name}",
                        as_of=as_of,
                        url=str(raw.get("source_note", "")) or None,
                    ),
                )
            )
        return ConstituentSet(
            constituents=constituents,
            source=f"人工名單 {path.name}（來源註記：{raw.get('source_note', '未填')}）",
            as_of=as_of,
        )


# --------------------------------------------------------------------------
# 名單異動比較（規格第二節：成分股異動／新增／刪除）
# --------------------------------------------------------------------------


@dataclass
class ConstituentChanges:
    added: list[Constituent] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    weight_changes: list[dict] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.weight_changes)

    def to_dict(self) -> dict:
        return {
            "added": [c.to_dict() for c in self.added],
            "removed": self.removed,
            "weight_changes": self.weight_changes,
        }


def diff_constituents(
    current: ConstituentSet,
    previous: dict | None,
    *,
    weight_change_threshold: float = 0.005,
) -> ConstituentChanges:
    """與上次快照比較成分股異動。"""
    changes = ConstituentChanges()
    if not previous:
        return changes

    prev_entries = {
        entry["stock_id"]: entry for entry in previous.get("constituents", []) if entry.get("stock_id")
    }
    current_by_id = current.by_id()

    for stock_id, constituent in current_by_id.items():
        if stock_id not in prev_entries:
            changes.added.append(constituent)
            continue
        prev_weight = (prev_entries[stock_id].get("weight") or {}).get("value")
        curr_weight = constituent.weight.value
        if prev_weight is None or curr_weight is None:
            continue
        delta = curr_weight - prev_weight
        if abs(delta) >= weight_change_threshold:
            changes.weight_changes.append(
                {
                    "stock_id": stock_id,
                    "name": constituent.name,
                    "previous": prev_weight,
                    "current": curr_weight,
                    "delta": delta,
                }
            )

    for stock_id, entry in prev_entries.items():
        if stock_id not in current_by_id:
            changes.removed.append({"stock_id": stock_id, "name": entry.get("name", stock_id)})

    return changes


# --------------------------------------------------------------------------
# 解析輔助
# --------------------------------------------------------------------------


def _fuhwa_entry(payload: Any) -> dict | None:
    """取出復華回應的第一筆 result，並確認它含有持股明細。

    非交易日會回 ``{"result": []}`` 或 ``dDate: null``，
    兩者都視為「這天沒有資料」，由呼叫端往前再試一天。
    """
    if not isinstance(payload, dict):
        return None
    outer = payload.get("result")
    if not isinstance(outer, list) or not outer:
        return None
    entry = outer[0]
    if not isinstance(entry, dict):
        return None
    if not entry.get("dDate"):
        return None
    detail = entry.get("detail")
    if not isinstance(detail, list) or not detail:
        return None
    return entry


def _parse_slash_date(value: Any) -> date | None:
    """解析 ``2026/08/17`` 格式的日期。"""
    if not value:
        return None
    parts = str(value).strip().split("/")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return None


def _looks_like_stock_id(text: str) -> bool:
    """台股代號為 4–6 位數字（含興櫃與特別股代號）。"""
    stripped = text.strip()
    return stripped.isdigit() and 4 <= len(stripped) <= 6


__all__ = [
    "Constituent",
    "ConstituentChanges",
    "ConstituentResolver",
    "ConstituentSet",
    "ConstituentsUnavailable",
    "diff_constituents",
]
