"""期別排程與快取的行為測試。

這兩者一起決定「哪些資料可以重複使用」，判斷錯的後果不對稱：
多抓幾次只是慢，把還會變動的數字永久快取則會讓過時資料被當成最新資料。
"""

from __future__ import annotations

import time
from datetime import date

import pytest

from buffett00929.models import FiscalPeriod
from buffett00929.sources.cache import DiskCache
from buffett00929.sources.periods import (
    filing_deadline,
    from_roc,
    is_published,
    is_settled,
    periods_to_fetch,
    to_roc,
)


class TestFilingDeadlines:
    @pytest.mark.parametrize(
        "period,expected",
        [
            (FiscalPeriod(2025, 1), date(2025, 5, 15)),
            (FiscalPeriod(2025, 2), date(2025, 8, 14)),
            (FiscalPeriod(2025, 3), date(2025, 11, 14)),
            (FiscalPeriod(2025, 0), date(2026, 3, 31)),
            # 第四季不單獨申報，併入年報。
            (FiscalPeriod(2025, 4), date(2026, 3, 31)),
        ],
    )
    def test_statutory_deadlines(self, period, expected):
        assert filing_deadline(period) == expected

    def test_published_only_after_the_deadline(self):
        q2 = FiscalPeriod(2026, 2)
        assert not is_published(q2, date(2026, 8, 13))
        assert is_published(q2, date(2026, 8, 14))

    def test_unpublished_periods_are_never_fetched(self):
        """抓還沒公告的期別只會拿到空結果，然後被誤記成「這家公司缺料」。"""
        periods = periods_to_fetch(date(2026, 8, 17), years=1)
        assert FiscalPeriod(2026, 3) not in periods  # 11/14 才到期
        assert FiscalPeriod(2026, 0) not in periods  # 年報要等 2027/3/31
        assert FiscalPeriod(2026, 2) in periods


class TestSettlement:
    """定案＝可永久快取。刻意保守：季報會在年度查核完成時被追溯調整。"""

    def test_recent_period_is_not_settled(self):
        assert not is_settled(FiscalPeriod(2026, 2), date(2026, 8, 17))

    def test_last_year_is_not_settled_yet(self):
        # 2025 會計年度結束後未滿 15 個月，年報數字仍可能更正重編。
        assert not is_settled(FiscalPeriod(2025, 1), date(2026, 8, 17))

    def test_old_period_is_settled(self):
        assert is_settled(FiscalPeriod(2020, 3), date(2026, 8, 17))

    def test_settlement_boundary_is_exactly_fifteen_months(self):
        period = FiscalPeriod(2024, 1)  # 年度結束 2024-12-31
        assert not is_settled(period, date(2026, 3, 30))
        assert is_settled(period, date(2026, 3, 31))


class TestRocConversion:
    def test_round_trip(self):
        assert to_roc(2026) == 115
        assert from_roc(115) == 2026
        assert from_roc("115") == 2026


class TestOrdering:
    def test_periods_run_oldest_to_newest(self):
        periods = periods_to_fetch(date(2026, 8, 17), years=3)
        assert periods == sorted(periods, key=lambda p: (p.year, 4 if p.is_annual else p.quarter))

    def test_annual_sorts_after_the_third_quarter_of_the_same_year(self):
        periods = periods_to_fetch(date(2026, 8, 17), years=3)
        same_year = [p for p in periods if p.year == 2025]
        assert same_year == [
            FiscalPeriod(2025, 1),
            FiscalPeriod(2025, 2),
            FiscalPeriod(2025, 3),
            FiscalPeriod(2025, 0),
        ]


class TestRequestBudget:
    """請求數直接決定會不會被擋，所以取捨要被測試釘住。"""

    def test_old_years_contribute_only_annual_figures(self):
        periods = periods_to_fetch(date(2026, 8, 17), years=10, quarterly_years=2)
        old = [p for p in periods if p.year == 2018]
        assert old == [FiscalPeriod(2018, 0)]

    def test_recent_years_keep_their_quarters(self):
        periods = periods_to_fetch(date(2026, 8, 17), years=10, quarterly_years=2)
        assert FiscalPeriod(2025, 2) in periods
        assert FiscalPeriod(2026, 2) in periods

    def test_a_decade_stays_within_a_sane_request_budget(self):
        """三張表 × 兩個市場 × 期別數。超過幾百次就該重新設計，不是調參數。"""
        periods = periods_to_fetch(date(2026, 8, 17), years=10, quarterly_years=2)
        assert len(periods) * 3 * 2 < 150


class TestCacheTtl:
    def test_expired_current_period_entry_is_not_returned(self, tmp_path):
        """規格第二節：過期就是過期，不能拿舊資料頂替。"""
        cache = DiskCache(tmp_path, ttl_hours=0)
        cache.set("http://x/latest", {"q": 1}, {"value": 42})
        time.sleep(0.01)
        assert cache.get("http://x/latest", {"q": 1}) is None

    def test_fresh_entry_is_returned(self, tmp_path):
        cache = DiskCache(tmp_path, ttl_hours=12)
        cache.set("http://x/latest", None, {"value": 42})
        entry = cache.get("http://x/latest", None)
        assert entry is not None and entry.payload == {"value": 42}


class TestImmutableCache:
    def test_settled_period_survives_ttl_expiry(self, tmp_path):
        """已定案的期別不該每天重抓——那是被來源端擋掉的最快方式。"""
        cache = DiskCache(tmp_path, ttl_hours=0)
        cache.set("http://x/2020Q1", {"p": "2020Q1"}, {"rows": 3}, immutable=True)
        entry = cache.get("http://x/2020Q1", {"p": "2020Q1"}, immutable=True)
        assert entry is not None and entry.payload == {"rows": 3}

    def test_an_entry_written_as_current_never_becomes_immutable(self, tmp_path):
        """呼叫端事後改口說「這期已定案」，不能讓過期的當期資料復活。"""
        cache = DiskCache(tmp_path, ttl_hours=0)
        cache.set("http://x/latest", None, {"rows": 1})  # 未宣告 immutable
        assert cache.get("http://x/latest", None, immutable=True) is None

    def test_namespace_separates_history_from_daily_churn(self, tmp_path):
        cache = DiskCache(tmp_path, ttl_hours=12)
        cache.set("http://x/a", None, {"n": 1}, namespace="mops")
        assert (tmp_path / "mops").is_dir()
        # 同一把鍵在不同 namespace 下互不干擾。
        assert cache.get("http://x/a", None) is None
        assert cache.get("http://x/a", None, namespace="mops").payload == {"n": 1}

    def test_namespace_cannot_escape_the_cache_directory(self, tmp_path):
        cache = DiskCache(tmp_path, ttl_hours=12)
        cache.set("http://x/a", None, {"n": 1}, namespace="../../etc")
        assert cache.get("http://x/a", None, namespace="../../etc").payload == {"n": 1}
        assert not (tmp_path.parent.parent / "etc").exists()


class TestHttpClientPost:
    def test_post_and_get_do_not_share_a_cache_entry(self, tmp_path):
        """同一個 URL 的 GET 與 POST 是不同請求，快取不能混在一起。"""
        from buffett00929.sources.base import HttpClient

        cache = DiskCache(tmp_path, ttl_hours=12)
        client = HttpClient(cache=cache)

        # 預先寫入兩筆，若快取鍵沒有區分 method，第二筆會蓋掉第一筆。
        cache.set("http://x/api", {"a": 1}, {"via": "get"})
        cache.set("http://x/api", {"_method": "POST", "a": 1}, {"via": "post"})

        assert client.get_json("http://x/api", {"a": 1}) == {"via": "get"}
        assert client.post_json("http://x/api", {"a": 1}) == {"via": "post"}

    def test_throttle_waits_between_real_requests(self):
        from buffett00929.sources.base import HttpClient

        client = HttpClient(min_interval_seconds=0.05)
        started = time.monotonic()
        client._throttle()  # 第一次不等
        client._throttle()  # 第二次要等滿間隔
        assert time.monotonic() - started >= 0.05

    def test_cache_hits_are_not_throttled(self, tmp_path):
        """節流是為了保護來源端；沒送出請求就不該付出等待成本。"""
        from buffett00929.sources.base import HttpClient

        cache = DiskCache(tmp_path, ttl_hours=12)
        cache.set("http://x/api", None, {"cached": True})
        client = HttpClient(cache=cache, min_interval_seconds=5)

        started = time.monotonic()
        assert client.get_json("http://x/api") == {"cached": True}
        assert time.monotonic() - started < 1
