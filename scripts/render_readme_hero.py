#!/usr/bin/env python3
"""Render the README hero (docs/readme/hero.png) — the product loop in one still.

English question → compiled Cypher → exact answer, over the populated graph
from examples/trials.csv. Shares node/edge data with render_readme_demos.py so
the hero and the animations are the same graph.

Needs Chromium via Playwright (dev-only dependency, not needed at runtime):

    pip install playwright  # plus a chromium install or PLAYWRIGHT_BROWSERS_PATH
    python scripts/render_readme_hero.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from render_readme_demos import (
    ACCENT, COLOR, DIM, EXTRA_DOTS, FAINT, FG, HAIR, INDICATION_OF, OK,
    OUT_FG, POS, RUNS, STUDIES, LABEL_ABOVE, C_SPONSOR, C_TRIAL, C_DRUG,
    C_IND, CY_KW, CY_STR, trim,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "readme" / "hero.png"

CSS_W, CSS_H = 1520, 640
PATH_EDGES = {("AstraZeneca", "FLAURA2"), ("FLAURA2", "NSCLC")}
PATH_NODES = {"AstraZeneca", "FLAURA2", "NSCLC"}

# demo graph pane is x 452..940, y 44..430 — remap into the 900x620 graph svg
SX, SY = 1.60, 1.42
OX, OY = -663, -21


def m(name: str) -> tuple[float, float]:
    x, y = POS[name]
    return x * SX + OX, y * SY + OY


def graph_svg() -> str:
    w, h = 900, 620
    parts = [
        f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">',
        '<defs>',
        f'<marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="5.5" '
        f'markerHeight="5.5" orient="auto-start-reverse">'
        f'<path d="M 0 0.8 L 7.2 4 L 0 7.2 Z" fill="{ACCENT}"/></marker>',
        '</defs>',
    ]

    def map_edge(a, b, pad):
        ax, ay = m(a)
        bx, by = m(b)
        return trim(ax, ay, bx, by, pad)

    for a, b in RUNS + STUDIES + INDICATION_OF:
        if (a, b) in PATH_EDGES:
            continue
        x1, y1, x2, y2 = map_edge(a, b, 14)
        parts.append(
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            f'stroke="{FAINT}" stroke-width="1.3" stroke-linecap="round" opacity="0.55"/>'
        )
    for a, b in PATH_EDGES:
        x1, y1, x2, y2 = map_edge(a, b, 20)
        for extra in (
            f'stroke-width="7" opacity="0.28" filter="blur(6px)"',
            f'stroke-width="2.8" opacity="1" marker-end="url(#arr)"',
        ):
            parts.append(
                f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
                f'stroke="{ACCENT}" stroke-linecap="round" {extra}/>'
            )

    for label, (a, b), dy in (("runs", ("AstraZeneca", "FLAURA2"), -14),
                              ("indication", ("FLAURA2", "NSCLC"), -14)):
        (ax, ay), (bx, by) = m(a), m(b)
        parts.append(
            f'<text x="{(ax + bx) / 2:.0f}" y="{(ay + by) / 2 + dy:.0f}" font-size="13" '
            f'fill="{ACCENT}" text-anchor="middle" letter-spacing="0.5">{label}</text>'
        )

    for name in POS:
        x, y = m(name)
        color = COLOR[name]
        if name in PATH_NODES:
            parts.append(
                f'<circle cx="{x:.0f}" cy="{y:.0f}" r="15" fill="{ACCENT}" '
                f'opacity="0.35" filter="blur(7px)"/>'
                f'<circle cx="{x:.0f}" cy="{y:.0f}" r="13" fill="none" '
                f'stroke="{ACCENT}" stroke-opacity="0.6" stroke-width="1.4"/>'
                f'<circle cx="{x:.0f}" cy="{y:.0f}" r="7.5" fill="{ACCENT}"/>'
            )
            ly = y - 22 if name in LABEL_ABOVE else y + 30
            parts.append(
                f'<text x="{x:.0f}" y="{ly:.0f}" font-size="15" font-weight="600" '
                f'fill="{FG}" text-anchor="middle">{name}</text>'
            )
        else:
            ly = y - 18 if name in LABEL_ABOVE else y + 26
            parts.append(
                f'<circle cx="{x:.0f}" cy="{y:.0f}" r="10" fill="none" '
                f'stroke="{color}" stroke-opacity="0.25" stroke-width="1.2"/>'
                f'<circle cx="{x:.0f}" cy="{y:.0f}" r="6" fill="{color}" opacity="0.75"/>'
                f'<text x="{x:.0f}" y="{ly:.0f}" font-size="12.5" fill="#93a49a" '
                f'text-anchor="middle">{name}</text>'
            )
    for c, x, y in EXTRA_DOTS:
        hx, hy = x * SX + OX, y * SY + OY
        parts.append(
            f'<circle cx="{hx:.0f}" cy="{hy:.0f}" r="4" fill="{c}" opacity="0.4"/>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def html() -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{ width: {CSS_W}px; height: {CSS_H}px; overflow: hidden; }}
  body {{
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background:
      radial-gradient(1100px 700px at 72% 30%, #16211a 0%, transparent 60%),
      linear-gradient(135deg, #0e1512 0%, #121b16 100%);
    color: {FG};
    position: relative;
  }}
  .grid {{
    position: absolute; inset: 0;
    background-image: radial-gradient(circle, #18221c 1.1px, transparent 1.1px);
    background-size: 30px 30px;
    -webkit-mask-image: linear-gradient(90deg, transparent 0%, #000 30%);
            mask-image: linear-gradient(90deg, transparent 0%, #000 30%);
  }}
  .graph {{ position: absolute; right: 8px; top: 10px; }}
  .col {{
    position: absolute; left: 56px; top: 50%; transform: translateY(-50%);
    width: 560px; display: flex; flex-direction: column; gap: 0;
  }}
  .card {{
    background: rgba(19, 29, 24, 0.92);
    border: 1px solid {HAIR};
    border-radius: 12px;
    padding: 20px 24px;
    box-shadow: 0 18px 44px rgba(0, 0, 0, 0.35);
  }}
  .joint {{ width: 1px; height: 26px; background: {HAIR}; margin-left: 44px; }}
  .k {{ font-size: 12px; color: {DIM}; letter-spacing: 1.8px; margin-bottom: 10px; }}
  .k b {{ color: {ACCENT}; font-weight: 600; }}
  .q {{ font-size: 21px; line-height: 1.45; color: {FG}; }}
  .q .p {{ color: {DIM}; }}
  .cy {{ font-size: 14.5px; line-height: 1.75; color: {OUT_FG}; white-space: pre; }}
  .cy .kw {{ color: {CY_KW}; }} .cy .s {{ color: {CY_STR}; }}
  .cy .sp {{ color: {C_SPONSOR}; }} .cy .tr {{ color: {C_TRIAL}; }} .cy .in {{ color: {C_IND}; }}
  .ans {{ border-left: 3px solid {ACCENT}; }}
  .a1 {{ font-size: 24px; font-weight: 600; color: {FG}; margin-bottom: 8px; }}
  .a1 .hl {{ color: {ACCENT}; }}
  .a2 {{ font-size: 14.5px; color: {OUT_FG}; }}
  .a3 {{ font-size: 12.5px; color: {DIM}; margin-top: 12px; }}
  .a3 .okc {{ color: {OK}; }}
</style></head>
<body>
  <div class="grid"></div>
  <div class="graph">{graph_svg()}</div>
  <div class="col">
    <div class="card">
      <div class="k"><b>●</b>&nbsp; ASK IN ENGLISH</div>
      <div class="q"><span class="p">$ infona ask</span> "Which Phase 3 NSCLC trials is AstraZeneca running?"</div>
    </div>
    <div class="joint"></div>
    <div class="card">
      <div class="k">COMPILED TO CYPHER · KG: TRIALS · NEO4J</div>
      <div class="cy"><span class="kw">MATCH</span> (s:<span class="sp">Sponsor</span> {{name: <span class="s">'AstraZeneca'</span>}})
      -[:<span class="kw">runs</span>]-&gt;(t:<span class="tr">Trial</span> {{phase: <span class="s">'Phase 3'</span>,
                          status: <span class="s">'Active'</span>}})
      -[:<span class="kw">indication</span>]-&gt;(i:<span class="in">Indication</span> {{name: <span class="s">'NSCLC'</span>}})
<span class="kw">RETURN</span> t.trial, t.status, t.enrollment</div>
    </div>
    <div class="joint"></div>
    <div class="card ans">
      <div class="a1"><span class="hl">FLAURA2</span> — Phase 3 · Active</div>
      <div class="a2">osimertinib (Tagrisso) · first-line · 557 enrolled</div>
      <div class="a3"><span class="okc">1 row</span> · an exact answer from the graph — not a vibe</div>
    </div>
  </div>
</body></html>"""


async def render() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_BIN", "/opt/pw-browsers/chromium")
        )
        page = await browser.new_page(
            viewport={"width": CSS_W, "height": CSS_H}, device_scale_factor=2
        )
        await page.set_content(html())
        await page.wait_for_timeout(150)
        await page.screenshot(path=str(OUT))
        await browser.close()

    # ~3x smaller with no visible difference; best-effort if pngquant exists.
    import shutil
    import subprocess

    if shutil.which("pngquant"):
        subprocess.run(
            ["pngquant", "--quality=80-98", "--speed", "1", "--force",
             "-o", str(OUT), str(OUT)],
            check=False,
        )
    print("wrote", OUT)


if __name__ == "__main__":
    asyncio.run(render())
