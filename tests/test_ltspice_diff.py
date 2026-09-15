"""Differential test: netlist.py against LTspice's own netlister.

LTspice will netlist a schematic headlessly (`LTspice.exe -netlist file.asc`),
which makes the tool this project imitates available as an oracle. For every
shipped `.asc`, both netlisters run on the same file and the connectivity is
compared device by device: same refdes, same nets, same node order.

That is a stronger claim than "my extractor is self-consistent". It says the
node ordering, the T-junction rule, the ground handling and the SpiceOrder
lookup all agree with LTspice on real files.

Skips cleanly with no LTspice installed, which is also the CI path.
"""
import re

import pytest

import ascview
import netlist
import symlib

# LTspice marks instance names it generated with a section sign, and appends a
# pin-order comment to subcircuit calls.
_MARK = re.compile(r"[§¶]")


@pytest.fixture(scope="module")
def ltspice():
    exe = symlib.find_executable()
    if exe is None:
        pytest.skip("LTspice not installed")
    return exe


def _devices(text: str) -> dict:
    """Parse a netlist into {refdes: [nodes]}, ignoring the trailing model
    or value token, comments, and directives."""
    out = {}
    in_control = False
    for raw in text.splitlines():
        line = _MARK.sub("", raw).split(";")[0].strip()
        low = line.lower()
        if low.startswith(".control"):
            in_control = True
            continue
        if low.startswith(".endc"):
            in_control = False
            continue
        # A control block holds analysis commands, not devices: `ac dec 10 1 1k`
        # would otherwise parse as a device called "ac".
        if in_control or not line or line.startswith(("*", ".", "+")):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        refdes = parts[0]
        letter = refdes[0].upper()
        counts = {"R": 2, "C": 2, "L": 2, "V": 2, "I": 2, "D": 2,
                  "Q": 3, "M": 4, "J": 3, "S": 4, "E": 4, "G": 4}
        n = counts.get(letter)
        nodes = parts[1:1 + n] if n else parts[1:-1]
        out[refdes] = nodes
    return out


def _both(asc_path, exe, tmp_path):
    """Netlist one schematic with LTspice and with netlist.py."""
    import shutil
    local = tmp_path / asc_path.name
    shutil.copy(asc_path, local)
    theirs = _devices(symlib.netlist_with_ltspice(local, exe))

    sch = ascview.parse_asc(asc_path)
    text, _warnings = netlist.to_netlist(sch)
    return _devices(text), theirs


ASC_FILES = ["examples/active_lowpass.asc", "tests/fixtures/primitives.asc",
             "tests/fixtures/rc_lowpass.asc"]


@pytest.mark.parametrize("rel", ASC_FILES)
def test_same_devices(project_dir, ltspice, tmp_path, rel):
    mine, theirs = _both(project_dir / rel, ltspice, tmp_path)
    assert set(mine) == set(theirs)


@pytest.mark.parametrize("rel", ASC_FILES)
def test_same_nets_in_the_same_order(project_dir, ltspice, tmp_path, rel):
    """Node ORDER is the whole game -- `Q1 c b e` and `Q1 e b c` are different
    circuits, and only the order LTspice emits is right."""
    mine, theirs = _both(project_dir / rel, ltspice, tmp_path)
    for refdes in sorted(set(mine) & set(theirs)):
        assert mine[refdes] == theirs[refdes], refdes


def test_subcircuit_pin_order_agrees(project_dir, ltspice, tmp_path):
    """The SpiceOrder lookup, checked against LTspice on a real op-amp."""
    mine, theirs = _both(project_dir / "examples/active_lowpass.asc",
                         ltspice, tmp_path)
    assert mine["XU1"] == theirs["XU1"] == ["in", "N001", "VCC", "VEE", "out"]


def test_transistor_and_switch_ordering_agrees(project_dir, ltspice, tmp_path):
    """The devices whose geometry was wrong until the .asy files settled it."""
    mine, theirs = _both(project_dir / "tests/fixtures/primitives.asc",
                         ltspice, tmp_path)
    for refdes in ("Q1", "Q2", "M1", "S1"):
        assert mine[refdes] == theirs[refdes], refdes


def test_ltspice_netlist_helper_reports_a_missing_executable(tmp_path):
    with pytest.raises(FileNotFoundError, match="LTSPICE_EXE"):
        symlib.netlist_with_ltspice(tmp_path / "x.asc", exe=None,
                                    timeout=1) if symlib.find_executable() is None \
            else pytest.skip("LTspice is installed here")


def test_device_parser_ignores_comments_and_directives():
    text = ("* a comment\n.ac dec 10 1 1k\nR1 a b 1k\n"
            "X§U1 p m out MODEL ;§pnba In+)In-)OUT\n.end\n")
    got = _devices(text)
    assert got == {"R1": ["a", "b"], "XU1": ["p", "m", "out"]}
