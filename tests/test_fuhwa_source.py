"""復華持股 API 解析。

以下 fixture 的**結構與欄位名稱**取自 2026-08-17 對正式端點的實測回應
（見 `scripts/probe_constituent_sources.py` 的探測結果），
數值則經過縮減以保持測試可讀。結構是真的，數字是簡化的。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from buffett00929.sources.base import SourceUnavailable
from buffett00929.sources.constituents import ConstituentResolver, ConstituentsUnavailable

PROVIDER = {
    "name": "fuhwa_official",
    "enabled": True,
    "url": "https://www.fhtrust.com.tw/api/assets",
    "fund_id": "ETF21",
    "expected_etf_id": "00929",
    "lookback_days": 5,
}


def make_payload(
    *,
    data_date: str | None = "2026/08/17",
    etf_id: str = "00929",
    detail: list[dict] | None = None,
) -> dict:
    if detail is None:
        detail = [
            {
                "ftype": "股票",
                "stockid": "2357",
                "stockname": "華碩電腦",
                "qshare": "5,792,000",
                "mvalue": "5,432,896,000",
                "price": "938.0000",
                "prate_addaccint": "3.790%",
            },
            {
                "ftype": "股票",
                "stockid": "5347",
                "stockname": "世界先進",
                "qshare": "12,000,000",
                "mvalue": "1,800,000,000",
                "price": "150.0000",
                "prate_addaccint": "1.256%",
            },
            # 期貨與現金也在 detail 裡，必須被排除。
            {
                "ftype": "期貨",
                "stockid": "TXF",
                "stockname": "臺股期貨",
                "qshare": "100",
                "prate_addaccint": "0.500%",
            },
            {
                "ftype": "現金",
                "stockid": "",
                "stockname": "現金餘額",
                "prate_addaccint": "2.620%",
            },
        ]
    return {
        "result": [
            {
                "fundID": "ETF21",
                "twNameFull": "復華台灣科技優息ETF基金",
                "etf002": etf_id,
                "ec038": "臺灣指數公司特選臺灣上市上櫃科技優息指數",
                "dDate": data_date,
                "pcf_FundNav": "143,340,322,172",
                "detail": detail,
                "summary": [{"ftype": "股票", "totValue": "136,410,994,724", "totRatio": "95.163%"}],
            }
        ],
        "status": 0,
    }


class FakeHttp:
    """依 qDate 回傳預先安排好的回應，用來驗證回溯邏輯。"""

    def __init__(self, by_date: dict[str, dict]):
        self.by_date = by_date
        self.requested: list[str] = []

    def get_json(self, url, params=None, headers=None, use_cache=True):
        qdate = (params or {}).get("qDate", "")
        self.requested.append(qdate)
        return self.by_date.get(qdate, {"result": [], "status": 0})


def resolver_for(http, tmp_path: Path) -> ConstituentResolver:
    return ConstituentResolver(
        http=http,
        config={"providers": [PROVIDER], "manual_max_age_days": 45},
        repo_root=tmp_path,
    )


class TestParsing:
    def test_extracts_only_equity_holdings(self, tmp_path):
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({today: make_payload(data_date=today)})
        result = resolver_for(http, tmp_path).resolve()

        assert len(result) == 2
        assert result.stock_ids == ["2357", "5347"]

    def test_weight_converted_from_percent_string(self, tmp_path):
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({today: make_payload(data_date=today)})
        result = resolver_for(http, tmp_path).resolve()

        assert result.by_id()["2357"].weight.value == pytest.approx(0.03790)
        assert result.by_id()["5347"].weight.value == pytest.approx(0.01256)

    def test_shares_parsed_with_thousand_separators(self, tmp_path):
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({today: make_payload(data_date=today)})
        result = resolver_for(http, tmp_path).resolve()
        assert result.by_id()["2357"].shares.value == pytest.approx(5_792_000)

    def test_as_of_comes_from_response_not_request(self, tmp_path):
        """資料日期要用回應裡的 dDate，不是我們請求的日期。"""
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({today: make_payload(data_date="2026/08/14")})
        result = resolver_for(http, tmp_path).resolve()
        assert result.as_of == date(2026, 8, 14)

    def test_source_records_tracked_index(self, tmp_path):
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({today: make_payload(data_date=today)})
        result = resolver_for(http, tmp_path).resolve()
        assert "復華投信官方持股 API" in result.source
        assert "科技優息指數" in result.source

    def test_weights_carry_provenance(self, tmp_path):
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({today: make_payload(data_date=today)})
        result = resolver_for(http, tmp_path).resolve()
        weight = result.by_id()["2357"].weight
        assert weight.provenance is not None
        assert "fhtrust" in (weight.provenance.url or "")


class TestTradingDayLookback:
    def test_walks_back_to_the_last_day_with_data(self, tmp_path):
        """非交易日回空，必須自動往前找。"""
        target = date.today() - timedelta(days=3)
        http = FakeHttp({target.strftime("%Y/%m/%d"): make_payload(data_date=target.strftime("%Y/%m/%d"))})
        result = resolver_for(http, tmp_path).resolve()

        assert len(result) == 2
        assert result.as_of == target
        # 前三天各試一次後才命中。
        assert len(http.requested) == 4

    def test_null_ddate_is_treated_as_no_data(self, tmp_path):
        """未來日期會回 dDate=null 但 HTTP 200，不能當成有效資料。"""
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({today: make_payload(data_date=None)})
        with pytest.raises(ConstituentsUnavailable):
            resolver_for(http, tmp_path).resolve()

    def test_gives_up_after_lookback_window(self, tmp_path):
        http = FakeHttp({})
        with pytest.raises(ConstituentsUnavailable) as excinfo:
            resolver_for(http, tmp_path).resolve()
        assert "回溯" in str(excinfo.value)
        assert len(http.requested) == PROVIDER["lookback_days"] + 1


class TestFundIdentityGuard:
    def test_wrong_fund_aborts_rather_than_analysing_it(self, tmp_path):
        """fundID 是投信內部代號，對應變動時寧可報錯也不要分析錯的基金。"""
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({today: make_payload(data_date=today, etf_id="00940")})
        with pytest.raises(ConstituentsUnavailable) as excinfo:
            resolver_for(http, tmp_path).resolve()
        assert "00940" in str(excinfo.value)

    def test_no_equity_rows_is_a_failure(self, tmp_path):
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({
            today: make_payload(
                data_date=today,
                detail=[{"ftype": "現金", "stockid": "", "prate_addaccint": "100%"}],
            )
        })
        with pytest.raises(ConstituentsUnavailable):
            resolver_for(http, tmp_path).resolve()


class TestTotals:
    def test_total_weight_reflects_equity_share_of_fund(self, tmp_path):
        """權重是佔基金淨值比例，股票合計不會是 100%（其餘為現金與期貨）。"""
        today = date.today().strftime("%Y/%m/%d")
        http = FakeHttp({today: make_payload(data_date=today)})
        result = resolver_for(http, tmp_path).resolve()
        assert result.total_weight == pytest.approx(0.05046)
        assert result.total_weight < 1.0
