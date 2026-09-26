"""The lab round: specs for circuits from a real course handout.

Each spec is held to two things. The hand-built deck in reference/ must pass
it -- otherwise the spec is wrong, not the model -- and a plausibly broken
version of that deck must fail it, or the spec is not checking anything.
The broken versions are the mistakes the handout itself is about: a diode
the wrong way round, a missing battery, an RC too short to clamp.
"""
from pathlib import Path

import pytest

import benchmark
import ltspice
import speccheck
import specs
from conftest import needs_ngspice

ROOT = Path(__file__).resolve().parent.parent
LAB = [c["id"] for c in benchmark.CIRCUITS_LAB]


def _deck(cid):
    return (ROOT / "reference" / f"{cid}.cir").read_text(encoding="utf-8")


def _spec(cid):
    return specs.load(ROOT / "specs_lib" / f"{cid}.yaml")


def test_every_lab_circuit_has_a_spec_and_a_reference_deck():
    for cid in LAB:
        assert (ROOT / "specs_lib" / f"{cid}.yaml").exists(), cid
        assert (ROOT / "reference" / f"{cid}.cir").exists(), cid


@needs_ngspice
@pytest.mark.parametrize("cid", LAB)
def test_reference_deck_meets_its_spec(cid):
    v = specs.verdict(_spec(cid), _deck(cid))
    assert v["pass"], v["measured"] or v["error"]


@needs_ngspice
@pytest.mark.parametrize("cid", LAB)
def test_benchmark_check_reads_the_yaml_spec(cid):
    """No hand-written check exists for these; speccheck falls back."""
    assert cid not in speccheck.CHECKS
    assert speccheck.check(cid, _deck(cid))["pass"]


BROKEN = [
    ("lab_clipper", "no batteries: clips at one diode drop",
     [("V1 n1 0 DC 1.5", "V1 n1 0 DC 0"), ("V2 0 n2 DC 1.5", "V2 0 n2 DC 0")]),
    ("lab_clipper", "D2 reversed", [("D2 n2 out", "D2 out n2")]),
    ("lab_clamper", "diode reversed: clamps negative", [("D1 0 out", "D1 out 0")]),
    ("lab_clamper", "R = 2.2k: RC too short to clamp", [("R1 out 0 68k", "R1 out 0 2.2k")]),
    ("lab_halfwave", "diode reversed", [("D1 in out", "D1 out in")]),
    ("lab_bridge", "one diode missing: half-wave", [("D3 outn in D1N4001\n", "")]),
    # Both of these are "repairs" the model actually made in the first lab
    # run: change the stimulus the handout fixes until the number fits.
    ("lab_bridge", "source cut to 9.5 V to hit Vdc", [("SIN(0 10 60)", "SIN(0 9.5 60)")]),
    ("lab_clamper", "input amplitude changed", [("PULSE(-5 5", "PULSE(-4 4")]),
    ("inv_active_lowpass", "capacitor 10x too big",
     [("Cf minus out 1.5915n", "Cf minus out 15.915n")]),
    ("inv_active_lowpass", "wired non-inverting",
     [("Rin in minus 10k", "Rin 0 minus 10k"), ("XU1 0 minus", "XU1 in minus")]),
]


@needs_ngspice
@pytest.mark.parametrize("cid,why,subs", BROKEN, ids=[f"{c}: {w}" for c, w, _ in BROKEN])
def test_spec_rejects_a_broken_version(cid, why, subs):
    deck = _deck(cid)
    for old, new in subs:
        assert old in deck
        deck = deck.replace(old, new)
    v = specs.verdict(_spec(cid), deck)
    assert not v["pass"], f"{why} passed: {v['measured']}"


@needs_ngspice
def test_rectifier_targets_are_the_exact_average_not_vp_over_pi():
    """The handout's check is Vdc ~ Vp/pi = 3.18 V. A real diode both drops
    Vd and conducts for less of the cycle, which puts it near 2.87 V. A spec
    built on Vp/pi would fail a correct circuit."""
    import math
    vp, vd = 10.0, 0.65
    t0 = math.asin(vd / vp)
    exact = (vp * math.cos(t0) - vd * (math.pi / 2 - t0)) / math.pi
    assert _spec("lab_halfwave").measurement("vdc").limits.ok(exact)
    assert not _spec("lab_halfwave").measurement("vdc").limits.ok(vp / math.pi)


@needs_ngspice
def test_clamp_level_ignores_the_switching_spike_at_each_edge():
    """With 1 ns input edges the diode's switching transient dips to about
    -1.0 V for a moment. A min() read that as the clamp level and failed a
    deck identical to the reference; the time-percentile level does not
    move with rise time."""
    fast = _deck("lab_clamper").replace("PULSE(-5 5 0 1u 1u", "PULSE(-5 5 0 1n 1n")
    spec = _spec("lab_clamper")
    v_fast = specs.verdict(spec, fast)
    v_slow = specs.verdict(spec, _deck("lab_clamper"))
    assert v_fast["values"]["vlow"] == pytest.approx(v_slow["values"]["vlow"], abs=0.02)
    assert v_fast["pass"]


def test_ltspice_reads_a_differential_vector():
    """ngspice's v(a,b) -- the bridge's floating load -- is a difference of
    two node voltages in LTspice's raw file."""
    raw = ltspice.Raw(variables=["time", "V(outp)", "V(outn)"],
                      points=[[0.0, 5.0, 1.0], [1.0, 7.0, 2.0]])
    assert ltspice.vector(raw, "v(outp,outn)") == [4.0, 5.0]
    assert ltspice.vector(raw, "v(outp, outn)") == [4.0, 5.0]
