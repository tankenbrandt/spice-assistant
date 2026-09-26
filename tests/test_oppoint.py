"""Whole-deck operating point: parsing ngspice's two report formats, and the
rule that keeps a capacitor's initial condition from being read as a current.

The parsing tests run anywhere -- they work on captured ngspice output. The
end-to-end class needs ngspice and skips without it.
"""
import pytest

import oppoint
from conftest import needs_ngspice

# Real `print all` output, banner and all, so the parser is tested against what
# ngspice actually emits rather than an idealised version of it.
PRINT_ALL = """
Note: No compatibility mode selected!

Circuit: * common-emitter bjt amplifier

Doing analysis at TEMP = 27.000000 and TNOM = 27.000000

No. of Data Rows : 1
base = 1.624301e+00
coll = 4.436952e+00
vcc = 1.200000e+01
vcc#branch = -1.16099e-03
vin#branch = 0.000000e+00
"""

# A BJT, a capacitor and a resistor in one `show`, which is where the trap is:
# the capacitor's `ic` is an initial condition and reads as a plausible 0.
SHOW = """
 BJT: Bipolar Junction Transistor
     device                    q1
      model                qn2222
          i             ?????????
         ic            0.00100841
         ib           4.92013e-06
         ie           -0.00101333
         id             ?????????

 Capacitor: Fixed capacitor
     device                  cout                    ce
      model                     C                     C
          i                     0                     0
         ic                     0                     0
         ib             ?????????             ?????????

 Resistor: Simple linear resistor
     device                    rc
      model                     R
          i            0.00100841
         ic             ?????????
"""


# ------------------------------------------------------------------- voltages

def test_print_all_splits_nodes_from_source_currents():
    nodes, sources = oppoint.parse_voltages(PRINT_ALL)
    assert nodes == {"base": pytest.approx(1.624301),
                     "coll": pytest.approx(4.436952),
                     "vcc": pytest.approx(12.0)}
    assert sources == {"vcc": pytest.approx(-1.16099e-3),
                       "vin": pytest.approx(0.0)}


def test_banner_lines_with_equals_are_not_mistaken_for_vectors():
    """'Doing analysis at TEMP = 27.0 and TNOM = 27.0' has an '=' and is not
    a node; a looser regex picks it up as one."""
    nodes, _ = oppoint.parse_voltages(PRINT_ALL)
    assert not any(k.lower().startswith(("doing", "temp", "circuit")) for k in nodes)


# ------------------------------------------------------------------- currents

def test_show_columns_map_to_the_right_devices():
    devs = {d.refdes: d for d in oppoint.parse_currents(SHOW)}
    assert set(devs) == {"q1", "cout", "ce", "rc"}
    assert devs["q1"].currents["ic"] == pytest.approx(0.00100841)
    assert devs["q1"].currents["ie"] == pytest.approx(-0.00101333)
    assert devs["rc"].currents["i"] == pytest.approx(0.00100841)
    assert devs["q1"].model == "qn2222"


def test_capacitor_ic_is_not_reported_as_a_current():
    """`ic` on a capacitor is its initial condition. ngspice answers with a
    real 0 rather than '?????????', so only the per-type list keeps it out."""
    devs = {d.refdes: d for d in oppoint.parse_currents(SHOW)}
    assert set(devs["cout"].currents) == {"i"}
    assert "ic" not in devs["ce"].currents


def test_missing_parameters_are_dropped():
    devs = {d.refdes: d for d in oppoint.parse_currents(SHOW)}
    assert "i" not in devs["q1"].currents      # came back as ?????????
    assert "ic" not in devs["rc"].currents


@pytest.mark.parametrize("kind,expected", [
    ("BJT", ("ic", "ib", "ie")),
    ("Diode", ("id",)),
    ("Mos1", ("id",)),
    ("Resistor", ("i",)),
    ("Capacitor", ("i",)),
    ("Inductor", ("i",)),
    ("Vsource", ("i",)),
    ("Something unheard of", ("i",)),
])
def test_currents_for_each_device_type(kind, expected):
    assert oppoint.currents_for(kind) == expected


def test_mosfet_bulk_leakage_is_not_labelled_a_base_current():
    """A Mos1's `ib` is bulk, not base. Reporting it as Ib would be wrong."""
    show = ("\n Mos1: Level 1 MOSfet model\n"
            "     device                    m1\n"
            "      model               nmosmod\n"
            "         ib           -5.9688e-12\n"
            "         id             0.0010128\n")
    m = oppoint.parse_currents(show)[0]
    assert set(m.currents) == {"id"}


# ------------------------------------------------------------------ rendering

def test_solver_zero_prints_as_zero():
    """-8.45e-29 A is the solver's zero; printed literally it reads as current."""
    assert oppoint._eng(-8.45e-29, "A") == "0A"
    assert oppoint._eng(1.01e-3, "A") == "1.01mA"


def test_source_is_not_listed_twice():
    """A voltage source appears in `show` and again as `<name>#branch`."""
    op = oppoint.OperatingPoint(
        nodes={"a": 1.0},
        sources={"vcc": -1e-3},
        devices=[oppoint.Device("vcc", "Vsource", currents={"i": -1e-3})])
    refs = [r[0] for r in oppoint._device_rows(op)]
    assert refs == ["vcc"]


def test_source_with_no_show_row_is_still_reported():
    op = oppoint.OperatingPoint(nodes={"a": 1.0}, sources={"v1": 2e-3}, devices=[])
    assert [r[0] for r in oppoint._device_rows(op)] == ["v1"]


def test_markdown_has_both_tables():
    op = oppoint.OperatingPoint(
        nodes={"out": 5.0}, sources={},
        devices=[oppoint.Device("r1", "Resistor", "R", {"i": 1e-3})])
    md = oppoint.markdown(op)
    assert "| Node | Voltage |" in md
    assert "| Device | Type | Current |" in md
    assert "`out`" in md and "5V" in md
    assert "I 1mA" in md


# ---------------------------------------------------------------- end-to-end

def _needs_show(op):
    """`show all : <params>` is the one piece here that varies by ngspice build.

    CI's Linux job installs whatever apt ships, which is older than the 46
    bundled for Windows. Node voltages come from `print all` and are safe; if a
    build reports no device currents, say so rather than failing as though the
    parser were wrong.
    """
    if not op.devices:
        pytest.skip("this ngspice build's `show` reported no device currents")


@needs_ngspice
class TestAgainstNgspice:
    def test_amplifier_bias_point(self, project_dir):
        netlist = (project_dir / "repaired" / "ce_bjt_amp.cir").read_text()
        op = oppoint.operating_point(netlist)
        # A working common-emitter stage sits with its collector between the
        # emitter and the rail; that is what "biased correctly" means.
        assert 0 < op.nodes["emit"] < op.nodes["coll"] < op.nodes["vcc"]

    def test_transistor_currents_obey_kirchhoff(self, project_dir):
        netlist = (project_dir / "repaired" / "ce_bjt_amp.cir").read_text()
        op = oppoint.operating_point(netlist)
        _needs_show(op)
        q1 = next(d for d in op.devices if d.refdes == "q1")
        assert q1.currents["ic"] == pytest.approx(1e-3, rel=0.2)
        # Kirchhoff: emitter current is the sum of the other two.
        assert q1.currents["ic"] + q1.currents["ib"] == pytest.approx(
            -q1.currents["ie"], rel=1e-6)

    def test_a_capacitor_carries_no_dc_current(self, project_dir):
        """The `ic`-is-an-initial-condition trap, against the real simulator."""
        netlist = (project_dir / "repaired" / "ce_bjt_amp.cir").read_text()
        op = oppoint.operating_point(netlist)
        _needs_show(op)
        cin = next(d for d in op.devices if d.refdes == "cin")
        assert set(cin.currents) == {"i"}
        assert cin.currents["i"] == pytest.approx(0.0, abs=1e-12)

    def test_every_node_appears_without_being_asked_for(self, project_dir):
        """The point of this layer: no spec named these nodes."""
        netlist = (project_dir / "repaired" / "ce_bjt_amp.cir").read_text()
        op = oppoint.operating_point(netlist)
        assert {"base", "coll", "emit", "vcc"} <= set(op.nodes)

    def test_a_deck_with_no_circuit_raises(self):
        """ngspice answers a deck with no nodes out of its constants plot
        rather than erroring, so staying quiet here would look like success."""
        with pytest.raises(RuntimeError, match="no operating point"):
            oppoint.operating_point("* nothing here\n.end\n")


def test_ngspice_constants_are_not_nodes():
    """With no nodes to print, `print all` falls back to ngspice's constants
    plot; unfiltered, a deck with no circuit reports eleven node voltages."""
    nodes, _ = oppoint.parse_voltages(
        "pi = 3.141593e+00\nboltz = 1.380649e-23\nechargeing = 5.000000e+00\n")
    assert nodes == {"echargeing": pytest.approx(5.0)}


def test_a_real_node_named_like_a_constant_survives():
    """`c` is a constant name and a plausible node name. Only the value tells
    them apart: no circuit node sits at 3e8 volts."""
    nodes, _ = oppoint.parse_voltages("c = 3.300000e+00\ne = 2.718282e+00\n")
    assert nodes == {"c": pytest.approx(3.3)}
