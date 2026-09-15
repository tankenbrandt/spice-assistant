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
