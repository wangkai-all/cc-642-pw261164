"""Shared fixtures for the CANLab test suite.

The tests exercise the same objects the server uses: the parser, the bit
engine, the validators, the oracle and a real ``Session``.  No test reaches
into a private attribute of the UI; the browser side has its own harness
(``tests/ui_smoke.js``).
"""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURE_DIR = os.path.join(ROOT, "fixtures")


def fixture_path(*parts: str) -> str:
    return os.path.join(FIXTURE_DIR, *parts)


def read_fixture(*parts: str) -> str:
    with open(fixture_path(*parts), "r", encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="session")
def demo_text() -> str:
    return read_fixture("dbc", "demo_ecu.dbc")


@pytest.fixture(scope="session")
def conflict_a_text() -> str:
    return read_fixture("dbc", "conflict_a.dbc")


@pytest.fixture(scope="session")
def conflict_b_text() -> str:
    return read_fixture("dbc", "conflict_b.dbc")


@pytest.fixture(scope="session")
def demo(demo_text):
    from canlab.dbc import parse_dbc_text
    return parse_dbc_text(demo_text, "demo_ecu.dbc")


@pytest.fixture(scope="session")
def conflict_a(conflict_a_text):
    from canlab.dbc import parse_dbc_text
    return parse_dbc_text(conflict_a_text, "conflict_a.dbc")


@pytest.fixture(scope="session")
def conflict_b(conflict_b_text):
    from canlab.dbc import parse_dbc_text
    return parse_dbc_text(conflict_b_text, "conflict_b.dbc")


@pytest.fixture(scope="session")
def scenarios() -> list[dict]:
    with open(fixture_path("frames", "samples.json"), "r", encoding="utf-8") as h:
        return json.load(h)["scenarios"]


@pytest.fixture()
def lab_session(demo_text, conflict_a_text, conflict_b_text):
    """A fresh session with the three fixtures loaded, like the demo."""
    from canlab.session import Session
    s = Session()
    s.add_dbc("demo_ecu.dbc", demo_text)
    s.add_dbc("conflict_a.dbc", conflict_a_text)
    s.add_dbc("conflict_b.dbc", conflict_b_text)
    return s


@pytest.fixture()
def lab_session_no_oracle(monkeypatch):
    """A session that behaves as if cantools were not installed.

    The oracle being optional is a product requirement, so at least one
    path must be tested without it.
    """
    from canlab import session as session_module
    monkeypatch.setattr(session_module, "load_with_cantools",
                        lambda text, filename="uploaded.dbc": _Unavailable())
    s = session_module.Session()
    return s


class _Unavailable:
    """Stands in for ``LibraryLoadResult`` with ``available=False``."""

    available = False
    version = "not installed"
    ok = False
    strict_ok = None
    strict_error = None
    db = None
    messages: list = []
    diagnostics: list = []

    def to_dict(self):
        return {
            "available": False,
            "version": "not installed",
            "ok": False,
            "strict_ok": None,
            "strict_error": None,
            "messages": [],
            "diagnostics": [{
                "call": "cantools.database.load_string(text, database_format='dbc', strict=False)",
                "exception": "ImportError",
                "message": "cantools is not installed; the oracle is disabled",
                "detail": "test stub",
                "business_meaning": "decode continues without the oracle",
            }],
        }
