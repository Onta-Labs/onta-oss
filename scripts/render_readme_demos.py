#!/usr/bin/env python3
"""Render the looping README demo SVGs (terminal + live graph).

These are SMIL animations — GitHub READMEs play them inline, no JS, no video.
Everything in the terminal panes mirrors the real CLI output (`cliIngest.ts`,
`cliShared.ts` renderMapping, `cliQuery.ts` + `--debug`), and every node and
edge in the graph panes comes from `examples/trials.csv`.

Regenerate:

    python scripts/render_readme_demos.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "readme"

# ---------------------------------------------------------------------------
# Design system — brand ink + one coral accent (docs/brand lockup colors).
# ---------------------------------------------------------------------------

W, H = 960, 440
BAR_H = 36
DIV_X = 440

INK_0 = "#0e1512"  # canvas gradient start
INK_1 = "#121b16"  # canvas gradient end
BAR = "#131d18"  # title bar
HAIR = "#233029"  # hairlines / border
GRID = "#18221c"  # graph-pane dot grid

FG = "#e9efeb"  # primary text (brand reverse)
OUT_FG = "#aebbb2"  # normal output
DIM = "#6c7b72"  # secondary
FAINT = "#49574f"  # tertiary / structural
ACCENT = "#f0734e"  # brand coral — reserved for the answer path
OK = "#7fb096"  # success counters
CY_KW = "#7ea3c0"  # cypher keywords
CY_STR = "#d99873"  # cypher string literals

C_SPONSOR = "#7e93ad"
C_TRIAL = "#7fa892"
C_DRUG = "#ab9a76"
C_IND = "#a189a6"

FS = 11.5  # terminal font size
ADV = FS * 0.602  # measured monospace advance
LH = 19  # terminal line height
TX = 22  # terminal left margin

MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

END = 0.955  # loop-wide fade-out start (fraction of DUR)

_uid = 0


def uid(prefix: str) -> str:
    global _uid
    _uid += 1
    return f"{prefix}{_uid}"


def esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def fade(t: float, dur: float, lo: float = 0.0, hi: float = 1.0,
         ease: float = 0.010) -> str:
    """Opacity lo → hi at fraction `t`, hold, graceful fade at loop end."""
    a0 = max(0.0, t - ease)
    return (
        f'<animate attributeName="opacity" '
        f'values="{lo};{lo};{hi};{hi};{lo}" '
        f'keyTimes="0;{a0:.4f};{t:.4f};{END};1" '
        f'dur="{dur}s" repeatCount="indefinite"/>'
    )


def window(t0: float, t1: float, dur: float, hi: float = 1.0) -> str:
    """Opacity on between t0 and t1 only (discrete-ish, quick edges)."""
    e = 0.006
    return (
        f'<animate attributeName="opacity" '
        f'values="0;0;{hi};{hi};0;0" '
        f'keyTimes="0;{max(0.0, t0 - e):.4f};{t0:.4f};{t1:.4f};{min(1.0, t1 + e):.4f};1" '
        f'dur="{dur}s" repeatCount="indefinite"/>'
    )


def typed(x: float, y: float, text: str, t0: float, dur: float,
          cps: float = 34.0, fill: str = FG) -> tuple[str, float]:
    """Typewriter via a discrete clip-width animation (one element per line).

    Returns (markup, fraction-of-loop when typing finishes).
    """
    n = len(text)
    step = (1.0 / cps) / dur
    widths = ["0"] + [f"{ADV * i:.1f}" for i in range(1, n + 1)]
    times = ["0"] + [f"{min(t0 + step * i, 0.999):.4f}" for i in range(1, n + 1)]
    cid = uid("t")
    t_end = t0 + step * n
    markup = (
        f'<clipPath id="{cid}"><rect x="{x}" y="{y - FS}" width="0" height="{FS + 6}">'
        f'<animate attributeName="width" calcMode="discrete" '
        f'values="{";".join(widths)}" keyTimes="{";".join(times)}" '
        f'dur="{dur}s" repeatCount="indefinite"/></rect></clipPath>'
        f'<text x="{x}" y="{y}" font-size="{FS}" fill="{fill}" '
        f'clip-path="url(#{cid})">{esc(text)}'
        f'<animate attributeName="opacity" values="1;1;0" '
        f'keyTimes="0;{END};1" dur="{dur}s" repeatCount="indefinite"/></text>'
    )
    return markup, t_end


def line(x: float, y: float, parts, t: float, dur: float,
         weight: str | None = None) -> str:
    """One output line, printed at `t`. `parts` = str or [(text, fill), ...].

    Browsers collapse runs of whitespace in SVG text, so each word is its
    own tspan pinned to the monospace character grid — indentation and
    column alignment survive any renderer.
    """
    if isinstance(parts, str):
        parts = [(parts, OUT_FG)]
    spans, col = [], 0
    for text, fill in parts:
        for m in re.finditer(r"\S+", text):
            cx = x + ADV * (col + m.start())
            spans.append(
                f'<tspan x="{cx:.1f}" fill="{fill}">{esc(m.group(0))}</tspan>'
            )
        col += len(text)
    w = f' font-weight="{weight}"' if weight else ""
    return (
        f'<text y="{y}" font-size="{FS}"{w} opacity="0">'
        f"{''.join(spans)}{fade(t, dur)}</text>"
    )


def cursor(x: float, y: float, t0: float, t1: float, dur: float) -> str:
    """Blinking block cursor, visible between t0 and t1."""
    return (
        f'<g opacity="0">{window(t0, t1, dur)}'
        f'<rect x="{x:.1f}" y="{y - FS + 1}" width="{ADV:.1f}" height="{FS + 2}" '
        f'fill="{FG}" opacity="0.8">'
        f'<animate attributeName="opacity" calcMode="discrete" '
        f'values="0.8;0" keyTimes="0;0.55" dur="1.1s" repeatCount="indefinite"/>'
        f"</rect></g>"
    )


def frame(dur: float, aria: str, bar_right: str) -> str:
    """Canvas, title bar with the brand dot, pane divider, dot grid."""
    g = uid("bg")
    p = uid("dots")
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{MONO}" xml:space="preserve" role="img" aria-label="{esc(aria)}">
<defs>
  <linearGradient id="{g}" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="{INK_0}"/><stop offset="1" stop-color="{INK_1}"/>
  </linearGradient>
  <pattern id="{p}" width="26" height="26" patternUnits="userSpaceOnUse">
    <circle cx="1" cy="1" r="1" fill="{GRID}"/>
  </pattern>
</defs>
<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12" fill="url(#{g})" stroke="{HAIR}"/>
<rect x="{DIV_X}" y="{BAR_H}" width="{W - DIV_X - 8}" height="{H - BAR_H - 8}" fill="url(#{p})"/>
<path d="M 12.5 0.5 H {W - 12.5} A 12 12 0 0 1 {W - 0.5} 12.5 V {BAR_H - 0.5} H 0.5 V 12.5 A 12 12 0 0 1 12.5 0.5 Z" fill="{BAR}"/>
<line x1="0.5" y1="{BAR_H}" x2="{W - 0.5}" y2="{BAR_H}" stroke="{HAIR}"/>
<line x1="{DIV_X}" y1="{BAR_H}" x2="{DIV_X}" y2="{H - 1}" stroke="{HAIR}"/>
<circle cx="26" cy="{BAR_H / 2}" r="3.5" fill="{ACCENT}"/>
<text x="37" y="{BAR_H / 2 + 4}" font-size="11.5" fill="{FG}" letter-spacing="0.5">infona</text>
<text x="{W - 20}" y="{BAR_H / 2 + 4}" font-size="10.5" fill="{DIM}" text-anchor="end">{esc(bar_right)}</text>
"""


# ---------------------------------------------------------------------------
# Graph pane — real entities from examples/trials.csv, laid out by type tier.
# ---------------------------------------------------------------------------

SPONSORS = {
    "BMS": (494, 96),
    "AstraZeneca": (500, 156),
    "Merck": (496, 238),
    "Roche": (508, 306),
    "Pfizer": (500, 370),
}
TRIALS = {
    "CheckMate-9ER": (662, 72),
    "AURORA-3": (650, 132),
    "FLAURA": (628, 194),
    "KEYNOTE-189": (658, 252),
    "KEYNOTE-522": (634, 310),
    "IMpower150": (646, 358),
    "CROWN": (664, 404),
}
DRUGS = {
    "Opdivo": (795, 96),
    "Tagrisso": (786, 176),
    "Keytruda": (794, 268),
    "Tecentriq": (780, 340),
}
INDICATIONS = {
    "RCC": (900, 76),
    "NSCLC": (892, 152),
    "TNBC": (898, 272),
}
LABEL_ABOVE = {"IMpower150", "CROWN", "TNBC"}
EXTRA_DOTS = [  # the graph is bigger than the labels we draw
    (C_SPONSOR, 489, 52), (C_SPONSOR, 511, 404),
    (C_TRIAL, 625, 56), (C_DRUG, 799, 388), (C_DRUG, 779, 54),
    (C_IND, 888, 336), (C_IND, 903, 390),
]

RUNS = [  # sponsor -> trial (all true in trials.csv)
    ("BMS", "CheckMate-9ER"),
    ("AstraZeneca", "AURORA-3"),
    ("AstraZeneca", "FLAURA"),
    ("Merck", "KEYNOTE-189"),
    ("Merck", "KEYNOTE-522"),
    ("Roche", "IMpower150"),
    ("Pfizer", "CROWN"),
]
STUDIES = [  # trial -> drug (brand names)
    ("CheckMate-9ER", "Opdivo"),
    ("AURORA-3", "Tagrisso"),
    ("FLAURA", "Tagrisso"),
    ("KEYNOTE-189", "Keytruda"),
    ("KEYNOTE-522", "Keytruda"),
    ("IMpower150", "Tecentriq"),
]
INDICATION_OF = [  # trial -> indication (visual subset; IMpower150/CROWN
    ("CheckMate-9ER", "RCC"),  # also hit NSCLC but those chords cross the
    ("AURORA-3", "NSCLC"),  # drug tier, so we leave them to the dots)
    ("FLAURA", "NSCLC"),
    ("KEYNOTE-189", "NSCLC"),
    ("KEYNOTE-522", "TNBC"),
]

POS = {**SPONSORS, **TRIALS, **DRUGS, **INDICATIONS}
COLOR = (
    {k: C_SPONSOR for k in SPONSORS}
    | {k: C_TRIAL for k in TRIALS}
    | {k: C_DRUG for k in DRUGS}
    | {k: C_IND for k in INDICATIONS}
)

HEADERS = [  # column captions — schema types over their tier
    ("SPONSOR", 500), ("TRIAL", 648), ("DRUG", 788), ("INDICATION", 895),
]


def trim(x1, y1, x2, y2, pad=9.0):
    dx, dy = x2 - x1, y2 - y1
    d = (dx * dx + dy * dy) ** 0.5 or 1.0
    ux, uy = dx / d, dy / d
    return x1 + ux * pad, y1 + uy * pad, x2 - ux * pad, y2 - uy * pad


def drawn_edge(a: str, b: str, t0: float, t1: float, dur: float,
               color: str = FAINT, width: float = 1.1, pad: float = 9.0) -> str:
    """Edge that draws itself from a to b (stroke-dash reveal)."""
    x1, y1, x2, y2 = trim(*POS[a], *POS[b], pad)
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{color}" stroke-width="{width}" stroke-linecap="round" '
        f'stroke-dasharray="{length:.1f}" stroke-dashoffset="{length:.1f}" opacity="0">'
        f'<animate attributeName="stroke-dashoffset" '
        f'values="{length:.1f};{length:.1f};0;0" '
        f'keyTimes="0;{t0:.4f};{t1:.4f};1" calcMode="spline" '
        f'keySplines="0 0 1 1;0.25 0.6 0.3 1;0 0 1 1" '
        f'dur="{dur}s" repeatCount="indefinite"/>'
        f"{fade(t0, dur)}</line>"
    )


def static_edge(a: str, b: str, color: str = FAINT, width: float = 1.1) -> str:
    x1, y1, x2, y2 = trim(*POS[a], *POS[b])
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{color}" stroke-width="{width}" stroke-linecap="round"/>'
    )


def node(name: str, t: float | None, dur: float, r: float = 5.0) -> str:
    """Labeled node. If t is None the node is static (no entrance)."""
    x, y = POS[name]
    color = COLOR[name]
    ly = y - 13 if name in LABEL_ABOVE else y + 17
    pop = ""
    if t is not None:
        pop = (
            f'<animate attributeName="r" values="0.1;0.1;{r * 1.5:.1f};{r};{r}" '
            f'keyTimes="0;{max(0.0, t - 0.01):.4f};{t:.4f};{min(0.999, t + 0.02):.4f};1" '
            f'dur="{dur}s" repeatCount="indefinite"/>'
        )
    gate = fade(t, dur) if t is not None else ""
    lgate = fade(min(0.999, t + 0.015), dur) if t is not None else ""
    op = ' opacity="0"' if t is not None else ""
    return (
        f'<circle cx="{x}" cy="{y}" r="{r + 3.5}" fill="none" stroke="{color}" '
        f'stroke-opacity="0.30"{op}>{gate}</circle>'
        f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"{op}>{pop}{gate}</circle>'
        f'<text x="{x}" y="{ly}" font-size="9.5" fill="#c2cec6" '
        f'text-anchor="middle"{op}>{esc(name)}{lgate}</text>'
    )


def dot(color: str, x: float, y: float, t: float | None, dur: float) -> str:
    if t is None:
        return f'<circle cx="{x}" cy="{y}" r="3" fill="{color}" fill-opacity="0.55"/>'
    return (
        f'<circle cx="{x}" cy="{y}" r="3" fill="{color}" fill-opacity="0.55" '
        f'opacity="0">{fade(t, dur)}</circle>'
    )


def headers(t: float | None, dur: float) -> str:
    out = []
    for text, x in HEADERS:
        gate = fade(t, dur) if t is not None else ""
        op = ' opacity="0"' if t is not None else ""
        out.append(
            f'<text x="{x}" y="58" font-size="8.5" fill="{FAINT}" '
            f'text-anchor="middle" letter-spacing="1.6"{op}>{text}{gate}</text>'
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# demo-ingest — one schema pass, then every row lands deterministically.
# ---------------------------------------------------------------------------

def write_ingest() -> str:
    dur = 14.0
    parts = [frame(dur, "infona ingest turning trials.csv into a typed Neo4j graph",
                   "examples/trials.csv → kg: trials")]

    y = 62.0
    parts.append(f'<text x="{TX}" y="{y}" font-size="{FS}" fill="{ACCENT}" font-weight="600">$</text>')
    cmd, t = typed(TX + 2 * ADV, y, "infona ingest examples/trials.csv --kg trials", 0.030, dur)
    parts.append(cmd)

    def out(rows, t, dy=1.0, weight=None):
        nonlocal y
        y += LH * dy
        parts.append(line(TX, y, rows, t, dur, weight))
        return t

    out("Ingesting examples/trials.csv...", t + 0.015)

    t0 = t + 0.095  # one LLM pass for the shape — let it think a beat
    out([("Proposed schema", FG), ("  (profiled 16 of 16 rows)", DIM)], t0, dy=1.6, weight="600")
    out([("Entities & keys", FG)], t0 + 0.020, dy=1.4, weight="600")
    out([("  • ", DIM), ("Trial", C_TRIAL), ("  key: trial_id", DIM), ("  (0.98)", FAINT)], t0 + 0.035)
    out([("      [attr] ", FAINT), ("phase · status · enrollment", OUT_FG), (" [int]", FAINT)], t0 + 0.050)
    out([("      [edge] ", FAINT), ("sponsor → ", OUT_FG), ("Sponsor", C_SPONSOR), ("   drug → ", OUT_FG), ("Drug", C_DRUG)], t0 + 0.065)
    out([("      [edge] ", FAINT), ("indication → ", OUT_FG), ("Indication", C_IND)], t0 + 0.080)
    out([("  • ", DIM), ("Sponsor", C_SPONSOR), (" · ", DIM), ("Drug", C_DRUG), (" · ", DIM), ("Indication", C_IND), (" · ", DIM), ("Site", OUT_FG), ("  key: name", DIM)], t0 + 0.095)
    out([("Edges", FG)], t0 + 0.125, dy=1.5, weight="600")
    out([("  • ", DIM), ("Sponsor", C_SPONSOR), (" runs ", DIM), ("Trial", C_TRIAL), ("      (0.97)", FAINT)], t0 + 0.140)
    out([("  • ", DIM), ("Trial", C_TRIAL), (" studies ", DIM), ("Drug", C_DRUG), ("  ·  ", DIM), ("Trial", C_TRIAL), (" indication ", DIM), ("Indication", C_IND)], t0 + 0.155)

    ta = t0 + 0.20
    y += LH * 1.5
    parts.append(line(TX, y, [("Apply this mapping and ingest 16 rows?", FG)], ta, dur))
    yk, _ = typed(TX + ADV * 39, y, "y", ta + 0.030, dur, fill=OK)
    parts.append(yk)

    # rows land — counters mirror printIngestResult()
    tw = ta + 0.055
    out([("  Entities resolved:  ", DIM), ("48", OK)], tw + 0.10, dy=1.5)
    out([("  Triples inserted:   ", DIM), ("208", OK)], tw + 0.16)
    out([("  Types created:  ", DIM), ("Trial", C_TRIAL), (", ", DIM), ("Sponsor", C_SPONSOR), (", ", DIM), ("Drug", C_DRUG), (", ", DIM), ("Indication", C_IND), (", ", DIM), ("Site", OUT_FG)], tw + 0.22)

    y += LH * 1.6
    parts.append(line(TX, y, [("$", ACCENT)], tw + 0.30, dur))
    parts.append(cursor(TX + 2 * ADV, y, tw + 0.30, END, dur))

    # ---- graph pane: the same schema assembling as rows land ----
    parts.append(headers(tw - 0.02, dur))

    tt = tw
    for name in SPONSORS:
        parts.append(node(name, tt, dur))
        tt += 0.012
    tt = tw + 0.03
    for a, b in RUNS:
        parts.append(drawn_edge(a, b, tt, tt + 0.030, dur))
        tt += 0.013
    tt = tw + 0.06
    for name in TRIALS:
        parts.append(node(name, tt, dur))
        tt += 0.012
    tt = tw + 0.12
    for name in list(DRUGS) + list(INDICATIONS):
        parts.append(node(name, tt, dur))
        tt += 0.009
    tt = tw + 0.15
    for a, b in STUDIES + INDICATION_OF:
        parts.append(drawn_edge(a, b, tt, tt + 0.030, dur))
        tt += 0.008
    for i, (c, x, yy) in enumerate(EXTRA_DOTS):
        parts.append(dot(c, x, yy, tw + 0.05 + i * 0.02, dur))

    # row progress, bottom-right of the pane
    px, py = W - 24, H - 20
    steps = [(tw, tw + 0.08, "rows   5/16"), (tw + 0.08, tw + 0.16, "rows  11/16")]
    for a, b, label in steps:
        parts.append(
            f'<text x="{px}" y="{py}" font-size="9.5" fill="{DIM}" '
            f'text-anchor="end" opacity="0">{label}{window(a, b, dur)}</text>'
        )
    parts.append(
        f'<text x="{px}" y="{py}" font-size="9.5" fill="{OK}" '
        f'text-anchor="end" opacity="0">rows  16/16 ✓{fade(tw + 0.16, dur)}</text>'
    )

    parts.append("</svg>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# demo-ask — English in, Cypher on the populated graph, exact answer out.
# ---------------------------------------------------------------------------

def write_ask() -> str:
    dur = 16.0
    parts = [frame(dur, "infona ask compiling English to Cypher and lighting the answer path",
                   "kg: trials · neo4j")]

    # The populated graph is already there — ask queries it. It dims when
    # the match starts so the answer path carries the pane.
    dim_at = 0.364  # == p1 below
    parts.append(f'<g opacity="0">'
                 f'<animate attributeName="opacity" values="0;0.85;0.85;0.38;0.38;0" '
                 f'keyTimes="0;0.03;{dim_at - 0.01:.4f};{dim_at + 0.05:.4f};{END};1" '
                 f'dur="{dur}s" repeatCount="indefinite"/>')
    parts.append(headers(None, dur))
    for a, b in RUNS + STUDIES + INDICATION_OF:
        if (a, b) not in (("AstraZeneca", "AURORA-3"), ("AURORA-3", "NSCLC")):
            parts.append(static_edge(a, b))
    for name in POS:
        if name not in ("AstraZeneca", "AURORA-3", "NSCLC"):
            parts.append(node(name, None, dur))
    for c, x, yy in EXTRA_DOTS:
        parts.append(dot(c, x, yy, None, dur))
    parts.append("</g>")

    # dim base state for the three path nodes (lit later)
    parts.append(f'<g opacity="0">'
                 f'<animate attributeName="opacity" values="0;0.85;0.85;0" '
                 f'keyTimes="0;0.03;{END};1" dur="{dur}s" repeatCount="indefinite"/>'
                 + static_edge("AstraZeneca", "AURORA-3")
                 + static_edge("AURORA-3", "NSCLC")
                 + node("AstraZeneca", None, dur)
                 + node("AURORA-3", None, dur)
                 + node("NSCLC", None, dur)
                 + "</g>")

    # ---- terminal ----
    y = 62.0
    parts.append(f'<text x="{TX}" y="{y}" font-size="{FS}" fill="{ACCENT}" font-weight="600">$</text>')
    c1, t = typed(TX + 2 * ADV, y, "infona ask --kg trials --debug \\", 0.030, dur, cps=40)
    parts.append(c1)
    y += LH
    c2, t = typed(TX + 2 * ADV, y, '"Which Phase 3 NSCLC trials is AstraZeneca running?"', t + 0.008, dur, cps=40)
    parts.append(c2)

    def out(rows, t, dy=1.0):
        nonlocal y
        y += LH * dy
        parts.append(line(TX, y, rows, t, dur))
        return t

    out([("Q: Which Phase 3 NSCLC trials is AstraZeneca running?", DIM)], t + 0.020, dy=1.5)
    tg = t + 0.035
    out([("Generating answer...", DIM)], tg)

    # --debug: the compiled Cypher (formatAskDebug)
    tc = tg + 0.115  # the LLM call is the slow part — let it breathe
    parts.append(cursor(TX + 21 * ADV, y, tg + 0.010, tc, dur))
    out([("Cypher:", FG)], tc, dy=1.6)
    out([("MATCH ", CY_KW), ("(s:", OUT_FG), ("Sponsor", C_SPONSOR), (" {name: ", OUT_FG), ("'AstraZeneca'", CY_STR), ("})", OUT_FG)], tc + 0.030, dy=1.3)
    out([("      -[:", OUT_FG), ("runs", CY_KW), ("]->(t:", OUT_FG), ("Trial", C_TRIAL), (" {phase: ", OUT_FG), ("'Phase 3'", CY_STR), ("})", OUT_FG)], tc + 0.060)
    out([("      -[:", OUT_FG), ("indication", CY_KW), ("]->(i:", OUT_FG), ("Indication", C_IND), (" {name: ", OUT_FG), ("'NSCLC'", CY_STR), ("})", OUT_FG)], tc + 0.090)
    out([("RETURN", CY_KW), (" t.trial, t.status, t.enrollment", OUT_FG)], tc + 0.120)

    # exact answer
    ta = tc + 0.210
    out([("A: ", ACCENT), ("One — ", FG), ("AURORA-3", ACCENT), (", the only active Phase 3", FG)], ta, dy=1.7)
    out([("   NSCLC trial AstraZeneca is running:", FG)], ta + 0.012)
    out([("   osimertinib (Tagrisso) · first-line · 557 enrolled", OUT_FG)], ta + 0.024)
    y += LH * 1.4
    parts.append(line(TX, y, [("1 row · exact match from kg: trials", DIM)], ta + 0.055, dur))

    y += LH * 1.6
    parts.append(line(TX, y, [("$", ACCENT)], ta + 0.09, dur))
    parts.append(cursor(TX + 2 * ADV, y, ta + 0.09, END, dur))

    # ---- graph pane: the path lights hop by hop as the Cypher plans it ----
    glow = uid("glow")
    parts.append(
        f'<defs><filter id="{glow}" x="-80%" y="-80%" width="260%" height="260%">'
        f'<feGaussianBlur stdDeviation="4"/></filter></defs>'
    )

    def lit_node(name: str, t: float, label_dy: int = 17) -> str:
        x, yy = POS[name]
        return (
            f'<g opacity="0">{fade(t, dur)}'
            f'<circle cx="{x}" cy="{yy}" r="9" fill="{ACCENT}" opacity="0.45" filter="url(#{glow})"/>'
            f'<circle cx="{x}" cy="{yy}" r="8.5" fill="none" stroke="{ACCENT}" stroke-opacity="0.55"/>'
            f'<circle cx="{x}" cy="{yy}" r="5" fill="{ACCENT}"/>'
            f'<text x="{x}" y="{yy + label_dy}" font-size="10" font-weight="600" '
            f'fill="{FG}" text-anchor="middle">{esc(name)}</text></g>'
        )

    def lit_edge(a: str, b: str, t0: float, t1: float) -> str:
        under = drawn_edge(a, b, t0, t1, dur, ACCENT, 2.2, pad=11)
        return f'<g filter="url(#{glow})" opacity="0.5">{under}</g>' + \
               drawn_edge(a, b, t0, t1, dur, ACCENT, 2.2, pad=11)

    p1, p2 = tc + 0.045, tc + 0.105  # sync with the MATCH lines printing
    parts.append(lit_node("AstraZeneca", p1 - 0.012))
    parts.append(lit_edge("AstraZeneca", "AURORA-3", p1, p1 + 0.035))
    mx1, my1 = (POS["AstraZeneca"][0] + POS["AURORA-3"][0]) / 2, (POS["AstraZeneca"][1] + POS["AURORA-3"][1]) / 2
    parts.append(
        f'<text x="{mx1:.0f}" y="{my1 - 8:.0f}" font-size="9" fill="{ACCENT}" '
        f'text-anchor="middle" opacity="0">runs{fade(p1 + 0.030, dur, hi=0.9)}</text>'
    )
    parts.append(lit_node("AURORA-3", p1 + 0.040))
    parts.append(lit_edge("AURORA-3", "NSCLC", p2, p2 + 0.035))
    mx2, my2 = (POS["AURORA-3"][0] + POS["NSCLC"][0]) / 2, (POS["AURORA-3"][1] + POS["NSCLC"][1]) / 2
    parts.append(
        f'<text x="{mx2:.0f}" y="{my2 - 8:.0f}" font-size="9" fill="{ACCENT}" '
        f'text-anchor="middle" opacity="0">indication{fade(p2 + 0.030, dur, hi=0.9)}</text>'
    )
    parts.append(lit_node("NSCLC", p2 + 0.040))

    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "demo-ingest.svg").write_text(write_ingest() + "\n")
    (OUT / "demo-ask.svg").write_text(write_ask() + "\n")
    print("wrote", OUT / "demo-ingest.svg")
    print("wrote", OUT / "demo-ask.svg")


if __name__ == "__main__":
    main()
