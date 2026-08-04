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

WHAT IT NOW CARRIES, having thrown it away until today. Each version keeps the
address it was fetched from, who filed it and on what day, so every claim drawn
from this docket links back to the Commission's own copy of the PDF. That is the
single thing a regulatory reader wants from a citation: not our text, theirs.
The address goes through the same check the screens use, so a provenance file
naming a `javascript:` address is refused here rather than in a template.

AND ONE CLAIM, WRITTEN BY A PERSON. Nothing in this product extracts claims yet;
app/seed.py writes the synthetic ones by rule. Without a claim, this proceeding
would be 144 changes with nothing said about any of them, and the link this
script exists to carry would render on no screen at all -- built and connected
to nothing. So CLAIMS below holds one hand-written statement about the one
substantive edit the filer made, citing the corrected filing at the offsets
where it sits. No model wrote it. Its citation is re-checked on every render
like any other, and this script prints the verdict it gets.

Run:  .venv/bin/python scripts/ingest_real.py
"""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_env  # noqa: E402

load_env()

from app.pipeline import ACTION_INGEST, ACTOR, ingest_and_diff  # noqa: E402
from app.state.audit import record_event  # noqa: E402
from app.state.claims import safe_source_url, verified_claims  # noqa: E402
from app.state.db import session_scope  # noqa: E402
from app.state.models import Change, Claim, Proceeding  # noqa: E402
from app.state.queries import versions_for_company  # noqa: E402

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


#: The one claim on the real corpus. A person wrote this sentence.
#:
#: The filer changed "Brown BESS" to "Cane Run BESS" in the list of settled CPCN
#: requests, and that single edit is buried in about 1,370 diff lines of pure
#: re-layout noise. The statement says only what the cited characters show. The
#: original names the Brown BESS in the same sentence, and that is deliberately
#: NOT asserted here: this claim cites the corrected filing, and a statement
#: about a document the citation does not cover is the sloppiness this product
#: exists to refuse.
#:
#: The occurrence is not optional. "Cane Run BESS" appears four times in the
#: corrected filing -- twice from this edit and twice in text that already said
#: it -- so a citation that does not name which one it means is ambiguous, and
#: the verifier refuses it. This is the same trap the synthetic corpus plants on
#: purpose, arriving on its own in a real document.
CLAIMS = [
    {
        "id": "CLM-KY-2025-00113-BESS",
        "version_id": "ky-2025-00113-kollen-direct-testimony-corrected-clean",
        "start": 243069,
        "end": 243082,
        "quote": "Cane Run BESS",
        "occurrence": 0,
        "statement": (
            "The corrected testimony names the Cane Run BESS among the settled "
            "CPCN requests."
        ),
    },
]

#: Flat, and the same number the synthetic corpus uses. Nothing here is a
#: probability: a person wrote the sentence and the citation either matches the
#: stored bytes or it does not.
CONFIDENCE_BP = 10000


def provenance(stem: str) -> dict:
    return json.loads((REAL / f"{stem}.provenance.json").read_text(encoding="utf-8"))


def _instant(raw: str | None) -> datetime | None:
    """The retrieval time as an aware UTC instant, or nothing.

    The provenance files stamp "2026-08-04T16:47:36Z". Anything this cannot
    parse, and anything that arrives without a zone, returns None: a naive
    timestamp in a provenance record cannot be compared across systems, and
    guessing UTC would invent the one fact the record exists to carry. main()
    says out loud when it drops one.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def provenance_columns(meta: dict) -> dict:
    """The four provenance columns a version carries, from the JSON beside it.

    WHY THIS EXISTS AT ALL. The script used to read this file for a display line
    and throw the rest away, so a claim drawn from Kentucky 2025-00113 could not
    link back to the Commission's own copy of the PDF -- the one thing a
    regulatory reader wants from a citation: not our text, theirs.

    The URL goes through safe_source_url, the same check the screens use, so a
    provenance file naming a `javascript:` address never reaches the database
    rather than being caught later by a template. One check, at the edge.

    source_registration_id is not here and is not an oversight. These 102
    filings were pulled by hand with curl before any source registry existed, so
    there is no registration to point at, and NULL says exactly that.
    """
    return {
        "source_url": safe_source_url(meta.get("source_url")),
        "filer": meta.get("filer") or None,
        "filing_date": meta.get("filing_date") or None,
        "source_retrieved_at": _instant(meta.get("retrieved_at")),
    }


def attach_provenance(session, wanted: dict[str, dict]) -> list[str]:
    """Write the provenance columns onto versions already stored. Idempotent.

    Returns the version ids it changed, so a re-run that changes nothing says
    nothing and writes no audit row. The log records what the system decided,
    not how many times it was asked.

    The read goes through versions_for_company -- the tenant chokepoint -- and
    not through a query written here. A second scoped read of the same table is
    a second thing to get wrong.
    """
    changed: list[str] = []
    for version in versions_for_company(session, COMPANY):
        columns = wanted.get(version.id)
        if columns is None:
            continue
        if all(getattr(version, name) == value for name, value in columns.items()):
            continue
        for name, value in columns.items():
            setattr(version, name, value)
        changed.append(version.id)

        # Only what is actually known. A reason reading "filed None by None"
        # puts a fabricated-looking value in a table that cannot be edited
        # afterwards.
        known = ", ".join(
            f"{name} {value}" for name, value in columns.items() if value is not None
        )
        record_event(
            session,
            company_id=COMPANY,
            actor=ACTOR,
            action=ACTION_INGEST,
            subject_type="document_version",
            subject_id=version.id,
            reason=f"recorded where this version came from: {known or 'nothing'}",
        )
    return changed


def anchor(spec: dict, changes: list[Change]) -> Change | None:
    """The first change the citation lands in, or nothing.

    A citation can cover several passages and the diff reports one change per
    passage, so the claim attaches to the earliest text a reader would look at.
    Deterministic, so a re-run attaches it to the same change.
    """
    covering = [
        row
        for row in changes
        if row.to_version_id == spec["version_id"]
        and row.after_start is not None
        and row.after_start < spec["end"]
        and spec["start"] < row.after_end
    ]
    if not covering:
        return None
    return min(covering, key=lambda row: (row.after_start, row.id))


def attach_claim(
    session, spec: dict, text: str, changes: list[Change]
) -> str | None:
    """Write the hand-written claim onto the change its citation lands in.

    Returns the change id, or None when it refused to write. Two things make it
    refuse, and both are announced rather than swallowed: the corpus no longer
    carrying the quoted characters at those offsets, and no change covering
    them. Either means the stored offsets address different words than the ones
    this claim was written about, and a claim pointing at the wrong bytes is
    worse than no claim -- it is the exact failure the verifier exists to catch,
    laid down by hand.
    """
    if text[spec["start"] : spec["end"]] != spec["quote"]:
        print(
            f"    REFUSED to write {spec['id']}: {spec['version_id']} no longer "
            f"reads {spec['quote']!r} at {spec['start']}-{spec['end']}."
        )
        return None

    change = anchor(spec, changes)
    if change is None:
        print(
            f"    REFUSED to write {spec['id']}: no change covers "
            f"{spec['start']}-{spec['end']} in {spec['version_id']}."
        )
        return None

    stored = session.get(Claim, spec["id"])
    if stored is not None:
        # The stored row wins, and its change is what gets reported. A re-run
        # after the diff shifted would otherwise report on the change this call
        # computed while the claim still hangs off the one it was written to.
        return stored.change_id

    session.add(
        Claim(
            id=spec["id"],
            company_id=COMPANY,
            change_id=change.id,
            statement=spec["statement"],
            citation_version_id=spec["version_id"],
            citation_start=spec["start"],
            citation_end=spec["end"],
            citation_quote=spec["quote"],
            cited_occurrence=spec["occurrence"],
            confidence_bp=CONFIDENCE_BP,
        )
    )
    session.flush()
    return change.id


def report_verdict(session, claim_id: str, change_id: str) -> None:
    """Say what the verifier makes of the claim, now, against the stored bytes.

    Printed rather than assumed. A claim written by hand is a claim whose
    citation somebody could have typed wrong, and the product's whole answer to
    that is to re-read the source instead of trusting the person -- so the
    script that wrote it is the last place to take it on trust.
    """
    good, held = verified_claims(session, COMPANY, change_id)
    for claim in good:
        if claim.claim_id == claim_id:
            print(f"  {claim_id} asserts, cited in {change_id}")
            print(f"    source: {claim.filing.link_text or claim.filing.note}")
            return
    for claim in held:
        if claim.claim_id == claim_id:
            print(f"  {claim_id} WITHHELD as {claim.reason_code}: {claim.reason_text}")
            return
    print(f"  {claim_id} is not on {change_id} at all; nothing was written")


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
            wanted: dict[str, dict] = {}
            texts: dict[str, str] = {}
            produced: list = []
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

                texts[version_id] = text
                produced.extend(changes)

                columns = provenance_columns(meta)
                wanted[version_id] = columns
                # A dropped field is announced here rather than left as a NULL
                # somebody discovers on a screen six weeks later.
                if meta.get("source_url") and columns["source_url"] is None:
                    print(
                        f"    REFUSED the recorded address for {stem}: it is not "
                        "an http or https address. The version keeps no link."
                    )
                if meta.get("retrieved_at") and columns["source_retrieved_at"] is None:
                    print(
                        f"    DROPPED retrieved_at for {stem}: "
                        f"{meta['retrieved_at']!r} is not an instant with a zone."
                    )

                previous = version_id

            for version_id in attach_provenance(session, wanted):
                print(f"  provenance recorded on {version_id}")

            for spec in CLAIMS:
                if spec["version_id"] not in texts:
                    continue
                change_id = attach_claim(
                    session, spec, texts[spec["version_id"]], produced
                )
                if change_id is not None:
                    report_verdict(session, spec["id"], change_id)


if __name__ == "__main__":
    main()
