#!/usr/bin/env python3
"""Measure one deck in two independent simulators and compare.

`speccheck` answers "does this circuit meet its spec?" with ngspice. That is
one program's opinion. This asks the same spec of LTspice -- a separately
written, closed-source simulator that most analog designers already have --
and reports where the two agree.

Agreement is the useful signal in both directions:

* **They agree.** The measurement is a property of the circuit, not of
  ngspice. On this library that is the common case, often to six figures.
* **They disagree.** Something is genuinely simulator-dependent and worth
  knowing about before trusting the number -- switching-converter ripple
  depends on timestep control, and an `argmax` over a flat response picks a
  different point in each program without the physics differing at all.

    python crosscheck.py repaired/ce_bjt_amp.cir --spec specs_lib/ce_bjt_amp.yaml
    python crosscheck.py --all                 # every spec with a deck

Exit status is 1 if a spec measurement disagrees by more than `--tol`.
Informational measurements are reported but never fail the run.
"""

import argparse
import glob
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import ltspice
import specs

DEFAULT_TOL = 1.0     # percent


@dataclass
class Row:
    name: str
    ngspice: float
    ltspice: float
    informational: bool

    @property
    def diff_pct(self) -> float:
        """Relative difference, against the larger magnitude.

        Using the larger of the two as the denominator keeps the number
        symmetric and bounded at 200%, so one simulator reading ~0 cannot
        produce a meaningless six-figure percentage.
        """
        scale = max(abs(self.ngspice), abs(self.ltspice))
        if scale < 1e-12:
            return 0.0
        return abs(self.ngspice - self.ltspice) / scale * 100.0

    def agrees(self, tol: float) -> bool:
        return self.diff_pct <= tol


def compare(spec, netlist: str, exe=None) -> list:
    """Measure `netlist` against `spec` in both simulators."""
    ng = specs.measure(spec, netlist)
    lt = specs.measure(spec, netlist, runner=lambda n, a: ltspice.run_analysis(n, a, exe=exe))
    info = {m.name: m.informational for m in spec.measurements}
    return [Row(k, ng[k], lt[k], info.get(k, False)) for k in ng if k in lt]


def find_deck(name: str):
    """The deck a spec applies to: the repaired one if there is one."""
    for p in (f"repaired/{name}.cir", f"repaired/round2/{name}.cir",
              f"baseline/{name}.cir", f"baseline/round2/{name}.cir"):
        if os.path.exists(p):
            return Path(p)
    return None


def report(title: str, rows: list, tol: float) -> tuple[str, bool]:
    """Format one circuit's comparison. Returns (text, all spec rows agree)."""
    lines = [f"{title}"]
    ok = True
    for r in rows:
        mark = "ok " if r.agrees(tol) else ("--" if r.informational else "DIFF")
        if not r.agrees(tol) and not r.informational:
            ok = False
        tag = " (informational)" if r.informational else ""
        lines.append(
            f"  {mark:<4} {r.name:<12} ngspice={r.ngspice:<13.6g} "
            f"ltspice={r.ltspice:<13.6g} {r.diff_pct:7.3f}%{tag}")
    return "\n".join(lines), ok


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="crosscheck",
        description="Measure a deck in ngspice and LTspice and compare.")
    ap.add_argument("deck", nargs="?", help="netlist file")
    ap.add_argument("--spec", help="spec YAML (default: specs_lib/<deck stem>.yaml)")
    ap.add_argument("--all", action="store_true",
                    help="every spec in specs_lib/ that has a deck")
    ap.add_argument("--tol", type=float, default=DEFAULT_TOL,
                    help=f"percent disagreement allowed (default {DEFAULT_TOL})")
    ap.add_argument("--exe", help="path to LTspice.exe")
    args = ap.parse_args(argv)

    if ltspice.find_executable(args.exe) is None:
        print("LTspice not found. Set LTSPICE_EXE or pass --exe.", file=sys.stderr)
        return 2

    jobs = []
    if args.all:
        for y in sorted(glob.glob("specs_lib/*.yaml")):
            deck = find_deck(Path(y).stem)
            if deck:
                jobs.append((Path(y), deck))
    elif args.deck:
        deck = Path(args.deck)
        spec = Path(args.spec) if args.spec else Path("specs_lib") / f"{deck.stem}.yaml"
        if not spec.exists():
            print(f"no spec at {spec}; pass --spec", file=sys.stderr)
            return 2
        jobs.append((spec, deck))
    else:
        ap.error("give a deck or --all")

    agreed = disagreed = failed = 0
    for spec_path, deck in jobs:
        spec = specs.load(spec_path)
        netlist = deck.read_text(encoding="utf-8", errors="replace")
        try:
            rows = compare(spec, netlist, exe=args.exe)
        except Exception as exc:
            print(f"{deck}\n  ERROR {type(exc).__name__}: {str(exc)[:200]}")
            failed += 1
            continue
        text, ok = report(f"{deck}  (spec: {spec_path.name})", rows, args.tol)
        print(text)
        agreed += ok
        disagreed += (not ok)

    total = agreed + disagreed + failed
    print(f"\n{agreed}/{total} circuits agree within {args.tol}% "
          f"on every spec measurement" + (f"; {failed} errored" if failed else ""))
    return 1 if (disagreed or failed) else 0


if __name__ == "__main__":
    raise SystemExit(_cli())
