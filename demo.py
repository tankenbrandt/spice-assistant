#!/usr/bin/env python3
"""A sixty-second tour of the project, with no API key and no network.

Every layer except generation runs on ngspice (and LTspice, if installed), so
the whole story can be shown offline on a borrowed laptop:

    python demo.py             # the tour
    python demo.py --fast      # skip the LTspice cross-check

It narrates one circuit the whole way through -- a common-emitter amplifier
that simulates perfectly and is wrong -- because that is the project's point:
"it ran" is not "it works".
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
PAUSE = 0.0


def rule(title: str) -> None:
    width = min(shutil.get_terminal_size((80, 20)).columns, 78)
    print("\n" + "=" * width)
    print(f"  {title}")
    print("=" * width)


def say(text: str) -> None:
    print(text)
    time.sleep(PAUSE)


def run(args: list, expect_fail: bool = False) -> int:
    """Run one of the project's own CLIs and show it working.

    The child is pinned to UTF-8 both ways. Left to the Windows default it
    writes UTF-8 and is decoded as cp1252, which turns the plus-minus in a
    tolerance into a replacement character.
    """
    print(f"\n$ python {' '.join(str(a) for a in args)}\n")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([PY, *[str(a) for a in args]], cwd=HERE, env=env,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out = (proc.stdout + proc.stderr).rstrip()
    print(out)
    if proc.returncode and not expect_fail:
        print(f"  [exit {proc.returncode}]")
    time.sleep(PAUSE)
    return proc.returncode


def main(argv=None) -> int:
    global PAUSE
    ap = argparse.ArgumentParser(description="Offline tour of spice-assistant.")
    ap.add_argument("--fast", action="store_true", help="skip the LTspice cross-check")
    ap.add_argument("--pause", type=float, default=0.6,
                    help="seconds between steps (0 for no pauses)")
    args = ap.parse_args(argv)
    PAUSE = args.pause

    rule("spice-assistant - natural language to a verified analog circuit")
    say("""
Three questions, three different answers. No API key is needed for any of
this: generation is the only layer that costs anything, and its output is
already committed to baseline/ and repaired/.
""".strip())

    rule("1. Does it simulate?  (the bar most tools stop at)")
    say("The model's own output for 'a common-emitter amplifier with gain -50'.")
    run(["speccheck.py", "--circuit", "ce_bjt_amp", "baseline/ce_bjt_amp.cir"],
        expect_fail=True)
    say("""
It simulates cleanly. ngspice is perfectly happy with it. And the gain is
107 V/V against a spec of 50 -- more than twice what was asked for. A
"did it run?" check cannot see this.
""".strip())

    rule("2. Does it meet its spec?  (after spec-in-the-loop repair)")
    run(["speccheck.py", "--circuit", "ce_bjt_amp", "repaired/ce_bjt_amp.cir"])
    say("Same topology, measured against the same spec, now inside the band.")

    rule("3. Does it stay correct once the parts have tolerances?")
    say("""
Nominal-correct is not manufacturable. The robustness layer runs Monte Carlo
over component tolerances, supply and temperature. Full study: ROBUSTNESS.md
(the worst circuit in the benchmark had a 1% yield before repair).
""".strip())

    if not args.fast:
        rule("4. Does a second, independent simulator agree?")
        say("""
Everything so far is ngspice's opinion. LTspice is a separately written,
closed-source simulator -- and it runs headless, so this is a test rather
than a manual step.
""".strip())
        code = run(["crosscheck.py", "repaired/ce_bjt_amp.cir",
                    "--spec", "specs_lib/ce_bjt_amp.yaml"], expect_fail=True)
        if code == 2:
            say("(LTspice is not installed here, so this layer sat out.)")
        else:
            say("Two independently written simulators, agreeing to six figures.")

    rule("What is on disk")
    say("""
  REPORT.md       the benchmark: 100% simulate, 62% meet spec
  ROBUSTNESS.md   Monte Carlo / sensitivity / worst-case / PVT
  examples/       one circuit as schematic, netlist, spec and plot
  docs/           per-round analysis

  python benchmark.py --report-only        regenerates REPORT.md from logs/
  python crosscheck.py --all               every spec, both simulators
  python ascview.py examples/active_lowpass.asc --png --open
  pytest                                   440 tests, no API calls
""".rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
