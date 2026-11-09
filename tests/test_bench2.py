"""Benchmark v2: as-built grading closes the loopholes the first A/B exposed.

The decks here are the shapes a model actually produced (logs/ab_tolerance):
a resistor split into four series pieces, an off-series value, an ideal
current source.
"""

import pytest

import asbuilt
import bench2
import kit
import specs
from conftest import needs_ngspice

CE_SPEC = specs.load("specs_lib/ce_bjt_amp.yaml")
COMMODITY = {"r": 0.05, "c": 0.10, "l": 0.20}
PRECISION = {"r": 0.01, "c": 0.05, "l": 0.10}


def test_e96_is_the_rounded_geometric_series():
    assert kit.E_SERIES["E96"] == [round(10 ** (i / 96), 2) for i in range(96)]


@pytest.mark.parametrize("v,series,want", [
    (10.55, "E24", 11.0), (59.0, "E24", 62.0), (7960.0, "E96", 7870.0),
    (1.5915e-9, "E24", 1.6e-9), (4700.0, "E24", 4700.0), (0.0, "E24", 0.0)])
def test_snap_value_picks_the_nearest_buyable_part(v, series, want):
    assert asbuilt.snap_value(v, series) == pytest.approx(want)


def test_series_follows_the_part_grade():
    assert asbuilt.series_for("r", 0.05) == "E24"
    assert asbuilt.series_for("r", 0.01) == "E96"
    assert asbuilt.series_for("c", 0.05) == "E24"
    assert asbuilt.series_for("c", 0.10) == "E12"
    assert asbuilt.series_for("l", 0.05) == "E12"


SPLIT = """split
Vcc vcc 0 DC 12
R1A vcc d1 1.5k
R1B d1 d2 1.5k
R1C d2 b 1.5k
R2A b 0 1k
R2B b 0 1k
C1 out 0 1u
C2 out 0 1u
RL out x 1k
Q1 x b e QN
.model QN NPN(BF=200)
.end
"""


def test_merge_collapses_series_chains_and_parallel_pairs():
    deck, merges = asbuilt.merge_passives(SPLIT, keep={"0", "out"})
    lines = deck.splitlines()
    assert "R1A vcc b 4500" in lines                    # three in series
    assert "R2A b 0 500" in lines                       # two in parallel
    assert "C1 out 0 2e-06" in lines
    assert not any(ln.startswith(("R1B", "R1C", "R2B", "C2")) for ln in lines)
    assert len(merges) == 4


def test_merge_keeps_probed_and_shared_nodes():
    deck = "t\nVin in 0 DC 0 AC 1\nR1 in out 1k\nR2 out 0 1k\nR3 b x 1k\nR4 x y 1k\nQ1 x b 0 QN\n.end\n"
    merged, merges = asbuilt.merge_passives(deck, keep={"0", "out", "in"})
    assert merges == []          # 'out' is measured; 'x' also touches Q1
    assert merged.splitlines()[2:6] == deck.splitlines()[2:6]


def test_merge_ignores_subcircuit_internals():
    deck = "t\nX1 a b OA\n.subckt OA p q\nR1 p m 1k\nR2 m q 1k\n.ends\n.end\n"
    assert asbuilt.merge_passives(deck, keep={"0"})[1] == []


def test_prepare_snaps_each_part_before_merging():
    """Two buyable parts may make an unbuyable total; the chain stays exact."""
    deck = "t\nVcc vcc 0 DC 12\nR1 vcc m 10.55\nR2 m out 4.7k\nR3 out 0 4.7k\n.end\n"
    p = asbuilt.prepare(deck, specs.load("specs_lib/voltage_divider.yaml"), COMMODITY)
    assert "R1 10.55 -> 11" in p["snaps"]
    assert "R1 vcc out 4711" in p["deck"].splitlines()


def test_violations_name_ideal_sources_and_extra_supplies():
    deck = ("t\nVcc vcc 0 DC 12\nVb b 0 DC 0.7\nVin in 0 DC 0 AC 1\nI1 vcc c 1m\n"
            "E1 o 0 in 0 10\n.end\n")
    v = asbuilt.violations(deck, asbuilt.BASE_ELEMENTS, supplies=1)
    assert "I1: ideal current source not allowed" in v
    assert "E1: ideal controlled source not allowed" in v
    assert "2 DC sources; the prompt gives 1" in v
    assert asbuilt.violations(deck.replace("I1 vcc c 1m\n", ""),
                              asbuilt.BASE_ELEMENTS | {"e"}, supplies=2) == []


def test_violations_look_inside_subcircuits():
    deck = "t\nX1 a b OA\n.subckt OA p q\nG1 p 0 q 0 1\n.ends\n.end\n"
    assert asbuilt.violations(deck, allow_subckt=True) == ["G1: ideal controlled source not allowed"]
    assert asbuilt.violations(deck, asbuilt.BASE_ELEMENTS | {"g"}, allow_subckt=True) == []
    # Wrapping a design in a subcircuit hid it from snapping and merging.
    assert asbuilt.violations(deck, asbuilt.BASE_ELEMENTS | {"g"}) == [
        "OA: subcircuit definitions not allowed"]


def test_expression_values_are_violations():
    deck = "t\nVcc vcc 0 DC 12\nRc vcc c {8.2k}\nC1 c 0 1u\nR2 c 0 rval\n.end\n"
    assert asbuilt.violations(deck) == ["Rc: value '{8.2k}' must be a plain number",
                                        "R2: value 'rval' must be a plain number"]


def test_device_cards_are_replaced_by_the_real_part():
    models = bench2.load_devices()["models"]
    deck = ("t\nQ1 c b e QX\n.model QX NPN(BF=5000 VAF=1e6\n+ IS=1e-15)\n"
            "D1 a b DX\n.model DX D(IS=1e-20 N=0.5)\n.end\n")
    out, swapped = asbuilt.substitute_models(deck, models)
    assert swapped == ["DX -> 1N4001", "QX -> 2N2222"]
    assert "BF=5000" not in out and "+ IS=1e-15" not in out
    assert f".model QX NPN({models['npn']['params']})" in out.splitlines()


def test_grader_subckt_replaces_the_designs_copy_and_is_exempt_from_rules():
    subckts = bench2.load_devices()["subckts"]
    deck = ("t\nX1 0 m vcc vee out UA741\n.subckt UA741 a b c d e\nE1 e 0 a b 1e9\n.ends\n"
            "Vcc vcc 0 DC 15\nVee vee 0 DC -15\n.end\n")
    p = asbuilt.prepare(deck, specs.load("specs_lib/inv_active_lowpass.yaml"), PRECISION,
                        supplies=2, subckts=subckts)
    assert p["violations"] == []
    assert "E1 e 0 a b 1e9" not in p["deck"]
    assert "Egain gain 0 plus minus 200k" in p["deck"]
    assert p["deck"].rstrip().endswith(".end")


def test_probed_nodes_come_from_the_spec_vectors():
    assert asbuilt.probed_nodes(CE_SPEC) == {"0", "out"}


def test_every_suite_entry_resolves():
    suite = bench2.load_suite()
    assert len({e["id"] for e in suite}) == len(suite)
    for e in suite:
        assert bench2.task(e)
        assert bench2.spec_for(e).specs
        assert (bench2.PROJECT_DIR / e["reference"]).exists()
        tol = bench2.tol_for(e)
        assert set(tol) == {"r", "c", "l"}
        assert e.get("excluded") or e["profile"] in ("commodity", "precision")


def test_prompts_state_what_the_grader_enforces():
    e = bench2.entry("rlc_bandpass")
    rules, tol = bench2.rules_text(e), bench2.tolerance_text(e)
    assert "E96 resistors, E24 capacitors, E12 inductors" in rules
    assert "No ideal current sources" in rules
    assert "inductor +/-5%" in tol and "resistor +/-1%" in tol
    d = bench2.entry("voltage_divider")
    assert "DC supply +/-1%" in bench2.tolerance_text(d)


@needs_ngspice
def test_split_resistors_earn_nothing_and_ideal_sources_score_zero():
    e = bench2.entry("ce_bjt_amp")
    ref = (bench2.PROJECT_DIR / e["reference"]).read_text(encoding="utf-8")
    split = ref.replace("Rc vcc coll 9.1k", "RcA vcc c1 1.6k\nRcB c1 coll 7.5k")
    assert split != ref
    g = bench2.grade(e, split, n=20, dists=("gaussian",))
    assert g["merges"] == ["RcA+RcB in series -> 9100"]
    assert g["yield"]["gaussian"] == bench2.grade(e, ref, n=20, dists=("gaussian",))["yield"]["gaussian"]
    bad = bench2.grade(e, ref.replace("Vin in 0", "Ibias vcc coll 1u\nVin in 0"), n=20)
    assert bad["violations"] and bad["yield"]["gaussian"] == 0.0


@needs_ngspice
def test_a_fantasy_transistor_card_scores_the_same_as_the_real_part():
    e = bench2.entry("ce_bjt_amp")
    ref = (bench2.PROJECT_DIR / e["reference"]).read_text(encoding="utf-8")
    fantasy = ref.replace("BF=200 VAF=100", "BF=5000 VAF=1e6")
    assert fantasy != ref
    a = bench2.grade(e, ref, n=20, dists=("gaussian",))
    b = bench2.grade(e, fantasy, n=20, dists=("gaussian",))
    assert b["models"] == ["QN2222 -> 2N2222"]
    assert a["nominal"]["gain"] == pytest.approx(b["nominal"]["gain"])
    assert a["yield"] == b["yield"]


def test_excluded_circuits_say_why():
    for e in bench2.load_suite():
        if e.get("excluded"):
            assert e["why"].startswith("EXCLUDED")
