"""labreport.py: the simulate / design / test-plan sections from one deck.

No API calls; the end-to-end tests drive ngspice.
"""
import pytest

import labreport as lr
import specs
from conftest import PROJECT_DIR, needs_ngspice

LOWPASS = PROJECT_DIR / "reference" / "inv_active_lowpass.cir"
LOWPASS_SPEC = PROJECT_DIR / "specs_lib" / "inv_active_lowpass.yaml"


def test_sources_are_described_as_bench_setups():
    deck = ("t\nVcc vcc 0 DC 12\nVee 0 vee 12\nVin in 0 DC 0 AC 1\n"
            "Vs a 0 SIN(0 5 1k)\nVp b 0 PULSE(0 5 0 1n 1n 0.5m 1m)\n"
            ".subckt X a b\nVint a b DC 3\n.ends\n")
    by = {s["name"]: s for s in lr.sources(deck)}
    assert set(by) == {"Vcc", "Vee", "Vin", "Vs", "Vp"}       # not the subckt's Vint
    assert by["Vcc"]["kind"] == "dc" and by["Vcc"]["value"] == 12
    assert by["Vin"]["kind"] == "ac"
    assert "5 V peak" in by["Vs"]["text"] and "10 Vpp" in by["Vs"]["text"]
    assert "1kHz" in by["Vs"]["text"]
    assert "0 V to 5 V" in by["Vp"]["text"] and "1kHz" in by["Vp"]["text"]


def test_instructions_read_like_bench_steps():
    spec = specs.load(LOWPASS_SPEC)
    fc = lr.instruction(spec.measurement("fc"), spec)
    assert "0.707" in fc and "frequency" in fc
    gain = lr.instruction(spec.measurement("gain"), spec)
    assert "10 Hz" in gain and "dB" in gain
    assert "calculate" in lr.instruction(spec.measurement("inversion"), spec)


def test_value_at_an_earlier_reading_is_explained():
    spec = specs.load(PROJECT_DIR / "specs_lib" / "ce_bjt_amp.yaml")
    phase = next(m for m in spec.measurements
                 if m.extract.get("kind") == "value_at")
    assert "found above" in lr.instruction(phase, spec)


def test_markdown_escapes_pipes_in_cells(tmp_path):
    md = lr.render_md([lr.Block("table", header=["a"], rows=[["x || y"]])], tmp_path)
    assert "x \\|\\| y" in md


def test_bench_range_is_the_middle_90_percent():
    mc = {"samples": [{"metrics": {"g": float(i)}} for i in range(101)]}
    lo, hi = lr.bench_range(mc, "g")
    assert lo == pytest.approx(5) and hi == pytest.approx(95)
    assert lr.bench_range({"samples": mc["samples"][:5]}, "g") is None


@needs_ngspice
def test_report_has_the_three_sections_and_the_kit_build(tmp_path):
    pytest.importorskip("docx")
    pytest.importorskip("matplotlib")
    r = lr.analyse(specs.load(LOWPASS_SPEC), LOWPASS.read_text(), title="Lab X",
                   out=tmp_path, mc_n=30)
    paths = lr.write(r, tmp_path)
    md = paths["md"].read_text(encoding="utf-8")
    for heading in ("## Simulation", "## Design", "## Test Plan", "### Results"):
        assert heading in md
    assert "100pF ‖ 1.5nF in parallel" in md
    assert "Measured" in md
    # The Monte Carlo must be centred on the real design (regression for the
    # subcircuit key collision that put the gain near -26 dB).
    assert 19 < r.mc["metrics"]["gain"]["mean"] < 21
    assert (tmp_path / "inv_active_lowpass_ac.png").stat().st_size > 10_000

    from docx import Document
    doc = Document(str(paths["docx"]))
    heads = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert heads[:3] == ["Simulation", "Component selection", "Predicted performance"] \
        or "Simulation" in heads
    assert {"Simulation", "Design", "Test Plan"} <= set(heads)
    assert len(doc.tables) >= 6
    assert len(doc.inline_shapes) == 2              # response plot + test-set diagram


# ------------------------------------------------- multi-part logic labs

LAB = PROJECT_DIR / "labs" / "ee347_exp4"
FIGS = ["fig4_1", "fig4_2", "fig4_3", "fig4_4", "fig4_5"]


def test_stepped_logic_inputs_are_described_as_logic():
    deck = "t\nVdd vdd 0 DC 5\nVa a 0 PWL(0 0 1m 0 1.001m 5)\n"
    (src,) = [s for s in lr.sources(deck) if s["name"] == "Va"]
    assert src["kind"] == "logic"
    assert "0 V and 5 V" in src["text"]


def test_bench_text_in_the_spec_wins():
    spec = specs.load(LAB / "fig4_3.yaml")
    m = spec.measurements[0]
    assert m.bench.startswith("Set A = 0 V and B = 0 V")
    assert lr.instruction(m, spec) == f"{m.title}: {m.bench}"


def test_numerical_zero_volts_read_as_zero():
    assert lr.num(2.97e-9, "V") == "0 V"
    assert lr.num(2.97e-9, "A") == "2.97e-09 A"          # a current that small is real
    assert lr._range((3.3e-8, -1e-19), "V") == "0 V"
    assert lr._range((4.877, 5.128), "V") == "4.877 to 5.128 V"


def test_lab_file_lists_every_part():
    lab = lr.load_lab(LAB / "lab.yaml")
    assert [p["deck"].stem for p in lab.parts] == FIGS
    assert all(p["deck"].exists() and p["spec"].exists() for p in lab.parts)
    assert any("Figure 3.4" in n for n in lab.notes)


@needs_ngspice
@pytest.mark.parametrize("fig", FIGS)
def test_each_ee347_circuit_meets_its_truth_table(fig):
    v = specs.verdict(specs.load(LAB / f"{fig}.yaml"), (LAB / f"{fig}.cir").read_text())
    assert v["pass"], v


@needs_ngspice
def test_nmos_pass_gate_stops_a_threshold_short_of_vdd():
    """The handout's question for Figure 4.1; Figure 4.2's transmission gates don't."""
    nmos = specs.measure(specs.load(LAB / "fig4_1.yaml"), (LAB / "fig4_1.cir").read_text())
    cmos = specs.measure(specs.load(LAB / "fig4_2.yaml"), (LAB / "fig4_2.cir").read_text())
    assert 4.5 < nmos["x1"] < 6.5
    assert cmos["x1"] > 7.9


@needs_ngspice
def test_a_miswired_domino_gate_fails_its_truth_table():
    """C wired in series with B instead of D: ABCD = 0011 no longer pulls X low."""
    deck = (LAB / "fig4_5.cir").read_text().replace("Mc x c n2 0", "Mc x c n1 0")
    v = specs.verdict(specs.load(LAB / "fig4_5.yaml"), deck)
    assert not v["pass"]
    assert not v["specs"]["Y (ABCD = 0011, evaluate)"]["pass"]


@needs_ngspice
def test_lab_report_is_one_document_with_a_timing_diagram(tmp_path):
    pytest.importorskip("docx")
    pytest.importorskip("matplotlib")
    lab, results = lr.run_lab(LAB / "lab.yaml", tmp_path, mc_n=0)
    paths = lr.write_lab(lab, results, tmp_path)
    md = paths["md"].read_text(encoding="utf-8")
    assert md.count("\n## Figure 4.") == 5
    assert md.count("### Test Plan") == 5
    assert "Component selection" not in md                  # nothing to pick
    assert (tmp_path / "ee347_fig4_5.cir").exists()          # hand-in netlist
    assert (tmp_path / "ee347_fig4_5_tran.png").stat().st_size > 10_000

    from docx import Document
    doc = Document(str(paths["docx"]))
    assert len(doc.inline_shapes) == 10             # a timing diagram and a test set per part


def test_power_rating_is_the_next_standard_size_at_twice_the_dissipation():
    assert lr.rating_for(0.010) == 1 / 8
    assert lr.rating_for(0.070) == 1 / 4
    assert lr.rating_for(0.2) == 1 / 2
    assert lr.watts_text(0.25) == "1/4 W" and lr.watts_text(2) == "2 W"


@needs_ngspice
def test_design_lab_report_compares_both_designs_at_the_extremes(tmp_path):
    pytest.importorskip("docx")
    pytest.importorskip("matplotlib")
    lab, results = lr.run_lab(PROJECT_DIR / "labs" / "ee346_exp8" / "lab.yaml", tmp_path, mc_n=20)
    r = results[0]
    assert r.pick.kit.name.startswith("EE 3306L")              # the lab's own kit
    md = lr.write_lab(lab, results, tmp_path)["md"].read_text(encoding="utf-8")
    assert "Both designs at the extremes" in md and "✗" in md and "✓" in md
    assert "Q-point at the extremes" in md and "Resistor power ratings" in md
    qp = r.study["qpoints"]
    assert len(qp) == 2 and all(2 < vce < 15 for _, _, _, _, vce, _, _ in qp)


def test_a_lab_part_can_carry_its_ltspice_schematic(tmp_path):
    import ascview
    if not ascview.find_chromium():
        pytest.skip("no Chromium/Edge to rasterise the SVG")
    asc = PROJECT_DIR / "labs" / "ee346_exp8" / "ce_amp.asc"
    copied, png = lr.attach_schematic(asc, tmp_path)
    assert copied.read_text() == asc.read_text()
    assert png and png.stat().st_size > 10_000
