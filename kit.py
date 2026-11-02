#!/usr/bin/env python3
"""Build it from the kit: pick real parts that land closest to the ideal design.

A handout gives the circuit and the theory. The theory wants Cf = 1.5915 nF;
the drawer holds 1.5 nF and 2.2 nF. This module finds, for every R, C and L in
a deck, the kit part -- or two in series/parallel -- that puts the *measured*
behaviour closest to the spec, by simulating candidate builds.

It searches jointly rather than rounding each part on its own, because parts
interact: in an inverting low-pass Rf sets both the gain and the corner, so the
best Rf depends on which capacitor you could get. Every build it reports was
simulated; none of the numbers are hand formulas.

    python kit.py reference/inv_active_lowpass.cir --spec specs_lib/inv_active_lowpass.yaml
    python kit.py deck.cir --spec s.yaml --kit my_kit.yaml --fix Rin --out kit_build.cir
    python kit.py deck.cir --spec s.yaml --singles-only

"Closest" is measured against the spec: a measurement's error is its distance
from the spec target (or, for a min/max band with no target, from what the
ideal deck measures), divided by the half-width of its acceptance band. So
1.0 means "at the edge of spec", and builds are ranked by the RMS of those.

Pure ngspice -- no API calls.
"""

import argparse
import bisect
import itertools
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml

import robustness
import specs as specfile
from plot import eng
from speccheck import spice_value

KIT_PATH = Path(__file__).with_name("kit.yaml")

# Standard IEC 60063 mantissas. E24 is not the geometric progression rounded:
# eight of its values were fixed long before the standard (see tolerances.yaml).
E_SERIES = {
    "E3": [1.0, 2.2, 4.7],
    "E6": [1.0, 1.5, 2.2, 3.3, 4.7, 6.8],
    "E12": [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2],
    "E24": [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
            3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1],
}

FAMILY_KEYS = {"r": "resistors", "c": "capacitors", "l": "inductors"}
UNITS = {"r": "", "c": "F", "l": "H"}   # 15k, 1.5nF: ASCII so a cp1252 console can print it

# A combined part is one more thing to wire and one more tolerance to stack.
# It costs this much score (2% of a tolerance band), so a pair only wins when
# it is genuinely closer, not when it ties a single part.
COMBO_PENALTY = 0.02

# Builds that cannot be simulated or measured rank below everything.
FAILED = math.inf


# --------------------------------------------------------------------- the kit

def series_values(name: str, lo: float, hi: float) -> list[float]:
    """Every value of an E-series between lo and hi, inclusive."""
    try:
        mantissas = E_SERIES[name.upper()]
    except KeyError:
        raise ValueError(f"unknown series {name!r}; use one of "
                         f"{', '.join(E_SERIES)}") from None
    out = []
    for d in range(math.floor(math.log10(lo)) - 1, math.ceil(math.log10(hi)) + 1):
        for m in mantissas:
            v = float(f"{m * 10.0 ** d:.6g}")      # 4.7 * 1e3 -> 4700.0, not 4700.000000001
            if lo * (1 - 1e-9) <= v <= hi * (1 + 1e-9):
                out.append(v)
    return sorted(set(out))


def _as_value(x) -> float:
    return float(x) if isinstance(x, (int, float)) else spice_value(str(x))


def _as_fraction(x) -> float:
    return specfile._as_fraction(x)


@dataclass
class Family:
    """One drawer: the values on hand and how far a part may be from its label."""
    letter: str
    values: list[float]
    tol: float

    def __bool__(self) -> bool:
        return bool(self.values)


@dataclass
class Kit:
    name: str
    families: dict = field(default_factory=dict)   # letter -> Family
    combine: tuple = ("series", "parallel")
    source: Path | None = None

    def family(self, letter: str) -> Family:
        return self.families.get(letter.lower()) or Family(letter.lower(), [], 0.0)

    def passive_tol(self) -> dict:
        """The {"r": .., "c": .., "l": ..} map robustness.monte_carlo takes."""
        return {k: f.tol for k, f in self.families.items()}


def parse_kit(data: dict, source: Path | None = None) -> Kit:
    families = {}
    for letter, key in FAMILY_KEYS.items():
        d = data.get(key) or {}
        values = [_as_value(v) for v in d.get("values", [])]
        if "series" in d:
            lo, hi = (_as_value(v) for v in d.get("range", [1, 1e6]))
            values += series_values(d["series"], lo, hi)
        tol = _as_fraction(d["tol"]) if "tol" in d else 0.05
        families[letter] = Family(letter, sorted(set(values)), tol)
    combine = tuple(data.get("combine", ("series", "parallel")))
    bad = set(combine) - {"series", "parallel"}
    if bad:
        raise ValueError(f"combine may list series/parallel, not {sorted(bad)}")
    return Kit(name=data.get("name", source.stem if source else "kit"),
               families=families, combine=combine, source=source)


def load_kit(path=None) -> Kit:
    path = Path(path) if path else KIT_PATH
    return parse_kit(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path)


# ------------------------------------------------------------------ candidates

def fmt(v: float, letter: str, sig: int = 3) -> str:
    return f"{eng(v, sig)}{UNITS.get(letter.lower(), '')}"


@dataclass(frozen=True)
class Choice:
    """What goes in one component's place on the breadboard."""
    value: float
    parts: tuple
    how: str            # single | series | parallel | ideal (not in the kit)

    @property
    def combo(self) -> bool:
        return len(self.parts) > 1

    def describe(self, letter: str) -> str:
        if self.how == "ideal":
            return f"{fmt(self.value, letter, 5)} (not in kit)"
        if not self.combo:
            return fmt(self.parts[0], letter)
        joiner = " + " if self.how == "series" else " || "
        return f"{joiner.join(fmt(p, letter) for p in self.parts)} ({self.how})"


def _adds_in(letter: str) -> str:
    """Which connection sums the values: series for R/L, parallel for C."""
    return "parallel" if letter.lower() == "c" else "series"


def _nearest(values: list[float], target: float) -> float:
    i = bisect.bisect_left(values, target)
    near = values[max(0, i - 1):i + 1]
    return min(near, key=lambda v: abs(v - target))


def _best_sum(values: list[float], target: float):
    """The pair a <= b from `values` whose sum is closest to target."""
    best = None
    for a in values:
        if a >= target:
            break
        b = _nearest(values, target - a)
        if b < a:
            continue
        if best is None or abs(a + b - target) < abs(sum(best) - target):
            best = (a, b)
    return best


def _best_recip(values: list[float], target: float):
    """The pair a <= b whose reciprocal sum ab/(a+b) is closest to target."""
    best, best_err = None, math.inf
    for a in values:
        if a <= target:
            continue
        want = 1.0 / (1.0 / target - 1.0 / a)
        b = _nearest(values, want)
        if b < a:
            continue
        err = abs(a * b / (a + b) - target)
        if err < best_err:
            best, best_err = (a, b), err
    return best


def candidates(target: float, letter: str, kit: Kit, *,
               singles_only: bool = False) -> list[Choice]:
    """The builds worth simulating for one component, closest first.

    The nearest kit value on each side, plus the best two-part combination of
    each allowed kind. A value the kit already holds exactly stays as it is:
    those are the handout's given parts, and they should not move.
    """
    fam = kit.family(letter)
    if not fam:
        return [Choice(target, (target,), "ideal")]
    vals = fam.values
    exact = [v for v in vals if abs(v - target) <= 1e-6 * target]
    if exact:
        return [Choice(exact[0], (exact[0],), "single")]

    out = []
    i = bisect.bisect_left(vals, target)
    for v in vals[max(0, i - 1):i + 1]:
        out.append(Choice(v, (v,), "single"))

    if not singles_only:
        adds = _adds_in(letter)
        recip = "series" if adds == "parallel" else "parallel"
        if adds in kit.combine:
            pair = _best_sum(vals, target)
            if pair:
                out.append(Choice(pair[0] + pair[1], pair, adds))
        if recip in kit.combine:
            pair = _best_recip(vals, target)
            if pair:
                a, b = pair
                out.append(Choice(a * b / (a + b), pair, recip))

    seen, uniq = set(), []
    for c in sorted(out, key=lambda c: (abs(c.value - target) / target, len(c.parts))):
        key = round(c.value / target, 9)
        if key not in seen:
            seen.add(key)
            uniq.append(c)
    return uniq


# --------------------------------------------------------------- the deck side

_SUBCKT_RE = re.compile(r"(?i)^\s*\.subckt\b")
_ENDS_RE = re.compile(r"(?i)^\s*\.ends\b")


def _subckt_mask(lines: list[str]) -> list[bool]:
    """True inside .subckt bodies: an op-amp model's internals are not kit parts."""
    mask, inside = [], False
    for ln in lines:
        if _SUBCKT_RE.match(ln):
            inside = True
        mask.append(inside)
        if _ENDS_RE.match(ln):
            inside = False
    return mask


def kit_params(netlist: str, fixed=()) -> list:
    """The R/C/L components a builder actually places, minus any held fixed."""
    lines, params = robustness.extract_params(netlist, vary_temp=False)
    inside = _subckt_mask(lines)
    held = {f.lower() for f in fixed}
    return [p for p in params
            if p.kind == "passive" and not inside[p.line] and p.key.lower() not in held]


@dataclass
class Build:
    choices: dict                 # refdes -> Choice
    values: dict | None = None    # measurement name -> value
    score: float = FAILED
    error: str | None = None

    @property
    def combos(self) -> int:
        return sum(c.combo for c in self.choices.values())

    @property
    def rank(self) -> float:
        return self.score + COMBO_PENALTY * self.combos


def build_deck(netlist: str, params: list, build: Build, kit: Kit | None = None) -> str:
    """The deck with kit values in place and a parts list as comments."""
    deck = robustness.apply_params(
        netlist, params, {p.key: build.choices[p.key].value for p in params})
    if not params:
        return deck
    note = [f"* Kit build ({kit.name if kit else 'kit'}), chosen by kit.py:"]
    for p in params:
        letter = p.key[0]
        note.append(f"*   {p.key} = {build.choices[p.key].describe(letter)}"
                    f"  [ideal {fmt(p.nominal, letter, 5)}]")
    lines = deck.splitlines()
    return "\n".join(lines[:1] + note + lines[1:]) + "\n"


# ------------------------------------------------------------------- scoring

def references(spec, ideal: dict) -> dict:
    """name -> (reference value, scale) for every pass/fail measurement.

    Reference is the spec target when there is one, else what the ideal deck
    measured. Scale is the half-width of the acceptance band, so an error of
    1.0 is a build sitting on the edge of spec.
    """
    out = {}
    for m in spec.specs:
        lim = m.limits
        ref = lim.target if lim.target is not None else ideal.get(m.name)
        if ref is None:
            continue
        if lim.lsl is not None and lim.usl is not None:
            scale = (lim.usl - lim.lsl) / 2
        else:
            scale = abs(ref) * 0.05
        out[m.name] = (ref, scale or 1.0)
    return out


def score(values: dict, refs: dict) -> float:
    errs = [((values[n] - ref) / scale) ** 2 for n, (ref, scale) in refs.items()
            if n in values]
    if len(errs) < len(refs):
        return FAILED
    return math.sqrt(sum(errs) / len(errs)) if errs else 0.0


def errors(values: dict, refs: dict) -> dict:
    """Per-measurement error as a fraction of the band half-width."""
    return {n: (values[n] - ref) / scale for n, (ref, scale) in refs.items()
            if n in values}


# -------------------------------------------------------------------- search

@dataclass
class Pick:
    spec: object
    netlist: str
    kit: Kit
    params: list
    ideal: dict
    refs: dict
    best: Build
    rounded: Build             # each part to its nearest kit value, on its own
    singles: Build | None      # best build with no combined parts
    sims: int = 0
    exhaustive: bool = True

    def deck(self, build: Build | None = None) -> str:
        return build_deck(self.netlist, self.params, build or self.best, self.kit)


class _Evaluator:
    """Simulates builds, once each, in parallel."""

    def __init__(self, spec, netlist, params, refs, workers):
        self.spec, self.netlist, self.params = spec, netlist, params
        self.refs, self.workers = refs, workers
        self.cache: dict = {}

    def _run(self, key):
        choices = dict(key)
        b = Build(choices)
        deck = robustness.apply_params(
            self.netlist, self.params, {k: c.value for k, c in choices.items()})
        try:
            b.values = specfile.measure(self.spec, deck)
            b.score = score(b.values, self.refs)
        except Exception as exc:          # noqa: BLE001 - a failed build is data
            b.error = f"{type(exc).__name__}: {exc}"
        return key, b

    def run(self, builds: list[dict]) -> list[Build]:
        keys = [tuple(sorted(b.items())) for b in builds]
        todo = list(dict.fromkeys(k for k in keys if k not in self.cache))
        if todo:
            if self.workers > 1 and len(todo) > 1:
                with ThreadPoolExecutor(max_workers=self.workers) as ex:
                    done = list(ex.map(self._run, todo))
            else:
                done = [self._run(k) for k in todo]
            self.cache.update(done)
        return [self.cache[k] for k in keys]


def _search(ev: _Evaluator, params: list, cands: dict, max_sims: int):
    """Exhaustive when it fits the budget, else coordinate descent."""
    keys = [p.key for p in params]
    space = math.prod(len(cands[k]) for k in keys)
    if space <= max_sims:
        builds = [dict(zip(keys, combo, strict=True))
                  for combo in itertools.product(*(cands[k] for k in keys))]
        return min(ev.run(builds), key=lambda b: b.rank), True

    # Start from every part at its closest candidate; then, a part at a time,
    # try each of its candidates with the rest held. Stops at a fixed point.
    current = {k: cands[k][0] for k in keys}
    best = ev.run([current])[0]
    for _ in range(6):
        improved = False
        for k in keys:
            trials = [{**current, k: c} for c in cands[k]]
            top = min(ev.run(trials), key=lambda b: b.rank)
            if top.rank < best.rank - 1e-12:
                best, current, improved = top, dict(top.choices), True
        if not improved:
            break
    return best, False


def pick(spec, netlist: str, kit: Kit | None = None, *, fixed=(),
         singles_only: bool = False, max_sims: int = 400, workers: int = 8) -> Pick:
    """Find the kit build closest to the spec. See the module docstring."""
    kit = kit or load_kit()
    ideal = specfile.measure(spec, netlist)       # raises if the ideal deck fails
    refs = references(spec, ideal)
    params = kit_params(netlist, fixed)
    ev = _Evaluator(spec, netlist, params, refs, workers)

    letter = {p.key: p.key[0] for p in params}
    singles = {p.key: candidates(p.nominal, letter[p.key], kit, singles_only=True)
               for p in params}
    rounded_choice = {k: min(c, key=lambda c, p=p: abs(c.value - p.nominal))
                      for p in params for k, c in [(p.key, singles[p.key])]}
    rounded = ev.run([rounded_choice])[0]

    best_singles, exh1 = _search(ev, params, singles, max_sims)
    if singles_only or not kit.combine:
        best, exh = best_singles, exh1
    else:
        full = {p.key: candidates(p.nominal, letter[p.key], kit) for p in params}
        best, exh2 = _search(ev, params, full, max_sims)
        exh = exh1 and exh2
        if best_singles.rank <= best.rank:
            best = best_singles

    return Pick(spec=spec, netlist=netlist, kit=kit, params=params, ideal=ideal,
                refs=refs, best=best, rounded=rounded,
                singles=None if singles_only else best_singles,
                sims=len(ev.cache), exhaustive=exh)


# ----------------------------------------------------------------------- CLI

def _val(v, units: str) -> str:
    return "-" if v is None else f"{v:.4g}{(' ' + units) if units else ''}"


def summary(p: Pick) -> str:
    rows = [f"Kit: {p.kit.name}",
            f"Simulated {p.sims} builds ({'exhaustive' if p.exhaustive else 'coordinate descent'}).",
            "", "Parts:"]
    if not p.params:
        rows.append("  (no R/C/L to choose -- every part is fixed or inside a model)")
    for prm in p.params:
        letter = prm.key[0]
        c = p.best.choices[prm.key]
        dev = (c.value - prm.nominal) / prm.nominal
        rows.append(f"  {prm.key:<6} ideal {fmt(prm.nominal, letter, 5):<10} "
                    f"-> {c.describe(letter):<28} ({dev:+.2%})")
    rows += ["", f"  {'measurement':<18}{'spec':>16}{'ideal':>14}{'rounded':>14}{'kit build':>14}"]
    for m in p.spec.specs:
        rows.append(
            f"  {m.title:<18}{m.limits.describe():>16}"
            f"{_val(p.ideal.get(m.name), ''):>14}"
            f"{_val((p.rounded.values or {}).get(m.name), ''):>14}"
            f"{_val((p.best.values or {}).get(m.name), ''):>14}")
    ok = p.best.values is not None and all(
        m.limits.ok(p.best.values[m.name]) for m in p.spec.specs)
    rows += ["", f"Kit build {'meets' if ok else 'does NOT meet'} spec "
                 f"(score {p.best.score:.3f}; rounding each part alone: {p.rounded.score:.3f})."]
    return "\n".join(rows)


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("deck", help="the ideal netlist (theory values)")
    ap.add_argument("--spec", required=True, help="spec YAML the build is measured against")
    ap.add_argument("--kit", help=f"kit YAML (default: {KIT_PATH.name})")
    ap.add_argument("--fix", action="append", default=[], metavar="REFDES",
                    help="keep this component's value as given (repeatable)")
    ap.add_argument("--singles-only", action="store_true",
                    help="no series/parallel combinations")
    ap.add_argument("--max-sims", type=int, default=400)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", help="write the kit-build deck here")
    a = ap.parse_args(argv)

    spec = specfile.load(a.spec)
    netlist = Path(a.deck).read_text(encoding="utf-8")
    p = pick(spec, netlist, load_kit(a.kit), fixed=a.fix,
             singles_only=a.singles_only, max_sims=a.max_sims, workers=a.workers)
    print(summary(p))
    if a.out:
        Path(a.out).write_text(p.deck(), encoding="utf-8")
        print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
