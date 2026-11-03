"""layout.py: package pin assignment (ten-step method, step 4).

The pinouts are checked against the course's Appendix A and the 2N2222
datasheets; the assignments against the CD4007's shared-pin rules; and the
end-to-end check simulates each EE 347 circuit rebuilt from its pin table.
"""
import pytest

import layout
import specs
from conftest import PROJECT_DIR, needs_ngspice

EE347 = PROJECT_DIR / "labs" / "ee347_exp4"
EXP8 = PROJECT_DIR / "labs" / "ee346_exp8" / "ce_amp.cir"
FIGS = ["fig4_1", "fig4_2", "fig4_3", "fig4_4", "fig4_5"]


def _deck(name):
    return (EE347 / f"{name}.cir").read_text(encoding="utf-8")


def test_cd4007_pinout_matches_appendix_a():
    pkg = layout.PACKAGES["CD4007"]
    by = {s.unit: s for s in pkg.slots}
    assert [s.kind for s in pkg.slots].count("nmos") == 3
    assert [s.kind for s in pkg.slots].count("pmos") == 3
    # Shared gates: Q1/Q2 on 3, Q3/Q6 on 6, Q4/Q5 on 10.
    assert by["Q1"].pins[0] == by["Q2"].pins[0] == 3
    assert by["Q3"].pins[0] == by["Q6"].pins[0] == 6
    assert by["Q4"].pins[0] == by["Q5"].pins[0] == 10
    # Q4/Q5 share pin 12; Q3 has a leg on VSS (7), Q6 on VDD (14).
    assert 12 in by["Q4"].pins[1:] and 12 in by["Q5"].pins[1:]
    assert 7 in by["Q3"].pins[1:] and 14 in by["Q6"].pins[1:]
    assert pkg.rails == {14: "vdd", 7: "vss"}
    assert set(pkg.labels) == set(range(1, 15))


def test_4001b_and_2n2222_pinouts():
    nor = layout.PACKAGES["4001B"]
    assert [s.pins for s in nor.slots] == [(1, 2, 3), (5, 6, 4), (8, 9, 10), (12, 13, 11)]
    q = layout.PACKAGES["2N2222"]
    assert q.labels == {1: "emitter", 2: "base", 3: "collector"}
    assert "TO-92" in q.note and "TO-18" in q.note


def test_the_title_line_is_not_a_part():
    refs = {p.refdes for p in layout.parts(_deck("fig4_2"))}
    assert refs >= {"Mnb", "Mpb", "Mnc", "Mpc", "XU1", "XU2"}
    assert not any(r.lower().startswith(("cmos", "2-1", "mux")) for r in refs)


def test_four_transistor_mux_fits_one_cd4007():
    lay = layout.place(_deck("fig4_2"))
    names = [pl.package.name for pl in lay.packages]
    assert names == ["CD4007", "4001B"]
    assert not lay.unplaced
    # Every pin holds one net: the shared gate/drain pins agree.
    for ref, (pl, _slot, want) in lay.where.items():
        for pin, net in want.items():
            assert pl.nets[pin] == net, (ref, pin)


def test_five_nmos_need_two_packages():
    lay = layout.place(_deck("fig4_5"))
    assert [pl.package.name for pl in lay.packages] == ["CD4007", "CD4007", "4001B"]


def test_q3_and_q6_only_take_a_transistor_with_a_leg_on_the_rail():
    lay = layout.place(_deck("fig4_4"))
    for _ref, (_pl, slot, want) in lay.where.items():
        if slot.unit == "Q3":
            assert want[7] == "0"
        if slot.unit == "Q6":
            assert want[14] == lay.vdd


def test_a_substrate_off_the_rail_is_refused():
    deck = ("t\nVDD vdd 0 DC 5\nM1 out in 0 nb NM W=30u L=5u\n"
            ".model NM NMOS (LEVEL=1 VTO=1)\n.end\n")
    lay = layout.place(deck)
    assert lay.unplaced and lay.unplaced[0][0] == "M1"
    assert "substrate" in lay.unplaced[0][1]


def test_unused_transistor_on_a_live_shared_pin_is_shorted():
    lay = layout.place(_deck("fig4_4"))
    pl = lay.packages[0]
    ties = layout.unused_ties(pl, lay.vss, (lay.vdd, lay.vss))
    assert ties[11][0] == pl.nets[12]          # unused Q5's free leg tied to pin 12's node
    rows = {r[0]: r[2] for r in layout.pin_rows(pl, lay.vss, (lay.vdd, lay.vss))}
    assert rows["1.11"].startswith("unused Q5: tie to 1.12")
    assert rows["1.1"] == "no connection"      # unused Q1 hangs on nothing live
    assert any(t.startswith("1.11") for t in lay.wiring[pl.nets[12]])


def test_unused_gates_go_to_vss():
    lay = layout.place(_deck("fig4_1"))
    cd, nor = lay.packages
    rows = {r[0]: r[2] for r in layout.pin_rows(cd, lay.vss)}
    assert "tie to VSS" in rows["1.6"]
    nrows = {r[0]: r[2] for r in layout.pin_rows(nor, lay.vss)}
    assert all("tie to VSS" in nrows[f"2.{p}"] for p in (8, 9, 12, 13))
    assert nrows["2.10"] == "no connection"    # an unused output stays open


def test_2n2222_and_electrolytic_polarity():
    deck = EXP8.read_text(encoding="utf-8")
    lay = layout.place(deck, {"b": 2.49, "e": 1.84, "out": 11.4, "vcc": 20.0, "in": 0.0})
    (pl,) = lay.packages
    assert pl.refdes == "Q1" and pl.nets == {1: "e", 2: "b", 3: "out"}
    assert "Cb (+ lead)" in lay.wiring["b"]
    assert "Cb (− lead)" in lay.wiring["in"]
    # Without an operating point there is no claim about polarity.
    assert "Cb" in layout.place(deck).wiring["b"]


@needs_ngspice
@pytest.mark.parametrize("fig", FIGS)
def test_the_circuit_rebuilt_from_its_pin_table_still_meets_spec(fig):
    deck = _deck(fig)
    lay = layout.place(deck)
    packaged = layout.packaged_deck(deck, lay)
    assert "XPKG1" in packaged
    msg = layout.verify(specs.load(EE347 / f"{fig}.yaml"), deck, lay)
    assert msg.startswith("Checked"), msg


@needs_ngspice
def test_the_check_catches_a_floating_unused_transistor():
    """Leave unused Q5 hanging on Fig. 4.4's precharged node: it fails."""
    deck = _deck("fig4_4")
    lay = layout.place(deck)
    orig = layout.unused_ties
    try:
        layout.unused_ties = lambda pl, vss, rails=(): {
            p: v for p, v in orig(pl, vss, rails).items() if "VSS" in v[1]}
        msg = layout.verify(specs.load(EE347 / "fig4_4.yaml"), deck, lay)
    finally:
        layout.unused_ties = orig
    assert msg.startswith("FAILED"), msg
