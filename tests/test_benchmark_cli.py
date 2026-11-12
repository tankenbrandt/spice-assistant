"""benchmark.py must refuse --help and unknown flags before it spends anything."""

import benchmark


def test_help_exits_zero(capsys):
    assert benchmark.check_args(["--help"]) == 0
    assert "Anthropic API" in capsys.readouterr().out


def test_unknown_flag_refused():
    assert benchmark.check_args(["--dry-run"]) == 2


def test_out_needs_a_path():
    assert benchmark.check_args(["--out"]) == 2


def test_known_flags_pass():
    assert benchmark.check_args([]) is None
    assert benchmark.check_args(["--spec-repair", "--round2", "--lab", "--out", "x.json"]) is None


def test_main_entry_help_spends_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("--help reached the API")
    monkeypatch.setattr(benchmark.main, "_message_text", boom)
    monkeypatch.setattr(benchmark.sys, "argv", ["benchmark.py", "--help"])
    assert benchmark.main_entry() == 0
