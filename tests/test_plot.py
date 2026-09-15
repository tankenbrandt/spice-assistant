"""Waveform plotting: scales, ticks, panel splitting, labels and rendering."""
import xml.etree.ElementTree as ET

import pytest

import plot
from conftest import needs_ngspice


# ------------------------------------------------------------------- units

@pytest.mark.parametrize("vector,family,unit", [
    ("v(out)", "volts", "V"),
    ("V(OUT)", "volts", "V"),
    ("vm(out)", "volts", "V"),
    ("vdb(out)", "dB", "dB"),
    ("vp(out)", "phase", "deg"),
    ("i(vin)", "current", "A"),
    ("vin#branch", "current", "A"),
])
def test_unit_family(vector, family, unit):
    assert plot.unit_family(vector) == (family, unit)


@pytest.mark.parametrize("value,expected", [
    (0, "0"), (1, "1"), (1000, "1k"), (1500, "1.5k"), (1e6, "1M"),
    (4.7e-6, "4.7u"), (100e-9, "100n"), (0.05, "50m"), (-2200, "-2.2k"),
])
def test_engineering_notation(value, expected):
    assert plot.eng(value) == expected


def test_eng_handles_non_finite():
    assert plot.eng(float("inf")) == "-"


# ------------------------------------------------------------------- ticks

def test_linear_ticks_are_round_numbers():
    ticks = plot.linear_ticks(0, 10)
    assert ticks[0] >= 0 and ticks[-1] <= 10
    assert all(abs(t / 2 - round(t / 2)) < 1e-9 for t in ticks)


def test_linear_ticks_cover_the_range():
    ticks = plot.linear_ticks(-3.3, 7.7)
    assert len(ticks) >= 3
    assert min(ticks) >= -3.3 and max(ticks) <= 7.7


def test_linear_ticks_degenerate_range():
    assert plot.linear_ticks(5, 5) == [5]


def test_log_ticks_are_decades_over_a_wide_span():
    ticks = plot.log_ticks(10, 1e6)
    assert 10 in ticks and 1000 in ticks and 1e6 in ticks


def test_log_ticks_subdivide_a_narrow_span():
    ticks = plot.log_ticks(100, 1000)
    assert len(ticks) > 2       # 100, 200, 500, 1000


def test_log_ticks_refuse_non_positive():
    assert plot.log_ticks(0, 100) == []


# ------------------------------------------------------------------ scaling

def test_linear_scale_maps_endpoints():
    s = plot.Scale(0, 10, 100, 200)
    assert s(0) == 100
    assert s(10) == 200
    assert s(5) == pytest.approx(150)


def test_log_scale_is_even_per_decade():
    s = plot.Scale(1, 1000, 0, 300, log=True)
    assert s(1) == pytest.approx(0)
    assert s(10) == pytest.approx(100)
    assert s(100) == pytest.approx(200)
    assert s(1000) == pytest.approx(300)


def test_log_scale_survives_a_non_positive_value():
    s = plot.Scale(1, 1000, 0, 300, log=True)
    assert s(0) == 0            # clamped to the axis start, not a crash


# ------------------------------------------------------------------- labels

def test_declutter_separates_overlapping_labels():
    labels = [(100.0, "a", "#000", "x"), (101.0, "b", "#000", "x"),
              (102.0, "c", "#000", "x")]
    out = plot._declutter(labels, 0, 500, gap=13)
    ys = [y for y, *_ in out]
    assert all(ys[i + 1] - ys[i] >= 13 - 1e-9 for i in range(len(ys) - 1))


def test_declutter_keeps_order():
    labels = [(300.0, "low", "#000", "x"), (100.0, "high", "#000", "x")]
    out = plot._declutter(labels, 0, 500)
    assert [t[1] for t in out] == ["high", "low"]


def test_declutter_pulls_the_stack_off_the_bottom_edge():
    labels = [(295.0, "a", "#000", "x"), (296.0, "b", "#000", "x"),
              (297.0, "c", "#000", "x")]
    out = plot._declutter(labels, 0, 300, gap=13)
    assert max(y for y, *_ in out) <= 300 + 1e-9


def test_declutter_leaves_a_single_label_alone():
    assert plot._declutter([(50.0, "a", "#000", "x")], 0, 300)[0][0] == 50.0


def test_declutter_handles_nothing():
    assert plot._declutter([], 0, 300) == []


# ------------------------------------------------------------------ panels

def _captured():
    xs = [1.0, 10.0, 100.0]
    return {"ac": (xs, {"vm(out)": [1.0, 2.0, 3.0], "vp(out)": [0.0, -90.0, -180.0]})}


def test_magnitude_and_phase_become_separate_panels():
    """Never a second y-scale on one chart: a crossing point would then be an
    artefact of where the two scales happened to be pinned."""
    panels = plot.panels_from(_captured())
    assert len(panels) == 2
    units = {p.unit for p in panels}
    assert units == {"V", "deg"}
    for p in panels:
        assert len(p.series) == 1


def test_same_unit_vectors_share_one_panel():
    captured = {"tran": ([0.0, 1.0], {"v(in)": [0.0, 1.0], "v(out)": [0.0, 0.9]})}
    panels = plot.panels_from(captured)
    assert len(panels) == 1
    assert len(panels[0].series) == 2


@needs_ngspice
def test_markers_land_on_the_right_panel_and_axis(project_dir):
    import specs
    spec = specs.load(project_dir / "specs_lib" / "ce_bjt_amp.yaml")
    netlist = (project_dir / "baseline/ce_bjt_amp.cir").read_text(encoding="utf-8")
    captured = plot.capture(netlist, spec.analyses)
    values = specs.measure(spec, netlist)
    panels = plot.panels_from(captured, spec, values)

    volts = next(p for p in panels if p.unit == "V")
    gain = next(m for m in volts.markers if m.label == "|gain|")
    assert gain.axis == "y"
    assert gain.ok is False                 # 107 against a 50 +/-5% target
    assert gain.unit == "V/V"               # the measurement's unit, not the panel's


@needs_ngspice
def test_informational_measurements_are_not_drawn(project_dir):
    """They are reported, not stamped on the curve -- a rule per measured
    quantity is the clutter that goes unread."""
    import specs
    spec = specs.load(project_dir / "specs_lib" / "ce_bjt_amp.yaml")
    netlist = (project_dir / "baseline/ce_bjt_amp.cir").read_text(encoding="utf-8")
    panels = plot.panels_from(plot.capture(netlist, spec.analyses), spec,
                              specs.measure(spec, netlist))
    drawn = {m.label for p in panels for m in p.markers}
    assert "mid-band" not in drawn
    assert "|gain|" in drawn


@needs_ngspice
def test_a_span_measurement_is_not_drawn_as_a_level(project_dir):
    """Ripple is a magnitude. A horizontal rule at 8 mV would read as a DC
    level on a 5 V trace and mean nothing."""
    import specs
    spec = specs.load(project_dir / "specs_lib" / "buck_converter.yaml")
    netlist = (project_dir / "baseline/buck_converter.cir").read_text(encoding="utf-8")
    panels = plot.panels_from(plot.capture(netlist, spec.analyses), spec,
                              specs.measure(spec, netlist))
    drawn = {m.label for p in panels for m in p.markers}
    assert "ripple" not in drawn
    assert "V(out) avg" in drawn


# ----------------------------------------------------------------- rendering

def _html_tag(page: str) -> str:
    """The <html ...> element itself -- `data-theme` also appears in the CSS
    selectors, so a substring search over the whole page proves nothing."""
    start = page.index("<html")
    return page[start:page.index(">", start) + 1]


@pytest.fixture
def panels():
    return plot.panels_from(_captured())


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_svg_is_well_formed(panels, theme):
    ET.fromstring(plot.render_svg(panels, theme))


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_svg_uses_that_theme_surface(panels, theme):
    svg = plot.render_svg(panels, theme)
    assert plot.THEMES[theme]["surface"] in svg
    assert plot.THEMES["dark" if theme == "light" else "light"]["series"][0] not in svg


def test_svg_draws_one_polyline_per_series(panels):
    assert plot.render_svg(panels).count("<polyline") == 2


def test_multi_series_panel_gets_a_legend():
    captured = {"tran": ([0.0, 1.0], {"v(in)": [0.0, 1.0], "v(out)": [0.0, 0.9]})}
    svg = plot.render_svg(plot.panels_from(captured))
    assert 'class="legendlabel"' in svg


def test_single_series_panel_needs_no_legend(panels):
    """The panel title names it; a legend box would be noise."""
    assert 'class="legendlabel"' not in plot.render_svg(panels)


def test_gridlines_are_solid():
    """Dashes are reserved for spec limits, where a dash means threshold."""
    svg = plot.render_svg(plot.panels_from(_captured()))
    for line in svg.split("<line")[1:]:
        head = line.split("/>")[0]
        if "stroke-dasharray" in head:
            pytest.fail("a gridline is dashed")


def test_html_page_is_self_contained(panels):
    page = plot.render_html(panels, "demo", "sub")
    assert page.lstrip().startswith("<!doctype html>")
    assert "<svg" in page
    assert "http://" not in page.replace("http://www.w3.org", "")   # no CDN


def test_html_default_follows_the_reader_not_the_generator(panels):
    """A page generated on a light machine must still be dark for a reader
    whose OS is dark -- so the default stamps nothing."""
    html_tag = _html_tag(plot.render_html(panels, "demo"))
    assert "data-theme" not in html_tag, html_tag


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_explicit_theme_stamps_the_page(panels, theme):
    html_tag = _html_tag(plot.render_html(panels, "demo", theme=theme))
    assert f'data-theme="{theme}"' in html_tag


def test_inline_svg_uses_the_page_variables(panels):
    """The panels must switch with the page, not stay on whichever theme
    rendered them."""
    page = plot.render_html(panels, "demo")
    assert "var(--series-1)" in page
    assert plot.THEMES["light"]["series"][0] in page      # declared as a token
    assert 'stroke="#2a78d6"' not in page                 # but never baked in


def test_html_declares_dark_mode_both_ways(panels):
    """OS preference and an explicit theme stamp must each win."""
    page = plot.render_html(panels, "demo")
    assert "prefers-color-scheme: dark" in page
    assert '[data-theme="dark"]' in page


def test_html_includes_a_table_view_when_there_is_a_verdict(panels):
    verdict = {"specs": {"gain": {"target": "50 +/-5%", "measured": "107",
                                  "pass": False, "value": 107.0}}}
    page = plot.render_html(panels, "demo", verdict=verdict)
    assert "<table>" in page and "FAIL" in page


def test_series_names_are_escaped_into_the_svg():
    """Vector names come from a spec file, so they are untrusted text."""
    captured = {"ac": ([1.0, 2.0], {'v(<script>)': [1.0, 2.0]})}
    svg = plot.render_svg(plot.panels_from(captured))
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


# ------------------------------------------------------------------ end-to-end

@needs_ngspice
def test_cli_writes_an_svg(project_dir, tmp_path):
    out = tmp_path / "plot.svg"
    rc = plot._cli([str(project_dir / "baseline/rc_lowpass.cir"),
                    "--spec", str(project_dir / "specs_lib/rc_lowpass.yaml"),
                    "--format", "svg", "-o", str(out)])
    assert rc == 0
    ET.fromstring(out.read_text(encoding="utf-8"))


@needs_ngspice
def test_cli_works_without_a_spec(project_dir, tmp_path):
    """An ad-hoc sweep, for when you just want to look at the waveform."""
    out = tmp_path / "plot.svg"
    rc = plot._cli([str(project_dir / "baseline/rc_lowpass.cir"),
                    "--analysis", "ac dec 50 10 1meg", "--vectors", "vdb(out)",
                    "--format", "svg", "-o", str(out)])
    assert rc == 0
    assert "<polyline" in out.read_text(encoding="utf-8")


@needs_ngspice
def test_cli_reports_a_missing_deck(tmp_path):
    assert plot._cli([str(tmp_path / "nope.cir"), "--analysis", "op",
                      "--vectors", "v(out)"]) == 2


def test_cli_requires_a_spec_or_an_analysis(project_dir):
    assert plot._cli([str(project_dir / "baseline/rc_lowpass.cir")]) == 2
