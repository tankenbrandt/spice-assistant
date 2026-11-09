"""ab_tolerance: prompts match the grader, the budget guard holds, stats are right.

No API calls: the budget guard is exercised with a stand-in call.
"""

import pytest

import ab_tolerance as ab
import main
import robustness


def test_arms_are_nested_and_a0_is_the_benchmark_prompt():
    p = ab.arm_prompts()
    base = next(c for c in ab.benchmark.CIRCUITS if c["id"] == "ce_bjt_amp")["description"]
    assert p["A0"] == base
    assert p["A1"].startswith(p["A0"]) and "Acceptance criteria" in p["A1"]
    assert p["A2"].startswith(p["A1"]) and "Monte Carlo" in p["A2"]
    assert "Monte Carlo" not in p["A1"] and "Acceptance" not in p["A0"]


def test_tolerance_brief_states_the_graders_own_numbers():
    t = ab.tolerance_brief()
    assert "500 Monte Carlo builds" in t
    assert f"resistor varies +/-{robustness.PASSIVE_TOL['r'] * 100:g}%" in t
    assert f"capacitor +/-{robustness.PASSIVE_TOL['c'] * 100:g}%" in t
    assert f"supply +/-{robustness.SUPPLY_TOL * 100:g}%" in t
    assert f"BF varies +/-{robustness.MODEL_TOL[('npn', 'bf')] * 100:g}%" in t
    lo, hi = robustness.TEMP_RANGE
    assert f"{lo:g} to {hi:g} C" in t
    assert "47.5-52.5 V/V" in t


def test_budget_refuses_a_call_that_could_cross_the_cap(monkeypatch):
    monkeypatch.setattr(main, "MODEL", "claude-opus-5-5")
    worst = ab.Budget.worst_call("claude-opus-5-5")
    calls = []
    guarded = ab.Budget(cap=worst * 1.5).guard(lambda: calls.append(1))
    guarded()                                  # spent stays 0: fits
    b = ab.Budget(cap=worst * 1.5, spent=worst)
    with pytest.raises(ab.BudgetExceeded):
        b.guard(lambda: calls.append(1))()
    assert calls == [1]


def test_budget_charges_tokens_at_the_calling_models_price(monkeypatch):
    monkeypatch.setattr(main, "MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(main, "USAGE", {"calls": 0, "input_tokens": 0, "output_tokens": 0})

    def fake_call():
        main.USAGE["input_tokens"] += 1_000_000
        main.USAGE["output_tokens"] += 100_000
    b = ab.Budget(cap=100)
    b.guard(fake_call)()
    pin, pout = main.prices("claude-sonnet-5-5")
    assert b.spent == pytest.approx(pin + 0.1 * pout)


def test_no_deck_scores_zero_everywhere():
    g = ab.grade(None)
    assert g["nominal_pass"] is False
    assert g["yield"] == {"gaussian": 0.0, "uniform": 0.0}


def test_resistor_count_ignores_comments_and_other_parts():
    deck = "title\n* R1 a b 1k\nR1 a b 1k\nRc vcc c 2k\nC1 a b 1u\nrload out 0 10k\n"
    assert ab.resistor_count(deck) == 3


def test_wilson_matches_the_textbook_value():
    lo, hi = ab.wilson(8, 10)
    assert lo == pytest.approx(0.490, abs=1e-3) and hi == pytest.approx(0.943, abs=1e-3)
    assert ab.wilson(0, 0) == (None, None)


def test_bootstrap_cis_are_deterministic_and_bracket_the_mean():
    xs = [0.2, 0.5, 0.9, 0.7, 0.4]
    assert ab.bootstrap_mean_ci(xs) == ab.bootstrap_mean_ci(xs)
    lo, hi = ab.bootstrap_mean_ci(xs)
    assert lo <= sum(xs) / len(xs) <= hi
    dlo, dhi = ab.bootstrap_diff_ci([0.9, 0.95, 0.92], [0.1, 0.2, 0.15])
    assert dlo > 0.6 and dhi < 0.9


def _trial(model, arm, y, nominal=True, resistors=5):
    return {"model": model, "arm": arm, "i": 0,
            "grade": {"nominal_pass": nominal, "resistors": resistors,
                      "yield": {"gaussian": y, "uniform": y}}}


def test_summary_pools_models_and_reports_contrasts_and_ceiling():
    trials = [_trial("claude-sonnet-5-5", "A0", 0.5), _trial("claude-opus-5-5", "A0", 0.7),
              _trial("claude-sonnet-5-5", "A2", 0.9, resistors=8),
              _trial("claude-opus-5-5", "A2", 0.0, nominal=False)]
    s = ab.summarize(trials, {"gaussian": 0.9, "uniform": 0.8})
    a0 = s["groups"]["pooled|gaussian|A0"]
    assert a0["n"] == 2 and a0["mean_yield"] == pytest.approx(0.6)
    assert a0["of_ceiling"] == pytest.approx(0.6 / 0.9)
    a2 = s["groups"]["pooled|gaussian|A2"]
    assert a2["nominal_pass"] == 1 and a2["split_resistors"] == 1
    assert s["contrasts"]["pooled|gaussian|A2-A0"]["diff"] == pytest.approx(-0.15)
    assert s["groups"]["claude-opus-5-5|gaussian|A0"]["mean_yield"] == pytest.approx(0.7)
    assert "A1" not in {k.split("|")[2] for k in s["groups"] if s["groups"][k]["n"]}


def test_ideal_current_sources_counts_only_i_elements():
    deck = "title\nI1 vcc c 1m\n* I2 a b 1m\nibias x 0 2m\nR1 a b 1k\n.include x\n"
    assert ab.ideal_current_sources(deck) == 2


def test_reel_draws_give_same_value_resistors_one_factor(monkeypatch):
    """A resistor split into equal pieces must vary like the one part it replaces."""
    deck = ("t\nVCC vcc 0 DC 12\nRA vcc m 1k\nRB m out 1k\nRC out 0 2k\n"
            "Q1 a b c QN\n.model QN NPN(BF=200)\n.end\n")
    seen = []
    monkeypatch.setattr(ab.robustness, "_evaluate",
                        lambda fn, nl, params, v: seen.append(v) or {"metrics": {}})
    ab.reel_monte_carlo(deck, n=20, workers=1)
    assert len(seen) == 20
    assert all(v["RA"] == v["RB"] for v in seen)            # one reel
    assert any(v["RC"] / 2 != v["RA"] for v in seen)        # another value, another reel
    assert len({v["RA"] for v in seen}) > 1                 # it does vary
