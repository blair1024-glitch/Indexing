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
from datetime import date, datetime
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
            "twse_pcf": self._from_twse_pcf,
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
            + "\n\n請確認網路可連線至證交所／復華投信，"
            f"或在 {self.config.get('providers', [{}])[-1].get('path', 'data/manual/constituents.yaml')} "
            "填入最新名單與 as_of 日期。"
        )

    # ------------------------------------------------------------------
    # 官方來源
    # ------------------------------------------------------------------

    def _from_twse_pcf(self, provider: dict) -> ConstituentSet:
        """證交所 ETF 申購買回清單（PCF）——官方每日揭露的實際持股籃。"""
        payload = self.http.get_json(provider["url"], params=provider.get("params"))
        records = _extract_records(payload)
        if not records:
            raise SourceUnavailable("PCF 回應中找不到持股明細")

        constituents = []
        for record in records:
            stock_id = _first_str(record, ("股票代號", "stockNo", "code", "Code", "成分股代號"))
            name = _first_str(record, ("股票名稱", "stockName", "name", "Name", "成分股名稱"))
            if not stock_id or not _looks_like_stock_id(stock_id):
                continue
            weight_raw = _first_value(record, ("權重", "weight", "比重", "持股權重(%)"))
            shares_raw = _first_value(record, ("股數", "shares", "申購股數", "持股股數"))
            weight = parse_number(weight_raw)
            constituents.append(
                Constituent(
                    stock_id=stock_id,
                    name=name or stock_id,
                    # 官方以百分比揭露，轉為小數。
                    weight=DataPoint.of(
                        weight / 100 if weight is not None else None,
                        "TWSE:etfPCF",
                        as_of=date.today(),
                        url=provider["url"],
                    ),
                    shares=DataPoint.of(parse_number(shares_raw), "TWSE:etfPCF", as_of=date.today()),
                )
            )
        if not constituents:
            raise SourceUnavailable("PCF 回應中沒有可辨識的成分股代號")
        return ConstituentSet(
            constituents=constituents, source="TWSE:etfPCF（證交所申購買回清單）", as_of=date.today()
        )

    def _from_fuhwa(self, provider: dict) -> ConstituentSet:
        """復華投信官網。

        該頁面為 JavaScript 動態載入，純 HTTP 取回的 HTML 通常不含持股表格。
        這裡明確以失敗回報並說明原因，而不是回傳一份殘缺名單。
        """
        raise SourceUnavailable(
            "復華官網 00929 頁面為 JS 動態渲染，純 HTTP 無法取得持股表；"
            "如需啟用請改用瀏覽器自動化，或使用人工名單"
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


def _extract_records(payload: Any) -> list[dict]:
    """從各種可能的回應包裝中取出記錄陣列。

    證交所的 JSON 端點包裝方式並不統一（``data`` / ``result`` / 直接陣列），
    因此逐一嘗試而不是假設單一結構。
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "result", "aaData", "records", "constituents", "holdings"):
            value = payload.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        # 部分端點回傳 fields + data 的二維陣列組合。
        fields = payload.get("fields")
        rows = payload.get("data")
        if isinstance(fields, list) and isinstance(rows, list) and rows:
            if isinstance(rows[0], list):
                return [dict(zip(fields, row)) for row in rows if isinstance(row, list)]
    return []


def _first_str(record: dict, keys: tuple[str, ...]) -> str | None:
    value = _first_value(record, keys)
    return str(value).strip() if value not in (None, "") else None


def _first_value(record: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
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
