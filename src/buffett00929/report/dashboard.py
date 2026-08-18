"""HTML Dashboard（規格第二十節）。

單一自足檔案：CSS 內嵌、無外部資源，可直接用瀏覽器開啟，
也可由 GitHub Pages 提供。輸出至 ``docs/index.html``。

配色隨讀者的系統主題切換（淺色／深色皆有明確定義），
表格在窄螢幕上各自橫向捲動，頁面本身不會橫向溢出。
"""

from __future__ import annotations

import html
from pathlib import Path

from ..pipeline import AnalysisRun, CompanyResult
from . import format as fmt

CSS = """
:root {
  --bg: #f7f8fa;
  --surface: #ffffff;
  --border: #e2e6ec;
  --text: #171a1f;
  --muted: #5d6673;
  --accent: #1f5fd6;
  --good: #12805c;
  --warn: #a3690a;
  --bad: #c0392f;
  --tile: #eef2f8;
  --shadow: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.04);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0f1216;
    --surface: #171b21;
    --border: #2a313b;
    --text: #e8ebef;
    --muted: #9aa4b2;
    --accent: #6da3ff;
    --good: #3ecf9a;
    --warn: #e0a63d;
    --bad: #ff7a6d;
    --tile: #1e242c;
    --shadow: none;
  }
}
:root[data-theme="dark"] {
  --bg: #0f1216;
  --surface: #171b21;
  --border: #2a313b;
  --text: #e8ebef;
  --muted: #9aa4b2;
  --accent: #6da3ff;
  --good: #3ecf9a;
  --warn: #e0a63d;
  --bad: #ff7a6d;
  --tile: #1e242c;
  --shadow: none;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC",
        "PingFang TC", "Microsoft JhengHei", sans-serif;
  padding: 0 0 4rem;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 1.25rem; }
header { padding: 2.5rem 0 1.5rem; border-bottom: 1px solid var(--border); margin-bottom: 2rem; }
h1 { font-size: 1.85rem; margin: 0 0 .4rem; letter-spacing: -.02em; }
h2 { font-size: 1.2rem; margin: 2.5rem 0 .35rem; letter-spacing: -.01em; }
h2:first-of-type { margin-top: 1.5rem; }
.sub { color: var(--muted); font-size: .9rem; margin: 0 0 1rem; }
.banner {
  background: color-mix(in srgb, var(--warn) 14%, var(--surface));
  border: 1px solid var(--warn);
  border-radius: 8px; padding: .85rem 1rem; margin: 1rem 0; font-size: .92rem;
}
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .75rem; margin: 1.25rem 0 0; }
.tile { background: var(--tile); border: 1px solid var(--border); border-radius: 10px; padding: .85rem .95rem; }
.tile .label { color: var(--muted); font-size: .78rem; margin-bottom: .3rem; }
.tile .value { font-size: 1.3rem; font-weight: 600; letter-spacing: -.02em; }
.tile .note { color: var(--muted); font-size: .74rem; margin-top: .2rem; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: var(--shadow); overflow: hidden; margin-bottom: 1rem; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .88rem; min-width: 640px; }
th, td { padding: .6rem .75rem; text-align: left; border-bottom: 1px solid var(--border); white-space: nowrap; }
th { background: var(--tile); font-weight: 600; font-size: .8rem; color: var(--muted); position: sticky; top: 0; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: color-mix(in srgb, var(--accent) 6%, transparent); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.wrapline { white-space: normal; min-width: 260px; }
.missing { color: var(--muted); font-style: italic; }
.good { color: var(--good); font-weight: 600; }
.warn { color: var(--warn); font-weight: 600; }
.bad { color: var(--bad); font-weight: 600; }
.pill { display: inline-block; padding: .1rem .5rem; border-radius: 999px; font-size: .75rem; font-weight: 600; border: 1px solid var(--border); background: var(--tile); }
.change { padding: .55rem .85rem; border-left: 3px solid var(--accent); background: var(--surface); border-radius: 0 8px 8px 0; margin-bottom: .5rem; border-top: 1px solid var(--border); border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); }
.change .head { font-weight: 600; }
.change .why { color: var(--muted); font-size: .85rem; }
.empty { color: var(--muted); padding: 1rem; }
footer { margin-top: 3rem; padding-top: 1.25rem; border-top: 1px solid var(--border); color: var(--muted); font-size: .82rem; }
ul.gaps { margin: .4rem 0 1rem; padding-left: 1.25rem; color: var(--muted); font-size: .86rem; }
"""


def write_dashboard(run: AnalysisRun, repo_root: Path) -> Path:
    docs = repo_root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / "index.html"
    path.write_text(render_dashboard(run), encoding="utf-8")
    return path



def _source_summary(run) -> str:
    """本次執行實際命中的來源，由 provenance 反推（見 markdown._add_sources）。"""
    sources = run.data_sources
    if not sources:
        return "無任何可用數據點"
    return "、".join(f"{source}（{count:,}）" for source, count in sources)

def render_dashboard(run: AnalysisRun) -> str:
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="zh-Hant"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>00929 Buffett Dashboard</title>",
        f"<style>{CSS}</style></head><body><div class='wrap'>",
    ]
    add = parts.append

    add("<header>")
    add("<h1>00929 Buffett Dashboard</h1>")
    add(
        f"<p class='sub'>復華台灣科技優息 ETF ｜ 巴菲特式 3M 選股與財報追蹤 ｜ "
        f"資料日期 {run.run_date.isoformat()}</p>"
    )
    if run.is_demo:
        add(
            "<div class='banner'>⚠️ <strong>本頁使用合成範例資料產生</strong>，"
            "所有數字均非真實財報，僅供版面與計算邏輯驗證。"
            "接上官方資料來源後執行 <code>make update</code> 即可產生真實分析。</div>"
        )
    add(_etf_tiles(run))
    add("</header>")

    add(_constituent_changes(run))
    add(_score_changes(run))
    add(_red_flags(run))

    for board in run.leaderboards():
        add(f"<h2>{_esc(board.title)}</h2>")
        add(f"<p class='sub'>{_esc(board.description)}</p>")
        add(_leaderboard_table(board.entries))

    add(_yield_traps(run))
    add(_full_table(run))
    add(_data_gaps(run))

    add("<footer>")
    add(
        "<p>資料來源：成分股名單 "
        f"{_esc(run.constituents.source)}（{run.constituents.as_of.isoformat()}）；"
        f"財報與市場資料 {_esc(_source_summary(run))}。</p>"
    )
    add(
        "<p>缺料一律標示「資料不足」且不計入評分分母，不以推估值填補。"
        "「公司治理重大問題」與「競爭對手市占率快速增加」無法由財報判定，"
        "標示為未檢查而非安全。</p>"
    )
    add("</footer></div></body></html>")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 區塊
# --------------------------------------------------------------------------


def _etf_tiles(run: AnalysisRun) -> str:
    total_weight = run.constituents.total_weight
    total = len(run.results)
    roe, roe_n = run.average_roe
    pe, pe_n = run.average_pe
    yield_value, yield_n = run.average_dividend_yield
    tiles = [
        ("成分股數量", f"{len(run.constituents)}", "檔"),
        (
            "權重合計",
            f"{total_weight:.1%}" if total_weight is not None else fmt.MISSING_TEXT,
            "",
        ),
        ("平均 ROE", fmt.pct(roe), f"{roe_n} / {total} 檔有資料"),
        ("平均本益比", fmt.num(pe), f"{pe_n} / {total} 檔有資料"),
        ("平均殖利率", fmt.pct(yield_value), f"{yield_n} / {total} 檔有資料"),
        ("可排名", f"{len(run.rankable)} / {total}", "資料覆蓋率達標"),
    ]
    cells = "".join(
        f"<div class='tile'><div class='label'>{_esc(label)}</div>"
        f"<div class='value'>{_esc(value)}</div>"
        + (f"<div class='note'>{_esc(note)}</div>" if note else "")
        + "</div>"
        for label, value, note in tiles
    )
    return f"<div class='tiles'>{cells}</div>"


def _score_changes(run: AnalysisRun) -> str:
    parts = ["<h2>📈 評分變化</h2>"]
    significant = run.significant_changes

    if run.basis_changed_count:
        parts.append(
            "<div class='card'><p class='warn'>⚠️ 本次有 "
            f"<strong>{run.basis_changed_count} 檔</strong>的可評分基準改變"
            "（資料可得性變動使分母位移），其總分不可與上次直接比較，"
            "分數位移本身不列為變化；但紅旗與等級不受分母影響，"
            "若有變動仍會列在下方。</p></div>"
        )

    if not significant:
        parts.append(
            "<div class='card'><p class='empty'>與上次執行相比，沒有成分股出現顯著的評分變化。</p></div>"
        )
        return "".join(parts)
    for change in significant:
        arrow_class = "good" if (change.delta or 0) > 0 else "bad"
        parts.append(
            f"<div class='change'><div class='head'>"
            f"<span class='{arrow_class}'>{_esc(change.headline)}</span></div>"
            f"<div class='why'>{_esc(change.explanation)}</div></div>"
        )
    return "".join(parts)


def _constituent_changes(run: AnalysisRun) -> str:
    changes = run.constituent_changes
    if not changes.has_changes:
        return ""
    parts = ["<h2>🔄 成分股異動</h2><div class='card'><div class='scroll'><table>"]
    parts.append("<thead><tr><th>類型</th><th>公司</th><th>代號</th><th class='num'>權重變動</th></tr></thead><tbody>")
    for item in changes.added:
        parts.append(
            f"<tr><td><span class='pill good'>新增</span></td><td>{_esc(item.name)}</td>"
            f"<td>{_esc(item.stock_id)}</td><td class='num'>{fmt.pct(item.weight, 2)}</td></tr>"
        )
    for item in changes.removed:
        parts.append(
            f"<tr><td><span class='pill bad'>刪除</span></td><td>{_esc(item['name'])}</td>"
            f"<td>{_esc(item['stock_id'])}</td><td class='num'>—</td></tr>"
        )
    for item in changes.weight_changes:
        parts.append(
            f"<tr><td><span class='pill'>權重</span></td><td>{_esc(item['name'])}</td>"
            f"<td>{_esc(item['stock_id'])}</td>"
            f"<td class='num'>{item['previous']:.2%} → {item['current']:.2%}"
            f"（{item['delta']:+.2%}）</td></tr>"
        )
    parts.append("</tbody></table></div></div>")
    return "".join(parts)


def _red_flags(run: AnalysisRun) -> str:
    parts = ["<h2>🚨 紅旗警報</h2>"]
    flagged = run.red_flag_results
    if not flagged:
        parts.append(
            "<div class='card'><p class='empty'>目前沒有成分股觸發紅旗。"
            "註：公司治理與競爭市占兩項無法由財報判定，標示為未檢查而非安全。</p></div>"
        )
        return "".join(parts)

    rows = []
    for result in flagged:
        flags = result.score.red_flags
        items = "、".join(_esc(f.label) for f in flags.triggered)
        detail = "；".join(_esc(f"{f.label}：{f.evidence}") for f in flags.triggered)
        rows.append(
            f"<tr><td>{_esc(result.company.name)}</td><td>{_esc(result.company.stock_id)}</td>"
            f"<td class='num bad'>{flags.critical_count}</td>"
            f"<td class='num warn'>{flags.warning_count}</td>"
            f"<td class='wrapline'>{items}</td>"
            f"<td class='wrapline'>{detail}</td></tr>"
        )
    parts.append(
        "<div class='card'><div class='scroll'><table><thead><tr>"
        "<th>公司</th><th>代號</th><th class='num'>重大</th><th class='num'>警告</th>"
        "<th>觸發項目</th><th>依據</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div></div>"
    )
    return "".join(parts)


def _leaderboard_table(entries: list[CompanyResult]) -> str:
    if not entries:
        return "<div class='card'><p class='empty'>目前沒有符合條件且資料足夠的成分股。</p></div>"
    rows = []
    for index, result in enumerate(entries, start=1):
        score = result.score
        roe = result.metrics.value("roe", "roe_latest")
        leverage = " ⚠️" if result.metrics.high_roe_via_leverage else ""
        rows.append(
            f"<tr><td class='num'>{index}</td>"
            f"<td>{_esc(result.company.name)}</td>"
            f"<td>{_esc(result.company.stock_id)}</td>"
            f"<td class='num'>{fmt.pct(result.company.etf_weight, 2)}</td>"
            f"<td class='num'><strong>{score.total_score:.1f}</strong></td>"
            f"<td><span class='pill'>{_esc(score.grade[0])}</span></td>"
            f"<td class='num'>{fmt.pct(roe)}{leverage}</td>"
            f"<td>{_esc(score.moat_grade)}</td>"
            f"<td class='num'>{_mos_cell(score.valuation.margin_of_safety)}</td>"
            f"<td>{_esc(result.verdict)}</td></tr>"
        )
    return (
        "<div class='card'><div class='scroll'><table><thead><tr>"
        "<th class='num'>#</th><th>公司</th><th>代號</th><th class='num'>權重</th>"
        "<th class='num'>總分</th><th>等級</th><th class='num'>ROE</th><th>護城河</th>"
        "<th class='num'>安全邊際</th><th>判斷</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></div>"
    )


def _mos_cell(point) -> str:
    if not point.is_available or point.value is None:
        return f"<span class='missing'>{fmt.MISSING_TEXT}</span>"
    css = "good" if point.value >= 0.20 else ("warn" if point.value >= 0 else "bad")
    return f"<span class='{css}'>{point.value:.0%}</span>"


def _yield_traps(run: AnalysisRun) -> str:
    parts = ["<h2>⚠️ 高殖利率陷阱</h2>"]
    parts.append(
        "<p class='sub'>殖利率偏高，但同時出現 EPS 下滑、FCF 惡化、配息率過高或負債快速增加</p>"
    )
    traps = run.yield_traps[:10]
    if not traps:
        parts.append("<div class='card'><p class='empty'>目前沒有成分股觸發高殖利率陷阱判定。</p></div>")
        return "".join(parts)
    rows = []
    for result in traps:
        trap = result.metrics.yield_trap
        rows.append(
            f"<tr><td>{_esc(result.company.name)}</td><td>{_esc(result.company.stock_id)}</td>"
            f"<td class='num warn'>{fmt.pct(trap.yield_value, 2)}</td>"
            f"<td class='wrapline'>{_esc('；'.join(trap.reasons))}</td></tr>"
        )
    parts.append(
        "<div class='card'><div class='scroll'><table><thead><tr>"
        "<th>公司</th><th>代號</th><th class='num'>殖利率</th><th>警訊</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></div>"
    )
    return "".join(parts)


def _full_table(run: AnalysisRun) -> str:
    rows = []
    for result in sorted(run.results, key=lambda r: r.score.total_score, reverse=True):
        score = result.score
        rank_note = "" if score.is_rankable else " <span class='pill'>未列入排名</span>"
        rows.append(
            f"<tr><td>{_esc(result.company.name)}{rank_note}</td>"
            f"<td>{_esc(result.company.stock_id)}</td>"
            f"<td>{_esc(result.company.industry)}</td>"
            f"<td class='num'>{fmt.pct(result.company.etf_weight, 2)}</td>"
            f"<td class='num'><strong>{score.total_score:.1f}</strong></td>"
            f"<td class='num'>{score.scorable_max:.0f}</td>"
            f"<td class='num'>{score.coverage:.0%}</td>"
            f"<td><span class='pill'>{_esc(score.grade[0])}</span></td>"
            f"<td>{_esc(result.verdict)}</td>"
            f"<td>{_esc(result.seven_year)}</td>"
            f"<td class='wrapline'>{_esc(result.conclusion)}</td></tr>"
        )
    return (
        "<h2>全部成分股</h2>"
        "<div class='card'><div class='scroll'><table><thead><tr>"
        "<th>公司</th><th>代號</th><th>產業</th><th class='num'>權重</th>"
        "<th class='num'>總分</th><th class='num'>可評分</th><th class='num'>覆蓋率</th>"
        "<th>等級</th><th>判斷</th><th>7 年持有</th><th>一句話結論</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></div>"
    )


def _data_gaps(run: AnalysisRun) -> str:
    gaps = [r for r in run.data_gap_results if r.company.data_gaps or not r.score.is_rankable]
    if not gaps and not run.warnings:
        return ""
    parts = ["<h2>📋 資料缺口</h2>"]
    parts.append(
        "<p class='sub'>以下項目未能取得資料。系統不以推估值填補，"
        "相關指標一律標示「資料不足」且不計入評分分母。</p>"
    )
    parts.append("<div class='card'><div style='padding:1rem'>")
    for result in gaps:
        parts.append(f"<strong>{_esc(result.company.name)}（{_esc(result.company.stock_id)}）</strong>")
        items = []
        if not result.score.is_rankable:
            items.append(f"資料覆蓋率 {result.score.coverage:.0%}，未列入排名")
        items.extend(result.company.data_gaps[:5])
        parts.append("<ul class='gaps'>" + "".join(f"<li>{_esc(i)}</li>" for i in items) + "</ul>")
    if run.warnings:
        parts.append("<strong>執行警告</strong>")
        parts.append(
            "<ul class='gaps'>" + "".join(f"<li>{_esc(w)}</li>" for w in run.warnings) + "</ul>"
        )
    parts.append("</div></div>")
    return "".join(parts)


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


__all__ = ["render_dashboard", "write_dashboard"]
