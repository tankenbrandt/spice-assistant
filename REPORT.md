# spice-assistant Benchmark Report

- **Run started:** 2026-07-08T00:10:23+00:00
- **Run finished:** 2026-07-08T00:12:55+00:00
- **Model:** claude-sonnet-5
- **ngspice:** C:\Users\tommy\Somerset Systems\SPICE-assistant\tools\Spice64\bin\ngspice_con.exe
- **Max attempts per circuit:** 4

## Overall results

- **Simulation success (clean run): 8/8 (100%)**
- **First-attempt clean-run rate: 7/8**
- **Spec compliance (electrically correct): 5/8 (62%)**
- **Spec compliance after spec-in-the-loop repair (stage 2): 8/8** — see the repair section below
- **API usage:** 11 calls, 35,184 in / 11,632 out tokens ≈ **$0.187** (claude-sonnet-5 intro pricing). Spec checks use ngspice only — no API cost.

## Solve rate by difficulty tier

| Tier | Solved | Total | Solve rate | Avg attempts (solved) |
|------|--------|-------|------------|------------------------|
| easy | 3 | 3 | 100% | 1.0 |
| medium | 3 | 3 | 100% | 1.0 |
| hard | 2 | 2 | 100% | 2.5 |

## Per-circuit results

| Circuit | Tier | Result | Attempts | Failure categories seen |
|---------|------|--------|----------|--------------------------|
| rc_lowpass | easy | SOLVED | 1 | — |
| rl_step | easy | SOLVED | 1 | — |
| voltage_divider | easy | SOLVED | 1 | — |
| halfwave_rectifier | medium | SOLVED | 1 | — |
| ce_bjt_amp | medium | SOLVED | 1 | — |
| noninv_opamp | medium | SOLVED | 1 | — |
| bridge_rectifier | hard | SOLVED | 4 | control_or_vector, convergence |
| buck_converter | hard | SOLVED | 1 | — |

## Spec compliance (electrical correctness)

A clean ngspice run says nothing about whether the circuit meets its
electrical targets. This section re-runs each solved netlist with a
harness-controlled measurement (.control + wrdata), extracts real
output vectors, and asserts the spec.

| Circuit | Tier | Sim | Spec target | Measured | Spec |
|---------|------|-----|-------------|----------|------|
| rc_lowpass | easy | clean | -3 dB point at 1 kHz ±5% | f(-3dB) = 997.8 Hz (-0.2% vs 1 kHz) | **PASS** |
| rl_step | easy | clean | time constant within 5% of L/R = 1.000e-04 s (deck: R=100, L=0.01) | tau = 9.990e-05 s (-0.1% vs L/R) | **PASS** |
| voltage_divider | easy | clean | V(out) = 3.3 V ±1% | V(out) = 3.3000 V (+0.00%) | **PASS** |
| halfwave_rectifier | medium | clean | peak drop ≤ 1 diode drop (≤1.0 V) vs 10 V input; V(out) never < -0.1 V | peak_out = 8.93 V (drop 1.07 V vs peak_in 10.00 V), min = -0.000 V | **FAIL** |
| ce_bjt_amp | medium | clean | mid-band \|gain\| = 50 ±5%, inverting | \|gain\| = 107.31 (+114.6%), phase = -180 deg | **FAIL** |
| noninv_opamp | medium | clean | gain = 11 (1 + Rf/Rin) ±2% | gain = 11.000 (-0.00%) | **PASS** |
| bridge_rectifier | hard | clean | steady-state output ripple < 1.0 Vpp into 100 ohm load | ripple = 0.367 Vpp (avg 12.66 V) over last 20% of 300 ms | **PASS** |
| buck_converter | hard | clean | V(out) = 5 V ±3% average, ripple ≤ 100 mVpp, steady state | V(out) avg = 4.519 V (-9.6%), ripple = 8.3 mVpp | **FAIL** |

### Spec-compliance rate by tier

| Tier | Sim clean | Spec pass | Total |
|------|-----------|-----------|-------|
| easy | 3 | 3 | 3 |
| medium | 3 | 1 | 3 |
| hard | 2 | 1 | 2 |

### ⚠️ Ran cleanly but NOT electrically correct

These circuits would be scored as 'solved' by a did-it-run check —
the failure mode that check misses entirely:

- **halfwave_rectifier** (medium): peak_out = 8.93 V (drop 1.07 V vs peak_in 10.00 V), min = -0.000 V
- **ce_bjt_amp** (medium): |gain| = 107.31 (+114.6%), phase = -180 deg
- **buck_converter** (hard): V(out) avg = 4.519 V (-9.6%), ripple = 8.3 mVpp

## Spec-in-the-loop repair (stage 2)

The spec failures above were repaired by feeding the **measured value**
(not ngspice stderr, which was clean) back into a redesign step, then
re-verifying with the same measurement harness.
Performed by: in-session Claude (Claude Code subscription) — per user cost rule: benchmark ran once on API; repair iteration done on subscription. Zero API calls in this stage.

| Circuit | Before (measured) | Targeted change | After (measured) | Iterations |
|---------|-------------------|-----------------|------------------|------------|
| ce_bjt_amp | \|gain\| = 107.31 (+114.6%), phase = -180 deg | Split emitter resistance into unbypassed Re1=56 + bypassed Re2=910; rebias with R1=68k/R2=11k (IE~1.0mA); Rc=7.5k so gain ~ (Rc\|\|RL\|\|ro)/(re+Re1) ~ 50; removed 600-ohm series source resistor so measured source-referred gain equals designed gain. | \|gain\| = 51.53 (+3.1%), phase = -180 deg **PASS** | 1 |
| buck_converter | V(out) avg = 4.519 V (-9.6%), ripple = 8.3 mVpp | Scaled duty by 5/4.519 -> 46.1% (PULSE width 4.61u at 10u period). No other changes. | V(out) avg = 5.085 V (+1.7%), ripple = 8.5 mVpp **PASS** | 1 |
| halfwave_rectifier | peak_out = 8.93 V (drop 1.07 V vs peak_in 10.00 V), min = -0.000 V | Diode model changed to IS=1e-12, N=1 (RS/BV kept). No topology change. | peak_out = 9.40 V (drop 0.60 V vs peak_in 10.00 V), min = -0.000 V **PASS** | 1 |

**Spec compliance after repair: 8/8 (from 5/8). Every repair converged in a single iteration once the measured value was available as feedback.**

Repaired netlists: `repaired/*.cir`; full before/after evidence: `logs/repair_log.json`.

## Failure pattern analysis (from logged ngspice output)

Counts are over all failed attempts, including early attempts of
circuits that were eventually solved.

| Category | Failed attempts | Example (verbatim from log) |
|----------|-----------------|------------------------------|
| convergence | 3 | bridge_rectifier attempt 1: doAnalyses: TRAN:  Timestep too small; time = 0.0101951, timestep = 6.25e-17: trouble with node "a" |
| control_or_vector | 2 | bridge_rectifier attempt 1: Warning from checkvalid: vector vout_ripple is not available or has zero length. |

### Narrative (grounded in logs/run_log.json)

**1. The system-prompt fix worked.** The previous run's only error class —
`batch_analysis_invocation` (mixing `.op`/`.ac`/`.tran` dot-lines with a `.control run`
block) — occurred **zero times** in this run after adding the "all analyses inside one
`.control` block" rule. First-attempt clean-run rate improved from 6/8 to **7/8**.

**2. The one remaining sim failure was genuinely physical.** `bridge_rectifier` took 4
attempts, all failures being real transient convergence breakdowns at the diode nodes:

```
doAnalyses: TRAN:  Timestep too small; time = 0.0101951, timestep = 6.25e-17: trouble with node "a"
```

The model's successful attempt-4 repair used textbook SPICE convergence craft: 1 Ω series
resistors on the source legs (`Rs1`/`Rs2`), a softer diode model, and
`.options GMIN=1e-9 RELTOL=0.01 ... METHOD=GEAR`. This is a different — and more
interesting — failure class than the invocation errors the prompt fix eliminated.

**3. The headline: 3 of 8 circuits ran cleanly but are NOT electrically correct.**
Exactly the failure mode a did-it-run check cannot see, with root causes visible in the
final netlists:

- **ce_bjt_amp — measured |gain| = 107 vs target 50 (+115%).** The deck fully bypasses
  the 100 Ω emitter resistor with a 100 µF capacitor, so the AC gain is set by the BJT's
  intrinsic re (≈26 mV/IE) rather than a designed Rc/Re ratio — a classic way to get
  "some large gain" instead of a *specified* gain. Notably, the model's own `.control`
  block computed `gain = vm(out)/vm(in)` and printed it, but never compared it to −50.
- **buck_converter — measured 4.519 V vs 5 V ±3% (−9.6%).** The deck uses duty cycle
  4.167 µs/10 µs = 41.7%, which gives exactly D·Vin = 5.0 V for an *ideal* converter —
  but never compensates for the freewheeling diode drop during the (1−D) interval, which
  costs ≈0.5 V at the output. The ripple spec was met with huge margin (8.3 mVpp vs
  100 mV allowed), so the topology and LC sizing are fine; the control parameter is wrong.
- **halfwave_rectifier — peak drop 1.07 V vs ≤1.0 V allowed (marginal).** The generated
  diode model uses emission coefficient N=1.5, which at the ~9 mA load current gives a
  1.07 V forward drop — a real silicon diode (N≈1) drops ~0.6–0.7 V here. The waveform
  shape is correct (min = −0.000 V, never negative); the component *model parameters* are
  slightly non-physical.

**Takeaway.** Once invocation errors are prompt-fixed, the remaining gap splits cleanly
in two: (a) rare genuine convergence failures, which the existing error-feedback loop
self-heals (bridge, 4 attempts); and (b) **quantitative design errors — 3/8 circuits —
which the loop never even sees**, because ngspice exits cleanly. Sim-success (100%)
overstates real capability by 38 percentage points versus spec compliance (62%). Closing
that gap requires feeding *measured values* back into the repair loop (spec-in-the-loop),
not just stderr — which stage 2 (below) confirmed: all three repairs converged in a
single iteration once the measured number was available.

Full per-attempt netlists and ngspice output: `logs/run_log.json`.

---

## Round 2: Generalization Test (new circuits, --spec-repair enabled)

- **Run:** 2026-07-08T00:42:00+00:00 → 2026-07-08T00:46:07+00:00 | model claude-sonnet-5
- **API usage:** 13 calls, 6,602 in / 20,444 out tokens ≈ **$0.218** (includes automated spec-repair rounds)
- **Simulation success (clean run): 7/7**
- **First-attempt clean-run rate: 6/7**
- **Spec compliance BEFORE repair: 4/7**
- **Spec compliance AFTER automated spec-repair: 5/7**

### Per-circuit results

| Circuit | Tier | Sim attempts | Spec before repair | Repair rounds | Spec after repair |
|---------|------|--------------|--------------------|---------------|--------------------|
| rlc_bandpass | easy | 1 | PASS | 0 | **PASS** — f0: 10000 Hz (+0.0%); Q: 5.00 (-0.0%) |
| rc_highpass | easy | 1 | PASS | 0 | **PASS** — f(-3dB) = 2004.8 Hz (+0.2% vs 2 kHz) |
| cs_mosfet_amp | medium | 1 | PASS | 0 | **PASS** — \|gain\| = 9.34 (-6.6%) |
| inv_opamp_gbw | medium | 1 | PASS | 0 | **PASS** — gain: \|gain\| = 20.00 (-0.0%); bandwidth: f(-3dB) = 95.3 kHz |
| bjt_diffamp | medium | 2 | FAIL — diff_gain: Ad = 0.00 (-100.0%); cmrr: >200 dB (Ad=0.00, Acm≈0) | 2 | **FAIL** — RuntimeError: wrdata output file was not created (check vector names) |
| boost_converter | hard | 1 | FAIL — V(out) avg = 11.129 V (-7.3%), ripple = 171.0 mVpp | 1 | **PASS** — V(out) avg = 11.766 V (-1.9%), ripple = 177.0 mVpp |
| ce_amp_bias_and_gain | hard | 1 | FAIL — vce: Vce = 12.00 V (+100.0%); gain: \|gain\| = 77.31 (-3.4%), phase = -180 deg | 2 | **FAIL** — RuntimeError: could not read v(coll)/v(emit) from op output (node naming?) |

### Coupled-spec analysis (did repair break the other spec?)

| Circuit | Sub-spec | Before repair | After repair | Outcome |
|---------|----------|---------------|--------------|---------|
| rlc_bandpass | f0 | PASS (10000 Hz (+0.0%)) | PASS (10000 Hz (+0.0%)) | no repair needed |
| rlc_bandpass | Q | PASS (5.00 (-0.0%)) | PASS (5.00 (-0.0%)) | no repair needed |
| inv_opamp_gbw | gain | PASS (\|gain\| = 20.00 (-0.0%)) | PASS (\|gain\| = 20.00 (-0.0%)) | no repair needed |
| inv_opamp_gbw | bandwidth | PASS (f(-3dB) = 95.3 kHz) | PASS (f(-3dB) = 95.3 kHz) | no repair needed |
| bjt_diffamp | diff_gain | FAIL (Ad = 0.00 (-100.0%)) | unmeasurable (RuntimeError: wrdata output file was not created (check vector names)) | **unmeasurable after repair** — repair broke the sim/harness contract, not this spec specifically |
| bjt_diffamp | cmrr | PASS (>200 dB (Ad=0.00, Acm≈0)) | unmeasurable (RuntimeError: wrdata output file was not created (check vector names)) | **unmeasurable after repair** — repair broke the sim/harness contract, not this spec specifically |
| ce_amp_bias_and_gain | vce | FAIL (Vce = 12.00 V (+100.0%)) | unmeasurable (RuntimeError: could not read v(coll)/v(emit) from op output (node naming?)) | **unmeasurable after repair** — repair broke the sim/harness contract, not this spec specifically |
| ce_amp_bias_and_gain | gain | PASS (\|gain\| = 77.31 (-3.4%), phase = -180 deg) | unmeasurable (RuntimeError: could not read v(coll)/v(emit) from op output (node naming?)) | **unmeasurable after repair** — repair broke the sim/harness contract, not this spec specifically |

### Repair convergence

- Passed spec with no repair needed: **4**
- Repaired, converged in 1 round: **1**
- Repaired, converged in >1 round: **0**
- Repair failed to converge (after 2 rounds): **2**

### Round 1 vs Round 2

| Metric | Round 1 | Round 2 |
|--------|---------|---------|
| Circuits | 8 | 7 |
| Sim success | 8/8 | 7/7 |
| First-attempt clean run | 7/8 | 6/7 |
| Spec compliance before repair | 5/8 | 4/7 |
| Spec compliance after repair | 8/8 (manual, in-session) | 5/7 (automated --spec-repair) |
| API cost | $0.187 (repairs free, in-session) | $0.218 (repairs included) |

### Round-2 narrative (grounded in logs/run_log_round2.json)

**What generalized well.** 4/7 circuits passed spec with no repair, including *both*
easy/medium coupled-spec circuits — and those are the striking ones:

- `rlc_bandpass`: f0 = 10000 Hz (+0.0%), Q = 5.00 (−0.0%) — the model solved the
  two-coupled-constraints component-selection problem (three components, two targets)
  exactly, first try.
- `inv_opamp_gbw`: gain = 20.00 (−0.0%) **and** f(−3dB) = 95.3 kHz. The theoretical
  bandwidth for gain-20 inverting with a 2 MHz GBW op-amp is GBW/noise-gain =
  2 MHz/21 = 95.2 kHz — the model built a finite-GBW single-pole op-amp model and landed
  within 0.1% of the theoretical trade-off point. This is genuine understanding of the
  gain-bandwidth coupling, not tuning.

**The topology test (boost) — automated repair generalized correctly.** Before repair:
11.129 V (−7.3%). The repair changed pulse width 5.833 µs → 6.05 µs (ΔD = +2.2 points).
Boost math predicts ΔVout = Vin/(1−D)²·ΔD ≈ 0.62 V; measured change was +0.64 V →
11.766 V PASS. Had the model pattern-matched round 1's buck fix (proportional duty
scaling, D → D·12/11.129 = 0.629), the boost law Vout = Vin/(1−D) predicts ~13.5 V — a
gross overshoot. The correction magnitude matches inverse-topology reasoning, not
pattern-matching. **1 round, converged.**

**The two automated-repair failures — and what they actually reveal.**

1. `bjt_diffamp` — **circuit identity drift, caught only by the spec layer.** Generation
   attempt 1 returned an *empty netlist*. The error-fix loop (whose prompt contains only
   the netlist + error, **not the original circuit description**) then produced… a
   circuit titled "Simple RC Circuit Test" — V1/R1/C1, no transistors. It simulates
   cleanly, so the clean-run loop scored it SOLVED; the spec layer measured Ad = 0.00 and
   caught the impostor. Automated spec repair couldn't recover: round 1's output failed
   the clean-run check, and round 2's output dropped the harness's `vinp`/`vinn` naming
   (measurement error). **Failed after 2 rounds.**
2. `ce_amp_bias_and_gain` — **a silently-corrupted measurement, not a bias design error.**
   The deck's bias network is actually correct (bisected: Vce = 5.96 V, within spec!) —
   but the output node connects only through the coupling cap (no load), leaving `out`
   floating at DC. ngspice's `op` silently degenerates to a cutoff solution (v(base) =
   176 µV, Vce reads 12.00 V) while the `ac` command internally converges to the correct
   active bias in the *same run* (gain 77.3 measured). One deck, two operating points, no
   error message. Automated repair failed the same way as the diffamp (round 1 broke the
   sim, round 2 broke node naming). **Failed after 2 rounds.**

**The coupled-spec question — answered with a controlled experiment.** The automated
repairs never got far enough to trade one spec against the other. So we measured the trap
directly on `ce_amp_bias_and_gain`: the *naive* fix for the floating node (add a typical
10 kΩ load) fixes Vce (5.96 V PASS) **but breaks the previously-passing gain spec**
(77.3 → 63.59, −20.5% FAIL) because RL loads the collector. The correct fix (light 100 kΩ
load) passes both: Vce = 5.96 V, gain = 75.68. This is the "fix one spec, silently break
the other" failure mode in its purest measurable form — and only per-sub-spec
re-verification (this round's requirement 3) can see it.

**In-session repairs (stage 2b, zero API — per the project cost rule):** both automated
failures were then repaired in-session using the full context (description + measured
values + diagnosis): `bjt_diffamp` rebuilt as a real differential pair (ideal 1 mA tail,
Rc = 2.6 kΩ → Ad = gm·Rc/2 ≈ 25) → measured **Ad = 24.81 (−0.8%), CMRR = 116.4 dB PASS**;
`ce_amp_bias_and_gain` got the light-load fix → **both specs PASS**. One iteration each.
Decks in `repaired/round2/`.

**Harness lessons (the honest part).** Both automated-repair failures trace as much to
two harness design gaps as to model capability: (a) `fix_netlist` and `fix_netlist_spec`
prompts don't include the original circuit description or harness naming requirements, so
nothing anchors circuit identity or the measurement contract during repair; (b) when a
repair round fails the clean-run check, the next round receives only the string
"clean-run failed after spec repair" — no ngspice error output to work from. Fixing both
(carry the description through the repair chain; feed sim errors into the next round) is
the highest-leverage improvement before a round 3, and the in-session repairs — which had
exactly that context and converged in one iteration each — are direct evidence it would
close the gap.

**Bottom line vs round 1.** Round 1's failures were invocation conventions and quantitative
sizing — all recoverable with the measured value. Round 2 surfaced three genuinely new
failure classes: circuit identity drift under an unanchored fix loop, silent DC-solution
corruption from a floating node, and repair-induced violation of the measurement contract.
Spec-in-the-loop repair generalizes when the failure is quantitative (boost); it does not
yet self-recover when the failure is structural — that requires anchoring the repair
prompts, which is a harness fix, not a model limitation.

Full round-2 evidence: `logs/run_log_round2.json`.
