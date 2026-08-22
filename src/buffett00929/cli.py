"""命令列介面。

    buffett00929 update          抓取最新資料，重新評分並產生 Dashboard 與報表
    buffett00929 demo            用合成資料產生 Dashboard（不需網路，驗證版面與計算）
    buffett00929 verify-sources  檢查各資料來源可否連線，並列出實際回傳的科目名稱
    buffett00929 check-config    驗證設定檔與指標實作是否一致
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .config import Config, ConfigError, REPO_ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="buffett00929",
        description="00929 巴菲特式 3M 選股與財報即時追蹤系統",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=None, help="設定檔目錄（預設 config/）"
    )
    parser.add_argument(
        "--repo-root", type=Path, default=REPO_ROOT, help="專案根目錄（輸出位置的基準）"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("update", help="抓取最新資料並產生完整分析")
    sub.add_parser("demo", help="以合成資料產生 Dashboard（不需網路）")
    verify = sub.add_parser("verify-sources", help="檢查資料來源可用性")
    verify.add_argument(
        "--stock-id", default="2330", help="用於測試 FinMind 科目對應的股票代號"
    )
    sub.add_parser("check-config", help="驗證設定檔與指標實作一致性")
    stock = sub.add_parser("analyse", help="對任意上市櫃股號跑一次巴菲特分析")
    stock.add_argument("stock_id", help="股票代號，例如 2330")

    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config_dir)
    except ConfigError as exc:
        print(f"設定檔錯誤：{exc}", file=sys.stderr)
        return 2

    handlers = {
        "update": _cmd_update,
        "demo": _cmd_demo,
        "verify-sources": _cmd_verify_sources,
        "check-config": _cmd_check_config,
        "analyse": _cmd_analyse,
    }
    return handlers[args.command](config, args)


# --------------------------------------------------------------------------
# 指令
# --------------------------------------------------------------------------


def _cmd_analyse(config: Config, args) -> int:
    """單檔查詢。與每日更新走同一條流程、同一套門檻。"""
    from .pipeline import analyse_stock
    from .report.markdown import render_company
    from .sources.base import SourceUnavailable

    repo_root: Path = args.repo_root
    stock_id = str(args.stock_id).strip()
    if not stock_id.isdigit():
        print(f"股票代號應為數字：{stock_id}", file=sys.stderr)
        return 2

    try:
        result, run = analyse_stock(stock_id, config, repo_root)
    except SourceUnavailable as exc:
        print(f"\n資料來源不可用，無法分析 {stock_id}：\n\n{exc}\n", file=sys.stderr)
        return 3

    score = result.score
    if score.scorable_max == 0:
        print(
            f"\n找不到 {stock_id} 的任何財報資料。"
            "請確認代號正確、且該公司有在公開資訊觀測站申報。\n",
            file=sys.stderr,
        )
        return 4

    path = repo_root / "reports" / "lookup" / f"{stock_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_company(result, run), encoding="utf-8")

    company = result.company
    print(f"\n{company.name}（{company.stock_id}）")
    print(f"  Buffett Score  {score.total_score:.1f} / {score.scorable_max:.0f} 可評分"
          f"　等級 {score.grade}")
    mos = score.valuation.margin_of_safety
    print(f"  安全邊際       {mos.value:+.1%}" if mos.is_available
          else f"  安全邊際       資料不足（{mos.unavailable_reason}）")
    print(f"  投資判斷       {result.verdict}")
    print(f"    理由：{result.verdict_reason}")
    print(f"  7 年持有測試   {result.seven_year}")
    if score.red_flags.triggered:
        print(f"  紅旗           重大 {score.red_flags.critical_count}、"
              f"警告 {score.red_flags.warning_count}")
    for warning in run.warnings[:3]:
        print(f"  ⚠ {warning}")
    print(f"\n完整報表：{path.relative_to(repo_root)}\n")
    return 0


def _cmd_update(config: Config, args) -> int:
    from .pipeline import run_analysis
    from .report.dashboard import write_dashboard
    from .report.markdown import write_reports
    from .snapshots import save_snapshot
    from .sources.constituents import ConstituentsUnavailable

    repo_root: Path = args.repo_root
    try:
        run = run_analysis(config, repo_root)
    except ConstituentsUnavailable as exc:
        # 成分股名單是整個分析的前提，拿不到就中止——不用過期名單充數。
        print(f"\n無法取得成分股名單，已中止分析：\n\n{exc}\n", file=sys.stderr)
        return 3

    snapshot_path = save_snapshot(repo_root, run.to_snapshot())
    dashboard_path = write_dashboard(run, repo_root)
    report_paths = write_reports(run, repo_root)

    _print_run_summary(run)
    print(f"\n快照：      {snapshot_path.relative_to(repo_root)}")
    print(f"Dashboard： {dashboard_path.relative_to(repo_root)}")
    print(f"報表：      {len(report_paths)} 個檔案於 reports/")
    return 0


def _cmd_demo(config: Config, args) -> int:
    from .demo import build_demo_run
    from .report.dashboard import write_dashboard
    from .report.markdown import write_reports

    repo_root: Path = args.repo_root
    run = build_demo_run(config)

    dashboard_path = write_dashboard(run, repo_root)
    report_paths = write_reports(run, repo_root)

    print("⚠️  合成範例資料——所有數字均非真實財報，僅供版面與計算邏輯驗證。\n")
    _print_run_summary(run)
    print(f"\nDashboard： {dashboard_path.relative_to(repo_root)}")
    print(f"報表：      {len(report_paths)} 個檔案於 reports/")
    # demo 刻意不寫快照，避免合成分數污染真實的評分變化歷史。
    print("\n（demo 不寫入快照，以免合成分數混入真實評分歷史）")
    return 0


def _cmd_verify_sources(config: Config, args) -> int:
    """逐一檢查來源，並把 FinMind 實際回傳的科目名稱列出來。

    FinMind 的科目命名若有調整，這裡會顯示未對應的科目，
    可據以修正 ``sources/finmind.py`` 的對應表——不必逐一猜測。
    """
    from .loader import DataLoader
    from .sources.base import FetchError, SourceUnavailable
    from .sources.constituents import ConstituentsUnavailable

    loader = DataLoader(config=config, repo_root=args.repo_root)
    failures = 0

    print("=== 成分股名單 ===")
    try:
        constituents = loader.load_constituents()
    except ConstituentsUnavailable as exc:
        print(f"  ✗ 失敗\n{exc}\n")
        failures += 1
    else:
        print(f"  ✓ {constituents.source}｜{len(constituents)} 檔｜資料日期 {constituents.as_of}")
        for attempt in constituents.attempts:
            print(f"    · {attempt}")

    stock_id = args.stock_id
    print(f"\n=== 證交所 OpenAPI（以 {stock_id} 測試）===")
    for label, fetch in (
        ("綜合損益表", lambda: loader.twse.income_statement(stock_id)),
        ("資產負債表", lambda: loader.twse.balance_sheet(stock_id)),
        ("月營收", lambda: loader.twse.monthly_revenue(stock_id)),
        ("市場資料", lambda: loader.twse.market_data(stock_id)),
    ):
        try:
            result = fetch()
        except (SourceUnavailable, FetchError) as exc:
            print(f"  ✗ {label}：{exc}")
            failures += 1
        else:
            print(f"  {'✓' if result else '·'} {label}：{'取得資料' if result else '該代號無資料'}")

    if loader.twse.schema_watch.has_issues:
        print("\n  ⚠ 欄位對應異常（API schema 可能已變動）：")
        for issue in loader.twse.schema_watch.unknown_fields:
            print(f"    · {issue}")

    print(f"\n=== FinMind（以 {stock_id} 測試）===")
    if not loader.finmind.is_available:
        print(f"  · 略過：{loader.finmind.unavailable_reason}")
        print("    設定 FINMIND_TOKEN 後可取得 5–10 年歷史，否則 CAGR 與長期平均將標示資料不足")
    else:
        for key, label in (
            ("income_statement", "綜合損益表"),
            ("balance_sheet", "資產負債表"),
            ("cash_flow", "現金流量表"),
        ):
            try:
                names = loader.finmind.discover_types(stock_id, key)
            except (SourceUnavailable, FetchError) as exc:
                print(f"  ✗ {label}：{exc}")
                failures += 1
                continue
            print(f"  ✓ {label}：{len(names)} 個科目")
            unmapped = loader.finmind.unmapped_types.get(key, set())
            if unmapped:
                print(f"    ⚠ {len(unmapped)} 個科目未對應到欄位：")
                for name in sorted(unmapped)[:20]:
                    print(f"      · {name}")

    print(f"\n{'✓ 全部來源可用' if failures == 0 else f'✗ {failures} 項來源失敗'}")
    return 0 if failures == 0 else 1


def _cmd_check_config(config: Config, args) -> int:
    from .metrics import verify_criteria_coverage

    weights = config.weights
    total = sum(weights.values())
    print("=== 評分權重 ===")
    for key, value in weights.items():
        print(f"  {key:<20} {value:>5.1f}")
    print(f"  {'合計':<20} {total:>5.1f}" + ("" if total == 100 else f"（將換算為 100 分制）"))

    print("\n=== 指標實作對應 ===")
    problems = verify_criteria_coverage(config.scoring)
    if problems:
        for problem in problems:
            print(f"  ✗ {problem}")
        print(f"\n✗ {len(problems)} 項不一致")
        return 1

    criteria_count = sum(len(config.criteria(c)) for c in weights)
    print(f"  ✓ {criteria_count} 個評分指標全部有對應實作")

    overrides = config.overrides.get("overrides") or {}
    if overrides:
        print(f"\n=== 人工覆寫 ===")
        for stock_id, components in overrides.items():
            for component, criteria in components.items():
                for key, payload in criteria.items():
                    print(
                        f"  · {stock_id} {component}.{key} = {payload['points']} 分"
                        f"（{payload['reviewed_on']}）"
                    )

    print("\n✓ 設定檔驗證通過")
    return 0


def _print_run_summary(run) -> None:
    print(f"分析日期：{run.run_date.isoformat()}")
    print(f"成分股：  {len(run.results)} 檔（{len(run.rankable)} 檔資料足夠可排名）")
    print(f"名單來源：{run.constituents.source}")

    boards = run.leaderboards(limit=10)
    top = boards[0]
    if top.entries:
        print(f"\n{top.title}")
        for index, result in enumerate(top.entries, start=1):
            score = result.score
            print(
                f"  {index:>2}. {result.company.name:<12} {result.company.stock_id:<6} "
                f"{score.total_score:>5.1f} 分  {score.grade[0]:<2} "
                f"護城河 {score.moat_grade:<5} {result.verdict}"
            )

    flagged = run.red_flag_results
    if flagged:
        print(f"\n🚨 紅旗（{len(flagged)} 檔）")
        for result in flagged[:10]:
            flags = result.score.red_flags
            print(
                f"  · {result.company.name}（{result.company.stock_id}）"
                f"重大 {flags.critical_count}／警告 {flags.warning_count}："
                + "、".join(f.label for f in flags.triggered)
            )

    changes = run.significant_changes
    if changes:
        print(f"\n📈 評分變化（{len(changes)} 檔）")
        for change in changes[:10]:
            print(f"  · {change.headline}　{change.explanation}")

    if run.warnings:
        print(f"\n⚠ 警告")
        for warning in run.warnings:
            print(f"  · {warning}")


if __name__ == "__main__":
    sys.exit(main())
