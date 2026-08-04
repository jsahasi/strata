"""Test configuration. Read both notes before editing.

ORDERING. The environment assignment below runs before any `app.` import, on
purpose: app.state.db reads STRATA_DATABASE_URL at import time and init_db()
drops tables. Pointing the suite at a scratch file is what keeps the developer's
strata.db from being destroyed on every `make test`.

ISOLATION. The scratch path carries the process id. Without it every concurrent
pytest process shares one file, and because init_db() drops tables, two runs at
once delete each other's schema mid-test. The failures that produces look like
real defects -- assertion errors deep in unrelated modules -- and cost more to
diagnose than they do to prevent. Anything that runs tests in parallel hits this:
several agents in one working tree, pytest-xdist, a watcher alongside a manual
run. One integer removes the whole class.
"""

import os
import tempfile
from pathlib import Path

os.environ["STRATA_DATABASE_URL"] = (
    "sqlite:///" + str(Path(tempfile.gettempdir()) / f"strata-test-{os.getpid()}.db")
)

import pytest  # noqa: E402


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def data_dir(repo_root: Path) -> Path:
    return repo_root / "data"
