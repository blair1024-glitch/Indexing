"""報表層。

* ``markdown`` — 存進 repo，``git diff`` 即為評分歷史
* ``dashboard`` — 自足的 HTML，可直接開啟或由 GitHub Pages 提供
* ``format`` — 共用的數值格式化（缺料一律顯示「資料不足」，絕不顯示 0）
"""

from .dashboard import render_dashboard, write_dashboard
from .markdown import render_company, render_summary, write_reports

__all__ = [
    "render_company",
    "render_dashboard",
    "render_summary",
    "write_dashboard",
    "write_reports",
]
