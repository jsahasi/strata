"""What a registered source must say about itself before anything fetches it.

THIS FILE HOLDS NO NETWORK CODE AND IMPORTS NOTHING FROM app.state, AND BOTH
ARE DELIBERATE. Two modules need the same answer to one question -- can this
build fetch from THIS ROW -- and they sit on opposite sides of it:

* app/state/sources.py needs it while REGISTERING, so a new row gets a status
  that is true. A public docket naming a document is never_tried. A public
  docket naming a commission's front door is not_implemented, because nothing
  here can turn a search page into a filing.
* app/sources/fetch.py needs it before OPENING A SOCKET, so a row missing half
  its configuration is refused for the reason it is missing rather than
  retrieved and then found to be unusable.

If the answer lived in fetch.py, the state layer would import the network
module to render a page; if it lived in app/state/sources.py, the fetcher would
import the writer it is called by. Either way round is an import cycle waiting
for the first person who adds a convenience import. So the contract lives here,
one level up from both, with no dependencies at all.

WHAT A FETCHABLE public_docket SAYS, AND WHY EACH FIELD IS REQUIRED RATHER THAN
DEFAULTED.

    endpoint         The address of ONE document. Not a docket search page and
                     not a commission's home page: this build retrieves bytes
                     and hashes them, and a navigation page hashed as if it
                     were an order would report a changed filing every time a
                     banner changed.
    proceeding_id    Which proceeding the retrieved text belongs to. There is
                     no way to read a document and know which docket in THIS
                     workspace it continues, and a fetcher that guessed would
                     file a Georgia order under a Kentucky docket with complete
                     confidence. Absence is denial.
    document_status  DRAFT or FINAL, decided by the person who registered the
                     source. ADR-005 puts this in a line of Python rather than
                     in anything inferred: acting on a draft wastes money on
                     something that may not survive comment, and treating a
                     final order as a draft misses a binding deadline. No model
                     and no fetcher may decide it.

THE EIGHT CORPUS ROWS FAIL THIS ON PURPOSE. register_corpus_sources() writes one
registration per commission, carrying `origins` -- the hosts the 102 filings in
data/real were fetched from -- and the docket numbers. That is a true record of
where this workspace's corpus came from and it is not a document address, so
fetch_problem() refuses it and those rows stay not_implemented. The registry
therefore shows two public dockets of the same kind with two different statuses,
which is the point: the kind says a fetcher exists, the row says whether this
one can use it.
"""

from __future__ import annotations

from app.state.models import (
    SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,
    SOURCE_REGISTRATION_KINDS,
)

#: The kinds a fetcher in this package can actually retrieve from. ONE, and it
#: is the one whose documents are published to everybody, so a mistake here
#: reaches nothing that was not already public.
#:
#: app/state/sources.py reads this rather than keeping a second list. When a
#: fetcher for another kind lands, this tuple gains an entry and the registry
#: follows without being edited.
FETCHER_KINDS: tuple[str, ...] = (SOURCE_REGISTRATION_KIND_PUBLIC_DOCKET,)

#: The config keys a fetchable registration carries. Named constants because
#: three modules and a form all spell them, and a key typed from memory in one
#: of the four produces a row that registers, renders, and then refuses to
#: fetch for a reason nobody can find.
CONFIG_ENDPOINT = "endpoint"
CONFIG_PROCEEDING = "proceeding_id"
CONFIG_DOCUMENT_STATUS = "document_status"

#: The two the pipeline accepts. app/ingestion/ingest.py raises on anything
#: else, and a person deserves the refusal before a socket opens rather than a
#: ValueError after one closed.
DOCUMENT_STATUSES = ("DRAFT", "FINAL")


def fetch_problem(kind: str, config: dict | None) -> str:
    """Why this build cannot fetch this row, or "" when it can.

    A SENTENCE, NEVER A BOOLEAN, because both callers have to say why. The
    registry prints it beside a status and the fetcher raises it as a refusal,
    and "cannot fetch" with no reason is the answer that sends an administrator
    to re-type a field that was never the problem.

    It reads the row and never the network. Whether the endpoint exists, resolves
    or answers is a different question and app/sources/fetch.py is the only
    thing that may ask it.
    """
    if kind not in SOURCE_REGISTRATION_KINDS:
        return (
            f"There is no kind of source called {kind!r} in this product, so "
            "nothing can be said about fetching from it."
        )
    if kind not in FETCHER_KINDS:
        return (
            f"No fetcher exists for a {kind} in this build. The only kind this "
            "product retrieves from is "
            + ", ".join(FETCHER_KINDS)
            + ", whose documents are published to everybody."
        )

    settings = config or {}
    missing: list[str] = []

    endpoint = str(settings.get(CONFIG_ENDPOINT) or "").strip()
    if not endpoint:
        missing.append(
            "the address of one document, as "
            f"{CONFIG_ENDPOINT}. A commission's search page is not a filing: "
            "this build retrieves bytes and compares their hash, so it has to "
            "be pointed at the document itself"
        )

    proceeding = str(settings.get(CONFIG_PROCEEDING) or "").strip()
    if not proceeding:
        missing.append(
            f"which proceeding the document belongs to, as {CONFIG_PROCEEDING}. "
            "Nothing here can read a filing and work out which docket in this "
            "workspace it continues, and guessing would file it under the wrong "
            "one with complete confidence"
        )

    status = str(settings.get(CONFIG_DOCUMENT_STATUS) or "").strip()
    if status not in DOCUMENT_STATUSES:
        missing.append(
            "whether what it fetches is a draft or a final order, as "
            f"{CONFIG_DOCUMENT_STATUS}, spelt "
            + " or ".join(DOCUMENT_STATUSES)
            + ". A person decides that, per ADR-005: acting on a draft wastes "
            "work on something that may not survive comment, and treating a "
            "final order as a draft misses a binding deadline"
        )

    if not missing:
        return ""
    return (
        "This registration does not say " + "; and it does not say ".join(missing) + "."
    )


__all__ = [
    "CONFIG_DOCUMENT_STATUS",
    "CONFIG_ENDPOINT",
    "CONFIG_PROCEEDING",
    "DOCUMENT_STATUSES",
    "FETCHER_KINDS",
    "fetch_problem",
]
