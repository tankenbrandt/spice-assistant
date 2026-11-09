#!/usr/bin/env python3
"""Controlled A/B: does telling the model how it is graded change yield?

The benchmark asks for a CE amplifier with a gain of "approximately -50",
then grades it against 47.5-52.5 V/V and a 500-build Monte Carlo over part
tolerances, beta, supply and temperature -- none of which the prompt states.
This experiment holds everything fixed except what the prompt tells the model.

PROTOCOL (fixed before any API call; the commit that adds this file is the
pre-registration):

  Circuit   ce_bjt_amp, the benchmark's own description.
  Arms      A0  today's prompt, unchanged.
            A1  A0 + the acceptance window (design.spec_brief, unchanged).
            A2  A1 + the tolerance conditions and the yield scoring rule,
                generated from robustness.py's own constants.
  Models    claude-sonnet-5-5, claude-opus-5-5.
  Runs      N per arm per model, interleaved (run i: every model x every arm)
            so drift over the session cannot pose as an arm effect.
  Held      system prompt, benchmark.run_circuit (up to 4 simulator-error
  fixed     fixes), spec repair OFF -- repair would feed measurements back.
  Metric    yield = share of 500 Monte Carlo builds (seed 1) inside the window.
            Intention to treat: a deck that never simulates or misses the
            window at nominal scores 0.
            Primary: gaussian draws (3 sigma at the limit), as in every
            published yield and as A2's prompt states. Secondary: uniform.
  Contrasts A2 - A0 (primary), A1 - A0 (knowing the target), A2 - A1
            (knowing the tolerances). Bootstrap 95% CIs, 10k resamples.
  Ceiling   the same Monte Carlo on examples/ce_bjt_amp_recentred.cir;
            every arm is also reported as a fraction of it.
  Flag      resistor count per deck: splitting a resistor into a series pair
            averages independent tolerances, which real same-reel parts may
            not do.
  Budget    every API call is refused unless spend + that call's worst case
            (16k output tokens) stays under --cap.

    python ab_tolerance.py run --n 10 --cap 2.00      # metered API
    python ab_tolerance.py report                     # free, from the log
    python ab_tolerance.py run --dry-run --n 1 --mc 40 --out <tmp>   # free
"""

import argparse
import json
import random
import re
import statistics
import sys
from pathlib import Path

import benchmark
import design
import main
import robustness
import specs as specfile
import speccheck

PROJECT_DIR = Path(__file__).resolve().parent
CIRCUIT = "ce_bjt_amp"
MODELS = ("claude-sonnet-5-5", "claude-opus-5-5")
ARMS = ("A0", "A1", "A2")
MC_N, MC_SEED = 500, 1
DISTS = ("gaussian", "uniform")
CEILING_DECK = PROJECT_DIR / "examples" / "ce_bjt_amp_recentred.cir"
DEFAULT_OUT = PROJECT_DIR / "logs" / "ab_tolerance"
MAX_OUTPUT_TOKENS = 16000     # main._message_text's max_tokens
WORST_INPUT_TOKENS = 6000     # longest fix prompt: a deck plus ngspice errors


# ------------------------------------------------------------------ prompts

def _pct(x: float) -> str:
    return f"+/-{x * 100:g}%"


def tolerance_brief(n: int = MC_N) -> str:
    """A2's addition, built from the constants the grader actually uses."""
    _, specs = robustness.resolve(CIRCUIT)
    g = specs["gain"]
    t_lo, t_hi = robustness.TEMP_RANGE
    p = robustness.PASSIVE_TOL
    m = robustness.MODEL_TOL
    return (
        f"Robustness test: after the nominal check, the deck is re-simulated as "
        f"{n} Monte Carlo builds. In each build every resistor varies {_pct(p['r'])}, "
        f"every capacitor {_pct(p['c'])} and the supply {_pct(robustness.SUPPLY_TOL)} "
        f"(Gaussian, 3 sigma at those limits); NPN transistor BF varies "
        f"{_pct(m[('npn', 'bf')])}, IS {_pct(m[('npn', 'is')])} and VAF "
        f"{_pct(m[('npn', 'vaf')])}; the temperature is uniform from {t_lo:g} to "
        f"{t_hi:g} C. A build passes only if |gain| stays within "
        f"{g.lsl:g}-{g.usl:g} V/V. Your design is scored on the fraction of "
        f"builds that pass."
    )


def arm_prompts() -> dict:
    base = next(c for c in benchmark.CIRCUITS if c["id"] == CIRCUIT)["description"]
    spec = specfile.load(PROJECT_DIR / "specs_lib" / f"{CIRCUIT}.yaml")
    a1 = base + "\n\n" + design.spec_brief(spec)
    return {"A0": base, "A1": a1, "A2": a1 + "\n\n" + tolerance_brief()}


# ------------------------------------------------------------------ budget

class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """Refuses any API call whose worst case would cross the cap."""

    def __init__(self, cap: float, spent: float = 0.0):
        self.cap, self.spent = cap, spent

    @staticmethod
    def worst_call(model: str) -> float:
        pin, pout = main.prices(model)
        return (WORST_INPUT_TOKENS * pin + MAX_OUTPUT_TOKENS * pout) / 1e6

    def guard(self, call):
        def guarded(*a, **k):
            model = main.MODEL
            if self.spent + self.worst_call(model) > self.cap:
                raise BudgetExceeded(f"${self.spent:.3f} spent; next call could pass ${self.cap:.2f}")
            before = dict(main.USAGE)
            try:
                return call(*a, **k)
            finally:
                pin, pout = main.prices(model)
                self.spent += ((main.USAGE["input_tokens"] - before["input_tokens"]) * pin
                               + (main.USAGE["output_tokens"] - before["output_tokens"]) * pout) / 1e6
        return guarded


# ------------------------------------------------------------------ grading

def resistor_count(deck: str) -> int:
    return sum(1 for ln in deck.splitlines() if re.match(r"(?i)^\s*r\w*\s", ln))


def grade(deck: str | None, mc_n: int = MC_N) -> dict:
    """Nominal check, then Monte Carlo under each distribution. ITT: 0 if no deck."""
    if not deck:
        return {"nominal_pass": False, "gain": None, "resistors": None,
                "yield": {d: 0.0 for d in DISTS}}
    sc = speccheck.check(CIRCUIT, deck)
    out = {"nominal_pass": bool(sc.get("pass")), "gain": sc.get("measured"),
           "resistors": resistor_count(deck), "yield": {}}
    for d in DISTS:
        if not out["nominal_pass"]:
            out["yield"][d] = 0.0
            continue
        mc = robustness.monte_carlo(CIRCUIT, deck, mc_n, seed=MC_SEED, dist=d)
        out["yield"][d] = mc["yield"]
        out.setdefault("gain_mean", {})[d] = mc["metrics"].get("gain", {}).get("mean")
    return out


def ceiling(mc_n: int = MC_N) -> dict:
    deck = CEILING_DECK.read_text(encoding="utf-8")
    return {d: robustness.monte_carlo(CIRCUIT, deck, mc_n, seed=MC_SEED, dist=d)["yield"]
            for d in DISTS}


# ------------------------------------------------------------------ statistics

def bootstrap_mean_ci(xs: list, n: int = 10000, seed: int = 0) -> tuple:
    if not xs:
        return (None, None)
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return (means[int(0.025 * n)], means[int(0.975 * n) - 1])


def bootstrap_diff_ci(a: list, b: list, n: int = 10000, seed: int = 0) -> tuple:
    """95% CI on mean(a) - mean(b), groups resampled independently."""
    if not a or not b:
        return (None, None)
    rng = random.Random(seed)
    diffs = sorted(statistics.fmean(rng.choices(a, k=len(a)))
                   - statistics.fmean(rng.choices(b, k=len(b))) for _ in range(n))
    return (diffs[int(0.025 * n)], diffs[int(0.975 * n) - 1])


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / den
    return (max(0.0, mid - half), min(1.0, mid + half))


def summarize(trials: list, ceil: dict) -> dict:
    out = {"ceiling": ceil, "groups": {}, "contrasts": {}}
    scopes = {"pooled": list(MODELS), **{m: [m] for m in MODELS}}
    for scope, models in scopes.items():
        for d in DISTS:
            ys = {arm: [t["grade"]["yield"][d] for t in trials
                        if t["arm"] == arm and t["model"] in models]
                  for arm in ARMS}
            for arm in ARMS:
                arm_trials = [t for t in trials if t["arm"] == arm and t["model"] in models]
                y = ys[arm]
                hi = sum(v >= 0.9 for v in y)
                nom = sum(t["grade"]["nominal_pass"] for t in arm_trials)
                out["groups"][f"{scope}|{d}|{arm}"] = {
                    "n": len(y),
                    "mean_yield": statistics.fmean(y) if y else None,
                    "ci": bootstrap_mean_ci(y),
                    "of_ceiling": (statistics.fmean(y) / ceil[d]) if y and ceil.get(d) else None,
                    "share_ge_90": (hi / len(y)) if y else None,
                    "share_ge_90_ci": wilson(hi, len(y)),
                    "nominal_pass": nom, "nominal_ci": wilson(nom, len(arm_trials)),
                    "split_resistors": sum((t["grade"].get("resistors") or 0) > 6
                                           for t in arm_trials),
                }
            for a, b in (("A2", "A0"), ("A1", "A0"), ("A2", "A1")):
                if ys[a] and ys[b]:
                    out["contrasts"][f"{scope}|{d}|{a}-{b}"] = {
                        "diff": statistics.fmean(ys[a]) - statistics.fmean(ys[b]),
                        "ci": bootstrap_diff_ci(ys[a], ys[b]),
                    }
    return out


def _f(x, pct=True):
    if x is None:
        return "–"
    return f"{x * 100:.0f}%" if pct else f"{x:.2f}"


def render(summary: dict, meta: dict) -> str:
    c = summary["ceiling"]
    lines = [
        "# A/B: does telling the model how it is graded change yield?", "",
        f"Circuit `{CIRCUIT}`; {MC_N} Monte Carlo builds per deck, seed {MC_SEED}. "
        f"API spend ${meta.get('spent', 0):.2f} of a ${meta.get('cap', 0):.2f} cap. "
        "Protocol: the docstring of `ab_tolerance.py`.", "",
        f"Ceiling (hand-recentred reference deck): gaussian {_f(c.get('gaussian'))}, "
        f"uniform {_f(c.get('uniform'))}.", "",
    ]
    for d in DISTS:
        lines += [f"## Yield, {d} draws" + (" (primary)" if d == "gaussian" else ""), "",
                  "| Scope | Arm | n | Nominal pass | Mean yield (95% CI) | Of ceiling | Yield ≥ 90% | >6 resistors |",
                  "|---|---|---|---|---|---|---|---|"]
        for scope in ("pooled", *MODELS):
            for arm in ARMS:
                g = summary["groups"].get(f"{scope}|{d}|{arm}")
                if not g or not g["n"]:
                    continue
                lo, hi = g["ci"]
                lines.append(
                    f"| {scope} | {arm} | {g['n']} | {g['nominal_pass']}/{g['n']} | "
                    f"{_f(g['mean_yield'])} ({_f(lo)}–{_f(hi)}) | {_f(g['of_ceiling'])} | "
                    f"{_f(g['share_ge_90'])} | {g['split_resistors']} |")
        lines += ["", "| Scope | Contrast | Difference (95% CI) |", "|---|---|---|"]
        for key, v in summary["contrasts"].items():
            scope, dist, name = key.split("|")
            if dist == d:
                lo, hi = v["ci"]
                lines.append(f"| {scope} | {name} | {v['diff'] * 100:+.0f} pts "
                             f"({lo * 100:+.0f} to {hi * 100:+.0f}) |")
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ running

def _load(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"trials": [], "spent": 0.0}


def _save(path: Path, log: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=1), encoding="utf-8")


def run(n: int, cap: float, out: Path, mc_n: int = MC_N, dry_run: bool = False,
        log=print) -> dict:
    path = out / "trials.json"
    state = _load(path)
    state["cap"] = cap
    done = {(t["model"], t["arm"], t["i"]) for t in state["trials"]}
    prompts = arm_prompts()
    budget = Budget(cap, state.get("spent", 0.0))
    real = main._message_text
    if dry_run:   # free end-to-end check: the "model" returns the reference deck
        ref = CEILING_DECK.read_text(encoding="utf-8")
        main._message_text = budget.guard(lambda *a, **k: ref)
    else:
        main._message_text = budget.guard(real)
    try:
        for i in range(n):
            for model in MODELS:
                for arm in ARMS:
                    if (model, arm, i) in done:
                        continue
                    main.MODEL = model
                    log(f"[{i + 1}/{n}] {model} {arm}  (spent ${budget.spent:.3f})")
                    rec = benchmark.run_circuit(
                        {"id": CIRCUIT, "tier": "medium", "description": prompts[arm]})
                    err = rec.get("harness_error")
                    if err and "BudgetExceeded" in err:
                        log(f"budget stop: {err}")
                        return state
                    if err:
                        # API/transport failures aren't the model's design: log
                        # them apart from the trials, and retry them on a rerun.
                        state.setdefault("errors", []).append(
                            {"model": model, "arm": arm, "i": i, "error": err})
                        state["spent"] = budget.spent
                        _save(path, state)
                        log(f"    harness error, not scored: {err}")
                        continue
                    deck = rec["attempts"][-1]["netlist"] if rec["success"] else None
                    state["trials"].append({
                        "model": model, "arm": arm, "i": i,
                        "attempts": rec["attempts_used"], "simulated": rec["success"],
                        "harness_error": rec.get("harness_error"), "deck": deck,
                        "grade": grade(deck, mc_n),
                    })
                    state["spent"] = budget.spent
                    _save(path, state)
                    g = state["trials"][-1]["grade"]
                    log(f"    nominal {g['gain']}  yield {g['yield']}")
    finally:
        main._message_text = real
        state["spent"] = budget.spent
        _save(path, state)
    return state


# ------------------------------------------------------------------ post hoc

# NOT pre-registered. Added after the run, when every Opus A2 deck turned out
# to split its resistors into equal series pieces. The pre-registered grader
# varies each resistor independently, so splitting one into k pieces divides
# its spread by about sqrt(k). Resistors of one value usually come off one
# reel and drift together, so this grader draws ONE factor per resistor value:
# a split chain varies like the single part it replaces.

def ideal_current_sources(deck: str) -> int:
    """Independent current sources: the grader never varies them."""
    return sum(1 for ln in deck.splitlines() if re.match(r"(?i)^\s*i\w*\s", ln))


def reel_monte_carlo(deck: str, n: int = MC_N, dist: str = "gaussian",
                     seed: int = MC_SEED, workers: int = 8) -> float:
    metric_fn, specs = robustness.resolve(CIRCUIT)
    _, params = robustness.extract_params(deck, vary_temp=True)
    groups = {}
    for p in params:
        if p.kind == "passive" and p.key.lower().startswith("r"):
            groups.setdefault(round(p.nominal, 9), []).append(p)
    unit = {v: robustness.Param(f"reel:{v}", "passive", 1.0, ps[0].tol)
            for v, ps in groups.items()}
    rng = random.Random(seed)
    draws = []
    for _ in range(n):
        f = {v: u.sample(rng, dist) for v, u in unit.items()}
        draw = {}
        for p in params:
            if p.kind == "passive" and p.key.lower().startswith("r"):
                draw[p.key] = p.nominal * f[round(p.nominal, 9)]
            else:
                draw[p.key] = p.sample(rng, dist)
        draws.append(draw)
    samples = robustness._map(
        lambda v: robustness._evaluate(metric_fn, deck, params, v), draws, workers)
    return robustness.summarize(samples, specs)["yield"]


def reel_regrade(out: Path, mc_n: int = MC_N) -> str:
    state = _load(out / "trials.json")
    rows, by = [], {}
    ref = CEILING_DECK.read_text(encoding="utf-8")
    ceil = {d: reel_monte_carlo(ref, mc_n, d) for d in DISTS}
    for t in state["trials"]:
        g, deck = t["grade"], t["deck"]
        if g["nominal_pass"]:
            y = {d: reel_monte_carlo(deck, mc_n, d) for d in DISTS}
        else:
            y = {d: 0.0 for d in DISTS}
        isrc = ideal_current_sources(deck) if deck else 0
        rows.append({"model": t["model"], "arm": t["arm"], "i": t["i"],
                     "independent": g["yield"], "reel": y,
                     "resistors": g["resistors"], "ideal_isrc": isrc})
        by.setdefault((t["model"], t["arm"]), []).append(rows[-1])
    (out / "reel_regrade.json").write_text(json.dumps({"ceiling": ceil, "rows": rows}, indent=1),
                                           encoding="utf-8")
    lines = ["## Post hoc: same-value resistors drift together (not pre-registered)", "",
             f"Reference deck: gaussian {_f(ceil['gaussian'])}, uniform {_f(ceil['uniform'])}.", "",
             "| Model | Arm | n | Gaussian: independent → same-reel | Uniform: independent → same-reel "
             "| Decks with ideal current sources |", "|---|---|---|---|---|---|"]
    for (model, arm), rs in sorted(by.items()):
        gi = statistics.fmean(r["independent"]["gaussian"] for r in rs)
        gr = statistics.fmean(r["reel"]["gaussian"] for r in rs)
        ui = statistics.fmean(r["independent"]["uniform"] for r in rs)
        ur = statistics.fmean(r["reel"]["uniform"] for r in rs)
        lines.append(f"| {model} | {arm} | {len(rs)} | {_f(gi)} → {_f(gr)} | {_f(ui)} → {_f(ur)} "
                     f"| {sum(r['ideal_isrc'] > 0 for r in rs)} |")
    md = "\n".join(lines) + "\n"
    with (out / "report.md").open("a", encoding="utf-8") as fh:
        fh.write("\n" + md)
    return md


def report(out: Path, mc_n: int = MC_N) -> str:
    state = _load(out / "trials.json")
    summary = summarize(state["trials"], ceiling(mc_n))
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    md = render(summary, state)
    (out / "report.md").write_text(md, encoding="utf-8")
    return md


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--n", type=int, default=10, help="runs per arm per model")
    r.add_argument("--cap", type=float, default=2.00, help="hard API spend cap, $")
    r.add_argument("--dry-run", action="store_true", help="no API: the model returns the reference deck")
    for p in (r, sub.add_parser("report"), sub.add_parser("prompts")):
        p.add_argument("--out", type=Path, default=DEFAULT_OUT)
        p.add_argument("--mc", type=int, default=MC_N)
    a = ap.parse_args(argv)
    if a.cmd == "prompts":
        for arm, text in arm_prompts().items():
            print(f"===== {arm} =====\n{text}\n")
        return 0
    if a.cmd == "run":
        main.load_dotenv()
        state = run(a.n, a.cap, a.out, a.mc, a.dry_run)
        print(f"{len(state['trials'])} trials logged, ${state['spent']:.3f} spent")
    print(report(a.out, a.mc))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
