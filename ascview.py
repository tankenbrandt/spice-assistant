#!/usr/bin/env python3
"""LTspice .asc schematic viewer -> standalone SVG / HTML page.

Opens an LTspice schematic without LTspice. The .asc format already carries
explicit coordinates for every wire, symbol and label, so rendering is a
parse-and-draw problem: no placement or routing is required.

    python ascview.py "Sallen-Key Filter Sim.asc" --open
    python ascview.py sch.asc -o out.svg --theme dark
    python ascview.py *.asc --check        # validate symbol pin geometry

`--open` writes an HTML page and opens it in the default browser, sized for a
clean screenshot into a lab report.

Symbol geometry
---------------
Pin offsets for `res`, `cap`, `voltage`, `nmos` and `pmos` are VERIFIED against
real LTspice schematics: every declared pin lands exactly on a wire endpoint
(see --check), across all four orientations those parts appear in. Other
primitives use the standard LTspice geometry and are checked the same way. Library symbols (`OpAmps\\OP07`) fall back to a triangle or a labeled box,
which is cosmetic only -- wires are always drawn from their own coordinates,
so connectivity is never guessed at.
"""

import argparse
import html
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

GRID = 16

# ---------------------------------------------------------------- orientation

# LTspice orientation -> (matrix applied to local offsets, svg transform)
# Verified against real files: R90 maps (dx,dy) -> (-dy, dx);
# M180 maps (dx,dy) -> (dx, -dy).
ORIENT = {
    "R0":   ((1, 0, 0, 1),   ""),
    "R90":  ((0, -1, 1, 0),  "rotate(90)"),
    "R180": ((-1, 0, 0, -1), "rotate(180)"),
    "R270": ((0, 1, -1, 0),  "rotate(270)"),
    "M0":   ((-1, 0, 0, 1),  "scale(-1,1)"),
    "M90":  ((0, -1, -1, 0), "rotate(90) scale(-1,1)"),
    "M180": ((1, 0, 0, -1),  "scale(1,-1)"),
    "M270": ((0, 1, 1, 0),   "rotate(270) scale(-1,1)"),
}


def xf(rot: str, dx: float, dy: float) -> tuple[float, float]:
    a, b, c, d = ORIENT.get(rot, ORIENT["R0"])[0]
    return a * dx + b * dy, c * dx + d * dy


# ------------------------------------------------------------- symbol library

@dataclass
class Sym:
    """A symbol's local geometry: pins, drawing ops, bounding box."""
    pins: list[tuple[float, float]]
    draw: list[tuple]                 # ("line",x1,y1,x2,y2) ("poly",pts,closed)
                                      # ("circle",cx,cy,r) ("arc",...) ("text",x,y,s,size)
    box: tuple[float, float, float, float]
    verified: bool = False            # pin offsets confirmed against real files


def _zigzag(x, y0, y1, n=6, amp=8):
    """US-style resistor body between (x,y0) and (x,y1)."""
    pts = [(x, y0)]
    step = (y1 - y0) / n
    for i in range(n):
        pts.append((x + (amp if i % 2 == 0 else -amp), y0 + step * (i + 0.5)))
    pts.append((x, y1))
    return pts


def _coils(x, y0, y1, n=4):
    """Inductor body: n half-circle arcs down the lead."""
    ops, step = [], (y1 - y0) / n
    for i in range(n):
        ops.append(("arc", x, y0 + step * i, x, y0 + step * (i + 1), step / 2))
    return ops


def _mos_body():
    """Shared MOSFET drawing: gate plate, broken channel, D/S/bulk leads.

    Local frame is 48 x 96 with D(48,0), G(0,80), S(48,96). The bulk arrow is
    added per-type by the caller (nmos points in, pmos points out).
    """
    return [
        ("line", 0, 80, 16, 80),                   # gate lead
        ("line", 16, 20, 16, 80),                  # gate plate
        ("line", 28, 20, 28, 36),                  # channel (enhancement mode:
        ("line", 28, 42, 28, 58),                  #  three separate segments)
        ("line", 28, 64, 28, 80),
        ("line", 28, 28, 48, 28), ("line", 48, 0, 48, 28),    # drain
        ("line", 28, 72, 48, 72), ("line", 48, 72, 48, 96),   # source
        ("line", 28, 50, 48, 50),                  # bulk, tied to source
        ("line", 48, 50, 48, 72),
    ]


SYMBOLS: dict[str, Sym] = {
    # ---- verified against the user's real LTspice schematics ----
    "res": Sym(
        pins=[(16, 16), (16, 96)],
        draw=[("line", 16, 16, 16, 24), ("poly", _zigzag(16, 24, 88), False),
              ("line", 16, 88, 16, 96)],
        box=(8, 16, 24, 96), verified=True),
    "cap": Sym(
        pins=[(16, 0), (16, 64)],
        draw=[("line", 16, 0, 16, 28), ("line", 0, 28, 32, 28),
              ("line", 0, 36, 32, 36), ("line", 16, 36, 16, 64)],
        box=(0, 0, 32, 64), verified=True),
    "voltage": Sym(
        pins=[(0, 16), (0, 96)],
        draw=[("line", 0, 16, 0, 32), ("circle", 0, 56, 24),
              ("line", 0, 80, 0, 96),
              ("line", -6, 42, 6, 42), ("line", 0, 36, 0, 48),   # +
              ("line", -6, 70, 6, 70)],                          # -
        box=(-24, 16, 24, 96), verified=True),

    # ---- standard LTspice geometry ----
    "ind": Sym(
        pins=[(16, 16), (16, 96)],
        draw=[("line", 16, 16, 16, 24)] + _coils(16, 24, 88) +
             [("line", 16, 88, 16, 96)],
        box=(8, 16, 24, 96)),
    "current": Sym(
        pins=[(0, 0), (0, 80)],
        draw=[("line", 0, 0, 0, 16), ("circle", 0, 40, 24),
              ("line", 0, 56, 0, 80),
              ("line", 0, 28, 0, 52), ("poly", [(-5, 34), (0, 24), (5, 34)], True)],
        box=(-24, 0, 24, 80)),
    "diode": Sym(
        pins=[(16, 0), (16, 64)],
        draw=[("line", 16, 0, 16, 16),
              ("poly", [(4, 16), (28, 16), (16, 48)], True),
              ("line", 4, 48, 28, 48), ("line", 16, 48, 16, 64)],
        box=(4, 0, 28, 64)),
    "zener": Sym(
        pins=[(16, 0), (16, 64)],
        draw=[("line", 16, 0, 16, 16),
              ("poly", [(4, 16), (28, 16), (16, 48)], True),
              ("poly", [(4, 56), (4, 48), (28, 48), (28, 40)], False),
              ("line", 16, 48, 16, 64)],
        box=(4, 0, 28, 64)),
    "schottky": Sym(
        pins=[(16, 0), (16, 64)],
        draw=[("line", 16, 0, 16, 16),
              ("poly", [(4, 16), (28, 16), (16, 48)], True),
              ("poly", [(2, 40), (2, 48), (30, 48), (30, 56)], False),
              ("line", 16, 48, 16, 64)],
        box=(4, 0, 28, 64)),
    # BJT: base lead left, collector up-right, emitter down-right
    "npn": Sym(
        pins=[(16, -16), (-16, 24), (16, 64)],     # C, B, E
        draw=[("line", -16, 24, 0, 24), ("line", 0, 8, 0, 40),
              ("line", 0, 16, 16, -16), ("line", 0, 32, 16, 64),
              ("poly", [(10, 42), (16, 64), (4, 52)], True)],
        box=(-16, -16, 16, 64)),
    "pnp": Sym(
        pins=[(16, -16), (-16, 24), (16, 64)],
        draw=[("line", -16, 24, 0, 24), ("line", 0, 8, 0, 40),
              ("line", 0, 16, 16, -16), ("line", 0, 32, 16, 64),
              ("poly", [(0, 32), (10, 14), (14, 26)], True)],
        box=(-16, -16, 16, 64)),
    # ---- MOSFET geometry verified against a real CMOS schematic ----
    # Drain top-right, source bottom-right, gate at the LOWER left (not
    # centred): confirmed on 8 FETs across R0/M0/R180/M180 placements, every
    # pin landing on a wire endpoint. See --check.
    "nmos": Sym(
        pins=[(48, 0), (0, 80), (48, 96)],         # D, G, S
        draw=_mos_body() + [("poly", [(37, 45), (28, 50), (37, 55)], True)],
        box=(0, 0, 48, 96), verified=True),
    "pmos": Sym(
        pins=[(48, 0), (0, 80), (48, 96)],         # D, G, S
        draw=_mos_body() + [("poly", [(39, 45), (48, 50), (39, 55)], True)],
        box=(0, 0, 48, 96), verified=True),
    "sw": Sym(
        pins=[(16, 0), (16, 96)],
        draw=[("line", 16, 0, 16, 24), ("line", 16, 24, 32, 72),
              ("line", 16, 72, 16, 96), ("circle", 16, 24, 3),
              ("circle", 16, 72, 3)],
        box=(8, 0, 32, 96)),
}

# Op-amp geometry derived from a real OpAmps\OP07 placement: pins sit at
# In-(-32,48) In+(-32,80) Out(32,64) V+(0,32) V-(0,96) in local coordinates.
OPAMP = Sym(
    pins=[(-32, 48), (-32, 80), (32, 64), (0, 32), (0, 96)],
    # The +/- markers are drawn as line glyphs, not text: a symbol placed M180
    # or R90 carries its whole group transform, which would mirror or rotate
    # any glyph text with it.
    draw=[("poly", [(-32, 28), (-32, 100), (32, 64)], True, "bg"),
          ("line", -28, 48, -18, 48),                              # In-
          ("line", -28, 80, -18, 80), ("line", -23, 75, -23, 85)], # In+
    box=(-32, 28, 32, 100))


# Generic op-amps that share the geometry above. Specific part symbols
# (LTC2053, AD8221, ...) have their own pin layouts and are handled by pin
# inference instead of being guessed at from the name.
OPAMP_NAMES = {"opamp", "opamp2", "op07", "op27", "op37", "ua741", "lt1001",
               "lt1006", "lt1012", "universalopamp", "universalopamp2"}

_EMPTY = Sym(pins=[], draw=[], box=(-32, 0, 32, 64))


def symbol_for(name: str) -> tuple[Sym, str]:
    """Resolve an .asc SYMBOL name to geometry. Returns (sym, kind)."""
    leaf = name.replace("\\\\", "\\").split("\\")[-1].lower()
    if leaf in SYMBOLS:
        return SYMBOLS[leaf], "primitive"
    if leaf in OPAMP_NAMES:
        return OPAMP, "opamp"
    return _EMPTY, "unknown"


# -------------------------------------------------------------------- parsing

@dataclass
class Wire:
    x1: int; y1: int; x2: int; y2: int


@dataclass
class Flag:
    x: int; y: int; name: str


@dataclass
class Symbol:
    name: str; x: int; y: int; rot: str
    attrs: dict = field(default_factory=dict)
    windows: dict = field(default_factory=dict)
    inferred: list = field(default_factory=list)   # pins recovered from wiring


@dataclass
class TextItem:
    x: int; y: int; just: str; size: int; text: str; directive: bool


@dataclass
class Schematic:
    path: Path
    wires: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    symbols: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    sheet: tuple = (880, 680)


def parse_asc(path: Path) -> Schematic:
    """Parse an LTspice .asc file. Handles UTF-8, UTF-16 and cp1252 files."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")

    sch = Schematic(path=path)
    cur: Symbol | None = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.split()
        key = parts[0].upper()
        try:
            if key == "SHEET" and len(parts) >= 4:
                sch.sheet = (int(parts[2]), int(parts[3]))
            elif key == "WIRE" and len(parts) >= 5:
                sch.wires.append(Wire(*(int(v) for v in parts[1:5])))
                cur = None
            elif key == "FLAG" and len(parts) >= 4:
                sch.flags.append(Flag(int(parts[1]), int(parts[2]), parts[3]))
                cur = None
            elif key == "SYMBOL" and len(parts) >= 5:
                cur = Symbol(parts[1], int(parts[2]), int(parts[3]), parts[4].upper())
                sch.symbols.append(cur)
            elif key == "SYMATTR" and cur is not None and len(parts) >= 2:
                cur.attrs[parts[1]] = " ".join(parts[2:])
            elif key == "WINDOW" and cur is not None and len(parts) >= 4:
                cur.windows[parts[1]] = (int(parts[2]), int(parts[3]))
            elif key == "TEXT" and len(parts) >= 5:
                body = " ".join(parts[5:])
                directive = body.startswith("!")
                # LTspice stores a multi-line comment on one .asc line, with
                # the breaks escaped as a literal backslash-n.
                body = body.lstrip("!;").strip().replace("\\n", "\n")
                sch.texts.append(TextItem(
                    int(parts[1]), int(parts[2]), parts[3], int(parts[4]),
                    body, directive))
                cur = None
        except ValueError:
            continue  # malformed line: skip rather than fail the whole file
    infer_pins(sch)
    return sch


# ------------------------------------------------------------------- geometry

def sym_pins(sym: Symbol) -> list[tuple[int, int]]:
    geo, kind = symbol_for(sym.name)
    if kind == "unknown":
        return list(sym.inferred)
    out = []
    for dx, dy in geo.pins:
        gx, gy = xf(sym.rot, dx, dy)
        out.append((int(sym.x + gx), int(sym.y + gy)))
    return out


INFER_RADIUS = 200


def infer_pins(sch: Schematic) -> None:
    """Recover pin locations for library symbols we have no geometry for.

    A part like `OpAmps\\LTC2053` has its own pin layout, and guessing it from
    the name produces a wrong drawing. But the schematic already states where
    the pins are: they are the wire endpoints that no known symbol claims and
    that no other symbol is closer to. Nothing about connectivity is invented —
    the wires are drawn from their own coordinates regardless.
    """
    unknown = [s for s in sch.symbols if symbol_for(s.name)[1] == "unknown"]
    if not unknown:
        return

    claimed = set()
    for s in sch.symbols:
        if symbol_for(s.name)[1] != "unknown":
            claimed.update(sym_pins(s))

    ends: dict[tuple[int, int], int] = {}
    for w in sch.wires:
        for p in ((w.x1, w.y1), (w.x2, w.y2)):
            ends[p] = ends.get(p, 0) + 1
    flags = {(f.x, f.y) for f in sch.flags}

    for p, n in ends.items():
        if p in claimed or p in flags or n > 1:
            continue   # already a pin, a labelled net, or an interior corner
        best = min(unknown, key=lambda s: (s.x - p[0]) ** 2 + (s.y - p[1]) ** 2)
        if (best.x - p[0]) ** 2 + (best.y - p[1]) ** 2 <= INFER_RADIUS ** 2:
            best.inferred.append(p)
    for s in unknown:
        s.inferred.sort()


def unknown_box(sym: Symbol) -> tuple[int, int, int, int]:
    """Body rectangle for an inferred-pin symbol, inset to leave pin stubs."""
    if not sym.inferred:
        return sym.x - 32, sym.y, sym.x + 32, sym.y + 64
    xs = [p[0] for p in sym.inferred]
    ys = [p[1] for p in sym.inferred]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    inset_x = 28 if x1 - x0 > 96 else 0
    inset_y = 28 if y1 - y0 > 96 else 0
    x0, x1 = x0 + inset_x, x1 - inset_x
    y0, y1 = y0 + inset_y, y1 - inset_y
    if x1 - x0 < 56:
        cx = (x0 + x1) / 2
        x0, x1 = cx - 28, cx + 28
    if y1 - y0 < 56:
        cy = (y0 + y1) / 2
        y0, y1 = cy - 28, cy + 28
    return int(x0), int(y0), int(x1), int(y1)


def bounds(sch: Schematic) -> tuple[int, int, int, int]:
    xs, ys = [], []
    for w in sch.wires:
        xs += [w.x1, w.x2]; ys += [w.y1, w.y2]
    for f in sch.flags:
        xs.append(f.x); ys.append(f.y)
    for t in sch.texts:
        lines = t.text.splitlines() or [""]
        xs += [t.x, t.x + 8 * max(len(ln) for ln in lines)]
        ys += [t.y, t.y + 20 * (len(lines) - 1)]
    for s in sch.symbols:
        geo, kind = symbol_for(s.name)
        if kind == "unknown":
            bx0, by0, bx1, by1 = unknown_box(s)
            xs += [bx0, bx1]; ys += [by0, by1]
        else:
            x0, y0, x1, y1 = geo.box
            for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                gx, gy = xf(s.rot, cx, cy)
                xs.append(s.x + gx); ys.append(s.y + gy)
        xs.append(s.x + 90)   # room for InstName / Value labels
    if not xs:
        xs, ys = [0, sch.sheet[0]], [0, sch.sheet[1]]
    return min(xs), min(ys), max(xs), max(ys)


def junctions(sch: Schematic) -> set[tuple[int, int]]:
    """Points where three or more conductors meet."""
    count: dict[tuple[int, int], int] = {}
    for w in sch.wires:
        for p in ((w.x1, w.y1), (w.x2, w.y2)):
            count[p] = count.get(p, 0) + 1
    for s in sch.symbols:
        for p in sym_pins(s):
            count[p] = count.get(p, 0) + 1
    return {p for p, c in count.items() if c >= 3}


# ------------------------------------------------------------------ rendering

THEMES = {
    "light": {"bg": "#ffffff", "wire": "#1b3a6b", "sym": "#14181f",
              "label": "#14181f", "net": "#0b7a5a", "dir": "#7a3fb8",
              "gnd": "#1b3a6b", "grid": "#eef1f5"},
    "dark": {"bg": "#12151b", "wire": "#6fa8ff", "sym": "#e8ecf2",
             "label": "#e8ecf2", "net": "#4fd1a5", "dir": "#c79bf0",
             "gnd": "#6fa8ff", "grid": "#1b202a"},
}


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def draw_ops(ops, color: str, width: float = 2.0, bg: str = "#ffffff") -> list[str]:
    out = []
    for op in ops:
        kind = op[0]
        if kind == "line":
            _, x1, y1, x2, y2 = op
            out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                       f'stroke="{color}" stroke-width="{width}" stroke-linecap="round"/>')
        elif kind == "poly":
            pts, closed = op[1], op[2]
            mode = op[3] if len(op) > 3 else ("solid" if closed else "none")
            d = " ".join(f"{x},{y}" for x, y in pts)
            tag = "polygon" if closed else "polyline"
            fill = {"solid": color, "none": "none", "bg": bg}.get(mode, mode)
            out.append(f'<{tag} points="{d}" fill="{fill}" stroke="{color}" '
                       f'stroke-width="{width}" stroke-linejoin="round"/>')
        elif kind == "circle":
            _, cx, cy, r = op
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
                       f'stroke="{color}" stroke-width="{width}"/>')
        elif kind == "arc":
            _, x1, y1, x2, y2, r = op
            out.append(f'<path d="M{x1} {y1} A{r} {r} 0 0 1 {x2} {y2}" fill="none" '
                       f'stroke="{color}" stroke-width="{width}"/>')
        elif kind == "text":
            _, x, y, s, size = op
            out.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" '
                       f'text-anchor="middle">{_esc(s)}</text>')
    return out


def ground_glyph(x: int, y: int, color: str) -> str:
    return (f'<g stroke="{color}" stroke-width="2" stroke-linecap="round">'
            f'<line x1="{x}" y1="{y}" x2="{x}" y2="{y+6}"/>'
            f'<line x1="{x-11}" y1="{y+6}" x2="{x+11}" y2="{y+6}"/>'
            f'<line x1="{x-7}" y1="{y+11}" x2="{x+7}" y2="{y+11}"/>'
            f'<line x1="{x-3}" y1="{y+16}" x2="{x+3}" y2="{y+16}"/></g>')


MARGIN = 48


def svg_size(sch: Schematic, scale: float = 1.0) -> tuple[int, int]:
    x0, y0, x1, y1 = bounds(sch)
    return (round((x1 - x0 + 2 * MARGIN) * scale),
            round((y1 - y0 + 2 * MARGIN) * scale))


def render_svg(sch: Schematic, theme: str = "light", scale: float = 1.0,
               show_grid: bool = False) -> str:
    C = THEMES[theme]
    x0, y0, x1, y1 = bounds(sch)
    m = MARGIN
    x0, y0, x1, y1 = x0 - m, y0 - m, x1 + m, y1 + m
    w, h = x1 - x0, y1 - y0

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0} {y0} {w} {h}" '
        f'width="{w*scale:.0f}" height="{h*scale:.0f}" '
        f'font-family="ui-sans-serif, Segoe UI, Helvetica, Arial, sans-serif">',
        f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" fill="{C["bg"]}"/>',
    ]

    if show_grid:
        out.append(f'<g stroke="{C["grid"]}" stroke-width="1">')
        gx = x0 - (x0 % GRID)
        while gx < x1:
            out.append(f'<line x1="{gx}" y1="{y0}" x2="{gx}" y2="{y1}"/>')
            gx += GRID
        gy = y0 - (y0 % GRID)
        while gy < y1:
            out.append(f'<line x1="{x0}" y1="{gy}" x2="{x1}" y2="{gy}"/>')
            gy += GRID
        out.append("</g>")

    # wires
    out.append(f'<g stroke="{C["wire"]}" stroke-width="2.5" stroke-linecap="round">')
    for wr in sch.wires:
        out.append(f'<line x1="{wr.x1}" y1="{wr.y1}" x2="{wr.x2}" y2="{wr.y2}"/>')
    out.append("</g>")

    # junction dots
    for jx, jy in sorted(junctions(sch)):
        out.append(f'<circle cx="{jx}" cy="{jy}" r="4" fill="{C["wire"]}"/>')

    # symbols
    for s in sch.symbols:
        geo, kind = symbol_for(s.name)
        inst = s.attrs.get("InstName", "")
        val = s.attrs.get("Value", "")

        if kind == "unknown":
            # A library part: draw the body inferred from its own wiring, with
            # a stub out to each pin, and name the part inside the block.
            bx0, by0, bx1, by1 = unknown_box(s)
            for px, py in s.inferred:
                ex = min(max(px, bx0), bx1)
                ey = min(max(py, by0), by1)
                out.append(f'<line x1="{px}" y1="{py}" x2="{ex}" y2="{ey}" '
                           f'stroke="{C["sym"]}" stroke-width="2" '
                           f'stroke-linecap="round"/>')
            out.append(f'<rect x="{bx0}" y="{by0}" width="{bx1-bx0}" '
                       f'height="{by1-by0}" rx="7" fill="{C["bg"]}" '
                       f'stroke="{C["sym"]}" stroke-width="2.5"/>')
            leaf = s.name.replace("\\\\", "\\").split("\\")[-1]
            mx, my = (bx0 + bx1) / 2, (by0 + by1) / 2
            out.append(f'<text x="{mx}" y="{my + 1}" font-size="15" '
                       f'font-weight="600" text-anchor="middle" '
                       f'fill="{C["label"]}">{_esc(inst or leaf)}</text>')
            if inst:
                out.append(f'<text x="{mx}" y="{my + 18}" font-size="13" '
                           f'text-anchor="middle" fill="{C["label"]}" '
                           f'opacity="0.75">{_esc(leaf)}</text>')
            if val:
                out.append(f'<text x="{bx1 + 10}" y="{by0 + 16}" font-size="14" '
                           f'fill="{C["label"]}" opacity="0.85">{_esc(val)}</text>')
            continue

        tr = ORIENT.get(s.rot, ORIENT["R0"])[1]
        out.append(f'<g transform="translate({s.x},{s.y}) {tr}">')
        out += draw_ops(geo.draw, C["sym"], bg=C["bg"])
        out.append("</g>")

        # labels drawn upright in root coordinates, just right of the body
        bx0, by0, bx1, by1 = geo.box
        cx = cy = None
        for px, py in ((bx1, by0), (bx1, by1), (bx0, by0), (bx0, by1)):
            gx, gy = xf(s.rot, px, py)
            cx = s.x + gx if cx is None else max(cx, s.x + gx)
            cy = s.y + gy if cy is None else min(cy, s.y + gy)
        lx, ly = cx + 10, cy + 16
        if inst:
            out.append(f'<text x="{lx}" y="{ly}" font-size="15" font-weight="600" '
                       f'fill="{C["label"]}">{_esc(inst)}</text>')
        if val:
            out.append(f'<text x="{lx}" y="{ly + 17}" font-size="14" '
                       f'fill="{C["label"]}" opacity="0.85">{_esc(val)}</text>')

    # flags: ground glyph for "0", net label otherwise
    for f in sch.flags:
        if f.name == "0":
            out.append(ground_glyph(f.x, f.y, C["gnd"]))
        else:
            out.append(f'<circle cx="{f.x}" cy="{f.y}" r="3" fill="{C["net"]}"/>')
            out.append(f'<text x="{f.x + 7}" y="{f.y - 6}" font-size="14" '
                       f'font-weight="600" fill="{C["net"]}">{_esc(f.name)}</text>')

    # text: SPICE directives and comments
    for t in sch.texts:
        anchor = {"Left": "start", "Center": "middle", "Right": "end"}.get(t.just, "start")
        color = C["dir"] if t.directive else C["label"]
        prefix = "" if t.directive else ""
        for i, ln in enumerate(t.text.splitlines() or [""]):
            out.append(f'<text x="{t.x}" y="{t.y + i*20}" font-size="15" '
                       f'text-anchor="{anchor}" fill="{color}" '
                       f'font-family="ui-monospace, Consolas, monospace">'
                       f'{_esc(prefix + ln)}</text>')

    out.append("</svg>")
    return "\n".join(out)


HTML_TMPL = """<!doctype html>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; background:{page}; color:{fg};
         font-family: ui-sans-serif, "Segoe UI", Helvetica, Arial, sans-serif; }}
  header {{ padding:18px 24px 10px; }}
  h1 {{ font-size:17px; margin:0 0 3px; font-weight:650; letter-spacing:-0.01em; }}
  .sub {{ font-size:13px; opacity:.62; }}
  .sheet {{ margin:8px 24px 28px; background:{bg}; border-radius:10px;
            box-shadow:0 1px 3px rgba(0,0,0,.12), 0 8px 26px rgba(0,0,0,.07);
            overflow:auto; }}
  .sheet svg {{ display:block; max-width:100%; height:auto; }}
  footer {{ padding:0 24px 26px; font-size:12px; opacity:.5; }}
</style>
<header>
  <h1>{title}</h1>
  <div class="sub">{sub}</div>
</header>
<div class="sheet">{svg}</div>
<footer>Rendered from {path} — ascview</footer>
"""


def render_html(sch: Schematic, theme: str = "light", scale: float = 1.0) -> str:
    C = THEMES[theme]
    ndir = sum(1 for t in sch.texts if t.directive)
    sub = (f"{len(sch.symbols)} symbols · {len(sch.wires)} wires · "
           f"{len({f.name for f in sch.flags})} nets · {ndir} SPICE directive"
           f"{'s' if ndir != 1 else ''}")
    return HTML_TMPL.format(
        title=_esc(sch.path.stem), sub=_esc(sub), path=_esc(sch.path.name),
        svg=render_svg(sch, theme, scale), bg=C["bg"], fg=C["sym"],
        page="#f4f6f9" if theme == "light" else "#0c0e13")


# --------------------------------------------------------------- pin checking

def check(sch: Schematic) -> list[str]:
    """Validate symbol pin offsets: every pin should land on a wire endpoint.

    This is how the symbol library is verified against real schematics instead
    of trusted from memory.
    """
    ends = set()
    for w in sch.wires:
        ends.add((w.x1, w.y1)); ends.add((w.x2, w.y2))
    for f in sch.flags:
        ends.add((f.x, f.y))

    msgs = []
    for s in sch.symbols:
        geo, kind = symbol_for(s.name)
        if kind == "unknown":
            msgs.append(f"  ~  {s.name} {s.attrs.get('InstName','')} @"
                        f"({s.x},{s.y}) {s.rot}: no built-in geometry — "
                        f"{len(s.inferred)} pins inferred from wiring")
            continue
        if not geo.pins:
            msgs.append(f"  ?  {s.name} {s.attrs.get('InstName','')} — no pins")
            continue
        pins = sym_pins(s)
        hit = [p for p in pins if p in ends]
        tag = "OK " if len(hit) == len(pins) else ("~  " if hit else "!! ")
        if len(hit) != len(pins):
            miss = [p for p in pins if p not in ends]
            msgs.append(f"  {tag}{s.name} {s.attrs.get('InstName','')} @"
                        f"({s.x},{s.y}) {s.rot}: {len(hit)}/{len(pins)} pins on a "
                        f"wire; unconnected {miss}")
        else:
            msgs.append(f"  {tag}{s.name} {s.attrs.get('InstName','')} @"
                        f"({s.x},{s.y}) {s.rot}: {len(hit)}/{len(pins)} pins")
    return msgs


# ------------------------------------------------------------------- open/CLI

CHROMIUM = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/chromium", "/usr/bin/google-chrome",
]


def find_chromium() -> str | None:
    """Any Chromium build can rasterize headlessly; Edge ships with Windows."""
    for c in CHROMIUM:
        if Path(c).exists():
            return c
    return None


def to_png(svg_path: Path, png_path: Path, w: int, h: int) -> bool:
    """Rasterize via headless Chromium, sized to the drawing (no clipping)."""
    exe = find_chromium()
    if not exe:
        print("  no Chrome/Edge found — skipping PNG", file=sys.stderr)
        return False
    uri = svg_path.resolve().as_uri()
    proc = subprocess.run(
        [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--screenshot={png_path.resolve()}",
         f"--window-size={w},{h}", uri],
        capture_output=True, text=True, timeout=90,
    )
    if not png_path.exists():
        print(f"  PNG failed: {proc.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return True


def open_file(path: Path) -> None:
    """Open with the OS default handler (browser for .html/.svg)."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as exc:
        print(f"could not open {path}: {exc}", file=sys.stderr)


def expand(pat: str) -> list[Path]:
    """Resolve one CLI path argument to concrete paths.

    POSIX shells expand globs themselves; PowerShell and cmd do not, so a
    literal `*.asc` has to be expanded here. `Path().glob()` rejects absolute
    patterns outright, so those are globbed relative to their own anchor.
    A pattern with no magic characters is passed through untouched, which
    keeps a plain absolute filename working.
    """
    path = Path(pat)
    if not any(ch in pat for ch in "*?["):
        return [path]
    if path.is_absolute():
        anchor = Path(path.anchor)
        hits = sorted(anchor.glob(str(path.relative_to(anchor))))
    else:
        hits = sorted(Path().glob(pat))
    return hits or [path]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", help=".asc schematic file(s)")
    ap.add_argument("-o", "--out", help="output path (default: alongside the .asc)")
    ap.add_argument("--format", choices=("html", "svg"), default="html")
    ap.add_argument("--theme", choices=("light", "dark"), default="light")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--grid", action="store_true", help="draw the schematic grid")
    ap.add_argument("--open", action="store_true", help="open in the default viewer")
    ap.add_argument("--png", action="store_true",
                    help="also write a PNG (headless Chrome/Edge), sized to the drawing")
    ap.add_argument("--check", action="store_true",
                    help="report symbol pin geometry against wire endpoints")
    args = ap.parse_args()

    rc = 0
    for pat in args.files:
        for path in expand(pat):
            if not path.exists():
                print(f"not found: {path}", file=sys.stderr)
                rc = 1
                continue
            sch = parse_asc(path)
            print(f"{path.name}: {len(sch.symbols)} symbols, {len(sch.wires)} wires, "
                  f"{len(sch.flags)} flags, {len(sch.texts)} text")
            if args.check:
                for m in check(sch):
                    print(m)
                continue
            body = (render_html(sch, args.theme, args.scale) if args.format == "html"
                    else render_svg(sch, args.theme, args.scale, args.grid))
            out = Path(args.out) if args.out else path.with_suffix("." + args.format)
            out.write_text(body, encoding="utf-8")
            print(f"  wrote {out}  ({out.stat().st_size/1024:.1f} KB)")

            if args.png:
                svg_path = out
                if args.format != "svg":   # rasterize the bare drawing, not the page
                    svg_path = out.with_suffix(".svg")
                    svg_path.write_text(
                        render_svg(sch, args.theme, args.scale, args.grid),
                        encoding="utf-8")
                w, h = svg_size(sch, args.scale)
                png = out.with_suffix(".png")
                if to_png(svg_path, png, w, h):
                    print(f"  wrote {png}  ({png.stat().st_size/1024:.1f} KB, "
                          f"{w}x{h})")
            if args.open:
                open_file(out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
