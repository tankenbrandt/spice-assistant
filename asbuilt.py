#!/usr/bin/env python3
"""Grade a deck as it would be built, not as it was typed.

The first A/B (ab_tolerance.py) showed a model told how it is graded will
optimise the grader rather than the circuit: Opus split every resistor into
series pieces, which independent Monte Carlo draws reward, and leaned on an
ideal current source the grader never varies. This module closes those gaps
before a deck is measured:

* snap    every R, C and L moves to the nearest value you can buy: E24 for
          5% resistors, E96 for 1%, E24 for C0G/film capacitors, E12 otherwise.
* merge   then series/parallel resistor chains and parallel capacitors become
          the one part they stand for: a chain may hit an exact value, but it
          varies as one part, so splitting buys no tolerance.
* rules   only the element types the circuit allows (no ideal current
          sources; no controlled sources unless the prompt grants an ideal
          op-amp), and no more DC sources than the prompt's supplies.

All of it is stated to the model in every prompt arm, so the grader checks
nothing the model wasn't told.
"""

import math
import re

import kit
from robustness import _control_mask, _subckt_owner
from speccheck import spice_value

BASE_ELEMENTS = frozenset("rcldqmvx")      # passives, diodes, BJTs, MOSFETs, sources, subckt calls
_VEC_NODE_RE = re.compile(r"(?i)\b(?:v|vm|vdb|vp|vr|vi)\(([^)]*)\)")


def series_for(kind: str, tol: float) -> str:
    """The E-series a part of this kind and tolerance is sold in."""
    if kind == "r":
        return "E96" if tol <= 0.01 else "E24"
    if kind == "c":         # C0G/film (<= 5%) are sold in E24; X7R and wider in E12
        return "E24" if tol <= 0.05 else "E12"
    return "E12"


def snap_value(v: float, series: str) -> float:
    """Nearest value in `series`, by ratio (log distance)."""
    if v <= 0:
        return v
    d = math.floor(math.log10(v))
    cands = kit.series_values(series, 10.0 ** (d - 1), 10.0 ** (d + 2))
    return min(cands, key=lambda c: abs(math.log(c / v)))


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def _top_level(lines: list) -> list:
    """Indices of element lines outside .control and .subckt bodies."""
    ctrl, owner = _control_mask(lines), _subckt_owner(lines)
    title = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    out = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if i == title or not s or s.startswith(("*", ".", "+")) or ctrl[i] or owner[i]:
            continue
        out.append(i)
    return out


def _all_elements(lines: list) -> list:
    """Indices of element lines anywhere (subckt bodies included), not .control."""
    ctrl = _control_mask(lines)
    title = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    return [i for i, ln in enumerate(lines)
            if i != title and ln.strip() and not ln.strip().startswith(("*", ".", "+"))
            and not ctrl[i]]


def probed_nodes(spec) -> set:
    nodes = {"0"}
    for a in spec.analyses.values():
        for v in a.vectors:
            for m in _VEC_NODE_RE.finditer(v):
                nodes.update(n.strip().lower() for n in m.group(1).split(","))
    return nodes


def _two_terminal(ln: str):
    """(name, n1, n2, value) for a plain R/C/L line, else None."""
    toks = ln.split()
    if len(toks) != 4 or toks[0][0].lower() not in "rcl":
        return None
    try:
        return toks[0], toks[1].lower(), toks[2].lower(), spice_value(toks[3])
    except ValueError:
        return None


def merge_passives(deck: str, keep: set) -> tuple:
    """Collapse resistor chains and parallel R/C pairs. Returns (deck, merges)."""
    lines = deck.splitlines()
    merges = []
    changed = True
    while changed:
        changed = False
        top = _top_level(lines)
        parts = {i: _two_terminal(lines[i]) for i in top}
        # How many times each node is named by any top-level line.
        uses = {}
        for i in top:
            for t in lines[i].split()[1:]:
                uses[t.lower()] = uses.get(t.lower(), 0) + 1
        res = [i for i in top if parts[i] and parts[i][0][0].lower() == "r"]
        caps = [i for i in top if parts[i] and parts[i][0][0].lower() == "c"]

        # Series resistors: an internal node named by exactly two resistors.
        for a in res:
            for b in res:
                if b <= a or changed:
                    continue
                na, a1, a2, va = parts[a]
                nb, b1, b2, vb = parts[b]
                shared = ({a1, a2} & {b1, b2}) - keep
                if len(shared) != 1:
                    continue
                n = shared.pop()
                ends = [x for x in (a1, a2) if x != n] + [x for x in (b1, b2) if x != n]
                if uses.get(n) != 2 or len(ends) != 2 or ends[0] == ends[1]:
                    continue
                lines[a] = f"{na} {ends[0]} {ends[1]} {_fmt(va + vb)}"
                del lines[b]
                merges.append(f"{na}+{nb} in series -> {_fmt(va + vb)}")
                changed = True
        if changed:
            continue
        # Parallel pairs on the same two nodes.
        for group, kind in ((res, "r"), (caps, "c")):
            for a in group:
                for b in group:
                    if b <= a or changed:
                        continue
                    na, a1, a2, va = parts[a]
                    nb, b1, b2, vb = parts[b]
                    if {a1, a2} != {b1, b2}:
                        continue
                    v = va + vb if kind == "c" else 1.0 / (1.0 / va + 1.0 / vb)
                    lines[a] = f"{na} {a1} {a2} {_fmt(v)}"
                    del lines[b]
                    merges.append(f"{na}||{nb} -> {_fmt(v)}")
                    changed = True
    return "\n".join(lines) + "\n", merges


def snap_deck(deck: str, tol: dict) -> tuple:
    """Every plain R/C/L value to its purchasable series. Returns (deck, snaps)."""
    lines = deck.splitlines()
    snaps = []
    for i in _top_level(lines):
        p = _two_terminal(lines[i])
        if not p:
            continue
        name, n1, n2, v = p
        kind = name[0].lower()
        if kind not in tol or v <= 0:
            continue
        s = snap_value(v, series_for(kind, tol[kind]))
        if abs(s / v - 1) > 1e-6:
            lines[i] = f"{name} {n1} {n2} {_fmt(s)}"
            snaps.append(f"{name} {_fmt(v)} -> {_fmt(s)}")
    return "\n".join(lines) + "\n", snaps


def dc_supplies(deck: str) -> int:
    """Top-level V sources with a non-zero DC value: the deck's supplies."""
    lines = deck.splitlines()
    n = 0
    for i in _top_level(lines):
        toks = lines[i].split()
        if toks[0][0].lower() != "v" or len(toks) < 4:
            continue
        k = next((j + 1 for j in range(3, len(toks) - 1) if toks[j].lower() == "dc"), 3)
        try:
            if spice_value(toks[k]) != 0:
                n += 1
        except ValueError:
            pass
    return n


def violations(deck: str, allow: frozenset = BASE_ELEMENTS, supplies: int = 1) -> list:
    lines = deck.splitlines()
    out = []
    for i in _all_elements(lines):
        letter = lines[i].strip()[0].lower()
        if letter not in allow:
            what = {"i": "ideal current source", "b": "behavioral source",
                    "e": "ideal controlled source", "g": "ideal controlled source",
                    "f": "ideal controlled source", "h": "ideal controlled source"}
            out.append(f"{lines[i].split()[0]}: {what.get(letter, 'element')} not allowed")
    n = dc_supplies(deck)
    if n > supplies:
        out.append(f"{n} DC sources; the prompt gives {supplies}")
    return out


def prepare(deck: str, spec, tol: dict, allow: frozenset = BASE_ELEMENTS,
            supplies: int = 1) -> dict:
    """The deck as built, plus everything that was changed or is disallowed."""
    v = violations(deck, allow, supplies)
    # Snap each part first: every resistor in a chain must be one you can buy.
    # Then merge: two E96 parts in series may hit a value no single part does
    # (a fair technique), but the chain varies as one part, so it buys no
    # tolerance.
    snapped, snaps = snap_deck(deck, tol)
    built, merges = merge_passives(snapped, probed_nodes(spec))
    return {"deck": built, "violations": v, "merges": merges, "snaps": snaps}
