"""Tests for the LTspice measurement backend.

The deck translation and raw-file parsing are pure text handling, so they are
tested against fixtures and run everywhere, CI included. Only the tests that
actually invoke LTspice are skipped when it is not installed.
"""
import math

import pytest

import crosscheck
import ltspice
import specs

# ------------------------------------------------------------ deck translation


def test_strip_control_removes_the_block():
    deck = """RC test
V1 in 0 AC 1
R1 in out 1k

.control
ac dec 20 1 1meg
print v(out)
.endc

.end
"""
    out = ltspice.strip_control(deck)
    assert ".control" not in out
    assert "print" not in out
    assert "R1 in out 1k" in out


def test_strip_control_is_case_insensitive():
    assert ".AC" not in ltspice.strip_control(".CONTROL\n.AC dec 1 1 2\n.ENDC\n")


def test_directive_prefixes_a_bare_command():
    assert ltspice.directive("ac dec 20 10 10meg") == ".ac dec 20 10 10meg"


def test_directive_passes_an_existing_directive_through():
    assert ltspice.directive(".tran 1u 1m") == ".tran 1u 1m"


def test_deck_for_puts_the_directive_before_end():
    deck = "title\nR1 a 0 1k\n.end\n"
    out = ltspice.deck_for(deck, "op")
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert lines[-2] == ".op"
    assert lines[-1] == ".end"
    # Exactly one .end, or LTspice stops at the first and ignores the rest.
    assert sum(ln == ".end" for ln in lines) == 1


def test_deck_for_keeps_the_title_line():
    """The first line of a deck is its title; promoting it to a component or
    dropping it changes the circuit."""
    out = ltspice.deck_for("My Circuit Title\nR1 a 0 1k\n.end\n", "op")
    assert out.splitlines()[0] == "My Circuit Title"


# ------------------------------------------------------------ analysis options


def test_alter_sets_ac_magnitude_and_phase():
    deck = "t\nVinp inp 0 DC 0 AC 1\nR1 inp 0 1k\n.end\n"
    out = ltspice.apply_options(
        deck, ["alter @vinp[acmag]=0.5", "alter @vinp[acphase]=180"])
    line = next(ln for ln in out.splitlines() if ln.startswith("Vinp"))
    assert "AC 0.5 180" in line
    assert "DC 0" in line       # the DC value must survive


def test_alter_on_a_source_with_no_ac_spec_adds_one():
    out = ltspice.apply_options("t\nV1 a 0 DC 5\n", ["alter @v1[acmag]=2"])
    assert "AC 2 0" in out


def test_alter_leaves_other_sources_alone():
    deck = "t\nVinp inp 0 AC 1\nVinn inn 0 AC 1\n"
    out = ltspice.apply_options(deck, ["alter @vinp[acmag]=0.5"])
    assert "Vinn inn 0 AC 1" in out


def test_op_option_is_a_noop():
    """LTspice always solves the operating point before an AC analysis, so
    ngspice's explicit `op` beforehand has nothing to translate to."""
    assert ltspice.apply_options("t\nR1 a 0 1k\n", ["op"]) == "t\nR1 a 0 1k\n"


def test_unknown_option_raises_rather_than_being_ignored():
    """The failure this guards against is silent: a dropped `alter` once left
    a differential-pair measurement reading common-mode gain instead."""
    with pytest.raises(ltspice.UnsupportedOption):
        ltspice.apply_options("t\nR1 a 0 1k\n", ["alter @v1[temp]=50"])


def test_alter_naming_a_missing_source_raises():
    with pytest.raises(ltspice.UnsupportedOption):
        ltspice.apply_options("t\nR1 a 0 1k\n", ["alter @vnope[acmag]=1"])


# ---------------------------------------------------------------- raw parsing

AC_RAW = """Title: * test
Date: Wed Sep 16 13:38:19 2026
Plotname: AC Analysis
Flags: complex forward log
No. Variables: 3
No. Points: 2
Offset: 0.0000000000000000e+00
Command: Linear Technology Corporation LTspice
Variables:
\t0\tfrequency\tfrequency
\t1\tV(in)\tvoltage
\t2\tV(out)\tvoltage
Values:
0\t\t1.000000000000000e+01,0.000000000000000e+00
\t1.000000000000000e+00,0.000000000000000e+00
\t3.000000000000000e+00,4.000000000000000e+00
1\t\t1.000000000000000e+02,0.000000000000000e+00
\t1.000000000000000e+00,0.000000000000000e+00
\t0.000000000000000e+00,-1.000000000000000e+00
"""

TRAN_RAW = """Title: * test
Plotname: Transient Analysis
Flags: real forward
No. Variables: 2
No. Points: 3
Variables:
\t0\ttime\ttime
\t1\tV(out)\tvoltage
Values:
0\t0.000000000000000e+00
\t0.000000000000000e+00
1\t-1.000000000000000e-03
\t2.500000000000000e+00
2\t2.000000000000000e-03
\t5.000000000000000e+00
"""

OP_RAW = """Title: * test
Plotname: Operating Point
Flags: real
No. Variables: 2
No. Points: 1
Variables:
\t0\tV(in)\tvoltage
\t1\tV(out)\tvoltage
Values:
0\t1.200000000000000e+01
\t3.300000000000000e+00
"""


def test_parse_ac_header_and_shape():
    raw = ltspice.parse_raw(AC_RAW)
    assert raw.plotname == "AC Analysis"
    assert raw.complex_data
    assert raw.variables == ["frequency", "V(in)", "V(out)"]
    assert len(raw.points) == 2


def test_parse_reads_complex_pairs():
    raw = ltspice.parse_raw(AC_RAW)
    assert raw.points[0][2] == complex(3.0, 4.0)


def test_parse_truncated_file_raises():
    """A run cut short leaves a short raw file; reading it as if complete
    would silently measure a partial sweep."""
    short = AC_RAW.replace("No. Points: 2", "No. Points: 9")
    with pytest.raises(ValueError):
        ltspice.parse_raw(short)


@pytest.mark.parametrize("name,expected", [
    ("vm(out)", 5.0),                       # |3+4j|
    ("vr(out)", 3.0),
    ("vi(out)", 4.0),
    ("vdb(out)", 20 * math.log10(5.0)),
    ("vp(out)", math.degrees(math.atan2(4.0, 3.0))),
])
def test_ac_accessors(name, expected):
    raw = ltspice.parse_raw(AC_RAW)
    assert ltspice.vector(raw, name)[0] == pytest.approx(expected)


def test_plain_v_on_an_ac_run_is_magnitude():
    """ngspice's wrdata reports v(x) as magnitude on a complex sweep, so the
    two backends must agree on the spelling as well as the number."""
    raw = ltspice.parse_raw(AC_RAW)
    assert ltspice.vector(raw, "v(out)")[0] == pytest.approx(5.0)


def test_vector_on_a_real_run_is_the_value():
    raw = ltspice.parse_raw(TRAN_RAW)
    assert ltspice.vector(raw, "v(out)") == pytest.approx([0.0, 2.5, 5.0])


def test_unknown_vector_names_the_available_ones():
    raw = ltspice.parse_raw(AC_RAW)
    with pytest.raises(KeyError, match="V\\(in\\)"):
        ltspice.vector(raw, "v(nope)")


def test_transient_time_axis_is_unsigned():
    """LTspice flags a compressed transient point by writing its time
    negative; the magnitude is the real time."""
    raw = ltspice.parse_raw(TRAN_RAW)
    assert ltspice.sweep_axis(raw) == pytest.approx([0.0, 1e-3, 2e-3])


def test_operating_point_axis_is_not_the_first_node_voltage():
    """An operating point has no sweep variable. Returning column 0 would
    hand measurements V(in) where they expect time or frequency."""
    raw = ltspice.parse_raw(OP_RAW)
    assert ltspice.sweep_axis(raw) == [0.0]
    assert ltspice.vector(raw, "v(out)") == pytest.approx([3.3])


# ------------------------------------------------------------------ comparison


def test_row_diff_is_symmetric_and_bounded():
    """Scaling by the larger magnitude keeps a near-zero reading from
    producing a meaningless six-figure percentage."""
    r = crosscheck.Row("x", 1e-9, 25.0, False)
    assert r.diff_pct == pytest.approx(100.0, abs=0.01)
    flipped = crosscheck.Row("x", 25.0, 1e-9, False)
    assert flipped.diff_pct == pytest.approx(r.diff_pct)


def test_row_agreement_uses_the_tolerance():
    r = crosscheck.Row("g", 100.0, 100.5, False)
    assert r.agrees(1.0)
    assert not r.agrees(0.1)


def test_report_does_not_fail_on_an_informational_disagreement():
    """`f_mid` is an argmax over a flat response: the two simulators pick
    different points without disagreeing about the circuit."""
    rows = [crosscheck.Row("f_mid", 1.26e6, 1e7, True),
            crosscheck.Row("gain", 75.6, 75.4, False)]
    _text, ok = crosscheck.report("t", rows, tol=1.0)
    assert ok


def test_report_fails_on_a_spec_disagreement():
    rows = [crosscheck.Row("ripple", 0.0085, 0.011, False)]
    _text, ok = crosscheck.report("t", rows, tol=1.0)
    assert not ok


# --------------------------------------------------- live LTspice (skippable)

@pytest.fixture(scope="module")
def have_ltspice():
    if not ltspice.available():
        pytest.skip("LTspice not installed")


RC_DECK = """RC low-pass, fc = 1 kHz
Vin in 0 DC 0 AC 1
R1 in out 1k
C1 out 0 159.15n
.control
ac dec 20 1 1meg
.endc
.end
"""


def test_live_rc_cutoff_matches_theory(have_ltspice):
    """A 1k/159.15n low-pass is 1/(2*pi*R*C) = 1.000 kHz by construction."""
    raw = ltspice.run(RC_DECK, "ac dec 50 10 100k")
    freqs = ltspice.sweep_axis(raw)
    db = ltspice.vector(raw, "vdb(out)")
    f3 = min(zip(freqs, db, strict=True), key=lambda p: abs(p[1] + 3.0103))[0]
    assert f3 == pytest.approx(1000.0, rel=0.03)


def test_live_agrees_with_ngspice_on_the_same_spec(have_ltspice):
    """The headline claim: two independently written simulators, one spec."""
    spec = specs.load("specs_lib/rc_lowpass.yaml")
    netlist = open("baseline/rc_lowpass.cir", encoding="utf-8").read()
    rows = crosscheck.compare(spec, netlist)
    assert rows, "no measurements compared"
    for r in rows:
        assert r.agrees(1.0), f"{r.name}: {r.ngspice} vs {r.ltspice}"


def test_live_op_analysis_reads_node_voltages(have_ltspice):
    raw = ltspice.run("Divider\nV1 in 0 DC 12\nR1 in out 8.7k\nR2 out 0 3.3k\n.end\n",
                      "op")
    assert ltspice.vector(raw, "v(out)")[0] == pytest.approx(3.3, rel=1e-3)


def test_live_control_block_would_otherwise_simulate_nothing(have_ltspice):
    """Guards the translation itself: the deck still carries its `.control`
    block, and it is the stripping that makes LTspice run the analysis."""
    raw = ltspice.run(RC_DECK, "ac dec 20 1 1meg")
    assert len(raw.points) > 10
    assert raw.plotname.lower().startswith("ac")
