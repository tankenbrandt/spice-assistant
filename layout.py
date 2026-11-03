#!/usr/bin/env python3
"""Step 4 of the ten-step method: which package pin each part of the circuit goes on.

A netlist says what connects to what; the breadboard needs which *pin*. For
the parts this course uses that is not a lookup:

* **CD4007** -- six MOSFETs in one 14-pin package, with strings attached. Q1/Q2
  share a gate pin, as do Q3/Q6 and Q4/Q5; Q4 and Q5 also share a drain (pin
  12); Q3 has one end on pin 7 (VSS) and Q6 one end on pin 14 (VDD); every
  P-channel's substrate is pin 14 and every N-channel's is pin 7. So a circuit's
  transistors have to be *assigned* to slots that respect all of that, and a
  circuit with five NMOS needs two packages. `place` searches for the
  assignment that uses the fewest packages.
* **4001B** -- four 2-input NOR gates. Unused inputs must be tied to a rail,
  which the pin table says.
* **2N2222** -- pin 1 emitter, 2 base, 3 collector, on both the plastic TO-92
  (ON Semi PN2222) and the metal TO-18 can (Microsemi 2N2222A), so it does not
  matter which one is in the kit.

Pinouts are from the course's Appendix A (Figures 1.1 and 2.5) and the two
2N2222 datasheets. Pins are written m.n: package m, pin n.

    python layout.py labs/ee347_exp4/fig4_5.cir
"""

import re
import sys
from dataclasses import dataclass, field

import ascgen
import kit as kitmod


@dataclass(frozen=True)
class Slot:
    unit: str              # "Q1" (a transistor) or "A" (a gate)
    kind: str              # nmos | pmos | nor2 | npn
    pins: tuple            # MOSFET: (gate, sd1, sd2); NOR: (in1, in2, out); BJT: (c, b, e)


@dataclass(frozen=True)
class Package:
    name: str
    npins: int
    slots: tuple
    rails: dict = field(default_factory=dict)       # pin -> "vdd" | "vss"
    labels: dict = field(default_factory=dict)      # pin -> function text
    source: str = ""
    note: str = ""


def _cd4007() -> Package:
    slots = (Slot("Q1", "pmos", (3, 2, 1)), Slot("Q2", "nmos", (3, 5, 4)),
             Slot("Q3", "nmos", (6, 8, 7)), Slot("Q4", "nmos", (10, 12, 9)),
             Slot("Q5", "pmos", (10, 12, 11)), Slot("Q6", "pmos", (6, 13, 14)))
    labels = {1: "Q1 (P) S/D", 2: "Q1 (P) S/D", 3: "Q1/Q2 gate", 4: "Q2 (N) S/D",
              5: "Q2 (N) S/D", 6: "Q3/Q6 gate", 7: "VSS: Q3 (N) S/D, N substrate",
              8: "Q3 (N) S/D", 9: "Q4 (N) S/D", 10: "Q4/Q5 gate",
              11: "Q5 (P) S/D", 12: "Q4/Q5 common S/D", 13: "Q6 (P) S/D",
              14: "VDD: Q6 (P) S/D, P substrate"}
    return Package("CD4007", 14, slots, {14: "vdd", 7: "vss"}, labels,
                   "EE 347 Appendix A, Figure 1.1",
                   "Source and drain are interchangeable on the CD4007 (symmetric "
                   "transistors); the substrates are pins 14 and 7, so pin 14 must be "
                   "the most positive voltage in the circuit and pin 7 the most "
                   "negative.")


def _cd4001() -> Package:
    slots = (Slot("A", "nor2", (1, 2, 3)), Slot("B", "nor2", (5, 6, 4)),
             Slot("C", "nor2", (8, 9, 10)), Slot("D", "nor2", (12, 13, 11)))
    labels = {}
    for s in slots:
        a, b, y = s.pins
        labels[a] = labels[b] = f"gate {s.unit} input"
        labels[y] = f"gate {s.unit} output"
    labels.update({14: "VDD", 7: "VSS"})
    return Package("4001B", 14, slots, {14: "vdd", 7: "vss"}, labels,
                   "EE 347 Appendix A, Figure 2.5",
                   "Tie every unused input to VSS: a floating CMOS input drifts "
                   "and can draw supply current.")


def _2n2222() -> Package:
    return Package("2N2222", 3, (Slot("Q", "npn", (3, 2, 1)),), {},
                   {1: "emitter", 2: "base", 3: "collector"},
                   "ON Semi PN2222 (TO-92) and Microsemi 2N2222A (TO-18) datasheets",
                   "Pin 1 = emitter, 2 = base, 3 = collector on both packages. "
                   "Plastic TO-92: flat face toward you, leads down, pins 1-2-3 left "
                   "to right (E B C). Metal TO-18 can: pin 1 is the lead next to the "
                   "tab, then base, then collector going round; the case is the "
                   "collector, so keep it off other metal.")


PACKAGES = {"CD4007": _cd4007(), "4001B": _cd4001(), "2N2222": _2n2222()}


@dataclass
class Part:
    refdes: str
    kind: str                  # nmos | pmos | nor2 | npn | two (R, C, L, sources)
    nodes: tuple
    model: str = ""
    value: str = ""


@dataclass
class Placed:
    number: int                # the m in m.n
    package: Package
    nets: dict                 # pin -> net
    units: dict                # unit -> refdes
    refdes: str = ""           # a single-device package keeps its own name (Q1)


@dataclass
class Layout:
    packages: list
    where: dict                # refdes -> (Placed, Slot, pins in device-terminal order)
    wiring: dict               # net -> [connection text]
    vdd: str | None
    vss: str
    unplaced: list             # (refdes, why)
    check: str = ""            # verify(): does the packaged circuit still meet spec?


class LayoutError(Exception):
    pass


def parts(netlist: str) -> list:
    """The top-level parts a builder places, typed for packaging."""
    lines = ascgen._join_continuations(netlist.splitlines())
    inside = kitmod._subckt_mask(lines)
    models = {m.group(1).lower(): (m.group(2).lower(), m.group(1))
              for m in (re.match(r"(?i)^\s*\.model\s+(\S+)\s+([a-z]+)", ln) for ln in lines)
              if m}
    out, control, titled = [], False, False
    for ln, sub in zip(lines, inside, strict=True):
        s = ln.strip()
        low = s.lower()
        if not titled and s:
            titled = True               # a deck's first line is its title, whatever it says
            continue
        if low.startswith(".control"):
            control = True
        if low.startswith(".endc"):
            control = False
            continue
        if sub or control or not s or s[0] in "*.+":
            continue
        tok = s.split()
        ref, p = tok[0], tok[0][0].upper()
        if p == "M" and len(tok) >= 6:
            kind = models.get(tok[5].lower(), ("",))[0]
            out.append(Part(ref, kind, tuple(n.lower() for n in tok[1:5]), tok[5]))
        elif p == "Q" and len(tok) >= 5:
            model = next((t for t in tok[4:6] if t.lower() in models), tok[4])
            out.append(Part(ref, models.get(model.lower(), ("npn",))[0],
                            tuple(n.lower() for n in tok[1:4]), model))
        elif p == "X":
            nodes, name = tok[1:-1], tok[-1]
            out.append(Part(ref, "nor2" if "4001" in name else "sub",
                            tuple(n.lower() for n in nodes), name))
        elif p in "RCLVID" and len(tok) >= 3:
            out.append(Part(ref, "two", (tok[1].lower(), tok[2].lower()),
                            value=" ".join(tok[3:])))
    return out


def _rails(ps: list) -> tuple:
    """(vdd, vss) nets: the substrate / supply pins every package shares."""
    vdd = next((p.nodes[3] for p in ps if p.kind == "nor2"), None) \
        or next((p.nodes[3] for p in ps if p.kind == "pmos"), None)
    if vdd is None:
        best = 0.0
        from speccheck import spice_value
        for p in ps:
            if p.refdes[0].upper() == "V" and "0" in p.nodes:
                m = re.search(r"(?i)(?:\bdc\s+)?([-+]?[\d.]+[a-z]*)", p.value)
                try:
                    v = spice_value(m.group(1)) if m else 0.0
                except ValueError:
                    continue
                if v > best and p.nodes[1] == "0":
                    vdd, best = p.nodes[0], v
    return vdd, "0"


def _wants(slot: Slot, d: Part, order) -> dict:
    """pin -> net if device `d` goes in `slot` (MOSFET legs in `order`)."""
    if slot.kind in ("nmos", "pmos"):
        gate, a, b = slot.pins
        return {gate: d.nodes[1], a: order[0], b: order[1]}
    in_a, in_b, y = slot.pins
    return {in_a: d.nodes[0], in_b: d.nodes[1], y: d.nodes[2]}


def _fill(devs: list, i: int, pkg: Package, chips: list, used: list, where: dict) -> bool:
    """Depth-first: place devs[i:] into the packages without a pin holding two nets."""
    if i == len(devs):
        return True
    d = devs[i]
    for c in range(len(chips)):
        if c > 0 and not used[c - 1]:
            break                           # packages are identical: fill them in order
        for slot in pkg.slots:
            if slot.kind != d.kind or slot.unit in used[c]:
                continue
            orders = [(d.nodes[0], d.nodes[2]), (d.nodes[2], d.nodes[0])] \
                if slot.kind in ("nmos", "pmos") else [None]
            for order in orders:
                want = _wants(slot, d, order)
                if any(chips[c].get(pin, net) != net for pin, net in want.items()):
                    continue
                new = {p: v for p, v in want.items() if p not in chips[c]}
                chips[c].update(new)
                used[c].add(slot.unit)
                where[d.refdes] = (c, slot, want)
                if _fill(devs, i + 1, pkg, chips, used, where):
                    return True
                for p in new:
                    del chips[c][p]
                used[c].discard(slot.unit)
                del where[d.refdes]
    return False


def _search(devs: list, pkg: Package, rails: dict, max_packages: int = 8):
    """Fewest packages of `pkg` that hold every device; None if none do."""
    for n in range(1, max_packages + 1):
        chips = [{pin: rails[r] for pin, r in pkg.rails.items()} for _ in range(n)]
        used = [set() for _ in range(n)]
        where: dict = {}
        if _fill(devs, 0, pkg, chips, used, where):
            return [(chips[k], k) for k in range(n) if used[k]], where
    return None


def place(netlist: str, op_nodes: dict | None = None) -> Layout:
    """Assign every packaged device to a package pin, and list the wiring by net."""
    ps = parts(netlist)
    vdd, vss = _rails(ps)
    unplaced, packages, where = [], [], {}
    number = 0

    mos = [p for p in ps if p.kind in ("nmos", "pmos")]
    for p in mos:
        body = p.nodes[3]
        want = vdd if p.kind == "pmos" else vss
        if body != want:
            unplaced.append((p.refdes, f"its substrate is on {body}, but a CD4007's "
                                       f"{'P' if p.kind == 'pmos' else 'N'}-channel "
                                       f"substrate is pin {14 if p.kind == 'pmos' else 7} "
                                       f"({want})"))
    mos = [p for p in mos if p.refdes not in {u for u, _ in unplaced}]

    for pkg_name, devs in (("CD4007", mos), ("4001B", [p for p in ps if p.kind == "nor2"])):
        if not devs:
            continue
        pkg = PACKAGES[pkg_name]
        if devs and vdd is None:
            raise LayoutError("no supply net found for the packages' VDD pin")
        found = _search(devs, pkg, {"vdd": vdd, "vss": vss})
        if found is None:
            unplaced += [(d.refdes, f"no {pkg_name} assignment fits") for d in devs]
            continue
        chips, w = found
        index = {}
        for nets, k in chips:
            number += 1
            pl = Placed(number, pkg, nets, {})
            packages.append(pl)
            index[k] = pl
        for ref, (k, slot, want) in w.items():
            index[k].units[slot.unit] = ref
            where[ref] = (index[k], slot, want)

    for p in ps:
        if p.kind == "npn" and "2n2222" in p.model.lower():
            number += 1
            c, b, e = p.nodes
            pl = Placed(number, PACKAGES["2N2222"], {3: c, 2: b, 1: e}, {"Q": p.refdes},
                        refdes=p.refdes)
            packages.append(pl)
            where[p.refdes] = (pl, PACKAGES["2N2222"].slots[0], {3: c, 2: b, 1: e})
        elif p.kind in ("npn", "pnp", "sub"):
            unplaced.append((p.refdes, f"no pinout on file for {p.model}"))

    wiring = _wiring(ps, packages, op_nodes)
    for pl in packages:
        for pin, (net, why) in sorted(unused_ties(pl, vss, (vdd, vss)).items()):
            short = "unused input" if "input" in why else f"unused {why.split()[1].rstrip(':')}"
            wiring.setdefault(net, []).append(f"{pl.number}.{pin} ({short}, tied)")
    return Layout(packages, where, wiring, vdd, vss, unplaced)


_SIZE_RE = re.compile(r"(?i)\b([wl])\s*=\s*(\S+)")


def packaged_deck(netlist: str, lay: Layout) -> str:
    """The circuit rebuilt from the pin table: each CD4007 as the whole package.

    The pin table only proves itself if the circuit it describes still works,
    and on the bench the *unused* transistors are there too: an idle Q5 shares
    pin 12 with whatever Q4 drives, an idle gate shares its pin with a live
    one, and unused gate pins are tied to VSS as the table says. This deck has
    all six transistors of every CD4007 in place, wired by pin number, so
    simulating it against the spec checks the assignment, not just its
    bookkeeping. Unconnected pins get 1 GΩ to ground: breadboard-grade leakage,
    and it keeps the matrix solvable. That value matters for dynamic logic --
    with the unused Q5 left floating on Fig. 4.4's precharged node, the node
    holds with a 1 TΩ pin but sags to 2.15 V with 1 GΩ -- which is why
    `unused_ties` shorts such transistors rather than leaving them hanging.
    """
    chips = [pl for pl in lay.packages if pl.package.name == "CD4007"]
    if not chips:
        return netlist
    lines = ascgen._join_continuations(netlist.splitlines())
    inside = kitmod._subckt_mask(lines)
    placed = {ref.lower() for ref, (pl, _, _) in lay.where.items()
              if pl.package.name == "CD4007"}
    model, size = {}, {}
    for p in parts(netlist):
        if p.kind in ("nmos", "pmos") and p.kind not in model:
            model[p.kind] = p.model
            src = next(ln for ln in lines if ln.split() and ln.split()[0] == p.refdes)
            size[p.kind] = " ".join(f"{k.upper()}={v}" for k, v in _SIZE_RE.findall(src))
    kept, at = [], None
    titled = False
    for ln, sub in zip(lines, inside, strict=True):
        tok = ln.split()
        if not titled and tok:
            titled = True
            kept.append(ln)
            continue
        if not sub and tok and tok[0].lower() in placed:
            continue
        low = ln.strip().lower()
        if at is None and not sub and (low.startswith(".control")
                                       or (low.startswith(".end") and not low.startswith(".ends"))):
            at = len(kept)
        kept.append(ln)
    added = []
    for pl in chips:
        body = []
        for s in pl.package.slots:
            if s.kind not in model:
                continue                     # no model for it in the deck: cannot simulate it
            g, a, b = s.pins
            bulk = 14 if s.kind == "pmos" else 7
            body.append(f"M{s.unit} p{a} p{g} p{b} p{bulk} {model[s.kind]} {size[s.kind]}")
        name = f"CD4007_PKG{pl.number}"
        added += [f".subckt {name} " + " ".join(f"p{i}" for i in range(1, 15))] + body \
            + [f".ends {name}"]
        ties = unused_ties(pl, lay.vss, (lay.vdd, lay.vss))
        nets = []
        for pin in range(1, 15):
            net = pl.nets.get(pin)
            if net is None and pin in ties:
                net = ties[pin][0]            # wired exactly as the pin table says
            if net is None:
                net = f"nc{pl.number}_{pin}"
                added.append(f"Rnc{pl.number}_{pin} {net} 0 1G")
            nets.append(net)
        added.append(f"XPKG{pl.number} " + " ".join(nets) + f" {name}")
    at = len(kept) if at is None else at
    return "\n".join(kept[:at] + ["* --- CD4007 packages as wired (layout.py) ---"]
                     + added + kept[at:]) + "\n"


def verify(spec, netlist: str, lay: Layout) -> str:
    """Simulate the packaged circuit against the spec; say whether it still passes."""
    import specs as specfile
    if not any(pl.package.name == "CD4007" for pl in lay.packages):
        return ""
    deck = packaged_deck(netlist, lay)
    try:
        got = specfile.measure(spec, deck)
    except Exception as e:                    # noqa: BLE001 - report, don't hide
        return f"FAILED: the circuit rebuilt from this pin table did not simulate ({e})."
    bad = [m.title for m in spec.specs if not m.limits.ok(got.get(m.name, float("nan")))]
    n = len(spec.specs)
    if bad:
        return (f"FAILED: rebuilt from this pin table, with every transistor in each "
                f"package present, the circuit misses {len(bad)} of {n} checks: "
                + ", ".join(bad) + ".")
    return (f"Checked: the circuit rebuilt from this pin table, with all six "
            f"transistors of each CD4007 present and every unused pin wired as the "
            f"table says, passes all {n} checks in simulation.")


def _polarity(p: Part, op_nodes: dict | None) -> str:
    """Which end of an electrolytic goes positive, from the DC operating point."""
    from speccheck import spice_value
    if not op_nodes or p.refdes[0].upper() != "C":
        return ""
    try:
        if spice_value(p.value.split()[0]) < 1e-6:
            return ""
    except (ValueError, IndexError):
        return ""
    v = [0.0 if n == "0" else op_nodes.get(n) for n in p.nodes]
    if None in v or abs(v[0] - v[1]) < 0.05:
        return ""
    return "+" if v[0] > v[1] else "−"


def _wiring(ps: list, packages: list, op_nodes) -> dict:
    nets: dict = {}

    def add(net, text):
        nets.setdefault(net, []).append(text)

    for pl in packages:
        pinfo = pl.package.labels
        for pin in range(1, pl.package.npins + 1):
            net = pl.nets.get(pin)
            if net is None:
                continue
            name = pl.refdes or f"IC {pl.number}"
            what = pinfo.get(pin, "")
            add(net, f"{pl.number}.{pin} ({name} {what})" if pl.refdes
                else f"{pl.number}.{pin} ({what})")
    for p in ps:
        if p.kind != "two":
            continue
        pol = _polarity(p, op_nodes)
        for i, net in enumerate(p.nodes):
            if p.refdes[0].upper() == "V":
                add(net, f"{p.refdes} {'+' if i == 0 else '−'}")
            elif pol:
                end = pol if i == 0 else ("−" if pol == "+" else "+")
                add(net, f"{p.refdes} ({end} lead)")
            else:
                add(net, p.refdes)
    return nets


def unused_ties(pl: Placed, vss: str, rails=()) -> dict:
    """What each unconnected pin should be wired to: pin -> (net, why).

    * An unused input (a gate pin no device uses) goes to VSS: floating CMOS
      inputs drift.
    * An unused CD4007 transistor that shares one leg with a live node (Q4 and
      Q5 share pin 12) has its other leg tied to that same node. Otherwise it
      switches on whenever its gate -- shared with a live transistor -- says
      so, and connects the node to a floating pin, whose leakage a dynamic
      (precharged) node then depends on. Shorted to itself it does nothing.
    """
    ties = {}
    used = set(pl.units)
    for s in pl.package.slots:
        if s.kind == "nor2":
            for pin in s.pins[:2]:
                if pin not in pl.nets:
                    ties[pin] = (vss, "unused input: tie to VSS")
            continue
        if s.kind not in ("nmos", "pmos"):
            continue
        g, a, b = s.pins
        if g not in pl.nets:
            ties[g] = (vss, "unused input: tie to VSS")
        if s.unit in used:
            continue
        for live, free in ((a, b), (b, a)):
            net = pl.nets.get(live)
            if net is not None and net not in rails and free not in pl.nets:
                ties[free] = (net, f"unused {s.unit}: tie to {pl.number}.{live} "
                                   f"({net}) so it cannot conduct")
    return ties


def pin_rows(pl: Placed, vss: str, rails=()) -> list:
    """(m.n, function, net or what to do with it) for every pin of a package."""
    rows = []
    ties = unused_ties(pl, vss, rails)
    for pin in range(1, pl.package.npins + 1):
        net = pl.nets.get(pin)
        if net is not None:
            shown = "ground (0)" if net == "0" else net
        elif pin in ties:
            shown = ties[pin][1]
        else:
            shown = "no connection"
        rows.append((f"{pl.number}.{pin}", pl.package.labels.get(pin, ""), shown))
    return rows


def device_rows(lay: Layout) -> list:
    rows = []
    for ref, (pl, slot, _want) in lay.where.items():
        pkg = pl.package
        if slot.kind in ("nmos", "pmos"):
            g, a, b = slot.pins
            pins = f"gate {pl.number}.{g}; S/D {pl.number}.{a}, {pl.number}.{b}"
            unit = f"{slot.unit} ({'P' if slot.kind == 'pmos' else 'N'})"
        elif slot.kind == "nor2":
            a, b, y = slot.pins
            pins = f"in {pl.number}.{a}, {pl.number}.{b}; out {pl.number}.{y}"
            unit = f"gate {slot.unit}"
        else:
            c, b, e = slot.pins
            pins = f"E {pl.number}.{e}, B {pl.number}.{b}, C {pl.number}.{c}"
            unit = "—"
        rows.append((ref, f"{pl.number}: {pkg.name}", unit, pins))
    return rows


def _cli(argv=None) -> int:
    path = (argv or sys.argv[1:])[0]
    from pathlib import Path
    lay = place(Path(path).read_text(encoding="utf-8"))
    for r in device_rows(lay):
        print(" | ".join(r))
    for pl in lay.packages:
        print(f"\nPackage {pl.number}: {pl.package.name}")
        for r in pin_rows(pl, lay.vss, (lay.vdd, lay.vss)):
            print("  " + " | ".join(r))
    print("\nWiring by net:")
    for net, items in lay.wiring.items():
        print(f"  {net}: " + ", ".join(items))
    for ref, why in lay.unplaced:
        print(f"NOT PLACED {ref}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
