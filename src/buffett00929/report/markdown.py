"""Markdown 報表。

輸出進 repo 並每日 commit，因此 ``git diff`` 本身就是評分變化的歷史紀錄——
想知道某次財報公布後哪些數字動了，直接看那天的 diff 即可。
"""

from __future__ import annotations

from pathlib import Path

from ..pipeline import AnalysisRun, CompanyResult
from ..scoring.engine import COMPONENT_LABELS
from . import format as fmt


def write_reports(run: AnalysisRun, repo_root: Path) -> list[Path]:
    """產生總覽與逐檔報告，並清掉已不在名單中的舊報告。

    清理是必要的：成分股被剔除後，它的報告若留在原地，
    看起來就像是最新分析的一部分——日期還是舊的，但讀者不會注意到。
    ETF 換股後留下一份「已不是成分股」的報告，比沒有報告更誤導。
    """
    reports_dir = repo_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    companies_dir = reports_dir / "companies"
    companies_dir.mkdir(parents=True, exist_ok=True)

    written = [_write(reports_dir / "README.md", render_summary(run))]
    current_ids = set()
    for result in run.results:
        current_ids.add(result.company.stock_id)
        path = companies_dir / f"{result.company.stock_id}.md"
        written.append(_write(path, render_company(result, run)))

    for stale in companies_dir.glob("*.md"):
        if stale.stem not in current_ids:
            stale.unlink()

    return written


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# 總覽
# --------------------------------------------------------------------------


def render_summary(run: AnalysisRun) -> str:
    lines: list[str] = []
    add = lines.append

    add("# 00929 Buffett Dashboard")
    add("")
    if run.is_demo:
        add("> ⚠️ **本報告使用合成範例資料產生，所有數字均非真實財報，僅供版面與計算邏輯驗證。**")
        add("")
    add(f"**資料日期**：{run.run_date.isoformat()}")
    add("")

    add("## ETF 資訊")
    add("")
    add("| 項目 | 數值 |")
    add("| --- | --- |")
    add(f"| 成分股數量 | {len(run.constituents)} 檔 |")
    total_weight = run.constituents.total_weight
    add(f"| 權重合計 | {f'{total_weight:.1%}' if total_weight is not None else fmt.MISSING_TEXT} |")
    roe, roe_n = run.average_roe
    pe, pe_n = run.average_pe
    yield_value, yield_n = run.average_dividend_yield
    total = len(run.results)
    add(f"| 成分股平均 ROE | {fmt.pct(roe)}（{roe_n} / {total} 檔有資料）|")
    add(f"| 成分股平均本益比 | {fmt.num(pe)}（{pe_n} / {total} 檔有資料）|")
    add(f"| 成分股平均殖利率 | {fmt.pct(yield_value)}（{yield_n} / {total} 檔有資料）|")
    add(f"| 可排名檔數 | {len(run.rankable)} / {len(run.results)} |")
    add(f"| 成分股名單來源 | {run.constituents.source} |")
    add(f"| 名單資料日期 | {run.constituents.as_of.isoformat()} |")
    add("")

    _add_constituent_changes(add, run)
    _add_score_changes(add, run)
    _add_red_flags(add, run)

    for board in run.leaderboards():
        add(f"## {board.title}")
        add("")
        add(f"_{board.description}_")
        add("")
        if not board.entries:
            add("目前沒有符合條件且資料足夠的成分股。")
            add("")
            continue
        add("| # | 公司 | 代號 | 權重 | 總分 | 等級 | ROE | 護城河 | 安全邊際 | 判斷 |")
        add("| --: | --- | --- | --: | --: | --- | --: | --- | --: | --- |")
        for index, result in enumerate(board.entries, start=1):
            add(_leaderboard_row(index, result))
        add("")

    _add_yield_traps(add, run)
    _add_full_table(add, run)
    _add_data_gaps(add, run)
    _add_sources(add, run)

    return "\n".join(lines) + "\n"


def _leaderboard_row(index: int, result: CompanyResult) -> str:
    score = result.score
    roe = result.metrics.value("roe", "roe_latest")
    leverage_mark = " ⚠️" if result.metrics.high_roe_via_leverage else ""
    return (
        f"| {index} | {result.company.name} | {result.company.stock_id} "
        f"| {fmt.pct(result.company.etf_weight, 2)} "
        f"| {score.total_score:.1f} | {score.grade[0]} "
        f"| {fmt.pct(roe)}{leverage_mark} | {score.moat_grade} "
        f"| {fmt.pct(score.valuation.margin_of_safety, 0)} | {result.verdict} |"
    )


def _add_constituent_changes(add, run: AnalysisRun) -> None:
    changes = run.constituent_changes
    if not changes.has_changes:
        return
    add("## 🔄 成分股異動")
    add("")
    if changes.added:
        add("**新增**：" + "、".join(f"{c.name}（{c.stock_id}）" for c in changes.added))
        add("")
    if changes.removed:
        add("**刪除**：" + "、".join(f"{c['name']}（{c['stock_id']}）" for c in changes.removed))
        add("")
    if changes.weight_changes:
        add("**權重變動**：")
        add("")
        add("| 公司 | 代號 | 前次 | 本次 | 變動 |")
        add("| --- | --- | --: | --: | --: |")
        for item in changes.weight_changes:
            add(
                f"| {item['name']} | {item['stock_id']} | {item['previous']:.2%} "
                f"| {item['current']:.2%} | {item['delta']:+.2%} |"
            )
        add("")


def _add_score_changes(add, run: AnalysisRun) -> None:
    add("## 📈 評分變化")
    add("")
    significant = run.significant_changes
    if not significant:
        add("與上次執行相比，沒有成分股出現顯著的評分變化。")
        add("")
        return
    for change in significant:
        add(f"- **{change.headline}**　{change.explanation}")
    add("")


def _add_red_flags(add, run: AnalysisRun) -> None:
    add("## 🚨 紅旗警報")
    add("")
    flagged = run.red_flag_results
    if not flagged:
        add("目前沒有成分股觸發紅旗。")
        add("")
        add(
            "> 註：「公司治理重大問題」與「競爭對手市占率快速增加」無法由財報數字判定，"
            "系統標示為未檢查而非安全，請於 `data/manual/governance.yaml` 登錄。"
        )
        add("")
        return

    add("| 公司 | 代號 | 重大 | 警告 | 觸發項目 |")
    add("| --- | --- | --: | --: | --- |")
    for result in flagged:
        flags = result.score.red_flags
        items = "、".join(f.label for f in flags.triggered)
        add(
            f"| {result.company.name} | {result.company.stock_id} "
            f"| {flags.critical_count} | {flags.warning_count} | {items} |"
        )
    add("")


def _add_yield_traps(add, run: AnalysisRun) -> None:
    add("## ⚠️ 高殖利率陷阱 Top 10")
    add("")
    add("_殖利率偏高，但同時出現 EPS 下滑、FCF 惡化、配息率過高或負債快速增加_")
    add("")
    traps = run.yield_traps[:10]
    if not traps:
        add("目前沒有成分股觸發高殖利率陷阱判定。")
        add("")
        return
    add("| 公司 | 代號 | 殖利率 | 警訊 |")
    add("| --- | --- | --: | --- |")
    for result in traps:
        trap = result.metrics.yield_trap
        add(
            f"| {result.company.name} | {result.company.stock_id} "
            f"| {fmt.pct(trap.yield_value, 2)} | {'；'.join(trap.reasons)} |"
        )
    add("")


def _add_full_table(add, run: AnalysisRun) -> None:
    add("## 全部成分股")
    add("")
    add("| 公司 | 代號 | 產業 | 權重 | 總分 | 可評分 | 覆蓋率 | 等級 | 判斷 | 7 年持有 |")
    add("| --- | --- | --- | --: | --: | --: | --: | --- | --- | --- |")
    for result in sorted(run.results, key=lambda r: r.score.total_score, reverse=True):
        score = result.score
        add(
            f"| [{result.company.name}](companies/{result.company.stock_id}.md) "
            f"| {result.company.stock_id} | {result.company.industry} "
            f"| {fmt.pct(result.company.etf_weight, 2)} "
            f"| {score.total_score:.1f} | {score.scorable_max:.0f} "
            f"| {score.coverage:.0%} | {score.grade[0]} "
            f"| {result.verdict} | {result.seven_year} |"
        )
    add("")


def _add_data_gaps(add, run: AnalysisRun) -> None:
    gaps = run.data_gap_results
    if not gaps and not run.warnings:
        return
    add("## 📋 資料缺口")
    add("")
    add("_以下項目未能取得資料。系統不會以推估值填補，相關指標一律標示「資料不足」且不計入評分分母。_")
    add("")
    for result in gaps:
        if not result.company.data_gaps and result.score.is_rankable:
            continue
        add(f"- **{result.company.name}（{result.company.stock_id}）**")
        if not result.score.is_rankable:
            add(f"  - 資料覆蓋率 {result.score.coverage:.0%}，未列入排名")
        for gap in result.company.data_gaps[:5]:
            add(f"  - {gap}")
    if run.warnings:
        add("")
        add("**執行警告**：")
        for warning in run.warnings:
            add(f"- {warning}")
    add("")


def _add_sources(add, run: AnalysisRun) -> None:
    add("## 資料來源")
    add("")
    add(f"- 成分股名單：{run.constituents.source}（資料日期 {run.constituents.as_of.isoformat()}）")
    add("- 財報最新一期：證交所／櫃買中心 OpenAPI（官方）")
    add("- 多年度歷史：FinMind（用於 CAGR、5–10 年平均 ROE、毛利率趨勢、歷史本益比分位）")
    add(f"- 產生時間：{run.run_date.isoformat()}")
    add("")


# --------------------------------------------------------------------------
# 個別公司
# --------------------------------------------------------------------------


def render_company(result: CompanyResult, run: AnalysisRun) -> str:
    company = result.company
    score = result.score
    metrics = result.metrics
    lines: list[str] = []
    add = lines.append

    add(f"# {company.name}（{company.stock_id}）")
    add("")
    if run.is_demo:
        add("> ⚠️ **合成範例資料，非真實財報。**")
        add("")
    add(f"> {result.conclusion}")
    add("")

    grade, grade_label = score.grade
    add("## 評分總覽")
    add("")
    add("| 項目 | 數值 |")
    add("| --- | --- |")
    add(f"| **Buffett Score** | **{score.total_score:.1f}** / {score.scorable_max:.0f} 可評分 |")
    add(f"| 投資等級 | {grade}（{grade_label}）|")
    add(f"| 企業品質 Business Quality | {fmt.score_text(score.business_quality_score)} |")
    add(f"| 財務安全 Financial Safety | {fmt.score_text(score.financial_safety_score)} |")
    add(f"| 估值 Valuation | {fmt.score_text(score.valuation_score)} |")
    add(f"| 護城河 | {score.moat_grade} |")
    add(f"| 資料覆蓋率 | {score.coverage:.0%}（{fmt.coverage_badge(score.coverage)}）|")
    if score.penalty:
        add(f"| 紅旗扣分 | −{score.penalty:.1f} 分 |")
    add(f"| ETF 權重 | {fmt.pct(company.etf_weight, 2)} |")
    add("")

    add(f"**投資判斷**：{result.verdict}　—　{result.verdict_reason}")
    add("")
    add(f"**7 年長期持有測試**：{result.seven_year}　—　{result.seven_year_reason}")
    add("")

    _company_scorecard(add, result)
    _company_valuation(add, result)
    _company_changes(add, result)
    _company_flags(add, result)
    _company_qualitative(add, result)

    if company.data_gaps:
        add("## 資料缺口")
        add("")
        for gap in company.data_gaps:
            add(f"- {gap}")
        add("")

    return "\n".join(lines) + "\n"


def _company_scorecard(add, result: CompanyResult) -> None:
    add("## 評分明細")
    add("")
    for key, component in result.score.components.items():
        label = COMPONENT_LABELS.get(key, key)
        if not component.has_any_data:
            add(f"### {label}")
            add("")
            add(f"整項資料不足，未計分（滿分 {component.max_points:.0f}）。")
            add("")
            continue
        add(f"### {label}　{component.earned:.1f} / {component.scorable_max:.0f}")
        add("")
        add("| 指標 | 實際值 | 評級 | 得分 | 資料來源 |")
        add("| --- | --: | --- | --: | --- |")
        for criterion in component.criteria:
            points = (
                f"{criterion.points:.1f} / {criterion.max_points:.0f}"
                if criterion.is_scorable
                else "未計分"
            )
            band = criterion.band_label or ("—" if criterion.is_scorable else "資料不足")
            if criterion.overridden:
                band += "（人工調整）"
            add(
                f"| {criterion.label} | {criterion.display_value} | {band} | {points} "
                f"| {fmt.provenance_text(criterion.value)} |"
            )
        add("")
        overridden = [c for c in component.criteria if c.overridden]
        for criterion in overridden:
            stale = "　⚠️ 覆寫已超過檢視期限，請重新確認" if criterion.override_stale else ""
            original = (
                f"（原計算值 {criterion.computed_points:.1f}）"
                if criterion.computed_points is not None
                else ""
            )
            add(
                f"> **人工調整** {criterion.label}{original}"
                f"：{criterion.override_rationale}"
                f"　—　{criterion.override_reviewed_on}{stale}"
            )
            add("")


def _company_valuation(add, result: CompanyResult) -> None:
    valuation = result.score.valuation
    add("## 估值與安全邊際")
    add("")
    add(f"**結論**：{valuation.classification}　{valuation.note}")
    add("")
    add("| 估值方法 | 每股價值 | 假設 |")
    add("| --- | --: | --- |")
    for method in valuation.methods:
        value = (
            f"{method.value_per_share.value:,.1f} 元"
            if method.is_available
            else fmt.MISSING_TEXT
        )
        assumptions = method.assumptions or (
            method.value_per_share.unavailable_reason or "—"
        )
        add(f"| {method.label} | {value} | {assumptions} |")
    add("")
    add("| 項目 | 數值 |")
    add("| --- | --: |")
    add(f"| 內在價值（中位數）| {fmt.num(valuation.intrinsic_value, 1, ' 元')} |")
    add(f"| 目前股價 | {fmt.num(valuation.price, 1, ' 元')} |")
    add(f"| **安全邊際** | **{fmt.pct(valuation.margin_of_safety, 1)}** |")
    add("")


def _company_changes(add, result: CompanyResult) -> None:
    if not result.changes:
        return
    add("## 財報變化")
    add("")
    add(f"**{result.change_summary}**")
    add("")
    add("| 指標 | 上期 | 本期 | YoY | QoQ | 趨勢 |")
    add("| --- | --: | --: | --: | --: | :-: |")
    for change in result.changes:
        add(
            f"| {change.label} "
            f"| {fmt.value_cell(change.previous, change.is_percentage)} "
            f"| {fmt.value_cell(change.current, change.is_percentage)} "
            f"| {fmt.change_cell(change.yoy, change.is_percentage)} "
            f"| {fmt.change_cell(change.qoq, change.is_percentage)} "
            f"| {change.trend} |"
        )
    add("")
    add("> 比較基礎為**單季**數字。台灣財報以累計數揭露，直接比較累計數會讓每一季都看似成長。")
    add("")


def _company_flags(add, result: CompanyResult) -> None:
    flags = result.score.red_flags
    add("## 紅旗警報")
    add("")
    if flags.triggered:
        add("| 嚴重度 | 項目 | 依據 |")
        add("| --- | --- | --- |")
        for flag in flags.triggered:
            icon = {"critical": "🔴 重大", "warning": "🟠 警告"}.get(flag.severity, "🟡 提示")
            add(f"| {icon} | {flag.label} | {flag.evidence} |")
        add("")
    else:
        add("未觸發任何紅旗。")
        add("")

    if flags.unchecked:
        add("**未檢查項目**（資料不足或無法自動偵測，不等於安全）：")
        add("")
        for flag in flags.unchecked:
            add(f"- {flag.label}：{flag.evidence}")
        add("")


def _company_qualitative(add, result: CompanyResult) -> None:
    metrics = result.metrics
    add("## 體質判定")
    add("")
    add("| 項目 | 判定 |")
    add("| --- | --- |")
    add(f"| 獲利型態 | {metrics.earnings_profile} |")
    add(f"| 資本效率 | {metrics.capital_efficiency}　{metrics.capital_efficiency_note} |")
    add(f"| 高 ROE 是否靠槓桿 | {'⚠️ 是' if metrics.high_roe_via_leverage else '否'}　{metrics.high_roe_note} |")
    add(f"| 利息保障倍數 | {fmt.multiple(metrics.interest_coverage)} |")
    add(f"| 淨負債 | {fmt.money(metrics.net_debt)} |")
    add(f"| 連續配息年數 | {fmt.num(metrics.dividend_continuity, 0, ' 年')} |")
    add(f"| 配息率 | {fmt.pct(metrics.payout_ratio)} |")
    add(f"| 股利成長率 | {fmt.growth(metrics.dividend_growth)} |")
    add("")

    if metrics.dupont and metrics.dupont.roe.is_available:
        dupont = metrics.dupont
        add("**杜邦拆解**：ROE "
            f"{fmt.pct(dupont.roe)} ＝ 淨利率 {fmt.pct(dupont.net_margin)}"
            f" × 資產週轉率 {fmt.multiple(dupont.asset_turnover)}"
            f" × 權益乘數 {fmt.multiple(dupont.equity_multiplier)}")
        add("")

    if metrics.earnings_quality_warnings:
        add("**盈餘品質警訊**：")
        add("")
        for warning in metrics.earnings_quality_warnings:
            add(f"- {warning.message}（{warning.evidence}）")
        add("")

    trap = metrics.yield_trap
    if trap:
        status = "⚠️ 是" if trap.is_trap else "否"
        add(f"**高殖利率陷阱**：{status}　{trap.note}")
        add("")
        for reason in trap.reasons:
            add(f"- {reason}")
        if trap.reasons:
            add("")


__all__ = ["render_company", "render_summary", "write_reports"]
