"""The declarative spec engine.

The important class here is the cross-validation at the bottom: for every
circuit that has both a hand-written checker and a spec file, the two must
reach the same verdict on the same deck. That is what makes the declarative
format trustworthy -- it is not a parallel implementation with its own
opinions, it reproduces the measurements the report was built from.
"""
import pytest

import specs
from conftest import needs_ngspice

MINIMAL = {
    "circuit": "demo",
    "analyses": {"ac": {"command": "ac dec 10 1 1meg", "vectors": ["vdb(out)"]}},
    "measurements": [
        {"name": "f3db", "extract": {"kind": "crossing", "level": "first - 3"},
         "target": 1000, "tol": "5%"},
    ],
}


def _spec(**overrides):
    data = {**MINIMAL, **overrides}
    return specs.parse(data)


# --------------------------------------------------------------- expressions

@pytest.mark.parametrize("expr,expected", [
    ("1 + 2", 3), ("10 / 4", 2.5), ("2 ** 10", 1024), ("-5", -5),
    ("abs(-3)", 3), ("sqrt(16)", 4), ("20 * log10(100)", 40),
    ("max(1, 7, 3)", 7), ("min(1, 7, 3)", 1),
])
def test_safe_eval_arithmetic(expr, expected):
    assert specs.safe_eval(expr, {}) == pytest.approx(expected)


def test_safe_eval_binds_names():
    assert specs.safe_eval("f0 / (hi - lo)", {"f0": 100, "hi": 60, "lo": 40}) == 5


def test_numbers_pass_straight_through():
    assert specs.safe_eval(3.5, {}) == 3.5
    assert specs.safe_eval(7, {}) == 7.0


def test_unknown_name_names_what_is_available():
    with pytest.raises(specs.SpecError, match="alpha"):
        specs.safe_eval("nope + 1", {"alpha": 1, "beta": 2})


@pytest.mark.parametrize("expr", [
    "__import__('os').system('echo hi')",
    "open('x')",
    "(1).__class__",
    "[1, 2, 3]",
    "lambda: 1",
    "'a string'",
    "print(1)",
])
def test_expressions_are_not_arbitrary_python(expr):
    """Spec files are data. Nothing in them reaches the interpreter."""
    with pytest.raises(specs.SpecError):
        specs.safe_eval(expr, {})


def test_division_by_zero_is_a_spec_error_not_a_traceback():
    with pytest.raises(specs.SpecError, match="division by zero"):
        specs.safe_eval("a / b", {"a": 1.0, "b": 0.0})


def test_math_domain_error_is_reported_clearly():
    with pytest.raises(specs.SpecError):
        specs.safe_eval("log10(x)", {"x": -1.0})


# -------------------------------------------------------------------- limits

def test_target_plus_percent_tolerance():
    lim = specs.limits_from({"target": 100, "tol": "5%"}, "Hz")
    assert (lim.lsl, lim.usl) == pytest.approx((95.0, 105.0))
    assert lim.ok(95.0) and lim.ok(105.0)
    assert not lim.ok(94.9) and not lim.ok(105.1)


def test_tolerance_accepts_fraction_or_percent():
    a = specs.limits_from({"target": 100, "tol": "5%"}, "")
    b = specs.limits_from({"target": 100, "tol": 0.05}, "")
    assert (a.lsl, a.usl) == pytest.approx((b.lsl, b.usl))


def test_absolute_tolerance():
    lim = specs.limits_from({"target": 0, "abs_tol": 0.5}, "dB")
    assert lim.ok(0.5) and lim.ok(-0.5) and not lim.ok(0.6)


def test_one_sided_limit():
    lim = specs.limits_from({"max": 0.1}, "Vpp")
    assert lim.ok(0.0) and lim.ok(0.1) and not lim.ok(0.11)
    assert lim.lsl is None


def test_a_measurement_must_have_a_band():
    with pytest.raises(specs.SpecError, match="acceptance band"):
        specs.limits_from({}, "")


def test_informational_measurement_needs_no_band():
    lim = specs.limits_from({"informational": True}, "Hz")
    assert not lim.bounded
    assert lim.ok(1e9)          # nothing is out of spec if there is no spec


def test_bad_tolerance_string_is_rejected():
    with pytest.raises(specs.SpecError, match="tolerance"):
        specs.limits_from({"target": 1, "tol": "loose"}, "")


# --------------------------------------------------------------- spec parsing

def test_single_analysis_is_implied():
    """With exactly one analysis defined, measurements need not name it."""
    assert _spec().measurements[0].analysis == "ac"


def test_multiple_analyses_must_be_named():
    data = {
        "measurements": [{"name": "x", "extract": {"kind": "max"}, "max": 1}],
        "analyses": {
            "a": {"command": "op", "vectors": ["v(out)"]},
            "b": {"command": "ac dec 10 1 1k", "vectors": ["v(out)"]},
        },
    }
    with pytest.raises(specs.SpecError, match="must name an analysis"):
        specs.parse(data)


def test_unknown_extractor_lists_the_real_ones():
    with pytest.raises(specs.SpecError, match="crossing"):
        _spec(measurements=[{"name": "x", "extract": {"kind": "wat"}, "max": 1}])


def test_unknown_analysis_is_rejected():
    with pytest.raises(specs.SpecError, match="unknown analysis"):
        _spec(measurements=[{"name": "x", "analysis": "dc",
                             "extract": {"kind": "max"}, "max": 1}])


def test_duplicate_measurement_names_are_rejected():
    m = {"name": "dup", "extract": {"kind": "max"}, "max": 1}
    with pytest.raises(specs.SpecError, match="duplicate"):
        _spec(measurements=[dict(m), dict(m)])


def test_measurement_needs_extract_or_expr():
    with pytest.raises(specs.SpecError, match="extract"):
        _spec(measurements=[{"name": "x", "max": 1}])


def test_measurement_cannot_have_both():
    with pytest.raises(specs.SpecError, match="both"):
        _spec(measurements=[{"name": "x", "extract": {"kind": "max"},
                             "expr": "1 + 1", "max": 1}])


def test_spec_without_measurements_is_rejected():
    with pytest.raises(specs.SpecError, match="measurements"):
        specs.parse({"circuit": "x"})


def test_missing_file_is_reported():
    with pytest.raises(specs.SpecError, match="not found"):
        specs.load("no/such/spec.yaml")


def test_analysis_control_block_is_ordered():
    """Options run before the analysis command, which runs before wrdata."""
    a = specs.Analysis(name="ac", command="ac dec 10 1 1k",
                       vectors=["v(out)", "v(in)"], options=["op", "set units=degrees"])
    body = a.control_body()
    assert body.index("op") < body.index("ac dec")
    assert body.index("ac dec") < body.index("wrdata")
    assert "wrdata meas.txt v(out) v(in)" in body


# ---------------------------------------------------------------- extractors

XS = [1.0, 2.0, 3.0, 4.0, 5.0]
YS = [10.0, 20.0, 30.0, 20.0, 10.0]


def _ctx(**values):
    return specs.Context(vectors={}, values=values)


def test_max_min_mean_ptp():
    c = _ctx()
    assert specs.e_max(XS, YS, {}, c) == 30.0
    assert specs.e_min(XS, YS, {}, c) == 10.0
    assert specs.e_mean(XS, YS, {}, c) == pytest.approx(18.0)
    assert specs.e_ptp(XS, YS, {}, c) == 20.0


def test_at_peak_returns_the_x_not_the_y():
    assert specs.x_at_peak(XS, YS, {}, _ctx()) == 3.0


def test_window_restricts_to_the_tail():
    xs = list(range(11))
    ys = [0.0] * 6 + [100.0] * 5
    assert specs.e_mean(xs, ys, {"window": "last 20%"}, _ctx()) == 100.0
    assert specs.e_mean(xs, ys, {}, _ctx()) < 100.0


def test_bad_window_is_rejected():
    with pytest.raises(specs.SpecError, match="window"):
        specs.e_mean(XS, YS, {"window": "most of it"}, _ctx())


def test_crossing_level_can_reference_the_curve():
    """`max - 10` is 20, first reached rising between x=1 and x=2."""
    cfg = {"kind": "crossing", "level": "max - 10", "direction": "rising"}
    assert specs.e_crossing(XS, YS, cfg, _ctx()) == pytest.approx(2.0)


def test_crossing_can_search_either_side_of_the_peak():
    cfg = {"level": 20, "direction": "rising", "search": "before_peak"}
    assert specs.e_crossing(XS, YS, cfg, _ctx()) == pytest.approx(2.0)
    cfg = {"level": 20, "direction": "falling", "search": "after_peak"}
    assert specs.e_crossing(XS, YS, cfg, _ctx()) == pytest.approx(4.0)


def test_crossing_that_never_happens_says_so():
    with pytest.raises(specs.SpecError, match="no rising crossing"):
        specs.e_crossing(XS, YS, {"level": 999, "direction": "rising"}, _ctx())


def test_bad_direction_is_rejected():
    with pytest.raises(specs.SpecError, match="rising or falling"):
        specs.e_crossing(XS, YS, {"level": 20, "direction": "sideways"}, _ctx())


def test_value_at_interpolates():
    assert specs.e_value_at(XS, YS, {"at": 1.5}, _ctx()) == pytest.approx(15.0)
    assert specs.e_value_at(XS, YS, {"at": 3.0}, _ctx()) == pytest.approx(30.0)


def test_value_at_clamps_outside_the_sweep():
    assert specs.e_value_at(XS, YS, {"at": -99}, _ctx()) == 10.0
    assert specs.e_value_at(XS, YS, {"at": 99}, _ctx()) == 10.0


def test_value_at_can_reference_an_earlier_measurement():
    """This is how a common-mode gain is read at the differential peak."""
    got = specs.e_value_at(XS, YS, {"at": "f_peak"}, _ctx(f_peak=3.0))
    assert got == pytest.approx(30.0)


def test_curve_stats_shadow_earlier_measurements():
    """`max` always means this curve's maximum, whatever else is named max."""
    cfg = {"level": "max - 10", "direction": "rising"}
    assert specs.e_crossing(XS, YS, cfg, _ctx(max=1e6)) == pytest.approx(2.0)


# ------------------------------------------------------------- shipped specs

SPEC_FILES = [
    "rc_lowpass", "rc_highpass", "voltage_divider", "halfwave_rectifier",
    "ce_bjt_amp", "buck_converter", "boost_converter", "rlc_bandpass",
    "bjt_diffamp", "ce_amp_bias_and_gain",
]


@pytest.mark.parametrize("name", SPEC_FILES)
def test_shipped_specs_load(project_dir, name):
    spec = specs.load(project_dir / "specs_lib" / f"{name}.yaml")
    assert spec.name == name
    assert spec.specs, "a spec file with nothing to check is not a spec"


@pytest.mark.parametrize("name", SPEC_FILES)
def test_shipped_specs_describe_themselves(project_dir, name):
    spec = specs.load(project_dir / "specs_lib" / f"{name}.yaml")
    assert "(unbounded)" not in specs.describe(spec)


def test_json_and_yaml_are_equivalent(project_dir, tmp_path):
    import json
    spec = specs.load(project_dir / "specs_lib" / "rc_lowpass.yaml")
    raw = {
        "circuit": "rc_lowpass",
        "analyses": {"ac": {"command": "ac dec 200 10 1meg", "vectors": ["vdb(out)"]}},
        "measurements": [{"name": "f3db", "label": "f(-3dB)", "units": "Hz",
                          "extract": {"kind": "crossing", "of": "vdb(out)",
                                      "level": "first - 3", "direction": "falling"},
                          "target": 1000, "tol": "5%"}],
    }
    p = tmp_path / "rc_lowpass.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    from_json = specs.load(p)
    assert specs.describe(from_json) == specs.describe(spec)


# ------------------------------------------------------------- cross-validation

# Every circuit that has both a hand-written checker and a spec file, with the
# decks to compare them on.
PAIRS = [
    ("rc_lowpass", "baseline/rc_lowpass.cir"),
    ("voltage_divider", "baseline/voltage_divider.cir"),
    ("rc_highpass", "baseline/round2/rc_highpass.cir"),
    ("rlc_bandpass", "baseline/round2/rlc_bandpass.cir"),
    ("halfwave_rectifier", "baseline/halfwave_rectifier.cir"),
    ("halfwave_rectifier", "repaired/halfwave_rectifier.cir"),
    ("ce_bjt_amp", "baseline/ce_bjt_amp.cir"),
    ("ce_bjt_amp", "repaired/ce_bjt_amp.cir"),
    ("buck_converter", "baseline/buck_converter.cir"),
    ("buck_converter", "repaired/buck_converter.cir"),
    ("boost_converter", "baseline/round2/boost_converter.cir"),
    ("boost_converter", "repaired/round2/boost_converter.cir"),
    ("bjt_diffamp", "baseline/round2/bjt_diffamp.cir"),
    ("bjt_diffamp", "repaired/round2/bjt_diffamp.cir"),
    ("ce_amp_bias_and_gain", "baseline/round2/ce_amp_bias_and_gain.cir"),
    ("ce_amp_bias_and_gain", "repaired/round2/ce_amp_bias_and_gain.cir"),
]


@needs_ngspice
@pytest.mark.parametrize("name,deck", PAIRS, ids=[f"{n}-{d.split('/')[0]}" for n, d in PAIRS])
def test_declarative_matches_handwritten_checker(project_dir, name, deck):
    """The whole justification for the format: same deck, same verdict."""
    import speccheck
    netlist = (project_dir / deck).read_text(encoding="utf-8")
    spec = specs.load(project_dir / "specs_lib" / f"{name}.yaml")

    handwritten = speccheck.check(name, netlist)
    declared = specs.verdict(spec, netlist)
    assert declared["pass"] is handwritten["pass"], (
        f"{deck}: handwritten={handwritten['measured']} "
        f"declared={declared['measured'] or declared['error']}")


@needs_ngspice
@pytest.mark.parametrize("name,deck,metric,expected", [
    ("rc_lowpass", "baseline/rc_lowpass.cir", "f3db", 997.8),
    ("voltage_divider", "baseline/voltage_divider.cir", "vout", 3.3),
    ("ce_bjt_amp", "baseline/ce_bjt_amp.cir", "gain", 107.31),
    ("ce_bjt_amp", "repaired/ce_bjt_amp.cir", "gain", 51.53),
    ("buck_converter", "baseline/buck_converter.cir", "vout", 4.519),
    ("rlc_bandpass", "baseline/round2/rlc_bandpass.cir", "q", 5.0),
])
def test_declarative_reproduces_the_reported_numbers(project_dir, name, deck,
                                                     metric, expected):
    """Not just the same verdict -- the same measured value the report prints."""
    netlist = (project_dir / deck).read_text(encoding="utf-8")
    spec = specs.load(project_dir / "specs_lib" / f"{name}.yaml")
    values = specs.measure(spec, netlist)
    assert values[metric] == pytest.approx(expected, rel=0.01)


@needs_ngspice
def test_verdict_reports_a_measurement_failure_instead_of_raising(project_dir):
    """A batch run must survive one unmeasurable deck."""
    spec = specs.load(project_dir / "specs_lib" / "rc_lowpass.yaml")
    result = specs.verdict(spec, "* empty deck\nV1 a 0 1\nR1 a 0 1k\n.end\n")
    assert result["pass"] is False
    assert result["error"]


@needs_ngspice
def test_informational_values_are_measured_but_do_not_decide(project_dir):
    spec = specs.load(project_dir / "specs_lib" / "rlc_bandpass.yaml")
    netlist = (project_dir / "baseline/round2/rlc_bandpass.cir").read_text(encoding="utf-8")
    result = specs.verdict(spec, netlist)
    assert result["pass"] is True
    assert set(result["specs"]) == {"f0", "Q"}
    assert set(result["informational"]) == {"f_lo", "f_hi"}
    # and the derived Q really is built from the informational band edges
    v = result["values"]
    assert v["q"] == pytest.approx(v["f0"] / (v["f_hi"] - v["f_lo"]))


# ------------------------------------------------ robustness layer integration

@needs_ngspice
def test_one_spec_file_drives_the_robustness_layer(project_dir):
    """The payoff: a circuit is described once and signed off end to end."""
    import robustness as rb
    spec = specs.load(project_dir / "specs_lib" / "ce_bjt_amp.yaml")
    before = (project_dir / "baseline/ce_bjt_amp.cir").read_text(encoding="utf-8")
    after = (project_dir / "repaired/ce_bjt_amp.cir").read_text(encoding="utf-8")

    assert rb.nominal(spec, before)["metrics"]["gain"] == pytest.approx(107.3, rel=0.02)

    mc_before = rb.monte_carlo(spec, before, 40, seed=1)
    mc_after = rb.monte_carlo(spec, after, 40, seed=1)
    assert mc_before["yield"] < mc_after["yield"]
    assert mc_after["metrics"]["gain"]["sigma"] < mc_before["metrics"]["gain"]["sigma"]


@needs_ngspice
def test_sensitivity_ranks_the_gain_setting_resistor_first(project_dir):
    """A degenerated common-emitter stage has gain ~ Rc/Re, so the unbypassed
    emitter resistor must dominate. This also guards the ranking against a
    wrapped-phase metric swamping it, which it previously did."""
    import robustness as rb
    spec = specs.load(project_dir / "specs_lib" / "ce_bjt_amp.yaml")
    netlist = (project_dir / "repaired/ce_bjt_amp.cir").read_text(encoding="utf-8")
    sens = rb.sensitivity(spec, netlist)
    ranked = [r["param"] for r in sens["rows"] if "gain" in r["metrics"]]
    assert ranked[0] == "Re1"
    assert "Rc" in ranked[:3]


def test_unknown_builtin_circuit_lists_the_known_ones():
    import robustness as rb
    with pytest.raises(KeyError, match="ce_bjt_amp"):
        rb.resolve("no_such_circuit")
