"""blockdiagram.py: the test-set cartoon from a deck's sources and a spec's vectors."""
import pytest

import blockdiagram as bd
import specs
from conftest import PROJECT_DIR

LAB = PROJECT_DIR / "labs" / "ee347_exp4"


def _ts(deck, spec):
    return bd.from_deck(specs.load(spec), deck.read_text(), "t")


def test_supplies_generators_and_switches_are_told_apart():
    ts = _ts(LAB / "fig4_5.cir", LAB / "fig4_5.yaml")
    assert [(n, v) for _, n, v in ts.supplies] == [("vdd", 5.0)]
    assert [n for _, n, _ in ts.generators] == ["clk"]
    assert [n for _, n, _ in ts.logic] == ["a", "b", "c", "d"]


def test_outputs_get_scope_channels_before_switched_inputs():
    """Four channels: the clock, then the circuit's own nodes X and Y.
    A switched input is a DC level you already know, so it goes last."""
    ts = _ts(LAB / "fig4_5.cir", LAB / "fig4_5.yaml")
    assert ts.channels[:3] == ["clk", "x", "y"]
    assert set(ts.overflow) <= {"a", "b", "c", "d"}
    assert len(ts.channels) == bd.SCOPE_CHANNELS


def test_the_generator_is_probed_even_when_the_spec_never_names_it():
    """Gain is Vout/Vin: the input needs a channel although the spec only
    captures vdb(out)."""
    ts = _ts(PROJECT_DIR / "reference" / "inv_active_lowpass.cir",
             PROJECT_DIR / "specs_lib" / "inv_active_lowpass.yaml")
    assert ts.channels == ["in", "out"]
    assert "vcc" not in ts.probes and "vee" not in ts.probes


def test_a_dc_only_spec_uses_a_meter_not_a_scope():
    spec = specs.parse({"analyses": {"op": {"command": "op", "vectors": ["v(out)"]}},
                        "measurements": [{"name": "v", "extract": {"kind": "max", "of": "v(out)"},
                                          "min": 1}]})
    ts = bd.from_deck(spec, "t\nV1 vcc 0 DC 12\nR1 vcc out 1k\nR2 out 0 1k\n")
    assert ts.dmm_only and ts.probes == ["out"]


def test_draws_a_png(tmp_path):
    pytest.importorskip("matplotlib")
    out = bd.draw(_ts(LAB / "fig4_5.cir", LAB / "fig4_5.yaml"), tmp_path / "ts.png")
    assert out.stat().st_size > 20_000
