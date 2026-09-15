"""Parsing, path handling and rendering for the .asc viewer."""
from pathlib import Path

import pytest

import ascview as av

BACKSLASH = chr(92)


@pytest.fixture(scope="module")
def sch(fixtures):
    return av.parse_asc(fixtures / "rc_lowpass.asc")


# ------------------------------------------------------------------ parsing

def test_parses_counts(sch):
    assert len(sch.symbols) == 3
    assert len(sch.wires) == 4
    assert len(sch.flags) == 2
    assert len(sch.texts) == 2


def test_symbol_attrs(sch):
    r1 = next(s for s in sch.symbols if s.attrs.get("InstName") == "R1")
    assert r1.name == "res"
    assert r1.attrs["Value"] == "1.6k"


def test_flags_become_nets(sch):
    assert {f.name for f in sch.flags} == {"0", "out"}


def test_spice_directive_is_distinguished_from_a_comment(sch):
    directives = [t for t in sch.texts if t.directive]
    comments = [t for t in sch.texts if not t.directive]
    assert len(directives) == 1 and len(comments) == 1
    assert directives[0].text == ".tran 5m"


def test_multiline_comment_is_split_into_lines(sch):
    """LTspice stores a multi-line comment on a single .asc line with the
    breaks escaped as backslash-n; they must become real newlines or the
    lines render stacked on top of each other."""
    comment = next(t for t in sch.texts if not t.directive)
    assert BACKSLASH + "n" not in comment.text   # the escape, not a newline
    assert comment.text.count(chr(10)) == 2
    assert comment.text.splitlines() == [
        "First-order low pass", "fc = 1 kHz", "- test fixture"]


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    p = tmp_path / "broken.asc"
    p.write_text("Version 4\nSHEET 1 880 680\n"
                 "WIRE not a number here\n"
                 "WIRE 0 0 0 64\n"
                 "SYMBOL res oops 0 R0\n", encoding="utf-8")
    parsed = av.parse_asc(p)
    assert len(parsed.wires) == 1
    assert parsed.symbols == []


# ------------------------------------------------------------ pin validation

def test_check_reports_all_pins_connected(sch):
    msgs = av.check(sch)
    assert len(msgs) == 3
    assert all(m.strip().startswith("OK") for m in msgs), msgs


def test_check_flags_a_disconnected_pin(fixtures, tmp_path):
    """A symbol nudged off the grid must be reported, not silently drawn."""
    text = (fixtures / "rc_lowpass.asc").read_text(encoding="utf-8")
    moved = text.replace("SYMBOL res 0 0 R0", "SYMBOL res 8 0 R0")
    p = tmp_path / "moved.asc"
    p.write_text(moved, encoding="utf-8")
    msgs = av.check(av.parse_asc(p))
    assert any("!!" in m or "~" in m for m in msgs), msgs


# --------------------------------------------------------- path expansion

def test_expand_plain_relative_name():
    assert av.expand("sch.asc") == [Path("sch.asc")]


def test_expand_absolute_path_does_not_raise(fixtures):
    """Regression: Path().glob() rejects absolute patterns outright, so an
    absolute filename used to crash the CLI before it opened anything."""
    target = (fixtures / "rc_lowpass.asc").resolve()
    assert av.expand(str(target)) == [target]


def test_expand_absolute_glob(fixtures):
    hits = av.expand(str(fixtures.resolve() / "*.asc"))
    assert (fixtures / "rc_lowpass.asc").resolve() in [h.resolve() for h in hits]


def test_expand_relative_glob(project_dir, monkeypatch):
    monkeypatch.chdir(project_dir / "tests" / "fixtures")
    assert Path("rc_lowpass.asc") in av.expand("*.asc")


def test_expand_unmatched_glob_returns_the_pattern():
    """So the caller can print a 'not found' message instead of silently
    doing nothing."""
    assert av.expand("no_such_dir/*.asc") == [Path("no_such_dir/*.asc")]


# ------------------------------------------------------------------ render

@pytest.mark.parametrize("theme", ["light", "dark"])
def test_svg_renders_and_is_well_formed(sch, theme):
    import xml.etree.ElementTree as ET
    svg = av.render_svg(sch, theme)
    ET.fromstring(svg)                      # raises if malformed
    assert svg.lstrip().startswith("<svg")


def test_svg_contains_every_wire(sch):
    svg = av.render_svg(sch)
    for w in sch.wires:
        assert f'x1="{w.x1}" y1="{w.y1}"' in svg or f'x1="{w.x2}" y1="{w.y2}"' in svg


def test_bounds_cover_all_geometry(sch):
    x0, y0, x1, y1 = av.bounds(sch)
    for w in sch.wires:
        assert x0 <= w.x1 <= x1 and y0 <= w.y1 <= y1
        assert x0 <= w.x2 <= x1 and y0 <= w.y2 <= y1


def test_bounds_measure_widest_line_not_whole_blob(sch):
    """A 3-line comment must not be measured as one very long string, or the
    canvas ends up padded with dead space."""
    widest = max(len(ln) for t in sch.texts for ln in t.text.splitlines())
    total = max(len(t.text) for t in sch.texts)
    assert widest < total
    x0, x1 = av.bounds(sch)[0], av.bounds(sch)[2]
    assert (x1 - x0) < 8 * total + 400


def test_html_page_embeds_the_svg(sch):
    page = av.render_html(sch)
    assert page.lstrip().startswith("<!doctype html>")
    assert "<svg" in page and "</svg>" in page
