"""Symbol pin geometry, checked against coordinates measured from real
LTspice schematics.

The numbers in GROUND_TRUTH are not computed from ascview's symbol table --
they are wire endpoints transcribed out of actual .asc files that LTspice
itself wrote. A pin offset that drifts stops matching them, which is the whole
point: this is the test that would have caught the MOSFET offsets being a
copy of the BJT ones.
"""
import pytest

import ascview as av


# (symbol leaf name, origin x, origin y, orientation, expected absolute pins)
#
# MOSFETs: measured from a CMOS NOR SR latch, 8 FETs covering all four
# orientations that appear in real schematics.
MOSFET_GROUND_TRUTH = [
    ("nmos", 144, 288, "R0",   [(192, 288), (144, 368), (192, 384)]),
    ("nmos", 656, 288, "R0",   [(704, 288), (656, 368), (704, 384)]),
    ("nmos", 400, 288, "M0",   [(352, 288), (400, 368), (352, 384)]),
    ("nmos", 912, 288, "M0",   [(864, 288), (912, 368), (864, 384)]),
    ("pmos", 320, 224, "R180", [(272, 224), (320, 144), (272, 128)]),
    ("pmos", 832,  96, "R180", [(784,  96), (832,  16), (784,   0)]),
    ("pmos", 224,  96, "M180", [(272,  96), (224,  16), (272,   0)]),
    ("pmos", 736, 224, "M180", [(784, 224), (736, 144), (784, 128)]),
]

# Passives and op-amp: measured from a Sallen-Key active filter.
PASSIVE_GROUND_TRUTH = [
    ("op07",     256,  192, "M180", [(224, 144), (224, 112), (288, 128),
                                     (256, 160), (256, 96)]),
    ("cap",       64,  144, "R0",   [(80, 144), (80, 208)]),
    ("cap",      288,  -64, "R90",  [(288, -48), (224, -48)]),
    ("res",       48,   96, "R90",  [(32, 112), (-48, 112)]),
    ("res",      -80,   96, "R90",  [(-96, 112), (-176, 112)]),
    ("voltage", -208,  384, "R0",   [(-208, 400), (-208, 480)]),
]

GROUND_TRUTH = MOSFET_GROUND_TRUTH + PASSIVE_GROUND_TRUTH


def _placed(name, x, y, rot):
    return av.Symbol(name=name, x=x, y=y, rot=rot)


@pytest.mark.parametrize("name,x,y,rot,expected", GROUND_TRUTH,
                         ids=[f"{g[0]}-{g[3]}-{g[1]},{g[2]}" for g in GROUND_TRUTH])
def test_pins_match_real_schematic(name, x, y, rot, expected):
    """Every pin lands exactly where LTspice put the wire that connects it."""
    assert av.sym_pins(_placed(name, x, y, rot)) == expected


def test_mosfet_is_not_the_bjt_symbol():
    """Regression: nmos/pmos used to share the BJT offsets, so no FET pin in a
    real schematic touched a wire."""
    fet = av.SYMBOLS["nmos"].pins
    bjt = av.SYMBOLS["npn"].pins
    assert fet != bjt
    assert fet == [(48, 0), (0, 80), (48, 96)]


@pytest.mark.parametrize("name", ["res", "cap", "voltage", "nmos", "pmos"])
def test_verified_symbols_are_flagged_verified(name):
    """--check advertises which geometry is measured vs. assumed; keep the flag
    honest for the ones covered by GROUND_TRUTH."""
    assert av.SYMBOLS[name].verified is True


def test_orientation_transforms_are_involutions():
    """Mirrors and 180 rotation applied twice return the original point."""
    for rot in ("R180", "M0", "M180"):
        for pt in [(48, 0), (0, 80), (17, -33)]:
            once = av.xf(rot, *pt)
            assert av.xf(rot, *once) == pytest.approx(pt)


def test_r90_r270_are_inverse():
    for pt in [(48, 0), (0, 80), (17, -33)]:
        assert av.xf("R270", *av.xf("R90", *pt)) == pytest.approx(pt)


def test_unknown_symbol_reports_no_builtin_geometry():
    sym, kind = av.symbol_for(r"OpAmps\LTC2053")
    assert kind == "unknown"
    assert sym.pins == []


def test_generic_opamp_names_resolve_to_opamp_geometry():
    for n in ("opamp2", "OP07", r"OpAmps\OP07", "UniversalOpAmp2"):
        _, kind = av.symbol_for(n)
        assert kind == "opamp", n
