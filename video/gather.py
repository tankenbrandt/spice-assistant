#!/usr/bin/env python3
"""Run the real tools on the EE 346 Exp. 4 clipper and save what the video shows.

Nothing in the video is typed in by hand: every waveform, number and spread is
read from this file, which is written by the project's own code (specs.py for
the measurements, robustness.py for Monte Carlo, ltspice.py via crosscheck.py
for the second simulator). Free to run; no API calls.

    python video/gather.py        # -> video/assets/data.json, lab_clipper.svg
"""

import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import robustness  # noqa: E402
import specs  # noqa: E402

DECK = ROOT / "reference" / "lab_clipper.cir"
SPEC = ROOT / "specs_lib" / "lab_clipper.yaml"
OUT = HERE / "assets"


def decimate(xs, ys, n):
    step = max(1, len(xs) // n)
    return [round(v, 6) for v in xs[::step]], [round(v, 4) for v in ys[::step]]


def trace(spec, netlist, n=420):
    xs, vecs = specs._run_analysis(netlist, spec.analyses["tran"])
    t, vin = decimate(xs, vecs["v(in)"], n)
    _, vout = decimate(xs, vecs["v(out)"], n)
    return {"t": t, "vin": vin, "vout": vout}


def with_batteries(netlist, volts):
    nl = re.sub(r"^V1 n1 0 DC [\d.]+", f"V1 n1 0 DC {volts}", netlist, flags=re.M)
    return re.sub(r"^V2 0 n2 DC [\d.]+", f"V2 0 n2 DC {volts}", nl, flags=re.M)


def main():
    OUT.mkdir(exist_ok=True)
    spec = specs.load(SPEC)
    deck = DECK.read_text(encoding="utf-8")

    good = specs.verdict(spec, deck)
    miswired = with_batteries(deck, 0)
    bad = specs.verdict(spec, miswired)

    # The textbook answer: ideal diodes clip at the battery voltage.
    ideal = {"vmax": 1.5, "vmin": -1.5}

    # Which knob? Sweep the battery voltage and read the clip level.
    sweep = []
    for v in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]:
        m = specs.measure(spec, with_batteries(deck, v))
        sweep.append({"v1": v, "vmax": round(m["vmax"], 4)})

    mc = robustness.monte_carlo(spec, deck, 200, seed=1)
    samples = [round(s["metrics"]["vmax"], 4) for s in mc["samples"]]

    sens = robustness.sensitivity(spec, deck)

    xc = subprocess.run(
        [sys.executable, str(ROOT / "crosscheck.py"), str(DECK), "--spec", str(SPEC)],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}).stdout
    cross = {}
    for m in re.finditer(r"(vmax|vmin)\s+ngspice=(\S+)\s+ltspice=(\S+)\s+(\S+)%", xc):
        cross[m.group(1)] = {"ngspice": float(m.group(2)), "ltspice": float(m.group(3)),
                             "diff_pct": float(m.group(4))}

    data = {
        "circuit": "EE 346 Exp. 4, Fig. 1: double diode clipper",
        "good": {"trace": trace(spec, deck)},
        "miswired": {"trace": trace(spec, miswired)},
        "measures": {"good": specs.measure(spec, deck), "miswired": specs.measure(spec, miswired)},
        "ideal": ideal,
        "limits": {"min": 1.7, "max": 2.4},
        "sweep": sweep,
        "mc": {"n": len(samples), "vmax": samples,
               "yield": mc["yield"], "mean": mc["metrics"]["vmax"]["mean"],
               "sigma": mc["metrics"]["vmax"]["sigma"], "cpk": mc["metrics"]["vmax"]["cpk"]},
        "sens_vmax": [{"param": r["param"], "S": r["metrics"]["vmax"]["S"],
                       "span_pct": r["metrics"]["vmax"]["span_pct"]} for r in sens["rows"]],
        "crosscheck": cross,
    }
    blob = json.dumps(data, default=str)
    (OUT / "data.json").write_text(blob, encoding="utf-8")
    (OUT / "data.js").write_text("window.DATA = " + blob + ";" + chr(10), encoding="utf-8")
    (OUT / "clipper_no_batteries.cir").write_text(miswired, encoding="utf-8")

    asc = OUT / "lab_clipper.asc"
    subprocess.run([sys.executable, str(ROOT / "ascgen.py"), str(DECK), "-o", str(asc)], check=True)
    subprocess.run([sys.executable, str(ROOT / "ascview.py"), str(asc), "--format", "svg",
                    "--theme", "light", "-o", str(OUT / "lab_clipper.svg")], check=True)
    print("wrote", OUT / "data.json")
    print("good:", data["measures"]["good"], "miswired:", data["measures"]["miswired"])
    print("sweep:", sweep)
    print("mc yield:", data["mc"]["yield"], "n:", len(samples),
          "min/max vmax:", min(samples), max(samples))
    print("crosscheck:", cross)


if __name__ == "__main__":
    main()
