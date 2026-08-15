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
    cmd1 = "infona ask --kg bookstore"
    cmd2 = '"Orwell to Dystopian?"'
    # graph coords (right pane, x>=350)
    orwell = (430, 210)
    book = (620, 118)
    dyst = (800, 210)
    animal = (620, 300)
    gatsby = (500, 52)
    hobbit = (740, 52)
    dune = (820, 300)
    austen = (400, 300)

    parts = []
    parts.append(f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 360" width="900" height="360" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" role="img" aria-label="infona ask lighting a path across the bookstore knowledge graph">
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
        (114, "MATCH (a:Author)-[:wrote]->(b:Book)"),
        (132, "      -[:genre]->(g:Genre)"),
        (150, "WHERE a.name = 'Orwell'"),
        (168, "  AND g.name = 'Dystopian'"),
        (196, "Path  Orwell --wrote--> 1984"),
        (214, "         --genre--> Dystopian"),
        (240, "1984   $11.99   1949"),
    ]
    appear = 0.50
    for y, line in answers:
        fill = "#8eb4ff" if line.startswith("MATCH") or line.startswith(" ") and "wrote" not in line and "Path" not in line else "#d7e2f5"
        if line.startswith("Path"):
            fill = "#f0b429"
        if line.startswith("1984"):
            fill = "#f5d27a"
        parts.append(
            f'<text x="20" y="{y}" font-size="11" fill="{fill}" opacity="0">{esc(line)}{fade("opacity", appear)}</text>'
        )
        appear += 0.035

    # right graph — dim context
    parts.append('<g font-family="ui-sans-serif, system-ui, sans-serif">')
    for n in (
        dim_node(*gatsby, "Gatsby"),
        dim_node(*hobbit, "Hobbit"),
        dim_node(*dune, "Dune"),
        dim_node(*austen, "Austen"),
        dim_node(*animal, "Animal Farm", "#4a5568"),
    ):
        parts.append(n)
    # faint context edges
    parts.append(f'<line x1="{orwell[0]}" y1="{orwell[1]}" x2="{animal[0]}" y2="{animal[1]}" stroke="#2a3348" stroke-width="1"/>')
    parts.append(f'<line x1="{book[0]}" y1="{book[1]}" x2="{hobbit[0]}" y2="{hobbit[1]}" stroke="#2a3348" stroke-width="1"/>')

    # path lights hop-by-hop after the MATCH appears
    parts.append(edge(*orwell, *book, 0.58, "#7aa2ff", 2.4))
    parts.append(edge_label(520, 150, "wrote", 0.60, "#7aa2ff"))
    parts.append(node(*orwell, "Orwell", "#7aa2ff", 0.56, r=12))
    parts.append(node(*book, "1984", "#f0b429", 0.64, r=13))
    parts.append(edge(*book, *dyst, 0.72, "#f0b429", 2.4))
    parts.append(edge_label(715, 150, "genre", 0.74, "#f0b429"))
    parts.append(node(*dyst, "Dystopian", "#c084fc", 0.78, r=12))
    parts.append("</g></svg>")
    return "\n".join(parts)


def write_ingest() -> str:
    cmd1 = "infona ingest --kg bookstore"
    cmd2 = "examples/bookstore.csv"
    # bloom layout
    books = [
        (520, 80, "1984"),
        (640, 70, "Dune"),
        (760, 95, "Hobbit"),
        (560, 170, "Gatsby"),
        (700, 165, "Neuromancer"),
    ]
    authors = [
        (430, 250, "Orwell"),
        (560, 290, "Herbert"),
        (700, 300, "Tolkien"),
        (820, 250, "Fitzgerald"),
    ]
    genres = [
        (480, 40, "Dystopian"),
        (820, 40, "Sci-Fi"),
        (880, 160, "Fantasy"),
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
        (0.40, 136, "#d7e2f5", "types    Book · Author · Genre · Publisher"),
        (0.44, 154, "#d7e2f5", "edges    wrote · genre · published_by"),
        (0.50, 178, "#6ee7b7", "rows     20 / 20 mapped deterministically"),
        (0.56, 202, "#f5d27a", "graph    20 books · 18 authors · 9 genres"),
        (0.62, 226, "#d7e2f5", "store    Neo4j  ·  Cypher ready"),
        (0.70, 258, "#8eb4ff", "next     infona ask \"...\" --kg bookstore"),
    ]
    for appear, y, fill, line in log:
        parts.append(
            f'<text x="20" y="{y}" font-size="11" fill="{fill}" opacity="0">{esc(line)}{fade("opacity", appear)}</text>'
        )

    parts.append('<g font-family="ui-sans-serif, system-ui, sans-serif">')
    t = 0.42
    # authors first (inferred entities), then books, then genres, then edges
    for x, y, label in authors:
        parts.append(node(x, y, label, "#7aa2ff", t, r=9, label_dy=18))
        t += 0.03
    for x, y, label in books:
        parts.append(node(x, y, label, "#f0b429", t, r=10, label_dy=18))
        t += 0.025
    for x, y, label in genres:
        parts.append(node(x, y, label, "#c084fc", t, r=8, label_dy=16))
        t += 0.03
    # connecting bloom
    links = [
        (430, 250, 520, 80, 0.72),   # Orwell-1984
        (520, 80, 480, 40, 0.76),    # 1984-Dystopian
        (560, 290, 640, 70, 0.78),   # Herbert-Dune
        (640, 70, 820, 40, 0.81),    # Dune-SciFi
        (700, 300, 760, 95, 0.83),   # Tolkien-Hobbit
        (760, 95, 880, 160, 0.86),   # Hobbit-Fantasy
        (820, 250, 560, 170, 0.88),  # Fitzgerald-Gatsby
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
