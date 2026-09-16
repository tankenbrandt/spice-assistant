#!/usr/bin/env python3
"""Sourced component tolerances, and the part profiles built from them.

`robustness.py` perturbed every resistor by a flat 5% and every capacitor by
10%. Those were sensible guesses. `tolerances.yaml` replaces them with
numbers from manufacturer datasheets, each carrying its citation and how far
it was verified, so a yield figure can be traced back to a real part.

    python tolerances.py                    # what is in the table
    python tolerances.py --profile precision
    python tolerances.py --gaps             # what could not be verified

The profiles are the point of the exercise. "Yield is 62% with commodity
parts, 94% with 1% thin film and C0G" is a purchasing decision; "yield is
62%" on its own is not.
"""

import argparse
import sys
from pathlib import Path

import yaml

TABLE = Path(__file__).resolve().parent / "tolerances.yaml"

# Which YAML family a netlist prefix belongs to.
FAMILY = {"r": "resistors", "c": "capacitors", "l": "inductors"}

_cache = None


def load(path=None) -> dict:
    """Read the table. Cached, because it is read per Monte Carlo sample."""
    global _cache
    if path is not None:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if _cache is None:
        _cache = yaml.safe_load(TABLE.read_text(encoding="utf-8"))
    return _cache


def _entry(family: str, part: str) -> dict:
    table = load()
    if family not in table:
        raise KeyError(f"no such family {family!r}; have: {', '.join(sorted(table))}")
    if part not in table[family]:
        raise KeyError(
            f"no {family} entry {part!r}; have: {', '.join(sorted(table[family]))}")
    return table[family][part]


def tolerance(family: str, part: str, pct: float | None = None) -> float:
    """The fractional tolerance of a part, e.g. 0.05 for a 5% resistor.

    Fractional, not percent, because that is what `robustness.Param.tol`
    expects -- handing it 5.0 would perturb the part by 500%.

    `pct` selects a grade other than the typical one, and must be a grade
    the part is actually sold in: modelling a 0.01% thick-film chip would
    produce a yield for something nobody can buy.
    """
    entry = _entry(family, part)
    grades = entry.get("tolerance_pct", [])
    if pct is None:
        pct = entry.get("typical_pct")
        if pct is None:
            raise KeyError(f"{family}.{part} states no typical tolerance")
    elif pct not in grades:
        raise ValueError(
            f"{part} is not sold at {pct}%; available grades: "
            + ", ".join(f"{g}%" for g in grades))
    return pct / 100.0


def tempco(family: str, part: str):
    """Temperature coefficient in ppm/C, or None where it is unverified.

    None is a real answer here. The film capacitor's tempco could not be
    confirmed from a primary source, so the table omits it rather than
    carrying a plausible number.
    """
    return _entry(family, part).get("tempco_ppm_per_C")


def profiles() -> dict:
    return load()["profiles"]


def profile(name: str) -> dict:
    """A whole BOM as fractional tolerances: {"r": .., "c": .., "l": ..}.

    Shaped to drop straight into `robustness.PASSIVE_TOL`.
    """
    table = load()
    if name not in table["profiles"]:
        raise KeyError(
            f"no profile {name!r}; have: {', '.join(sorted(table['profiles']))}")
    p = table["profiles"][name]
    return {
        "r": p["resistor_tolerance_pct"] / 100.0,
        "c": p["capacitor_tolerance_pct"] / 100.0,
        "l": p["inductor_tolerance_pct"] / 100.0,
    }


def describe(name: str) -> str:
    """One line naming the actual parts a profile stands for."""
    p = load()["profiles"][name]
    r, c = _entry("resistors", p["resistor"]), _entry("capacitors", p["capacitor"])
    return (f"{p['label']}: {r['label']} at {p['resistor_tolerance_pct']}%, "
            f"{c['label']} at {p['capacitor_tolerance_pct']}%, "
            f"inductors at {p['inductor_tolerance_pct']}%")


# ---------------------------------------------------------------------- CLI

def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="tolerances", description="Sourced component tolerances.")
    ap.add_argument("--profile", help="show one profile in detail")
    ap.add_argument("--gaps", action="store_true",
                    help="what the research could not verify")
    args = ap.parse_args(argv)
    table = load()

    if args.gaps:
        print("Not verified -- recorded rather than filled in:\n")
        for g in table["unverified"]:
            print(f"  - {' '.join(g.split())}")
        low = [f"{fam}.{n}" for fam in ("resistors", "capacitors", "inductors")
               for n, e in table[fam].items() if e["confidence"] != "primary"]
        print(f"\nEntries not read from a primary datasheet: {', '.join(low)}")
        return 0

    if args.profile:
        try:
            print(describe(args.profile))
        except KeyError as exc:
            print(exc, file=sys.stderr)
            return 2
        for k, v in profile(args.profile).items():
            print(f"  {k}: +-{v * 100:g}%")
        return 0

    for family in ("resistors", "capacitors", "inductors"):
        print(f"\n{family}:")
        for name, e in table[family].items():
            typ = e.get("typical_pct")
            tc = e.get("tempco_ppm_per_C")
            print(f"  {name:22s} +-{typ:<5g}% "
                  f"{('tempco ' + str(tc) + ' ppm/C') if tc else '':22s}"
                  f"[{e['confidence']}]")
    print("\nprofiles:")
    for name in table["profiles"]:
        print(f"  {name:12s} {describe(name)}")
    print("\n(python tolerances.py --gaps for what could not be verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
