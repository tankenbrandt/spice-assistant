"""kit.py: choosing real parts from a lab kit by simulating candidate builds.

The candidate maths is pure; the search drives ngspice. No API calls.
"""
import math

import pytest

import kit
import robustness as rb
import specs
from conftest import PROJECT_DIR, needs_ngspice

LOWPASS = PROJECT_DIR / "reference" / "inv_active_lowpass.cir"
LOWPASS_SPEC = PROJECT_DIR / "specs_lib" / "inv_active_lowpass.yaml"
CE = PROJECT_DIR / "examples" / "ce_bjt_amp_recentred.cir"
CE_SPEC = PROJECT_DIR / "specs_lib" / "ce_bjt_amp.yaml"


@pytest.fixture(scope="module")
def default_kit():
    return kit.load_kit()


# ---------------------------------------------------------------- the kit

def test_e12_resistor_range_has_every_value_once():
    vals = kit.series_values("E12", 10, 1e6)
    assert len(vals) == 5 * 12 + 1           # five decades plus the closing 1meg
    assert vals[0] == 10 and vals[-1] == 1e6
    assert 4700.0 in vals and 82000.0 in vals and 75000.0 not in vals


def test_unknown_series_is_refused():
    with pytest.raises(ValueError, match="unknown series"):
        kit.series_values("E7", 1, 10)


def test_kit_file_parses_tolerances_and_explicit_values():
    k = kit.parse_kit({"resistors": {"values": ["4.7k", 330], "tol": "1%"},
                       "capacitors": {"series": "E3", "range": ["1n", "10n"]},
                       "combine": ["series"]})
    assert k.family("r").values == [330.0, 4700.0]
    assert k.family("r").tol == pytest.approx(0.01)
    assert k.family("c").values == pytest.approx([1e-9, 2.2e-9, 4.7e-9, 1e-8])
    assert not k.family("l")
    assert k.combine == ("series",)


def test_bad_combine_entry_is_refused():
    with pytest.raises(ValueError):
        kit.parse_kit({"combine": ["diagonal"]})


# ------------------------------------------------------------ candidates

def test_a_value_the_kit_holds_is_kept_as_given(default_kit):
    c = kit.candidates(15e3, "r", default_kit)
    assert c == [kit.Choice(15e3, (15e3,), "single")]


def test_candidates_bracket_the_target_and_add_combinations(default_kit):
    c = kit.candidates(1.5915e-9, "c", default_kit)
    singles = [x.value for x in c if not x.combo]
    assert singles == pytest.approx([1.5e-9, 2.2e-9])
    best = c[0]
    assert best.combo and abs(best.value - 1.5915e-9) / 1.5915e-9 < 0.01


def test_capacitors_add_in_parallel_and_resistors_in_series(default_kit):
    cap = next(x for x in kit.candidates(1.6e-9, "c", default_kit) if x.how == "parallel")
    assert cap.value == pytest.approx(sum(cap.parts))
    res = next(x for x in kit.candidates(11e3, "r", default_kit) if x.how == "series")
    assert res.value == pytest.approx(sum(res.parts))
    a, b = kit._best_recip(default_kit.family("r").values, 11e3)
    assert a * b / (a + b) == pytest.approx(11e3)          # 22k || 22k


def test_two_builds_of_the_same_value_are_offered_once(default_kit):
    """1k + 10k and 22k || 22k both make 11k exactly: one is enough."""
    c = kit.candidates(11e3, "r", default_kit)
    assert sum(abs(x.value - 11e3) < 1e-6 for x in c) == 1


def test_singles_only_offers_no_pairs(default_kit):
    assert not any(c.combo for c in
                   kit.candidates(1.5915e-9, "c", default_kit, singles_only=True))


def test_a_family_the_kit_lacks_keeps_its_ideal_value(default_kit):
    (c,) = kit.candidates(10e-3, "l", default_kit)
    assert c.how == "ideal" and c.value == 10e-3


# -------------------------------------------------------------- the deck

def test_model_internals_are_not_kit_parts():
    deck = LOWPASS.read_text()
    keys = [p.key for p in kit.kit_params(deck)]
    assert keys == ["Rin", "Rf", "Cf"]       # not the UA741's Rin/Rp/Cp/Rout


def test_fixed_parts_are_left_out():
    keys = [p.key for p in kit.kit_params(LOWPASS.read_text(), fixed=["rf"])]
    assert keys == ["Rin", "Cf"]


def test_subcircuit_parts_get_their_own_keys():
    """Regression: the op-amp model's Rin (2 Mohm) shared a key with the
    circuit's Rin, so Monte Carlo drew the circuit's input resistor near 2 Mohm."""
    _, params = rb.extract_params(LOWPASS.read_text(), vary_temp=False)
    keys = [p.key for p in params]
    assert "Rin" in keys and "UA741.Rin" in keys
    assert len(keys) == len(set(keys))
    top = next(p for p in params if p.key == "Rin")
    assert top.nominal == 10e3


def test_scores_are_fractions_of_the_band():
    spec = specs.load(LOWPASS_SPEC)
    refs = kit.references(spec, {"gain": 20, "fc": 1000, "inversion": 180})
    assert refs["fc"] == (1000, pytest.approx(50))
    assert kit.score({"gain": 20, "fc": 1050, "inversion": 180}, refs) == \
        pytest.approx(math.sqrt(1 / 3))
    assert kit.score({"gain": 20}, refs) == kit.FAILED


# ----------------------------------------------------------------- search

@needs_ngspice
def test_lowpass_build_beats_rounding_each_part():
    p = kit.pick(specs.load(LOWPASS_SPEC), LOWPASS.read_text())
    cf = p.best.choices["Cf"]
    assert cf.combo
    assert p.best.score < p.rounded.score
    assert all(m.limits.ok(p.best.values[m.name]) for m in p.spec.specs)
    deck = p.deck()
    assert "Cf = 100pF || 1.5nF (parallel)" in deck
    assert "1.6e-09" in deck


@needs_ngspice
def test_joint_search_moves_interacting_parts_together():
    """Rc and Re2 both round down ~9%: alone each shifts the gain, together
    they keep the ratio. Rounding independently lands at 48 V/V."""
    p = kit.pick(specs.load(CE_SPEC), CE.read_text())
    assert abs(p.best.values["gain"] - 50) < 0.5
    assert abs(p.rounded.values["gain"] - 50) > 1.0
    assert p.exhaustive


@needs_ngspice
def test_a_circuit_of_kit_values_is_left_alone():
    deck = (PROJECT_DIR / "reference" / "lab_clipper.cir").read_text()
    p = kit.pick(specs.load(PROJECT_DIR / "specs_lib" / "lab_clipper.yaml"), deck)
    assert p.best.choices["R1"].value == 15e3
    assert p.sims == 1
