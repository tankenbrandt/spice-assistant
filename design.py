#!/usr/bin/env python3
"""Design to a spec: generate, simulate, measure, repair -- until it passes.

`main.py` stops at "it simulates". The benchmark showed why that is not enough:
every generated deck ran, and a quarter of them missed their spec -- a
common-emitter stage with twice its gain, a buck converter half a volt low. Its
spec-in-the-loop repair fixed all of them in one round, but that loop lived
inside the benchmark, wired to hand-written checks. This is the same loop for
any spec file in `specs_lib/`, and for a deck you already have:

    python design.py "a common-emitter amplifier, gain -50" \
        --spec specs_lib/ce_bjt_amp.yaml -o amp.cir
    python design.py --deck my_buck.cir --spec specs_lib/buck_converter.yaml -o fixed.cir

Two things differ from the benchmark's loop, both aimed at the first answer
rather than the repair:

* The spec goes to the model *before* it designs -- the acceptance limits and
  which vectors the harness will read -- so it is designing to a number rather
  than to a paragraph.
* A repair gets the DC operating point alongside the measured value. A gain of
  107 says the design is wrong; `V(e) = 0.002 V` says the emitter resistor is
  bypassed, which is the thing to change.

Every model call is billed; a deck that already passes costs nothing past
generation, and `--rounds` caps the rest.
"""

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import main
import oppoint
import specs as specfile

REPAIR_ROUNDS = 3


def spec_brief(spec: specfile.CircuitSpec) -> str:
    """What the model has to know to be measured: limits, analyses, vectors."""
    lines = ["Acceptance criteria (the design is measured against these):"]
    lines += [f"- {m.title}: {m.limits.describe()}" for m in spec.specs]
    lines.append("")
    lines.append("The test harness replaces your .control block with its own "
                 "analyses and reads these vectors, so the nodes and sources "
                 "they name must exist in the deck:")
    for a in spec.analyses.values():
        vecs = ", ".join(a.vectors) or "(none)"
        lines.append(f"- `{a.command}` reading {vecs}")
    if any(a.command.split()[0].lower() == "ac" for a in spec.analyses.values()):
        lines.append("An AC analysis needs a source with an AC magnitude "
                     "(e.g. `AC 1`) driving the input.")
    return "\n".join(lines)


def bias_matters(spec: specfile.CircuitSpec) -> bool:
    """Whether the DC operating point explains this spec's measurements.

    It does for a small-signal or DC spec -- an amplifier's gain is set by its
    bias. For a transient-only spec (a converter, a rectifier) the DC point is
    the circuit at t=0 with its switch open and its sine at zero: an output of
    0 V that is true and useless, and worse than nothing in a repair prompt.
    """
    return any(a.command.split()[0].lower() in ("ac", "dc", "op", "noise")
               for a in spec.analyses.values())


def _op_summary(netlist: str) -> str | None:
    """The operating point as text, or None if the deck will not solve one."""
    try:
        return oppoint.text(oppoint.operating_point(netlist))
    except Exception:                       # noqa: BLE001 - optional context
        return None


@dataclass
class Round:
    """One measurement of the deck, and what the model was asked to change."""
    netlist: str
    verdict: dict
    sim_attempts: int = 1


@dataclass
class Result:
    netlist: str
    passed: bool
    rounds: list = field(default_factory=list)

    @property
    def repairs(self) -> int:
        return max(len(self.rounds) - 1, 0)


def simulate_clean(netlist: str, log=print) -> tuple[str, int]:
    """Repair simulation errors until the deck runs; returns (deck, attempts)."""
    for attempt in range(1, main.MAX_ATTEMPTS + 1):
        ok, output = main.run_ngspice(netlist)
        if ok:
            return netlist, attempt
        if attempt == main.MAX_ATTEMPTS:
            raise RuntimeError(f"still failing to simulate after {attempt} "
                               f"attempts:\n{output.strip()[-1500:]}")
        log(f"  simulation failed (attempt {attempt}); asking for a fix")
        netlist = main.fix_netlist(netlist, output)
    raise AssertionError("unreachable")     # pragma: no cover


def repair_to_spec(netlist: str, spec: specfile.CircuitSpec,
                   rounds: int = REPAIR_ROUNDS, log=print,
                   requirements: str | None = None) -> Result:
    """Measure `netlist` against `spec`; repair it until it passes or `rounds` run out."""
    netlist, attempts = simulate_clean(netlist, log)
    result = Result(netlist=netlist, passed=False)
    for k in range(rounds + 1):
        v = specfile.verdict(spec, netlist)
        result.rounds.append(Round(netlist, v, attempts))
        result.netlist, result.passed = netlist, v["pass"]
        measured = v["measured"] or v["error"]
        log(f"  {'PASS' if v['pass'] else 'FAIL'}: {measured}")
        if v["pass"] or k == rounds:
            break
        log(f"  spec repair {k + 1}/{rounds}")
        netlist = main.fix_netlist_spec(
            netlist, spec_brief(spec), measured,
            operating_point=_op_summary(netlist) if bias_matters(spec) else None,
            requirements=requirements)
        netlist, attempts = simulate_clean(netlist, log)
    return result


def design(description: str, spec: specfile.CircuitSpec,
           rounds: int = REPAIR_ROUNDS, log=print) -> Result:
    """Description + spec -> a deck that meets the spec, or the closest attempt."""
    log("  generating")
    netlist = main.generate_netlist(f"{description}\n\n{spec_brief(spec)}")
    return repair_to_spec(netlist, spec, rounds, log, requirements=description)


# ------------------------------------------------------------------------ CLI

def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="spice-design",
        description="Generate or repair a deck until it meets its spec.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("description", nargs="?", help="what to design")
    src.add_argument("--deck", help="repair an existing netlist instead")
    ap.add_argument("--spec", required=True, help="spec file (see specs_lib/)")
    ap.add_argument("-o", "--out", help="write the final deck here")
    ap.add_argument("--rounds", type=int, default=REPAIR_ROUNDS,
                    help=f"max spec-repair rounds (default {REPAIR_ROUNDS}); "
                         "each one is an API call")
    args = ap.parse_args(argv)

    try:
        spec = specfile.load(args.spec)
    except Exception as exc:                # noqa: BLE001 - a bad spec is user error
        print(f"{args.spec}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.deck and not Path(args.deck).exists():
        print(f"not found: {args.deck}", file=sys.stderr)
        return 2

    main.load_dotenv()
    if not main.os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set (export it, or put it in .env).",
              file=sys.stderr)
        return 2

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    try:
        if args.deck:
            deck = Path(args.deck).read_text(encoding="utf-8", errors="replace")
            result = repair_to_spec(deck, spec, args.rounds, log)
        else:
            result = design(args.description, spec, args.rounds, log)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.out:
        Path(args.out).write_text(result.netlist, encoding="utf-8")
        log(f"wrote {args.out}")
    else:
        print(result.netlist.rstrip("\n"))
    u = main.USAGE
    log(f"{'PASS' if result.passed else 'FAIL'} after {result.repairs} spec "
        f"repair(s); {u['calls']} API calls, {u['input_tokens']} in / "
        f"{u['output_tokens']} out tokens")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(_cli())
