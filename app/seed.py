"""Load data/ end to end. Idempotent: safe to run on every `make run`.

What this does, in order: creates the company's proceeding, ingests the three
versions through the pipeline chokepoint, persists the diff between them, writes
one claim per labelled change in the manifest, writes two claims that are
supposed to fail, then asks the product which of them may be asserted and
escalates the rest.

**The statements are built by code, not by a model.** There is no model call
anywhere in this repository, so the claim sentences here are assembled from the
manifest by the small extractors below: a date, a percentage, a section label, a
measure carried over unchanged. Each rule is deterministic and tested. What that
buys is a working demo with no API key and no network; what it does not buy is
materiality judgement, which is the one thing a model would add and the one
thing that is out of scope. This module is a fixture standing in for the
interpretation stage that architecture.html places between diff and state. When
that stage exists, these sentences come from it and this file loads data only.

**The two failures are deliberate and are the point.** A demo where everything
verifies proves nothing about ADR-003. So the seed writes a claim whose quote was
altered at real offsets -- the failure a model produces fluently -- and a claim
citing a sentence that appears three times without saying which one it means.
Both are written the same way as the others and are refused at read time by the
same code that the workspace runs. Nothing marks them as the planted failures;
they fail because the source does not support them.

**Idempotency is real, not a truncate.** Running twice writes nothing the second
time: the pipeline reuses versions and changes by derived id, and every claim and
escalation is keyed by a stable id and skipped when present. An escalation is
never rewritten once created, because a person may have resolved it and the
resolution is the part worth keeping.

What is NOT loaded: the obligations, projects and documents in
data/company_context.json. Nothing in app/state/models.py stores them yet --
impact mapping is designed and not built (docs/.ai/state.json) -- and inventing
a half-table here would make the gap harder to see, not smaller. The file is
read for the company's name and id, which every row is scoped by.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.pipeline import ingest_and_diff
from app.state.audit import record_event
from app.state.claims import (
    WithheldClaim,
    changes_for_proceeding,
    claims_for_change,
    escalations_for_company,
    verified_claims,
)
from app.state.db import get_engine, session_scope
from app.state.models import Base, Change, Claim, Escalation, Proceeding
from app.state.queries import versions_for_company

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

ACTOR = "system:seed"
ACTION_WITHHOLD = "withhold_claim"

# Deterministic assembly, so confidence is not a probability from anything. It
# is flat because nothing here is uncertain: the sentence was built by a rule
# and the citation either matches the stored bytes or it does not. The field
# exists for the day a model fills it, and the two planted failures carry the
# same number so that what withholds them is plainly the evidence and not a
# score (ADR-006).
SEED_CONFIDENCE_BP = 10000

CLAIM_MISQUOTE = "CLM-MISQUOTE"
CLAIM_AMBIGUOUS = "CLM-AMBIGUOUS"

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
# "March 1, 2027". A compliance date, the token an analyst most fears missing.
_DATE = re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}},\s+\d{{4}}\b")
# "100%", "fifty percent (50%)", "not less than fifty percent (50%)".
_SHARE = re.compile(
    r"\b(?:not less than\s+|at least\s+)?(?:[a-z]+\s+percent\s+)?\(?\d{1,3}%\)?"
)
# "20 megawatts (MW)". A quantity with its unit spelled out and abbreviated.
_MEASURE = re.compile(r"\b\d+\s+[a-z]+\s+\([A-Z]{1,4}\)")
# The leading section number of a passage: "6", "5.4.1", "SECTION 6.".
_LABEL = re.compile(r"^\s*(?:SECTION\s+)?(\d+(?:\.\d+)*)")
# "not less than five (5) years".
_RETENTION = re.compile(r"not less than [a-z]+ \(\d+\) years")


@dataclass(frozen=True, slots=True)
class SeedReport:
    """What is in the database after the run. Totals, not "rows I just wrote".

    Totals are what makes idempotency checkable: run the seed twice, compare two
    reports, and any duplication shows up as a number that moved.
    """

    company_id: str
    company_name: str
    proceeding_id: str
    versions: int
    changes: int
    claims: int
    escalations: int
    withheld: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Reading the corpus
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict:
    return json.loads(path.read_bytes().decode("utf-8"))


def _read_source(path: Path) -> str:
    """Read as bytes and decode, never in text mode.

    The manifest's offsets were measured this way. Text mode translates newlines
    on some platforms, which would move every offset after the first line break
    and silently break every citation in the corpus.
    """
    return path.read_bytes().decode("utf-8")


def _plain(value: str) -> str:
    """Strip the corpus's own disclaimer suffix: "MPUC-2026-0142 -- invented"."""
    return value.split(" -- ")[0].strip()


# ---------------------------------------------------------------------------
# Building a claim sentence from a labelled change, by rule
# ---------------------------------------------------------------------------


def _first(matches: list[str], what: str, text: str) -> str:
    """The first match, or a refusal naming what was missing.

    A loader that shrugs and substitutes a blank writes a sentence with a hole
    in it and stores it as a claim. Failing here is loud, early and one line to
    read; the alternative is a plausible sentence nobody can trace.
    """
    if not matches:
        raise ValueError(
            f"expected {what} in {text[:60]!r}; the corpus and this loader disagree"
        )
    return matches[0]


def _section_ref(section_field: str) -> str:
    """Turn "5.2-5.3 (Cost Allocation ...)" into "Section 5.2-5.3"."""
    head = section_field.split(" (")[0].strip()
    if head.lower().startswith("section "):
        head = head[len("section "):].strip()
    return f"Section {head}"


def _subject(section_field: str) -> str:
    """The thing the section is about, as a noun phrase for the middle of a line.

    "7.1 (Compliance Timeline and Reporting -- Updated Load Forecasts)" gives
    "updated load forecast": the tail after the dash, lowercased, and the last
    word made singular so it reads as one obligation rather than a heading.
    """
    inner = section_field[section_field.find("(") + 1: section_field.rfind(")")]
    tail = inner.split(" -- ")[-1].strip().lower()
    words = tail.split()
    if words and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]
    return " ".join(words)


def _label_of(text: str) -> str | None:
    match = _LABEL.match(text)
    return match.group(1) if match else None


def _heading_of(text: str) -> str:
    """The section's own name, from its first line, without number or full stop."""
    line = text.splitlines()[0].strip()
    label = _label_of(line)
    if label:
        line = line[line.find(label) + len(label):]
    return line.strip(" .")


def statement_for(change: dict, version_labels: dict[str, str]) -> str:
    """One plain sentence about one labelled change, chosen by rule.

    The rules run in order and the first that fires wins, so the sentence names
    the sharpest thing that moved: a date before a percentage, a percentage
    before a renumbering, and a bare "this changed" only when no rule found
    anything specific. Saying less is the correct failure here -- a sentence
    that invents a detail is exactly what this product exists to prevent.

    A limit worth stating: a "moved from X to Y" sentence carries one citation,
    and that citation is the Y side -- the text now in force. The X side is
    proved by the change's own before offsets, which the change screen renders
    beside it. The verifier checks that the quote is really there; it does not
    check that the passage supports the sentence (docs/security.html).
    """
    before = change["before"]["exact_text"]
    after = change["after"]["exact_text"]
    to_label = version_labels[change["after"]["version"]]

    before_dates, after_dates = _DATE.findall(before or ""), _DATE.findall(after)
    if before_dates and after_dates and before_dates != after_dates:
        return (
            f"The compliance date for the {_subject(change['section'])} moved "
            f"from {before_dates[0]} to {after_dates[0]}."
        )

    before_shares, after_shares = _SHARE.findall(before or ""), _SHARE.findall(after)
    if before_shares and after_shares and before_shares != after_shares:
        return (
            f"The share in {_section_ref(change['section'])} moved from "
            f"{before_shares[0]} to {after_shares[0]}."
        )

    if before is None:
        return (
            f"{_section_ref(change['section'])} appears for the first time in "
            f"the {to_label}."
        )

    before_label, after_label = _label_of(before), _label_of(after)
    if before_label and after_label and before_label != after_label:
        return (
            f"The {_heading_of(after)} section moved from Section {before_label} "
            f"to Section {after_label}."
        )

    held = [token for token in _MEASURE.findall(after) if token in before]
    if held:
        return (
            f"The wording of {_section_ref(change['section'])} changed and the "
            f"{held[0]} threshold did not."
        )

    from_label = version_labels[change["before"]["version"]]
    return (
        f"{_section_ref(change['section'])} changed between the {from_label} "
        f"and the {to_label}."
    )


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _anchor(rows: list[Change], version_id: str, start: int, end: int) -> Change:
    """The first change this citation lands in.

    A citation can span several passages -- the cost-allocation edit covers two
    -- and the diff reports one change per passage. The claim attaches to the
    first change its citation covers, which is the earliest text a reader would
    look at. Deterministic, so a re-run attaches it to the same change.
    """
    covering = [
        row
        for row in rows
        if row.to_version_id == version_id
        and row.after_start is not None
        and row.after_start < end
        and start < row.after_end
    ]
    if not covering:
        raise ValueError(
            f"no change in {version_id} covers offsets {start}:{end}; "
            "the corpus and the manifest disagree about where a change is"
        )
    return min(covering, key=lambda row: (row.after_start, row.id))


def _add_claim(
    session: Session,
    *,
    claim_id: str,
    company_id: str,
    change: Change,
    statement: str,
    version_id: str,
    start: int,
    end: int,
    quote: str,
    occurrence: int | None = None,
) -> None:
    """Write one claim, or leave the stored one alone. Never a second copy."""
    if session.get(Claim, claim_id) is not None:
        return
    session.add(
        Claim(
            id=claim_id,
            company_id=company_id,
            change_id=change.id,
            statement=statement,
            citation_version_id=version_id,
            citation_start=start,
            citation_end=end,
            citation_quote=quote,
            cited_occurrence=occurrence,
            confidence_bp=SEED_CONFIDENCE_BP,
        )
    )
    session.flush()


def _escalate(session: Session, company_id: str, held: WithheldClaim) -> None:
    """Raise one escalation for a claim the product refused to assert.

    The reason text is the verifier's own words, unedited, and the reason code is
    the one app/state/claims.py branched on. Rewording it here would give the
    analyst a second vocabulary to learn and a second thing that can disagree
    with the code.

    Nothing is rewritten once written. A stored escalation may already carry a
    person's resolution, and reopening it on the next `make run` would erase
    somebody's work.
    """
    escalation_id = f"ESC-{held.claim_id}"
    if session.get(Escalation, escalation_id) is not None:
        return

    citation = (
        f"{held.citation_version_id}:{held.citation_start}:{held.citation_end}"
    )
    detail = f"quoted {held.citation_quote!r}"
    if held.source_excerpt:
        detail += f"; the source at {citation} reads {held.source_excerpt!r}"
    else:
        detail += f"; nothing could be read at {citation}"

    session.add(
        Escalation(
            id=escalation_id,
            company_id=company_id,
            claim_id=held.claim_id,
            reason_code=held.reason_code,
            reason_text=held.reason_text,
            detail=detail,
        )
    )
    session.flush()

    # An escalation row carries no timestamp of its own. The audit log is where
    # "when did we refuse this, and why" is answered, and a refusal is a
    # decision like any other.
    record_event(
        session,
        company_id=company_id,
        actor=ACTOR,
        action=ACTION_WITHHOLD,
        subject_type="claim",
        subject_id=held.claim_id,
        reason=f"{held.reason_code}: {held.reason_text}",
        citation=citation,
    )


def ensure_tables(engine=None) -> None:
    """Create any missing tables. Never drops.

    Deliberately not app.state.db.init_db(), which drops first. A loader that
    quietly emptied the database on every start would make idempotency
    unfalsifiable -- everything would look stable because nothing survived.
    """
    Base.metadata.create_all(engine or get_engine())


def load(session: Session, *, data_dir: Path = DATA_DIR) -> SeedReport:
    """Load the corpus into an open session. Returns the totals afterwards."""
    manifest = _read_json(data_dir / "manifest.json")
    context = _read_json(data_dir / "company_context.json")

    company_id = context["company"]["short_name"]
    company_name = context["company"]["name"]
    docket = _plain(manifest["docket"]["docket_number"])
    proceeding_id = docket

    if session.get(Proceeding, proceeding_id) is None:
        session.add(
            Proceeding(
                id=proceeding_id,
                company_id=company_id,
                docket=docket,
                commission=_plain(manifest["docket"]["commission"]),
                subject=manifest["docket"]["subject"],
            )
        )
        session.flush()

    sources = {
        version["id"]: _read_source(data_dir / version["file"])
        for version in manifest["versions"]
    }
    version_labels = {v["id"]: v["label"] for v in manifest["versions"]}

    rows: list[Change] = []
    previous: str | None = None
    for version in manifest["versions"]:
        rows += ingest_and_diff(
            session,
            company_id=company_id,
            proceeding_id=proceeding_id,
            version_id=version["id"],
            label=version["label"],
            status=version["status"],
            source_text=sources[version["id"]],
            previous_version_id=previous,
        )
        previous = version["id"]

    touched: list[str] = []

    # One claim per labelled change, cited at the manifest's own offsets. These
    # verify, and the test suite asserts that they do -- if the corpus is edited
    # without the manifest, this is where it shows.
    for change in manifest["changes"]:
        after = change["after"]
        anchor = _anchor(rows, after["version"], after["start"], after["end"])
        _add_claim(
            session,
            claim_id=f"CLM-{change['id']}",
            company_id=company_id,
            change=anchor,
            statement=statement_for(change, version_labels),
            version_id=after["version"],
            start=after["start"],
            end=after["end"],
            quote=after["exact_text"],
        )
        touched.append(anchor.id)

    # Failure one: real offsets, invented words. The quote was altered in place
    # so the span is still the right length and still inside the document --
    # everything about this citation looks correct except what it says.
    definition = next(c for c in manifest["changes"] if c["id"] == "CHG-2")["after"]
    fabricated = definition["exact_text"].replace("20 megawatts", "10 megawatts")
    anchor = _anchor(
        rows, definition["version"], definition["start"], definition["end"]
    )
    _add_claim(
        session,
        claim_id=CLAIM_MISQUOTE,
        company_id=company_id,
        change=anchor,
        statement=(
            "The Large Load Customer threshold moved to "
            f"{_first(_MEASURE.findall(fabricated), 'a load threshold', fabricated)}."
        ),
        version_id=definition["version"],
        start=definition["start"],
        end=definition["end"],
        quote=fabricated,
    )
    touched.append(anchor.id)

    # Failure two: a sentence that appears three times in the version, cited
    # without saying which one. The words match the source exactly at these
    # offsets, so text equality alone would pass it. The claim is about study
    # records; the offsets point at the general recordkeeping section. Which
    # occurrence a citation means is part of what it means.
    boilerplate = manifest["repeated_boilerplate"]
    occurrence = next(
        o
        for o in boilerplate["occurrences"]
        if o["version"] == "v3" and o["section"].startswith("6.3")
    )
    anchor = _anchor(
        rows, occurrence["version"], occurrence["start"], occurrence["end"]
    )
    _add_claim(
        session,
        claim_id=CLAIM_AMBIGUOUS,
        company_id=company_id,
        change=anchor,
        statement=(
            "The Utility must keep study records for "
            + _first(
                _RETENTION.findall(boilerplate["sentence"]),
                "a retention period",
                boilerplate["sentence"],
            )
            + "."
        ),
        version_id=occurrence["version"],
        start=occurrence["start"],
        end=occurrence["end"],
        quote=boilerplate["sentence"],
        occurrence=None,
    )
    touched.append(anchor.id)

    # Ask the product, rather than deciding here, which claims may be asserted.
    # The escalations then say exactly what the workspace will withhold, because
    # the same function produced both.
    withheld: list[tuple[str, str]] = []
    for change_id in sorted(set(touched)):
        _verified, held_back = verified_claims(session, company_id, change_id)
        for held in held_back:
            _escalate(session, company_id, held)
            withheld.append((held.claim_id, held.reason_code))

    persisted = changes_for_proceeding(session, company_id, proceeding_id)
    return SeedReport(
        company_id=company_id,
        company_name=company_name,
        proceeding_id=proceeding_id,
        versions=len(versions_for_company(session, company_id)),
        changes=len(persisted),
        claims=sum(
            len(claims_for_change(session, company_id, row.id)) for row in persisted
        ),
        escalations=len(escalations_for_company(session, company_id)),
        withheld=tuple(sorted(withheld)),
    )


def main() -> None:
    ensure_tables()
    with session_scope() as session:
        report = load(session)

    print(f"seed: company {report.company_id} ({report.company_name})")
    print(
        f"seed: docket {report.proceeding_id} -- {report.versions} versions, "
        f"{report.changes} changes"
    )
    print(f"seed: {report.claims} claims, {report.escalations} escalated")
    for claim_id, reason_code in report.withheld:
        print(f"seed:   withheld {claim_id} -- {reason_code}")
    print("seed: run it again and these numbers do not move.")


if __name__ == "__main__":
    main()
