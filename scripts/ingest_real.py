#!/usr/bin/env python3
"""Ingest a real public filing pair from data/real/ and diff it.

WHY THIS EXISTS. The product shipped its demonstration on a synthetic corpus
while 102 real public filings sat in data/real/ untouched, each with a
provenance record naming its docket, its filer, the date it was filed and the
URL it came from. The synthetic corpus earns its place -- it carries traps built
on purpose, one sentence repeated three times per version and a restructure that
scores 0.944 on raw similarity, and the eval harness measures against those. It
is a test fixture. A test fixture is not a demonstration.

WHAT THIS ADDS THAT SYNTHETIC CANNOT. Every claim gets a link to the filing on
the commission's own site, so a reader can leave and check. The version pair is
one a filer actually produced: Kentucky 2025-00113, Lane Kollen's direct
testimony, filed and then corrected. 1,024,409 characters against 1,024,536 --
a 127-character difference across a million characters, which is the shape of
the problem stated as a number rather than as a claim. And the filer published
a MARKED-UP copy saying what they changed, so for once there is an answer key
nobody on this side wrote.

WHAT IT DOES NOT DO. It does not replace the synthetic corpus and must not: the
eval numbers are measured against traps a real filing will not reproduce on
demand. Both corpora live in the product, each labelled for what it is, which is
what ADR-40 decided.

Run:  .venv/bin/python scripts/ingest_real.py
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_env  # noqa: E402

load_env()

from app.pipeline import ingest_and_diff  # noqa: E402
from app.state.db import session_scope  # noqa: E402
from app.state.models import Proceeding  # noqa: E402

REAL = ROOT / "data" / "real"
COMPANY = "MEP"

#: One pair, chosen for what it demonstrates rather than for being convenient.
#: The corrected copy differs from the original by 127 characters in a million,
#: and the filer's own markup is in the corpus beside them.
PAIRS = [
    {
        "proceeding_id": "KY-PSC-2025-00113",
        "docket": "KY PSC 2025-00113",
        "subject": "Curtailable Service Rider and large load terms",
        "commission": "Kentucky Public Service Commission",
        "versions": [
            ("ky-2025-00113-kollen-direct-testimony-original", "FINAL"),
            ("ky-2025-00113-kollen-direct-testimony-corrected-clean", "FINAL"),
        ],
    },
]


def provenance(stem: str) -> dict:
    return json.loads((REAL / f"{stem}.provenance.json").read_text(encoding="utf-8"))


def main() -> None:
    with session_scope() as session:
        for pair in PAIRS:
            existing = (
                session.query(Proceeding)
                .filter(
                    Proceeding.company_id == COMPANY,
                    Proceeding.id == pair["proceeding_id"],
                )
                .one_or_none()
            )
            if existing is None:
                session.add(
                    Proceeding(
                        id=pair["proceeding_id"],
                        company_id=COMPANY,
                        docket=pair["docket"],
                        subject=pair["subject"],
                        commission=pair["commission"],
                    )
                )
                session.flush()
                print(f"proceeding {pair['proceeding_id']} created")
            else:
                print(f"proceeding {pair['proceeding_id']} already present")

            previous = None
            for stem, status in pair["versions"]:
                meta = provenance(stem)
                text = (REAL / f"{stem}.txt").read_text(encoding="utf-8", errors="replace")
                version_id = stem
                label = meta.get("document_title") or stem
                changes = ingest_and_diff(
                    session,
                    company_id=COMPANY,
                    proceeding_id=pair["proceeding_id"],
                    version_id=version_id,
                    label=label[:200],
                    status=status,
                    source_text=text,
                    previous_version_id=previous,
                )
                filed = meta.get("filing_date", "?")
                print(
                    f"  {stem[:56]:58} {len(text):>9,} chars  filed {filed}  "
                    f"{len(changes)} change(s)"
                )
                previous = version_id


if __name__ == "__main__":
    main()
