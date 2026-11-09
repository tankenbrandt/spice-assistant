#!/usr/bin/env python3
"""Benchmark v2: feasible specs, as-built grading, a ceiling for every circuit.

Stage 1 (this file, free): load the suite, state its rules and tolerances to
the model, grade any deck as built, and validate that every circuit's
reference design clears the ceiling gate. No API calls.

    python bench2.py validate            # ceilings for every circuit -> logs/bench2/
    python bench2.py prompt rc_lowpass   # what each arm would be told
    python bench2.py grade rc_lowpass my.cir
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

import asbuilt
import benchmark
import robustness
import specs as specfile
import tolerances

PROJECT_DIR = Path(__file__).resolve().parent
SUITE_PATH = PROJECT_DIR / "bench2" / "suite.yaml"
DEVICES_PATH = PROJECT_DIR / "bench2" / "devices.yaml"
OUT_DIR = PROJECT_DIR / "logs" / "bench2"
GATE = 0.85                     # reference yield, gaussian, that admits a circuit
MC_N, MC_SEED = 500, 1
DISTS = ("gaussian", "uniform")
_LETTERS = {"r": "resistors", "c": "capacitors", "l": "inductors", "d": "diodes",
            "q": "BJTs", "m": "MOSFETs", "v": "voltage sources", "x": "subcircuits",
            "e": "voltage-controlled voltage sources (op-amp model only)",
            "g": "voltage-controlled current sources (op-amp model only)",
            "s": "voltage-controlled switches"}


def load_suite(path: Path = SUITE_PATH) -> list:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["circuits"]


def load_devices(path: Path = DEVICES_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def models_for(e: dict) -> dict:
    """Device types the grader replaces with real parts (default: BJTs and diodes)."""
    allm = load_devices()["models"]
    return {t: allm[t] for t in e.get("models", ["npn", "d"])}


def subckts_for(e: dict) -> dict:
    alls = load_devices()["subckts"]
    return {n: alls[n] for n in e.get("subckts", [])}


def entry(cid: str, suite: list | None = None) -> dict:
    for e in suite or load_suite():
        if e["id"] == cid:
            return e
    raise KeyError(f"no circuit {cid!r} in {SUITE_PATH.name}")


def tol_for(e: dict) -> dict:
    t = tolerances.profile(e["profile"])
    t.update(e.get("tol") or {})
    return t


def supply_tol(e: dict) -> float:
    return e.get("supply_tol", robustness.SUPPLY_TOL)


def allowed(e: dict) -> frozenset:
    return asbuilt.BASE_ELEMENTS | frozenset(e.get("allow") or [])


def spec_for(e: dict):
    return specfile.load(PROJECT_DIR / e["spec"])


def task(e: dict) -> str:
    pool = benchmark.CIRCUITS + benchmark.CIRCUITS_ROUND2 + benchmark.CIRCUITS_LAB
    return next(c for c in pool if c["id"] == e["prompt_from"])["description"]


# ------------------------------------------------------------------ prompts

def rules_text(e: dict) -> str:
    """Stated in every arm: the grader enforces these, so the model is told."""
    t = tol_for(e)
    parts = ", ".join(_LETTERS[x] for x in sorted(allowed(e)))
    n = e.get("supplies", 1)
    return (
        "Design rules (enforced by the grader):\n"
        f"- Use only: {parts}. No ideal current sources.\n"
        f"- At most {n} DC supply source{'s' if n != 1 else ''}; the input source carries no DC.\n"
        f"- Every part is rounded to the nearest value you can buy before testing: "
        f"{asbuilt.series_for('r', t['r'])} resistors, {asbuilt.series_for('c', t['c'])} "
        f"capacitors, {asbuilt.series_for('l', t['l'])} inductors.\n"
        "- Parts in series or parallel are tested as the single part they make up.\n"
        "- Component values are plain numbers (no .param or {expressions}); no .subckt "
        "definitions."
        + "".join(f"\n- Every {kind.upper()} .model card is replaced by the grader's "
                  f"{m['part']}: {kind.upper()}({' '.join(m['params'].split())}). Include a "
                  ".model card for it; its parameters will be overwritten."
                  for kind, m in models_for(e).items())
        + "".join(f"\n- The grader supplies .subckt {name} (pins: {s['pins']}). Call it; "
                  "don't define it." for name, s in subckts_for(e).items())
    )


def tolerance_text(e: dict, n: int = MC_N) -> str:
    """Stated only in the informed arm: how robustness is scored."""
    t, m = tol_for(e), robustness.MODEL_TOL
    lo, hi = robustness.TEMP_RANGE

    def pct(x):
        return f"+/-{x * 100:g}%"
    return (
        f"Robustness test: after the nominal check the design is re-simulated as {n} "
        f"Monte Carlo builds. Each build varies every resistor {pct(t['r'])}, capacitor "
        f"{pct(t['c'])}, inductor {pct(t['l'])} and DC supply {pct(supply_tol(e))} "
        "(Gaussian, 3 sigma at those limits, each part independently); BJT BF "
        f"{pct(m[('npn', 'bf')])}, IS {pct(m[('npn', 'is')])}, VAF {pct(m[('npn', 'vaf')])}; "
        f"MOSFET VTO {pct(m[('nmos', 'vto')])}, KP {pct(m[('nmos', 'kp')])}; diode IS "
        f"{pct(m[('d', 'is')])}, N {pct(m[('d', 'n')])}; temperature uniform {lo:g} to "
        f"{hi:g} C. A build passes only if every acceptance criterion holds. Your design "
        "is scored on the fraction of builds that pass."
    )


# ------------------------------------------------------------------ grading

def grade(e: dict, deck: str, n: int = MC_N, dists=DISTS, seed: int = MC_SEED,
          workers: int = 8, models: bool = True, snap: bool = True) -> dict:
    """As-built nominal check, then Monte Carlo. Zero yield if it breaks a rule
    or misses at nominal. models=False keeps the deck's own device cards (for
    re-grading decks whose prompt never said they would be replaced)."""
    spec = spec_for(e)
    tol, st = tol_for(e), supply_tol(e)
    prep = asbuilt.prepare(deck, spec, tol, allowed(e), e.get("supplies", 1),
                           models=models_for(e) if models else None,
                           subckts=subckts_for(e), snap=snap)
    out = {"violations": prep["violations"], "merges": prep["merges"],
           "snaps": prep["snaps"], "models": prep["models"],
           "nominal": None, "nominal_pass": False,
           "yield": {d: 0.0 for d in dists}}
    if prep["violations"]:
        return out
    _, limits = robustness.resolve(spec)
    nom = robustness.nominal(spec, prep["deck"], passive_tol=tol, supply_tol=st)
    out["nominal"] = nom.get("metrics")
    out["nominal_error"] = nom.get("error")
    out["nominal_pass"] = robustness.summarize([nom], limits)["yield"] == 1.0
    if not out["nominal_pass"]:
        return out
    for d in dists:
        mc = robustness.monte_carlo(spec, prep["deck"], n, seed=seed, dist=d,
                                    passive_tol=tol, supply_tol=st, workers=workers)
        out["yield"][d] = mc["yield"]
        out.setdefault("sim_fail", {})[d] = mc["n_sim_fail"]
    return out


def validate(n: int = MC_N, only: str | None = None, log=print) -> dict:
    """Grade every reference design; a circuit is admitted if it clears GATE."""
    results = {}
    for e in load_suite():
        if (only and e["id"] != only) or e.get("excluded"):
            continue
        deck = (PROJECT_DIR / e["reference"]).read_text(encoding="utf-8")
        g = grade(e, deck, n)
        g["admitted"] = g["yield"]["gaussian"] >= GATE
        results[e["id"]] = g
        log(f"{e['id']:22s} nominal {'pass' if g['nominal_pass'] else 'FAIL'}  "
            f"gaussian {g['yield']['gaussian']:.1%}  uniform {g['yield']['uniform']:.1%}  "
            f"{'admitted' if g['admitted'] else 'REJECTED'}"
            + (f"  violations: {g['violations']}" if g["violations"] else ""))
    return results


def render(results: dict) -> str:
    lines = ["# Benchmark v2 ceilings", "",
             f"Each circuit's reference design, graded as built, {MC_N} Monte Carlo builds "
             f"(seed {MC_SEED}). Admitted if the gaussian yield is at least {GATE:.0%}. "
             "Definitions and reasons: `bench2/suite.yaml`.", "",
             "| Circuit | Parts | Nominal | Gaussian | Uniform | Admitted |",
             "|---|---|---|---|---|---|"]
    suite = {e["id"]: e for e in load_suite()}
    for cid, g in results.items():
        e = suite[cid]
        parts = e["profile"] + (f" + {e['tol']}" if e.get("tol") else "") + (
            f", supply ±{supply_tol(e):.0%}" if "supply_tol" in e else "")
        lines.append(f"| {cid} | {parts} | {'pass' if g['nominal_pass'] else 'fail'} | "
                     f"{g['yield']['gaussian']:.1%} | {g['yield']['uniform']:.1%} | "
                     f"{'yes' if g['admitted'] else '**no**'} |")
    return "\n".join(lines) + "\n"


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--n", type=int, default=MC_N)
    v.add_argument("--only")
    p = sub.add_parser("prompt")
    p.add_argument("circuit")
    g = sub.add_parser("grade")
    g.add_argument("circuit")
    g.add_argument("deck", type=Path)
    g.add_argument("--n", type=int, default=MC_N)
    a = ap.parse_args(argv)
    if a.cmd == "prompt":
        e = entry(a.circuit)
        print(f"{task(e)}\n\n{rules_text(e)}\n\n[informed arm adds]\n{tolerance_text(e)}")
        return 0
    if a.cmd == "grade":
        print(json.dumps(grade(entry(a.circuit), a.deck.read_text(encoding="utf-8"), a.n),
                         indent=1))
        return 0
    results = validate(a.n, a.only)
    if not a.only:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "ceilings.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
        md = render(results)
        (OUT_DIR / "ceilings.md").write_text(md, encoding="utf-8")
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
