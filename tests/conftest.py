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

OFFLINE. `no_api_key` below is autouse and takes the key out of the environment
for every test in the suite. Read why before deleting it.
"""

import os
import tempfile
from pathlib import Path

os.environ["STRATA_DATABASE_URL"] = (
    "sqlite:///" + str(Path(tempfile.gettempdir()) / f"strata-test-{os.getpid()}.db")
)

import pytest  # noqa: E402

#: The one name that turns a model path on. Spelled here rather than imported so
#: that this file, which runs before anything else, still imports nothing from
#: app/. app/chat/agent.py::ENV_API_KEY and
#: app/interpretation/propose.py::transport_from_environment own the same string;
#: tests/test_model_status.py imports the constant and pins them together.
ENV_API_KEY = "ANTHROPIC_API_KEY"


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    """Take the key out of the environment for every test. Offline is the rule.

    THIS IS NOT BELT AND BRACES. A real key lives in the repository's .env,
    app/main.py calls load_env() at import, and several test modules import
    app.main -- so by the time collection finishes, ANTHROPIC_API_KEY is in
    os.environ of the pytest process. Any code path that reads it at request
    time then reaches the live API from `make test`: real money, real latency,
    and a suite whose result depends on what a model said this afternoon. The
    change screen now judges materiality on a GET, so that path exists.

    The rule the repository already states -- every test drives a deterministic
    fake through an injected transport, no test needs a key or a network -- was
    true only because nothing read the environment during a request. Making it
    true by construction is one fixture.

    A test that wants the key-present branch sets it itself. monkeypatch.setenv
    inside the test body runs after this fixture and wins, which is what
    tests/test_model_status.py already does.
    """
    monkeypatch.delenv(ENV_API_KEY, raising=False)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def data_dir(repo_root: Path) -> Path:
    return repo_root / "data"
