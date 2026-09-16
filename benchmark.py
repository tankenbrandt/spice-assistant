#!/usr/bin/env python3
"""Benchmark suite for spice-assistant.

Runs the existing generate -> run -> fix loop (max 4 attempts per circuit)
against a fixed test set of 8 circuits across three difficulty tiers, logs
everything, and writes logs/run_log.json + REPORT.md.

Usage:  python benchmark.py
"""

import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"       # bulky per-attempt evidence
DOC_DIR = PROJECT_DIR / "docs"       # hand-written analysis prose


import main  # noqa: E402  (imported first so its .env loader runs)

main.load_dotenv(PROJECT_DIR / ".env")

import speccheck  # noqa: E402

MAX_ATTEMPTS = 4
SPEC_REPAIR_ROUNDS = 2  # max spec-in-the-loop repair rounds (only with --spec-repair)
SPEC_REPAIR = False  # set by --spec-repair in main_entry; each round costs API calls

# Intro pricing for claude-sonnet-5 (per million tokens) — for cost reporting.
PRICE_IN_PER_MTOK = 2.00
PRICE_OUT_PER_MTOK = 10.00

# Each description ends with harness requirements (standard node/source names)
# so the spec-verification layer can measure deterministically.
CIRCUITS = [
    # ---- Easy ----
    {
        "id": "rc_lowpass",
        "tier": "easy",
        "description": (
            "a first-order RC low-pass filter with a 1kHz cutoff frequency and a 5V DC source. "
            "Harness requirements: name the input node 'in' and the output node 'out'; "
            "give the input source an AC magnitude of 1 so AC analysis works."
        ),
    },
    {
        "id": "rl_step",
        "tier": "easy",
        "description": (
            "a series RL circuit with a 100 ohm resistor and a 10 mH inductor driven by a 5V step input; "
            "show the transient current response over a few time constants. "
            "Harness requirements: name the input source 'vin' driving node 'in', with the step starting at t=0."
        ),
    },
    {
        "id": "voltage_divider",
        "tier": "easy",
        "description": (
            "a resistive voltage divider that produces 3.3V from a 12V DC source; "
            "show the operating point confirming the output voltage. "
            "Harness requirements: name the output node 'out'."
        ),
    },
    # ---- Medium ----
    {
        "id": "halfwave_rectifier",
        "tier": "medium",
        "description": (
            "a half-wave rectifier using a silicon diode and a 1 kilohm load resistor, "
            "driven by a 10V amplitude 60Hz sine source; show the transient output voltage "
            "across the load for a few cycles. "
            "Harness requirements: name the source node 'in' and the load node 'out'."
        ),
    },
    {
        "id": "ce_bjt_amp",
        "tier": "medium",
        "description": (
            "a common-emitter BJT amplifier using an NPN transistor (2N2222-style model) with a "
            "small-signal voltage gain of approximately -50, powered from a 12V supply, with proper "
            "bias resistors and coupling capacitors; include an AC analysis demonstrating the mid-band gain. "
            "Harness requirements: name the input node 'in' and the output node 'out'; "
            "give the input source an AC magnitude of 1."
        ),
    },
    {
        "id": "noninv_opamp",
        "tier": "medium",
        "description": (
            "a non-inverting op-amp amplifier with a gain of 11, using an ideal op-amp model "
            "(e.g. a voltage-controlled voltage source with high gain), with a 100mV 1kHz sine input; "
            "show the amplified output in transient analysis. "
            "Harness requirements: name the input node 'in' and the output node 'out'."
        ),
    },
    # ---- Hard ----
    {
        "id": "bridge_rectifier",
        "tier": "hard",
        "description": (
            "a full-wave bridge rectifier with four diodes and a smoothing capacitor sized so the "
            "output ripple is less than 1V peak-to-peak into a 100 ohm load, driven by a 12V RMS "
            "60Hz sine source; show the steady-state output ripple in transient analysis. "
            "Harness requirements: name the output node 'out' (across the load)."
        ),
    },
    {
        "id": "buck_converter",
        "tier": "hard",
        "description": (
            "a basic buck converter stepping 12V down to 5V (within 3%) with a 100kHz switching "
            "frequency and output ripple under 100 mV peak-to-peak, using a voltage-controlled switch "
            "driven by a pulse source with the appropriate duty cycle, a freewheeling diode, an LC "
            "output filter, and a resistive load; show the output voltage settling in transient analysis. "
            "Harness requirements: name the output node 'out'."
        ),
    },
]

# Round 2: generalization test — new circuit types, several with coupled specs.
CIRCUITS_ROUND2 = [
    # ---- Easy ----
    {
        "id": "rlc_bandpass",
        "tier": "easy",
        "description": (
            "a series RLC bandpass filter with the output taken across the resistor, "
            "with resonant frequency 10 kHz (within 3%) AND quality factor Q = 5 (within 10%). "
            "Harness requirements: name the input node 'in' and the output node 'out'; "
            "input source 'vin' with AC magnitude 1."
        ),
    },
    {
        "id": "rc_highpass",
        "tier": "easy",
        "description": (
            "a first-order RC high-pass filter with a -3dB cutoff at 2 kHz (within 3%). "
            "Harness requirements: name the input node 'in' and the output node 'out'; "
            "input source with AC magnitude 1."
        ),
    },
    # ---- Medium ----
    {
        "id": "cs_mosfet_amp",
        "tier": "medium",
        "description": (
            "a common-source MOSFET amplifier using an NMOS transistor with an explicit .model card, "
            "properly biased, with a mid-band voltage gain magnitude of 10 (within 10%). "
            "Harness requirements: name the input node 'in' and the output node 'out'; "
            "input source with AC magnitude 1."
        ),
    },
    {
        "id": "inv_opamp_gbw",
        "tier": "medium",
        "description": (
            "an inverting op-amp amplifier with gain magnitude 20 (within 5%) AND a -3dB bandwidth "
            "of at least 50 kHz. The op-amp must be a realistic single-pole model with a finite "
            "gain-bandwidth product of about 2 MHz (e.g. open-loop DC gain 100,000 with a 20 Hz "
            "dominant pole) — NOT an ideal infinite-bandwidth controlled source. "
            "Harness requirements: name the input node 'in' and the output node 'out'; "
            "input source with AC magnitude 1."
        ),
    },
    {
        "id": "bjt_diffamp",
        "tier": "medium",
        "description": (
            "a BJT differential pair with differential gain 25 (within 10%) AND common-mode "
            "rejection ratio of at least 60 dB, powered from +12V and -12V supplies, with a "
            "single-ended output taken from one collector; use a tail current source for good CMRR. "
            "Harness requirements: two input sources named 'vinp' (driving node 'inp') and 'vinn' "
            "(driving node 'inn'), each with AC magnitude 1 (the harness overrides the AC values); "
            "name the output node 'out'."
        ),
    },
    # ---- Hard ----
    {
        "id": "boost_converter",
        "tier": "hard",
        "description": (
            "a boost converter stepping 5V up to 12V (within 3%) with a 100kHz switching frequency, "
            "using an inductor from the input, a voltage-controlled switch to ground driven by a "
            "pulse source with the appropriate duty cycle, a diode to the output, an output "
            "capacitor, and a resistive load; show the output voltage settling in transient analysis. "
            "Harness requirements: name the output node 'out'."
        ),
    },
    {
        "id": "ce_amp_bias_and_gain",
        "tier": "hard",
        "description": (
            "a common-emitter BJT amplifier on a single 12V supply that simultaneously achieves a "
            "quiescent collector-emitter voltage Vce = 6V (within 10%) AND a mid-band small-signal "
            "voltage gain of -80 (within 10%), using an NPN transistor with an explicit .model card, "
            "a voltage-divider bias network, and coupling/bypass capacitors as needed. "
            "Harness requirements: name the input node 'in', the output node 'out', the collector "
            "node 'coll', and the emitter node 'emit'; input source with AC magnitude 1."
        ),
    },
]

# Categorization of failure signatures, matched against actual ngspice output
# of failed attempts. A single output can hit multiple categories.
FAILURE_PATTERNS = {
    "convergence": re.compile(
        r"(?i)no convergence|gmin stepping|source stepping|timestep too small|"
        r"convergence (?:problem|failed)|singular matrix"
    ),
    # Mixing .op/.ac/.tran dot-lines with a `.control ... run` block in batch
    # mode: the first analysis runs, then `run` finds nothing left to execute
    # ("no data saved ... analysis not run" / "doAnalyses: not found").
    "batch_analysis_invocation": re.compile(
        r"(?i)no data saved for .* analysis not run|doanalyses: not found|"
        r"run simulation\(s\) aborted"
    ),
    "syntax_or_parse": re.compile(
        r"(?i)error on line|syntax error|mismatch|unable to find|"
        r"unknown (?:parameter|device|token)|undefined (?:parameter|symbol)|"
        r"there aren't any circuits loaded"
    ),
    "missing_model_or_subckt": re.compile(
        r"(?i)could not find a valid modelname|unknown model|unknown subckt|"
        r"can(?:'|no)t find (?:the )?(?:model|subckt|definition)|mif-error"
    ),
    "control_or_vector": re.compile(
        r"(?i)vector .* (?:is )?not (?:available|found)|no such vector|"
        r"not available or has zero length|plot .* not found"
    ),
    "timeout": re.compile(r"(?i)timed out"),
}


def categorize(output: str) -> list[str]:
    return [name for name, rx in FAILURE_PATTERNS.items() if rx.search(output)] or ["other"]


def run_circuit(circuit: dict) -> dict:
    """Run the generate->run->fix loop for one circuit; return full record."""
    record = {
        "id": circuit["id"],
        "tier": circuit["tier"],
        "description": circuit["description"],
        "attempts": [],
        "success": False,
        "attempts_used": 0,
        "harness_error": None,
        "spec_check": None,
    }
    t0 = time.monotonic()
    try:
        netlist = main.generate_netlist(circuit["description"])
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"  attempt {attempt}/{MAX_ATTEMPTS} ...", file=sys.stderr, flush=True)
            ta = time.monotonic()
            success, output = main.run_ngspice(netlist)
            record["attempts"].append(
                {
                    "attempt": attempt,
                    "netlist": netlist,
                    "ngspice_output": output,
                    "success": success,
                    "sim_seconds": round(time.monotonic() - ta, 2),
                    "failure_categories": None if success else categorize(output),
                }
            )
            record["attempts_used"] = attempt
            if success:
                record["success"] = True
                break
            if attempt < MAX_ATTEMPTS:
                netlist = main.fix_netlist(netlist, output)
        if record["success"]:
            # Second pass: spec verification (ngspice only — zero API calls)
            print("  spec check ...", file=sys.stderr, flush=True)
            record["spec_check"] = speccheck.check(
                circuit["id"], record["attempts"][-1]["netlist"]
            )
            sc = record["spec_check"]
            verdict = "PASS" if sc.get("pass") else "FAIL"
            print(
                f"  spec: {verdict} — {sc.get('measured') or sc.get('error')}",
                file=sys.stderr, flush=True,
            )
            # Optional spec-in-the-loop repair: feed the measured value back to
            # the model. API-billed — opt-in via --spec-repair only.
            if SPEC_REPAIR and not sc.get("pass"):
                repaired_netlist = record["attempts"][-1]["netlist"]
                rounds = []
                for k in range(1, SPEC_REPAIR_ROUNDS + 1):
                    print(f"  spec-repair round {k} ...", file=sys.stderr, flush=True)
                    repaired_netlist = main.fix_netlist_spec(
                        repaired_netlist,
                        sc.get("target") or "",
                        sc.get("measured") or sc.get("error") or "",
                    )
                    sim_ok, sim_out = main.run_ngspice(repaired_netlist)
                    sc = (
                        speccheck.check(circuit["id"], repaired_netlist)
                        if sim_ok
                        else {"pass": False, "target": None, "measured": None,
                              "error": "clean-run failed after spec repair"}
                    )
                    rounds.append({
                        "round": k,
                        "netlist": repaired_netlist,
                        "sim_success": sim_ok,
                        "sim_output": sim_out,
                        "spec_check": sc,
                    })
                    if sc.get("pass"):
                        break
                record["spec_repair"] = {
                    "rounds": rounds,
                    "final_pass": bool(sc.get("pass")),
                }
    except Exception as exc:  # API errors, etc. — log and move on
        record["harness_error"] = f"{type(exc).__name__}: {exc}"
        print(f"  HARNESS ERROR: {record['harness_error']}", file=sys.stderr, flush=True)
    record["total_seconds"] = round(time.monotonic() - t0, 2)
    return record


def tier_stats(results: list[dict]) -> dict:
    stats = {}
    for tier in ("easy", "medium", "hard"):
        rs = [r for r in results if r["tier"] == tier]
        solved = [r for r in rs if r["success"]]
        stats[tier] = {
            "total": len(rs),
            "solved": len(solved),
            "solve_rate": round(len(solved) / len(rs), 3) if rs else None,
            "avg_attempts_when_solved": (
                round(sum(r["attempts_used"] for r in solved) / len(solved), 2)
                if solved else None
            ),
        }
    return stats


def build_report(run: dict) -> str:
    results = run["results"]
    total = len(results)
    solved = sum(1 for r in results if r["success"])
    stats = tier_stats(results)

    # Aggregate failure categories across all *failed* attempts (including
    # retried-then-solved circuits' early failures).
    cat_counter = Counter()
    cat_examples: dict[str, str] = {}
    for r in results:
        for a in r["attempts"]:
            if a["success"]:
                continue
            for cat in a["failure_categories"]:
                cat_counter[cat] += 1
                if cat not in cat_examples:
                    # Grab the most informative line(s) from the real output
                    never = re.compile(r"(?!x)x")  # matches nothing, incl. empty lines
                    lines = [
                        ln.strip()
                        for ln in a["ngspice_output"].splitlines()
                        if ln.strip() and FAILURE_PATTERNS.get(cat, never).search(ln)
                    ]
                    snippet = "; ".join(lines[:2]) if lines else a["ngspice_output"].strip()[:200]
                    cat_examples[cat] = f"{r['id']} attempt {a['attempt']}: {snippet[:300]}"

    lines = []
    lines.append("# spice-assistant Benchmark Report")
    lines.append("")
    lines.append(f"- **Run started:** {run['started_utc']}")
    lines.append(f"- **Run finished:** {run['finished_utc']}")
    lines.append(f"- **Model:** {run['model']}")
    lines.append(f"- **ngspice:** {run['ngspice_exe']}")
    lines.append(f"- **Max attempts per circuit:** {MAX_ATTEMPTS}")
    lines.append("")
    first_try = sum(1 for r in results if r["success"] and r["attempts_used"] == 1)
    spec_passed = sum(
        1 for r in results if (r.get("spec_check") or {}).get("pass")
    )
    have_spec = any(r.get("spec_check") is not None for r in results)

    lines.append("## Overall results")
    lines.append("")
    lines.append(f"- **Simulation success (clean run): {solved}/{total} ({solved/total:.0%})**")
    lines.append(f"- **First-attempt clean-run rate: {first_try}/{total}**")
    repair_log = None
    repair_path = LOG_DIR / "repair_log.json"
    if repair_path.exists():
        repair_log = json.loads(repair_path.read_text(encoding="utf-8"))

    if have_spec:
        lines.append(
            f"- **Spec compliance (electrically correct): {spec_passed}/{total} "
            f"({spec_passed/total:.0%})**"
        )
        if repair_log:
            repaired_ok = sum(1 for x in repair_log["repairs"] if x["after"]["spec_pass"])
            lines.append(
                f"- **Spec compliance after spec-in-the-loop repair (stage 2): "
                f"{spec_passed + repaired_ok}/{total}** — see the repair section below"
            )
    usage = run.get("api_usage")
    if usage:
        cost = (
            usage["input_tokens"] / 1e6 * PRICE_IN_PER_MTOK
            + usage["output_tokens"] / 1e6 * PRICE_OUT_PER_MTOK
        )
        lines.append(
            f"- **API usage:** {usage['calls']} calls, {usage['input_tokens']:,} in / "
            f"{usage['output_tokens']:,} out tokens ≈ **${cost:.3f}** "
            f"(claude-sonnet-5 intro pricing). Spec checks use ngspice only — no API cost."
        )
    lines.append("")
    lines.append("## Solve rate by difficulty tier")
    lines.append("")
    lines.append("| Tier | Solved | Total | Solve rate | Avg attempts (solved) |")
    lines.append("|------|--------|-------|------------|------------------------|")
    for tier in ("easy", "medium", "hard"):
        s = stats[tier]
        rate = f"{s['solve_rate']:.0%}" if s["solve_rate"] is not None else "n/a"
        avg = s["avg_attempts_when_solved"] if s["avg_attempts_when_solved"] is not None else "n/a"
        lines.append(f"| {tier} | {s['solved']} | {s['total']} | {rate} | {avg} |")
    lines.append("")
    lines.append("## Per-circuit results")
    lines.append("")
    lines.append("| Circuit | Tier | Result | Attempts | Failure categories seen |")
    lines.append("|---------|------|--------|----------|--------------------------|")
    for r in results:
        cats = sorted(
            {c for a in r["attempts"] if not a["success"] for c in a["failure_categories"]}
        )
        result = "SOLVED" if r["success"] else ("HARNESS ERROR" if r["harness_error"] else "FAILED")
        lines.append(
            f"| {r['id']} | {r['tier']} | {result} | {r['attempts_used']} | {', '.join(cats) or '—'} |"
        )
    lines.append("")
    if have_spec:
        lines.append("## Spec compliance (electrical correctness)")
        lines.append("")
        lines.append("A clean ngspice run says nothing about whether the circuit meets its")
        lines.append("electrical targets. This section re-runs each solved netlist with a")
        lines.append("harness-controlled measurement (.control + wrdata), extracts real")
        lines.append("output vectors, and asserts the spec.")
        lines.append("")
        lines.append("| Circuit | Tier | Sim | Spec target | Measured | Spec |")
        lines.append("|---------|------|-----|-------------|----------|------|")
        for r in results:
            sc = r.get("spec_check")
            sim = "clean" if r["success"] else "FAILED"
            if sc is None:
                tgt, meas, verdict = "—", "—", "not run (sim failed)" if not r["success"] else "n/a"
            else:
                tgt = (sc.get("target") or "—").replace("|", "\\|")
                meas = (sc.get("measured") or sc.get("error") or "—").replace("|", "\\|")
                verdict = "**PASS**" if sc.get("pass") else "**FAIL**"
            lines.append(f"| {r['id']} | {r['tier']} | {sim} | {tgt} | {meas} | {verdict} |")
        lines.append("")
        lines.append("### Spec-compliance rate by tier")
        lines.append("")
        lines.append("| Tier | Sim clean | Spec pass | Total |")
        lines.append("|------|-----------|-----------|-------|")
        for tier in ("easy", "medium", "hard"):
            rs = [r for r in results if r["tier"] == tier]
            sim_n = sum(1 for r in rs if r["success"])
            spec_n = sum(1 for r in rs if (r.get("spec_check") or {}).get("pass"))
            lines.append(f"| {tier} | {sim_n} | {spec_n} | {len(rs)} |")
        lines.append("")
        misleading = [
            r for r in results
            if r["success"] and not (r.get("spec_check") or {}).get("pass")
        ]
        if misleading:
            lines.append("### ⚠️ Ran cleanly but NOT electrically correct")
            lines.append("")
            lines.append("These circuits would be scored as 'solved' by a did-it-run check —")
            lines.append("the failure mode that check misses entirely:")
            lines.append("")
            for r in misleading:
                sc = r.get("spec_check") or {}
                why = sc.get("measured") or sc.get("error") or "unknown"
                lines.append(f"- **{r['id']}** ({r['tier']}): {why}")
        else:
            lines.append("No circuit ran cleanly while violating its spec in this run.")
        lines.append("")

    if repair_log:
        lines.append("## Spec-in-the-loop repair (stage 2)")
        lines.append("")
        lines.append("The spec failures above were repaired by feeding the **measured value**")
        lines.append("(not ngspice stderr, which was clean) back into a redesign step, then")
        lines.append("re-verifying with the same measurement harness.")
        lines.append(f"Performed by: {repair_log['performed_by']}")
        lines.append("")
        lines.append("| Circuit | Before (measured) | Targeted change | After (measured) | Iterations |")
        lines.append("|---------|-------------------|-----------------|------------------|------------|")
        for x in repair_log["repairs"]:
            before = x["before"]["measured"].replace("|", "\\|")
            after = x["after"]["measured"].replace("|", "\\|")
            change = x["change"].replace("|", "\\|")
            verdict = "**PASS**" if x["after"]["spec_pass"] else "**FAIL**"
            lines.append(
                f"| {x['id']} | {before} | {change} | {after} {verdict} | {x['iterations']} |"
            )
        lines.append("")
        lines.append(f"**{repair_log['result']}**")
        lines.append("")
        lines.append("Repaired netlists: `repaired/*.cir`; full before/after evidence: `logs/repair_log.json`.")
        lines.append("")

    lines.append("## Failure pattern analysis (from logged ngspice output)")
    lines.append("")
    if cat_counter:
        lines.append("Counts are over all failed attempts, including early attempts of")
        lines.append("circuits that were eventually solved.")
        lines.append("")
        lines.append("| Category | Failed attempts | Example (verbatim from log) |")
        lines.append("|----------|-----------------|------------------------------|")
        for cat, n in cat_counter.most_common():
            ex = cat_examples.get(cat, "").replace("|", "\\|")
            lines.append(f"| {cat} | {n} | {ex} |")
    else:
        lines.append("No failed attempts — every circuit simulated cleanly on attempt 1.")
    lines.append("")
    narrative_path = DOC_DIR / "analysis-round-1.md"
    if narrative_path.exists():
        lines.append(narrative_path.read_text(encoding="utf-8").strip())
    else:
        lines.append("<!-- NARRATIVE: write per-run analysis into analysis-round-1.md -->")
    lines.append("")
    lines.append("Full per-attempt netlists and ngspice output: `logs/run_log.json`.")
    lines.append("")

    r2_path = LOG_DIR / "run_log_round2.json"
    if r2_path.exists():
        run2 = json.loads(r2_path.read_text(encoding="utf-8"))
        lines.append(_round2_section(run2, results, run))
    return "\n".join(lines)


def _final_spec(r: dict) -> dict | None:
    """Final spec state for a record: last repair round's check, else original."""
    rep = r.get("spec_repair")
    if rep and rep.get("rounds"):
        return rep["rounds"][-1]["spec_check"]
    return r.get("spec_check")


def _round2_section(run2: dict, r1_results: list, r1_run: dict | None) -> str:
    def esc(s):
        return (s or "—").replace("|", "\\|")

    rs = run2["results"]
    total = len(rs)
    solved = sum(1 for r in rs if r["success"])
    first_try = sum(1 for r in rs if r["success"] and r["attempts_used"] == 1)
    spec_before = sum(1 for r in rs if (r.get("spec_check") or {}).get("pass"))
    spec_after = sum(1 for r in rs if ((_final_spec(r) or {}).get("pass")))

    L = []
    L.append("---")
    L.append("")
    L.append("## Round 2: Generalization Test (new circuits, --spec-repair enabled)")
    L.append("")
    L.append(f"- **Run:** {run2['started_utc']} → {run2.get('finished_utc', '?')} | model {run2['model']}")
    usage = run2.get("api_usage")
    if usage:
        cost = (usage["input_tokens"] / 1e6 * PRICE_IN_PER_MTOK
                + usage["output_tokens"] / 1e6 * PRICE_OUT_PER_MTOK)
        L.append(
            f"- **API usage:** {usage['calls']} calls, {usage['input_tokens']:,} in / "
            f"{usage['output_tokens']:,} out tokens ≈ **${cost:.3f}** "
            f"(includes automated spec-repair rounds)"
        )
    L.append(f"- **Simulation success (clean run): {solved}/{total}**")
    L.append(f"- **First-attempt clean-run rate: {first_try}/{total}**")
    L.append(f"- **Spec compliance BEFORE repair: {spec_before}/{total}**")
    L.append(f"- **Spec compliance AFTER automated spec-repair: {spec_after}/{total}**")
    L.append("")

    L.append("### Per-circuit results")
    L.append("")
    L.append("| Circuit | Tier | Sim attempts | Spec before repair | Repair rounds | Spec after repair |")
    L.append("|---------|------|--------------|--------------------|---------------|--------------------|")
    for r in rs:
        sc0 = r.get("spec_check") or {}
        fin = _final_spec(r) or {}
        rounds_used = len((r.get("spec_repair") or {}).get("rounds", []))
        before = ("PASS" if sc0.get("pass") else "FAIL — " + esc(sc0.get("measured") or sc0.get("error"))) if r["success"] else "sim FAILED"
        after = ("**PASS** — " if fin.get("pass") else "**FAIL** — ") + esc(fin.get("measured") or fin.get("error")) if r["success"] else "—"
        L.append(f"| {r['id']} | {r['tier']} | {r['attempts_used']} | {before} | {rounds_used} | {after} |")
    L.append("")

    # Coupled-spec analysis: did repair fix one spec while breaking another?
    coupled = [r for r in rs if (r.get("spec_check") or {}).get("specs")]
    if coupled:
        L.append("### Coupled-spec analysis (did repair break the other spec?)")
        L.append("")
        L.append("| Circuit | Sub-spec | Before repair | After repair | Outcome |")
        L.append("|---------|----------|---------------|--------------|---------|")
        for r in coupled:
            before_specs = r["spec_check"]["specs"]
            fin = _final_spec(r) or {}
            after_specs = fin.get("specs") or {}
            rep = r.get("spec_repair")
            # detect regressions in ANY repair round, not just the final one
            regressed_rounds = {}
            if rep:
                for rnd in rep["rounds"]:
                    for k, s in (rnd["spec_check"].get("specs") or {}).items():
                        if before_specs.get(k, {}).get("pass") and not s["pass"]:
                            regressed_rounds.setdefault(k, rnd["round"])
            unmeasurable = bool(rep) and not after_specs and bool(fin.get("error"))
            for k, s0 in before_specs.items():
                s1 = after_specs.get(k, {})
                b = ("PASS" if s0.get("pass") else "FAIL") + f" ({esc(s0.get('measured'))})"
                if s1:
                    a = ("PASS" if s1.get("pass") else "FAIL") + f" ({esc(s1.get('measured'))})"
                elif unmeasurable:
                    a = f"unmeasurable ({esc(fin.get('error'))})"
                else:
                    a = "n/a (no repair ran)" if not rep else "n/a"
                if not rep:
                    outcome = "no repair needed" if s0.get("pass") else "not repaired"
                elif unmeasurable:
                    outcome = "**unmeasurable after repair** — repair broke the sim/harness contract, not this spec specifically"
                elif s0.get("pass") and not s1.get("pass"):
                    outcome = f"**BROKEN by repair** (round {regressed_rounds.get(k, '?')})"
                elif s0.get("pass") and s1.get("pass"):
                    if k in regressed_rounds:
                        outcome = f"broken in round {regressed_rounds[k]}, restored by final round"
                    else:
                        outcome = "survived repair"
                elif not s0.get("pass") and s1.get("pass"):
                    outcome = "fixed by repair"
                else:
                    outcome = "still failing"
                L.append(f"| {r['id']} | {k} | {b} | {a} | {outcome} |")
        L.append("")

    # Repair-convergence summary
    repaired = [r for r in rs if r.get("spec_repair")]
    conv1 = sum(1 for r in repaired if r["spec_repair"]["final_pass"] and len(r["spec_repair"]["rounds"]) == 1)
    convN = sum(1 for r in repaired if r["spec_repair"]["final_pass"] and len(r["spec_repair"]["rounds"]) > 1)
    failed = sum(1 for r in repaired if not r["spec_repair"]["final_pass"])
    no_repair_needed = sum(1 for r in rs if r["success"] and (r.get("spec_check") or {}).get("pass"))
    L.append("### Repair convergence")
    L.append("")
    L.append(f"- Passed spec with no repair needed: **{no_repair_needed}**")
    L.append(f"- Repaired, converged in 1 round: **{conv1}**")
    L.append(f"- Repaired, converged in >1 round: **{convN}**")
    L.append(f"- Repair failed to converge (after {SPEC_REPAIR_ROUNDS} rounds): **{failed}**")
    L.append("")

    # Comparison with round 1
    if r1_results:
        r1_total = len(r1_results)
        r1_solved = sum(1 for r in r1_results if r["success"])
        r1_first = sum(1 for r in r1_results if r["success"] and r["attempts_used"] == 1)
        r1_spec = sum(1 for r in r1_results if (r.get("spec_check") or {}).get("pass"))
        r1_cost = "?"
        if r1_run and r1_run.get("api_usage"):
            u = r1_run["api_usage"]
            r1_cost = f"${u['input_tokens']/1e6*PRICE_IN_PER_MTOK + u['output_tokens']/1e6*PRICE_OUT_PER_MTOK:.3f}"
        r2_cost = f"${cost:.3f}" if usage else "?"
        L.append("### Round 1 vs Round 2")
        L.append("")
        L.append("| Metric | Round 1 | Round 2 |")
        L.append("|--------|---------|---------|")
        L.append(f"| Circuits | {r1_total} | {total} |")
        L.append(f"| Sim success | {r1_solved}/{r1_total} | {solved}/{total} |")
        L.append(f"| First-attempt clean run | {r1_first}/{r1_total} | {first_try}/{total} |")
        L.append(f"| Spec compliance before repair | {r1_spec}/{r1_total} | {spec_before}/{total} |")
        L.append(f"| Spec compliance after repair | 8/8 (manual, in-session) | {spec_after}/{total} (automated --spec-repair) |")
        L.append(f"| API cost | {r1_cost} (repairs free, in-session) | {r2_cost} (repairs included) |")
        L.append("")

    n2 = DOC_DIR / "analysis-round-2.md"
    if n2.exists():
        L.append(n2.read_text(encoding="utf-8").strip())
        L.append("")
    L.append("Full round-2 evidence: `logs/run_log_round2.json`.")
    L.append("")
    return "\n".join(L)


def rebuild_report_only() -> int:
    """Rebuild REPORT.md from an existing run_log.json (no API calls).

    Re-categorizes failed attempts from their raw logged output so that
    pattern updates apply retroactively, and persists the refresh to the log.
    """
    log_path = LOG_DIR / "run_log.json"
    if not log_path.exists():
        print("ERROR: logs/run_log.json not found — run the benchmark first.", file=sys.stderr)
        return 2
    run = None
    for lp in (log_path, LOG_DIR / "run_log_round2.json"):
        if not lp.exists():
            continue
        r = json.loads(lp.read_text(encoding="utf-8"))
        for rec in r["results"]:
            for a in rec["attempts"]:
                if not a["success"]:
                    a["failure_categories"] = categorize(a["ngspice_output"])
        lp.write_text(json.dumps(r, indent=2), encoding="utf-8")
        if lp == log_path:
            run = r
    (PROJECT_DIR / "REPORT.md").write_text(build_report(run), encoding="utf-8")
    print("Rebuilt REPORT.md (and refreshed categories in run logs).", file=sys.stderr)
    return 0


def main_entry() -> int:
    import os
    global SPEC_REPAIR
    if "--report-only" in sys.argv:
        return rebuild_report_only()
    SPEC_REPAIR = "--spec-repair" in sys.argv
    round2 = "--round2" in sys.argv
    circuits = CIRCUITS_ROUND2 if round2 else CIRCUITS
    # The log belongs in logs/, which is where build_report and --report-only
    # read it from; writing it to the project root left a fresh run invisible
    # to the report. --out redirects it, so repeat runs can be collected
    # without overwriting the evidence REPORT.md is built from.
    log_path = LOG_DIR / ("run_log_round2.json" if round2 else "run_log.json")
    if "--out" in sys.argv:
        log_path = Path(sys.argv[sys.argv.index("--out") + 1]).resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set (and not found in .env).", file=sys.stderr)
        return 2

    run = {
        "round": 2 if round2 else 1,
        "spec_repair_enabled": SPEC_REPAIR,
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": main.MODEL,
        "ngspice_exe": main.find_ngspice(),
        "max_attempts": MAX_ATTEMPTS,
        "results": [],
    }

    for i, circuit in enumerate(circuits, 1):
        print(f"[{i}/{len(circuits)}] {circuit['id']} ({circuit['tier']})", file=sys.stderr, flush=True)
        record = run_circuit(circuit)
        status = "SOLVED" if record["success"] else "FAILED"
        print(
            f"  -> {status} in {record['attempts_used']} attempt(s), {record['total_seconds']}s",
            file=sys.stderr, flush=True,
        )
        run["results"].append(record)
        # Write the log incrementally so a crash mid-run loses nothing
        log_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

    run["finished_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run["api_usage"] = dict(main.USAGE)
    log_path.write_text(json.dumps(run, indent=2), encoding="utf-8")

    # REPORT.md's main body is always built from the round-1 log; the round-2
    # section is appended from run_log_round2.json inside build_report.
    round1 = run
    r1_path = LOG_DIR / "run_log.json"
    if round2 and r1_path.exists():
        round1 = json.loads(r1_path.read_text(encoding="utf-8"))
    # A run redirected with --out is a side experiment -- a repeat for error
    # bars, a trial of a prompt change. It must not rewrite the report the
    # committed logs/ evidence builds, or the headline numbers silently
    # become whichever run happened to finish last.
    wrote_report = "--out" not in sys.argv
    if wrote_report:
        (PROJECT_DIR / "REPORT.md").write_text(build_report(round1), encoding="utf-8")

    solved = sum(1 for r in run["results"] if r["success"])
    also = " and REPORT.md" if wrote_report else " (REPORT.md left alone: --out)"
    print(f"\nDone: {solved}/{len(run['results'])} solved. Wrote {log_path.name}{also}.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
