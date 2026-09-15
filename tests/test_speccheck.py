"""Spec-verification layer: value parsing, deck rewriting, and the end-to-end
measurement of every committed deck.

The end-to-end class is the project's central claim in executable form: each
baseline deck simulates cleanly, and the ones the spec layer rejects are
exactly the ones the repaired decks fix. It runs on ngspice alone -- no API
calls, no cost -- so it is safe in CI.
"""
import pytest

import speccheck
from conftest import needs_ngspice


# ------------------------------------------------------------- value parsing

@pytest.mark.parametrize("token,expected", [
    ("1k", 1e3), ("1K", 1e3), ("2.2k", 2.2e3),
    ("1meg", 1e6), ("1MEG", 1e6), ("1G", 1e9), ("1t", 1e12),
    ("100n", 100e-9), ("4.7u", 4.7e-6), ("10p", 10e-12), ("1f", 1e-15),
    ("5m", 5e-3), ("5", 5.0), ("-2.5", -2.5), ("1e3", 1000.0),
])
def test_spice_value_suffixes(token, expected):
    assert speccheck.spice_value(token) == pytest.approx(expected)


def test_meg_beats_m_prefix():
    """SPICE's classic trap: 'm' is milli and 'meg' is mega, so a greedy
    single-letter match turns 1meg into 1 milli -- nine orders of magnitude."""
    assert speccheck.spice_value("1meg") > speccheck.spice_value("1m")
    assert speccheck.spice_value("1meg") / speccheck.spice_value("1m") == pytest.approx(1e9)


def test_trailing_unit_letters_are_ignored():
    assert speccheck.spice_value("1kohm") == pytest.approx(1e3)
    assert speccheck.spice_value("100nF") == pytest.approx(100e-9)


@pytest.mark.parametrize("bad", ["", "abc", "k"])
def test_unparseable_value_raises(bad):
    with pytest.raises(ValueError):
        speccheck.spice_value(bad)


def test_first_value_finds_first_matching_refdes():
    deck = "* title\nR1 in out 1.6k\nC1 out 0 100n\nR2 out 0 10k\n.end\n"
    assert speccheck.first_value(deck, "R") == pytest.approx(1.6e3)
    assert speccheck.first_value(deck, "C") == pytest.approx(100e-9)
    assert speccheck.first_value(deck, "L") is None


def test_first_value_skips_comments_and_directives():
    deck = "* R9 in out 999k\n.param R=1\n+ continued\nR1 in out 2k\n.end\n"
    assert speccheck.first_value(deck, "R") == pytest.approx(2e3)


# ---------------------------------------------------------- deck manipulation

def test_strip_analyses_removes_control_block_and_dot_analyses():
    deck = (
        "* title\n"
        "R1 in out 1k\n"
        ".tran 1u 1m\n"
        ".ac dec 10 1 1meg\n"
        ".control\n"
        "  tran 1u 1m\n"
        "  print v(out)\n"
        ".endc\n"
        ".end\n"
    )
    out = speccheck.strip_analyses(deck)
    assert "R1 in out 1k" in out
    for gone in (".control", ".endc", ".tran", ".ac", "print v(out)", ".end"):
        assert gone not in out, gone


def test_strip_analyses_keeps_models_and_components():
    deck = ("* t\n.model DMOD D(IS=1e-14 N=1)\nD1 a b DMOD\n"
            ".control\nop\n.endc\n.end\n")
    out = speccheck.strip_analyses(deck)
    assert ".model DMOD D(IS=1e-14 N=1)" in out
    assert "D1 a b DMOD" in out


def test_with_measurement_injects_our_control_block():
    deck = "* t\nR1 in out 1k\n.control\nop\n.endc\n.end\n"
    out = speccheck.with_measurement(deck, "ac dec 10 1 1k\nwrdata f v(out)")
    assert out.count(".control") == 1
    assert out.rstrip().endswith(".end")
    assert "wrdata f v(out)" in out
    assert "\nop\n" not in out          # the model's own analysis is gone


def test_unknown_circuit_id_is_reported_not_raised():
    r = speccheck.check("no_such_circuit", "* t\n.end\n")
    assert r["pass"] is None
    assert "no spec defined" in r["error"]


def test_check_never_raises_on_garbage():
    r = speccheck.check("rc_lowpass", "this is not a netlist at all")
    assert r["pass"] is False
    assert r["error"] is not None


def test_every_check_has_a_target_string():
    assert set(speccheck.CHECKS) == set(speccheck.TARGETS)


# ------------------------------------------------------------- end-to-end

# The benchmark's result table, as an executable contract. Each entry is
# (deck path, circuit id, expected spec verdict).
BASELINE = [
    ("baseline/rc_lowpass.cir",                "rc_lowpass",           True),
    ("baseline/rl_step.cir",                   "rl_step",              True),
    ("baseline/voltage_divider.cir",           "voltage_divider",      True),
    ("baseline/noninv_opamp.cir",              "noninv_opamp",         True),
    ("baseline/bridge_rectifier.cir",          "bridge_rectifier",     True),
    ("baseline/halfwave_rectifier.cir",        "halfwave_rectifier",   False),
    ("baseline/ce_bjt_amp.cir",                "ce_bjt_amp",           False),
    ("baseline/buck_converter.cir",            "buck_converter",       False),
    ("baseline/round2/rlc_bandpass.cir",       "rlc_bandpass",         True),
    ("baseline/round2/rc_highpass.cir",        "rc_highpass",          True),
    ("baseline/round2/cs_mosfet_amp.cir",      "cs_mosfet_amp",        True),
    ("baseline/round2/inv_opamp_gbw.cir",      "inv_opamp_gbw",        True),
    ("baseline/round2/bjt_diffamp.cir",        "bjt_diffamp",          False),
    ("baseline/round2/boost_converter.cir",    "boost_converter",      False),
    ("baseline/round2/ce_amp_bias_and_gain.cir", "ce_amp_bias_and_gain", False),
]

REPAIRED = [
    ("repaired/halfwave_rectifier.cir",          "halfwave_rectifier"),
    ("repaired/ce_bjt_amp.cir",                  "ce_bjt_amp"),
    ("repaired/buck_converter.cir",              "buck_converter"),
    ("repaired/round2/bjt_diffamp.cir",          "bjt_diffamp"),
    ("repaired/round2/boost_converter.cir",      "boost_converter"),
    ("repaired/round2/ce_amp_bias_and_gain.cir", "ce_amp_bias_and_gain"),
]


@needs_ngspice
@pytest.mark.parametrize("path,circuit,expected", BASELINE,
                         ids=[b[1] for b in BASELINE])
def test_baseline_spec_verdicts_are_reproducible(project_dir, path, circuit, expected):
    deck = (project_dir / path).read_text(encoding="utf-8")
    result = speccheck.check(circuit, deck)
    assert result["error"] is None, result["error"]
    assert result["pass"] is expected, result["measured"]


@needs_ngspice
@pytest.mark.parametrize("path,circuit", REPAIRED, ids=[r[1] for r in REPAIRED])
def test_repaired_decks_meet_spec(project_dir, path, circuit):
    """Every deck the spec layer rejected is fixed by its repaired counterpart:
    the 62% -> 100% step the report claims."""
    deck = (project_dir / path).read_text(encoding="utf-8")
    result = speccheck.check(circuit, deck)
    assert result["error"] is None, result["error"]
    assert result["pass"] is True, result["measured"]


@needs_ngspice
def test_clean_simulation_does_not_imply_correctness(project_dir):
    """The project's thesis, asserted directly: decks that ngspice runs
    without a single error can still miss their electrical target."""
    import main
    failing = [(p, c) for p, c, ok in BASELINE if not ok]
    assert failing, "expected some baseline decks to fail spec"
    for path, circuit in failing:
        deck = (project_dir / path).read_text(encoding="utf-8")
        sim_ok, _ = main.run_ngspice(deck)
        assert sim_ok is True, f"{circuit} should simulate cleanly"
        assert speccheck.check(circuit, deck)["pass"] is False
