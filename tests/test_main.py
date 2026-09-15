"""The generate -> run -> fix loop's non-API parts.

Nothing here calls Anthropic: the model boundary is exercised through the
pure text helpers (fence stripping) and the ngspice boundary directly.
"""
import os

import pytest

import main
from conftest import needs_ngspice

FENCE = "`" * 3


# ------------------------------------------------------------ fence stripping

def test_plain_netlist_is_untouched():
    deck = "* title\nR1 in out 1k\n.end\n"
    assert main.strip_fences(deck) == deck


def test_strips_a_wrapping_code_fence():
    deck = FENCE + "\n* title\nR1 in out 1k\n.end\n" + FENCE
    assert main.strip_fences(deck) == "* title\nR1 in out 1k\n.end\n"


def test_strips_a_language_tagged_fence():
    deck = FENCE + "spice\n* title\nR1 in out 1k\n.end\n" + FENCE
    assert main.strip_fences(deck) == "* title\nR1 in out 1k\n.end\n"


def test_strips_stray_fence_only_lines():
    deck = "* title\n" + FENCE + "\nR1 in out 1k\n.end\n"
    assert FENCE not in main.strip_fences(deck)


def test_always_ends_with_exactly_one_newline():
    for deck in ("* t\n.end", "* t\n.end\n", "* t\n.end\n\n\n"):
        out = main.strip_fences(deck)
        assert out.endswith(".end\n")
        assert not out.endswith(".end\n\n")


# ------------------------------------------------------- error classification

@pytest.mark.parametrize("output", [
    "Error on line 3 : R1 in out\n",
    "ERROR: unknown subckt\n",
    "fatal error during parsing\n",
    "run simulation(s) aborted\n",
    "doAnalyses: TRAN:  Timestep too small; simulation interrupted\n",
    "there aren't any circuits loaded\n",
    "can't find the model\n",
    "unknown device type\n",
])
def test_error_signatures_are_detected(output):
    assert main._ERROR_RE.search(output), output


@pytest.mark.parametrize("output", [
    "Circuit: * rc low-pass\nNo. of Data Rows : 51\n",
    "Total analysis time (seconds) = 0.012\n",
    "tran analysis completed\n",
])
def test_clean_output_is_not_flagged(output):
    assert not main._ERROR_RE.search(output), output


# ------------------------------------------------------------------- ngspice

@needs_ngspice
def test_run_ngspice_accepts_a_good_deck():
    deck = ("* divider\nV1 in 0 10\nR1 in out 1k\nR2 out 0 1k\n"
            ".control\nop\nprint v(out)\n.endc\n.end\n")
    ok, out = main.run_ngspice(deck)
    assert ok is True, out
    assert "5" in out


@needs_ngspice
def test_run_ngspice_rejects_a_broken_deck():
    """ngspice frequently exits 0 on a bad deck, so the exit code alone is not
    a usable success signal -- this is why the output is scanned too."""
    ok, out = main.run_ngspice("* broken\nX9 a b nosuchsubckt\n.control\nop\n.endc\n.end\n")
    assert ok is False
    assert "unknown subckt" in out.lower()


@needs_ngspice
def test_known_leniency_valueless_component_is_not_caught():
    """Documented limitation, not an aspiration.

    ngspice accepts `R1 in out` with no value and reports a clean run, so the
    output-scanning heuristic cannot see it either. This is exactly the class
    of miss that motivates the spec layer: correctness is established by
    measuring the circuit, not by watching the simulator exit quietly.
    """
    ok, _ = main.run_ngspice("* t\nV1 in 0 1\nR1 in out\n.control\nop\n.endc\n.end\n")
    assert ok is True


@needs_ngspice
def test_benign_checkvalid_warning_is_not_treated_as_failure():
    """`print ac v(out)` makes ngspice look for a vector literally named `ac`
    and warn that it is unavailable -- while the ac analysis itself ran fine.

    Treating that warning as an error signature would fail three committed
    decks, two of which meet their spec, so it is deliberately excluded.
    """
    deck = ("* t\nV1 in 0 ac 1\nR1 in out 1.6k\nC1 out 0 100n\n"
            ".control\nac dec 20 1 1meg\nprint ac v(out)\n.endc\n.end\n")
    ok, out = main.run_ngspice(deck)
    assert "not available or has zero length" in out
    assert ok is True


@needs_ngspice
def test_run_ngspice_cleans_up_its_temp_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    before = set(tmp_path.iterdir())
    main.run_ngspice("* t\nV1 a 0 1\nR1 a 0 1k\n.control\nop\n.endc\n.end\n")
    assert set(tmp_path.iterdir()) == before


@needs_ngspice
def test_find_ngspice_returns_an_existing_executable():
    from pathlib import Path
    assert Path(main.find_ngspice()).exists()


def test_ngspice_path_env_var_wins(monkeypatch, tmp_path):
    fake = tmp_path / "my_ngspice.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("NGSPICE_PATH", str(fake))
    assert main.find_ngspice() == str(fake)


def test_missing_ngspice_raises_a_helpful_error(monkeypatch, tmp_path):
    monkeypatch.delenv("NGSPICE_PATH", raising=False)
    monkeypatch.setattr(main.shutil, "which", lambda _n: None)
    monkeypatch.setattr(main, "__file__", str(tmp_path / "main.py"))
    with pytest.raises(FileNotFoundError, match="Could not find ngspice"):
        main.find_ngspice()


# -------------------------------------------------------------------- .env

def test_load_dotenv_reads_key_value_pairs(monkeypatch, tmp_path):
    monkeypatch.delenv("SPICE_TEST_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text('SPICE_TEST_KEY="abc123"\n', encoding="utf-8")
    main.load_dotenv(env)
    assert os.environ["SPICE_TEST_KEY"] == "abc123"
    monkeypatch.delenv("SPICE_TEST_KEY", raising=False)


def test_real_environment_wins_over_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("SPICE_TEST_KEY", "from-shell")
    env = tmp_path / ".env"
    env.write_text("SPICE_TEST_KEY=from-file\n", encoding="utf-8")
    main.load_dotenv(env)
    assert os.environ["SPICE_TEST_KEY"] == "from-shell"


def test_comments_and_blank_lines_are_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("SPICE_TEST_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("# a comment\n\nSPICE_TEST_KEY=ok\nnot_a_pair\n", encoding="utf-8")
    main.load_dotenv(env)
    assert os.environ["SPICE_TEST_KEY"] == "ok"
    monkeypatch.delenv("SPICE_TEST_KEY", raising=False)


def test_missing_env_file_is_not_an_error(tmp_path):
    main.load_dotenv(tmp_path / "nope.env")
