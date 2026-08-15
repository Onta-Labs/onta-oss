#!/usr/bin/env python3
"""Render the looping README demo SVGs (terminal + live graph).

These are SMIL animations — GitHub READMEs play them inline, no JS.
Regenerate:

    python scripts/render_readme_demos.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DUR = 12.0


def esc(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def fade(attr: str, appear: float, hold_until: float = 0.94) -> str:
    """Opacity 0 → 1 at `appear` (fraction of loop), hold, then reset."""
    a0 = max(0.0, appear - 0.012)
    a1 = appear
    return (
        f'<animate attributeName="{attr}" '
        f'values="0;0;1;1;0" '
        f'keyTimes="0;{a0:.4f};{a1:.4f};{hold_until:.2f};1" '
        f'dur="{DUR}s" repeatCount="indefinite"/>'
    )


def type_line(x: float, y: float, text: str, start: float, step: float = 0.014,
              fill: str = "#d7e2f5", prefix_w: float = 0.0) -> str:
    """Per-glyph typewriter. Monospace ~6.6px at 11px."""
    out = []
    cx = x + prefix_w
    t = start
    for ch in text:
        if ch == " ":
            cx += 6.6
            t += step
            continue
        out.append(
            f'<text x="{cx:.1f}" y="{y}" font-size="11" fill="{fill}" '
            f'opacity="0">{esc(ch)}{fade("opacity", t)}</text>'
        )
        cx += 6.6
        t += step
    return "\n".join(out)


def node(cx, cy, label, color, appear, r=11, label_dy=20, weight="600"):
    glow = (
        f'<circle cx="{cx}" cy="{cy}" r="22" fill="{color}" opacity="0">'
        f'{fade("opacity", appear)}</circle>'
        if appear
        else ""
    )
    # glow used as soft halo via low-opacity fill — keep it faint
    halo = (
        f'<circle cx="{cx}" cy="{cy}" r="18" fill="{color}" opacity="0">'
        f'<animate attributeName="opacity" values="0;0;0.22;0.12;0.12;0" '
        f'keyTimes="0;{max(0,appear-0.01):.4f};{appear:.4f};{appear+0.04:.4f};0.94;1" '
        f'dur="{DUR}s" repeatCount="indefinite"/></circle>'
    )
    return f"""{halo}
<circle cx="{cx}" cy="{cy}" r="{r}" fill="#141a2c" stroke="{color}" stroke-width="1.6" opacity="0">
  {fade("opacity", appear)}
</circle>
<text x="{cx}" y="{cy + label_dy}" font-size="10.5" fill="#d7e2f5" text-anchor="middle" font-weight="{weight}" opacity="0">{esc(label)}{fade("opacity", appear + 0.015)}</text>"""


def dim_node(cx, cy, label, color="#3a4563"):
    return f"""<circle cx="{cx}" cy="{cy}" r="5" fill="{color}"/>
<text x="{cx}" y="{cy + 16}" font-size="9" fill="#6b7694" text-anchor="middle">{esc(label)}</text>"""


def edge(x1, y1, x2, y2, appear, color="#f0b429", width=2.2):
    return f"""<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}" stroke-linecap="round" opacity="0">
  {fade("opacity", appear)}
</line>"""


def edge_label(x, y, text, appear, fill="#f0b429"):
    return (
        f'<text x="{x}" y="{y}" font-size="9" fill="{fill}" text-anchor="middle" '
        f'opacity="0" font-weight="600">{esc(text)}{fade("opacity", appear)}</text>'
    )


def write_ask() -> str:
    cmd1 = "infona ask --kg trials"
    cmd2 = '"AZ Phase 3 NSCLC?"'
    az = (430, 210)
    aurora = (620, 118)
    nsclc = (800, 210)
    tagrisso = (620, 300)
    merck = (500, 52)
    kn189 = (740, 52)
    roche = (820, 300)
    msk = (400, 300)

    parts = []
    parts.append(f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 360" width="900" height="360" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" role="img" aria-label="infona ask lighting AstraZeneca to AURORA-3 to NSCLC">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#0b1020"/>
    <stop offset="1" stop-color="#12182c"/>
  </linearGradient>
</defs>
<rect x="1" y="1" width="898" height="358" rx="14" fill="url(#bg)" stroke="#243049" stroke-width="1.5"/>
<line x1="348" y1="16" x2="348" y2="344" stroke="#243049" stroke-width="1"/>
<circle cx="28" cy="28" r="4.5" fill="#ff5f56"/>
<circle cx="46" cy="28" r="4.5" fill="#ffbd2e"/>
<circle cx="64" cy="28" r="4.5" fill="#27c93f"/>
<text x="332" y="32" text-anchor="end" font-size="10" fill="#6b7694">infona</text>
<text x="20" y="70" font-size="11" fill="#6ee7b7" font-weight="600">$</text>
''')
    parts.append(type_line(33, 70, cmd1, start=0.04))
    parts.append(
        f'<text x="20" y="88" font-size="11" fill="#6ee7b7" font-weight="600" '
        f'opacity="0">&gt;{fade("opacity", 0.22)}</text>'
    )
    parts.append(type_line(33, 88, cmd2, start=0.23))

    # answer block
    answers = [
        (114, "MATCH (s:Sponsor)-[:runs]->(t:Trial)"),
        (132, "      -[:indication]->(i:Indication)"),
        (150, "WHERE s.name = 'AstraZeneca'"),
        (168, "  AND t.phase = 'Phase 3'"),
        (186, "  AND i.name = 'NSCLC'"),
        (212, "Path  AstraZeneca --runs--> AURORA-3"),
        (230, "         --indication--> NSCLC"),
        (256, "AURORA-3   Tagrisso   Active"),
        (274, "FLAURA     Tagrisso   Completed"),
    ]
    appear = 0.48
    for y, line in answers:
        fill = "#8eb4ff" if line.startswith("MATCH") or line.startswith(" ") and "Path" not in line else "#d7e2f5"
        if line.startswith("Path"):
            fill = "#e85a2b"
        if line.startswith("AURORA") or line.startswith("FLAURA"):
            fill = "#f5b183"
        parts.append(
            f'<text x="20" y="{y}" font-size="11" fill="{fill}" opacity="0">{esc(line)}{fade("opacity", appear)}</text>'
        )
        appear += 0.03

    parts.append('<g font-family="ui-sans-serif, system-ui, sans-serif">')
    for n in (
        dim_node(*merck, "Merck"),
        dim_node(*kn189, "KEYNOTE-189"),
        dim_node(*roche, "Roche"),
        dim_node(*msk, "MSK"),
        dim_node(*tagrisso, "Tagrisso", "#4a5568"),
    ):
        parts.append(n)
    parts.append(f'<line x1="{az[0]}" y1="{az[1]}" x2="{tagrisso[0]}" y2="{tagrisso[1]}" stroke="#2a3348" stroke-width="1"/>')
    parts.append(f'<line x1="{aurora[0]}" y1="{aurora[1]}" x2="{kn189[0]}" y2="{kn189[1]}" stroke="#2a3348" stroke-width="1"/>')

    parts.append(edge(*az, *aurora, 0.58, "#7aa2ff", 2.4))
    parts.append(edge_label(520, 150, "runs", 0.60, "#7aa2ff"))
    parts.append(node(*az, "AstraZeneca", "#7aa2ff", 0.56, r=12))
    parts.append(node(*aurora, "AURORA-3", "#e85a2b", 0.64, r=13))
    parts.append(edge(*aurora, *nsclc, 0.72, "#e85a2b", 2.4))
    parts.append(edge_label(715, 150, "indication", 0.74, "#e85a2b"))
    parts.append(node(*nsclc, "NSCLC", "#c084fc", 0.78, r=12))
    parts.append("</g></svg>")
    return "\n".join(parts)


def write_ingest() -> str:
    cmd1 = "infona ingest --kg trials"
    cmd2 = "examples/trials.csv"
    trials = [
        (540, 78, "AURORA-3"),
        (670, 70, "KEYNOTE-189"),
        (790, 100, "IMvigor011"),
        (580, 168, "CASPIAN"),
        (730, 168, "MARIPOSA"),
    ]
    sponsors = [
        (420, 250, "AstraZeneca"),
        (560, 300, "Merck"),
        (720, 305, "Roche"),
        (840, 250, "J&J"),
    ]
    indications = [
        (470, 38, "NSCLC"),
        (820, 40, "TNBC"),
        (880, 165, "RCC"),
    ]

    parts = []
    parts.append(f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 360" width="900" height="360" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" role="img" aria-label="infona ingest turning a CSV into a live knowledge graph">
<defs>
  <linearGradient id="bg2" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#0b1020"/>
    <stop offset="1" stop-color="#12182c"/>
  </linearGradient>
</defs>
<rect x="1" y="1" width="898" height="358" rx="14" fill="url(#bg2)" stroke="#243049" stroke-width="1.5"/>
<line x1="348" y1="16" x2="348" y2="344" stroke="#243049" stroke-width="1"/>
<circle cx="28" cy="28" r="4.5" fill="#ff5f56"/>
<circle cx="46" cy="28" r="4.5" fill="#ffbd2e"/>
<circle cx="64" cy="28" r="4.5" fill="#27c93f"/>
<text x="332" y="32" text-anchor="end" font-size="10" fill="#6b7694">infona</text>
<text x="20" y="70" font-size="11" fill="#6ee7b7" font-weight="600">$</text>
''')
    parts.append(type_line(33, 70, cmd1, start=0.04))
    parts.append(
        f'<text x="20" y="88" font-size="11" fill="#6ee7b7" font-weight="600" '
        f'opacity="0">&gt;{fade("opacity", 0.20)}</text>'
    )
    parts.append(type_line(33, 88, cmd2, start=0.21))

    log = [
        (0.36, 118, "#8eb4ff", "schema   1 LLM pass"),
        (0.40, 136, "#d7e2f5", "types    Trial · Sponsor · Drug · Indication"),
        (0.44, 154, "#d7e2f5", "edges    runs · studies · indication"),
        (0.50, 178, "#6ee7b7", "rows     16 / 16 mapped deterministically"),
        (0.56, 202, "#f5b183", "graph    16 trials · 8 sponsors · 11 drugs"),
        (0.62, 226, "#d7e2f5", "store    Neo4j  ·  Cypher ready"),
        (0.70, 258, "#8eb4ff", "next     infona ask --kg trials"),
    ]
    for appear, y, fill, line in log:
        parts.append(
            f'<text x="20" y="{y}" font-size="11" fill="{fill}" opacity="0">{esc(line)}{fade("opacity", appear)}</text>'
        )

    parts.append('<g font-family="ui-sans-serif, system-ui, sans-serif">')
    t = 0.42
    for x, y, label in sponsors:
        parts.append(node(x, y, label, "#7aa2ff", t, r=9, label_dy=18))
        t += 0.03
    for x, y, label in trials:
        parts.append(node(x, y, label, "#e85a2b", t, r=10, label_dy=18))
        t += 0.025
    for x, y, label in indications:
        parts.append(node(x, y, label, "#c084fc", t, r=8, label_dy=16))
        t += 0.03
    links = [
        (420, 250, 540, 78, 0.72),   # AZ-AURORA
        (540, 78, 470, 38, 0.76),    # AURORA-NSCLC
        (560, 300, 670, 70, 0.78),   # Merck-KN189
        (670, 70, 470, 38, 0.81),    # KN189-NSCLC
        (720, 305, 790, 100, 0.83),  # Roche-IMvigor
        (840, 250, 730, 168, 0.86),  # J&J-MARIPOSA
        (420, 250, 580, 168, 0.88),  # AZ-CASPIAN
    ]
    for x1, y1, x2, y2, appear in links:
        parts.append(edge(x1, y1, x2, y2, appear, "#5b6b8c", 1.4))
    parts.append("</g></svg>")
    return "\n".join(parts)


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    (DOCS / "demo-ask.svg").write_text(write_ask() + "\n")
    (DOCS / "demo-ingest.svg").write_text(write_ingest() + "\n")
    print("wrote", DOCS / "demo-ask.svg")
    print("wrote", DOCS / "demo-ingest.svg")


if __name__ == "__main__":
    main()
