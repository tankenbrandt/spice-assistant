#!/usr/bin/env python3
"""One circuit, every layer, one report.

The pieces of this project each answer one question and print to a terminal.
That is the right shape for a test and the wrong shape for handing someone a
design. This runs the whole chain against a single circuit and writes a report
you can read, attach to a lab notebook, or bring to a design review:

    schematic -> operating point -> spec verdict -> Monte Carlo -> second simulator

    python report.py repaired/ce_bjt_amp.cir --spec specs_lib/ce_bjt_amp.yaml --out runs/amp
    python report.py --describe "a common-emitter amplifier with gain -50" \
        --spec specs_lib/ce_bjt_amp.yaml --out runs/amp

Only `--describe` costs anything: it is the one layer that calls the model. Give
it a deck instead and the entire report runs on ngspice (plus LTspice, if it is
installed) for free.

Everything lands in `--out`, which must not be the project root -- `report.md`
and the committed `REPORT.md` are the same file on a case-insensitive
filesystem, and the benchmark's report is not this tool's to overwrite.
"""

import argparse
import datetime
import sys
from pathlib import Path

import ascgen
import ascview
import oppoint
import robustness
import specs as specfile
from plot import eng

MC_DEFAULT = 200


class Step:
    """A named stage that records what happened rather than aborting the run.

    A report is most useful on the circuit that is going wrong, so one layer
    failing -- LTspice missing, a deck too exotic to draw -- must not cost the
    reader the layers that did work.
    """

    def __init__(self, label: str):
        self.label = label
        self.note = ""
        self.error = ""
        self.value = None

    def ok(self, value, note: str = "") -> "Step":
        self.value, self.note = value, note
        return self

    def failed(self, exc: BaseException) -> "Step":
        self.error = f"{type(exc).__name__}: {exc}"
        return self

    def skipped(self, why: str) -> "Step":
        self.note = why
        return self

    @property
    def status(self) -> str:
        if self.error:
            return f"error - {self.error}"
        return self.note or ("ok" if self.value is not None else "skipped")


def announce(step: Step, index: int, total: int) -> Step:
    print(f"  [{index}/{total}] {step.label:.<28} {step.status}", file=sys.stderr)
    return step


# ------------------------------------------------------------------- the layers

def generate(description: str, spec=None) -> tuple[str, str]:
    """Description -> a deck that simulates, repairing simulation errors.

    With a spec, "simulates" is not the finish line: the deck is designed to
    the spec and repaired until it meets it (see `design.py`).
    """
    import main

    main.load_dotenv()
    if spec is not None:
        import design

        result = design.design(description, spec,
                               log=lambda m: print(m, file=sys.stderr, flush=True))
        state = "meets spec" if result.passed else "still misses spec"
        return result.netlist, f"{state} after {result.repairs} spec repair(s)"
    netlist = main.generate_netlist(description)
    for attempt in range(1, main.MAX_ATTEMPTS + 1):
        ok, output = main.run_ngspice(netlist)
        if ok:
            return netlist, f"ok ({attempt} attempt{'s' if attempt > 1 else ''})"
        if attempt == main.MAX_ATTEMPTS:
            raise RuntimeError(f"still failing after {attempt} attempts:\n{output}")
        netlist = main.fix_netlist(netlist, output)
    raise AssertionError("unreachable")     # pragma: no cover


def draw(netlist: str, out: Path, stem: str) -> dict:
    """Write the deck out as an LTspice schematic, and as an SVG of that sheet."""
    asc = ascgen.generate(netlist)
    asc_path = out / f"{stem}.asc"
    asc_path.write_text(asc, encoding="utf-8")
    sch = ascview.parse_asc(asc_path)
    svg_path = out / f"{stem}.svg"
    svg_path.write_text(ascview.render_svg(sch), encoding="utf-8")
    return {"asc": asc_path, "svg": svg_path, "devices": len(sch.symbols)}


def monte_carlo(spec, netlist: str, n: int, seed: int, workers: int) -> dict:
    return robustness.monte_carlo(spec, netlist, n, seed=seed, workers=workers)


def cross_check(spec, netlist: str) -> list:
    import crosscheck
    import ltspice

    if not ltspice.available():
        raise RuntimeError("LTspice is not installed here")
    return crosscheck.compare(spec, netlist)


# --------------------------------------------------------------------- the report

def cell(text) -> str:
    """Escape a value for a Markdown table cell.

    Measurement titles are things like `|gain|`, whose pipes would otherwise
    split the row into extra columns. ROBUSTNESS.md escapes them the same way.
    """
    return str(text).replace("|", r"\|")


def _verdict_table(verdict: dict) -> list:
    rows = ["| Measurement | Target | Measured | |", "|---|---|---|---|"]
    for title, r in verdict.get("specs", {}).items():
        rows.append(f"| {cell(title)} | {cell(r['target'])} | {cell(r['measured'])} | "
                    f"{'PASS' if r['pass'] else '**FAIL**'} |")
    for title, r in verdict.get("informational", {}).items():
        rows.append(f"| {cell(title)} | (informational) | {cell(r['measured'])} | - |")
    return rows


def _mc_table(mc: dict) -> list:
    rows = ["| Measurement | Mean | Sigma | Min ... Max | In spec | Cpk |",
            "|---|---|---|---|---|---|"]
    for m in mc["metrics"].values():
        cpk = m["cpk"]
        rows.append(
            f"| {cell(m['label'])} | {eng(m['mean'])} | {eng(m['sigma'])} | "
            f"{eng(m['min'])} ... {eng(m['max'])} | {m['pass_rate']:.0%} | "
            f"{'-' if cpk is None else f'{cpk:.2f}'} |")
    return rows


def _cross_table(rows: list, tol: float) -> list:
    out = ["| Measurement | ngspice | LTspice | Difference | |", "|---|---|---|---|---|"]
    for r in rows:
        mark = "agree" if r.agrees(tol) else ("informational" if r.informational
                                              else "**differ**")
        out.append(f"| {cell(r.name)} | {r.ngspice:.6g} | {r.ltspice:.6g} | "
                   f"{r.diff_pct:.3f}% | {mark} |")
    return out


def compose(title: str, netlist: str, steps: dict, spec, mc_n: int, tol: float) -> str:
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    L = [f"# {title}", "",
         f"- **Generated:** {stamp}",
         f"- **Source:** {steps['netlist'].note}"]
    if spec is not None:
        L.append(f"- **Spec:** {specfile.describe(spec)}")
    L.append("")

    L.append("## Summary")
    L.append("")
    L.append("| Layer | Result |")
    L.append("|---|---|")
    for step in steps.values():
        L.append(f"| {step.label} | {cell(step.status)} |")
    L.append("")

    schematic = steps["schematic"]
    if schematic.value:
        L += ["## Schematic", "",
              f"![schematic]({schematic.value['svg'].name})", "",
              f"Also written as `{schematic.value['asc'].name}`, which opens in "
              "LTspice unchanged.", ""]

    op = steps["operating point"]
    if op.value:
        L += ["## " + oppoint.markdown(op.value, "Operating point")[4:], ""]

    verdict = steps["spec check"]
    if verdict.value:
        L += ["## Does it meet its spec?", "",
              f"**{'PASS' if verdict.value['pass'] else 'FAIL'}** at nominal "
              "component values, 27 C, exact supply.", ""]
        L += _verdict_table(verdict.value)
        L.append("")

    mc = steps["monte carlo"]
    if mc.value:
        L += ["## Does it stay correct once the parts have tolerances?", "",
              f"{mc_n} samples over {len(mc.value['params'])} perturbable "
              f"parameters ({mc.value['dist']}, seed {mc.value['seed']}), "
              "including temperature.", "",
              f"**Yield: {mc.value['yield']:.0%}** -- the share of samples "
              "meeting *every* spec at once.", ""]
        L += _mc_table(mc.value)
        L.append("")

    cross = steps["ltspice cross-check"]
    if cross.value:
        agree = sum(r.agrees(tol) for r in cross.value)
        L += ["## Does a second simulator agree?", "",
              f"The same spec, measured again in LTspice: {agree}/"
              f"{len(cross.value)} within {tol}%.", ""]
        L += _cross_table(cross.value, tol)
        L.append("")

    L += ["## The deck", "", "```spice", netlist.strip(), "```", ""]
    failures = [s for s in steps.values() if s.error]
    if failures:
        L += ["## Layers that did not run", ""]
        L += [f"- **{s.label}**: {s.error}" for s in failures]
        L.append("")
    return "\n".join(L)


# ------------------------------------------------------------------------- CLI

def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="spice-report",
        description="Run every layer against one circuit and write a report.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("deck", nargs="?", help="netlist file")
    src.add_argument("--describe", metavar="TEXT",
                     help="generate the deck from a description (needs an API key)")
    ap.add_argument("--spec", help="spec file; without one, the report stops "
                                   "after the operating point")
    ap.add_argument("--out", required=True, metavar="DIR",
                    help="directory for the report and its schematic")
    ap.add_argument("--mc", type=int, default=MC_DEFAULT, metavar="N",
                    help=f"Monte Carlo samples, 0 to skip (default {MC_DEFAULT})")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tol", type=float, default=1.0,
                    help="cross-simulator agreement threshold, percent")
    ap.add_argument("--no-ltspice", action="store_true",
                    help="skip the second-simulator check")
    args = ap.parse_args(argv)

    out = Path(args.out).resolve()
    if out == Path(__file__).resolve().parent:
        print("--out must not be the project root: report.md would overwrite the "
              "benchmark's REPORT.md on a case-insensitive filesystem.",
              file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)

    spec = None
    if args.spec:
        try:
            spec = specfile.load(args.spec)
        except Exception as exc:            # noqa: BLE001 - a bad spec is user error
            print(f"{args.spec}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    steps = {label: Step(label) for label in
             ("netlist", "schematic", "operating point", "spec check",
              "monte carlo", "ltspice cross-check")}
    total = len(steps)
    print(f"spice-report -> {out}", file=sys.stderr)

    # 1. the deck
    step = steps["netlist"]
    if args.describe:
        try:
            netlist, note = generate(args.describe, spec)
            step.ok(netlist, note)
        except Exception as exc:            # noqa: BLE001 - reported, not raised
            announce(step.failed(exc), 1, total)
            return 1
        step.note = f"generated from {args.describe!r} ({note})"
        stem = "circuit"
    else:
        path = Path(args.deck)
        if not path.exists():
            print(f"not found: {path}", file=sys.stderr)
            return 2
        netlist = path.read_text(encoding="utf-8", errors="replace")
        step.ok(netlist, f"`{path.as_posix()}`")
        stem = path.stem
    announce(step, 1, total)

    # 2. schematic
    step = steps["schematic"]
    try:
        drawn = draw(netlist, out, stem)
        step.ok(drawn, f"{drawn['devices']} devices -> {drawn['svg'].name}")
    except Exception as exc:                # noqa: BLE001 - reported, not raised
        step.failed(exc)
    announce(step, 2, total)

    # 3. operating point
    step = steps["operating point"]
    try:
        op = oppoint.operating_point(netlist)
        step.ok(op, f"{len(op.nodes)} nodes, {op.branches} branches")
    except Exception as exc:                # noqa: BLE001 - reported, not raised
        step.failed(exc)
    announce(step, 3, total)

    # 4. spec verdict
    step = steps["spec check"]
    if spec is None:
        step.skipped("no --spec given")
    else:
        verdict = specfile.verdict(spec, netlist)
        if verdict.get("error"):
            step.error = verdict["error"]
        else:
            step.ok(verdict, "PASS" if verdict["pass"] else "FAIL")
    announce(step, 4, total)

    # 5. monte carlo
    step = steps["monte carlo"]
    if spec is None or args.mc <= 0:
        step.skipped("no --spec given" if spec is None else "--mc 0")
    else:
        try:
            mc = monte_carlo(spec, netlist, args.mc, args.seed, args.workers)
            step.ok(mc, f"yield {mc['yield']:.0%} over {args.mc} samples")
        except Exception as exc:            # noqa: BLE001 - reported, not raised
            step.failed(exc)
    announce(step, 5, total)

    # 6. second simulator
    step = steps["ltspice cross-check"]
    if spec is None or args.no_ltspice:
        step.skipped("no --spec given" if spec is None else "--no-ltspice")
    else:
        try:
            rows = cross_check(spec, netlist)
            agree = sum(r.agrees(args.tol) for r in rows)
            step.ok(rows, f"{agree}/{len(rows)} agree within {args.tol}%")
        except Exception as exc:            # noqa: BLE001 - LTspice is optional
            step.failed(exc)
    announce(step, 6, total)

    title = f"{stem} - design report"
    path = out / "report.md"
    path.write_text(compose(title, netlist, steps, spec, args.mc, args.tol),
                    encoding="utf-8")
    print(f"\nwrote {path}", file=sys.stderr)
    return 0 if not steps["netlist"].error else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
