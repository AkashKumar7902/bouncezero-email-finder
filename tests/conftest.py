"""Shared pytest fixtures for the emailfinder test suite."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

PKG_DATA = Path(__file__).resolve().parent.parent / "emailfinder" / "data"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def data_dir(tmp_path) -> Path:
    """An isolated per-test data dir (silo) so nothing touches ~/.emailfinder."""
    d = tmp_path / "silo"
    d.mkdir()
    return d


@pytest.fixture
def config(data_dir):
    from emailfinder.config import load_config

    return load_config(overrides={"data_dir": str(data_dir), "user_id": "test"})


@pytest.fixture
def engine(config):
    from emailfinder.engine import Engine

    eng = Engine(config)
    yield eng
    eng.close()


# The shipped package carries NO real audit data (only generic knowledge + an
# empty KB seed). Tests use a SYNTHETIC fixture KB / bounce CSV with fake
# domains and names — the code paths are identical, no PII is bundled.
@pytest.fixture(scope="session")
def sample_kb() -> dict:
    """Synthetic on-disk-schema KB (decoded to in-memory form) for pure tests."""
    from emailfinder import kb_store

    tmp = FIXTURES / "sample_kb.json"
    return kb_store._decode(json.loads(tmp.read_text()))


@pytest.fixture(scope="session")
def sample_kb_path() -> Path:
    """Path to the synthetic on-disk KB fixture (raw, undecoded)."""
    return FIXTURES / "sample_kb.json"


@pytest.fixture(scope="session")
def sample_bounces_path() -> Path:
    """Path to the synthetic bounce CSV fixture (audit records.csv schema)."""
    return FIXTURES / "sample_bounces.csv"
