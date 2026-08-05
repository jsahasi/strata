#!/usr/bin/env python3
"""Build the passage index from the stored corpus. Says what it did.

WHY THIS IS A SCRIPT AND NOT A MIGRATION. The index is derived data, not schema.
app/state/migrate.py is additive and never backfills, on the argument that a
value invented for an old row is a fact the record cannot support -- and that
argument is right and does not cover this. Derived data has the opposite rule:
best-practices.html section 27 says it migrates all at once or not at all,
because half of it answers every query plausibly and wrongly, with no error to
notice. So the index is thrown away and rebuilt whole, every time, and it lives
behind its own command rather than inside a schema step that must never do that.

Safe to run at any time. It reads passages and writes only the index, so the
worst a bad run costs is that retrieval says the index is missing and answers
from a complete scan instead -- which is what it does before this is ever run.

Exits non-zero when it could not build, so a caller wiring this into a deploy
finds out rather than assuming.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import load_env  # noqa: E402

load_env()

from app.state.db import get_engine  # noqa: E402
from app.state.search import SCHEME, rebuild_index  # noqa: E402


def main() -> int:
    try:
        report = rebuild_index(get_engine())
    except Exception as exc:
        # Named rather than swallowed. A build step that reports success it did
        # not have is the fallback that does not announce itself, and here it
        # would leave every later query degrading for a reason nobody recorded.
        print(f"build_index: refused. {exc}", file=sys.stderr)
        return 1

    count = report["indexed"]
    word = "passage" if count == 1 else "passages"
    print(f"build_index: indexed {count} {word} under scheme {report['scheme']}.")
    if count == 0:
        # Not an error and not a success either. An empty corpus and a corpus
        # nobody loaded look identical from here, and the difference is the
        # whole question the reader has.
        print(
            "build_index: the corpus holds no passages. If that is a surprise, "
            "load one with `make seed` and run this again."
        )
    print(f"build_index: rerun this after any ingestion. Scheme {SCHEME}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
