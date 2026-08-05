#!/usr/bin/env python3
"""Give the three empty demo projects findings, drawn from three real filing pairs.

WHY THIS EXISTS. app/seed.py builds the workspace around one docket, so all
eight of its findings land on PRJ-MPUC-2026-0142. PRJ-1, PRJ-2 and PRJ-3 -- the
three projects the company context names -- had none, and a project with no
finding renders "No finding has been recorded on this project yet." A reviewer
who clicks any project but the first meets an empty product. The features work;
they had nothing to work on.

WHAT IT LOADS, AND WHY EACH PAIR. Three public dockets, each already in
data/real/ with a provenance record beside it naming the commission, the filer,
the filing date and the address the PDF came from.

  PRJ-1  Riverbend Data Center Campus Interconnection
         GA PSC 55378, Georgia Power's Q3 2025 quarterly large load economic
         development report against the March 2026 corrected refiling. A
         140 MW data centre campus is the same animal the report counts, and
         the errata is a filer telling the Commission that its own redaction
         process disturbed a formula in the public copy.

  PRJ-2  Multi-State Load Forecast Modernization
         UT PSC 24-035-04, Mark Ellis's Phase III direct testimony against the
         errata filed eleven days later. The subject of the testimony is
         self-insurance, not load forecasting, and the finding is not about the
         subject: the errata renumbers the witness from OCS-2D to OCS-6D, which
         moves a running head on thirty pages and renumbers two exhibits that
         also swap places. PRJ-2 exists to replace a spreadsheet-driven filing
         process that files the same numbers in two states, and this is that
         failure happening at a commission, with a diff that buries four real
         edits under thirty repetitions of a page header.

  PRJ-3  Emergency Curtailment Program Buildout
         MO PSC ET-2025-0184, the Non-Unanimous Global Stipulation for Ameren
         Missouri's Schedule LLCS against the corrected reissue two days later.
         The correction attaches Exhibit A, the initial pricing table the
         original left out, and a curtailment credit has to be priced against
         the demand and energy charges of the schedule the customer is served
         under.

THE RULE THAT MAKES THIS HONEST. Not one quote below is typed. Every claim names
a passage in words; this script finds that passage in the stored bytes, takes
its real offsets, and slices the quote out of the file. verify_citation re-reads
the source at those offsets on every render, so a typed quote would show up
withheld in front of the reviewer. The one deliberate misquote is derived the
same way and then altered by a named substitution, so even the failure is built
from the real text rather than invented -- the same construction app/seed.py
uses for CLM-MISQUOTE.

AND EVERY CLAIM CITES CHANGED TEXT. The anchor is worked out, not asserted: the
citation's offsets must fall inside a change the diff actually recorded, on the
side matching the version the claim names. Where no change covers them the claim
is refused and said so, because a claim attached to a change it does not quote
is the sloppiness the verifier exists to catch, laid down by hand. Analyst
reasoning about text that did not change belongs in a finding's detail, which is
prose a person signed, not an assertion the product makes.

TWO OF THE FOURTEEN ARE MEANT TO FAIL, and they fail differently.

  CLM-GA-55378-COMMITMENTS  quotes the Q3 report's commitment sentence with
                            "26 customers" where the filing says 28. The
                            offsets are inside the document and the words at
                            them are not the document's.
  CLM-UT-2403504-RUNNINGHEAD  quotes the running head, which matches the source
                            exactly and appears on thirty pages, and names no
                            occurrence.

A coverage bar reading 100 per cent on every project looks staged and teaches a
reviewer nothing about what the refusal does. Two is the whole budget: a demo
that mostly refuses is not a demo of a product that mostly works.

WHERE IT SITS IN THE SEED CHAIN. After ingest_real, before seed_demo_gaps and
build_index. It needs the projects app.seed creates, and it writes versions,
passages and changes -- so it must run ahead of both readers, for the reason the
Makefile already gives about them: a pass that reads what every writer wrote has
to run after the last writer, or `make seed` gives a different answer the second
time.

Idempotent throughout: every write checks first, so running it twice changes
nothing and says so.

Run:  .venv/bin/python scripts/seed_real_findings.py
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_env  # noqa: E402

load_env()

from app.pipeline import ACTOR, ingest_and_diff  # noqa: E402

# Imported, not copied. The escalation row's shape, its id and the audit entry
# beside it are decided in one place; a second spelling here would drift and the
# escalations screen would show two vocabularies for one refusal.
from app.seed import _escalate  # noqa: E402
from app.state.claims import safe_source_url, verified_claims  # noqa: E402
from app.state.db import session_scope  # noqa: E402
from app.state.models import (  # noqa: E402
    Claim,
    Finding,
    Proceeding,
    Source,
)
from app.state.projects import attach_change  # noqa: E402
from app.state.review import KIND_EXTERNAL, coverage_for_project  # noqa: E402
from app.verification.verifier import (  # noqa: E402
    Citation,
    occurrence_count,
    occurrence_index,
)

REAL = ROOT / "data" / "real"
COMPANY = "MEP"

#: Flat, and the same number app/seed.py and scripts/ingest_real.py use. Nothing
#: here is a probability: a person wrote every sentence below and the citation
#: either matches the stored bytes or it does not.
CONFIDENCE_BP = 10000

#: The three pairs, one per empty project. Each stem is a file in data/real/
#: with a .provenance.json beside it; the version id is the stem, which is what
#: scripts/ingest_real.py already does, so a reader can go from a citation on a
#: screen to the file and to the record of where it was fetched from.
PROCEEDINGS = (
    {
        "id": "GA-PSC-55378",
        "docket": "GA PSC 55378",
        "subject": "Quarterly large load economic development reports",
        "commission": "Georgia Public Service Commission",
        "project_id": "PRJ-1",
        "versions": (
            "ga-55378-large-load-econ-dev-report-q3-2025-pd",
            "ga-55378-large-load-econ-dev-report-q3-q4-2025-revised-pd",
        ),
    },
    {
        "id": "UT-PSC-24-035-04",
        "docket": "UT PSC 24-035-04",
        "subject": (
            "Phase III direct testimony and errata, Rocky Mountain Power rate case"
        ),
        "commission": "Public Service Commission of Utah",
        "project_id": "PRJ-2",
        "versions": (
            "ut-24-035-04-ellis-phase3-direct-testimony-original",
            "ut-24-035-04-ellis-phase3-direct-testimony-errata-clean",
        ),
    },
    {
        "id": "MO-PSC-ET-2025-0184",
        "docket": "MO PSC ET-2025-0184",
        "subject": "Schedule LLCS large load tariff stipulation",
        "commission": "Missouri Public Service Commission",
        "project_id": "PRJ-3",
        "versions": (
            "mo-ET-2025-0184-nonunanimous-stipulation-original",
            "mo-ET-2025-0184-nonunanimous-stipulation-corrected",
        ),
    },
)

#: Fourteen findings and the claim under each, in the order they were raised.
#:
#: `locate` is words, not offsets. The script joins them with \s+ and searches
#: the stored text, so the citation's offsets and its quote both come out of the
#: file. A locator that matches twice, or not at all, refuses the claim rather
#: than guessing which passage was meant -- the filings are PDF text, and a
#: passage that moved is exactly the case where a guess would be confident and
#: wrong.
#:
#: `alter` is present on one claim and is the deliberate misquote: a pair of
#: words substituted into the real slice after it is read. `omit_occurrence`
#: is present on one other and withholds the occurrence a repeated quote needs.
#: Both are named here rather than buried, because a fixture that fails on
#: purpose and does not say so is indistinguishable from a bug.
FINDINGS = (
    # -- PRJ-1, Georgia 55378 --------------------------------------------
    {
        "finding_id": "FND-GA-55378-DOCKET",
        "claim_id": "CLM-GA-55378-DOCKET",
        "project_id": "PRJ-1",
        "version": "ga-55378-large-load-econ-dev-report-q3-q4-2025-revised-pd",
        "locate": "Docket No. 55378",
        "statement": "The corrected filing is captioned under Docket No. 55378.",
        "headline": (
            "Corrected quarterly reports are captioned under a different docket"
        ),
        "detail": (
            "The Q3 report was filed under Docket No. 56002; the March 2026 "
            "refiling that corrects it is captioned 55378. Anyone watching one "
            "docket number for this report series sees only half of it. MEP "
            "files its own large load reports into a docket per state, so a "
            "watch built on a docket number and not on a document series has "
            "the same hole."
        ),
        "raised_by": "Sarah Lindqvist",
        "status": "confirmed",
        "days_ago": 12,
    },
    {
        "finding_id": "FND-GA-55378-REDACTION",
        "claim_id": "CLM-GA-55378-REDACTION",
        "project_id": "PRJ-1",
        "version": "ga-55378-large-load-econ-dev-report-q3-q4-2025-revised-pd",
        "locate": (
            "The error resulted from a formula being inadvertently disturbed by "
            "the Company’s redaction process used to create the public "
            "disclosure version."
        ),
        "statement": (
            "The corrected filing states that the error came from a formula "
            "disturbed by the redaction process used to make the public "
            "disclosure version."
        ),
        "headline": (
            "A redaction step broke a formula in the public copy of two reports"
        ),
        "detail": (
            "The filer redacts a spreadsheet to make the public copy, and the "
            "redaction moved a formula rather than a value. Two quarters went "
            "out with the wrong number before anyone noticed. MEP builds its "
            "own public and confidential copies of the large load report the "
            "same way, out of one workbook, and nothing in that process re-adds "
            "the totals after redaction."
        ),
        "raised_by": "Sarah Lindqvist",
        "status": "confirmed",
        "days_ago": 11,
    },
    {
        "finding_id": "FND-GA-55378-TRADESECRET",
        "claim_id": "CLM-GA-55378-TRADESECRET",
        "project_id": "PRJ-1",
        "version": "ga-55378-large-load-econ-dev-report-q3-q4-2025-revised-pd",
        "locate": (
            "The attachments to the trade secret versions of these Quarterly "
            "Reports were not impacted by the error and, therefore, are not "
            "being refiled."
        ),
        "statement": (
            "The corrected filing states that the trade secret attachments were "
            "not affected and are not being refiled."
        ),
        "headline": (
            "Only the public copy was refiled, so the two copies now differ in date"
        ),
        "detail": (
            "The confidential attachment on the record is the one filed in "
            "November; the public one is the one filed in March. They are meant "
            "to carry the same numbers and they no longer carry the same filing "
            "date, so a reader reconciling the two has to know which correction "
            "applies to which copy. Nothing in the docket says so on the "
            "confidential side."
        ),
        "raised_by": "Priya Nandakumar",
        "status": "confirmed",
        "days_ago": 10,
    },
    {
        "finding_id": "FND-GA-55378-PIPELINE",
        "claim_id": "CLM-GA-55378-PIPELINE",
        "project_id": "PRJ-1",
        "version": "ga-55378-large-load-econ-dev-report-q3-2025-pd",
        "locate": (
            "the total pipeline of economic development projects through the "
            "mid-2030s has decreased by 5,500 MW to 53,500 MW"
        ),
        "statement": (
            "The Q3 2025 report states that the total economic development "
            "pipeline through the mid-2030s decreased by 5,500 MW to 53,500 MW."
        ),
        "headline": (
            "The refiling carries the tables only; the totals stay in the "
            "superseded report"
        ),
        "detail": (
            "The corrected filing is attachments and a cover letter. The "
            "narrative that reads the tables -- pipeline totals, commitments, "
            "how much load has broken ground -- was not refiled, so the only "
            "copy of those numbers sits in the document the correction "
            "supersedes. Pulling the newest document in this docket gets the "
            "tables and none of the reading."
        ),
        "raised_by": "Denise Okoro",
        "status": "confirmed",
        "days_ago": 9,
    },
    {
        "finding_id": "FND-GA-55378-COMMITMENTS",
        "claim_id": "CLM-GA-55378-COMMITMENTS",
        "project_id": "PRJ-1",
        "version": "ga-55378-large-load-econ-dev-report-q3-2025-pd",
        "locate": (
            "has grown by 2,200 MW, reaching a total of 11,000 MW across 28 "
            "customers"
        ),
        # THE DELIBERATE MISQUOTE. The customer count is moved back one quarter,
        # which is the mistake an analyst actually makes: the previous report
        # said 26, and the sentence was updated everywhere except here.
        "alter": ("28 customers", "26 customers"),
        "statement": (
            "The Q3 2025 report states that commitments grew by 2,200 MW to "
            "11,000 MW across 26 customers."
        ),
        "headline": (
            "Commitment restatement cites a customer count the report does not carry"
        ),
        "detail": (
            "The offsets are inside the Q3 report and the quote at them is not "
            "what the report says: it reads 26 customers where the filing says "
            "28. Nothing is asserted from this finding, and the sentence is not "
            "shown anywhere it could be mistaken for the record."
        ),
        "raised_by": ACTOR,
        "status": "open",
        "days_ago": 8,
    },
    # -- PRJ-2, Utah 24-035-04 -------------------------------------------
    {
        "finding_id": "FND-UT-2403504-CAPTION",
        "claim_id": "CLM-UT-2403504-CAPTION",
        "project_id": "PRJ-2",
        "version": "ut-24-035-04-ellis-phase3-direct-testimony-errata-clean",
        "locate": "Ellis Phase III OCS – 6D",
        "statement": (
            "The corrected testimony is captioned Ellis Phase III OCS – 6D."
        ),
        "headline": (
            "Errata renumbers the witness, and the number is in the page header"
        ),
        "detail": (
            "The witness was OCS-2D and is now OCS-6D. Thirty-seven changes "
            "were recorded between the two filings and thirty of them are the "
            "same running head repeating. Four are real. This is what MEP's "
            "two-state filing process produces every time a document is "
            "reissued, and it is the case PRJ-2 has to make readable: a diff "
            "nobody can read is a diff nobody reads."
        ),
        "raised_by": "Alicia Ferreira",
        "status": "confirmed",
        "days_ago": 14,
    },
    {
        "finding_id": "FND-UT-2403504-EXHIBITS",
        "claim_id": "CLM-UT-2403504-EXHIBITS",
        "project_id": "PRJ-2",
        "version": "ut-24-035-04-ellis-phase3-direct-testimony-errata-clean",
        "locate": (
            "as Exhibit 6.1D, a statement of 43 qualifications containing "
            "additional details about my background."
        ),
        "statement": (
            "The corrected testimony attaches the statement of qualifications "
            "as Exhibit 6.1D."
        ),
        "headline": "The two exhibits changed number and swapped places",
        "detail": (
            "In the original the statement of qualifications was Exhibit 2.2D "
            "and the white paper was 2.1D. In the errata the statement is 6.1D "
            "and the white paper is 6.2D, so the suffix swapped as well as the "
            "prefix. A brief that cited OCS 2.1D now names a number that no "
            "longer exists, and the number nearest to it names the other "
            "document. A renumbering that only changed the prefix would be "
            "safe; this one is not."
        ),
        "raised_by": "Alicia Ferreira",
        "status": "confirmed",
        "days_ago": 13,
    },
    {
        "finding_id": "FND-UT-2403504-WHITEPAPER",
        "claim_id": "CLM-UT-2403504-WHITEPAPER",
        "project_id": "PRJ-2",
        "version": "ut-24-035-04-ellis-phase3-direct-testimony-errata-clean",
        "locate": (
            "as Exhibit 6.2D, a white paper 123 commissioned by the Utah Office "
            "of Consumer Services"
        ),
        "statement": (
            "The corrected testimony attaches the white paper as Exhibit 6.2D."
        ),
        "headline": "The white paper now carries the suffix the statement used to have",
        "detail": (
            "The other half of the swap, cited on its own so both halves rest "
            "on the document rather than on one citation and a sentence about "
            "it. Read together the two findings say the whole thing: the "
            "exhibits did not merely move from 2 to 6."
        ),
        "raised_by": "Marcus Whitfield",
        "status": "confirmed",
        "days_ago": 12,
    },
    {
        "finding_id": "FND-UT-2403504-HYPHEN",
        "claim_id": "CLM-UT-2403504-HYPHEN",
        "project_id": "PRJ-2",
        "version": "ut-24-035-04-ellis-phase3-direct-testimony-errata-clean",
        "locate": (
            "Catastrophic risk is characterized by infrequent but extreme events "
            "that"
        ),
        "statement": (
            "The corrected testimony describes catastrophic risk as infrequent "
            "but extreme events."
        ),
        "headline": "A hyphen came out of a defining phrase and nothing else moved",
        "detail": (
            "\"infrequent-but-extreme\" became \"infrequent but extreme\". "
            "Recorded and rejected. A materiality step that raises this raises "
            "everything, and a review centre where nothing is ever rejected "
            "shows nobody reading."
        ),
        "raised_by": "Marcus Whitfield",
        "status": "rejected",
        "days_ago": 11,
    },
    {
        "finding_id": "FND-UT-2403504-RUNNINGHEAD",
        "claim_id": "CLM-UT-2403504-RUNNINGHEAD",
        "project_id": "PRJ-2",
        "version": "ut-24-035-04-ellis-phase3-direct-testimony-errata-clean",
        "locate": "Phase III OCS-6D Ellis",
        # THE DELIBERATE AMBIGUITY. The words match the source exactly; they
        # match it on thirty pages. Which page a citation means is part of what
        # it means, so the verifier refuses it rather than picking one. The
        # count is written down so that a corpus which stopped repeating this
        # line refuses here rather than turning the fixture green.
        "repeats": 30,
        "nth": 0,
        "omit_occurrence": True,
        "statement": (
            "The corrected testimony carries the witness number OCS-6D in its "
            "running head."
        ),
        "headline": "Running-head citation does not say which page it means",
        "detail": (
            "The words match the source exactly at these offsets, and the same "
            "running head appears on thirty pages of the testimony. The "
            "citation names no occurrence, so it could mean any of them, and a "
            "citation that could mean any of thirty is not a citation."
        ),
        "raised_by": ACTOR,
        "status": "open",
        "days_ago": 10,
    },
    # -- PRJ-3, Missouri ET-2025-0184 ------------------------------------
    {
        "finding_id": "FND-MO-ET20250184-EXHIBIT",
        "claim_id": "CLM-MO-ET20250184-EXHIBIT",
        "project_id": "PRJ-3",
        "version": "mo-ET-2025-0184-nonunanimous-stipulation-corrected",
        "locate": "EXHIBIT A Schedule LLCS Initial Pricing",
        "statement": (
            "The corrected stipulation attaches Exhibit A, the Schedule LLCS "
            "Initial Pricing table."
        ),
        "headline": (
            "The corrected filing attaches the exhibit its own text points at "
            "three times"
        ),
        "detail": (
            "The original agreement refers to Exhibit A in three places and "
            "stops at page 27, where the certificate of service ends. The "
            "corrected reissue adds page 28 and the exhibit. For two days the "
            "signed agreement on the docket pointed at a table nobody outside "
            "the signatories could read."
        ),
        "raised_by": "Tom Baptiste",
        "status": "confirmed",
        "days_ago": 7,
    },
    {
        "finding_id": "FND-MO-ET20250184-PRICING",
        "claim_id": "CLM-MO-ET20250184-PRICING",
        "project_id": "PRJ-3",
        "version": "mo-ET-2025-0184-nonunanimous-stipulation-corrected",
        "locate": (
            "Charges Summer Non-Summer Customer $412.66 $412.66 Low-Income "
            "Pilot $291.99 $291.99 Demand ($/kW) $22.43 $10.66 Energy ($/kWh) "
            "$0.0406 $0.0371 Reactive ($/kvar) $0.4481 $0.4481"
        ),
        "statement": (
            "The corrected stipulation prices Schedule LLCS at a demand charge "
            "of $22.43 per kW in summer and $10.66 non-summer, and an energy "
            "charge of $0.0406 per kWh in summer and $0.0371 non-summer."
        ),
        "headline": (
            "The rates a curtailment credit would be priced against are now public"
        ),
        "detail": (
            "A credit for curtailed load is worth what the customer would "
            "otherwise have paid, so it is priced off the demand and energy "
            "charges of the schedule the customer takes service under. Until "
            "this refiling the public record of Missouri's large load schedule "
            "carried no rates at all. Note what the charges do NOT do: "
            "paragraph 15 sets a minimum monthly demand at 80 per cent of "
            "contract capacity, so curtailment does not take a bill below that "
            "floor, and a compensation method that ignores the floor "
            "over-credits every event."
        ),
        "raised_by": "Tom Baptiste",
        "status": "confirmed",
        "days_ago": 6,
    },
    {
        "finding_id": "FND-MO-ET20250184-SERVICE",
        "claim_id": "CLM-MO-ET20250184-SERVICE",
        "project_id": "PRJ-3",
        "version": "mo-ET-2025-0184-nonunanimous-stipulation-corrected",
        "locate": "via electronic mail (e-mail) on this 9th day of November, 2025.",
        "statement": (
            "The corrected stipulation certifies service on counsel for all "
            "parties on the 9th day of November, 2025."
        ),
        "headline": (
            "Service on the record moved two days, and response dates count from "
            "service"
        ),
        "detail": (
            "The original certifies service on the 7th, the corrected reissue "
            "on the 9th. Any date an analyst counted off the original is two "
            "days early, which is the safe direction here and the unsafe one "
            "when a correction moves the other way."
        ),
        "raised_by": "Alicia Ferreira",
        "status": "confirmed",
        "days_ago": 5,
    },
    {
        "finding_id": "FND-MO-ET20250184-CAPTION",
        "claim_id": "CLM-MO-ET20250184-CAPTION",
        "project_id": "PRJ-3",
        "version": "mo-ET-2025-0184-nonunanimous-stipulation-corrected",
        "locate": "CORRECTED NON-UNANIMOUS GLOBAL STIPULATION AND AGREEMENT",
        "statement": (
            "The corrected agreement is captioned CORRECTED NON-UNANIMOUS "
            "GLOBAL STIPULATION AND AGREEMENT."
        ),
        "headline": "Two agreements with near-identical captions now sit on the docket",
        "detail": (
            "One word apart, two days apart, and both signed. A citation to "
            "\"the Non-Unanimous Global Stipulation and Agreement\" in this "
            "docket no longer names one document, and the one it reads as by "
            "default is the one without the pricing table."
        ),
        "raised_by": "Sarah Lindqvist",
        "status": "confirmed",
        "days_ago": 4,
    },
)


# ---------------------------------------------------------------------------
# Reading the corpus
# ---------------------------------------------------------------------------


def provenance(stem: str) -> dict:
    return json.loads((REAL / f"{stem}.provenance.json").read_text(encoding="utf-8"))


def source_text(stem: str) -> str:
    """The bytes as ingest will store them. Read once, the same way, everywhere.

    errors="replace" matches scripts/ingest_real.py. It matters that this is the
    same call: an offset computed against a differently decoded string addresses
    different characters, and nothing downstream would say so.
    """
    return (REAL / f"{stem}.txt").read_text(encoding="utf-8", errors="replace")


def locate(words: str, text: str) -> list[tuple[int, int]]:
    """Every span whose text is these words, whitespace ignored.

    The corpus is PDF text: pdfminer and PyMuPDF pad columns with runs of
    spaces, break sentences across form feeds, and put a testimony's line
    numbers in the middle of a sentence. Matching the words with \\s+ between
    them finds the passage without anyone having to retype its spacing, and the
    slice taken afterwards is the file's own bytes, padding and all.

    Every span, not the first. How many there are is a fact about the document
    the caller has to reckon with rather than a detail to hide: a locator that
    was meant to name one passage and names four has not identified anything,
    and one that names thirty is the repeated-boilerplate trap this product
    exists to refuse. write_claim() checks the count against what the spec says
    it expects.
    """
    pattern = re.compile(r"\s+".join(re.escape(word) for word in words.split()))
    return [(match.start(), match.end()) for match in pattern.finditer(text)]


def anchor(changes: list, version_id: str, start: int, end: int):
    """The first recorded change whose span overlaps this citation. None if none.

    Both sides are looked at. A claim citing the newer version is matched
    against after_start/after_end; a claim citing the older one -- which is how
    a finding about text the correction DROPPED has to be written -- is matched
    against before_start/before_end. The side is chosen by which version the
    claim names, so a change cannot be matched on the wrong document's offsets.

    Deterministic: earliest span, then id. A re-run attaches the claim to the
    same change.
    """
    covering = []
    for row in changes:
        if row.to_version_id == version_id:
            span = (row.after_start, row.after_end)
        elif row.from_version_id == version_id:
            span = (row.before_start, row.before_end)
        else:
            continue
        if span[0] is None or span[1] is None:
            continue
        if span[0] < end and start < span[1]:
            covering.append((span[0], row.id, row))
    if not covering:
        return None
    return min(covering, key=lambda item: (item[0], item[1]))[2]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def ensure_proceeding(session, spec: dict) -> bool:
    """Open the proceeding if it is not already there. True when it created one."""
    existing = (
        session.query(Proceeding)
        .filter(Proceeding.company_id == COMPANY, Proceeding.id == spec["id"])
        .one_or_none()
    )
    if existing is not None:
        return False
    session.add(
        Proceeding(
            id=spec["id"],
            company_id=COMPANY,
            docket=spec["docket"],
            subject=spec["subject"],
            commission=spec["commission"],
        )
    )
    session.flush()
    return True


def ensure_source(session, spec: dict, stem: str, meta: dict) -> bool:
    """List the filing as an external source on the project. True when written.

    Coverage counts internal and external sources separately, and a project
    whose every finding cites a commission's filing must not report zero
    external sources. The locator is the address the provenance record carries,
    put through the same check the screens use, so a filing whose recorded
    address is not http or https is listed by file path rather than by a link
    nothing should follow.
    """
    source_id = f"SRC-{stem}"
    if session.get(Source, source_id) is not None:
        return False
    url = safe_source_url(meta.get("source_url"))
    session.add(
        Source(
            id=source_id,
            company_id=COMPANY,
            project_id=spec["project_id"],
            kind=KIND_EXTERNAL,
            label=(meta.get("document_title") or stem)[:256],
            locator=(url or f"data/real/{stem}.txt")[:512],
            version_id=stem,
            retrieved_at=datetime.now(timezone.utc),
            # A filing served by the commission that received it. Trusting the
            # source still says nothing about whether a quote taken from it
            # matches the bytes; that is the verifier's job and it runs anyway.
            trusted=True,
        )
    )
    return True


def write_claim(session, spec: dict, text: str, changes: list) -> str | None:
    """Write one claim from the stored bytes, or refuse and say why.

    Returns the change id it attached to, or None. Three things make it refuse,
    and each means the offsets would address words this claim was not written
    about:

      the locator finds a different number of passages than the spec expects;
      the substitution that builds the deliberate misquote no longer bites,
        which would turn a fixture that must fail into one that quietly passes;
      no recorded change covers the span.

    A stored claim wins and its own change is reported, so a re-run after the
    diff shifted does not report on a change the claim is not attached to.
    """
    stored = session.get(Claim, spec["claim_id"])
    if stored is not None:
        return stored.change_id

    # How many passages this locator should find, stated by the spec rather than
    # discovered. One, except on the claim written against a running head, where
    # the whole point is that there are thirty. Stating it turns a corpus that
    # moved into a refusal here instead of a citation quietly landing on a
    # different page.
    expected = spec.get("repeats", 1)
    found = locate(spec["locate"], text)
    if len(found) != expected:
        print(
            f"    REFUSED {spec['claim_id']}: {spec['version']} carries "
            f"{len(found)} passage(s) reading {spec['locate'][:50]!r}, and this "
            f"claim was written against {expected}."
        )
        return None
    start, end = found[spec.get("nth", 0)]

    # The quote is the file's own bytes at the offsets just found. This is the
    # line the whole script exists for: nothing below ever types a quote.
    quote = text[start:end]

    alter = spec.get("alter")
    if alter is not None:
        pattern = r"\s+".join(re.escape(word) for word in alter[0].split())
        altered = re.sub(pattern, alter[1], quote)
        if altered == quote:
            print(
                f"    REFUSED {spec['claim_id']}: the substitution {alter[0]!r} "
                f"-> {alter[1]!r} changes nothing, so this claim would verify "
                "and the demonstration would show a refusal that never happens."
            )
            return None
        quote = altered

    # Which occurrence, computed from the bytes rather than assumed. NULL where
    # the quote is unique, because the verifier only demands an occurrence when
    # there is a choice to make -- and deliberately NULL on the one claim that
    # is meant to be refused for not making it.
    occurrence = None
    if not spec.get("omit_occurrence") and occurrence_count(quote, text) > 1:
        found = occurrence_index(
            Citation(spec["version"], start, end, quote), text
        )
        occurrence = None if found < 0 else found

    change = anchor(changes, spec["version"], start, end)
    if change is None:
        print(
            f"    REFUSED {spec['claim_id']}: no recorded change covers "
            f"{start}-{end} in {spec['version']}."
        )
        return None

    session.add(
        Claim(
            id=spec["claim_id"],
            company_id=COMPANY,
            change_id=change.id,
            statement=spec["statement"],
            citation_version_id=spec["version"],
            citation_start=start,
            citation_end=end,
            citation_quote=quote,
            cited_occurrence=occurrence,
            confidence_bp=CONFIDENCE_BP,
        )
    )
    session.flush()
    return change.id


def write_finding(session, spec: dict, change_id: str, now: datetime) -> bool:
    """Record the finding over a claim that was written. True when it wrote one.

    Never written without its claim. A finding naming a claim id that does not
    exist would count against coverage while looking like it counts for it, and
    the row would point at nothing a reviewer could open.
    """
    if session.get(Finding, spec["finding_id"]) is not None:
        return False
    session.add(
        Finding(
            id=spec["finding_id"],
            company_id=COMPANY,
            project_id=spec["project_id"],
            change_id=change_id,
            claim_id=spec["claim_id"],
            headline=spec["headline"][:512],
            detail=spec["detail"],
            raised_by=spec["raised_by"],
            raised_at=now - timedelta(days=spec["days_ago"]),
            status=spec["status"],
        )
    )
    session.flush()
    return True


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def load_real_findings(session) -> dict[str, int]:
    """Ingest the three pairs and lay the findings on their projects.

    Returns a count per project of the findings this call wrote, so a second run
    returns zeros and a caller can tell "already there" from "written now"
    without counting rows itself.
    """
    now = datetime.now(timezone.utc)
    written: dict[str, int] = {}
    texts: dict[str, str] = {}
    changes: list = []

    for spec in PROCEEDINGS:
        if ensure_proceeding(session, spec):
            print(f"proceeding {spec['id']} created")
        else:
            print(f"proceeding {spec['id']} already present")

        previous = None
        for stem in spec["versions"]:
            meta = provenance(stem)
            text = source_text(stem)
            produced = ingest_and_diff(
                session,
                company_id=COMPANY,
                proceeding_id=spec["id"],
                version_id=stem,
                label=(meta.get("document_title") or stem)[:200],
                status="FINAL",
                source_text=text,
                previous_version_id=previous,
            )
            print(
                f"  {stem[:58]:60} {len(text):>7,} chars  "
                f"filed {meta.get('filing_date', '?')}  {len(produced)} change(s)"
            )
            texts[stem] = text
            changes.extend(produced)
            if ensure_source(session, spec, stem, meta):
                print(f"    listed as an external source on {spec['project_id']}")
            previous = stem

        written[spec["project_id"]] = 0

    touched: list[str] = []
    for spec in FINDINGS:
        text = texts.get(spec["version"])
        if text is None:
            print(f"    REFUSED {spec['claim_id']}: {spec['version']} was not loaded.")
            continue

        change_id = write_claim(session, spec, text, changes)
        if change_id is None:
            # No claim, no finding. Said once, above, in the refusal.
            continue
        if change_id not in touched:
            touched.append(change_id)

        if write_finding(session, spec, change_id, now):
            written[spec["project_id"]] = written.get(spec["project_id"], 0) + 1

        attach_change(session, COMPANY, spec["project_id"], change_id, ACTOR)

    # The verdict on every claim this script wrote, read back through the same
    # path the screens use. Printed rather than assumed: a claim written by hand
    # is a claim somebody could have got wrong, and the product's whole answer
    # to that is to re-read the bytes instead of trusting the person.
    asserted = 0
    for change_id in touched:
        good, held = verified_claims(session, COMPANY, change_id)
        for claim in good:
            if any(claim.claim_id == spec["claim_id"] for spec in FINDINGS):
                asserted += 1
        for claim in held:
            if any(claim.claim_id == spec["claim_id"] for spec in FINDINGS):
                _escalate(session, COMPANY, claim)
                print(f"  WITHHELD {claim.claim_id}: {claim.reason_code}")

    print(f"  {asserted} claim(s) verify against the stored bytes right now")
    return written


def main() -> int:
    with session_scope() as session:
        written = load_real_findings(session)
        for project_id in sorted(written):
            coverage = coverage_for_project(session, COMPANY, project_id)
            print(
                f"{project_id}: {coverage.findings_total} finding(s) -- "
                f"{coverage.findings_verified} included, "
                f"{coverage.findings_withheld} withheld "
                f"({written[project_id]} written this run)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
