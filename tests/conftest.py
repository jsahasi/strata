"""Test configuration. Read the note on ordering before editing.

The first two statements run before any `app.` import, on purpose: app.state.db
reads STRATA_DATABASE_URL at import time and init_db() drops tables. Point the
suite at a scratch file and the developer's strata.db is never touched.
"""

import os
import tempfile
from pathlib import Path

os.environ["STRATA_DATABASE_URL"] = (
    "sqlite:///" + str(Path(tempfile.gettempdir()) / "strata-test.db")
)

import pytest  # noqa: E402


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def data_dir(repo_root: Path) -> Path:
    return repo_root / "data"
