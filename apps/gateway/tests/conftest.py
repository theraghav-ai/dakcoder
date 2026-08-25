"""Fixtures for the gateway tests. The fakes themselves live in fakes.py,
so a test can import them directly without importing a conftest — which
pytest treats specially and which two apps cannot both name `tests`.
"""

from __future__ import annotations

import pytest

from fakes import FakeGitLab, FakeUpstream


@pytest.fixture
def gitlab() -> FakeGitLab:
    return FakeGitLab()


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()
