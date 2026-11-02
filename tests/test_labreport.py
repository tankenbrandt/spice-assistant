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
    assert len(doc.inline_shapes) == 1
