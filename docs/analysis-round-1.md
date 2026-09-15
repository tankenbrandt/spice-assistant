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
