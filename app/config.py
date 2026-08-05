"""Load .env into the process environment, once, at startup.

WHY THIS EXISTS. Three modules read configuration from os.environ and each one
falls back safely when it finds nothing: app/chat/agent.py returns no transport
and Clarke says the assistant is unavailable, app/interpretation/propose.py
declines to propose, app/notify/transport.py declines to send and says so. All
three announce the fallback, which is right.

What was wrong is that they announced it ALWAYS. The key lived in .env, nothing
put .env into os.environ, and so a deployment with a perfectly good key behaved
exactly like one with none. Every fallback fired, every message was honest, and
the product was still wrong -- which is the worst shape a bug can take, because
nothing looks broken. `make run` could never use a key a reviewer had already
put in the file the README told them to put it in.

NO NEW DEPENDENCY. python-dotenv would do this, and this is fifteen lines of
standard library against a file whose format we control. requirements.txt stays
as it is, which keeps the reviewer's `make run` on one clean path (ADR-14).

THE REAL ENVIRONMENT ALWAYS WINS. A variable already set in os.environ is never
overwritten by the file. A container, a systemd unit or an export on the command
line is a deliberate act by whoever is running the process; a file on disk is a
default. Reading it the other way round means a deployment cannot override its
own checked-out configuration, and the surprise lands in production.

NEVER LOG A VALUE. This function reports which NAMES it set and nothing else.
The file holds an API key, an OAuth refresh token and a client secret, and a
startup line that helpfully echoed them would put all three into every log
aggregator the host ships to.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The repository root, which is where .env sits and where .gitignore keeps it.
ROOT = Path(__file__).resolve().parents[1]

ENV_FILE = ROOT / ".env"


def load_env(path: Path | None = None, *, environ: dict[str, str] | None = None) -> list[str]:
    """Put any name from .env that is not already set into the environment.

    Returns the names it set, in file order, so a caller can say what it found
    without saying what the values were. A missing file is not an error: a
    reviewer cloning this repository has no .env and every consumer of these
    names already degrades safely and says so.
    """
    target = os.environ if environ is None else environ
    source = ENV_FILE if path is None else path
    if not source.exists():
        return []

    added: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or name in target:
            # Already set. The environment beats the file, always.
            continue
        # Strip one matching pair of surrounding quotes, and nothing cleverer --
        # this is not a shell and it must not try to be one.
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            # A NAME WITH NO VALUE IS NOT SET. It is not set to the empty
            # string, and the difference is the whole of this branch.
            #
            # .env.example ships every name with a bare trailing "=" and tells
            # the reader to "copy to .env and fill in what you need". Doing
            # exactly that used to KILL THE APPLICATION ON IMPORT: the blank
            # landed in os.environ as "", so os.environ.get(name, default)
            # stopped returning the default, app/state/db.py called
            # create_engine("") and raised, and app/state/claims.py called
            # int("") and raised. Both at module scope, so nothing started. That
            # is a reviewer's first five minutes, spent on a file that told them
            # to do the thing that broke it.
            #
            # The real environment is untouched by this rule and must be: a
            # variable EXPORTED as empty is how an operator switches one setting
            # off for one container, and that is a deliberate act. A blank line
            # in a template file is not.
            continue
        target[name] = value
        added.append(name)
    return added
