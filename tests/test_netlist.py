"""Schematic -> netlist extraction: connectivity, device emission, directives.

The headline is at the bottom: the deck extracted from `examples/active_lowpass.asc`
measures identically to the hand-written deck for the same circuit. That is the
loop closing -- draw it, simulate it, verify it, without LTspice.
"""
import pytest

import ascview
import netlist
from conftest import needs_ngspice


# ------------------------------------------------------------------ union-find

def test_union_merges_transitively():
    u = netlist.Union()
    u.union("a", "b")
    u.union("b", "c")
    assert u.find("a") == u.find("c")


def test_union_keeps_unrelated_apart():
    u = netlist.Union()
    u.union("a", "b")
    assert u.find("a") != u.find("z")


# ------------------------------------------------------------------ geometry

def _wire(x1, y1, x2, y2):
    return ascview.Wire(x1, y1, x2, y2)


@pytest.mark.parametrize("point,expected", [
    ((10, 50), True),      # inside a vertical run
    ((10, 0), False),      # its endpoint, not its middle
    ((10, 100), False),    # the other endpoint
    ((10, 150), False),    # past the end
    ((20, 50), False),     # off the line
])
def test_on_segment_vertical(point, expected):
    assert netlist._on_segment(point, _wire(10, 0, 10, 100)) is expected


def test_on_segment_horizontal():
    w = _wire(0, 40, 100, 40)
    assert netlist._on_segment((50, 40), w) is True
    assert netlist._on_segment((50, 41), w) is False


# ---------------------------------------------------------------------- nets

def test_flag_names_its_net(fixtures):
    sch = ascview.parse_asc(fixtures / "primitives.asc")
    nets = netlist.build_nets(sch)
    assert nets[(16, 16)] == "a"
    assert nets[(16, 96)] == "b"


def test_ground_flags_become_node_zero(fixtures):
    sch = ascview.parse_asc(fixtures / "primitives.asc")
    nets = netlist.build_nets(sch)
    assert nets[(-100, 96)] == "0"


@pytest.mark.parametrize("label", ["0", "GND", "gnd"])
def test_ground_spellings(tmp_path, label):
    p = tmp_path / "g.asc"
    p.write_text(f"Version 4\nSHEET 1 880 680\n"
                 f"WIRE 0 0 0 64\nFLAG 0 64 {label}\n", encoding="utf-8")
    nets = netlist.build_nets(ascview.parse_asc(p))
    assert nets[(0, 64)] == "0"


def test_wires_merge_into_one_net(tmp_path):
    p = tmp_path / "w.asc"
    p.write_text("Version 4\nSHEET 1 880 680\n"
                 "WIRE 0 0 100 0\nWIRE 100 0 100 100\nFLAG 0 0 sig\n",
                 encoding="utf-8")
    nets = netlist.build_nets(ascview.parse_asc(p))
    assert nets[(0, 0)] == nets[(100, 100)] == "sig"


def test_unnamed_nets_get_sequential_numbers(tmp_path):
    p = tmp_path / "u.asc"
    p.write_text("Version 4\nSHEET 1 880 680\n"
                 "WIRE 0 0 0 64\nWIRE 200 0 200 64\n", encoding="utf-8")
    names = set(netlist.build_nets(ascview.parse_asc(p)).values())
    assert names == {"N001", "N002"}


def test_a_point_on_a_wires_span_joins_that_net(tmp_path):
    """A T-junction. This is also how a schematic can short two pins without
    looking like it does: a wire routed past a pin connects to it."""
    p = tmp_path / "t.asc"
    p.write_text("Version 4\nSHEET 1 880 680\n"
                 "WIRE 0 0 0 100\n"           # vertical run
                 "WIRE 0 50 80 50\n"          # lands part-way along it
                 "FLAG 0 0 rail\n", encoding="utf-8")
    nets = netlist.build_nets(ascview.parse_asc(p))
    assert nets[(80, 50)] == "rail"


def test_a_crossing_without_an_endpoint_is_not_a_connection(tmp_path):
    """Wires that merely cross are not connected -- that is what makes a
    schematic readable, and SPICE agrees."""
    p = tmp_path / "x.asc"
    p.write_text("Version 4\nSHEET 1 880 680\n"
                 "WIRE 0 50 100 50\nWIRE 50 0 50 20\n"
                 "FLAG 0 50 h\nFLAG 50 0 v\n", encoding="utf-8")
    nets = netlist.build_nets(ascview.parse_asc(p))
    assert nets[(0, 50)] == "h"
    assert nets[(50, 0)] == "v"


def test_ground_wins_when_a_net_carries_two_labels(tmp_path):
    p = tmp_path / "d.asc"
    p.write_text("Version 4\nSHEET 1 880 680\n"
                 "WIRE 0 0 100 0\nFLAG 0 0 vout\nFLAG 100 0 0\n", encoding="utf-8")
    nets = netlist.build_nets(ascview.parse_asc(p))
    assert nets[(0, 0)] == "0"


# ------------------------------------------------------------- device lines

@pytest.fixture(scope="module")
def primitives(fixtures):
    sch = ascview.parse_asc(fixtures / "primitives.asc")
    lines, warnings = netlist.emit(sch)
    return lines, warnings


def test_every_primitive_emits_cleanly(primitives):
    lines, warnings = primitives
    assert warnings == []
    assert len(lines) == 9


@pytest.mark.parametrize("expected", [
    "V1 vp 0 5",                      # + terminal first
    "R1 a b 1k",
    "C1 b c 100n",
    "L1 c d 10m",
    "D1 d e 1N4148",                  # anode then cathode
    "Q1 f e 0 2N2222",                # collector, base, emitter
    "Q2 g f 0 2N3906",                # same order for the PNP
    "M1 h g 0 0 NMOS",                # drain, gate, source, bulk
    "S1 i 0 ctlp ctln MYSW",          # n+, n-, control+, control-
])
def test_pin_order_per_device(primitives, expected):
    assert expected in primitives[0]


def test_switch_is_four_terminal(primitives):
    """LTspice's `sw` is the voltage-controlled switch: S n+ n- nc+ nc-."""
    line = next(ln for ln in primitives[0] if ln.startswith("S1"))
    assert len(line.split()) == 6


def test_mosfet_bulk_is_tied_to_source(primitives):
    line = next(ln for ln in primitives[0] if ln.startswith("M1"))
    nodes = line.split()[1:5]
    assert nodes[2] == nodes[3]


def test_refdes_keeps_a_conforming_instname(tmp_path):
    sym = ascview.Symbol(name="res", x=0, y=0, rot="R0", attrs={"InstName": "Rload"})
    assert netlist._refdes("R", sym, 1) == "Rload"


def test_refdes_prefixes_a_nonconforming_one(tmp_path):
    sym = ascview.Symbol(name="res", x=0, y=0, rot="R0", attrs={"InstName": "load"})
    assert netlist._refdes("R", sym, 1) == "Rload"


def test_refdes_falls_back_to_an_index():
    sym = ascview.Symbol(name="res", x=0, y=0, rot="R0")
    assert netlist._refdes("R", sym, 7) == "R7"


def test_a_missing_value_on_a_passive_is_an_error():
    sym = ascview.Symbol(name="res", x=0, y=0, rot="R0", attrs={"InstName": "R1"})
    with pytest.raises(netlist.NetlistError, match="no Value"):
        netlist._value(sym, "res")


def test_a_missing_model_name_falls_back_to_a_default():
    sym = ascview.Symbol(name="diode", x=0, y=0, rot="R0", attrs={"InstName": "D1"})
    assert netlist._value(sym, "diode") == "D"


# ------------------------------------------------------------- subckt pins

def test_unknown_symbol_becomes_a_todo_not_a_guess(project_dir, no_symbol_library):
    """The no-LTspice path: pin order is reported, never invented."""
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    lines, warnings = netlist.emit(sch)
    assert any("no known SPICE pin order" in w for w in warnings)
    assert any(ln.startswith("* TODO") for ln in lines)


def test_strict_mode_refuses_to_emit_a_todo(project_dir, no_symbol_library):
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    with pytest.raises(netlist.NetlistError, match="no known SPICE pin order"):
        netlist.emit(sch, strict=True)


def test_the_warning_tells_you_the_flag_to_use(project_dir, no_symbol_library):
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    _lines, warnings = netlist.emit(sch)
    assert "--pinorder op07=in-,in+,out,v+,v-" in warnings[0]


def test_declared_pin_order_emits_a_subcircuit_call(project_dir):
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    order = netlist.parse_pinorder(["OP07=in+,in-,v+,v-,out"])
    lines, warnings = netlist.emit(sch, pinorder=order)
    assert warnings == []
    call = next(ln for ln in lines if ln.startswith("XU1"))
    assert call == "XU1 in N001 VCC VEE out OP07"


def test_pinorder_rejects_an_unknown_role(project_dir):
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    order = netlist.parse_pinorder(["OP07=in+,in-,v+,v-,nonsense"])
    with pytest.raises(netlist.NetlistError, match="nonsense"):
        netlist.emit(sch, pinorder=order)


def test_pinorder_rejects_the_wrong_pin_count(project_dir):
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    order = netlist.parse_pinorder(["OP07=in+,in-"])
    with pytest.raises(netlist.NetlistError, match="lists 2 pins"):
        netlist.emit(sch, pinorder=order)


@pytest.mark.parametrize("bad", ["OP07", "OP07="])
def test_malformed_pinorder_is_rejected(bad):
    with pytest.raises(netlist.NetlistError):
        netlist.parse_pinorder([bad])


# -------------------------------------------------------------- directives

def test_analysis_lines_go_to_the_control_block(tmp_path):
    p = tmp_path / "a.asc"
    p.write_text("Version 4\nSHEET 1 880 680\n"
                 "TEXT 0 0 Left 2 !.ac dec 10 1 1k\n", encoding="utf-8")
    cards, analyses = netlist.directives(ascview.parse_asc(p))
    assert analyses == [".ac dec 10 1 1k"]
    assert cards == []


def test_model_cards_stay_in_the_deck(tmp_path):
    p = tmp_path / "m.asc"
    p.write_text("Version 4\nSHEET 1 880 680\n"
                 "TEXT 0 0 Left 2 !.model DX D(IS=1e-14)\n", encoding="utf-8")
    cards, analyses = netlist.directives(ascview.parse_asc(p))
    assert cards == [".model DX D(IS=1e-14)"]
    assert analyses == []


def test_subckt_bodies_are_not_mistaken_for_analyses(project_dir):
    """A device line inside a .subckt starts with R or E, not a dot, so an
    'anything not a recognised card is an analysis' rule swept it into the
    control block and broke the deck."""
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    cards, analyses = netlist.directives(sch)
    assert "Rin plus minus 20meg" in cards
    assert all("Rin" not in a for a in analyses)
    assert analyses == [".ac dec 200 10 1meg"]


def test_comments_are_not_directives(project_dir):
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    cards, analyses = netlist.directives(sch)
    assert all("Active low-pass" not in ln for ln in cards + analyses)


# ------------------------------------------------------------------ round-trip

@pytest.fixture(scope="module")
def extracted(project_dir):
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    order = netlist.parse_pinorder(["OP07=in+,in-,v+,v-,out"])
    text, warnings = netlist.to_netlist(sch, pinorder=order)
    assert warnings == []
    return text


def test_extracted_deck_has_the_expected_shape(extracted):
    assert extracted.rstrip().endswith(".end")
    assert ".control" in extracted and ".endc" in extracted
    assert ".subckt OP07" in extracted
    for refdes in ("XU1", "V1", "V2", "V3", "Rf", "Rg", "C1"):
        assert any(ln.startswith(refdes) for ln in extracted.splitlines()), refdes


def test_analysis_is_not_left_as_a_dot_line(extracted):
    """This project runs analyses from a .control block; a stray .ac dot-line
    alongside one is the invocation error the system prompt exists to avoid."""
    body = extracted.split(".control")[1]
    assert "ac dec 200 10 1meg" in body
    assert "\n.ac " not in extracted


@needs_ngspice
def test_extracted_deck_simulates(extracted):
    import main
    ok, out = main.run_ngspice(extracted)
    assert ok is True, out


@needs_ngspice
def test_extracted_deck_meets_the_spec(project_dir, extracted):
    import specs
    spec = specs.load(project_dir / "examples" / "active_lowpass.yaml")
    result = specs.verdict(spec, extracted)
    assert result["error"] is None, result["error"]
    assert result["pass"] is True, result["measured"]


@needs_ngspice
def test_extracted_deck_measures_the_same_as_the_handwritten_one(project_dir, extracted):
    """The loop closing: the schematic and the deck are the same circuit, and
    the toolchain proves it by measuring both."""
    import specs
    spec = specs.load(project_dir / "examples" / "active_lowpass.yaml")
    handwritten = (project_dir / "examples" / "active_lowpass.cir").read_text(encoding="utf-8")

    a = specs.measure(spec, extracted)
    b = specs.measure(spec, handwritten)
    assert set(a) == set(b)
    for name in a:
        assert a[name] == pytest.approx(b[name], rel=1e-6), name


# ------------------------------------------------------------------------ CLI

def test_cli_prints_a_deck(project_dir, capsys):
    rc = netlist._cli([str(project_dir / "tests/fixtures/primitives.asc")])
    assert rc == 0
    assert "R1 a b 1k" in capsys.readouterr().out


def test_cli_writes_a_file(project_dir, tmp_path):
    out = tmp_path / "deck.cir"
    rc = netlist._cli([str(project_dir / "tests/fixtures/primitives.asc"),
                       "-o", str(out)])
    assert rc == 0
    assert "R1 a b 1k" in out.read_text(encoding="utf-8")


def test_cli_reports_a_missing_file(tmp_path):
    assert netlist._cli([str(tmp_path / "nope.asc")]) == 1


def test_cli_rejects_a_malformed_pinorder(project_dir):
    assert netlist._cli([str(project_dir / "tests/fixtures/primitives.asc"),
                         "--pinorder", "garbage"]) == 2
