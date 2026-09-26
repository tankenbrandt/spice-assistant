"""Spec-in-the-loop design, with the model stubbed out.

The model's answers are real decks from the repo: `baseline/` holds the
first-shot designs that miss their spec, `repaired/` the ones that meet it. So
the loop, the spec measurement and the operating point all run for real in
ngspice, and no test makes an API call.
"""
from pathlib import Path

import pytest

import design
import main
import specs
from conftest import needs_ngspice

ROOT = Path(__file__).resolve().parent.parent
AMP_SPEC = specs.load(ROOT / "specs_lib" / "ce_bjt_amp.yaml")
BAD_AMP = (ROOT / "baseline" / "ce_bjt_amp.cir").read_text(encoding="utf-8")
GOOD_AMP = (ROOT / "repaired" / "ce_bjt_amp.cir").read_text(encoding="utf-8")


class FakeModel:
    """Stands in for main's API calls; records what each one was shown."""

    def __init__(self, monkeypatch, generated=BAD_AMP, repairs=(GOOD_AMP,)):
        self.prompts, self.repair_calls = [], []
        self._repairs = list(repairs)
        self._generated = generated
        monkeypatch.setattr(main, "generate_netlist", self.generate)
        monkeypatch.setattr(main, "fix_netlist_spec", self.fix_spec)
        monkeypatch.setattr(main, "fix_netlist", self.fix_sim)

    def generate(self, description):
        self.prompts.append(description)
        return self._generated

    def fix_spec(self, netlist, target, measured, operating_point=None,
                 requirements=None):
        self.repair_calls.append(dict(target=target, measured=measured,
                                      operating_point=operating_point,
                                      requirements=requirements))
        return self._repairs.pop(0) if self._repairs else netlist

    def fix_sim(self, netlist, output):     # pragma: no cover - should not happen
        raise AssertionError("these decks all simulate; no sim repair expected")


def _quiet(_msg):
    pass


# ----------------------------------------------------------------- the brief

def test_brief_states_every_limit_and_the_vectors_the_harness_reads():
    brief = design.spec_brief(AMP_SPEC)
    assert "|gain|: 50 V/V +/-5.0%" in brief
    assert "|phase|" in brief
    assert "vm(out)" in brief and "ac dec" in brief


def test_brief_asks_for_an_ac_source_only_when_there_is_an_ac_analysis():
    buck = specs.load(ROOT / "specs_lib" / "buck_converter.yaml")
    assert "AC magnitude" in design.spec_brief(AMP_SPEC)
    assert "AC magnitude" not in design.spec_brief(buck)


def test_informational_measurements_are_not_presented_as_limits():
    assert "mid-band" not in design.spec_brief(AMP_SPEC)


# ------------------------------------------------------------------ the loop

@needs_ngspice
def test_a_failing_first_design_is_repaired_to_pass(monkeypatch):
    FakeModel(monkeypatch)
    result = design.design("a CE amp, gain -50", AMP_SPEC, log=_quiet)
    assert result.passed
    assert result.repairs == 1
    assert not result.rounds[0].verdict["pass"]
    assert result.netlist == GOOD_AMP


@needs_ngspice
def test_the_spec_goes_to_the_model_before_it_designs(monkeypatch):
    model = FakeModel(monkeypatch)
    design.design("a CE amp, gain -50", AMP_SPEC, log=_quiet)
    assert model.prompts[0].startswith("a CE amp, gain -50")
    assert "|gain|: 50 V/V" in model.prompts[0]


@needs_ngspice
def test_a_repair_is_shown_the_measurement_and_the_bias_point(monkeypatch):
    model = FakeModel(monkeypatch)
    design.design("a CE amp", AMP_SPEC, log=_quiet)
    (call,) = model.repair_calls
    assert "|gain|: 107" in call["measured"]
    # The baseline deck's bias point is what explains its gain.
    assert "node voltages" in call["operating_point"]
    assert "q1" in call["operating_point"].lower()


@needs_ngspice
def test_a_deck_that_already_passes_costs_no_repair(monkeypatch):
    model = FakeModel(monkeypatch)
    result = design.repair_to_spec(GOOD_AMP, AMP_SPEC, log=_quiet)
    assert result.passed and result.repairs == 0
    assert model.repair_calls == []


@needs_ngspice
def test_repairs_stop_at_the_round_limit(monkeypatch):
    model = FakeModel(monkeypatch, repairs=())    # the model never fixes it
    result = design.repair_to_spec(BAD_AMP, AMP_SPEC, rounds=2, log=_quiet)
    assert not result.passed
    assert len(model.repair_calls) == 2
    assert len(result.rounds) == 3                # the original + two repairs


@needs_ngspice
def test_an_unrepairable_simulation_is_reported_not_looped(monkeypatch):
    FakeModel(monkeypatch)
    monkeypatch.setattr(main, "fix_netlist", lambda nl, out: nl)
    broken = "broken\nR1 a 0\n.end\n"
    with pytest.raises(RuntimeError, match="still failing to simulate"):
        design.repair_to_spec(broken, AMP_SPEC, log=_quiet)


# ---------------------------------------------------------- the benchmark path

def test_benchmark_repair_prompt_is_unchanged_without_an_operating_point(monkeypatch):
    """The published repair numbers were made without a bias point; keep it so."""
    seen = {}
    monkeypatch.setattr(main, "_message_text",
                        lambda msgs, **_: seen.setdefault("p", msgs[0]["content"]))
    main.fix_netlist_spec("deck", "target", "measured")
    assert "OPERATING POINT" not in seen["p"]
    seen.clear()
    main.fix_netlist_spec("deck", "target", "measured", operating_point="V(e) 2 mV")
    assert "=== DC OPERATING POINT" in seen["p"] and "V(e) 2 mV" in seen["p"]


def test_the_bias_point_is_withheld_where_it_would_mislead():
    """A buck converter's DC point is its switch open and 0 V out."""
    buck = specs.load(ROOT / "specs_lib" / "buck_converter.yaml")
    assert design.bias_matters(AMP_SPEC)
    assert not design.bias_matters(buck)


@needs_ngspice
def test_a_repair_is_reminded_what_the_assignment_fixed(monkeypatch):
    """Without it, lab-round repairs changed the handout's own source and
    battery values to make the number fit."""
    model = FakeModel(monkeypatch)
    design.design("a CE amp from a 12 V supply", AMP_SPEC, log=_quiet)
    assert model.repair_calls[0]["requirements"] == "a CE amp from a 12 V supply"


def test_requirements_reach_the_repair_prompt(monkeypatch):
    seen = {}
    monkeypatch.setattr(main, "_message_text",
                        lambda msgs, **_: seen.setdefault("p", msgs[0]["content"]))
    main.fix_netlist_spec("deck", "target", "measured", requirements="V1 = 1.5 V")
    assert "ORIGINAL REQUIREMENTS" in seen["p"] and "V1 = 1.5 V" in seen["p"]
    seen.clear()
    main.fix_netlist_spec("deck", "target", "measured")
    assert "ORIGINAL REQUIREMENTS" not in seen["p"]
