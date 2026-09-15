"""Shared fixtures.

The whole suite is free to run: it exercises parsing, geometry and ngspice.
No test makes an Anthropic API call.
"""
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(PROJECT_DIR))


def _ngspice_available() -> bool:
    try:
        import main
        main.find_ngspice()
        return True
    except Exception:
        return False


needs_ngspice = pytest.mark.skipif(
    not _ngspice_available(),
    reason="ngspice not found (set NGSPICE_PATH or install it)",
)


@pytest.fixture(scope="session")
def project_dir() -> Path:
    return PROJECT_DIR


@pytest.fixture(scope="session")
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def no_symbol_library(monkeypatch):
    """Force the built-in symbol table, as on a machine without LTspice.

    Several behaviours only exist on that path -- inferred pins, the
    pin-order TODO -- and they must stay tested on a machine that does have
    LTspice installed.
    """
    import ascview
    monkeypatch.setattr(ascview, "USE_SYMBOL_LIBRARY", False)
    ascview._lib_cache.clear()
    yield
    ascview._lib_cache.clear()


@pytest.fixture(scope="session")
def symbol_library():
    """The LTspice symbol library, or skip if this machine has none."""
    import symlib
    root = symlib.find_library()
    if root is None:
        pytest.skip("no LTspice symbol library installed")
    return root
