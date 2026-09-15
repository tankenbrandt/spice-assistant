# spice-assistant

[![tests](https://github.com/tankenbrandt/spice-assistant/actions/workflows/tests.yml/badge.svg)](https://github.com/tankenbrandt/spice-assistant/actions/workflows/tests.yml)

**Natural language to a verified analog circuit.** Describe a circuit, get an
ngspice netlist that simulates, then check whether it is actually *correct* --
and whether it stays correct once the parts have tolerances, the supply sags
and the board runs from -40 to +85 C.

Three layers, because "it ran" is not "it works":

| Layer | Question | Cost |
|-------|----------|------|
| `main.py` | does the deck simulate at all? | one API call per repair |
| `speccheck.py` + `specs.py` | does it meet its electrical spec? | free (ngspice only) |
| `robustness.py` | how often does it meet it, across tolerance, supply and temperature? | free (ngspice only) |

On the 8-circuit benchmark those three questions get three very different
answers: **100% simulate cleanly, 62% meet spec**, and the worst offender had
a **1% Monte Carlo yield** before repair. Sim-success overstates real
capability by 38 points.

<p align="center">
  <img src="docs/schematic.png" width="420" alt="An op-amp active low-pass rendered from an LTspice .asc file">
</p>

<p align="center">
  <img src="docs/spec-fail.png" width="760" alt="Gain of 107 V/V sitting far above the 47.5 to 52.5 acceptance band">
</p>

*The failure a "did it run?" check cannot see: this deck simulates perfectly
and has more than twice its specified gain.*

<p align="center">
  <img src="docs/spec-pass.png" width="760" alt="Gain of 51.5 V/V inside the acceptance band">
</p>

*After spec-in-the-loop repair, the same measurement inside the band.*

## The generate-and-repair CLI

A tiny CLI that turns a plain-English circuit description into a working
[ngspice](https://ngspice.sourceforge.io/) netlist. It asks Claude for a
netlist, runs it through `ngspice -b`, and if the simulation fails it feeds the
error back to the model for a correction — retrying up to 4 total attempts. At
the end it prints the final netlist **and** the full ngspice output so you can
see exactly what happened.

ngspice is called directly as a subprocess in batch mode. There is intentionally
no PySpice / InSpice / other binding — raw text in, raw text out.

## Requirements

- Python 3.10+
- ngspice on your `PATH` (or reachable via `NGSPICE_PATH`, or bundled under
  `tools/Spice64/bin`)
- An Anthropic API key

## Install

```powershell
# from the project root
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### ngspice

**Windows** (this repo was set up on Windows): the official ngspice binary is
bundled under `tools/Spice64/bin` (downloaded from SourceForge). `main.py` finds
it automatically, so nothing else is needed. To make `ngspice` available in your
own shells, add that folder to `PATH`:

```powershell
$bin = "$PWD\tools\Spice64\bin"
setx PATH "$env:PATH;$bin"   # new shells only; restart the terminal after
```

**macOS / Linux**: install via your package manager and it will be on `PATH`:

```bash
brew install ngspice      # macOS
sudo apt install ngspice  # Debian/Ubuntu
```

`main.py` resolves the executable in this order: `NGSPICE_PATH` env var →
`ngspice_con`/`ngspice` on `PATH` → the bundled `tools/Spice64/bin` copy.

### API key

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # current shell
# or persistently:
setx ANTHROPIC_API_KEY "sk-ant-..."     # new shells only
```

Or drop a `.env` file next to `main.py` — every entry point reads it, and a
real environment variable always wins over the file:

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```powershell
python main.py "a first-order RC low-pass filter with a 1kHz cutoff frequency and a 5V DC source"
```

Progress lines (attempt N, success/failure) are written to stderr; the final
netlist and full ngspice output go to stdout. The process exits `0` on a
successful simulation, `1` if all attempts failed.

## How it works

`main.py` exposes three functions:

- `generate_netlist(description)` — one Anthropic call (`claude-sonnet-5`) that
  returns netlist-only text; stray ``` fences are stripped defensively.
- `run_ngspice(netlist_text)` — writes a temp `.cir`, runs `ngspice -b`, and
  returns `(success, combined_output)`. Because ngspice often exits `0` even on a
  bad deck, success also requires that the output contains no error signatures.
- `fix_netlist(netlist_text, error_output)` — sends the failing netlist plus the
  ngspice output back to the model and returns a corrected netlist.

## Specs for your own circuits (`specs_lib/`, `specs.py`)

The benchmark's spec checks are one hand-written Python function per circuit,
which is fine for a fixed test set and useless for your own amplifier. A
**declarative spec** describes a measurement instead of implementing it --
run an analysis, pull a quantity out of the resulting vectors, compare it to
a limit:

```yaml
circuit: rc_lowpass
analyses:
  ac: {command: ac dec 200 10 1meg, vectors: [vdb(out)]}
measurements:
  - name: f3db
    label: f(-3dB)
    units: Hz
    extract: {kind: crossing, of: vdb(out), level: first - 3, direction: falling}
    target: 1000
    tol: 5%
```

```powershell
python speccheck.py my_filter.cir --spec my_filter.yaml
python robustness.py my_filter.cir --spec my_filter.yaml --mc 500 --sens --worst
```

**One spec file drives both layers.** The measurements that produce the
nominal verdict are the same ones Monte Carlo, sensitivity and worst-case
perturb, so a circuit is described once and signed off end to end.

Extractors: `max`, `min`, `mean`, `peak_to_peak`, `crossing`, `value_at`,
`at_peak`. A `window: last 20%` restricts a transient to steady state, and
levels may reference the curve itself (`first - 3`, `max / sqrt(2)`).
Measurements can be `expr`-derived from earlier ones (`q: f0 / (f_hi - f_lo)`),
and intermediates that exist only to be referenced are marked
`informational: true` so they are measured and reported without deciding the
verdict. Expressions are evaluated through an AST whitelist, so a spec file
is data, not code.

The ten specs in `specs_lib/` cover the benchmark's circuits, and the test
suite asserts that each one reaches the same verdict *and the same measured
number* as the hand-written checker it replaces.

## Plotting what the spec measured (`plot.py`)

`speccheck` prints a number; `plot.py` draws the curve it came from, with the
acceptance band on it.

```powershell
python plot.py my_filter.cir --spec my_filter.yaml --open
python plot.py deck.cir --analysis "tran 10u 5m" --vectors "v(in)" "v(out)"
```

![Bode plot of the example active low-pass](docs/response.png)

The HTML page carries a crosshair readout (every series at the pointer's
frequency), a legend, and a table of the specs, so the numbers stay reachable
without hovering. Vectors are grouped into panels **by unit**, so a Bode plot
is a magnitude panel stacked on a phase panel rather than one chart with two
y-scales: with two scales on one set of axes, wherever the curves cross is an
artefact of where the scales were pinned, not a fact about the circuit.

## A worked example (`examples/`)

`examples/` holds one circuit in all four forms: schematic, netlist, spec, and
the plot above.

```powershell
python ascview.py examples/active_lowpass.asc --open
python speccheck.py examples/active_lowpass.cir --spec examples/active_lowpass.yaml
python robustness.py examples/active_lowpass.cir --spec examples/active_lowpass.yaml --mc 500 --sens
python plot.py examples/active_lowpass.cir --spec examples/active_lowpass.yaml --open
```

The sensitivity output doubles as a check that the layer measures physics
rather than noise. For `fc = 1 / (2*pi*Rf*C1)` it reports **-1.01 %/% for C1
and -1.02 %/% for Rf**, and for the gain it reports equal and opposite
sensitivities to `Rf` and `Rg`. Those are the textbook answers.

## Viewing LTspice schematics (`ascview.py`)

Opens LTspice `.asc` schematic files **without LTspice** and renders them to a
standalone SVG, an HTML page, or a PNG — for screenshotting into lab reports.

```powershell
python ascview.py "Sallen-Key Filter Sim.asc" --open     # render + open in browser
python ascview.py sch.asc --png                          # also write a PNG
python ascview.py sch.asc --format svg --theme dark
python ascview.py *.asc --check                          # validate symbol geometry
```

The `.asc` format stores explicit coordinates for every wire, symbol and label,
so rendering needs no placement or routing — it is a parse-and-draw problem.

- `--open` writes an HTML page and opens it in the default browser.
- `--png` rasterizes via headless Chrome/Edge, sized exactly to the drawing.
- `--check` reports, per symbol, how many of its pins land on a wire endpoint.

### Symbol geometry

Pin offsets for `res`, `cap`, `voltage`, `nmos`, `pmos` and OP07-class op-amps
are **verified** against real schematics: `--check` confirms every pin lands on
a wire endpoint. The MOSFET geometry is measured across all four orientations
(`R0`, `M0`, `R180`, `M180`) that appear in a real CMOS deck, and those exact
coordinates are pinned in `tests/test_geometry.py` so the offsets cannot drift.

Other primitives (`ind`, `diode`, BJTs, `sw`) use standard LTspice geometry but
have not been checked against a real file yet — run `--check` and any mismatch
is reported rather than silently drawn wrong.

Library parts (`OpAmps\LTC2053`, vendor symbols) have per-part pin layouts that
cannot be guessed from the name. Those are drawn as a labeled block whose pins
are **inferred from the schematic's own wiring**: the pins are the wire
endpoints no other symbol claims. Connectivity is never invented — wires are
always drawn from their own coordinates.

## Known limitations

- The spec and robustness layers are **nominal-topology tools**: they perturb
  component values, supplies, model parameters and temperature. They know
  nothing about layout parasitics, EMC, or anything the netlist does not say.
- `extract_params` descends into `.subckt` definitions, so a macromodel's
  internal components are perturbed like discrete parts. For an op-amp
  macromodel that is a rough stand-in for gain-bandwidth spread rather than a
  datasheet figure, so read those sensitivities as directional.
- Specs whose **target is computed from the netlist** (the `rl_step` circuit's
  tau = L/R) are not expressible declaratively yet; a spec target is a
  constant. The hand-written check still covers that circuit.
- Symbol geometry for `ind`, `diode`, BJTs and `sw` is standard LTspice but
  **not yet verified against a real file**. `ascview --check` reports any pin
  that misses rather than drawing it wrong quietly.
- ngspice accepts some malformed decks (a component with no value) and reports
  a clean run. That gap is the reason the spec layer exists.

## Tests

```powershell
pip install -r requirements-dev.txt
pytest
```

The suite makes **no API calls** — it covers the netlist helpers, the spec
extractors, the robustness statistics and the `.asc` parser and symbol
geometry, using ngspice alone. That includes the project's central claim as an
executable contract: every baseline deck simulates cleanly, the ones the spec
layer rejects are exactly the ones the repaired decks fix, and the repair moves
the Monte Carlo *distribution* onto target rather than just the nominal value.

Tests that need ngspice skip themselves if it is not installed, so the parser
and geometry tests still run anywhere.

## License

MIT — see [LICENSE](LICENSE).
