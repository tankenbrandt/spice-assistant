"""Reading LTspice's own .asy symbol library.

The test that matters most is `test_builtin_table_matches_ltspice`: it holds the
hand-written symbol table against the real library, symbol by symbol. That is
what turns "verified against a schematic I drew" into "verified against the
source of truth", and it keeps verifying itself on every run rather than
resting on a measurement made once.

Everything here skips cleanly on a machine with no LTspice, which is the same
machine the built-in table exists for.
"""
import pytest

import ascview
import symlib

MINIMAL = """Version 4
SymbolType CELL
LINE Normal 16 32 64 0
RECTANGLE Normal 0 0 32 64
CIRCLE Normal 0 0 32 32
WINDOW 0 56 32 Left 2
SYMATTR Value NPN
SYMATTR Prefix QN
PIN 64 0 NONE 0
PINATTR PinName C
PINATTR SpiceOrder 1
PIN 0 48 NONE 0
PINATTR PinName B
PINATTR SpiceOrder 2
"""


# ------------------------------------------------------------------- parsing

@pytest.fixture(scope="module")
def parsed():
    return symlib.parse(MINIMAL, "demo")


def test_pins_are_read(parsed):
    assert parsed.coords == [(64, 0), (0, 48)]
    assert parsed.pin_names == ["C", "B"]


def test_symattrs_are_read(parsed):
    assert parsed.attrs["Value"] == "NPN"
    assert parsed.prefix == "QN"


def test_drawing_primitives_become_ops(parsed):
    kinds = [op[0] for op in parsed.draw]
    assert "line" in kinds
    assert "poly" in kinds        # RECTANGLE
    assert "circle" in kinds


def test_window_lines_are_not_drawing_ops(parsed):
    """WINDOW positions an attribute label; it is not geometry."""
    assert all(op[0] != "window" for op in parsed.draw)


def test_spice_order_decides_pin_order():
    """SpiceOrder is the subcircuit's node order and may differ from the order
    the pins are declared in -- that is exactly why it exists."""
    text = """Version 4
PIN 0 0 NONE 0
PINATTR PinName OUT
PINATTR SpiceOrder 3
PIN 10 10 NONE 0
PINATTR PinName IN
PINATTR SpiceOrder 1
PIN 20 20 NONE 0
PINATTR PinName MID
PINATTR SpiceOrder 2
"""
    assert symlib.parse(text, "x").pin_names == ["IN", "MID", "OUT"]


def test_pins_without_spice_order_keep_declared_order():
    text = "Version 4\nPIN 0 0 NONE 0\nPINATTR PinName A\nPIN 9 9 NONE 0\nPINATTR PinName B\n"
    assert symlib.parse(text, "x").pin_names == ["A", "B"]


def test_malformed_lines_are_skipped():
    text = MINIMAL + "LINE Normal oops bad 1 2\nPIN not a number\n"
    sym = symlib.parse(text, "x")
    assert sym.coords == [(64, 0), (0, 48)]


def test_box_covers_pins_and_drawing(parsed):
    x0, y0, x1, y1 = parsed.box()
    for px, py in parsed.coords:
        assert x0 <= px <= x1 and y0 <= py <= y1


def test_empty_symbol_gets_a_fallback_box():
    assert symlib.parse("Version 4\n", "x").box() == (-32, 0, 32, 64)


# ------------------------------------------------------------------ locating

def test_no_library_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("LTSPICE_SYM_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr(symlib, "_CANDIDATES", [])
    assert symlib.find_library() is None
    assert symlib.available() is False


def test_env_var_overrides_autodetection(monkeypatch, tmp_path):
    (tmp_path / "res.asy").write_text(MINIMAL, encoding="utf-8")
    monkeypatch.setenv("LTSPICE_SYM_DIR", str(tmp_path))
    assert symlib.find_library() == tmp_path


def test_resolve_handles_nested_names(tmp_path):
    (tmp_path / "OpAmps").mkdir()
    (tmp_path / "OpAmps" / "OP07.asy").write_text(MINIMAL, encoding="utf-8")
    for spelled in ("OpAmps/OP07", "OpAmps\\OP07", "OpAmps\\\\OP07"):
        assert symlib.resolve(spelled, tmp_path) is not None, spelled


def test_resolve_is_case_insensitive(tmp_path):
    (tmp_path / "MyPart.asy").write_text(MINIMAL, encoding="utf-8")
    assert symlib.resolve("mypart", tmp_path) is not None


def test_missing_symbol_returns_none(tmp_path):
    assert symlib.resolve("nothing_here", tmp_path) is None
    assert symlib.lookup("nothing_here", tmp_path) is None


def test_utf16_symbol_files_are_read(tmp_path):
    """Some shipped .asy files are UTF-16; they must not come back as mojibake."""
    p = tmp_path / "wide.asy"
    p.write_bytes(MINIMAL.encode("utf-16"))
    assert symlib.parse(symlib._read_text(p), "wide").coords == [(64, 0), (0, 48)]


# ------------------------------------------------- against the real library

def test_library_has_symbols(symbol_library):
    assert any(symbol_library.rglob("*.asy"))


@pytest.mark.parametrize("name", sorted(ascview.SYMBOLS))
def test_builtin_table_matches_ltspice(symbol_library, name):
    """Every hand-written primitive, held against LTspice's own definition.

    This is the check that caught nmos/pmos carrying the BJT's offsets and
    `sw` being modelled with two pins when it has four.
    """
    asy = symlib.lookup(name, symbol_library)
    if asy is None:
        pytest.skip(f"{name} has no .asy in this library")
    assert list(ascview.SYMBOLS[name].pins) == asy.coords


def test_builtin_opamp_matches_op07(symbol_library):
    """Pin ORDER differs (ours is drawing order, the library's is SpiceOrder),
    so the geometry is compared as a set."""
    asy = symlib.lookup("OpAmps/OP07", symbol_library)
    assert sorted(ascview.OPAMP.pins) == sorted(asy.coords)


def test_every_builtin_is_marked_verified():
    """The table is now validated against LTspice itself, so nothing in it is
    an assumption any more."""
    unverified = [n for n, s in ascview.SYMBOLS.items() if not s.verified]
    assert unverified == [], unverified


def test_a_vendor_part_carries_spice_order(symbol_library):
    asy = symlib.lookup("OpAmps/LTC2053", symbol_library)
    assert asy is not None
    assert [p.order for p in asy.pins] == list(range(1, len(asy.pins) + 1))


# ------------------------------------------------------ ascview integration

def test_library_resolves_a_symbol_the_table_lacks(symbol_library):
    sym, kind = ascview.symbol_for(r"OpAmps\LTC2053")
    assert kind == "library"
    assert len(sym.pins) == 8


def test_builtins_still_win_over_the_library(symbol_library):
    """The hand-drawn primitives render better; the library fills gaps only."""
    _sym, kind = ascview.symbol_for("res")
    assert kind == "primitive"


def test_disabling_the_library_restores_the_unknown_path(no_symbol_library):
    _sym, kind = ascview.symbol_for(r"OpAmps\LTC2053")
    assert kind == "unknown"


def test_a_broken_asy_does_not_break_rendering(monkeypatch, tmp_path):
    """A render must survive a malformed or unreadable symbol file."""
    def boom(*_a, **_k):
        raise OSError("disk gone")
    monkeypatch.setattr(symlib, "lookup", boom)
    ascview._lib_cache.clear()
    sym, kind = ascview.symbol_for(r"Vendor\Whatever")
    assert kind == "unknown"
    assert sym.pins == []
    ascview._lib_cache.clear()


# ------------------------------------------------------ netlist integration

def test_spice_order_drives_the_subcircuit_call(project_dir, symbol_library):
    """No --pinorder needed when the library can be read: the node order comes
    from the symbol's own SpiceOrder."""
    import netlist
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    lines, warnings = netlist.emit(sch)
    assert warnings == []
    call = next(ln for ln in lines if ln.startswith("XU1"))
    # OP07 SpiceOrder: In+, In-, V+, V-, OUT
    assert call == "XU1 in N001 VCC VEE out OP07"


def test_explicit_pinorder_still_overrides_the_library(project_dir, symbol_library):
    import netlist
    sch = ascview.parse_asc(project_dir / "examples" / "active_lowpass.asc")
    order = netlist.parse_pinorder(["OP07=out,v-,v+,in-,in+"])
    lines, _w = netlist.emit(sch, pinorder=order)
    call = next(ln for ln in lines if ln.startswith("XU1"))
    assert call == "XU1 out VEE VCC N001 in OP07"


def test_cli_reports_the_library(capsys, symbol_library):
    assert symlib._cli([]) == 0
    assert "symbols available" in capsys.readouterr().out


def test_cli_prints_pins(capsys, symbol_library):
    assert symlib._cli(["npn"]) == 0
    out = capsys.readouterr().out
    assert "C" in out and "(   64,    0)" in out


def test_cli_reports_a_missing_symbol(capsys, symbol_library):
    assert symlib._cli(["definitely_not_a_symbol"]) == 1
