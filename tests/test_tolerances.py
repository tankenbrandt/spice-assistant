"""Tests for the sourced component-tolerance table.

Most of these guard the *data*, not the code: a tolerance table whose numbers
drift, lose their citations, or quietly acquire an invented figure is worse
than no table at all, because it looks authoritative.
"""
import pytest

import tolerances


def test_loads():
    t = tolerances.load()
    assert t["resistors"]
    assert t["capacitors"]


def test_load_is_cached_but_returns_equal_data():
    assert tolerances.load() == tolerances.load()


# ------------------------------------------------------------ the data itself

def test_every_component_entry_cites_a_confidence():
    """An unsourced number in a table like this is worse than no number:
    it reads as authoritative."""
    missing = []
    for family in ("resistors", "capacitors", "inductors"):
        for name, entry in tolerances.load()[family].items():
            if "confidence" not in entry:
                missing.append(f"{family}.{name}")
    assert not missing, f"no confidence level on: {missing}"


def test_confidence_levels_are_from_the_known_set():
    allowed = {"primary", "secondary", "pending"}
    for family in ("resistors", "capacitors", "inductors"):
        for name, entry in tolerances.load()[family].items():
            assert entry["confidence"] in allowed, f"{name}: {entry['confidence']}"


def test_primary_sourced_entries_carry_a_source():
    for family in ("resistors", "capacitors", "inductors"):
        for name, entry in tolerances.load()[family].items():
            if entry["confidence"] == "primary":
                assert entry.get("source"), f"{family}.{name} claims primary, cites nothing"


def test_typical_tolerance_is_one_of_the_offered_grades():
    for family in ("resistors", "capacitors", "inductors"):
        for name, entry in tolerances.load()[family].items():
            if "typical_pct" in entry and "tolerance_pct" in entry:
                assert entry["typical_pct"] in entry["tolerance_pct"], name


def test_known_gaps_are_recorded_rather_than_filled_in():
    """The research turned up things that could not be verified. They are
    meant to stay visible."""
    assert len(tolerances.load()["unverified"]) >= 4


def test_film_capacitor_has_no_invented_tempco():
    """Its tempco could not be verified from a primary source, so the entry
    must carry a caveat and no number."""
    film = tolerances.load()["capacitors"]["film"]
    assert "tempco_ppm_per_C" not in film
    assert film.get("caveat")


# ---------------------------------------------------------------- lookups

def test_tolerance_of_a_named_part():
    assert tolerances.tolerance("resistors", "thick_film_chip") == pytest.approx(0.05)


def test_tolerance_is_a_fraction_not_a_percent():
    """robustness.Param.tol is fractional; handing it 5.0 would perturb a
    resistor by 500%."""
    assert tolerances.tolerance("capacitors", "c0g_np0") < 1.0


def test_unknown_part_names_the_available_ones():
    with pytest.raises(KeyError, match="thick_film_chip"):
        tolerances.tolerance("resistors", "unobtainium")


def test_explicit_grade_overrides_the_typical():
    assert tolerances.tolerance("resistors", "thick_film_chip", pct=1.0) == pytest.approx(0.01)


def test_grade_must_actually_be_offered():
    """Asking for a 0.01% thick-film part should fail loudly rather than
    silently model a part nobody sells."""
    with pytest.raises(ValueError, match="0.01"):
        tolerances.tolerance("resistors", "thick_film_chip", pct=0.01)


# ---------------------------------------------------------------- profiles

def test_profiles_exist():
    assert {"commodity", "precision", "worst_case"} <= set(tolerances.profiles())


def test_profile_resolves_to_fractional_passive_tolerances():
    p = tolerances.profile("commodity")
    assert p["r"] == pytest.approx(0.05)
    assert p["c"] == pytest.approx(0.10)
    assert p["l"] == pytest.approx(0.20)


def test_precision_profile_is_tighter_than_commodity():
    tight, loose = tolerances.profile("precision"), tolerances.profile("commodity")
    assert tight["r"] < loose["r"]
    assert tight["c"] < loose["c"]


def test_legacy_profile_reproduces_the_layers_historical_default():
    """`legacy` must reproduce robustness.PASSIVE_TOL exactly, so the
    numbers already published in ROBUSTNESS.md stay reproducible."""
    import robustness
    assert tolerances.profile("legacy") == pytest.approx(robustness.PASSIVE_TOL)


def test_commodity_inductor_is_looser_than_the_old_guess():
    """The sourced value disagrees with the old default, and the sourced
    one wins: Coilcraft's power inductors are +-20%, not the +-10% this
    layer assumed. That is the whole point of the table."""
    import robustness
    assert tolerances.profile("commodity")["l"] > robustness.PASSIVE_TOL["l"]
    assert tolerances.load()["inductors"]["standard"]["typical_pct"] == 20.0


def test_commodity_matches_the_old_default_for_r_and_c():
    """Only the inductor moved; the resistor and capacitor guesses were
    already right."""
    import robustness
    c = tolerances.profile("commodity")
    assert c["r"] == pytest.approx(robustness.PASSIVE_TOL["r"])
    assert c["c"] == pytest.approx(robustness.PASSIVE_TOL["c"])


def test_unknown_profile_names_the_available_ones():
    with pytest.raises(KeyError, match="commodity"):
        tolerances.profile("nonesuch")


def test_profile_references_a_real_part_entry():
    t = tolerances.load()
    for name, prof in t["profiles"].items():
        assert prof["resistor"] in t["resistors"], name
        assert prof["capacitor"] in t["capacitors"], name


# ------------------------------------------------------- methodology guards

def test_the_table_says_not_to_snap_to_e_series():
    """The clearest finding of the research, and the easiest to get wrong
    later: E-series is a design-time constraint, not a variation model."""
    assert tolerances.load()["e_series"]["do_not_snap"]


def test_e192_pairs_with_half_a_percent_not_a_tenth():
    """A common simplification. E192 is the value grid used down to 0.1%,
    but its own native tolerance pairing is 0.5%."""
    assert tolerances.load()["e_series"]["pairing"]["E192"] == 0.5


def test_tolerance_letter_codes():
    letters = tolerances.load()["e_series"]["tolerance_letters"]
    assert letters["F"] == 1.0
    assert letters["J"] == 5.0
    assert letters["M"] == 20.0


def test_class_two_ceramics_are_flagged_as_deterministic_not_random():
    """The single most important modelling correction from the research."""
    x7r = tolerances.load()["capacitors"]["x7r"]
    assert x7r["dc_bias"] == "significant"
    assert "deterministic" in x7r["dc_bias_note"].lower()


def test_default_distribution_is_uniform():
    assert tolerances.load()["meta"]["default_distribution"] == "uniform"


def test_binning_question_is_presented_as_open():
    b = tolerances.load()["binning"]
    assert b["sources"]
    assert "uniform" in b["recommendation"].lower()


# ------------------------------------------------------------ semiconductors

def test_bjt_gain_spread_is_at_least_three_to_one():
    """Every graded part onsemi guarantees both ends of comes out at 3:1."""
    parts = tolerances.load()["semiconductors"]["bjt_hfe"]["parts"]
    for name in ("2N2222A", "2N3904", "2N3906"):
        p = parts[name]
        assert p["max"] / p["min"] == pytest.approx(3.0)


def test_ungraded_bc847_is_far_wider_than_a_graded_one():
    """BC847 A/B/C are the same die sorted into bins. The leftover ungraded
    part spans 7:1, which is why one flat spread cannot serve both."""
    parts = tolerances.load()["semiconductors"]["bjt_hfe"]["parts"]
    assert parts["BC847"]["ratio"] > 3 * parts["BC847C"]["ratio"]


def test_the_layers_existing_bf_spread_is_vindicated():
    """+-50% about 200 is exactly the 2N2222A's guaranteed 100..300, so the
    number already in robustness.py turns out to be well founded."""
    import robustness
    hfe = tolerances.load()["semiconductors"]["bjt_hfe"]
    assert hfe["fractional_tol"] == pytest.approx(robustness.MODEL_TOL[("npn", "bf")])
    nominal = 200
    assert nominal * (1 - hfe["fractional_tol"]) == pytest.approx(hfe["parts"]["2N2222A"]["min"])
    assert nominal * (1 + hfe["fractional_tol"]) == pytest.approx(hfe["parts"]["2N2222A"]["max"])


def test_early_voltage_is_recorded_as_unpublished():
    """No datasheet gives VAF, so the 25% the layer perturbs it by is not a
    sourced number. That has to stay visible rather than be quietly kept."""
    gaps = " ".join(tolerances.load()["unverified"]).lower()
    assert "early voltage" in gaps and "vaf" in gaps


def test_opamp_gbw_is_marked_do_not_model_randomly():
    """LM358, TL072 and LM741 publish a typical GBW and no limits at all.
    Inventing a sigma for it would be fabricating data."""
    gbw = tolerances.load()["semiconductors"]["opamp_gbw"]
    assert gbw["guaranteed"] is False
    assert gbw["do_not_model_randomly"] is True


def test_parameters_that_are_only_estimated_say_so():
    """The -2 mV/C diode tempco everyone quotes is read off a graph."""
    d = tolerances.load()["semiconductors"]["diode_vf"]
    assert d["tempco_confidence"] == "estimated"


def test_mosfet_vth_spread_is_wider_than_the_layer_assumes():
    """Real thresholds are looser than the 15% currently used for VTO."""
    import robustness
    vth = tolerances.load()["semiconductors"]["mosfet_vth"]
    assert vth["fractional_tol"] > robustness.MODEL_TOL[("nmos", "vto")]


def test_every_semiconductor_entry_states_its_confidence():
    for name, entry in tolerances.load()["semiconductors"].items():
        assert entry.get("confidence") in {"primary", "secondary"}, name


# ------------------------------------------------- wiring into robustness

DECK = """t
V1 in 0 DC 5
R1 in out 1k
C1 out 0 100n
L1 out 0 10u
.end
"""


def test_extract_params_defaults_to_the_historical_tolerances():
    """Calling it the old way must not change, or every published number
    silently moves."""
    import robustness
    params = {p.key: p.tol for p in robustness.extract_params(DECK)[1]
              if p.kind == "passive"}
    assert params["R1"] == pytest.approx(0.05)
    assert params["C1"] == pytest.approx(0.10)
    assert params["L1"] == pytest.approx(0.10)


def test_extract_params_accepts_a_profile():
    import robustness
    params = {p.key: p.tol for p in
              robustness.extract_params(DECK, passive_tol=tolerances.profile("precision"))[1]
              if p.kind == "passive"}
    assert params["R1"] == pytest.approx(0.01)
    assert params["C1"] == pytest.approx(0.05)


def test_a_profile_does_not_leak_between_calls():
    """The layer fans out over worker processes, so the tolerance in force
    has to travel with the call rather than sit in a module global."""
    import robustness
    robustness.extract_params(DECK, passive_tol=tolerances.profile("precision"))
    after = {p.key: p.tol for p in robustness.extract_params(DECK)[1]
             if p.kind == "passive"}
    assert after["R1"] == pytest.approx(0.05)


def test_precision_parts_give_a_narrower_spread_than_commodity():
    """The question the profiles exist to answer: what does buying better
    parts actually buy?"""
    import robustness
    def spread(profile):
        return [p.tol for p in
                robustness.extract_params(DECK, passive_tol=tolerances.profile(profile))[1]
                if p.kind == "passive"]
    assert sum(spread("precision")) < sum(spread("commodity"))


@pytest.mark.parametrize("fn", ["monte_carlo", "sensitivity", "worst_case", "pvt_corners"])
def test_every_analysis_accepts_a_parts_profile(fn):
    """--parts has to reach every analysis, not just the first. Each of
    these calls extract_params itself, so a profile threaded into only one
    of them would silently report commodity numbers under a precision
    heading."""
    import inspect

    import robustness
    assert "passive_tol" in inspect.signature(getattr(robustness, fn)).parameters


def test_monte_carlo_records_which_parts_it_used():
    """A yield figure is meaningless without the BOM it assumed."""
    import inspect

    import robustness
    src = inspect.getsource(robustness.monte_carlo)
    assert "passive_tol" in src


def test_a_missing_table_fails_with_a_clear_message():
    """The YAML is read from disk beside the module. If a packaging change
    ever leaves it behind, the error should say so rather than surface as a
    bare FileNotFoundError from inside a Monte Carlo run."""
    with pytest.raises(FileNotFoundError, match="tolerances.yaml"):
        tolerances.load("no/such/tolerances.yaml")
