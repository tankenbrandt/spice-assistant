#!/usr/bin/env python3
"""Read LTspice's own `.asy` symbol library.

`ascview` ships a small hand-drawn symbol table so it works on a machine with
no LTspice at all. That table cannot cover the 6000-odd symbols a real
installation carries, so anything outside it was drawn as a labelled box with
pins inferred from the wiring, and `netlist.py` refused to guess its
subcircuit pin order.

When LTspice *is* installed, none of that guessing is necessary. Every `.asy`
states its pin coordinates exactly, and -- the part that matters for netlisting
-- a `SpiceOrder` per pin, which is the subcircuit's node order. This module
reads those files, so a library part is drawn from its real geometry and
netlisted in its real pin order.

    python symlib.py npn                  # where it lives, and its pins
    python symlib.py OpAmps/OP07

Everything degrades cleanly: with no library found, callers fall back to the
built-in table exactly as before.
"""

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Where LTspice keeps lib/sym, newest layout first. LTSPICE_SYM_DIR overrides.
_CANDIDATES = [
    r"%LOCALAPPDATA%\LTspice\lib\sym",
    r"%ProgramFiles%\ADI\LTspice\lib\sym",
    r"%ProgramFiles%\LTC\LTspiceXVII\lib\sym",
    r"%ProgramFiles(x86)%\LTC\LTspiceXVII\lib\sym",
    r"%ProgramFiles(x86)%\LTC\LTspiceIV\lib\sym",
    "~/Library/Application Support/LTspice/lib/sym",
    "~/.wine/drive_c/Program Files/LTC/LTspiceXVII/lib/sym",
    "~/.wine/drive_c/users/$USER/AppData/Local/LTspice/lib/sym",
]


@dataclass
class AsyPin:
    x: int
    y: int
    name: str = ""
    order: int = 0


@dataclass
class AsySymbol:
    """One parsed `.asy`."""
    name: str
    path: Path
    pins: list = field(default_factory=list)      # in SpiceOrder
    draw: list = field(default_factory=list)      # ascview-style ops
    attrs: dict = field(default_factory=dict)

    @property
    def prefix(self) -> str:
        """SPICE refdes letter the symbol declares, e.g. QN -> Q."""
        return (self.attrs.get("Prefix") or "").strip()

    @property
    def coords(self) -> list:
        return [(p.x, p.y) for p in self.pins]

    @property
    def pin_names(self) -> list:
        return [p.name for p in self.pins]

    def box(self) -> tuple:
        xs, ys = [p.x for p in self.pins], [p.y for p in self.pins]
        for op in self.draw:
            if op[0] == "line":
                xs += [op[1], op[3]]
                ys += [op[2], op[4]]
            elif op[0] == "circle":
                xs += [op[1] - op[3], op[1] + op[3]]
                ys += [op[2] - op[3], op[2] + op[3]]
            elif op[0] == "poly":
                xs += [q[0] for q in op[1]]
                ys += [q[1] for q in op[1]]
        if not xs:
            return (-32, 0, 32, 64)
        return (min(xs), min(ys), max(xs), max(ys))


# ------------------------------------------------------------------ locating

def find_library(explicit: str | None = None) -> Path | None:
    """Locate lib/sym. LTSPICE_SYM_DIR wins, then the usual install paths."""
    for raw in filter(None, [explicit, os.environ.get("LTSPICE_SYM_DIR")]):
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if p.is_dir():
            return p
    for raw in _CANDIDATES:
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if "%" in expanded or "$" in expanded:   # a variable that did not resolve
            continue
        p = Path(expanded)
        if p.is_dir():
            return p
    return None


def _read_text(path: Path) -> str:
    """Some .asy files are UTF-16; most are plain ASCII."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


def resolve(name: str, root: Path | None = None) -> Path | None:
    """Map an .asc SYMBOL name like `OpAmps\\OP07` to its file on disk."""
    root = root or find_library()
    if root is None:
        return None
    parts = name.replace("\\\\", "\\").replace("\\", "/").split("/")
    direct = root.joinpath(*parts).with_suffix(".asy")
    if direct.is_file():
        return direct
    # Case-insensitive fallback, for libraries copied off a case-sensitive fs.
    folder = root.joinpath(*parts[:-1]) if len(parts) > 1 else root
    if folder.is_dir():
        want = parts[-1].lower() + ".asy"
        for cand in folder.iterdir():
            if cand.name.lower() == want:
                return cand
    return None


# ------------------------------------------------------------------- parsing

def parse(text: str, name: str = "", path: Path | None = None) -> AsySymbol:
    """Parse .asy source into pins (SpiceOrder) and ascview drawing ops."""
    sym = AsySymbol(name=name, path=path or Path(name))
    pending = None
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        key = parts[0].upper()
        try:
            if key == "PIN" and len(parts) >= 3:
                # Parse first: a malformed PIN line must not flush (and so
                # duplicate) the pin that is still pending.
                px, py = int(parts[1]), int(parts[2])
                if pending is not None:
                    sym.pins.append(pending)
                pending = AsyPin(px, py)
            elif key == "PINATTR" and pending is not None and len(parts) >= 3:
                if parts[1] == "PinName":
                    pending.name = " ".join(parts[2:])
                elif parts[1] == "SpiceOrder":
                    pending.order = int(parts[2])
            elif key == "SYMATTR" and len(parts) >= 2:
                sym.attrs[parts[1]] = " ".join(parts[2:])
            elif key == "LINE" and len(parts) >= 6:
                x1, y1, x2, y2 = (int(v) for v in parts[2:6])
                sym.draw.append(("line", x1, y1, x2, y2))
            elif key == "RECTANGLE" and len(parts) >= 6:
                x1, y1, x2, y2 = (int(v) for v in parts[2:6])
                sym.draw.append(("poly", [(x1, y1), (x2, y1), (x2, y2),
                                          (x1, y2)], True, "none"))
            elif key == "CIRCLE" and len(parts) >= 6:
                x1, y1, x2, y2 = (int(v) for v in parts[2:6])
                cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
                r = (abs(x2 - x1) + abs(y2 - y1)) / 4        # mean radius
                sym.draw.append(("circle", cx, cy, r))
            elif key == "ARC" and len(parts) >= 10:
                # Bounding box plus start/end points; approximated as a chord
                # through the box centre, which keeps the outline readable.
                x1, y1, x2, y2, xs_, ys_, xe, ye = (int(v) for v in parts[2:10])
                r = (abs(x2 - x1) + abs(y2 - y1)) / 4
                sym.draw.append(("arc", xs_, ys_, xe, ye, max(r, 1)))
            elif key == "TEXT" and len(parts) >= 6:
                x, y, size = int(parts[1]), int(parts[2]), int(parts[4])
                sym.draw.append(("text", x, y, " ".join(parts[5:]), 8 + size))
        except ValueError:
            continue          # malformed line: skip it, keep the symbol
    if pending is not None:
        sym.pins.append(pending)

    # SpiceOrder is the subcircuit's node order, and is what makes a library
    # part netlistable. Files that omit it keep their declared order.
    if all(p.order for p in sym.pins):
        sym.pins.sort(key=lambda p: p.order)
    return sym


_cache: dict = {}


def lookup(name: str, root: Path | None = None) -> AsySymbol | None:
    """Find and parse a symbol by its .asc name. Cached; None if unavailable."""
    key = (name.lower(), str(root) if root else "")
    if key in _cache:
        return _cache[key]
    path = resolve(name, root)
    sym = parse(_read_text(path), name, path) if path else None
    _cache[key] = sym
    return sym


def available(root: Path | None = None) -> bool:
    return (root or find_library()) is not None


# --------------------------------------------------------------------- CLI

def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="symlib", description="Inspect LTspice's .asy symbol library.")
    ap.add_argument("symbols", nargs="*", help="symbol names, e.g. npn OpAmps/OP07")
    ap.add_argument("--lib", help="path to lib/sym (default: autodetect)")
    args = ap.parse_args(argv)

    root = find_library(args.lib)
    if root is None:
        print("no LTspice symbol library found; set LTSPICE_SYM_DIR",
              file=sys.stderr)
        return 1
    print(f"library: {root}")
    if not args.symbols:
        print(f"{sum(1 for _ in root.rglob('*.asy'))} symbols available")
        return 0

    rc = 0
    for name in args.symbols:
        sym = lookup(name, root)
        if sym is None:
            print(f"{name}: not found")
            rc = 1
            continue
        print(f"\n{name}  ({sym.path.relative_to(root)})")
        if sym.prefix:
            print(f"  SPICE prefix: {sym.prefix}")
        for p in sym.pins:
            print(f"  {p.order}. {p.name or '?':<8s} ({p.x:>5},{p.y:>5})")
        print(f"  {len(sym.draw)} drawing ops, box {sym.box()}")
    return rc


if __name__ == "__main__":
    sys.exit(_cli())
