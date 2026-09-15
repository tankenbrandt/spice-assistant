### Round-2 narrative (grounded in run_log_round2.json)

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
