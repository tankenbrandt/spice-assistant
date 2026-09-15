#!/usr/bin/env python3
"""LTspice .asc -> ngspice netlist, closing the loop without LTspice.

`ascview` draws a schematic and `speccheck` verifies a netlist; nothing joined
them, so a schematic could be looked at but not simulated. This extracts the
connectivity and emits a deck:

    python netlist.py schematic.asc                 # print the deck
    python netlist.py schematic.asc -o deck.cir
    python netlist.py schematic.asc --strict        # fail on anything unresolved

Nets come from the drawing, not from guesswork. Wire endpoints and symbol pins
are merged with a union-find, including T-junctions where one wire lands on
another's span, and a net takes its name from any FLAG on it -- `0` for ground.
Anything left unnamed becomes N001, N002, ...

What it will not do is invent a pin order it does not know. A primitive has a
fixed, verified pin order; a library part (`OpAmps\\LTC2053`) does not, so it is
emitted as a commented X line listing the nets its pins landed on, for a human
to order. `--strict` turns that into an error instead of a comment, which is
what you want in a script.
"""

import argparse
import re
import sys
from pathlib import Path

import ascview

# SPICE prefix and pin order for each primitive. The pin order is the order in
# ascview.SYMBOLS, which is verified against real schematics by `--check`.
DEVICES = {
    "res":     ("R", ["n1", "n2"]),
    "cap":     ("C", ["n1", "n2"]),
    "ind":     ("L", ["n1", "n2"]),
    "voltage": ("V", ["plus", "minus"]),
    "current": ("I", ["plus", "minus"]),
    "diode":   ("D", ["anode", "cathode"]),
    "zener":   ("D", ["anode", "cathode"]),
    "schottky": ("D", ["anode", "cathode"]),
    "npn":     ("Q", ["C", "B", "E"]),
    "pnp":     ("Q", ["C", "B", "E"]),
    "nmos":    ("M", ["D", "G", "S"]),
    "pmos":    ("M", ["D", "G", "S"]),
    "sw":      ("S", ["n1", "n2"]),
}

# Devices that need a model name, and a default if the schematic gives none.
NEEDS_MODEL = {"diode": "D", "zener": "D", "schottky": "D",
               "npn": "NPN", "pnp": "PNP", "nmos": "NMOS", "pmos": "PMOS",
               "sw": "SW"}

# A MOSFET line is D G S B; LTspice's 3-terminal symbol ties bulk to source.
BULK_TO_SOURCE = {"nmos", "pmos"}

# Role names for the op-amp geometry, in ascview's pin order. These are what a
# --pinorder mapping refers to, so the order can be stated in the reader's
# terms rather than as pin indices.
OPAMP_ROLES = ["in-", "in+", "out", "v+", "v-"]


def roles_for(kind: str, npins: int) -> list:
    """Role name per pin. Unknown symbols fall back to 1-based indices."""
    if kind == "opamp" and npins == len(OPAMP_ROLES):
        return list(OPAMP_ROLES)
    return [str(i + 1) for i in range(npins)]


def parse_pinorder(specs: list) -> dict:
    """Parse `--pinorder OP07=in+,in-,v+,v-,out` into {symbol: [roles]}.

    A subcircuit's pin order is a property of the library, not the drawing,
    so it has to come from outside the .asc. Stating it explicitly is the
    only honest alternative to guessing it.
    """
    out = {}
    for spec in specs or []:
        if "=" not in spec:
            raise NetlistError(
                f"--pinorder wants SYMBOL=role,role,...; got {spec!r}")
        name, _, order = spec.partition("=")
        roles = [r.strip().lower() for r in order.split(",") if r.strip()]
        if not roles:
            raise NetlistError(f"--pinorder {name!r} lists no pins")
        out[name.strip().lower().split(chr(92))[-1]] = roles
    return out


class NetlistError(ValueError):
    """The schematic cannot be turned into a deck without guessing."""


# ------------------------------------------------------------------ topology

class Union:
    """Union-find over drawing coordinates."""

    def __init__(self):
        self.parent = {}

    def find(self, p):
        self.parent.setdefault(p, p)
        root = p
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[p] != root:            # path compression
            self.parent[p], p = root, self.parent[p]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _on_segment(p, w) -> bool:
    """Is point p strictly inside wire w's span? (LTspice wires are orthogonal.)"""
    px, py = p
    if w.x1 == w.x2 == px:
        lo, hi = sorted((w.y1, w.y2))
        return lo < py < hi
    if w.y1 == w.y2 == py:
        lo, hi = sorted((w.x1, w.x2))
        return lo < px < hi
    return False


def build_nets(sch) -> dict:
    """Map every connection point to a net name.

    Merges wire endpoints with each other and with symbol pins, then folds in
    T-junctions, where a wire endpoint or pin lands part-way along another
    wire rather than at its end.
    """
    u = Union()
    points = set()

    for w in sch.wires:
        a, b = (w.x1, w.y1), (w.x2, w.y2)
        points.update((a, b))
        u.union(a, b)

    for s in sch.symbols:
        for p in ascview.sym_pins(s):
            points.add(p)
            u.find(p)

    for f in sch.flags:
        points.add((f.x, f.y))
        u.find((f.x, f.y))

    # T-junctions: a point touching the middle of a wire joins that wire's net.
    for p in list(points):
        for w in sch.wires:
            if _on_segment(p, w):
                u.union(p, (w.x1, w.y1))

    # Named nets win; ground is always 0.
    names = {}
    for f in sch.flags:
        root = u.find((f.x, f.y))
        name = "0" if f.name.strip() in ("0", "GND", "gnd") else f.name.strip()
        if root in names and names[root] != name:
            # Two different labels on one net: keep ground, else keep the first.
            if "0" in (names[root], name):
                names[root] = "0"
            continue
        names[root] = name

    auto = 0
    net_of = {}
    for p in sorted(points):
        root = u.find(p)
        if root not in names:
            auto += 1
            names[root] = f"N{auto:03d}"
        net_of[p] = names[root]
    return net_of


# ------------------------------------------------------------------ emission

def _refdes(prefix: str, sym, index: int) -> str:
    """Use the schematic's InstName when it already fits SPICE's convention."""
    inst = (sym.attrs.get("InstName") or "").strip()
    if inst and inst[0].upper() == prefix.upper():
        return inst
    if inst:
        return prefix + inst
    return f"{prefix}{index}"


def _value(sym, leaf: str) -> str:
    value = (sym.attrs.get("Value") or "").strip()
    if value:
        return value
    if leaf in NEEDS_MODEL:
        return NEEDS_MODEL[leaf]
    raise NetlistError(
        f"{sym.attrs.get('InstName') or sym.name} has no Value attribute")


def emit(sch, strict: bool = False, pinorder: dict | None = None) -> tuple[list, list]:
    """Build the deck. Returns (lines, warnings)."""
    pinorder = pinorder or {}
    net_of = build_nets(sch)
    lines, warnings = [], []
    counter = 0

    for sym in sch.symbols:
        counter += 1
        leaf = sym.name.replace("\\\\", "\\").split("\\")[-1].lower()
        _geo, kind = ascview.symbol_for(sym.name)
        pins = ascview.sym_pins(sym)
        nets = [net_of.get(p, "?") for p in pins]
        inst = (sym.attrs.get("InstName") or f"U{counter}").strip()

        if leaf in DEVICES:
            prefix, order = DEVICES[leaf]
            if len(nets) != len(order):
                raise NetlistError(
                    f"{inst}: symbol {leaf} has {len(nets)} pins, expected "
                    f"{len(order)}")
            node_list = list(nets)
            if leaf in BULK_TO_SOURCE:
                node_list.append(nets[2])        # bulk tied to source
            lines.append(f"{_refdes(prefix, sym, counter)} "
                         f"{' '.join(node_list)} {_value(sym, leaf)}")
            continue

        # A declared pin order makes a subcircuit emittable.
        roles = roles_for(kind, len(pins))
        wanted = pinorder.get(leaf)
        if wanted:
            index = {r: i for i, r in enumerate(roles)}
            missing = [r for r in wanted if r not in index]
            if missing:
                raise NetlistError(
                    f"{inst} ({sym.name}): --pinorder names {missing}, but "
                    f"this symbol's pins are {roles}")
            if len(wanted) != len(pins):
                raise NetlistError(
                    f"{inst} ({sym.name}): --pinorder lists {len(wanted)} pins, "
                    f"the symbol has {len(pins)}")
            ordered = [nets[index[r]] for r in wanted]
            lines.append(f"X{inst} {' '.join(ordered)} {leaf.upper()}")
            continue

        # Otherwise: the pins are known, the subcircuit's pin ORDER is not, so
        # the connectivity is reported rather than asserted.
        detail = ", ".join(f"({x},{y})->{n}" for (x, y), n
                           in zip(pins, nets, strict=True)) or "no pins found"
        msg = (f"{inst} ({sym.name}): no known SPICE pin order for this "
               f"symbol; pins land on {detail}. Declare it with "
               f"--pinorder {leaf}={','.join(roles)}")
        if strict:
            raise NetlistError(msg)
        warnings.append(msg)
        hint = " ".join(nets) if nets else "..."
        lines.append("* TODO order these nets for the subcircuit's pin list:")
        lines.append(f"* X{inst} {hint} {leaf.upper()}")

    return lines, warnings


_ANALYSIS_RE = re.compile(r"(?i)^\s*\.(op|ac|tran|dc|noise|tf|four|pz|disto|sens)\b")


def _is_analysis(line: str) -> bool:
    return bool(_ANALYSIS_RE.match(line))


def directives(sch) -> tuple[list, list]:
    """Split the schematic's SPICE directives into card lines and analyses.

    Only an analysis dot-line is an analysis; everything else is a card. That
    way a device line inside a `.subckt` block -- which starts with `R`, not a
    dot -- stays with the cards where it belongs, instead of being swept into
    the control block. Lines between `.subckt` and `.ends` are never treated
    as analyses even if one looks like one.
    """
    cards, analyses = [], []
    depth = 0
    for t in sch.texts:
        if not t.directive:
            continue
        for line in t.text.splitlines():
            line = line.strip()
            if not line:
                continue
            lowered = line.lower()
            if lowered.startswith(".subckt"):
                depth += 1
            if depth or not _is_analysis(line):
                cards.append(line)
            else:
                analyses.append(line)
            if lowered.startswith(".ends"):
                depth = max(0, depth - 1)
    return cards, analyses


def to_netlist(sch, strict: bool = False, control: str = "",
               pinorder: dict | None = None) -> tuple[str, list]:
    """Render a whole deck. Returns (text, warnings)."""
    devices, warnings = emit(sch, strict, pinorder)
    cards, analyses = directives(sch)

    out = [f"* {sch.path.stem} -- generated from {sch.path.name} by netlist.py"]
    out.append("")
    out.extend(devices)
    if cards:
        out.append("")
        out.extend(cards)

    body = control.strip() or "\n".join(analyses).strip()
    if body:
        out.append("")
        out.append(".control")
        out.extend(ln.lstrip(".") if _is_analysis(ln) else ln
                   for ln in body.splitlines())
        out.append(".endc")
    out.append("")
    out.append(".end")
    return "\n".join(out) + "\n", warnings


# --------------------------------------------------------------------- CLI

def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="netlist",
        description="Turn an LTspice .asc schematic into an ngspice netlist.")
    ap.add_argument("files", nargs="+", help=".asc schematic file(s)")
    ap.add_argument("-o", "--out", help="write here (default: stdout)")
    ap.add_argument("--strict", action="store_true",
                    help="fail instead of emitting a TODO for unknown symbols")
    ap.add_argument("--control", default="",
                    help="control-block body to append, e.g. 'op'")
    ap.add_argument("--pinorder", action="append", metavar="SYM=roles",
                    help="subcircuit pin order, e.g. OP07=in+,in-,v+,v-,out")
    args = ap.parse_args(argv)

    try:
        pinorder = parse_pinorder(args.pinorder)
    except NetlistError as exc:
        print(exc, file=sys.stderr)
        return 2

    rc = 0
    for pattern in args.files:
        for path in ascview.expand(pattern):
            if not path.exists():
                print(f"not found: {path}", file=sys.stderr)
                rc = 1
                continue
            sch = ascview.parse_asc(path)
            try:
                text, warnings = to_netlist(sch, args.strict, args.control,
                                            pinorder)
            except NetlistError as exc:
                print(f"{path}: {exc}", file=sys.stderr)
                rc = 1
                continue
            for w in warnings:
                print(f"{path.name}: warning: {w}", file=sys.stderr)
            if args.out:
                Path(args.out).write_text(text, encoding="utf-8")
                print(f"wrote {args.out}", file=sys.stderr)
            else:
                print(text, end="")
    return rc


if __name__ == "__main__":
    sys.exit(_cli())
