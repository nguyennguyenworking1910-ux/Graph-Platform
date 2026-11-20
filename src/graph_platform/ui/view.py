"""What the interface draws: the network picture (Cytoscape.js mounted through Streamlit) and the introduction diagram."""

from __future__ import annotations

from html import escape
from pathlib import Path

import streamlit as st

HERE = Path(__file__).parent
MARK = {
    "person": '<circle cx="9" cy="9" r="6"/>',
    "merchant": '<rect x="3" y="3" width="12" height="12"/>',
    "device": '<path d="M9 2 16 9 9 16 2 9Z"/>',
    "identifier": '<path d="M9 3 16 15H2Z"/>',
    "item": '<rect x="2" y="7" width="14" height="4" class="full"/>',
    "suggest": '<rect x="2" y="5" width="14" height="8" stroke-dasharray="2 2"/>',
    "focus": '<circle cx="9" cy="9" r="6" class="full"/>',
    "flag": '<circle cx="9" cy="9" r="6.5" class="alarm"/><circle cx="9" cy="9" r="4" class="alarm"/>',
}
LINE = {
    "transfer": '<path d="M1 9H24"/><path d="M24 9l-6-3v6z" class="full"/>',
    "refer": '<path d="M1 9H24" stroke-dasharray="8 3 2 3"/>',
    "device": '<path d="M1 9H27" stroke-dasharray="5 4" class="faint"/>',
    "identifier": '<path d="M1 9H27" stroke-dasharray="1.5 3" class="faint"/>',
    "purchase": '<path d="M1 9H27" class="faint"/>',
}
WORDS = [
    (MARK, "person", "Người"), (MARK, "merchant", "Cửa hàng"), (MARK, "device", "Thiết bị"),
    (MARK, "identifier", "Số điện thoại, email, giấy tờ"), (MARK, "item", "Dịch vụ, sản phẩm"), (MARK, "focus", "Đang xem"),
    (MARK, "flag", "Cần lưu ý"), (LINE, "transfer", "Chuyển tiền, càng dày càng nhiều lần"), (LINE, "refer", "Giới thiệu"),
    (MARK, "suggest", "Được gợi ý"), (LINE, "device", "Dùng thiết bị"), (LINE, "identifier", "Có chung số, email, giấy tờ"), (LINE, "purchase", "Mua, sở hữu"),
]  # fmt: skip
HTML = '<div class="gp-net"></div><div class="gp-legend">' + "".join(
    f'<span><svg width="{28 if marks is LINE else 18}" height="18">{marks[k]}</svg>{word}</span>' for marks, k, word in WORDS
) + "</div>"  # fmt: skip
CSS = """
.gp-net { width: 100%; border-top: 1px solid var(--st-text-color); border-bottom: 1px solid var(--st-text-color); }
.gp-legend { display: flex; flex-wrap: wrap; gap: 4px 18px; padding-top: 8px; font-size: 12.5px; color: var(--st-text-color); }
.gp-legend span { display: inline-flex; align-items: center; gap: 5px; }
.gp-legend svg { fill: none; stroke: var(--st-text-color); stroke-width: 1.2; }
.gp-legend .full { fill: var(--st-text-color); }
.gp-legend .faint { stroke: #8a8a84; }
.gp-legend .alarm { stroke: #b3261e; }
"""

# three steps, left to right: each unit's own records, the same person recognized across units, one network
DIAGRAM = """
<svg viewBox="0 0 960 250" width="100%" role="img" aria-label="Dữ liệu của các đơn vị được nối thành một mạng"
     style="font-family:inherit;font-size:13px;fill:currentColor;stroke:currentColor;stroke-width:1.1">
  <g stroke="none">
    <text x="0" y="16" font-weight="600">1. Mỗi đơn vị giữ dữ liệu riêng</text>
    <text x="350" y="16" font-weight="600">2. Nhận ra cùng một người</text>
    <text x="640" y="16" font-weight="600">3. Thành một mạng chung</text>
    {units}
    <text x="350" y="232">trùng số điện thoại, email, giấy tờ</text>
    <text x="640" y="232">ai chuyển tiền cho ai, ai dùng chung thiết bị</text>
  </g>
  <g fill="none">
    {dots}
    {joins}
    <path d="M470 125H610" stroke-dasharray="2 4"/><path d="M610 125l-8-4v8z" fill="currentColor"/>
    <path d="M700 125 770 70M700 125 780 160M770 70 860 95M780 160 860 95M780 160 850 200M700 125 660 70M860 95 920 60"/>
    <circle cx="770" cy="70" r="7"/><circle cx="780" cy="160" r="7"/><circle cx="920" cy="60" r="7"/>
    <rect x="853" y="88" width="14" height="14"/><path d="M850 191 859 200 850 209 841 200Z"/><path d="M660 62 668 76H652Z"/>
  </g>
  <circle cx="440" cy="125" r="9"/><circle cx="700" cy="125" r="9"/>
</svg>
"""


def diagram(units: list[str]) -> str:
    ys = [44 + i * 172 / max(len(units) - 1, 1) for i in range(len(units))]
    drawn = DIAGRAM.format(
        units="".join(f'<text x="0" y="{y + 4:.0f}">{escape(u)}</text>' for u, y in zip(units, ys, strict=True)),
        dots="".join(f'<circle cx="190" cy="{y:.0f}" r="5"/>' for y in ys),
        joins="".join(f'<path d="M196 {y:.0f} 431 125"/>' for y in ys),
    )
    return " ".join(drawn.split())


def network():
    """Register the picture component. Called once per run of the interface script."""
    script = (HERE / "cytoscape.min.js").read_text(encoding="utf-8") + "\n" + (HERE / "network.js").read_text(encoding="utf-8")
    return st.components.v2.component("gp_network", html=HTML, css=CSS, js=script, isolate_styles=False)
