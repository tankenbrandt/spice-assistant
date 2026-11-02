#!/usr/bin/env python3
"""The simulate / design / test-plan sections of a lab report, from one deck.

Give it the handout's circuit with its theory values and a spec saying what
the circuit should do. It writes the three pre-lab sections a report asks for:

* **Simulation** -- the ideal circuit simulated, theory beside simulation.
* **Design** -- the real parts from the kit (`kit.py`), what they change, and a
  Monte Carlo run over the kit's tolerances: how far a real build can land.
* **Test plan** -- equipment, a part check, DC checkpoints, a step per
  measurement with its expected reading, and a results table with an empty
  "measured" column to fill in at the bench.

    python labreport.py reference/inv_active_lowpass.cir \\
        --spec specs_lib/inv_active_lowpass.yaml --title "Lab 5: Active filters" \\
        --out runs/lab5

Writes report.docx (open it in Word, or upload it to Google Drive, which turns
it into a Google Doc), report.md, the kit-build deck and the figures. Every
number in it comes from ngspice -- no API calls.
"""

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import kit as kitmod
import oppoint
import plot
import robustness
import specs as specfile

UNICODE_UNITS = {"r": "Ω", "c": "F", "l": "H"}
INTERNAL_NODE = re.compile(r"\.")          # xu1.pole: inside a model, not probeable


# ------------------------------------------------------------------ formatting

def value(v: float, letter: str, sig: int = 3) -> str:
    return f"{plot.eng(v, sig)}{UNICODE_UNITS.get(letter.lower(), '')}"


def part_text(c: kitmod.Choice, letter: str) -> str:
    if c.how == "ideal":
        return f"{value(c.value, letter, 4)} (not in kit)"
    if not c.combo:
        return value(c.parts[0], letter)
    joiner = " + " if c.how == "series" else " ‖ "
    return f"{joiner.join(value(p, letter) for p in c.parts)} in {c.how}"


# Below this a voltage is a numerical zero, not a reading: "2.974e-09 V" on a
# logic low is noise from the solver's tolerances.
NEGLIGIBLE_V = 1e-6


def num(v, units: str = "", sig: int = 4) -> str:
    if v is None:
        return "—"
    if units == "V" and abs(v) < NEGLIGIBLE_V:
        v = 0.0
    return f"{v:.{sig}g}{(' ' + units) if units else ''}"


def theory(m) -> str:
    return m.limits.describe().replace("+/-", "±")


# ----------------------------------------------------------- document blocks
#
# The report is built once as a list of blocks, then rendered to Markdown and
# to .docx, so the two can never say different things.

@dataclass
class Block:
    kind: str                 # h1-h4 p bullets steps table image code
    text: str = ""
    items: list = field(default_factory=list)
    header: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    path: Path | None = None


def _md_cell(s) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def render_md(blocks: list, rel_to: Path) -> str:
    out = []
    for b in blocks:
        if b.kind in ("h1", "h2", "h3", "h4"):
            out.append("#" * int(b.kind[1]) + " " + b.text)
        elif b.kind == "p":
            out.append(b.text)
        elif b.kind == "bullets":
            out.append("\n".join(f"- {i}" for i in b.items))
        elif b.kind == "steps":
            out.append("\n".join(f"{n}. {i}" for n, i in enumerate(b.items, 1)))
        elif b.kind == "table":
            out.append("\n".join(
                ["| " + " | ".join(_md_cell(h) for h in b.header) + " |",
                 "|" + "---|" * len(b.header)]
                + ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in b.rows]))
        elif b.kind == "image":
            out.append(f"![{b.text}]({b.path.relative_to(rel_to).as_posix()})\n\n*{b.text}*")
        elif b.kind == "code":
            out.append("```\n" + b.text.rstrip() + "\n```")
    return "\n\n".join(out) + "\n"


def render_docx(blocks: list, path: Path) -> None:
    from docx import Document
    from docx.shared import Inches, Pt

    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)
    for b in blocks:
        if b.kind == "h1":
            doc.add_heading(b.text, level=0)
        elif b.kind in ("h2", "h3", "h4"):
            doc.add_heading(b.text, level=int(b.kind[1]) - 1)
        elif b.kind == "p":
            doc.add_paragraph(b.text)
        elif b.kind in ("bullets", "steps"):
            style = "List Bullet" if b.kind == "bullets" else "List Number"
            for i in b.items:
                doc.add_paragraph(i, style=style)
        elif b.kind == "table":
            t = doc.add_table(rows=1, cols=len(b.header))
            t.style = "Table Grid"
            for cell, h in zip(t.rows[0].cells, b.header, strict=True):
                cell.text = ""
                cell.paragraphs[0].add_run(str(h)).bold = True
            for r in b.rows:
                for cell, c in zip(t.add_row().cells, r, strict=True):
                    cell.text = str(c)
            doc.add_paragraph()
        elif b.kind == "image":
            doc.add_picture(str(b.path), width=Inches(6.0))
            cap = doc.add_paragraph()
            cap.add_run(b.text).italic = True
        elif b.kind == "code":
            p = doc.add_paragraph()
            run = p.add_run(b.text.rstrip())
            run.font.name = "Consolas"
            run.font.size = Pt(9)
    doc.save(str(path))


# ------------------------------------------------------------------- figures

# A transient with this many vectors is a logic timing diagram: one strip per
# signal, stacked on a shared time axis, the way the handouts draw them.
TIMING_MIN_VECTORS = 4


def figures(spec, ideal_deck: str, kit_deck: str | None, out: Path, stem: str) -> list:
    """One PNG per analysis: the ideal trace dashed, the kit build solid.

    `kit_deck=None` means nothing was changed from the ideal circuit, so each
    signal is drawn once.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import EngFormatter

    ideal = plot.capture(ideal_deck, spec.analyses)
    built = plot.capture(kit_deck, spec.analyses) if kit_deck else None
    colors = plot.THEMES["light"]["series"]
    made = []
    for aname, (xs, vecs) in ideal.items():
        if (spec.analyses[aname].command.split()[0].lower() == "tran"
                and len(vecs) >= TIMING_MIN_VECTORS):
            made.append((aname, _timing(plt, EngFormatter, xs, vecs,
                                        built[aname] if built else None,
                                        out / f"{stem}_{aname}.png")))
            continue
        groups: dict = {}
        for vname in vecs:
            groups.setdefault(plot.unit_family(vname), []).append(vname)
        fig, axes = plt.subplots(len(groups), 1, figsize=(6.5, 2.6 * len(groups)),
                                 sharex=True, squeeze=False)
        xlog = plot.aname_is_swept_log(aname, spec)
        for ax, ((family, unit), names) in zip(axes[:, 0], groups.items(), strict=True):
            for i, n in enumerate(names):
                col = colors[i % len(colors)]
                if built:
                    bxs, bvecs = built[aname]
                    ax.plot(bxs, bvecs[n], color=col, lw=1.6, label=f"{n} (kit build)")
                    ax.plot(xs, vecs[n], color=col, lw=1.0, ls="--", alpha=0.7,
                            label=f"{n} (ideal)")
                else:
                    ax.plot(xs, vecs[n], color=col, lw=1.6, label=n)
            ax.set_ylabel(f"{family} ({unit})" if unit and unit != family else family)
            ax.grid(True, color="#e1e0d9", lw=0.6)
            ax.legend(fontsize=8, loc="best", frameon=False)
            if xlog:
                ax.set_xscale("log")
        last = axes[-1, 0]
        last.set_xlabel("frequency" if xlog else "time")
        last.xaxis.set_major_formatter(EngFormatter(unit="Hz" if xlog else "s"))
        fig.tight_layout()
        path = out / f"{stem}_{aname}.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        made.append((aname, path))
    return made


def _timing(plt, EngFormatter, xs, vecs, built, path: Path) -> Path:
    names = list(vecs)
    fig, axes = plt.subplots(len(names), 1, figsize=(6.5, 0.85 * len(names) + 0.6),
                             sharex=True, squeeze=False)
    ink = plot.THEMES["light"]["ink"]
    accent = plot.THEMES["light"]["series"][0]
    top = max(max(v) for v in vecs.values())
    rail = max(vecs[names[0]])           # the first strip is a driven input: a clean level
    for ax, n in zip(axes[:, 0], names, strict=True):
        if built:
            ax.plot(built[0], built[1][n], color=accent, lw=1.4)
            ax.plot(xs, vecs[n], color=ink, lw=0.9, ls="--", alpha=0.6)
        else:
            ax.plot(xs, vecs[n], color=accent if n == names[-1] else ink, lw=1.3)
        ax.set_ylim(-0.1 * top, 1.15 * top)
        ax.set_yticks([0, rail])
        ax.yaxis.set_major_formatter(EngFormatter(unit="V", places=0))
        ax.tick_params(labelsize=7)
        ax.set_ylabel(n.upper().removeprefix("V(").removesuffix(")"), rotation=0,
                      ha="right", va="center", fontsize=9)
        ax.grid(True, axis="x", color="#e1e0d9", lw=0.6)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    last = axes[-1, 0]
    last.set_xlabel("time")
    last.xaxis.set_major_formatter(EngFormatter(unit="s"))
    fig.tight_layout(h_pad=0.2)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ------------------------------------------------------------- the deck, read

_SRC_RE = re.compile(r"^\s*(v\S*)\s+(\S+)\s+(\S+)\s+(.*)$", re.I)


def sources(deck: str) -> list[dict]:
    """Independent voltage sources, described the way a bench sets them up."""
    lines = deck.splitlines()
    inside = kitmod._subckt_mask(lines)
    out = []
    for ln, sub in zip(lines, inside, strict=True):
        m = _SRC_RE.match(ln)
        if sub or not m or ln.lstrip().startswith(("*", ".")):
            continue
        name, npos, nneg, rest = m.groups()
        r = rest.lower()
        sin = re.search(r"sin\s*\(([^)]*)\)", r)
        pulse = re.search(r"pulse\s*\(([^)]*)\)", r)
        pwl = re.search(r"pwl\s*\(([^)]*)\)", r)
        if pwl:
            levels = sorted({specfile_value(v) for v in pwl.group(1).split()[1::2]})
            text = ("logic input, switched between "
                    + " and ".join(num(v, "V", 3) for v in levels)
                    + " row by row (DC supply or a jumper to VDD/ground)")
            out.append({"name": name, "kind": "logic", "text": text, "nodes": (npos, nneg)})
        elif sin:
            a = (sin.group(1).split() + ["0"] * 3)[:3]
            off, amp, f = (specfile_value(x) for x in a)
            text = (f"function generator, sine: {num(amp, 'V', 3)} peak "
                    f"({num(2 * amp, 'Vpp', 3)}), {plot.eng(f)}Hz"
                    + (f", {num(off, 'V', 3)} DC offset" if off else ", no DC offset"))
            out.append({"name": name, "kind": "signal", "text": text, "nodes": (npos, nneg)})
        elif pulse:
            a = (pulse.group(1).split() + ["0"] * 7)[:7]
            v1, v2, _, _, _, pw, per = (specfile_value(x) for x in a)
            text = (f"function generator, square/pulse: {num(v1, 'V', 3)} to "
                    f"{num(v2, 'V', 3)}" + (f", {plot.eng(1 / per)}Hz" if per else ""))
            out.append({"name": name, "kind": "signal", "text": text, "nodes": (npos, nneg)})
        elif re.search(r"\bac\b", r):
            out.append({"name": name, "kind": "ac",
                        "text": "function generator, sine: a small amplitude the output "
                                "will not clip at (e.g. 100 mV peak), frequency swept",
                        "nodes": (npos, nneg)})
        else:
            m2 = re.search(r"(?:\bdc\s+)?([-+]?[\d.]+\w*)", rest, re.I)
            try:
                dc = specfile_value(m2.group(1)) if m2 else 0.0
            except ValueError:
                continue
            if dc:
                out.append({"name": name, "kind": "dc", "value": dc,
                            "text": f"DC supply, {num(dc, 'V', 3)} "
                                    f"({npos} to {nneg})", "nodes": (npos, nneg)})
    return out


def specfile_value(tok: str) -> float:
    from speccheck import spice_value
    return spice_value(tok)


def _vector_words(vec: str, sweep: str = "") -> str:
    m = re.match(r"(?i)^(v|vdb|vp|vm|i)\((.+)\)$", vec.strip())
    if not m:
        return vec
    kind, node = m.group(1).lower(), m.group(2)
    if sweep == "ac" and kind in ("v", "vm"):
        # The simulation drives a 1 V AC source, so its V(out) *is* the gain.
        return f"the gain at node {node}, Vout/Vin (output amplitude over input amplitude)"
    return {"v": f"V({node})",
            "vm": f"the amplitude of V({node})",
            "vdb": f"the gain to node {node} in dB, 20·log10(Vout/Vin)",
            "vp": f"the phase of V({node}) relative to the input",
            "i": f"the current through {node}"}[kind]


def instruction(m, spec) -> str:
    """One measurement, as something to do with a scope or meter."""
    if m.bench:
        return f"{m.title}: {m.bench}"
    if m.derived:
        return f"{m.title}: calculate from the readings above ({m.expr})."
    analysis = spec.analyses[m.analysis]
    sweep = analysis.command.split()[0].lower()
    ex = m.extract
    of = _vector_words(ex.get("of", ""), sweep)
    kind = ex["kind"]
    settle = " once the trace has settled" if ex.get("window") else ""
    if kind == "value_at":
        at = str(ex["at"])
        try:
            float(at)
            where = f"{at} Hz" if sweep == "ac" else f"t = {at} s"
        except ValueError:      # an expression over earlier readings, e.g. f_mid
            where = f"the frequency found above ({at})" if sweep == "ac" \
                else f"the time found above ({at})"
        return f"{m.title}: at {where}, read {of}."
    if kind == "max" and sweep == "ac":
        return f"{m.title}: sweep the frequency and record the largest value of {of}."
    if kind == "crossing":
        level = str(ex.get("level", ""))
        if sweep == "ac" and re.fullmatch(r"\s*first\s*-\s*3\s*", level):
            return (f"{m.title}: from the low-frequency end, raise the frequency until "
                    f"the output amplitude falls to 0.707× its low-frequency value "
                    f"(−3 dB). Record that frequency.")
        return (f"{m.title}: find where {of} crosses {level} "
                f"({ex.get('direction', 'falling')}). Record the "
                f"{'frequency' if sweep == 'ac' else 'time'}.")
    if kind == "max":
        return f"{m.title}: read the positive peak of {of}{settle} (scope Max or a cursor)."
    if kind == "min":
        return f"{m.title}: read the negative peak of {of}{settle} (scope Min or a cursor)."
    if kind == "mean":
        return (f"{m.title}: read the average (DC) value of {of}{settle}: scope Mean "
                f"measurement, or a DMM on DC volts.")
    if kind == "peak_to_peak":
        return f"{m.title}: read the peak-to-peak value of {of}{settle}."
    if kind == "percentile":
        return (f"{m.title}: read the level where {of} sits on its flat part{settle}; "
                f"ignore switching spikes (put a cursor on the plateau).")
    if kind == "at_peak":
        return f"{m.title}: sweep the frequency and record where {of} peaks."
    return f"{m.title}: measure {kind} of {of}."


# ------------------------------------------------------------- the analysis

@dataclass
class LabResult:
    spec: object
    title: str
    pick: kitmod.Pick
    kit_deck: str
    mc: dict | None
    op_ideal: dict | None
    op_kit: dict | None
    figures: list
    supplies: list
    test_set: Path | None = None      # the block-diagram PNG (ten-step method, step 6)


def _percentile(xs: list, q: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def bench_range(mc: dict | None, name: str):
    """The middle 90% of Monte Carlo builds: where a real bench reading lands."""
    if not mc:
        return None
    vals = [s["metrics"][name] for s in mc["samples"]
            if s.get("metrics") and name in s["metrics"]]
    if len(vals) < 10:
        return None
    return _percentile(vals, 0.05), _percentile(vals, 0.95)


def _nodes(netlist: str):
    try:
        op = oppoint.operating_point(netlist)
    except Exception:                       # noqa: BLE001 - optional section
        return None
    return {n: v for n, v in op.nodes.items() if not INTERNAL_NODE.search(n)}


def analyse(spec, netlist: str, *, title: str, out: Path, kit=None, fixed=(),
            singles_only=False, mc_n: int = 200, seed: int = 0,
            workers: int = 8, make_figures: bool = True) -> LabResult:
    out.mkdir(parents=True, exist_ok=True)
    kit = kit or kitmod.load_kit()
    p = kitmod.pick(spec, netlist, kit, fixed=fixed, singles_only=singles_only,
                    workers=workers)
    kit_deck = p.deck() if p.params else netlist
    mc = None
    if mc_n:
        mc = robustness.monte_carlo(spec, kit_deck, mc_n, seed=seed, vary_temp=False,
                                    workers=workers, passive_tol=kit.passive_tol())
    figs, test_set = [], None
    if make_figures:
        figs = figures(spec, netlist, kit_deck if p.params else None, out, spec.name)
        import blockdiagram
        test_set = blockdiagram.draw(blockdiagram.from_deck(spec, kit_deck, title),
                                     out / f"{spec.name}_test_set.png")
    supplies = sources(kit_deck)
    # A DC checkpoint means "input off". With stepped logic inputs or a pulse
    # train there is no such state; the truth-table rows are the checkpoints.
    stepped = re.search(r"(?i)\b(pwl|pulse)\s*\(", _deck_body(kit_deck))
    return LabResult(spec=spec, title=title, pick=p, kit_deck=kit_deck, mc=mc,
                     op_ideal=None if stepped else _nodes(netlist),
                     op_kit=None if stepped else _nodes(kit_deck),
                     figures=figs, supplies=supplies, test_set=test_set)


# ---------------------------------------------------------------- the report

def _range(br, units: str) -> str:
    if not br:
        return "—"
    lo, hi = num(br[0], units), num(br[1], units)
    if lo == hi:
        return lo
    return f"{lo.removesuffix(' ' + units) if units else lo} to {hi}"


def compose(r: LabResult, top: int = 2, header: bool = True) -> list:
    """The report as blocks. `top` is the heading level of the three sections,
    so a multi-part lab can nest them under one heading per part."""
    spec, p = r.spec, r.pick
    B = Block
    H, h = f"h{top}", f"h{top + 1}"
    blocks = []
    if header:
        today = date.today()
        blocks += [B("h1", r.title),
                   B("p", f"Circuit: {spec.name}. Pre-lab prepared "
                          f"{today:%B} {today.day}, {today.year}. "
                          "Every value below is from an ngspice simulation.")]
    if spec.description:
        blocks.append(B("p", " ".join(spec.description.split())))

    # ---- Simulation
    blocks += [B(H, "Simulation")]
    if p.params:
        blocks.append(B("p", "The circuit was first simulated with the ideal (theory) "
                             "component values, to confirm the design meets its "
                             "requirements before any part is chosen."))
        blocks.append(B("table", header=["Component", "Ideal value"],
                        rows=[(prm.key, value(prm.nominal, prm.key[0], 5))
                              for prm in p.params]))
    blocks.append(B("table", header=["Measurement", "Theory / requirement",
                                     "Simulated", "In spec"],
                    rows=[(m.title, theory(m), num(p.ideal.get(m.name), m.units),
                           "yes" if m.limits.ok(p.ideal[m.name]) else "NO")
                          for m in spec.specs]))
    for aname, path in r.figures:
        what = ("Dashed: ideal values; solid: the kit build from the Design section."
                if p.params else "Simulated waveforms.")
        blocks.append(B("image", path=path, text=f"Figure: {spec.name}, {aname} "
                                                  f"analysis. {what}"))
    blocks.append(B("p", "Netlist simulated:" if not p.params
                    else "Netlist simulated (ideal values):"))
    blocks.append(B("code", text=_deck_body(p.netlist)))

    # ---- Design
    blocks += [B(H, "Design")]
    if not p.params:
        blocks.append(B("p", "No resistor, capacitor or inductor values need choosing: "
                             "every part is fixed by the handout (transistors, ICs, "
                             "sources). The table above is the predicted performance."))
    else:
        blocks += [B(h, "Component selection"),
                   B("p", f"Parts come from: {p.kit.name}. Rather than rounding "
                          f"each value on its own, {p.sims} candidate builds "
                          f"(nearest kit values, and two-part series/parallel "
                          f"combinations) were simulated, and the build whose "
                          f"measurements land closest to the requirements was "
                          f"chosen. Values already in the kit are kept as given."),
                   B("table", header=["Component", "Ideal", "Kit part(s)",
                                      "Built value", "Difference"],
                     rows=[(prm.key, value(prm.nominal, prm.key[0], 5),
                            part_text(p.best.choices[prm.key], prm.key[0]),
                            value(p.best.choices[prm.key].value, prm.key[0], 4),
                            f"{(p.best.choices[prm.key].value - prm.nominal) / prm.nominal:+.1%}")
                           for prm in p.params])]
        changed = [prm.key for prm in p.params
                   if p.rounded.choices[prm.key] != p.best.choices[prm.key]]
        if changed and p.rounded.values and p.best.values:
            worst = max(p.spec.specs, key=lambda m: abs(
                kitmod.errors(p.rounded.values, p.refs).get(m.name, 0)))
            rv, bv = p.rounded.values[worst.name], p.best.values[worst.name]

            def dev(v, m=worst):
                ref = p.refs[m.name][0]
                gap = f"{(v - ref) / abs(ref):+.1%} from {num(ref, m.units)}" if ref else ""
                return f"{num(v, m.units)} ({gap}{'' if m.limits.ok(v) else ', OUT of spec'})"

            blocks.append(B("p", (
                f"Rounding each part to its nearest kit value on its own would give "
                f"{worst.title} = {dev(rv)}; the chosen build gives {dev(bv)}. "
                f"The parts that differ: {', '.join(changed)}.")))

        rows = []
        for m in spec.specs:
            bv = (p.best.values or {}).get(m.name)
            rows.append((m.title, theory(m), num(p.ideal.get(m.name), m.units),
                         num(bv, m.units),
                         "—" if bv is None else ("yes" if m.limits.ok(bv) else "NO")))
        blocks += [B(h, "Predicted performance"),
                   B("table", header=["Measurement", "Theory / requirement", "Ideal",
                                      "Kit build", "In spec"], rows=rows)]

    if r.mc:
        varied = []
        if p.params:
            varied.append("every chosen part by its kit tolerance (" + ", ".join(
                f"{kitmod.FAMILY_KEYS[k]} ±{f.tol:.0%}"
                for k, f in p.kit.families.items() if f) + ")")
        varied.append(f"the supplies by ±{robustness.SUPPLY_TOL:.0%}")
        varied.append("device model parameters by their typical spread"
                      + _model_spread(r.kit_deck))
        mrows = []
        for m in spec.specs:
            st = r.mc["metrics"].get(m.name)
            if st:
                mrows.append((m.title, num(st["mean"], m.units),
                              _range(bench_range(r.mc, m.name), m.units),
                              f"{st['pass_rate']:.0%}"))
        blocks += [B(h, "Tolerance analysis"),
                   B("p", f"{r.mc['n']} Monte Carlo builds varied "
                          f"{', '.join(varied[:-1])} and {varied[-1]}. "
                          f"{r.mc['yield']:.0%} of builds met every requirement."),
                   B("table", header=["Measurement", "Mean", "90% of builds",
                                      "Builds in spec"], rows=mrows)]

    if p.params:
        blocks += [B("p", "Netlist of the kit build:"),
                   B("code", text=_deck_body(r.kit_deck))]

    # ---- Test plan
    blocks += [B(H, "Test Plan"), B(h, "Equipment")]
    kinds = {s["kind"] for s in r.supplies}
    eq = ["Digital multimeter (DMM)"]
    if "dc" in kinds:
        eq.append("DC power supply: " + "; ".join(
            f"{s['name']} = {num(s['value'], 'V', 3)}" for s in r.supplies if s["kind"] == "dc"))
    if kinds & {"signal", "ac"}:
        eq.append("Function generator")
    if any(a.command.split()[0].lower() in ("ac", "tran") for a in spec.analyses.values()):
        eq.append("Oscilloscope, DC-coupled (two channels or more)")
    eq.append("Breadboard and the parts listed in the schematic")
    blocks.append(B("bullets", items=eq))
    if r.test_set:
        blocks += [B(h, "Test set"),
                   B("image", path=r.test_set,
                     text="Test-set block diagram: which instrument connects to which "
                          "node. The procedure below refers to these channels.")]

    if p.params:
        prow = []
        for prm in p.params:
            fam = p.kit.family(prm.key[0])
            for part in p.best.choices[prm.key].parts:
                prow.append((prm.key, value(part, prm.key[0]),
                             f"{value(part * (1 - fam.tol), prm.key[0])} to "
                             f"{value(part * (1 + fam.tol), prm.key[0])}"))
        blocks += [B(h, "Part check (before building)"),
                   B("p", "Measure each part before it goes in the board; replace "
                          "any outside its range."),
                   B("table", header=["Component", "Part", "Acceptable reading"],
                     rows=prow)]

    steps = ["Build the circuit from the schematic."]
    dcs = [s for s in r.supplies if s["kind"] == "dc"]
    if dcs:
        steps.append("Set the supplies before connecting them: " + "; ".join(
            f"{s['name']} = {num(s['value'], 'V', 3)}" for s in dcs)
            + ". Use a low current limit for the first power-up.")
    if r.op_kit:
        steps.append("With the input signal off, check the DC voltages in the "
                     "checkpoint table below. Stop and find the wiring error if "
                     "any is far off.")
    # Inputs set up the same way are one step: "Inputs A, B, C, D: ...".
    same: dict = {}
    for s in r.supplies:
        if s["kind"] in ("signal", "ac", "logic"):
            same.setdefault(s["text"], []).append(s)
    for text, group in same.items():
        nodes = ", ".join(s["nodes"][0].upper() for s in group)
        names = ", ".join(s["name"] for s in group)
        steps.append(f"Input{'s' if len(group) > 1 else ''} {nodes} ({names}): {text}.")
    # Informational readings stay in: a derived requirement is calculated from them.
    steps += [instruction(m, spec) for m in spec.measurements]
    steps.append("Record each reading in the results table and compare it with "
                 "the expected range.")
    blocks += [B(h, "Procedure"), B("steps", items=steps)]

    if r.op_kit:
        cols = ["Node", "Expected (kit build)", "Ideal values"] if p.params \
            else ["Node", "Expected"]
        rows = [(n, num(r.op_kit[n], "V", 3),
                 num((r.op_ideal or {}).get(n), "V", 3))[:len(cols)]
                for n in sorted(r.op_kit)]
        blocks += [B(h, "DC checkpoints (input off)"),
                   B("table", header=cols, rows=rows)]

    res = [(m.title, num((p.best.values or {}).get(m.name), m.units),
            _range(bench_range(r.mc, m.name), m.units), theory(m), "", "")
           for m in spec.specs]
    blocks += [B(h, "Results"),
               B("table", header=["Measurement", "Expected", "Expected range "
                                  "(90% of builds)", "Requirement", "Measured",
                                  "Pass?"], rows=res)]
    return blocks


def _model_spread(deck: str) -> str:
    """Name the model parameters robustness.py actually varies in this deck."""
    kinds = {k.lower() for k in re.findall(r"(?im)^\s*\.model\s+\S+\s+(\w+)", deck)}
    said = []
    if kinds & {"nmos", "pmos"}:
        said.append(f"MOSFET threshold ±{robustness.MODEL_TOL[('nmos', 'vto')]:.0%}")
    if kinds & {"npn", "pnp"}:
        said.append(f"BJT beta ±{robustness.MODEL_TOL[('npn', 'bf')]:.0%}")
    if "d" in kinds:
        said.append(f"diode saturation current ±{robustness.MODEL_TOL[('d', 'is')]:.0%}")
    return f" ({', '.join(said)})" if said else ""


def _deck_body(deck: str) -> str:
    """The circuit, without the analysis block the report harness adds its own of."""
    from speccheck import strip_analyses
    return strip_analyses(deck).strip()


def _emit(blocks: list, out: Path, docx: bool, paths: dict) -> dict:
    paths["md"] = out / "report.md"
    paths["md"].write_text(render_md(blocks, out), encoding="utf-8")
    if docx:
        paths["docx"] = out / "report.docx"
        render_docx(blocks, paths["docx"])
    return paths


def write(r: LabResult, out: Path, docx: bool = True) -> dict:
    paths = {"deck": out / "kit_build.cir"}
    paths["deck"].write_text(r.kit_deck, encoding="utf-8")
    return _emit(compose(r), out, docx, paths)


# ------------------------------------------------------------ multi-part labs
#
# A handout is usually several circuits. A lab file lists them, and they go
# into one document, a heading per part:
#
#   title: EE 347 Exp. 4 -- Static and Dynamic CMOS Gates
#   notes: ["Model assumptions: ..."]
#   parts:
#     - {title: "Figure 4.1 -- NMOS 2-1 MUX", deck: fig4_1.cir, spec: fig4_1.yaml}

@dataclass
class Lab:
    title: str
    parts: list               # [{title, deck, spec, fix}]
    notes: list = field(default_factory=list)


def load_lab(path) -> Lab:
    import yaml
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parts = []
    for raw in data.get("parts", []):
        parts.append({"title": raw.get("title") or Path(raw["deck"]).stem,
                      "deck": path.parent / raw["deck"],
                      "spec": path.parent / raw["spec"],
                      "fix": list(raw.get("fix", []))})
    if not parts:
        raise ValueError(f"{path}: a lab file needs at least one entry under 'parts'")
    return Lab(title=data.get("title", path.stem), parts=parts,
               notes=[" ".join(str(n).split()) for n in data.get("notes", [])])


def compose_lab(lab: Lab, results: list) -> list:
    today = date.today()
    blocks = [Block("h1", lab.title),
              Block("p", f"Pre-lab prepared {today:%B} {today.day}, {today.year}. "
                         "Every value below is from an ngspice simulation; each "
                         "part's netlist is included.")]
    if lab.notes:
        blocks.append(Block("bullets", items=lab.notes))
    for part, r in zip(lab.parts, results, strict=True):
        blocks.append(Block("h2", part["title"]))
        blocks += compose(r, top=3, header=False)
    return blocks


def write_lab(lab: Lab, results: list, out: Path, docx: bool = True) -> dict:
    paths = {}
    for r in results:          # the .cir files a handout asks to be handed in
        p = out / f"{r.spec.name}.cir"
        p.write_text(r.kit_deck, encoding="utf-8")
        paths[r.spec.name] = p
    return _emit(compose_lab(lab, results), out, docx, paths)


def run_lab(path, out: Path, *, kit=None, mc_n: int = 200, seed: int = 0,
            workers: int = 8) -> tuple:
    lab = load_lab(path)
    results = [analyse(specfile.load(part["spec"]),
                       part["deck"].read_text(encoding="utf-8"),
                       title=part["title"], out=out, kit=kit, fixed=part["fix"],
                       mc_n=mc_n, seed=seed, workers=workers)
               for part in lab.parts]
    return lab, results


def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("deck", nargs="?", help="the handout circuit with its ideal values")
    ap.add_argument("--spec", help="spec YAML: what the circuit must do")
    ap.add_argument("--lab", help="lab YAML listing several parts (instead of deck + --spec)")
    ap.add_argument("--title", help="report title (default: the spec name)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--kit", help="kit YAML (default: kit.yaml)")
    ap.add_argument("--fix", action="append", default=[], metavar="REFDES",
                    help="keep this component as given (repeatable)")
    ap.add_argument("--singles-only", action="store_true",
                    help="no series/parallel part combinations")
    ap.add_argument("--mc", type=int, default=200, help="Monte Carlo builds (0 = skip)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-docx", action="store_true", help="Markdown only")
    a = ap.parse_args(argv)
    if bool(a.lab) == bool(a.deck and a.spec):
        ap.error("give either a deck with --spec, or --lab")

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    kit = kitmod.load_kit(a.kit)
    if a.lab:
        lab, results = run_lab(a.lab, out, kit=kit, mc_n=a.mc, seed=a.seed,
                               workers=a.workers)
        paths = write_lab(lab, results, out, docx=not a.no_docx)
        for part, r in zip(lab.parts, results, strict=True):
            bad = [m.title for m in r.spec.specs if not m.limits.ok(r.pick.ideal[m.name])]
            yld = f", Monte Carlo {r.mc['yield']:.0%}" if r.mc else ""
            print(f"{part['title']}: {len(r.spec.specs) - len(bad)}/{len(r.spec.specs)} "
                  f"rows as expected{yld}" + (f"  MISSES: {', '.join(bad)}" if bad else ""))
    else:
        spec = specfile.load(a.spec)
        r = analyse(spec, Path(a.deck).read_text(encoding="utf-8"),
                    title=a.title or spec.name, out=out, kit=kit, fixed=a.fix,
                    singles_only=a.singles_only, mc_n=a.mc, seed=a.seed,
                    workers=a.workers)
        paths = write(r, out, docx=not a.no_docx)
        print(kitmod.summary(r.pick))
        if r.mc:
            print(f"\nMonte Carlo ({r.mc['n']} builds, kit tolerances): "
                  f"{r.mc['yield']:.0%} meet spec")
    for v in paths.values():
        print(f"wrote {v}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
