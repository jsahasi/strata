#!/usr/bin/env python3
"""Run the additive schema migration and say what it did.

Called by deploy/entrypoint.sh on EVERY start, before anything reads a table.
A migration that runs only against a fresh database is the same as no migration:
the deploy that adds a column is exactly the deploy where the old table is still
there, and the failure lands on the live site rather than in a test.

Exits non-zero if the migration refused something, so a deploy stops rather than
starting an application against a schema it cannot read.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import load_env  # noqa: E402

load_env()

from app.state.db import get_engine  # noqa: E402
from app.state.migrate import migrate  # noqa: E402


def main() -> int:
    report = migrate(get_engine())

    for key in ("tables", "columns"):
        if report[key]:
            print(f"strata: migration added {key}: {', '.join(report[key])}")

    if report["refused"]:
        for line in report["refused"]:
            print(f"strata: MIGRATION REFUSED {line}", file=sys.stderr)
        print(
            "strata: refusing to start against a schema the code cannot read.",
            file=sys.stderr,
        )
        return 1

    if not any(report.values()):
        print("strata: schema already current.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
