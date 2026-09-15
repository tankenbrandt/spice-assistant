#!/usr/bin/env python3
"""Robustness signoff layer for spice-assistant.

The spec layer (`speccheck.py`) answers "is this deck electrically correct at
nominal component values, 27 C, exact supply?". That is a necessary check, not
a sufficient one: a design can hit its target dead-on at nominal and still be
unmanufacturable because the target is held by a quantity that drifts with
temperature, bias, or device spread.

This module perturbs the deck the way the physical world does and re-measures:

  monte-carlo   N randomized samples over passive tolerance, supply tolerance,
                device-model spread (beta, VTO, ...) and temperature
                -> yield %, mean, sigma, Cpk, distribution

  sensitivity   one-at-a-time +/- perturbation of every parameter
                -> normalized sensitivity S = (dMetric/Metric)/(dParam/Param),
                   i.e. "% change in the spec per 1% change in this part"

  worst-case    sensitivity-directed corner construction: every parameter is
                pushed simultaneously to the extreme that moves the metric
                away from target, giving the (linearized) worst-case bound

  pvt-corners   deterministic temperature x supply x beta grid

Everything here is pure ngspice. Zero API calls, zero cost.
"""

import json
import math
import random
import re
import shutil
import statistics
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import speccheck
from speccheck import spice_value

PROJECT_DIR = Path(__file__).resolve().parent

# ------------------------------------------------------------------ tolerances

# Fractional tolerances applied to netlist components. Defaults are ordinary
# commodity parts: 5% thick-film resistors, 10% ceramic caps, 10% inductors,
# 5% regulated supply.
PASSIVE_TOL = {"r": 0.05, "c": 0.10, "l": 0.10}
SUPPLY_TOL = 0.05

# Device-model spread, keyed (model type, parameter). BF (forward beta) is the
# dominant one for BJTs: a 2N2222 is specified 100 min / 300 typ-max, so +-50%
# around a nominal 200 is realistic, not pessimistic.
MODEL_TOL = {
    ("npn", "bf"): 0.50, ("pnp", "bf"): 0.50,
    ("npn", "is"): 0.30, ("pnp", "is"): 0.30,
    ("npn", "vaf"): 0.25, ("pnp", "vaf"): 0.25,
    ("nmos", "vto"): 0.15, ("pmos", "vto"): 0.15,
    ("nmos", "kp"): 0.20, ("pmos", "kp"): 0.20,
    ("nmos", "lambda"): 0.25, ("pmos", "lambda"): 0.25,
    ("d", "is"): 0.50, ("d", "n"): 0.05, ("d", "rs"): 0.20,
}

# Operating temperature: sampled uniformly for Monte Carlo, extremes for corners.
TEMP_RANGE = (-40.0, 85.0)
TEMP_CORNERS = (-40.0, 27.0, 85.0)
TEMP_NOM = 27.0

# Sensitivity reporting uses a 10 C step for temperature (it is not a
# multiplicative parameter, so a normalized %/% figure would be meaningless).
TEMP_SENS_STEP = 10.0


# ------------------------------------------------------------------ parameters

@dataclass
class Param:
    """One perturbable quantity in a deck."""
    key: str            # stable id, e.g. "Rc", "Vcc", "QN2222.BF", "TEMP"
    kind: str           # passive | supply | model | temp
    nominal: float
    tol: float          # fractional (unused for temp)
    line: int = -1      # netlist line index      (passive/supply/model)
    token: int = -1     # token index in the line (passive/supply)
    model: str = ""     # model card name         (model)
    pname: str = ""     # model parameter name    (model)

    def label(self) -> str:
        return "TEMP" if self.kind == "temp" else self.key

    def sample(self, rng: random.Random, dist: str) -> float:
        if self.kind == "temp":
            return rng.uniform(*TEMP_RANGE)
        if dist == "uniform":
            f = rng.uniform(1.0 - self.tol, 1.0 + self.tol)
        else:  # gaussian with 3-sigma at the tolerance limit, clipped at +-tol
            f = rng.gauss(1.0, self.tol / 3.0)
            f = max(1.0 - self.tol, min(1.0 + self.tol, f))
        return self.nominal * f

    def extremes(self) -> tuple[float, float]:
        if self.kind == "temp":
            return TEMP_RANGE
        return self.nominal * (1.0 - self.tol), self.nominal * (1.0 + self.tol)


_COMP_RE = re.compile(r"^([rlcv])(\S*)\s+(\S+)\s+(\S+)\s+(\S+)", re.I)
_MODEL_HEAD_RE = re.compile(r"(?i)^\s*\.model\s+(\S+)\s+(\w+)")
_TEMP_LINE_RE = re.compile(r"(?i)^\s*\.temp\b")


def _model_spans(lines: list[str]) -> list[tuple[str, str, int, int]]:
    """Locate .model cards: (name, type, first_line, last_line_inclusive)."""
    spans = []
    for i, ln in enumerate(lines):
        m = _MODEL_HEAD_RE.match(ln)
        if not m:
            continue
        j = i
        while j + 1 < len(lines) and lines[j + 1].lstrip().startswith("+"):
            j += 1
        spans.append((m.group(1), m.group(2).lower(), i, j))
    return spans


def _control_mask(lines: list[str]) -> list[bool]:
    """True for lines inside a .control/.endc block.

    Those lines are stripped before simulation, and their control-language
    statements alias component syntax (`let midband_index = 20` looks exactly
    like an inductor), so they must never become parameters.
    """
    mask, inside = [], False
    for ln in lines:
        s = ln.strip().lower()
        if s.startswith(".control"):
            inside = True
        mask.append(inside)
        if s.startswith(".endc"):
            inside = False
    return mask


def extract_params(netlist: str, *, vary_temp: bool = True) -> tuple[list[str], list[Param]]:
    """Find every perturbable parameter in a deck.

    Components whose value is not a plain SPICE number (expressions, PULSE/SIN
    sources, model references) are skipped rather than guessed at.
    """
    lines = netlist.splitlines()
    in_control = _control_mask(lines)
    params: list[Param] = []

    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s or s.startswith(("*", ".", "+")) or in_control[i]:
            continue
        m = _COMP_RE.match(s)
        if not m:
            continue
        letter = m.group(1).lower()
        toks = ln.split()
        refdes = toks[0]

        if letter in PASSIVE_TOL:
            if len(toks) < 4:
                continue
            try:
                val = spice_value(toks[3])
            except ValueError:
                continue
            if val == 0:
                continue
            params.append(Param(refdes, "passive", val, PASSIVE_TOL[letter], i, 3))
        elif letter == "v":
            # Supplies only: a source with a non-zero DC value. The AC stimulus
            # (Vin in 0 DC 0 AC 1) and PULSE/SIN sources fall out naturally.
            ti = -1
            for k in range(3, len(toks)):
                if toks[k].lower() == "dc" and k + 1 < len(toks):
                    ti = k + 1
                    break
            if ti < 0 and len(toks) > 3:
                ti = 3
            if ti < 0 or ti >= len(toks):
                continue
            try:
                dc = spice_value(toks[ti])
            except ValueError:
                continue
            if dc == 0:
                continue
            params.append(Param(refdes, "supply", dc, SUPPLY_TOL, i, ti))

    for name, mtype, i, j in _model_spans(lines):
        card = "\n".join(lines[i:j + 1])
        for pm in re.finditer(r"(?i)\b([a-z]\w*)\s*=\s*([^\s)]+)", card):
            pname = pm.group(1).lower()
            tol = MODEL_TOL.get((mtype, pname))
            if tol is None:
                continue
            try:
                val = spice_value(pm.group(2))
            except ValueError:
                continue
            if val == 0:
                continue
            params.append(
                Param(f"{name}.{pm.group(1).upper()}", "model", val, tol,
                      i, -1, model=name, pname=pm.group(1))
            )

    if vary_temp:
        params.append(Param("TEMP", "temp", TEMP_NOM, 0.0))

    return lines, params


def _fmt(v: float) -> str:
    """SPICE-safe number (no '+' in the exponent, which some parsers dislike)."""
    return f"{v:.6g}".replace("e+", "e")


def apply_params(netlist: str, params: list[Param], values: dict[str, float]) -> str:
    """Return a copy of `netlist` with the named parameters set to `values`."""
    lines = [ln for ln in netlist.splitlines() if not _TEMP_LINE_RE.match(ln)]
    temp = None
    by_model: dict[int, list[Param]] = {}

    for p in params:
        if p.key not in values:
            continue
        v = values[p.key]
        if p.kind == "temp":
            temp = v
        elif p.kind in ("passive", "supply"):
            toks = lines[p.line].split()
            if p.token < len(toks):
                toks[p.token] = _fmt(v)
                lines[p.line] = " ".join(toks)
        elif p.kind == "model":
            by_model.setdefault(p.line, []).append(p)

    # Model cards are rewritten span-wise so multi-line (+) cards work.
    for head in sorted(by_model, reverse=True):
        j = head
        while j + 1 < len(lines) and lines[j + 1].lstrip().startswith("+"):
            j += 1
        card = "\n".join(lines[head:j + 1])
        for p in by_model[head]:
            card = re.sub(
                r"(?i)\b" + re.escape(p.pname) + r"\s*=\s*[^\s)]+",
                p.pname + "=" + _fmt(values[p.key]),
                card,
                count=1,
            )
        lines[head:j + 1] = card.splitlines()

    out = "\n".join(lines).rstrip() + "\n"
    if temp is not None:
        out += ".temp " + _fmt(temp) + "\n"
    return out


# ------------------------------------------------------------- measurement lib

@contextmanager
def measured(netlist: str, control_body: str):
    """speccheck.run_measurement + guaranteed temp-dir cleanup.

    (speccheck leaks its temp dirs; harmless for 15 runs, not for 500 x 15.)
    """
    d, out = speccheck.run_measurement(netlist, control_body)
    try:
        yield d, out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _ac_peak(netlist: str, fmin: str, fmax: str) -> float:
    ctl = ("set units=degrees\nset wr_singlescale\n"
           "ac dec 20 " + fmin + " " + fmax + "\nwrdata meas.txt vm(out) vp(out)\n")
    with measured(netlist, ctl) as (d, _):
        _, (vm, vp) = speccheck.read_wrdata(d / "meas.txt", 2)
    return max(vm)


def m_ce_bjt_amp(nl: str) -> dict[str, float]:
    return {"gain": _ac_peak(nl, "10", "10meg")}


def m_halfwave_rectifier(nl: str) -> dict[str, float]:
    ctl = "set wr_singlescale\ntran 50u 50m\nwrdata meas.txt v(in) v(out)\n"
    with measured(nl, ctl) as (d, _):
        _, (vin, vout) = speccheck.read_wrdata(d / "meas.txt", 2)
    return {"drop": max(vin) - max(vout), "vmin": min(vout)}


def m_buck_converter(nl: str) -> dict[str, float]:
    ctl = "set wr_singlescale\ntran 200n 8m\nwrdata meas.txt v(out)\n"
    with measured(nl, ctl) as (d, _):
        t, (vout,) = speccheck.read_wrdata(d / "meas.txt", 1)
    win = speccheck._window(t, vout, 0.8)
    return {"vout": speccheck._mean(win), "ripple": max(win) - min(win)}


def m_boost_converter(nl: str) -> dict[str, float]:
    ctl = "set wr_singlescale\ntran 200n 12m\nwrdata meas.txt v(out)\n"
    with measured(nl, ctl) as (d, _):
        t, (vout,) = speccheck.read_wrdata(d / "meas.txt", 1)
    win = speccheck._window(t, vout, 0.8)
    return {"vout": speccheck._mean(win), "ripple": max(win) - min(win)}


def m_bjt_diffamp(nl: str) -> dict[str, float]:
    ctl_d = ("alter @vinp[acmag]=0.5\nalter @vinp[acphase]=0\n"
             "alter @vinn[acmag]=0.5\nalter @vinn[acphase]=180\n"
             "set wr_singlescale\nac dec 20 10 1meg\nwrdata meas.txt vm(out)\n")
    with measured(nl, ctl_d) as (d, _):
        _, (vm_d,) = speccheck.read_wrdata(d / "meas.txt", 1)
    ad = max(vm_d)
    idx = vm_d.index(ad)
    ctl_c = ("alter @vinp[acmag]=1\nalter @vinp[acphase]=0\n"
             "alter @vinn[acmag]=1\nalter @vinn[acphase]=0\n"
             "set wr_singlescale\nac dec 20 10 1meg\nwrdata meas.txt vm(out)\n")
    with measured(nl, ctl_c) as (d, _):
        _, (vm_c,) = speccheck.read_wrdata(d / "meas.txt", 1)
    acm = vm_c[idx]
    cmrr = 20.0 * math.log10(ad / acm) if acm > 0 and ad > 0 else 200.0
    return {"ad": ad, "cmrr": cmrr}


def m_ce_amp_bias_and_gain(nl: str) -> dict[str, float]:
    ctl = ("op\nprint v(coll) v(emit)\nset units=degrees\nset wr_singlescale\n"
           "ac dec 20 10 10meg\nwrdata meas.txt vm(out) vp(out)\n")
    with measured(nl, ctl) as (d, out):
        mc = re.search(r"(?i)v\(coll\)\s*=\s*([\d.eE+-]+)", out)
        me = re.search(r"(?i)v\(emit\)\s*=\s*([\d.eE+-]+)", out)
        if not mc or not me:
            raise RuntimeError("could not read v(coll)/v(emit) from op output")
        _, (vm, vp) = speccheck.read_wrdata(d / "meas.txt", 2)
    return {"vce": float(mc.group(1)) - float(me.group(1)), "gain": max(vm)}


# --------------------------------------------------------------------- specs

@dataclass
class Spec:
    label: str
    target: float | None
    lsl: float | None
    usl: float | None
    units: str = ""

    def ok(self, v: float) -> bool:
        if self.lsl is not None and v < self.lsl:
            return False
        if self.usl is not None and v > self.usl:
            return False
        return True


def _pct(target: float, frac: float) -> tuple[float, float]:
    return target * (1 - frac), target * (1 + frac)


METRICS = {
    "ce_bjt_amp": (m_ce_bjt_amp, {
        "gain": Spec("|gain|", 50.0, *_pct(50.0, 0.05), "V/V")}),
    "halfwave_rectifier": (m_halfwave_rectifier, {
        "drop": Spec("peak drop", None, None, 1.0, "V"),
        "vmin": Spec("min V(out)", None, -0.1, None, "V")}),
    "buck_converter": (m_buck_converter, {
        "vout": Spec("V(out) avg", 5.0, *_pct(5.0, 0.03), "V"),
        "ripple": Spec("ripple", None, None, 0.1, "Vpp")}),
    "boost_converter": (m_boost_converter, {
        "vout": Spec("V(out) avg", 12.0, *_pct(12.0, 0.03), "V")}),
    "bjt_diffamp": (m_bjt_diffamp, {
        "ad": Spec("Ad", 25.0, *_pct(25.0, 0.10), "V/V"),
        "cmrr": Spec("CMRR", None, 60.0, None, "dB")}),
    "ce_amp_bias_and_gain": (m_ce_amp_bias_and_gain, {
        "vce": Spec("Vce", 6.0, *_pct(6.0, 0.10), "V"),
        "gain": Spec("|gain|", 80.0, *_pct(80.0, 0.10), "V/V")}),
}


# ---------------------------------------------------------------- statistics

def cpk(values: list[float], spec: Spec) -> float | None:
    """Process capability index. >=1.33 is the usual "capable" threshold."""
    if len(values) < 2:
        return None
    mu = statistics.fmean(values)
    sd = statistics.stdev(values)
    if sd == 0:
        return float("inf") if spec.ok(mu) else 0.0
    sides = []
    if spec.usl is not None:
        sides.append((spec.usl - mu) / (3 * sd))
    if spec.lsl is not None:
        sides.append((mu - spec.lsl) / (3 * sd))
    return min(sides) if sides else None


def summarize(samples: list[dict], specs: dict[str, Spec]) -> dict:
    """Per-metric stats + overall yield over successful samples."""
    good = [s for s in samples if s.get("metrics")]
    out = {"n": len(samples), "n_measured": len(good),
           "n_sim_fail": len(samples) - len(good), "metrics": {}}
    for name, spec in specs.items():
        vals = [s["metrics"][name] for s in good if name in s["metrics"]]
        if not vals:
            continue
        mu = statistics.fmean(vals)
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        out["metrics"][name] = {
            "label": spec.label, "units": spec.units, "target": spec.target,
            "lsl": spec.lsl, "usl": spec.usl,
            "mean": mu, "sigma": sd, "cv": (sd / abs(mu) if mu else None),
            "min": min(vals), "max": max(vals),
            "pass_rate": sum(spec.ok(v) for v in vals) / len(vals),
            "cpk": cpk(vals, spec),
        }
    # A sample yields only if the sim converged and every spec passes.
    ok = sum(1 for s in good
             if all(n in s["metrics"] and sp.ok(s["metrics"][n])
                    for n, sp in specs.items()))
    out["yield"] = ok / len(samples) if samples else 0.0
    return out


# ------------------------------------------------------------------ analyses

def _evaluate(metric_fn, netlist: str, params: list[Param],
              values: dict[str, float]) -> dict:
    """One perturbed simulation. Never raises: a non-converging corner is data."""
    deck = apply_params(netlist, params, values)
    try:
        return {"values": values, "metrics": metric_fn(deck)}
    except Exception as exc:
        return {"values": values, "metrics": None,
                "error": type(exc).__name__ + ": " + str(exc)}


def _map(fn, jobs: list, workers: int) -> list:
    if workers <= 1:
        return [fn(j) for j in jobs]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, jobs))


def nominal(circuit: str, netlist: str) -> dict:
    metric_fn, _ = METRICS[circuit]
    _, params = extract_params(netlist, vary_temp=True)
    vals = {p.key: (TEMP_NOM if p.kind == "temp" else p.nominal) for p in params}
    return _evaluate(metric_fn, netlist, params, vals)


def monte_carlo(circuit: str, netlist: str, n: int, *, seed: int = 0,
                dist: str = "gaussian", vary_temp: bool = True,
                workers: int = 8) -> dict:
    metric_fn, specs = METRICS[circuit]
    _, params = extract_params(netlist, vary_temp=vary_temp)
    rng = random.Random(seed)
    draws = [{p.key: p.sample(rng, dist) for p in params} for _ in range(n)]
    samples = _map(lambda v: _evaluate(metric_fn, netlist, params, v), draws, workers)
    res = summarize(samples, specs)
    res.update({"dist": dist, "seed": seed, "vary_temp": vary_temp,
                "params": [p.label() for p in params], "samples": samples})
    return res


def sensitivity(circuit: str, netlist: str, *, workers: int = 8) -> dict:
    """One-at-a-time normalized sensitivity for every parameter.

    S = (dMetric / Metric_nom) / (dParam / Param_nom)  -- "% per %".
    Temperature is reported as % of the metric per TEMP_SENS_STEP degrees.
    """
    metric_fn, specs = METRICS[circuit]
    _, params = extract_params(netlist, vary_temp=True)
    base = {p.key: (TEMP_NOM if p.kind == "temp" else p.nominal) for p in params}
    nom = _evaluate(metric_fn, netlist, params, base)
    if not nom["metrics"]:
        return {"error": nom.get("error"), "nominal": None, "rows": []}

    jobs = []
    for p in params:
        if p.kind == "temp":
            lo, hi = TEMP_NOM - TEMP_SENS_STEP, TEMP_NOM + TEMP_SENS_STEP
        else:
            lo, hi = p.extremes()
        for v in (lo, hi):
            d = dict(base)
            d[p.key] = v
            jobs.append(d)

    out = _map(lambda d: _evaluate(metric_fn, netlist, params, d), jobs, workers)

    rows = []
    for i, p in enumerate(params):
        lo_r, hi_r = out[2 * i], out[2 * i + 1]
        row = {"param": p.label(), "kind": p.kind, "nominal": p.nominal,
               "tol": p.tol, "metrics": {}}
        for name in specs:
            y0 = nom["metrics"].get(name)
            ylo = (lo_r["metrics"] or {}).get(name)
            yhi = (hi_r["metrics"] or {}).get(name)
            if not y0 or ylo is None or yhi is None:
                continue
            if p.kind == "temp":
                s = ((yhi - ylo) / 2.0) / y0 * 100.0   # % of metric per 10 C
            else:
                s = ((yhi - ylo) / (2.0 * p.tol)) / y0 if p.tol else 0.0
            row["metrics"][name] = {
                "S": s, "span_pct": (yhi - ylo) / y0 * 100.0,
                "lo": ylo, "hi": yhi, "sign": 1 if yhi >= ylo else -1,
            }
        rows.append(row)

    rows.sort(key=lambda r: max((abs(m["span_pct"]) for m in r["metrics"].values()),
                                default=0.0), reverse=True)
    return {"nominal": nom["metrics"], "rows": rows}


def worst_case(circuit: str, netlist: str, metric: str, sens: dict) -> dict:
    """Sensitivity-directed worst-case corners (classic extreme-value analysis).

    Each parameter is pushed to the extreme that moves `metric` down (for the
    low corner) or up (for the high corner) simultaneously. This is the
    linearized worst case -- exact only if the response is monotonic in each
    parameter, which is why it is reported alongside Monte Carlo, not instead.
    """
    metric_fn, _ = METRICS[circuit]
    _, params = extract_params(netlist, vary_temp=True)
    sign = {r["param"]: r["metrics"].get(metric, {}).get("sign", 1)
            for r in sens["rows"]}

    corners = {}
    for direction, want in (("low", -1), ("high", +1)):
        vals = {}
        for p in params:
            lo, hi = p.extremes()
            vals[p.key] = hi if sign.get(p.label(), 1) == want else lo
        corners[direction] = _evaluate(metric_fn, netlist, params, vals)
    return corners


def pvt_corners(circuit: str, netlist: str, *, workers: int = 8) -> list[dict]:
    """Deterministic temperature x supply x beta grid (passives at nominal)."""
    metric_fn, _ = METRICS[circuit]
    _, params = extract_params(netlist, vary_temp=True)
    supply_keys = {p.key for p in params if p.kind == "supply"}
    beta_keys = {p.key for p in params
                 if p.kind == "model" and p.pname.lower() == "bf"}

    grid = []
    for t in TEMP_CORNERS:
        for sk, sname in ((-1, "V-"), (0, "Vnom"), (1, "V+")):
            for bk, bname in ((-1, "beta-"), (0, "beta_nom"), (1, "beta+")):
                vals = {}
                for p in params:
                    if p.kind == "temp":
                        vals[p.key] = t
                    elif p.key in supply_keys:
                        vals[p.key] = p.nominal * (1 + sk * p.tol)
                    elif p.key in beta_keys:
                        vals[p.key] = p.nominal * (1 + bk * p.tol)
                    else:
                        vals[p.key] = p.nominal
                grid.append({"name": f"{t:.0f}C/{sname}/{bname}", "temp": t,
                             "supply": sname, "beta": bname, "vals": vals})

    out = _map(lambda g: _evaluate(metric_fn, netlist, params, g["vals"]), grid, workers)
    for g, r in zip(grid, out):
        g["metrics"] = r["metrics"]
        g["error"] = r.get("error")
        g.pop("vals")
    return grid
