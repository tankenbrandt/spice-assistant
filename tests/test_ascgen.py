"""Tests for the deck -> LTspice `.asc` writer.

The contract is connectivity, not draughtsmanship: whatever the layout looks
like, extracting the generated schematic must give back the circuit we started
from. Most of these tests are that round trip, stated on progressively nastier
decks.
"""
import pytest

import ascgen

RC = """RC low-pass
V1 in 0 DC 0 AC 1
R1 in out 1k
C1 out 0 159.15n
.end
"""


# ------------------------------------------------------------------- parsing

def test_parses_each_device():
    deck = ascgen.parse_deck(RC)
    assert [d.refdes for d in deck.devices] == ["V1", "R1", "C1"]


def test_parses_nodes_in_spice_order():
    deck = ascgen.parse_deck(RC)
    r1 = next(d for d in deck.devices if d.refdes == "R1")
    assert r1.nodes == ["in", "out"]


def test_keeps_the_value():
    deck = ascgen.parse_deck(RC)
    assert next(d for d in deck.devices if d.refdes == "R1").value == "1k"


def test_first_line_is_a_title_not_a_component():
    """A SPICE deck's first line is its title. Read as a part it would
    invent a device and shift every node."""
    deck = ascgen.parse_deck(RC)
    assert deck.title == "RC low-pass"
    assert len(deck.devices) == 3


def test_control_block_is_not_read_as_devices():
    deck = ascgen.parse_deck(
        "t\nR1 a 0 1k\n.control\nac dec 20 1 1meg\nprint v(a)\n.endc\n.end\n")
    assert [d.refdes for d in deck.devices] == ["R1"]


def test_directives_are_kept():
    deck = ascgen.parse_deck("t\nR1 a 0 1k\n.model QN NPN(BF=100)\n.end\n")
    assert any("QN" in d for d in deck.directives)


def test_end_is_not_kept_as_a_directive():
    """`.end` is emitted by the writer itself; carrying it through would put
    a stray directive on the sheet."""
    deck = ascgen.parse_deck(RC)
    assert not any(d.lower().startswith(".end") for d in deck.directives)


# ------------------------------------------------- symbol choice from models

def test_npn_symbol_comes_from_the_model_card():
    deck = ascgen.parse_deck(
        "t\n.model QN NPN(BF=100)\nQ1 c b e QN\n.end\n")
    assert deck.devices[0].symbol == "npn"


def test_pnp_symbol_comes_from_the_model_card():
    deck = ascgen.parse_deck(
        "t\n.model QP PNP(BF=100)\nQ1 c b e QP\n.end\n")
    assert deck.devices[0].symbol == "pnp"


def test_bjt_substrate_node_is_dropped():
    """`Q1 c b e 0 QN` carries a substrate node between the emitter and the
    model. Kept, it would be drawn as a fourth terminal the symbol has not
    got."""
    deck = ascgen.parse_deck("t\n.model QN NPN(BF=100)\nQ1 c b e 0 QN\n.end\n")
    assert deck.devices[0].nodes == ["c", "b", "e"]


def test_bjt_without_a_model_card_raises():
    """NPN and PNP have different pin geometry, so guessing would produce a
    schematic that netlists back to a different circuit."""
    with pytest.raises(ascgen.UnsupportedDeck, match="NPN"):
        ascgen.parse_deck("t\nQ1 c b e QMYSTERY\n.end\n")


def test_mosfet_bulk_node_is_dropped():
    """The three-terminal symbol ties bulk to source."""
    deck = ascgen.parse_deck("t\n.model MN NMOS(VTO=1)\nM1 d g s s MN\n.end\n")
    assert deck.devices[0].nodes == ["d", "g", "s"]
    assert deck.devices[0].symbol == "nmos"


# -------------------------------------------------------------- emitted file

def test_output_starts_with_the_asc_header():
    asc = ascgen.generate(RC)
    assert asc.splitlines()[0] == "Version 4"
    assert asc.splitlines()[1].startswith("SHEET ")


def test_every_device_becomes_a_symbol_with_its_refdes():
    asc = ascgen.generate(RC)
    for ref in ("V1", "R1", "C1"):
        assert f"SYMATTR InstName {ref}" in asc


def test_ground_becomes_a_flag_not_a_net_name():
    """LTspice spells node 0 as a ground flag."""
    asc = ascgen.generate(RC)
    assert "FLAG" in asc
    assert any(ln.endswith(" 0") for ln in asc.splitlines() if ln.startswith("FLAG"))


def test_named_nets_are_labelled():
    asc = ascgen.generate(RC)
    flags = [ln for ln in asc.splitlines() if ln.startswith("FLAG")]
    assert any(ln.endswith(" in") for ln in flags)
    assert any(ln.endswith(" out") for ln in flags)


def test_directives_are_written_as_bang_text():
    asc = ascgen.generate("t\n.model QN NPN(BF=100)\nQ1 c b e QN\n.end\n")
    assert any(ln.startswith("TEXT ") and "!.model" in ln
               for ln in asc.splitlines())


# --------------------------------------------------------- the real contract

def _roundtrip_ok(deck_text):
    original, extracted = ascgen.roundtrip(deck_text)
    problems = ascgen.compare(original, extracted)
    assert not problems, "connectivity changed:\n  " + "\n  ".join(problems)
    assert original, "no devices compared"
    return original


def test_roundtrip_rc_lowpass():
    _roundtrip_ok(RC)


def test_roundtrip_series_chain():
    """Three resistors in series: every internal node is shared by exactly
    two parts, which is where an off-by-one in the layout would show."""
    _roundtrip_ok("chain\nV1 a 0 DC 1\nR1 a b 1k\nR2 b c 2k\nR3 c 0 3k\n.end\n")


def test_roundtrip_parallel_on_one_node():
    """Four parts meeting at one node -- the T-junction case."""
    _roundtrip_ok("star\nV1 n 0 DC 1\nR1 n 0 1k\nR2 n 0 2k\nC1 n 0 1u\n.end\n")


def test_roundtrip_bjt_amplifier():
    _roundtrip_ok("""CE amp
.model QN NPN(BF=200 IS=1e-14)
Vcc vcc 0 DC 12
Vin in 0 DC 0 AC 1
Cin in base 10u
R1 vcc base 68k
R2 base 0 11k
Rc vcc coll 7.5k
Re emit 0 1k
Q1 coll base emit QN
.end
""")


def test_roundtrip_preserves_transistor_pin_order():
    """A BJT's three pins are not interchangeable: swapping collector and
    emitter still netlists, and is a different circuit."""
    original, extracted = ascgen.roundtrip(
        "t\n.model QN NPN(BF=100)\nQ1 mycoll mybase myemit QN\n.end\n")
    assert extracted["Q1"] == ["mycoll", "mybase", "myemit"]


def test_roundtrip_mosfet():
    _roundtrip_ok("t\n.model MN NMOS(VTO=2)\nV1 d 0 DC 5\nM1 d g s s MN\n"
                  "R1 g 0 1k\nR2 s 0 100\n.end\n")


def test_roundtrip_diode():
    _roundtrip_ok("t\n.model DX D(IS=1e-14)\nV1 a 0 DC 5\nD1 a b DX\n"
                  "R1 b 0 1k\n.end\n")


def test_roundtrip_ignores_a_control_block():
    _roundtrip_ok("t\nV1 a 0 DC 1\nR1 a 0 1k\n"
                  ".control\nop\nprint v(a)\n.endc\n.end\n")


def test_generated_file_reparses_as_a_schematic():
    """ascview must be able to read what ascgen writes -- they are the two
    halves of the same format."""
    import tempfile
    from pathlib import Path

    import ascview
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "g.asc"
        p.write_text(ascgen.generate(RC), encoding="utf-8")
        sch = ascview.parse_asc(p)
    assert len(sch.symbols) == 3
    assert sch.wires


def test_empty_deck_raises_rather_than_writing_a_blank_sheet():
    with pytest.raises(ascgen.UnsupportedDeck):
        ascgen.generate("just a title\n.end\n")


# ------------------------------------------- LTspice itself (skippable)

@pytest.fixture(scope="module")
def have_ltspice():
    import ltspice
    if not ltspice.available():
        pytest.skip("LTspice not installed")


def test_ltspice_netlists_the_generated_schematic_the_same_way(have_ltspice):
    """The strongest form of the claim: LTspice's own netlister, run on the
    file we wrote, recovers the circuit we started from."""
    import re
    import tempfile
    from pathlib import Path

    import symlib

    deck = """CE amp
.model QN NPN(BF=200 IS=1e-14)
Vcc vcc 0 DC 12
Vin in 0 DC 0 AC 1
Cin in base 10u
R1 vcc base 68k
R2 base 0 11k
Rc vcc coll 7.5k
Re emit 0 1k
Q1 coll base emit QN
.end
"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "generated.asc"
        p.write_text(ascgen.generate(deck), encoding="utf-8")
        theirs_text = symlib.netlist_with_ltspice(p)

    theirs = ascgen._devices_of(re.sub(r"[§¶]", "", theirs_text))
    ours = ascgen._devices_of(deck)
    assert ascgen.compare(ours, theirs) == []


# ------------------------------------------------ regressions found by --check

def test_switch_control_pin_does_not_short_to_ground():
    """The voltage-controlled switch has two pins 48 units apart on the same
    x. A 48-unit ground lead from the lower one ended exactly on the other,
    and LTspice joins a wire that terminates on a pin -- so the gate node
    came back as ground and the converter had no drive.
    """
    original, extracted = ascgen.roundtrip(
        "buck\n.model SWMOD SW(VT=0.5)\nV1 in 0 DC 12\n"
        "Vg gate 0 PULSE(0 5 0 1n 1n 4u 10u)\n"
        "S1 in sw gate 0 SWMOD\nR1 sw 0 10\n.end\n")
    assert extracted["S1"] == ["in", "sw", "gate", "0"], ascgen.compare(original, extracted)


def test_title_line_is_never_a_component_even_if_it_looks_like_one():
    """`Simple RC Circuit Test` begins with S and has four tokens. SPICE does
    not guess: line one is the title, always."""
    deck = ascgen.parse_deck("Simple RC Circuit Test\nR1 a 0 1k\n.end\n")
    assert deck.title == "Simple RC Circuit Test"
    assert [d.refdes for d in deck.devices] == ["R1"]


def test_devices_of_also_skips_the_title():
    """The comparison helper must read a deck the same way the writer does,
    or the round trip compares against a phantom device."""
    assert list(ascgen._devices_of("Simple RC Circuit Test\nR1 a 0 1k\n.end\n")) == ["R1"]


def test_current_controlled_source_is_refused_by_name():
    """F/H sense a named source's current; saying so beats 'unknown prefix'."""
    with pytest.raises(ascgen.UnsupportedDeck, match="current-controlled"):
        ascgen.parse_deck("t\nV1 a 0 1\nFamp out 0 V1 10\n.end\n")


# ------------------------------------------------------- op-amps, E and G

OPAMP_SUB = """.subckt OP07 plus minus vcc vee out
Rin plus minus 20meg
Egain gain 0 plus minus 1meg
Rout gain out 60
.ends OP07
"""

NONINV = """non-inverting, gain 11
Vin in 0 AC 1
Vcc vcc 0 15
Vee vee 0 -15
Rf minus out 100k
Rg minus 0 10k
XU1 in minus vcc vee out OP07
""" + OPAMP_SUB + ".end\n"


def test_vcvs_op_amp_round_trips():
    """The benchmark's op-amp decks model the amplifier as a bare VCVS."""
    original, extracted = ascgen.roundtrip(
        "t\nVin in 0 AC 1\nR1 fb 0 1k\nRf out fb 10k\nEopamp out 0 in fb 1e6\n.end\n")
    assert extracted["EOPAMP"] == ["out", "0", "in", "fb"]
    assert ascgen.compare(original, extracted) == []


def test_vccs_round_trips():
    """G has + at the bottom of its symbol, the opposite of E."""
    original, extracted = ascgen.roundtrip(
        "t\nVin in 0 1\nGm 0 out in 0 1m\nRl out 0 1k\n.end\n")
    assert ascgen.compare(original, extracted) == []


def test_poly_source_is_refused():
    with pytest.raises(ascgen.UnsupportedDeck, match="POLY"):
        ascgen.parse_deck("t\nE1 out 0 POLY(2) a 0 b 0 0 1 1\n.end\n")


def test_subckt_body_is_one_directive_not_top_level_devices():
    """Egain inside the macromodel must not be drawn on the sheet -- it
    would be wired to top-level nets that happen to share its node names."""
    deck = ascgen.parse_deck(NONINV)
    assert [d.refdes for d in deck.devices] == ["Vin", "Vcc", "Vee", "Rf", "Rg", "XU1"]
    (sub,) = [d for d in deck.directives if d.lower().startswith(".subckt")]
    assert "Egain" in sub and sub.endswith(".ends OP07")


def test_opamp_subcircuit_is_drawn_on_the_generic_opamp():
    deck = ascgen.parse_deck(NONINV)
    xu1 = deck.devices[-1]
    assert xu1.symbol == ascgen.OPAMP_SYMBOL and xu1.value == "OP07"


def test_opamp_subcircuit_round_trips_with_inputs_the_right_way_round():
    """The built-in geometry lists In- first; SpiceOrder lists In+ first.
    Zipping them positionally put the signal on the inverting input."""
    original, extracted = ascgen.roundtrip(NONINV)
    assert extracted["XU1"] == ["in", "minus", "vcc", "vee", "out", "op07"]
    assert ascgen.compare(original, extracted) == []


def test_opamp_round_trips_without_the_ltspice_library(monkeypatch):
    """What CI sees: no .asy to read SpiceOrder from, so the built-in order
    for opamp2 has to carry it."""
    import ascview
    monkeypatch.setattr(ascview, "USE_SYMBOL_LIBRARY", False)
    ascview._lib_cache.clear()
    assert ascgen.compare(*ascgen.roundtrip(NONINV)) == []


def test_instname_leaves_the_x_to_ltspice():
    """LTspice's netlister prefixes X itself; XU1 would come back XXU1."""
    assert "SYMATTR InstName U1\n" in ascgen.generate(NONINV)


def test_subcircuit_that_is_not_an_opamp_is_refused():
    deck = NONINV.replace("plus minus vcc vee out", "a b c d e")
    with pytest.raises(ascgen.UnsupportedDeck, match="do not read as an op-amp"):
        ascgen.parse_deck(deck)


def test_opamp_pins_in_another_order_are_refused():
    """Five op-amp pins in a different order would draw a supply as an input."""
    deck = NONINV.replace("plus minus vcc vee out", "plus minus out vcc vee")
    with pytest.raises(ascgen.UnsupportedDeck, match="opamp2 order"):
        ascgen.parse_deck(deck)


def test_call_to_an_undefined_subcircuit_is_refused():
    with pytest.raises(ascgen.UnsupportedDeck, match="not defined in the deck"):
        ascgen.parse_deck("t\nXU1 in m vcc vee out OP07\n.end\n")


def test_only_one_analysis_stays_live():
    """LTspice refuses a sheet with two analyses; ngspice decks often have
    op + ac + tran. The first non-.op one stays, the rest become comments."""
    deck = ascgen.parse_deck(
        "t\nR1 a 0 1k\n.control\nop\nac dec 10 1 1k\ntran 1u 1m\n.endc\n.end\n")
    live = [d for d in deck.directives if d.startswith((".op", ".ac", ".tran"))]
    assert live == [".ac dec 10 1 1k"]
    assert deck.notes == [".op", ".tran 1u 1m"]
    asc = ascgen.generate(
        "t\nR1 a 0 1k\n.control\nop\nac dec 10 1 1k\n.endc\n.end\n")
    assert "!.ac dec 10 1 1k" in asc and ";.op" in asc


def test_ltspice_netlists_the_generated_opamp_the_same_way(have_ltspice):
    import re
    import tempfile
    from pathlib import Path

    import symlib

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "generated.asc"
        p.write_text(ascgen.generate(NONINV), encoding="utf-8")
        theirs = symlib.netlist_with_ltspice(p)
    theirs = ascgen._devices_of(re.sub(r"[§¶]", "", theirs))
    assert ascgen.compare(ascgen._devices_of(NONINV), theirs) == []


def test_analysis_from_a_control_block_lands_on_the_sheet():
    """A deck keeps its analysis inside `.control`, which LTspice cannot
    read. Stripping the block and carrying nothing across produces a
    schematic that opens fine and has nothing to run."""
    asc = ascgen.generate(
        "t\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 1n\n"
        ".control\nac dec 20 1 1meg\nprint v(out)\n.endc\n.end\n")
    directives = [ln for ln in asc.splitlines() if ln.startswith("TEXT")]
    assert any("!.ac dec 20 1 1meg" in ln for ln in directives), directives


def test_print_and_plot_commands_are_not_carried_over():
    """`print` is an ngspice control-block command, not a directive; on a
    schematic it would be an error, not an analysis."""
    asc = ascgen.generate(
        "t\nV1 in 0 AC 1\nR1 in 0 1k\n.control\nop\nprint v(in)\n.endc\n.end\n")
    assert "print" not in asc


def test_an_existing_dot_directive_analysis_is_kept():
    asc = ascgen.generate("t\nV1 in 0 AC 1\nR1 in 0 1k\n.ac dec 10 1 1k\n.end\n")
    assert any("!.ac dec 10 1 1k" in ln for ln in asc.splitlines())
