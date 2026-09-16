#!/usr/bin/env python3
"""Write an ngspice deck back out as an LTspice `.asc` schematic.

`netlist.py` goes schematic -> deck. This goes the other way, so a deck the
model generated can be opened, probed and edited in the real LTspice GUI
instead of only read as text.

    python ascgen.py repaired/ce_bjt_amp.cir -o ce_bjt_amp.asc
    python ascgen.py repaired/ce_bjt_amp.cir --check

The layout is deliberately mechanical -- one column per net, one row per
device -- rather than an attempt at draughtsmanship. It will not arrange a
differential pair the way a person would. What it guarantees is that the
drawing's *connectivity* is the deck's, which is the part that has to be
right: `--check` re-extracts the generated file with `netlist.py` and compares
device by device, and the test suite additionally round-trips it through
LTspice's own netlister.

Crossing wires are safe. LTspice joins wires that share an endpoint or form a
T-junction; two that merely cross are not connected, so a busy layout is ugly
without being wrong.
"""

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import ascview
import ltspice
import netlist as netlist_mod

# --------------------------------------------------------------- geometry

COL_W = 192       # x between adjacent net buses
ROW_H = 160       # y between adjacent device rows
X0 = 224          # x of the first bus
Y0 = 112          # y of the first device row
SHUNT_DX = 64     # how far an upright device sits from its net's bus

GROUND = {"0", "gnd", "gnd!", "vss!"}

PREFIX_SYMBOL = {"R": "res", "C": "cap", "L": "ind",
                 "V": "voltage", "I": "current", "D": "diode", "S": "sw"}

# Node count per prefix; everything after them is the value or model name.
PREFIX_NODES = {"R": 2, "C": 2, "L": 2, "V": 2, "I": 2, "D": 2,
                "Q": 3, "M": 4, "S": 4}


class UnsupportedDeck(Exception):
    """The deck holds something this writer will not guess at."""


@dataclass
class Device:
    refdes: str
    symbol: str
    nodes: list
    value: str = ""


@dataclass
class Deck:
    title: str = ""
    devices: list = field(default_factory=list)
    directives: list = field(default_factory=list)
    models: dict = field(default_factory=dict)      # name -> type, lowercased


# ------------------------------------------------------------------ parsing

_MODEL_RE = re.compile(r"(?i)^\s*\.model\s+(\S+)\s+([A-Za-z]+)")


# The control-block commands that are analyses, and so become directives on
# the sheet. Everything else in a control block (print, plot, wrdata, set,
# alter) is ngspice's interactive language and has no place on a schematic.
_ANALYSES = ("ac", "tran", "op", "dc", "noise", "tf", "disto", "pz", "sens")


def analyses_in_control(text: str) -> list:
    """Pull the analysis commands out of a deck's `.control` block.

    Without this the generated schematic opens correctly and has nothing to
    run: the deck keeps its analysis inside a control block, which is exactly
    the part LTspice cannot read and `strip_control` throws away.
    """
    out = []
    inside = False
    for raw in text.splitlines():
        low = raw.strip().lower()
        if low.startswith(".control"):
            inside = True
            continue
        if low.startswith(".endc"):
            inside = False
            continue
        if inside and low.split() and low.split()[0] in _ANALYSES:
            out.append(raw.strip())
    return out


def parse_deck(text: str) -> Deck:
    """Read a deck into devices plus the directives worth carrying over."""
    lines = ltspice.strip_control(text).splitlines()
    deck = Deck()
    carried = [ltspice.directive(c) for c in analyses_in_control(text)]

    # Model cards first: a Q or M cannot pick its symbol without them, and a
    # deck may declare them after the devices that use them.
    for line in lines:
        m = _MODEL_RE.match(line)
        if m:
            deck.models[m.group(1).lower()] = m.group(2).lower()

    deck.title, body = _strip_title(lines)
    for raw in body:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("*"):
            continue
        if line.startswith("+"):
            raise UnsupportedDeck(
                f"continuation lines are not supported yet: {line[:50]!r}")
        if line.startswith("."):
            low = line.lower()
            if low.startswith(".end") and not low.startswith(".ends"):
                continue
            deck.directives.append(line)
            continue
        deck.devices.append(_parse_device(line, deck.models))
    deck.directives.extend(carried)
    return deck


def _strip_title(lines: list) -> tuple:
    """Split a deck's title line off the front.

    SPICE has no ambiguity here: the first line of a deck is the title, full
    stop, whatever it happens to contain. This originally guessed -- treating
    line one as a component when it had a device prefix and enough tokens --
    and `Simple RC Circuit Test` duly parsed as a four-terminal switch named
    `Simple`. `ltspice.py` already stated the real rule; this now matches it.
    """
    for i, raw in enumerate(lines):
        if raw.strip():
            return lines[i].strip().lstrip("*").strip(), lines[i + 1:]
    return "", []


def _parse_device(line: str, models: dict) -> Device:
    parts = line.split()
    refdes = parts[0]
    prefix = refdes[0].upper()
    if prefix not in PREFIX_NODES:
        raise UnsupportedDeck(f"{refdes}: {_why_unsupported(prefix)}")
    n = PREFIX_NODES[prefix]
    if len(parts) < 1 + n:
        raise UnsupportedDeck(f"{refdes}: expected {n} nodes in {line[:60]!r}")
    nodes, rest = parts[1:1 + n], parts[1 + n:]

    if prefix == "Q":
        # An optional substrate node sits between the emitter and the model
        # name; it is there only when the next token is not itself a model.
        if len(rest) >= 2 and rest[0].lower() not in models and rest[1].lower() in models:
            rest = rest[1:]
        symbol = _device_symbol(rest, models, refdes, ("npn", "pnp"), "NPN from PNP")
    elif prefix == "M":
        symbol = _device_symbol(rest, models, refdes, ("nmos", "pmos"),
                                "NMOS from PMOS")
        nodes = nodes[:3]        # the 3-terminal symbol ties bulk to source
    else:
        symbol = PREFIX_SYMBOL[prefix]
    return Device(refdes=refdes, symbol=symbol, nodes=nodes, value=" ".join(rest))


def _why_unsupported(prefix: str) -> str:
    """Say what the deck asked for and why it cannot be drawn.

    These are the two that turn up in this project's own decks -- the op-amp
    circuits use a VCVS, and the worked example calls a subcircuit -- so the
    message is worth more than "unknown device prefix".
    """
    if prefix in "EGFH":
        return ("a controlled source (E/G/F/H) has no symbol in the library, "
                "so there is nothing to draw. Model the op-amp with a "
                "subcircuit, or extend ascview.SYMBOLS and netlist.DEVICES "
                "together -- both halves have to agree on pin order.")
    if prefix == "X":
        return ("a subcircuit call needs the symbol its pins came from. "
                "symlib can read the .asy and its SpiceOrder, but choosing "
                "which symbol a bare .subckt name refers to is a guess this "
                "writer will not make.")
    return f"device prefix {prefix!r} is not supported"


def _device_symbol(rest, models, refdes, allowed, what) -> str:
    name = rest[0].lower() if rest else ""
    kind = models.get(name)
    if kind in allowed:
        return kind
    raise UnsupportedDeck(
        f"{refdes}: cannot tell {what} -- no .model card for {name or '(none)'}. "
        "The pin geometry depends on it, so guessing would draw a schematic "
        "that netlists back to a different circuit.")


# ------------------------------------------------------------------- layout

def _is_ground(node: str) -> bool:
    return node.lower() in GROUND


def _orientation(dev: Device, columns: dict):
    """Pick a rotation, and say whether the part ends up lying down.

    A two-terminal part spanning two nets is drawn horizontally, turned so
    its first pin faces its first net -- otherwise both leads double back
    across the body. A part with a leg to ground is drawn upright with that
    leg at the bottom, which is how anyone would draw it.
    """
    if len(dev.nodes) != 2:
        return "R0", False
    a, b = dev.nodes
    if _is_ground(a) or _is_ground(b):
        # R0 puts pin 0 on top, R180 puts pin 1 on top.
        return ("R180" if _is_ground(a) else "R0"), False
    # R90 puts pin 0 right of pin 1; R270 puts it left.
    return ("R90" if columns[a] > columns[b] else "R270"), True


def build_schematic(deck: Deck) -> ascview.Schematic:
    """Place every device and wire each pin to a bus for its net."""
    if not deck.devices:
        raise UnsupportedDeck("the deck has no devices to draw")

    order = []
    for dev in deck.devices:
        for nd in dev.nodes:
            if not _is_ground(nd) and nd not in order:
                order.append(nd)
    columns = {n: X0 + i * COL_W for i, n in enumerate(order)}

    sch = ascview.Schematic(path=Path("generated.asc"))
    touches = {n: [] for n in order}

    for row, dev in enumerate(deck.devices):
        y = Y0 + row * ROW_H
        rot, horizontal = _orientation(dev, columns)
        x, sy = _anchor(dev, columns, y, horizontal)
        sym = ascview.Symbol(dev.symbol, x, sy, rot)
        sym.attrs["InstName"] = dev.refdes
        if dev.value:
            sym.attrs["Value"] = dev.value
        sch.symbols.append(sym)

        for pin, node in zip(ascview.sym_pins(sym), dev.nodes, strict=True):
            if _is_ground(node):
                _ground(sch, pin)
            else:
                _route(sch, pin, columns[node])
                touches[node].append(pin[1])

    _buses(sch, columns, touches)
    sch.texts.extend(_directive_texts(deck, len(deck.devices)))
    sch.sheet = _sheet(sch)
    return sch


def _anchor(dev, columns, y, horizontal):
    """Where to drop the symbol. Every pin is wired to its bus afterwards, so
    this only has to keep the leads short and the body clear of the buses."""
    xs = [columns[n] for n in dev.nodes if not _is_ground(n)]
    centre = sum(xs) // len(xs) if xs else X0
    if horizontal:
        return centre + 40, y - 16
    return (xs[0] if xs else X0) + SHUNT_DX, y


def _route(sch, pin, bus_x):
    """Wire a pin straight across to its net's bus, at the pin's own height.

    Each pin gets its own lane deliberately. Routing a device's pins along
    one shared row line looks tidier and shorts them together: the collector,
    base and emitter leads all met on that line, and the transistor came back
    out of the round trip with three pins on the same net.

    The run may cross other buses on the way. LTspice only joins wires that
    share an endpoint or form a T-junction, and this one ends on its target
    bus, so a crossing is cosmetic.
    """
    px, py = pin
    _wire(sch, px, py, bus_x, py)


def _ground(sch, pin):
    """Flag the pin itself as node 0 -- no lead.

    A lead is what a person draws, and it was how this worked first: a short
    wire down to a flag. On the voltage-controlled switch the two control
    pins sit 48 units apart on the same x, which is exactly the lead length,
    so the lead from the lower pin terminated on the upper one. LTspice joins
    a wire that ends on a pin, so the switch's gate silently became ground
    and the converter lost its drive.

    Flagging the pin directly cannot collide with anything, and it is what
    LTspice itself writes -- `examples/active_lowpass.asc`, drawn by hand,
    grounds V1 with a flag at the pin coordinate.
    """
    sch.flags.append(ascview.Flag(pin[0], pin[1], "0"))


def _wire(sch, x1, y1, x2, y2):
    if (x1, y1) != (x2, y2):
        sch.wires.append(ascview.Wire(x1, y1, x2, y2))


def _buses(sch, columns, touches):
    """One vertical wire per net, spanning everything attached to it.

    The label at the top is not decoration. LTspice joins same-named flags,
    so naming each bus keeps a net one net even where the drawing is crossed,
    and makes the generated file readable.
    """
    for net, x in columns.items():
        ys = touches.get(net)
        if not ys:
            continue
        top, bottom = min(ys), max(ys)
        label_y = top - 32
        _wire(sch, x, label_y, x, bottom)
        sch.flags.append(ascview.Flag(x, label_y, net))


def _directive_texts(deck: Deck, nrows: int) -> list:
    """Carry .model cards and analysis lines onto the sheet.

    LTspice stores a directive as a TEXT line beginning with `!`, exactly as
    a hand-drawn schematic holds its own SPICE directives.
    """
    y = Y0 + nrows * ROW_H + 64
    out = []
    for d in deck.directives:
        out.append(ascview.TextItem(X0, y, "Left", 2, d, True))
        y += 32
    return out


def _sheet(sch) -> tuple:
    x0, y0, x1, y1 = ascview.bounds(sch)
    return (max(880, int(x1 - x0) + 256), max(680, int(y1 - y0) + 256))


# ------------------------------------------------------------------- output

def to_asc(sch: ascview.Schematic) -> str:
    """Serialise a Schematic as LTspice `.asc` text."""
    out = ["Version 4", f"SHEET 1 {sch.sheet[0]} {sch.sheet[1]}"]
    out += [f"WIRE {w.x1} {w.y1} {w.x2} {w.y2}" for w in sch.wires]
    out += [f"FLAG {f.x} {f.y} {f.name}" for f in sch.flags]
    for s in sch.symbols:
        out.append(f"SYMBOL {s.name} {s.x} {s.y} {s.rot}")
        # InstName first, which is the order LTspice itself writes.
        if "InstName" in s.attrs:
            out.append(f"SYMATTR InstName {s.attrs['InstName']}")
        out += [f"SYMATTR {k} {v}" for k, v in s.attrs.items() if k != "InstName"]
    for t in sch.texts:
        body = ("!" if t.directive else ";") + t.text.replace("\n", "\\n")
        out.append(f"TEXT {t.x} {t.y} {t.just} {t.size} {body}")
    return "\n".join(out) + "\n"


def generate(netlist_text: str) -> str:
    """A deck in, an `.asc` out."""
    return to_asc(build_schematic(parse_deck(netlist_text)))


# -------------------------------------------------------------- round trip

def roundtrip(netlist_text: str) -> tuple:
    """Extract the generated schematic again; return (original, extracted).

    Each is {refdes: [nodes]}. Equality means the drawing carries exactly the
    deck's connectivity.
    """
    asc_text = generate(netlist_text)
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "generated.asc"
        p.write_text(asc_text, encoding="utf-8")
        sch = ascview.parse_asc(p)
        extracted, _warnings = netlist_mod.to_netlist(sch)
    return _devices_of(netlist_text), _devices_of(extracted)


def _devices_of(text: str) -> dict:
    """{refdes: [nodes]} for comparison, ignoring values and directives."""
    out = {}
    _title, body = _strip_title(ltspice.strip_control(text).splitlines())
    for raw in body:
        line = raw.split(";")[0].strip()
        if not line or line.startswith(("*", ".", "+")):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        n = PREFIX_NODES.get(parts[0][0].upper())
        if n is None:
            continue
        nodes = [x.lower() for x in parts[1:1 + n]]
        if parts[0][0].upper() == "M":
            nodes = nodes[:3]
        out[parts[0].upper()] = ["0" if _is_ground(x) else x for x in nodes]
    return out


def compare(original: dict, extracted: dict) -> list:
    """Readable differences between two {refdes: nodes} maps."""
    problems = []
    for ref in sorted(set(original) | set(extracted)):
        a, b = original.get(ref), extracted.get(ref)
        if a is None:
            problems.append(f"{ref}: invented by the writer ({b})")
        elif b is None:
            problems.append(f"{ref}: lost by the writer (was {a})")
        elif a != b:
            problems.append(f"{ref}: {a} -> {b}")
    return problems


# ---------------------------------------------------------------------- CLI

def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ascgen", description="Write an ngspice deck out as an LTspice .asc.")
    ap.add_argument("deck", help="netlist file")
    ap.add_argument("-o", "--out", help="output .asc (default: alongside the deck)")
    ap.add_argument("--check", action="store_true",
                    help="re-extract the result and compare connectivity")
    ap.add_argument("--stdout", action="store_true", help="write to stdout")
    args = ap.parse_args(argv)

    src = Path(args.deck)
    text = src.read_text(encoding="utf-8", errors="replace")
    try:
        asc = generate(text)
        if args.check:
            problems = compare(*roundtrip(text))
    except UnsupportedDeck as exc:
        print(f"{src}: {exc}", file=sys.stderr)
        return 2

    if args.check:
        if problems:
            print(f"{src}: connectivity changed by the round trip:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            return 1
        n = len(_devices_of(text))
        print(f"{src}: round-trips exactly ({n} devices)", file=sys.stderr)

    if args.stdout:
        sys.stdout.write(asc)
        return 0
    out = Path(args.out) if args.out else src.with_suffix(".asc")
    out.write_text(asc, encoding="utf-8")
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
