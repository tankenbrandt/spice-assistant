"""The end-to-end driver: one circuit through every layer into one report.

The composition tests build Step objects directly, so they run anywhere. The
end-to-end class drives the real CLI and needs ngspice.
"""
import report
from conftest import needs_ngspice


def _step(label, value=None, note="", error=""):
    s = report.Step(label)
    s.value, s.note, s.error = value, note, error
    return s


# --------------------------------------------------------------- table cells

def test_pipes_in_a_measurement_name_are_escaped():
    """Titles are things like `|gain|`; unescaped, the row grows two columns."""
    assert report.cell("|gain|") == r"\|gain\|"


def test_cell_accepts_non_strings():
    assert report.cell(51.5) == "51.5"


# ---------------------------------------------------------------------- steps

def test_status_prefers_the_error():
    s = _step("monte carlo", value={"yield": 1.0}, note="yield 100%",
              error="RuntimeError: boom")
    assert s.status.startswith("error - RuntimeError")


def test_status_falls_back_to_skipped():
    assert _step("ltspice cross-check").status == "skipped"


def test_skipped_records_the_reason():
    s = report.Step("monte carlo").skipped("--mc 0")
    assert s.status == "--mc 0"
    assert s.value is None


# ------------------------------------------------------------------ composing

def _steps(**overrides):
    base = {label: report.Step(label) for label in
            ("netlist", "schematic", "operating point", "spec check",
             "monte carlo", "ltspice cross-check")}
    base["netlist"].ok("* deck\n", "`x.cir`")
    base.update(overrides)
    return base


def test_a_layer_that_failed_does_not_lose_the_others():
    """A report is most useful on the circuit that is going wrong."""
    steps = _steps()
    steps["ltspice cross-check"] = _step("ltspice cross-check",
                                         error="RuntimeError: LTspice is not installed here")
    steps["operating point"] = _step(
        "operating point",
        value=__import__("oppoint").OperatingPoint(nodes={"out": 5.0}))
    md = report.compose("t", "* deck", steps, None, 0, 1.0)
    assert "Operating point" in md
    assert "## Layers that did not run" in md
    assert "LTspice is not installed here" in md


def test_summary_lists_every_layer():
    md = report.compose("t", "* deck", _steps(), None, 0, 1.0)
    for label in ("netlist", "schematic", "operating point", "spec check",
                  "monte carlo", "ltspice cross-check"):
        assert f"| {label} |" in md


def test_spec_sections_are_absent_without_a_spec():
    md = report.compose("t", "* deck", _steps(), None, 0, 1.0)
    assert "Does it meet its spec?" not in md
    assert "Does a second simulator agree?" not in md


def test_the_deck_is_always_included():
    md = report.compose("t", "* my deck here", _steps(), None, 0, 1.0)
    assert "```spice" in md and "* my deck here" in md


def test_verdict_table_escapes_and_marks_failures():
    verdict = {"specs": {"|gain|": {"pass": False, "target": "50 +/-5%",
                                    "measured": "107 (+114%)"}},
               "informational": {}}
    rows = "\n".join(report._verdict_table(verdict))
    assert r"\|gain\|" in rows
    assert "**FAIL**" in rows


# ---------------------------------------------------------------------- guard

def test_out_must_not_be_the_project_root(project_dir, capsys):
    """`report.md` and the committed `REPORT.md` are the same file on Windows."""
    rc = report._cli(["baseline/ce_bjt_amp.cir", "--out", str(project_dir)])
    assert rc == 2
    assert "REPORT.md" in capsys.readouterr().err


def test_a_missing_deck_is_reported(tmp_path, capsys):
    rc = report._cli(["no_such_deck.cir", "--out", str(tmp_path)])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


# ------------------------------------------------------------------ end-to-end

@needs_ngspice
class TestEndToEnd:
    def test_deck_and_spec_produce_a_full_report(self, tmp_path, project_dir):
        rc = report._cli([
            str(project_dir / "repaired" / "ce_bjt_amp.cir"),
            "--spec", str(project_dir / "specs_lib" / "ce_bjt_amp.yaml"),
            "--out", str(tmp_path), "--mc", "8", "--no-ltspice",
        ])
        assert rc == 0
        md = (tmp_path / "report.md").read_text(encoding="utf-8")
        for heading in ("## Summary", "## Schematic", "## Operating point",
                        "## Does it meet its spec?",
                        "## Does it stay correct once the parts have tolerances?"):
            assert heading in md
        # the schematic is a real artifact, not just a link
        assert (tmp_path / "ce_bjt_amp.asc").exists()
        assert (tmp_path / "ce_bjt_amp.svg").exists()

    def test_without_a_spec_it_still_reports_the_operating_point(
            self, tmp_path, project_dir):
        """The free, spec-less path: what is this circuit doing right now?"""
        rc = report._cli([str(project_dir / "baseline" / "ce_bjt_amp.cir"),
                          "--out", str(tmp_path)])
        assert rc == 0
        md = (tmp_path / "report.md").read_text(encoding="utf-8")
        assert "## Operating point" in md
        assert "Does it meet its spec?" not in md
        assert "no --spec given" in md

    def test_the_baseline_amp_is_reported_as_failing(self, tmp_path, project_dir):
        """The project's central claim, through the driver: this deck
        simulates cleanly and does not meet its spec."""
        rc = report._cli([
            str(project_dir / "baseline" / "ce_bjt_amp.cir"),
            "--spec", str(project_dir / "specs_lib" / "ce_bjt_amp.yaml"),
            "--out", str(tmp_path), "--mc", "0", "--no-ltspice",
        ])
        assert rc == 0
        md = (tmp_path / "report.md").read_text(encoding="utf-8")
        assert "**FAIL**" in md
        assert "| spec check | FAIL |" in md
