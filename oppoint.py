#!/usr/bin/env python3
"""The DC operating point of a whole deck: every node voltage, every branch current.

The spec layer is deliberately narrow -- a spec names the handful of vectors it
cares about, and `speccheck` dumps exactly those. That is the right shape for a
pass/fail verdict and the wrong shape for the question an engineer actually asks
first, which is "where is this circuit sitting?". A bias point that looks wrong
explains a gain that measures wrong, and no spec file has to have anticipated it.

ngspice can answer that without being told what to look for:

* `print all` after `op` gives every node voltage in the circuit, plus the
  branch current of every independent source.
* `show all : i, ic, ib, ie, id` gives per-device currents. One `show` covers
  every device type at once, because a parameter a device does not implement
  comes back as `?????????` rather than shifting the columns.

The catch is that a name ngspice accepts is not always a current: `ic` is
collector current on a BJT and the *initial condition* on a capacitor, where it
returns a plausible-looking 0. So each device type is read for only the currents
it actually has -- see `CURRENTS_BY_KIND`.

    python oppoint.py repaired/ce_bjt_amp.cir
    python oppoint.py repaired/ce_bjt_amp.cir --markdown

Node 0 is ground by definition and ngspice does not list it; neither does this.
"""

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import speccheck
from plot import eng

# One run answers both halves. `op` alone would leave the operating point in
# the plot but print nothing.
OP_CONTROL = "op\nprint all\nshow all : i, ic, ib, ie, id\n"

# ngspice renders a parameter a device does not implement as a row of question
# marks rather than omitting the column, so the width is stable and the value is
# not a number.
_MISSING = "?"

# ngspice always has a "constants" plot. When a deck has no nodes to print,
# `print all` silently falls back to it and every constant reads as a node
# voltage -- eleven plausible-looking nodes for a circuit that has none. Matched
# on value as well as name so a real node that happens to be called `c` or `e`
# survives.
NGSPICE_CONSTANTS = {
    "false": 0.0, "true": 1.0, "boltz": 1.380649e-23, "c": 2.997925e08,
    "e": 2.718282, "echarge": 1.602177e-19, "kelvin": -273.150,
    "no": 0.0, "pi": 3.141593, "planck": 6.626070e-34, "yes": 1.0,
}


def is_constant(name: str, value: float) -> bool:
    """True for one of ngspice's own constants rather than a circuit node."""
    ref = NGSPICE_CONSTANTS.get(name.lower())
    return ref is not None and math.isclose(value, ref, rel_tol=1e-6, abs_tol=1e-30)


_ASSIGN_RE = re.compile(
    r"^\s*([^\s=]+)\s*=\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$")
_TYPE_RE = re.compile(r"^\s{1,3}(\w[\w ]*):\s+(\S.*)$")

# What each current means, for the reader. ngspice's own names are terse and
# sign conventions are not obvious from them.
CURRENT_LABELS = {
    "i": "I", "ic": "Ic", "ib": "Ib", "ie": "Ie", "id": "Id",
}

# One `show` asks every device for every current name, and a name means
# different things to different devices: `ic` is collector current on a BJT and
# the *initial condition* on a capacitor or inductor, where it comes back as a
# perfectly plausible 0 rather than as `?????????`. Reporting that as a current
# would be wrong, so each device type is asked only for the currents it has.
CURRENTS_BY_KIND = (
    ("bjt", ("ic", "ib", "ie")),
    ("diode", ("id",)),
    ("mos", ("id",)),       # Mos1/Mos2/Mos3: `ib` here is bulk leakage, not base
    ("bsim", ("id",)),
    ("vdmos", ("id",)),
    ("jfet", ("id",)),
)
DEFAULT_CURRENTS = ("i",)

# Below this, a number is the solver's zero rather than a measurement. Printing
# a load current as -8.45e-29 A invites a reader to think something is flowing.
NEGLIGIBLE = 1e-15


def currents_for(kind: str) -> tuple:
    """The current parameters that are meaningful for one ngspice device type."""
    low = kind.lower()
    for prefix, params in CURRENTS_BY_KIND:
        if low.startswith(prefix):
            return params
    return DEFAULT_CURRENTS


def _eng(v: float, unit: str) -> str:
    return f"{eng(0.0 if abs(v) < NEGLIGIBLE else v)}{unit}"


@dataclass
class Device:
    """One device's currents, as `show` reported them."""
    refdes: str
    kind: str
    model: str = ""
    currents: dict = field(default_factory=dict)

    def describe(self) -> str:
        return ", ".join(f"{CURRENT_LABELS.get(k, k)} {_eng(v, 'A')}"
                         for k, v in self.currents.items())


@dataclass
class OperatingPoint:
    nodes: dict = field(default_factory=dict)      # node -> volts
    sources: dict = field(default_factory=dict)    # source -> amps
    devices: list = field(default_factory=list)
    output: str = ""

    @property
    def empty(self) -> bool:
        return not self.nodes and not self.devices

    @property
    def branches(self) -> int:
        """Rows in the current table: one per device, plus unlisted sources."""
        return len(_device_rows(self))


def parse_voltages(text: str) -> tuple[dict, dict]:
    """Split `print all` output into node voltages and source branch currents.

    ngspice names a source's current `<name>#branch`, and puts it in the same
    list as the node voltages. They are different quantities and belong in
    different tables.
    """
    nodes, sources = {}, {}
    for line in text.splitlines():
        m = _ASSIGN_RE.match(line)
        if not m:
            continue
        name, raw = m.group(1), m.group(2)
        try:
            value = float(raw)
        except ValueError:                  # pragma: no cover - regex-guarded
            continue
        if name.lower().endswith("#branch"):
            sources[name[: -len("#branch")]] = value
        elif not is_constant(name, value):
            nodes[name] = value
    return nodes, sources


def parse_currents(text: str) -> list:
    """Parse `show`'s column layout into one Device per column.

    `show` prints a block per device type, with devices across the columns and
    parameters down the rows, so a row is only meaningful against the `device`
    row above it.
    """
    devices: dict = {}
    order: list = []
    kind = ""
    columns: list = []
    for line in text.splitlines():
        if not line.strip():
            columns = []
            continue
        head = _TYPE_RE.match(line)
        if head:
            kind, columns = head.group(1).strip(), []
            continue
        parts = line.split()
        label, values = parts[0], parts[1:]
        if label == "device":
            columns = values
            for ref in columns:
                if ref not in devices:
                    devices[ref] = Device(refdes=ref, kind=kind)
                    order.append(ref)
            continue
        if not columns or len(values) != len(columns):
            continue
        if label == "model":
            for ref, val in zip(columns, values, strict=True):
                devices[ref].model = "" if val.startswith(_MISSING) else val
            continue
        for ref, val in zip(columns, values, strict=True):
            if val.startswith(_MISSING):
                continue
            if label not in currents_for(devices[ref].kind):
                continue
            try:
                devices[ref].currents[label] = float(val)
            except ValueError:
                continue
    return [devices[r] for r in order]


def operating_point(netlist: str) -> OperatingPoint:
    """Run the deck's DC operating point and read everything back."""
    _, out = speccheck.run_measurement(netlist, OP_CONTROL)
    nodes, sources = parse_voltages(out)
    op = OperatingPoint(nodes=nodes, sources=sources,
                        devices=parse_currents(out), output=out)
    if op.empty:
        raise RuntimeError(
            "ngspice reported no operating point; the deck may not converge "
            "(run it directly to see the error)")
    return op


# ------------------------------------------------------------------ rendering

def _node_rows(op: OperatingPoint) -> list:
    return [(n, _eng(v, "V")) for n, v in sorted(op.nodes.items())]


def _device_rows(op: OperatingPoint) -> list:
    rows = []
    for d in op.devices:
        if not d.currents:
            continue
        kind = f"{d.kind} ({d.model})" if d.model else d.kind
        rows.append((d.refdes, kind, d.describe()))
    seen = {d.refdes.lower() for d in op.devices}
    for name, amps in sorted(op.sources.items()):
        if name.lower() in seen:            # `show` already reported this source
            continue
        rows.append((name, "Source branch", f"I {_eng(amps, 'A')}"))
    return rows


def markdown(op: OperatingPoint, title: str = "Operating point") -> str:
    """The same two tables, as Markdown for a report."""
    out = [f"### {title}", "", "| Node | Voltage |", "|---|---|"]
    out += [f"| `{n}` | {v} |" for n, v in _node_rows(op)]
    out += ["", "| Device | Type | Current |", "|---|---|---|"]
    out += [f"| `{r}` | {k} | {c} |" for r, k, c in _device_rows(op)]
    out.append("")
    out.append("Node `0` is ground. Currents are ngspice's own sign convention: "
               "positive into the first-listed terminal.")
    return "\n".join(out)


def text(op: OperatingPoint) -> str:
    nodes = _node_rows(op)
    devs = _device_rows(op)
    width = max((len(n) for n, _ in nodes), default=4)
    out = [f"{len(nodes)} nodes, {len(devs)} branches", "", "  node voltages"]
    out += [f"    {n:<{width}}  {v:>10}" for n, v in nodes]
    out += ["", "  device currents"]
    dw = max((len(r) for r, _, _ in devs), default=6)
    out += [f"    {r:<{dw}}  {c}" for r, _, c in devs]
    return "\n".join(out)


# ------------------------------------------------------------------------ CLI

def _cli(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="spice-op",
        description="Every node voltage and branch current in a deck.")
    ap.add_argument("deck", help="netlist file")
    ap.add_argument("--markdown", action="store_true", help="emit a Markdown table")
    args = ap.parse_args(argv)

    path = Path(args.deck)
    if not path.exists():
        print(f"not found: {path}", file=sys.stderr)
        return 2
    try:
        op = operating_point(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:                # noqa: BLE001 - reported, not raised
        print(f"{path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"{path}")
    print(markdown(op) if args.markdown else text(op))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
