#!/usr/bin/env python3
"""Spec-verification layer for the spice-assistant benchmark.

After a circuit passes the clean-run check, its final netlist is re-run with a
harness-controlled measurement `.control` block: the model's own analyses are
stripped, our analyses + `wrdata` dumps are injected, ngspice runs in batch
mode, and the resulting numeric vectors are parsed and compared against each
circuit's electrical spec. Results carry the actual measured numbers.

The benchmark descriptions ask the model to use standard node names
('in'/'out') and to name the input source 'vin' so measurements are
deterministic; a netlist that ignores the convention shows up here as a
measurement error (counted as a spec failure, with the reason logged).
"""

import re
import subprocess
import tempfile
from pathlib import Path

import main

MEAS_TIMEOUT = 120

# ---------------------------------------------------------------- value parse

_SUFFIXES = {
    "t": 1e12, "g": 1e9, "meg": 1e6, "k": 1e3, "mil": 25.4e-6,
    "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15,
}
# 'meg'/'mil' must be tried before bare 'm'
_VALUE_RE = re.compile(r"^([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*(meg|mil|t|g|k|m|u|n|p|f)?", re.I)


def spice_value(token: str) -> float:
    m = _VALUE_RE.match(token.strip())
    if not m or not m.group(1):
        raise ValueError(f"cannot parse SPICE value: {token!r}")
    return float(m.group(1)) * _SUFFIXES.get((m.group(2) or "").lower(), 1.0)


def first_value(netlist: str, letter: str) -> float | None:
    """Value of the first component whose refdes starts with `letter`."""
    for ln in netlist.splitlines():
        s = ln.strip()
        if not s or s.startswith(("*", ".", "+")):
            continue
        if s[0].lower() == letter.lower():
            parts = s.split()
            if len(parts) >= 4:
                try:
                    return spice_value(parts[3])
                except ValueError:
                    continue
    return None


# ---------------------------------------------------------- deck manipulation

_DOT_STRIP_RE = re.compile(
    r"(?i)^\s*\.(op|ac|tran|dc|noise|disto|pz|sens|tf|four|print|plot|probe|save)\b"
)


def strip_analyses(netlist: str) -> str:
    """Remove the model's .control block and standalone analysis/output dot-lines."""
    txt = re.sub(r"(?is)^[ \t]*\.control\b.*?^[ \t]*\.endc[ \t]*$", "", netlist, flags=re.M)
    kept = [ln for ln in txt.splitlines() if not _DOT_STRIP_RE.match(ln)]
    # drop the .end line; we re-append it after our control block
    kept = [ln for ln in kept if not re.match(r"(?i)^\s*\.end\s*$", ln)]
    return "\n".join(kept).rstrip() + "\n"


def with_measurement(netlist: str, control_body: str) -> str:
    return (
        strip_analyses(netlist)
        + ".control\n"
        + control_body.strip()
        + "\n.endc\n.end\n"
    )


# ------------------------------------------------------------------ execution

def run_measurement(netlist: str, control_body: str) -> tuple[Path, str]:
    """Run the instrumented deck; returns (tmpdir, combined ngspice output).

    ngspice runs with cwd=tmpdir so wrdata can use bare filenames (the project
    path contains spaces, which the control language handles poorly).
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="spicemeas_"))
    (tmpdir / "meas.cir").write_text(
        with_measurement(netlist, control_body), encoding="ascii", errors="replace"
    )
    proc = subprocess.run(
        [main.find_ngspice(), "-b", "meas.cir"],
        cwd=str(tmpdir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=MEAS_TIMEOUT,
    )
    return tmpdir, (proc.stdout or "") + (proc.stderr or "")


def read_wrdata(path: Path, nvec: int) -> tuple[list[float], list[list[float]]]:
    """Parse a `set wr_singlescale` wrdata file: col0=scale, then one col/vector."""
    if not path.exists():
        raise RuntimeError("wrdata output file was not created (check vector names)")
    rows = []
    for line in path.read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        try:
            rows.append([float(p) for p in parts])
        except ValueError:
            continue
    if not rows:
        raise RuntimeError("wrdata file contains no numeric data")
    ncol = min(len(r) for r in rows)
    if ncol < nvec + 1:
        raise RuntimeError(f"wrdata has {ncol} columns, expected {nvec + 1}")
    xs = [r[0] for r in rows]
    vecs = [[r[i + 1] for r in rows] for i in range(nvec)]
    return xs, vecs


# ------------------------------------------------------------------- numerics

def crossing(xs, ys, level, rising):
    """First x where ys crosses `level` (linear interpolation)."""
    for i in range(len(ys) - 1):
        a, b = ys[i] - level, ys[i + 1] - level
        if (rising and a < 0 <= b) or (not rising and a > 0 >= b):
            if b == a:
                return xs[i]
            return xs[i] + (0 - a) / (b - a) * (xs[i + 1] - xs[i])
    return None


def _mean(v):
    return sum(v) / len(v)


def _window(xs, ys, frac_from):
    """Slice of ys where xs >= frac_from * xs[-1]."""
    x0 = frac_from * xs[-1]
    out = [y for x, y in zip(xs, ys) if x >= x0]
    return out if out else ys


def _result(ok: bool, target: str, measured: str) -> dict:
    return {"pass": bool(ok), "target": target, "measured": measured, "error": None}


def _subspec(ok: bool, target: str, measured: str) -> dict:
    return {"pass": bool(ok), "target": target, "measured": measured}


def _multi_result(specs: dict) -> dict:
    """Combine named sub-specs (coupled specs) into one result.

    The full spec passes only if EVERY sub-spec passes; each sub-spec keeps its
    own pass/measured record so a repair that fixes one spec while breaking
    another is visible in the log.
    """
    return {
        "pass": all(s["pass"] for s in specs.values()),
        "target": "; ".join(f"{k}: {s['target']}" for k, s in specs.items()),
        "measured": "; ".join(f"{k}: {s['measured']}" for k, s in specs.items()),
        "error": None,
        "specs": specs,
    }


# ------------------------------------------------------------ per-circuit specs

def check_rc_lowpass(nl: str) -> dict:
    target = "-3 dB point at 1 kHz ±5%"
    ctl = "set wr_singlescale\nac dec 200 10 1meg\nwrdata meas.txt vdb(out)\n"
    d, _ = run_measurement(nl, ctl)
    f, (vdb,) = read_wrdata(d / "meas.txt", 1)
    f3 = crossing(f, vdb, vdb[0] - 3.0, rising=False)
    if f3 is None:
        raise RuntimeError("no -3 dB crossing found in AC sweep")
    err = (f3 - 1000.0) / 1000.0
    return _result(abs(err) <= 0.05, target, f"f(-3dB) = {f3:.1f} Hz ({err:+.1%} vs 1 kHz)")


def check_rl_step(nl: str) -> dict:
    R = first_value(nl, "r")
    L = first_value(nl, "l")
    if not R or not L:
        raise RuntimeError("could not parse R and L values from the netlist")
    tau_exp = L / R
    target = f"time constant within 5% of L/R = {tau_exp:.3e} s (deck: R={R:g}, L={L:g})"
    tstep, tstop = tau_exp / 200.0, 8.0 * tau_exp
    ctl = (
        "set wr_singlescale\n"
        f"tran {tstep:g} {tstop:g}\n"
        "wrdata meas.txt v(in) vin#branch\n"
    )
    d, _ = run_measurement(nl, ctl)
    t, (vin, ib) = read_wrdata(d / "meas.txt", 2)
    i = [abs(x) for x in ib]
    n_tail = max(1, len(i) // 20)
    i_fin = _mean(i[-n_tail:])
    v_fin = _mean(vin[-n_tail:])
    if i_fin <= 0:
        raise RuntimeError("final current is zero — no step response measured")
    t0 = crossing(t, vin, 0.5 * v_fin, rising=True) or 0.0
    t63 = crossing(t, i, 0.632 * i_fin, rising=True)
    if t63 is None:
        raise RuntimeError("current never reached 63.2% of final value")
    tau_meas = t63 - t0
    err = (tau_meas - tau_exp) / tau_exp
    return _result(abs(err) <= 0.05, target, f"tau = {tau_meas:.3e} s ({err:+.1%} vs L/R)")


def check_voltage_divider(nl: str) -> dict:
    target = "V(out) = 3.3 V ±1%"
    _, out = run_measurement(nl, "op\nprint v(out)\n")
    m = re.search(r"(?i)v\(out\)\s*=\s*([\d.eE+-]+)", out)
    if not m:
        raise RuntimeError("could not find v(out) in op output")
    v = float(m.group(1))
    err = (v - 3.3) / 3.3
    return _result(abs(err) <= 0.01, target, f"V(out) = {v:.4f} V ({err:+.2%})")


def check_halfwave_rectifier(nl: str) -> dict:
    target = "peak drop ≤ 1 diode drop (≤1.0 V) vs 10 V input; V(out) never < -0.1 V"
    ctl = "set wr_singlescale\ntran 50u 50m\nwrdata meas.txt v(in) v(out)\n"
    d, _ = run_measurement(nl, ctl)
    _, (vin, vout) = read_wrdata(d / "meas.txt", 2)
    peak_in, peak_out, v_min = max(vin), max(vout), min(vout)
    drop = peak_in - peak_out
    ok = (-0.01 <= drop <= 1.0) and (v_min >= -0.1)
    return _result(
        ok, target,
        f"peak_out = {peak_out:.2f} V (drop {drop:.2f} V vs peak_in {peak_in:.2f} V), min = {v_min:.3f} V",
    )


def check_ce_bjt_amp(nl: str) -> dict:
    target = "mid-band |gain| = 50 ±5%, inverting"
    ctl = (
        "set units=degrees\nset wr_singlescale\n"
        "ac dec 20 10 10meg\nwrdata meas.txt vm(out) vp(out)\n"
    )
    d, _ = run_measurement(nl, ctl)
    _, (vm, vp) = read_wrdata(d / "meas.txt", 2)
    gain = max(vm)
    idx = vm.index(gain)
    ph = vp[idx]
    if max(abs(p) for p in vp) < 3.5:  # values look like radians, not degrees
        ph = ph * 180.0 / 3.141592653589793
    inverted = 90.0 <= abs(ph) % 360.0 <= 270.0
    err = (gain - 50.0) / 50.0
    ok = abs(err) <= 0.05 and inverted
    return _result(ok, target, f"|gain| = {gain:.2f} ({err:+.1%}), phase = {ph:.0f} deg")


def check_noninv_opamp(nl: str) -> dict:
    target = "gain = 11 (1 + Rf/Rin) ±2%"
    ctl = "set wr_singlescale\ntran 5u 10m\nwrdata meas.txt v(in) v(out)\n"
    d, _ = run_measurement(nl, ctl)
    t, (vin, vout) = read_wrdata(d / "meas.txt", 2)
    win_in = _window(t, vin, 0.2)
    win_out = _window(t, vout, 0.2)
    ptp_in = max(win_in) - min(win_in)
    ptp_out = max(win_out) - min(win_out)
    if ptp_in <= 0:
        raise RuntimeError("input has zero amplitude in measurement window")
    gain = ptp_out / ptp_in
    err = (gain - 11.0) / 11.0
    return _result(abs(err) <= 0.02, target, f"gain = {gain:.3f} ({err:+.2%})")


def check_bridge_rectifier(nl: str) -> dict:
    target = "steady-state output ripple < 1.0 Vpp into 100 ohm load"
    ctl = "set wr_singlescale\ntran 100u 300m\nwrdata meas.txt v(out)\n"
    d, _ = run_measurement(nl, ctl)
    t, (vout,) = read_wrdata(d / "meas.txt", 1)
    win = _window(t, vout, 0.8)
    ripple = max(win) - min(win)
    return _result(
        ripple < 1.0, target,
        f"ripple = {ripple:.3f} Vpp (avg {sum(win)/len(win):.2f} V) over last 20% of 300 ms",
    )


def check_buck_converter(nl: str) -> dict:
    target = "V(out) = 5 V ±3% average, ripple ≤ 100 mVpp, steady state"
    ctl = "set wr_singlescale\ntran 200n 8m\nwrdata meas.txt v(out)\n"
    d, _ = run_measurement(nl, ctl)
    t, (vout,) = read_wrdata(d / "meas.txt", 1)
    win = _window(t, vout, 0.8)
    v_avg = _mean(win)
    ripple = max(win) - min(win)
    err = (v_avg - 5.0) / 5.0
    ok = abs(err) <= 0.03 and ripple <= 0.1
    return _result(ok, target, f"V(out) avg = {v_avg:.3f} V ({err:+.1%}), ripple = {ripple*1000:.1f} mVpp")


# --------------------------------------------------------- round 2 circuits

def check_rlc_bandpass(nl: str) -> dict:
    ctl = "set wr_singlescale\nac dec 400 100 1meg\nwrdata meas.txt vm(out)\n"
    d, _ = run_measurement(nl, ctl)
    f, (vm,) = read_wrdata(d / "meas.txt", 1)
    peak = max(vm)
    i0 = vm.index(peak)
    f0 = f[i0]
    level = peak / 2 ** 0.5  # -3 dB
    f_lo = crossing(f[: i0 + 1], vm[: i0 + 1], level, rising=True)
    f_hi = crossing(f[i0:], vm[i0:], level, rising=False)
    if f_lo is None or f_hi is None:
        raise RuntimeError("could not find both -3 dB band edges around the peak")
    q = f0 / (f_hi - f_lo)
    err_f0 = (f0 - 10_000.0) / 10_000.0
    err_q = (q - 5.0) / 5.0
    return _multi_result({
        "f0": _subspec(abs(err_f0) <= 0.03, "10 kHz ±3%", f"{f0:.0f} Hz ({err_f0:+.1%})"),
        "Q": _subspec(abs(err_q) <= 0.10, "Q = 5 ±10%", f"{q:.2f} ({err_q:+.1%})"),
    })


def check_rc_highpass(nl: str) -> dict:
    target = "-3 dB cutoff at 2 kHz ±3%"
    ctl = "set wr_singlescale\nac dec 200 10 1meg\nwrdata meas.txt vdb(out)\n"
    d, _ = run_measurement(nl, ctl)
    f, (vdb,) = read_wrdata(d / "meas.txt", 1)
    plateau = max(vdb)  # high-frequency passband
    f3 = crossing(f, vdb, plateau - 3.0, rising=True)
    if f3 is None:
        raise RuntimeError("no rising -3 dB crossing found (not a high-pass response?)")
    err = (f3 - 2000.0) / 2000.0
    return _result(abs(err) <= 0.03, target, f"f(-3dB) = {f3:.1f} Hz ({err:+.1%} vs 2 kHz)")


def check_cs_mosfet_amp(nl: str) -> dict:
    target = "mid-band |gain| = 10 ±10%"
    ctl = "set wr_singlescale\nac dec 20 10 10meg\nwrdata meas.txt vm(out)\n"
    d, _ = run_measurement(nl, ctl)
    _, (vm,) = read_wrdata(d / "meas.txt", 1)
    gain = max(vm)
    err = (gain - 10.0) / 10.0
    return _result(abs(err) <= 0.10, target, f"|gain| = {gain:.2f} ({err:+.1%})")


def check_inv_opamp_gbw(nl: str) -> dict:
    ctl = "set wr_singlescale\nac dec 100 10 10meg\nwrdata meas.txt vm(out)\n"
    d, _ = run_measurement(nl, ctl)
    f, (vm,) = read_wrdata(d / "meas.txt", 1)
    gain = max(vm)  # low-frequency plateau of a low-pass closed-loop response
    err_g = (gain - 20.0) / 20.0
    f3 = crossing(f, vm, gain / 2 ** 0.5, rising=False)
    if f3 is None:
        # No rolloff below 10 MHz -> op-amp model is effectively ideal,
        # violating the finite-GBW requirement.
        bw = _subspec(False, "f(-3dB) ≥ 50 kHz and ≤ 200 kHz (finite GBW ≈ 2 MHz)",
                      "no rolloff below 10 MHz — op-amp model appears ideal (infinite GBW)")
    else:
        bw = _subspec(50_000.0 <= f3 <= 200_000.0,
                      "f(-3dB) ≥ 50 kHz and ≤ 200 kHz (finite GBW ≈ 2 MHz)",
                      f"f(-3dB) = {f3/1000:.1f} kHz")
    return _multi_result({
        "gain": _subspec(abs(err_g) <= 0.05, "|gain| = 20 ±5%", f"|gain| = {gain:.2f} ({err_g:+.1%})"),
        "bandwidth": bw,
    })


def check_bjt_diffamp(nl: str) -> dict:
    import math
    # Differential drive: +0.5 on vinp, 0.5∠180 on vinn -> 1 V differential input
    ctl_d = (
        "alter @vinp[acmag]=0.5\nalter @vinp[acphase]=0\n"
        "alter @vinn[acmag]=0.5\nalter @vinn[acphase]=180\n"
        "set wr_singlescale\nac dec 20 10 1meg\nwrdata meas.txt vm(out)\n"
    )
    d, out_d = run_measurement(nl, ctl_d)
    _, (vm_d,) = read_wrdata(d / "meas.txt", 1)
    ad = max(vm_d)
    idx = vm_d.index(ad)
    # Common-mode drive: both inputs 1∠0
    ctl_c = (
        "alter @vinp[acmag]=1\nalter @vinp[acphase]=0\n"
        "alter @vinn[acmag]=1\nalter @vinn[acphase]=0\n"
        "set wr_singlescale\nac dec 20 10 1meg\nwrdata meas.txt vm(out)\n"
    )
    d2, _ = run_measurement(nl, ctl_c)
    _, (vm_c,) = read_wrdata(d2 / "meas.txt", 1)
    acm = vm_c[idx]
    if acm > 0:
        cmrr_db = 20.0 * math.log10(ad / acm)
        cmrr_str = f"{cmrr_db:.1f} dB (Ad={ad:.2f}, Acm={acm:.4g})"
    else:
        cmrr_db, cmrr_str = 200.0, f">200 dB (Ad={ad:.2f}, Acm≈0)"
    err_ad = (ad - 25.0) / 25.0
    return _multi_result({
        "diff_gain": _subspec(abs(err_ad) <= 0.10, "Ad = 25 ±10%", f"Ad = {ad:.2f} ({err_ad:+.1%})"),
        "cmrr": _subspec(cmrr_db >= 60.0, "CMRR ≥ 60 dB", cmrr_str),
    })


def check_boost_converter(nl: str) -> dict:
    target = "V(out) = 12 V ±3% average, steady state"
    ctl = "set wr_singlescale\ntran 200n 12m\nwrdata meas.txt v(out)\n"
    d, _ = run_measurement(nl, ctl)
    t, (vout,) = read_wrdata(d / "meas.txt", 1)
    win = _window(t, vout, 0.8)
    v_avg = _mean(win)
    ripple = max(win) - min(win)
    err = (v_avg - 12.0) / 12.0
    return _result(abs(err) <= 0.03, target,
                   f"V(out) avg = {v_avg:.3f} V ({err:+.1%}), ripple = {ripple*1000:.1f} mVpp")


def check_ce_amp_bias_and_gain(nl: str) -> dict:
    ctl = (
        "op\nprint v(coll) v(emit)\n"
        "set units=degrees\nset wr_singlescale\n"
        "ac dec 20 10 10meg\nwrdata meas.txt vm(out) vp(out)\n"
    )
    d, out = run_measurement(nl, ctl)
    mc = re.search(r"(?i)v\(coll\)\s*=\s*([\d.eE+-]+)", out)
    me = re.search(r"(?i)v\(emit\)\s*=\s*([\d.eE+-]+)", out)
    if not mc or not me:
        raise RuntimeError("could not read v(coll)/v(emit) from op output (node naming?)")
    vce = float(mc.group(1)) - float(me.group(1))
    err_v = (vce - 6.0) / 6.0
    _, (vm, vp) = read_wrdata(d / "meas.txt", 2)
    gain = max(vm)
    ph = vp[vm.index(gain)]
    if max(abs(p) for p in vp) < 3.5:
        ph = ph * 180.0 / 3.141592653589793
    inverted = 90.0 <= abs(ph) % 360.0 <= 270.0
    err_g = (gain - 80.0) / 80.0
    return _multi_result({
        "vce": _subspec(abs(err_v) <= 0.10, "Vce = 6 V ±10%", f"Vce = {vce:.2f} V ({err_v:+.1%})"),
        "gain": _subspec(abs(err_g) <= 0.10 and inverted, "|gain| = 80 ±10%, inverting",
                         f"|gain| = {gain:.2f} ({err_g:+.1%}), phase = {ph:.0f} deg"),
    })


CHECKS = {
    "rc_lowpass": check_rc_lowpass,
    "rl_step": check_rl_step,
    "voltage_divider": check_voltage_divider,
    "halfwave_rectifier": check_halfwave_rectifier,
    "ce_bjt_amp": check_ce_bjt_amp,
    "noninv_opamp": check_noninv_opamp,
    "bridge_rectifier": check_bridge_rectifier,
    "buck_converter": check_buck_converter,
    # round 2
    "rlc_bandpass": check_rlc_bandpass,
    "rc_highpass": check_rc_highpass,
    "cs_mosfet_amp": check_cs_mosfet_amp,
    "inv_opamp_gbw": check_inv_opamp_gbw,
    "bjt_diffamp": check_bjt_diffamp,
    "boost_converter": check_boost_converter,
    "ce_amp_bias_and_gain": check_ce_amp_bias_and_gain,
}

TARGETS = {
    "rc_lowpass": "-3 dB point at 1 kHz ±5%",
    "rl_step": "time constant within 5% of L/R",
    "voltage_divider": "V(out) = 3.3 V ±1%",
    "halfwave_rectifier": "peak drop ≤ 1 diode drop; output never negative",
    "ce_bjt_amp": "mid-band |gain| = 50 ±5%, inverting",
    "noninv_opamp": "gain = 11 ±2%",
    "bridge_rectifier": "ripple < 1.0 Vpp",
    "buck_converter": "V(out) = 5 V ±3%, ripple ≤ 100 mVpp",
    # round 2
    "rlc_bandpass": "f0 = 10 kHz ±3% AND Q = 5 ±10%",
    "rc_highpass": "-3 dB cutoff at 2 kHz ±3%",
    "cs_mosfet_amp": "|gain| = 10 ±10%",
    "inv_opamp_gbw": "|gain| = 20 ±5% AND f(-3dB) 50–200 kHz (finite GBW)",
    "bjt_diffamp": "Ad = 25 ±10% AND CMRR ≥ 60 dB",
    "boost_converter": "V(out) = 12 V ±3%",
    "ce_amp_bias_and_gain": "Vce = 6 V ±10% AND |gain| = 80 ±10%",
}


def check(circuit_id: str, netlist: str) -> dict:
    """Run the spec check for one circuit; never raises."""
    fn = CHECKS.get(circuit_id)
    if fn is None:
        return {"pass": None, "target": None, "measured": None,
                "error": f"no spec defined for {circuit_id!r}"}
    try:
        return fn(netlist)
    except Exception as exc:
        return {"pass": False, "target": TARGETS.get(circuit_id),
                "measured": None, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------- CLI

def _cli(argv=None) -> int:
    """Verify one or more decks against a spec.

    The spec is either a declarative file (works on any circuit) or the name
    of one of the benchmark's built-in checks.
    """
    import argparse
    import specs as specfile

    ap = argparse.ArgumentParser(
        prog="speccheck",
        description="Measure an ngspice deck and check it against its spec.")
    ap.add_argument("decks", nargs="+", help="netlist file(s) to verify")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--spec", help="declarative spec file (.yaml/.json)")
    group.add_argument("--circuit", choices=sorted(CHECKS),
                       help="name of a built-in benchmark check")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also print informational measurements")
    args = ap.parse_args(argv)

    spec = specfile.load(args.spec) if args.spec else None
    rc = 0
    for name in args.decks:
        path = Path(name)
        if not path.exists():
            print(f"not found: {path}")
            rc = 1
            continue
        deck = path.read_text(encoding="utf-8")
        result = specfile.verdict(spec, deck) if spec else check(args.circuit, deck)

        verdict_text = "PASS" if result["pass"] else "FAIL"
        print(f"{path}: {verdict_text}")
        if result.get("target"):
            print(f"  spec     {result['target']}")
        if result.get("measured"):
            print(f"  measured {result['measured']}")
        if args.verbose and result.get("informational"):
            for label, row in result["informational"].items():
                print(f"  ({label} = {row['measured']})")
        if result.get("error"):
            print(f"  error    {result['error']}")
        if not result["pass"]:
            rc = 1
    return rc


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
