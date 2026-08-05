"""The three internal projects, and the real filings their findings rest on.

WHAT THIS SUITE IS AGAINST. app/seed.py puts all eight of its findings on the
docket project, so PRJ-1, PRJ-2 and PRJ-3 rendered "No finding has been recorded
on this project yet." Every feature on those screens worked; there was nothing
for them to work on, and no test asked. That is the same gap
scripts/seed_demo_gaps.py was written to close, one screen along: capability was
tested, content was not.

FOUR THINGS IT PINS, and each has a cheap wrong version that looks right.

1. Each of the three projects has findings at all. A coverage map cannot be read
   from an empty project, and an empty project is what the product shipped.

2. Every claim MEANT to verify does, through verified_claims -- the path the
   screens use, which re-reads the stored bytes at the cited offsets. Twelve
   quotes were lifted out of PDF text by a script; a single off-by-one in the
   locator would leave a citation pointing at the wrong words and the demo would
   show a wall of refusals.

3. The two claims meant to be withheld are withheld FOR THE REASON INTENDED, not
   merely absent from the verified list. A misquote that fails as an ambiguous
   occurrence, or as an unreadable source, is a different bug wearing the right
   answer's clothes.

4. Nothing was typed. Every citation's quote is compared against the file in
   data/real/ read fresh, at the offsets stored, so a hand-written quote that
   happened to verify against the database would still fail here.

Everything runs offline. No model call, no key, no network.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from seed_real_findings import (  # noqa: E402
    COMPANY,
    FINDINGS,
    PROCEEDINGS,
    load_real_findings,
    source_text,
)

from app.seed import load  # noqa: E402
from app.state.claims import verified_claims  # noqa: E402
from app.state.db import init_db, session_scope  # noqa: E402
from app.state.models import Claim, Escalation, Finding, Source  # noqa: E402
from app.state.review import coverage_for_project, findings_for_project  # noqa: E402
from app.verification.verifier import (  # noqa: E402
    REASON_AMBIGUOUS_OCCURRENCE,
    REASON_QUOTE_MISMATCH,
    Citation,
    verify_citation,
)

PROJECTS = ("PRJ-1", "PRJ-2", "PRJ-3")

#: The two the script builds to fail, and the reason each must fail for. Written
#: here rather than read from the script, so a spec that stopped carrying its
#: `alter` or its `omit_occurrence` fails this file instead of quietly turning
#: the demonstration into one where nothing is ever refused.
INTENDED_REFUSALS = {
    "CLM-GA-55378-COMMITMENTS": REASON_QUOTE_MISMATCH,
    "CLM-UT-2403504-RUNNINGHEAD": REASON_AMBIGUOUS_OCCURRENCE,
}


def _build():
    """A fresh database with the synthetic seed and then the real findings."""
    init_db()
    with session_scope() as session:
        load(session)
    with session_scope() as session:
        return load_real_findings(session)


# --------------------------------------------------------------------------
# The projects are no longer empty
# --------------------------------------------------------------------------


def test_every_internal_project_carries_findings():
    _build()
    with session_scope() as session:
        for project_id in PROJECTS:
            coverage = coverage_for_project(session, COMPANY, project_id)
            assert coverage.findings_total > 0, (
                f"{project_id} has no finding, so its coverage map renders the "
                "empty state and the reviewer sees a hollow product"
            )


def test_each_project_has_at_least_one_verified_finding():
    """A project whose findings are all withheld is as unreadable as an empty one.

    The two numbers are checked separately on purpose. findings_total > 0 alone
    would pass on a project where every citation failed, and that page says
    nothing about what the product can do.
    """
    _build()
    with session_scope() as session:
        for project_id in PROJECTS:
            coverage = coverage_for_project(session, COMPANY, project_id)
            assert coverage.findings_verified > 0, project_id
            assert (
                coverage.findings_verified + coverage.findings_withheld
                == coverage.findings_total
            )


def test_the_refusals_are_spread_and_rationed():
    """One or two withheld across the three, and not all on one project.

    A demonstration where every project reads 100 per cent verified teaches a
    reviewer nothing about the refusal. One where half the findings are withheld
    reads as a product that does not work. This pins the middle.
    """
    _build()
    with session_scope() as session:
        withheld = {
            project_id: coverage_for_project(
                session, COMPANY, project_id
            ).findings_withheld
            for project_id in PROJECTS
        }
    assert sum(withheld.values()) == len(INTENDED_REFUSALS), withheld
    assert max(withheld.values()) <= 1, withheld


def test_every_project_lists_the_filings_it_rests_on():
    """Coverage counts external sources, and zero of them beside a real docket lies."""
    _build()
    with session_scope() as session:
        for project_id in PROJECTS:
            coverage = coverage_for_project(session, COMPANY, project_id)
            assert coverage.sources_external >= 2, project_id


# --------------------------------------------------------------------------
# What verifies, verifies
# --------------------------------------------------------------------------


def _verdicts(session) -> dict[str, tuple[bool, str | None]]:
    """Every seeded claim's verdict, taken through the path the screens take."""
    wanted = {spec["claim_id"] for spec in FINDINGS}
    seen: dict[str, tuple[bool, str | None]] = {}
    for finding in (
        row
        for project_id in PROJECTS
        for row in findings_for_project(session, COMPANY, project_id)
    ):
        if finding.claim_id not in wanted:
            continue
        good, held = verified_claims(session, COMPANY, finding.change_id)
        for claim in good:
            if claim.claim_id in wanted:
                seen[claim.claim_id] = (True, None)
        for claim in held:
            if claim.claim_id in wanted:
                seen[claim.claim_id] = (False, claim.reason_text)
    return seen


def test_every_claim_meant_to_verify_does():
    _build()
    with session_scope() as session:
        verdicts = _verdicts(session)

    expected = {spec["claim_id"] for spec in FINDINGS}
    assert set(verdicts) == expected, "a seeded claim reached no verdict at all"

    for claim_id, (verified, reason) in sorted(verdicts.items()):
        if claim_id in INTENDED_REFUSALS:
            continue
        assert verified, f"{claim_id} was meant to assert and did not: {reason}"


def test_the_two_refusals_fail_for_the_reason_intended():
    """Withheld is not enough. The reason code is the thing the screen teaches."""
    _build()
    with session_scope() as session:
        verdicts = _verdicts(session)

    for claim_id, reason_text in INTENDED_REFUSALS.items():
        verified, actual = verdicts[claim_id]
        assert not verified, f"{claim_id} was meant to be refused and asserted"
        assert actual == reason_text, f"{claim_id} refused for the wrong reason"


def test_each_refusal_raised_an_escalation():
    """A refusal nobody can find is a refusal that did not happen."""
    _build()
    with session_scope() as session:
        for claim_id in INTENDED_REFUSALS:
            row = session.get(Escalation, f"ESC-{claim_id}")
            assert row is not None, claim_id
            assert row.claim_id == claim_id


# --------------------------------------------------------------------------
# Nothing here was typed
# --------------------------------------------------------------------------


def test_every_verifying_quote_is_the_file_read_fresh():
    """The stored quote equals the bytes of data/real/ at the stored offsets.

    This is the rule the whole seeding script exists to keep, checked against
    the file rather than against the database. A quote somebody typed that
    happened to match the ingested text would pass test_every_claim_meant_to
    _verify_does and fail here.
    """
    _build()
    with session_scope() as session:
        for spec in FINDINGS:
            if spec["claim_id"] in INTENDED_REFUSALS:
                continue
            claim = session.get(Claim, spec["claim_id"])
            assert claim is not None, spec["claim_id"]
            raw = source_text(claim.citation_version_id)
            assert (
                raw[claim.citation_start : claim.citation_end] == claim.citation_quote
            ), spec["claim_id"]


def test_the_misquote_is_the_real_sentence_with_one_substitution():
    """Even the failure is built from the filing, not invented.

    The stored quote must differ from the file at those offsets -- otherwise it
    would verify -- and must be the same length in words, because a quote that
    is obviously wrong teaches nothing. The check is the substitution the spec
    names, applied to the real slice.
    """
    _build()
    spec = next(s for s in FINDINGS if s["claim_id"] == "CLM-GA-55378-COMMITMENTS")
    with session_scope() as session:
        claim = session.get(Claim, spec["claim_id"])
        raw = source_text(claim.citation_version_id)
        actual = raw[claim.citation_start : claim.citation_end]

    assert actual != claim.citation_quote
    assert spec["alter"][0].split()[-1] in actual
    assert spec["alter"][1].split()[-1] in claim.citation_quote
    assert len(actual.split()) == len(claim.citation_quote.split())


def test_the_ambiguous_quote_matches_the_source_and_names_no_occurrence():
    """It fails on which one, not on what it says. Those are different lessons."""
    _build()
    with session_scope() as session:
        claim = session.get(Claim, "CLM-UT-2403504-RUNNINGHEAD")
        raw = source_text(claim.citation_version_id)
        assert raw[claim.citation_start : claim.citation_end] == claim.citation_quote
        assert claim.cited_occurrence is None

        # Naming the occurrence is all that stands between this and a clean
        # verdict, which is what makes the refusal about ambiguity rather than
        # about the words.
        result = verify_citation(
            Citation(
                claim.citation_version_id,
                claim.citation_start,
                claim.citation_end,
                claim.citation_quote,
            ),
            raw,
            expected_occurrence=0,
        )
        assert result.verified


# --------------------------------------------------------------------------
# Running it twice
# --------------------------------------------------------------------------


def test_a_second_run_writes_nothing():
    """`make seed` runs on every `make run`, so a loader that duplicates is fatal."""
    _build()
    with session_scope() as session:
        before = {
            model.__name__: sorted(str(row.id) for row in session.query(model).all())
            for model in (Finding, Claim, Source, Escalation)
        }

    with session_scope() as session:
        written = load_real_findings(session)
    assert written == {project_id: 0 for project_id in PROJECTS}

    with session_scope() as session:
        after = {
            model.__name__: sorted(str(row.id) for row in session.query(model).all())
            for model in (Finding, Claim, Source, Escalation)
        }
    assert before == after


# --------------------------------------------------------------------------
# The corpus these findings were written against
# --------------------------------------------------------------------------


def test_every_pair_named_here_is_on_disk_with_its_provenance():
    """A missing file must fail here, not halfway through a reviewer's `make run`."""
    real = pathlib.Path(__file__).resolve().parents[1] / "data" / "real"
    for spec in PROCEEDINGS:
        assert len(spec["versions"]) == 2, spec["id"]
        for stem in spec["versions"]:
            assert (real / f"{stem}.txt").is_file(), stem
            assert (real / f"{stem}.provenance.json").is_file(), stem


def test_no_withheld_sentence_reaches_a_finding_headline():
    """The refused wording must not arrive on a page under another name.

    A headline built from a claim's statement is the obvious way an assertion
    the product refused to make gets published anyway, and it would look
    entirely normal on screen.
    """
    _build()
    refused = {
        spec["claim_id"]: spec["statement"]
        for spec in FINDINGS
        if spec["claim_id"] in INTENDED_REFUSALS
    }
    with session_scope() as session:
        rows = [
            row
            for project_id in PROJECTS
            for row in findings_for_project(session, COMPANY, project_id)
        ]
    for statement in refused.values():
        for row in rows:
            assert statement not in row.headline
            assert statement not in row.detail
