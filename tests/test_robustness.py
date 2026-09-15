"""Robustness signoff layer: parameter extraction, deck perturbation, and the
statistics the report is built on.

Like the spec layer this is pure ngspice -- no API calls.
"""
import random

import pytest

import robustness as rb
from conftest import needs_ngspice

DECK = """* common-emitter amp
Vcc vcc 0 12
Rc vcc c 5k
Re e 0 100
R1 vcc b 220k
R2 b 0 16k
Cin in b 1u
Q1 c b e QN2222
.model QN2222 NPN(IS=14.34f BF=200 VAF=100)
.control
op
.endc
.end
"""


# ------------------------------------------------------- parameter extraction

@pytest.fixture(scope="module")
def params():
    _lines, ps = rb.extract_params(DECK)
    return {p.key: p for p in ps}


def test_finds_passives_supply_model_and_temperature(params):
    assert {"Rc", "Re", "R1", "R2"} <= set(params)
    assert "Cin" in params
    assert "Vcc" in params
    assert "QN2222.BF" in params
    assert "TEMP" in params


def test_nominal_values_are_read_from_the_deck(params):
    assert params["Rc"].nominal == pytest.approx(5e3)
    assert params["Re"].nominal == pytest.approx(100.0)
    assert params["Cin"].nominal == pytest.approx(1e-6)
    assert params["Vcc"].nominal == pytest.approx(12.0)
    assert params["QN2222.BF"].nominal == pytest.approx(200.0)


def test_parameter_kinds_are_classified(params):
    assert params["Rc"].kind == "passive"
    assert params["Vcc"].kind == "supply"
    assert params["QN2222.BF"].kind == "model"
    assert params["TEMP"].kind == "temp"


def test_tolerances_differ_by_component_class(params):
    """A 5% resistor and a 10% capacitor must not be perturbed identically."""
    assert params["Rc"].tol < params["Cin"].tol


def test_temperature_can_be_excluded():
    _lines, ps = rb.extract_params(DECK, vary_temp=False)
    assert "TEMP" not in {p.key for p in ps}


def test_control_block_contents_are_not_perturbed():
    """A `.control` body can contain tokens that look like components; varying
    them would rewrite the analysis instead of the circuit."""
    deck = DECK.replace("op\n", "op\nlet Rc = 99\n")
    _lines, ps = rb.extract_params(deck)
    assert sum(1 for p in ps if p.key == "Rc") == 1


# ---------------------------------------------------------- deck perturbation

def test_apply_params_rewrites_only_the_targeted_value():
    lines, ps = rb.extract_params(DECK)
    by_key = {p.key: p for p in ps}
    out = rb.apply_params(DECK, ps, {**{p.key: p.nominal for p in ps}, "Rc": 10e3})
    _l2, ps2 = rb.extract_params(out)
    got = {p.key: p.nominal for p in ps2}
    assert got["Rc"] == pytest.approx(10e3)
    assert got["Re"] == pytest.approx(by_key["Re"].nominal)
    assert got["R1"] == pytest.approx(by_key["R1"].nominal)


def test_apply_params_round_trips_at_nominal():
    lines, ps = rb.extract_params(DECK)
    out = rb.apply_params(DECK, ps, {p.key: p.nominal for p in ps})
    _l2, ps2 = rb.extract_params(out)
    for a, b in zip(sorted(ps, key=lambda p: p.key), sorted(ps2, key=lambda p: p.key)):
        assert a.key == b.key
        assert a.nominal == pytest.approx(b.nominal, rel=1e-6)


def test_apply_params_sets_model_parameters():
    lines, ps = rb.extract_params(DECK)
    vals = {p.key: p.nominal for p in ps}
    vals["QN2222.BF"] = 50.0
    out = rb.apply_params(DECK, ps, vals)
    _l2, ps2 = rb.extract_params(out)
    assert {p.key: p.nominal for p in ps2}["QN2222.BF"] == pytest.approx(50.0)


def test_temperature_becomes_a_temp_directive():
    lines, ps = rb.extract_params(DECK)
    vals = {p.key: p.nominal for p in ps}
    vals["TEMP"] = 85.0
    out = rb.apply_params(DECK, ps, vals).lower()
    assert ".temp" in out and "85" in out


# ----------------------------------------------------------------- sampling

def test_samples_stay_inside_the_tolerance_band():
    p = rb.Param(key="R1", kind="passive", nominal=1000.0, tol=0.05)
    rng = random.Random(0)
    for dist in ("gaussian", "uniform"):
        for _ in range(500):
            v = p.sample(rng, dist)
            assert 950.0 <= v <= 1050.0


def test_temperature_samples_span_the_configured_range():
    p = rb.Param(key="TEMP", kind="temp", nominal=27.0, tol=0.0)
    rng = random.Random(0)
    vals = [p.sample(rng, "uniform") for _ in range(500)]
    lo, hi = rb.TEMP_RANGE
    assert all(lo <= v <= hi for v in vals)
    assert min(vals) < lo + 10 and max(vals) > hi - 10


def test_sampling_is_reproducible_for_a_seed():
    p = rb.Param(key="R1", kind="passive", nominal=1000.0, tol=0.05)
    a = [p.sample(random.Random(7), "gaussian") for _ in range(5)]
    b = [p.sample(random.Random(7), "gaussian") for _ in range(5)]
    assert a == b


def test_extremes_are_the_tolerance_limits():
    p = rb.Param(key="R1", kind="passive", nominal=1000.0, tol=0.05)
    assert p.extremes() == pytest.approx((950.0, 1050.0))


# ---------------------------------------------------------------- statistics

def test_spec_limits():
    s = rb.Spec("|gain|", 50.0, 47.5, 52.5, "V/V")
    assert s.ok(50.0) and s.ok(47.5) and s.ok(52.5)
    assert not s.ok(47.4) and not s.ok(52.6)


def test_one_sided_spec():
    s = rb.Spec("ripple", None, None, 0.1, "Vpp")
    assert s.ok(0.0) and s.ok(0.1)
    assert not s.ok(0.11)


def test_cpk_is_high_for_a_tight_centred_distribution():
    s = rb.Spec("|gain|", 50.0, 47.5, 52.5)
    tight = [50.0 + 0.01 * ((i % 21) - 10) for i in range(200)]
    assert rb.cpk(tight, s) > 1.33


def test_cpk_is_low_for_a_distribution_straddling_the_limit():
    s = rb.Spec("|gain|", 50.0, 47.5, 52.5)
    wide = [50.0 + 3.0 * ((i % 21) - 10) for i in range(200)]
    assert rb.cpk(wide, s) < 1.0


def test_cpk_needs_at_least_two_samples():
    assert rb.cpk([50.0], rb.Spec("g", 50.0, 47.5, 52.5)) is None


def test_every_metric_has_specs_and_is_callable():
    for circuit, (fn, specs) in rb.METRICS.items():
        assert callable(fn), circuit
        assert specs, circuit
        for name, spec in specs.items():
            assert spec.lsl is not None or spec.usl is not None, (circuit, name)


# ------------------------------------------------------------------ end-to-end

@needs_ngspice
def test_nominal_reproduces_the_reported_baseline_gain(project_dir):
    """The ce_bjt_amp baseline is the report's headline miss: 107 V/V against
    a 50 V/V target."""
    deck = (project_dir / "baseline/ce_bjt_amp.cir").read_text(encoding="utf-8")
    result = rb.nominal("ce_bjt_amp", deck)
    assert result["metrics"]["gain"] == pytest.approx(107.3, rel=0.02)


@needs_ngspice
def test_repaired_deck_lands_on_target(project_dir):
    deck = (project_dir / "repaired/ce_bjt_amp.cir").read_text(encoding="utf-8")
    result = rb.nominal("ce_bjt_amp", deck)
    assert result["metrics"]["gain"] == pytest.approx(51.5, rel=0.03)


@needs_ngspice
def test_monte_carlo_yield_improves_after_repair(project_dir):
    """The robustness thesis in one assertion: the repair does not merely move
    the nominal value onto target, it moves the distribution onto target."""
    before = (project_dir / "baseline/ce_bjt_amp.cir").read_text(encoding="utf-8")
    after = (project_dir / "repaired/ce_bjt_amp.cir").read_text(encoding="utf-8")
    n = 40
    mc_before = rb.monte_carlo("ce_bjt_amp", before, n, seed=1)
    mc_after = rb.monte_carlo("ce_bjt_amp", after, n, seed=1)
    assert mc_before["n_sim_fail"] == 0 and mc_after["n_sim_fail"] == 0
    assert mc_before["yield"] < 0.2
    assert mc_after["yield"] > mc_before["yield"]
    # and the spread tightens, not just the centre
    assert (mc_after["metrics"]["gain"]["sigma"]
            < mc_before["metrics"]["gain"]["sigma"])
