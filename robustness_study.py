#!/usr/bin/env python3
"""Robustness study driver: before/after signoff on the benchmark's decks.

Pairs each deck that the spec layer originally FAILED with the repaired deck
that PASSED, and asks a question the nominal spec check cannot: does the repair
hold up once the parts, the supply, the transistor and the temperature are
allowed to be what they actually are?

Writes robustness_log.json (full evidence) and ROBUSTNESS.md (the report).

Usage:
    python robustness_study.py                  # full study, N=500
    python robustness_study.py --n 200
    python robustness_study.py --circuit ce_bjt_amp
    python robustness_study.py --report-only    # rebuild the report from the log

Pure ngspice. No API calls, no cost.
"""

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import main
import robustness as rb

PROJECT_DIR = Path(__file__).resolve().parent
LOG_PATH = PROJECT_DIR / "robustness_log.json"
REPORT_PATH = PROJECT_DIR / "ROBUSTNESS.md"

# circuit id, pre-repair deck, post-repair deck, headline metric, note
STUDIES = [
    ("ce_bjt_amp", "baseline/ce_bjt_amp.cir", "repaired/ce_bjt_amp.cir", "gain",
     "Re fully bypassed (gain set by intrinsic re) -> split Re: 56 unbypassed + 910 bypassed."),
    ("halfwave_rectifier", "baseline/halfwave_rectifier.cir",
     "repaired/halfwave_rectifier.cir", "drop",
     "Diode emission coefficient N=1.5 -> N=1 (physical silicon)."),
    ("buck_converter", "baseline/buck_converter.cir", "repaired/buck_converter.cir", "vout",
     "Duty 41.7% (ideal D*Vin) -> 46.1%, compensating the freewheel diode drop."),
    ("boost_converter", "baseline/round2/boost_converter.cir",
     "repaired/round2/boost_converter.cir", "vout",
     "Duty 58.3% -> 60.5% (automated spec-repair; topology-correct magnitude)."),
    ("bjt_diffamp", "baseline/round2/bjt_diffamp.cir",
     "repaired/round2/bjt_diffamp.cir", "ad",
     "Identity drift: generation produced an RC circuit, not a diff pair -> real "
     "differential pair with a 1 mA tail source."),
    ("ce_amp_bias_and_gain", "baseline/round2/ce_amp_bias_and_gain.cir",
     "repaired/round2/ce_amp_bias_and_gain.cir", "gain",
     "Floating output node (silent DC-solution corruption) -> light 100k load."),
]


def _load(rel: str) -> str:
    return (PROJECT_DIR / rel).read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------------ the study

def run_deck(circuit: str, netlist: str, headline: str, n: int,
             seed: int, dist: str, workers: int) -> dict:
    """Full signoff on one deck: nominal, Monte Carlo, sensitivity, WC, PVT."""
    entry = {"nominal": rb.nominal(circuit, netlist)}
    entry["nominal_metrics"] = entry["nominal"].get("metrics")
    entry.pop("nominal")

    mc = rb.monte_carlo(circuit, netlist, n, seed=seed, dist=dist, workers=workers)
    # Keep the log readable: per-sample metric values, plus the parameter draw
    # only for samples that failed to simulate (that is where you want it).
    mc["samples"] = [
        {"metrics": s["metrics"]} if s.get("metrics")
        else {"metrics": None, "error": s.get("error"), "values": s["values"]}
        for s in mc["samples"]
    ]
    entry["mc"] = mc

    sens = rb.sensitivity(circuit, netlist, workers=workers)
    entry["sensitivity"] = sens
    entry["worst_case"] = (rb.worst_case(circuit, netlist, headline, sens)
                           if sens.get("rows") else None)
    if entry["worst_case"]:
        for d in entry["worst_case"].values():
            d.pop("values", None)
    entry["pvt"] = rb.pvt_corners(circuit, netlist, workers=workers)
    return entry


def run_study(only: str | None, n: int, seed: int, dist: str, workers: int) -> dict:
    run = {
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ngspice_exe": main.find_ngspice(),
        "n_samples": n, "seed": seed, "dist": dist,
        "tolerances": {
            "passives": rb.PASSIVE_TOL, "supply": rb.SUPPLY_TOL,
            "model": {f"{k[0]}.{k[1]}": v for k, v in rb.MODEL_TOL.items()},
            "temp_range_C": list(rb.TEMP_RANGE), "temp_corners_C": list(rb.TEMP_CORNERS),
        },
        "studies": [],
    }
    for circuit, before, after, headline, note in STUDIES:
        if only and circuit != only:
            continue
        t0 = time.time()
        print(f"[{circuit}] ", end="", flush=True)
        rec = {"circuit": circuit, "headline": headline, "note": note,
               "specs": {k: vars(v) for k, v in rb.METRICS[circuit][1].items()},
               "decks": {}}
        for tag, rel in (("before", before), ("after", after)):
            print(f"{tag}...", end="", flush=True)
            rec["decks"][tag] = run_deck(circuit, _load(rel), headline,
                                         n, seed, dist, workers)
            rec["decks"][tag]["path"] = rel
        rec["seconds"] = round(time.time() - t0, 1)
        run["studies"].append(rec)
        print(f" {rec['seconds']}s")
    run["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return run


# ----------------------------------------------------------------- formatting

def _f(v, nd=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        if math.isinf(v):
            return "∞"
        if v != 0 and (abs(v) >= 1e5 or abs(v) < 1e-3):
            return f"{v:.{nd}g}"
        return f"{v:.{nd}f}"
    return str(v)


def _esc(s: str) -> str:
    """Escape pipes so metric labels like |gain| don't split table columns."""
    return str(s).replace("|", "\\|")


def _cpk(v):
    if v is None:
        return "—"
    if math.isinf(v):
        return "∞"
    return f"{v:.2f}"


def histogram(values: list[float], spec: dict, bins: int = 20, width: int = 40) -> str:
    """ASCII distribution with the spec window marked."""
    if not values:
        return "(no data)"
    lo, hi = min(values), max(values)
    lsl, usl = spec.get("lsl"), spec.get("usl")
    if lsl is not None:
        lo = min(lo, lsl)
    if usl is not None:
        hi = max(hi, usl)
    if hi <= lo:
        hi = lo + 1e-12
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        k = min(bins - 1, max(0, int((v - lo) / step)))
        counts[k] += 1
    peak = max(counts) or 1
    out = []
    for i, c in enumerate(counts):
        a, b = lo + i * step, lo + (i + 1) * step
        # a bin counts as in-spec only if the whole bin lies inside the limits
        inside = ((lsl is None or a >= lsl) and (usl is None or b <= usl))
        bar = ("#" if inside else "x") * round(c / peak * width)
        mark = " |" if not inside else "  "
        out.append(f"{a:10.4g}{mark} {bar}{'' if c == 0 else ' ' + str(c)}")
    legend = []
    if lsl is not None:
        legend.append(f"LSL={lsl:g}")
    if usl is not None:
        legend.append(f"USL={usl:g}")
    out.append(f"           (# = inside spec, x = outside; {', '.join(legend)})")
    return "\n".join(out)


def metric_row(tag: str, nominal: dict | None, mc: dict, name: str) -> str:
    m = mc["metrics"].get(name)
    nomv = (nominal or {}).get(name)
    if not m:
        return f"| {tag} | {_f(nomv)} | — | — | — | — | — | — |"
    cv = f"{m['cv']:.1%}" if m["cv"] is not None else "—"
    return (f"| {tag} | {_f(nomv)} | {_f(m['mean'])} | {_f(m['sigma'])} | {cv} | "
            f"{_f(m['min'])} … {_f(m['max'])} | {m['pass_rate']:.0%} | {_cpk(m['cpk'])} |")


def build_report(run: dict) -> str:
    L = []
    A = L.append
    A("# Robustness Signoff Report")
    A("")
    A(f"- **Generated:** {run.get('finished_utc', '')}")
    A(f"- **ngspice:** `{run['ngspice_exe']}`")
    A(f"- **Monte Carlo:** {run['n_samples']} samples/deck, "
      f"{run['dist']} (3σ at the tolerance limit), seed {run['seed']}")
    t = run["tolerances"]
    A(f"- **Tolerances:** R ±{t['passives']['r']:.0%}, C ±{t['passives']['c']:.0%}, "
      f"L ±{t['passives']['l']:.0%}, supply ±{t['supply']:.0%}, "
      f"BJT β ±{t['model']['npn.bf']:.0%}, "
      f"T ∈ [{t['temp_range_C'][0]:.0f}, {t['temp_range_C'][1]:.0f}] °C")
    A("- **API cost: $0.00** — this entire layer is ngspice only.")
    A("")
    A("## What this measures")
    A("")
    A("`speccheck.py` asks *is this deck correct at nominal values, 27 °C, exact supply?*")
    A("This layer asks the question a manufacturer asks: **how often is it correct**")
    A("once the resistors are 5% parts, the supply is ±5%, the transistor β is anywhere")
    A("in its data-sheet band, and the board runs from −40 to +85 °C.")
    A("")
    A("Each deck gets: Monte Carlo yield, per-parameter sensitivity (normalized,")
    A("`%` change in the spec per `%` change in the part), a sensitivity-directed")
    A("worst-case corner, and a deterministic temperature × supply × β grid.")
    A("")

    # ---- headline table
    A("## Headline: nominal correctness vs. manufacturable correctness")
    A("")
    A("| Circuit | Spec | Nominal before | Nominal after | **Yield before** | **Yield after** |")
    A("|---------|------|----------------|---------------|------------------|-----------------|")
    for s in run["studies"]:
        h = s["headline"]
        spec = s["specs"][h]
        b, a = s["decks"]["before"], s["decks"]["after"]
        nb = (b["nominal_metrics"] or {}).get(h)
        na = (a["nominal_metrics"] or {}).get(h)
        A(f"| `{s['circuit']}` | {_esc(spec['label'])} = {_f(spec['target'])} "
          f"[{_f(spec['lsl'])}, {_f(spec['usl'])}] | {_f(nb)} | {_f(na)} | "
          f"{b['mc']['yield']:.0%} | {a['mc']['yield']:.0%} |")
    A("")
    A("*Yield = share of Monte Carlo samples meeting **every** spec on the circuit.*")
    A("")

    # ---- per circuit
    for s in run["studies"]:
        cid, h = s["circuit"], s["headline"]
        A(f"## {cid}")
        A("")
        A(f"**Repair:** {s['note']}")
        A("")
        for name, spec in s["specs"].items():
            lim = f"[{_f(spec['lsl'])}, {_f(spec['usl'])}]"
            star = " ← headline" if name == h else ""
            A(f"### `{name}` — {_esc(spec['label'])} target {_f(spec['target'])} {spec['units']}, "
              f"limits {lim}{star}")
            A("")
            A("| Deck | Nominal | MC mean | σ | CV | min … max | in-spec | Cpk |")
            A("|------|---------|---------|---|----|-----------|---------|-----|")
            for tag in ("before", "after"):
                d = s["decks"][tag]
                A(metric_row(f"{tag} (`{Path(d['path']).name}`)",
                             d["nominal_metrics"], d["mc"], name))
            A("")

        # distribution of the headline metric
        for tag in ("before", "after"):
            d = s["decks"][tag]
            vals = [x["metrics"][h] for x in d["mc"]["samples"]
                    if x.get("metrics") and h in x["metrics"]]
            if not vals:
                continue
            A(f"**Distribution — {tag} ({h}):**")
            A("")
            A("```")
            A(histogram(vals, s["specs"][h]))
            A("```")
            A("")

        # sensitivity
        A(f"### Sensitivity ranking ({h})")
        A("")
        A("`S` = % change in the metric per 1% change in the parameter "
          "(temperature: % per 10 °C). `span` = total % swing of the metric "
          "across that parameter's full tolerance.")
        A("")
        A("| Rank | Parameter | Kind | Nominal | Tol | S (before) | span (before) | S (after) | span (after) |")
        A("|------|-----------|------|---------|-----|------------|---------------|-----------|--------------|")
        bs = {r["param"]: r for r in s["decks"]["before"]["sensitivity"].get("rows", [])}
        as_ = {r["param"]: r for r in s["decks"]["after"]["sensitivity"].get("rows", [])}
        order = [r["param"] for r in s["decks"]["before"]["sensitivity"].get("rows", [])]
        order += [p for p in as_ if p not in order]
        for i, p in enumerate(order[:8], 1):
            rb_, ra = bs.get(p), as_.get(p)
            mb = (rb_ or {}).get("metrics", {}).get(h)
            ma = (ra or {}).get("metrics", {}).get(h)
            src = rb_ or ra
            tol = "—" if src["kind"] == "temp" else f"±{src['tol']:.0%}"
            nomv = "27 °C" if src["kind"] == "temp" else _f(src["nominal"], 4)
            s_b = _f(mb["S"], 3) if mb else "—"
            sp_b = f"{mb['span_pct']:+.1f}%" if mb else "—"
            s_a = _f(ma["S"], 3) if ma else "—"
            sp_a = f"{ma['span_pct']:+.1f}%" if ma else "—"
            A(f"| {i} | `{p}` | {src['kind']} | {nomv} | {tol} | "
              f"{s_b} | {sp_b} | {s_a} | {sp_a} |")
        A("")

        # worst case + PVT
        A("### Worst-case corner and PVT grid")
        A("")
        A("| Deck | WC low | WC high | PVT min | PVT max | PVT corners in spec |")
        A("|------|--------|---------|---------|---------|---------------------|")
        for tag in ("before", "after"):
            d = s["decks"][tag]
            wc = d.get("worst_case") or {}
            wlo = ((wc.get("low") or {}).get("metrics") or {}).get(h)
            whi = ((wc.get("high") or {}).get("metrics") or {}).get(h)
            pv = [g["metrics"][h] for g in d["pvt"]
                  if g.get("metrics") and h in g["metrics"]]
            spec = s["specs"][h]
            sp = rb.Spec(spec["label"], spec["target"], spec["lsl"],
                         spec["usl"], spec["units"])
            ok = sum(sp.ok(v) for v in pv)
            A(f"| {tag} | {_f(wlo)} | {_f(whi)} | {_f(min(pv)) if pv else '—'} | "
              f"{_f(max(pv)) if pv else '—'} | {ok}/{len(pv)} |")
        A("")

    A("---")
    A("")
    A(f"Full evidence (every sample, every corner): `{LOG_PATH.name}`.")
    A("")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------- entry pt

def main_entry() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=500, help="Monte Carlo samples per deck")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--dist", choices=("gaussian", "uniform"), default="gaussian")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--circuit", default=None, help="run only this circuit")
    ap.add_argument("--report-only", action="store_true",
                    help="rebuild ROBUSTNESS.md from robustness_log.json")
    args = ap.parse_args()

    if args.report_only:
        if not LOG_PATH.exists():
            print(f"{LOG_PATH.name} not found — run the study first.", file=sys.stderr)
            return 1
        run = json.loads(LOG_PATH.read_text(encoding="utf-8"))
    else:
        t0 = time.time()
        run = run_study(args.circuit, args.n, args.seed, args.dist, args.workers)
        LOG_PATH.write_text(json.dumps(run, indent=1), encoding="utf-8")
        print(f"wrote {LOG_PATH.name} ({LOG_PATH.stat().st_size/1e6:.1f} MB) "
              f"in {time.time()-t0:.0f}s")

    REPORT_PATH.write_text(build_report(run), encoding="utf-8")
    print(f"wrote {REPORT_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
