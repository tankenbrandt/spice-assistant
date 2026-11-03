---
name: prelab
description: Turn a lab handout PDF into a finished pre-lab (Simulation, Design, Layout, Test Plan .docx) with this repo's labreport.py. Use when Tommy says /prelab, hands over a lab handout, or asks for a pre-lab, prelab or lab report sections for an EE lab.
---

# /prelab: handout PDF → pre-lab

`labreport.py` does everything after the circuit is written down: the kit
search, ngspice and LTspice at every condition, Monte Carlo, the pin
assignments, the test plan and the Word file. Your job here is the one
step it can't do: **read the handout and write the lab's three input
files**, then run it, check it and report back.

No API calls: this runs in the Claude Code session. Python is
`.venv\Scripts\python.exe` (there is no `python` on PATH).

## Inputs

- The handout PDF. Course PDFs sit in the repo root and are gitignored;
  **never commit a PDF.** Read all of it, including the pre-lab questions,
  the SPICE hints page and the equipment list.
- Whatever the handout points at: datasheets, Appendix A pinouts, the parts
  kit (`kits/3306L.yaml` for EE 346/3306L), earlier handouts it cites for
  values (e.g. W/L from a previous experiment).
- Values Tommy measured in earlier labs (e.g. β from Exp. 7) if the handout
  asks for "your measured value". Look for them in the conversation or ask.
  Never invent a measurement.

If something the circuit needs is in none of these, ask with
AskUserQuestion: name the file that would have it (a figure from another
week's handout means asking for that handout). Don't guess it silently. If
the user isn't there to ask, make the most defensible choice, mark it
ASSUMED in the notes, and put the question in your report.

## Write `labs/<course>_<exp>/`

Read the worked examples first and copy their shape:
`labs/ee346_exp8/` (analog design to spec, with kit, corners, `.asc`),
`labs/ee347_exp4/` (five logic circuits, truth tables, no kit) and
`labs/ee347_exp3/` (timing: rise/fall/delay from `crossing` extractors with
a `window`, combined by `expr`). The spec
format is documented in `docs/how-it-works.md` ("Specs for your own
circuits") and in `specs.py`.

1. **One netlist per circuit, `<name>.cir`.** Its first line is the title
   (SPICE ignores line 1). Name nodes after the handout's labels (`in`,
   `out`, `b`, `e`, `x`, `y`). Sources are written the way the bench sets
   them. The models are the handout's own `.model` values. Comment each
   block with where it came from ("handout part B", "Exp. 3 Fig. 3.4").
   A design lab starts from the course's worked or hand design, never from
   a design you made up.
2. **One spec per circuit, `<name>.yaml`.** Every handout requirement
   becomes a check, and **every check traces to a handout line**: quote
   it in a comment at the top (see `ce_amp.yaml`). Add no requirements of
   your own. A derived limit (e.g. "no distortion" → 1 V of V_CE headroom)
   is stated as an interpretation, in a comment and in the lab notes.
   - **"Measure X" with no limit** (most of a handout): `record: true`. The
     reading is tabulated everywhere with "record (no limit)" and never
     becomes a pass/fail check you invented. Scratch values that only feed
     an `expr` (a crossing time) are `informational: true`, which keeps
     them out of the tables.
   - A check the handout's own definitions need is fine, labelled as an
     interpretation: e.g. "10% to 90% of the final value" needs the output
     to settle, so VOH/VOL must reach logic levels.
   - Write a `bench:` line for each measurement: what to connect and read.
   - Extremes the handout names (temperature, β_min/β_max) go under
     `corners:`. A design lab uses `objective: margin` and
     `maximize_margin: true` on the headroom checks.
   - Quote any vector with a comma: `"v(out,e)"`. Unquoted YAML splits it.
   - If the handout fixes the output amplitude, set the drive from the
     measured gain (the `ac` + `let` + `alter @vin[sin]` options in
     `ce_amp.yaml`).
3. **`lab.yaml`**: title, `kit:` (only for labs where you choose part
   values; without it every part is kept exactly as the handout gives it),
   `notes:` and `parts:` (title, deck, spec, `fix:` for parts the handout
   gives in a design lab, optional `asc:`). **Every assumed value goes in
   `notes`** (what it is and why it was needed). So does the scope: which
   handout parts are left out and why.
4. **Stimuli the handout leaves to the student** ("select appropriate pulse
   parameters"): choose them, do a trial run if you need one to choose, and
   write the choice and its reason in the notes as CHOSEN.

### Rules learned the hard way

- **Logic stimuli:** change inputs 50 µs *after* a clock or select edge,
  never on it, or the race shows up as a glitch that isn't in the circuit.
- **Level-1 MOSFETs** need `CGSO/CGDO/CBD/CBS` and `.options method=gear`,
  or the run dies with "timestep too small".
- **A `.subckt`'s internal parts** must not share names with top-level
  ones that Monte Carlo varies. Monte Carlo keys them `SUBCKT.REFDES`.
- **Don't do the hand calculations** (step 2 of the ten-step method). The
  report says so on purpose. Tommy does them.
- **`.asc` for hand-in:** only when the course hands in LTspice schematics
  (EE 346 does) or Tommy asks; a handout that asks for `.cir` files gets the
  netlists. Try `ascgen.generate()` first and render it with
  `ascview.py --png`. If the layout crosses wires or doesn't read like
  the handout's figure, lay out the `.asc` by hand like the figure. The
  report has LTspice netlist it and refuses a sheet that doesn't match the
  design, so a wrong value can't slip through. Say in the notes who drew it.

## Run and check

```powershell
.venv\Scripts\python.exe labreport.py --lab labs/<dir>/lab.yaml --out runs/<dir>
```

Then read `runs/<dir>/report.md` yourself before reporting:

- **"N/N checks pass" isn't the goal.** A hand design that fails a check is
  a *finding*, and the kit search exists to fix it. But a check that fails
  because the spec is wrong (wrong window, wrong node, a level the handout
  never asked for) is your bug: fix the spec, not the circuit.
- Each requirement in the handout has a row, and each table the handout
  wants filled ("Simulated Data") can be filled from the report.
- Pre-lab questions: a "what is X" question is answered by a row. A "why"
  or "compare" question is Tommy's to write, like the hand calculations:
  list it in your report-back with the numbers it rests on.
- The LTspice section ran: it says it ran the `.asc` itself, or the netlist,
  and its checks agree with ngspice. If it says the schematic doesn't
  match the design, fix the `.asc`.
- The layout section has every IC and transistor placed. "No pinout on
  file" means `layout.PACKAGES` needs the part: add it from the handout's
  pinout or datasheet, with a test.
- No `ASSUMED` value is missing from `lab.yaml` notes.

Run the test suite (`.venv\Scripts\python.exe -m pytest -q`) and
`ruff check .`, then commit the `labs/<dir>/` files on green. Don't commit
`runs/`, PDFs or the .docx.

## Report back to Tommy

Lead with the answer in a few lines:

1. The `.docx` path (`runs/<dir>/report.docx`; uploading it to Google Drive
   makes it a Google Doc).
2. What the simulation found that matters at the bench: a hand design that
   fails, the kit parts picked, the numbers to expect.
3. **Everything assumed**, and what would replace each assumption (a
   datasheet, a measurement).
4. Anything the handout asks that the report doesn't cover, including the
   "why" questions left for him, and the numbers each one rests on.
