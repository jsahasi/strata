"""The coverage map: what every filing in scope says about a question, INCLUDING
the ones that say nothing.

WHY THIS SUITE EXISTS AND WHAT IT IS AGAINST. A ranked list answers "which
passage best matches these words". It cannot answer "does this hold across these
filings, and where does it not", because bm25 ranks globally: ask about
curtailment across eight filings and the top five can all come from the one
filing that mentions it most, with nothing from the other seven. The caller then
cannot tell "those seven do not mention it" from "those seven lost on rank", and
the second is the answer they were actually looking for.

THE SILENCE IS THE PRODUCT, AND IT IS THE HALF MOST LIKELY TO BE BUILT WRONG. If
seven filings address a subject and the eighth does not, the eighth is the
interesting one. A map that simply leaves it out is indistinguishable from one
where it ranked sixth. So every test here that asserts what came back also
asserts HOW MANY VERSIONS came back, and the flagship test compares that count
against the number of versions in scope. Drop a zero row silently and this file
goes red.

FOUR THINGS A ZERO MUST NEVER BE FOLDED WITH, and each has its own section:

  a version whose passages were searched and matched nothing;
  a version that holds no passages at all, so nothing was searched in it;
  a version the version cap never reached;
  a version whose matches were found and then not offered, because they carry
  the cited span of a withheld claim or because they would not verify.

All four are empty span lists. Reported as the same thing they would be a lie in
three cases out of four.

AND THE SENTENCE THE MODEL IS HANDED MUST NOT OVERCLAIM. "This version contains
no passage matching these terms" is what the index can support. "This version
does not address the subject" is a judgement no deterministic read may make -- a
filing can address curtailment for six pages without using the word. The wire
format keeps them apart and so does every note here.

Everything in this file runs offline. No model call, no key, no network.
"""

import pytest

from app.chat import agent as chat_agent
from app.chat import pills as chat_pills
from app.chat import tools as chat_tools
from app.ingestion.ingest import ingest_version
from app.state import search
from app.state.db import get_engine, init_db, session_scope
from app.state.identity import ROLE_ANALYST, create_user, ensure_system_roles, grant_role
from app.state.models import (
    Change,
    Claim,
    DocumentVersion,
    Escalation,
    Proceeding,
    User,
)
from app.verification.verifier import Citation, verify_citation

COMPANY = "MEP"
RIVAL = "RIVAL"
DOCKET = "24-M-0001"
OTHER_DOCKET = "99-Z-0009"
PASSWORD = "correct-horse-battery-staple"
ANALYST = "ana@mep.example"

# The two filings that mention curtailment ONCE EACH, in a long paragraph. Long
# on purpose: bm25 favours the short dense passage, so these are the rows a
# global cap drops first. That is the starvation this whole read exists against,
# and test_the_flat_list_cannot_answer_the_question pins that it is real here
# rather than merely argued for.
V1_SOURCE = (
    "SECTION 1. PURPOSE\n"
    "\n"
    "The commission opens this docket to consider whether the curtailment of "
    "large flexible loads served under the interruptible tariff should be "
    "addressed by rule, by contract, or by a combination of the two, and "
    "invites comment from every affected party on each of those three routes.\n"
)

# Eight short passages, every one of them about curtailment. This is the filing
# that eats a global budget of five.
V2_SOURCE = (
    "SECTION 1. PURPOSE\n"
    "\n"
    "Curtailment is addressed at length below.\n"
    "\n"
    "A curtailment event begins when the operator declares one.\n"
    "\n"
    "Curtailment orders are issued in writing.\n"
    "\n"
    "Curtailment of a large load is capped at eight hours.\n"
    "\n"
    "Curtailment credits are settled monthly.\n"
    "\n"
    "Curtailment reporting is due within ten days.\n"
    "\n"
    "Curtailment notices name the affected feeder.\n"
    "\n"
    "Curtailment records are kept for six years.\n"
)

V3_SOURCE = (
    "SECTION 1. PURPOSE\n"
    "\n"
    "The commission adopts the rule as proposed and directs that the "
    "curtailment window close at noon on the day before the forecast winter "
    "peak, with notice to every affected customer of record by electronic mail "
    "and by first class post no later than the preceding business day.\n"
)

# Says nothing about curtailment. This is the row the product exists to return.
V4_SOURCE = (
    "SECTION 2. INTERCONNECTION\n"
    "\n"
    "Staff recommends that the interconnection queue be published monthly.\n"
)

V5_SOURCE = (
    "SECTION 3. ERRATA\n"
    "\n"
    "Table 4 is corrected to read eleven megawatts.\n"
)

# A scanned exhibit whose text extraction produced nothing. It holds no
# passages, so nothing was searched in it -- which is not the same fact as
# "searched and found nothing", and the map must not say it is.
BLANK_SOURCE = ""

RIVAL_SOURCE = (
    "SECTION 1. PURPOSE\n"
    "\n"
    "RIVAL curtailment orders are issued by RIVAL in writing.\n"
)

OTHER_DOCKET_SOURCE = (
    "SECTION 1. PURPOSE\n"
    "\n"
    "Curtailment in the gas docket follows a different schedule entirely.\n"
)

#: Every version this company owns in the docket under test, in the order
#: versions_for_company returns them (by id). Written out rather than derived so
#: that a version added to the fixture and forgotten here is a failure.
SCOPE = ("mep-v1", "mep-v2", "mep-v3", "mep-v4", "mep-v5", "mep-vblank")

#: The three that mention curtailment.
MENTIONS = {"mep-v1", "mep-v2", "mep-v3"}

#: The two that were searched and matched nothing.
SILENT = {"mep-v4", "mep-v5"}


def _ingest(session, version_id, label, source_text, docket=DOCKET, company=COMPANY):
    ingest_version(
        session,
        version_id=version_id,
        company_id=company,
        docket=docket,
        label=label,
        status="FINAL",
        source_text=source_text,
    )


def _seed(session) -> str:
    ensure_system_roles(session)
    _ingest(session, "mep-v1", "Notice of proposed rulemaking", V1_SOURCE)
    _ingest(session, "mep-v2", "Revised proposed rule", V2_SOURCE)
    _ingest(session, "mep-v3", "Final order", V3_SOURCE)
    _ingest(session, "mep-v4", "Staff report", V4_SOURCE)
    _ingest(session, "mep-v5", "Errata", V5_SOURCE)
    _ingest(session, "mep-vblank", "Scanned exhibit", BLANK_SOURCE)
    _ingest(session, "mep-gas-v1", "Gas order", OTHER_DOCKET_SOURCE, docket=OTHER_DOCKET)
    _ingest(session, "rival-v1", "Rival order", RIVAL_SOURCE, company=RIVAL)
    analyst = create_user(
        session,
        COMPANY,
        email=ANALYST,
        display_name="Ana Ruiz",
        password=PASSWORD,
        actor="setup",
    )
    grant_role(
        session, COMPANY, user_id=analyst.id, role_name=ROLE_ANALYST, actor="setup"
    )
    session.flush()
    return analyst.id


@pytest.fixture
def corpus():
    """Six filings in one docket, a second docket, and another company.

    The index is dropped before and after. init_db() drops what SQLAlchemy's
    metadata knows about and an FTS5 virtual table is not in the metadata, so
    without this the index outlives the tables it points into.
    """
    init_db()
    search.drop_index(get_engine())
    with session_scope() as session:
        analyst_id = _seed(session)
        session.commit()
    search.rebuild_index(get_engine())
    yield {"analyst_id": analyst_id}
    search.drop_index(get_engine())


@pytest.fixture
def unindexed():
    """The same corpus with no index. The state a fresh checkout is in."""
    init_db()
    search.drop_index(get_engine())
    with session_scope() as session:
        analyst_id = _seed(session)
        session.commit()
    yield {"analyst_id": analyst_id}
    search.drop_index(get_engine())


WITHHELD_STATEMENT = "THE CURTAILMENT WINDOW CLOSES AT MIDNIGHT"


@pytest.fixture
def withheld(corpus):
    """A claim on the final order whose citation does not verify.

    Its cited span sits inside the only curtailment passage mep-v3 has, so the
    coverage map has to drop that span AND say why -- reporting mep-v3 as
    "nothing matched" would be false, and reporting it with the span would be the
    second door into text the product has already refused to assert.
    """
    with session_scope() as session:
        session.add(
            Proceeding(
                id="prc-mep",
                company_id=COMPANY,
                docket=DOCKET,
                commission="NYPSC",
                subject="Curtailment of large flexible loads",
            )
        )
        session.add(
            Change(
                id="chg-mep-1",
                company_id=COMPANY,
                proceeding_id="prc-mep",
                from_version_id="mep-v1",
                to_version_id="mep-v3",
                change_type="modified",
                before_start=0,
                before_end=len(V1_SOURCE),
                after_start=0,
                after_end=len(V3_SOURCE),
                section="1",
                alignment_confidence=0.94,
                materiality=None,
                status="FINAL",
            )
        )
        quote = "curtailment window close at noon"
        start = V3_SOURCE.index(quote)
        session.add(
            Claim(
                id="clm-bad",
                company_id=COMPANY,
                change_id="chg-mep-1",
                statement=WITHHELD_STATEMENT,
                citation_version_id="mep-v3",
                citation_start=start,
                citation_end=start + len(quote),
                # What the source says there is "close at noon". This says
                # midnight, so the gate refuses it.
                citation_quote="curtailment window close at midnight",
                cited_occurrence=None,
                confidence_bp=9500,
            )
        )
        session.add(
            Escalation(
                id="esc-bad",
                company_id=COMPANY,
                claim_id="clm-bad",
                reason_code="CITATION_QUOTE_MISMATCH",
                reason_text="the quoted text is not what the source says there",
                detail="quoted 'curtailment window close at midnight'",
            )
        )
        session.commit()
    yield corpus


def _map(session, query="curtailment", company=COMPANY, docket=DOCKET, **kwargs):
    return search.map_coverage(
        session, company_id=company, query=query, docket=docket, **kwargs
    )


def _by_id(coverage) -> dict:
    return {row.version_id: row for row in coverage.versions}


def _actor(session, user_id: str) -> User:
    return session.query(User).filter(User.id == user_id).one()


def _tool(session, analyst_id, **kwargs) -> dict:
    return chat_tools.coverage_map(
        session, company_id=COMPANY, actor=_actor(session, analyst_id), **kwargs
    )


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _strings(item)
    else:
        yield str(value)


# ---------------------------------------------------------------------------
# 0. Why the flat list cannot answer the question
# ---------------------------------------------------------------------------


def test_the_flat_list_cannot_answer_the_question(corpus):
    """A global cap spends its whole budget on one filing, and says nothing.

    This is the defect the coverage map exists for, measured on this fixture
    rather than argued from first principles. Five candidates is what
    app/chat/tools.py hands a model; here all five come from mep-v2, and a
    reader cannot tell whether the other filings are silent or merely outranked.
    """
    with session_scope() as session:
        flat = search.search_passages(
            session,
            company_id=COMPANY,
            query="curtailment",
            limit=chat_tools.MAX_CANDIDATE_PASSAGES,
        )
        reached = {hit.version_id for hit in flat.hits}
        assert len(flat.hits) == chat_tools.MAX_CANDIDATE_PASSAGES
        assert reached < MENTIONS, (
            "the fixture no longer reproduces the starvation this read is "
            f"against; the flat list reached {sorted(reached)}"
        )

        covered = {
            row.version_id for row in _map(session).versions if row.hits
        }
        assert covered == MENTIONS
        assert len(covered) > len(reached)


# ---------------------------------------------------------------------------
# 1. The flagship. Every version in scope comes back, silent ones included.
# ---------------------------------------------------------------------------


def test_every_version_in_scope_comes_back_even_the_silent_ones(corpus):
    """The one property the whole feature rests on.

    Two counts have to agree: the number of versions reported and the number in
    scope. A map that quietly filtered its zeroes would pass every other test in
    this file and fail this one.
    """
    with session_scope() as session:
        coverage = _map(session)

        assert coverage.in_scope == len(SCOPE)
        assert len(coverage.versions) == coverage.in_scope
        assert tuple(row.version_id for row in coverage.versions) == SCOPE

        rows = _by_id(coverage)
        for version_id in MENTIONS:
            assert rows[version_id].hits, f"{version_id} mentions curtailment"
            assert rows[version_id].reason == ""

        for version_id in SILENT:
            row = rows[version_id]
            assert row.hits == ()
            assert row.reason == search.VERSION_NO_MATCH
            assert row.note

        assert len([r for r in coverage.versions if r.hits]) == len(MENTIONS)
        assert len([r for r in coverage.versions if not r.hits]) == len(SCOPE) - len(
            MENTIONS
        )


def test_a_zero_is_present_rather_than_omitted(corpus):
    """Said twice on purpose, because the failure mode is an omission.

    A row that is absent and a row that is present with an empty span list are
    the same thing to a caller that only iterates. They are not the same thing
    to a caller that counts, and this product counts.
    """
    with session_scope() as session:
        coverage = _map(session)
        assert set(_by_id(coverage)) == set(SCOPE)
        assert all(row.note for row in coverage.versions if not row.hits)


def test_a_zero_says_it_is_about_the_words_and_not_about_the_subject(corpus):
    """The sentence handed to a model must not overclaim what the index knows.

    "No passage matches these terms" is what a deterministic read can support.
    "This filing does not address curtailment" is a judgement, and a filing can
    address curtailment for six pages without using the word.
    """
    with session_scope() as session:
        row = _by_id(_map(session))["mep-v4"]
        assert "matching" in row.note or "match" in row.note
        assert "not a statement about the subject" in row.note.lower()


# ---------------------------------------------------------------------------
# 2. Per-document caps. One verbose filing cannot spend everyone's budget.
# ---------------------------------------------------------------------------


def test_a_verbose_filing_cannot_starve_the_quiet_ones(corpus):
    """mep-v2 holds eight matching passages and still gets no more than its share."""
    with session_scope() as session:
        rows = _by_id(_map(session))
        assert len(rows["mep-v2"].hits) == search.MAX_SPANS_PER_VERSION
        assert rows["mep-v2"].more is True
        for version_id in MENTIONS - {"mep-v2"}:
            assert rows[version_id].hits, f"{version_id} was starved"
            assert rows[version_id].more is False


def test_the_span_cap_is_the_constant_that_is_written_down(corpus, monkeypatch):
    monkeypatch.setattr(search, "MAX_SPANS_PER_VERSION", 1)
    with session_scope() as session:
        rows = _by_id(_map(session))
        assert len(rows["mep-v2"].hits) == 1
        assert rows["mep-v2"].more is True


def test_the_version_cap_reports_the_rest_as_unsearched_not_as_silent(
    corpus, monkeypatch
):
    """The cap on versions must not manufacture silence.

    A version the read never looked at is not a version that said nothing, and
    the difference is the whole point. Every version in scope is still reported;
    the ones past the cap carry their own reason, and the count of them is a
    number the caller gets rather than a shortfall they have to notice.
    """
    monkeypatch.setattr(search, "MAX_VERSIONS_MAPPED", 2)
    with session_scope() as session:
        coverage = _map(session)
        assert len(coverage.versions) == len(SCOPE)
        assert coverage.in_scope == len(SCOPE)
        assert coverage.searched == 2

        unsearched = [
            row for row in coverage.versions if row.reason == search.VERSION_NOT_SEARCHED
        ]
        assert len(unsearched) == len(SCOPE) - 2
        for row in unsearched:
            assert row.hits == ()
            assert "not searched" in row.note.lower()
            # The sentence must not read as an absence of the subject.
            assert "no passage" not in row.note.lower()


# ---------------------------------------------------------------------------
# 3. A version with no passages is not a version that said nothing
# ---------------------------------------------------------------------------


def test_a_version_with_no_passages_is_not_reported_as_silence(corpus):
    """The scanned exhibit. Nothing was searched in it, so nothing may be claimed.

    Reporting it as "no passage matched" would be true of the index and false of
    the world: the filing was never read. It gets its own reason so a reader can
    act on it -- this one is a defect in the corpus, not an answer about the
    docket.
    """
    with session_scope() as session:
        row = _by_id(_map(session))["mep-vblank"]
        assert row.hits == ()
        assert row.reason == search.VERSION_NO_PASSAGES
        assert row.reason != search.VERSION_NO_MATCH
        assert "no stored passages" in row.note.lower()


# ---------------------------------------------------------------------------
# 4. Tenancy. The map is not a way round the join.
# ---------------------------------------------------------------------------


def test_the_map_never_crosses_a_company(corpus):
    with session_scope() as session:
        coverage = _map(session, docket=None)
        assert "rival-v1" not in _by_id(coverage)
        # Not a check on the row list alone: the other company's filing carries
        # the query's words, so if the join ever stopped scoping, its text would
        # arrive inside a span rather than as a row somebody would notice.
        everything = "".join(_strings(coverage))
        assert "RIVAL" not in everything
        assert "rival" not in everything

        theirs = search.map_coverage(
            session, company_id=RIVAL, query="curtailment", docket=None
        )
        assert set(_by_id(theirs)) == {"rival-v1"}


def test_an_unscoped_map_is_refused_rather_than_answered(corpus):
    with session_scope() as session:
        for value in (None, "", "   ", 0, "%", "_", " MEP"):
            with pytest.raises(ValueError):
                search.map_coverage(session, company_id=value, query="curtailment")


def test_a_docket_narrows_the_scope_and_says_how_many_it_holds(corpus):
    with session_scope() as session:
        one = _map(session, docket=OTHER_DOCKET)
        assert set(_by_id(one)) == {"mep-gas-v1"}
        assert one.in_scope == 1

        everything = _map(session, docket=None)
        assert set(_by_id(everything)) == set(SCOPE) | {"mep-gas-v1"}
        assert everything.in_scope == len(SCOPE) + 1


# ---------------------------------------------------------------------------
# 5. A degraded map still reports the zeroes
# ---------------------------------------------------------------------------


def test_a_missing_index_still_reports_every_zero(unindexed):
    """A short list with no announcement is the failure this feature is against.

    The degraded path is the one where it would happen: the scan is slower, so
    it is the path somebody would be tempted to cut short. It is not cut short,
    it carries its reason, and it still reports the silent filings.
    """
    with session_scope() as session:
        coverage = _map(session)
        assert coverage.source == search.SOURCE_SCAN
        assert coverage.reason == search.REASON_INDEX_MISSING
        assert coverage.note

        assert len(coverage.versions) == len(SCOPE)
        rows = _by_id(coverage)
        assert {v for v in SILENT if rows[v].reason == search.VERSION_NO_MATCH} == SILENT
        assert rows["mep-vblank"].reason == search.VERSION_NO_PASSAGES
        for version_id in MENTIONS:
            assert rows[version_id].hits
            assert all(hit.rank is None for hit in rows[version_id].hits)


def test_a_stale_index_still_reports_every_zero(corpus):
    with session_scope() as session:
        _ingest(session, "mep-v6", "Second errata", "SECTION 4.\n\nA later filing.\n")

    with session_scope() as session:
        coverage = _map(session)
        assert coverage.source == search.SOURCE_SCAN
        assert coverage.reason == search.REASON_INDEX_STALE
        assert len(coverage.versions) == len(SCOPE) + 1
        assert coverage.in_scope == len(SCOPE) + 1
        rows = _by_id(coverage)
        assert rows["mep-v6"].reason == search.VERSION_NO_MATCH


def test_the_two_paths_agree_on_which_filings_are_silent(unindexed):
    """The scan and the index must not disagree about who said nothing.

    They may disagree about ordering and about how many spans a filing yields.
    Disagreeing about WHICH filings are silent would mean the answer to the
    question this read exists for depends on whether somebody ran a build step.
    """
    with session_scope() as session:
        scanned = _map(session)
        assert scanned.source == search.SOURCE_SCAN
        silent_on_scan = {r.version_id for r in scanned.versions if not r.hits}

    search.rebuild_index(get_engine())
    with session_scope() as session:
        indexed = _map(session)
        assert indexed.source == search.SOURCE_INDEX
        silent_on_index = {r.version_id for r in indexed.versions if not r.hits}

    assert silent_on_index == silent_on_scan
    assert len(indexed.versions) == len(scanned.versions)


def test_a_query_with_no_searchable_words_is_not_a_map_of_zeroes(corpus):
    """"§§" holds no token. Every filing reported as silent would be a lie.

    Nothing was searched, so nothing may be said about what any filing contains.
    The rows come back -- the scope is still a fact -- carrying the reason that
    they were never searched.
    """
    with session_scope() as session:
        coverage = _map(session, query="§§")
        assert coverage.reason == search.REASON_NO_TERMS
        assert coverage.terms == ()
        assert len(coverage.versions) == len(SCOPE)
        assert all(row.reason == search.VERSION_NOT_SEARCHED for row in coverage.versions)
        assert coverage.searched == 0


# ---------------------------------------------------------------------------
# 6. The guard, checked on its own
# ---------------------------------------------------------------------------


def test_a_map_that_lost_a_row_raises_rather_than_answering():
    """The invariant that makes the rest of this file worth trusting.

    A coverage map with a row missing is not a degraded answer, it is a wrong
    one: it reads exactly like a complete map of a smaller docket. So the read
    refuses rather than returning it, and the refusal names the versions that
    went missing.
    """
    scope = ("a", "b", "c")
    rows = (
        search.VersionCoverage(
            version_id="a",
            label="A",
            status="FINAL",
            docket=DOCKET,
            hits=(),
            more=False,
            reason=search.VERSION_NO_MATCH,
            note="n",
        ),
    )
    with pytest.raises(RuntimeError) as raised:
        search.refuse_a_short_map(rows, scope)
    assert "b" in str(raised.value) and "c" in str(raised.value)


# ---------------------------------------------------------------------------
# 7. The tool layer. Every span verified, nothing withheld leaks, counts said.
# ---------------------------------------------------------------------------


def test_every_offered_span_verifies_through_the_real_verifier(corpus):
    """Not a re-implementation: verify_citation, the gate every claim goes through."""
    with session_scope() as session:
        sources = {
            row.id: row.source_text
            for row in session.query(DocumentVersion).all()
        }
        result = _tool(session, corpus["analyst_id"], query="curtailment", docket=DOCKET)
        offered = 0
        for version in result["versions"]:
            for span in version["spans"]:
                offered += 1
                verdict = verify_citation(
                    Citation(
                        version_id=span["version_id"],
                        char_start=span["start"],
                        char_end=span["end"],
                        quoted_text=sources[span["version_id"]][
                            span["start"] : span["end"]
                        ],
                    ),
                    sources[span["version_id"]],
                )
                assert verdict.verified, span
        assert offered > 0


def test_the_tool_reports_the_silent_filings_in_its_note_and_its_counts(corpus):
    """The answer to the question, in the two places a reader will look."""
    with session_scope() as session:
        result = _tool(session, corpus["analyst_id"], query="curtailment", docket=DOCKET)
        assert result["ok"] is True
        assert result["found"] == len(SCOPE), "found counts the filings reported"
        assert len(result["versions"]) == len(SCOPE)
        assert result["in_scope"] == len(SCOPE)

        # THE BUCKETS ARE EXHAUSTIVE AND THEY DO NOT OVERLAP. Every filing lands
        # in exactly one of them, and the five sum to the number reported. That
        # sum is what makes a dropped row impossible to hide in a count: lose a
        # zero and this arithmetic stops working before anybody reads the prose.
        assert result["covered"] == len(MENTIONS)
        assert result["silent"] == len(SILENT)
        assert result["no_passages"] == 1
        assert result["not_searched"] == 0
        assert result["not_offered"] == 0
        assert (
            result["covered"]
            + result["silent"]
            + result["no_passages"]
            + result["not_searched"]
            + result["not_offered"]
            == result["found"]
        )
        # The blank exhibit is NOT counted among the silent. It was never read.
        assert result["silent"] != len(SCOPE) - len(MENTIONS)
        assert "6" in result["note"] and "3" in result["note"]

        for row in result["versions"]:
            assert set(row) >= {
                "version_id",
                "label",
                "status",
                "docket",
                "spans",
                "reason",
                "note",
                "more",
                "withheld_spans",
                "unverifiable",
            }
            for span in row["spans"]:
                assert span["is_evidence"] is False


def test_a_span_carrying_a_withheld_claim_is_not_handed_over(withheld):
    """The rule tools.py already keeps, applied to the second door into the text.

    mep-v3's only curtailment passage carries the cited span of a claim the gate
    refused. The span is dropped, the count is said, and the filing is NOT
    reported as one that matched nothing -- that would be a third false
    statement built out of a true refusal.
    """
    with session_scope() as session:
        result = _tool(
            session, withheld["analyst_id"], query="curtailment", docket=DOCKET
        )
        rows = {row["version_id"]: row for row in result["versions"]}
        v3 = rows["mep-v3"]
        assert v3["spans"] == []
        assert v3["reason"] == chat_tools.VERSION_NOT_OFFERED
        assert v3["reason"] != search.VERSION_NO_MATCH
        assert v3["withheld_spans"] == 1
        assert result["retrieval"]["withheld_spans"] == 1
        assert result["withheld"] == 1

        # The counts here are one, which is the common case and the one whose
        # grammar shows the seam. A note assembled out of a number is one a
        # reader stops trusting the moment it reads as a template, and this note
        # is the sentence that explains a refusal.
        assert "1 claim on this company's record is withheld" in result["note"]
        assert "1 matching passage is not shown" in result["note"]
        assert "1 carries the cited span" in v3["note"]

        # Neither the statement nor the words its failed citation quoted.
        for text in _strings(result):
            assert WITHHELD_STATEMENT not in text
            assert "midnight" not in text.lower()


def test_the_opening_sentence_counts_filings_that_match_not_filings_that_are_shown(
    withheld,
):
    """A filing whose matches were all refused still carries matching passages.

    The opening sentence is about what the filings hold. Counting only what
    survived the citation gate put an undercount in the first line of the
    paragraph a model paraphrases, with the correction two sentences later --
    and on a one-filing docket it read "0 carry a passage matching those words"
    about a docket that plainly does.
    """
    with session_scope() as session:
        result = _tool(
            session, withheld["analyst_id"], query="curtailment", docket=DOCKET
        )
        assert result["covered"] == len(MENTIONS) - 1, "one filing's match was refused"
        assert result["not_offered"] == 1
        # Three filings carry the word; two of them are shown.
        assert f"{len(MENTIONS)} carry a passage matching those words" in result["note"]
        # Names its noun rather than pointing at it: a pronoun here can attach to
        # the clause counting the filings that carry nothing, which is the
        # opposite of what these rows are.
        assert f"1 of the {len(MENTIONS)} matching filings is not shown" in result["note"]


def test_a_row_whose_matches_were_refused_does_not_state_how_many_matched(withheld):
    """row.hits is capped, so its length is not the number that matched.

    A filing with eight matching passages arrives with two. "2 passages in this
    filing match those words" would be a precise-looking invention, contradicted
    by the `more` flag on the same row.
    """
    with session_scope() as session:
        result = _tool(
            session, withheld["analyst_id"], query="curtailment", docket=DOCKET
        )
        note = {r["version_id"]: r["note"] for r in result["versions"]}["mep-v3"]
        assert "carries passages matching those words" in note
        assert "the ones examined here" in note
        # No count of what matched, because this layer does not know one.
        assert "passages in this filing match those words" not in note
        assert "1 passage in this filing" not in note


def test_a_row_that_lost_only_some_spans_says_so_in_its_own_note(corpus):
    """The per-row note is the string a renderer is most likely to show alone.

    mep-v2 offers two spans. Withhold the first and the row still carries a
    span -- and used to carry an empty note beside a withheld_spans of 1, which
    read as though what it showed was all there was.
    """
    with session_scope() as session:
        first = _tool(session, corpus["analyst_id"], query="curtailment", docket=DOCKET)
        span = {r["version_id"]: r for r in first["versions"]}["mep-v2"]["spans"][0]

        session.add(
            Proceeding(
                id="prc-two",
                company_id=COMPANY,
                docket=DOCKET,
                commission="NYPSC",
                subject="Curtailment",
            )
        )
        session.add(
            Change(
                id="chg-two",
                company_id=COMPANY,
                proceeding_id="prc-two",
                from_version_id="mep-v1",
                to_version_id="mep-v2",
                change_type="modified",
                before_start=0,
                before_end=len(V1_SOURCE),
                after_start=0,
                after_end=len(V2_SOURCE),
                section="1",
                alignment_confidence=0.9,
                materiality=None,
                status="FINAL",
            )
        )
        session.add(
            Claim(
                id="clm-two",
                company_id=COMPANY,
                change_id="chg-two",
                statement="X",
                citation_version_id="mep-v2",
                citation_start=span["start"],
                citation_end=span["end"],
                citation_quote="something the source does not say there",
                cited_occurrence=None,
                confidence_bp=9000,
            )
        )
        session.commit()

    with session_scope() as session:
        result = _tool(session, corpus["analyst_id"], query="curtailment", docket=DOCKET)
        row = {r["version_id"]: r for r in result["versions"]}["mep-v2"]
        assert row["spans"], "the filing still shows a span"
        assert row["withheld_spans"] == 1
        assert row["reason"] == "", "it is still a covered row"
        assert "not shown" in row["note"]
        assert "withheld" in row["note"]


def test_the_map_never_reports_rows_it_handed_over_as_omitted(corpus, monkeypatch):
    """``omitted`` means rows cut from this list, and this list never cuts one."""
    monkeypatch.setattr(search, "MAX_VERSIONS_MAPPED", 2)
    with session_scope() as session:
        result = _tool(session, corpus["analyst_id"], query="curtailment", docket=DOCKET)
        assert result["not_searched"] == len(SCOPE) - 2
        assert len(result["versions"]) == len(SCOPE)
        assert result["omitted"] == 0, "every row is in the payload"

        nothing = _tool(session, corpus["analyst_id"], query="§§", docket=DOCKET)
        assert nothing["omitted"] == 0
        assert len(nothing["versions"]) == len(SCOPE)


def test_a_nul_byte_in_a_question_is_answered_rather_than_raised(corpus):
    """FTS5 parses the MATCH expression as a C string. A NUL ends it early.

    A model can write \\u0000 in a tool call. Uncaught it becomes SQLite's
    "unterminated string", and the turn handler puts the whole statement into
    the transcript. The scan path handles the same input without complaint, so
    leaving it would break the one promise this module makes about its two
    paths.
    """
    with session_scope() as session:
        result = _tool(
            session, corpus["analyst_id"], query="curt\x00ailment", docket=DOCKET
        )
        assert result["ok"] is True
        assert result["covered"] == len(MENTIONS)

        # And the same through the flat read it shares the expression with.
        flat = search.search_passages(
            session, company_id=COMPANY, query="curt\x00ailment"
        )
        assert flat.source == search.SOURCE_INDEX
        assert flat.hits


def test_tied_scores_do_not_decide_which_span_a_filing_shows(corpus):
    """bm25 ties, and a per-filing cap of two cuts through the middle of one.

    Without a second sort key, which span survives is whatever order the sorter
    yields -- and a span that is kept rather than dropped can turn a filing from
    one that shows a passage into one that shows none, if the kept one carries a
    withheld claim's citation.
    """
    with session_scope() as session:
        first = _map(session)
    for _ in range(3):
        with session_scope() as session:
            again = _map(session)
        assert [
            (row.version_id, tuple(h.passage_id for h in row.hits))
            for row in again.versions
        ] == [
            (row.version_id, tuple(h.passage_id for h in row.hits))
            for row in first.versions
        ]

    with session_scope() as session:
        rows = _by_id(_map(session))
        ranks = [hit.rank for hit in rows["mep-v2"].hits]
        ids = [hit.passage_id for hit in rows["mep-v2"].hits]
        if len(set(ranks)) == 1:
            assert ids == sorted(ids), "a tie has to be broken by passage id"


def test_the_retrieval_block_refuses_to_guess_a_count(corpus):
    """A read with no flat hit list cannot have its count guessed as zero."""
    with session_scope() as session:
        coverage = _map(session)
    with pytest.raises(ValueError) as raised:
        chat_tools._retrieval_payload(coverage)
    assert "found" in str(raised.value)


def test_the_tool_announces_a_degraded_map(unindexed):
    """A reply built on the scan says so in the transcript, not only in a log."""
    with session_scope() as session:
        result = _tool(
            session, unindexed["analyst_id"], query="curtailment", docket=DOCKET
        )
        assert result["retrieval"]["source"] == search.SOURCE_SCAN
        assert result["retrieval"]["reason"] == search.REASON_INDEX_MISSING
        assert len(result["versions"]) == len(SCOPE)
        assert result["silent"] == len(SILENT)
        assert result["covered"] == len(MENTIONS)
        assert result["no_passages"] == 1
        assert "index" in result["note"].lower()


def test_a_question_with_no_words_does_not_report_zero_filings_as_silence(corpus):
    """The counts may not be read out when no search happened.

    Every count is zero here, and "0 carry a passage matching those words" is a
    sentence about a search that did not take place. It would be the first line
    of the paragraph the model paraphrases, which is the worst place in the
    result for that blur to sit.
    """
    with session_scope() as session:
        result = _tool(session, corpus["analyst_id"], query="§§", docket=DOCKET)
        assert result["found"] == len(SCOPE), "the scope is still a fact"
        assert result["searched"] == 0
        assert result["silent"] == 0
        assert result["not_searched"] == len(SCOPE)
        assert "carry a passage matching" not in result["note"]
        assert "None of them was searched" in result["note"]
        assert result["retrieval"]["reason"] == search.REASON_NO_TERMS
        assert all(
            row["reason"] == search.VERSION_NOT_SEARCHED for row in result["versions"]
        )

        # Same for a call that supplies no query at all, which a model will make.
        empty = _tool(session, corpus["analyst_id"], docket=DOCKET)
        assert "carry a passage matching" not in empty["note"]
        assert empty["searched"] == 0


def test_the_note_reads_as_a_sentence_when_the_counts_are_one(corpus):
    """The note is handed to a model and kept in the transcript. It has to read.

    "1 filings have no stored passages" is the seam that tells a reader the
    sentence was assembled rather than written, and this is a paragraph a person
    is meant to trust.
    """
    with session_scope() as session:
        result = _tool(
            session, corpus["analyst_id"], query="curtailment", docket=OTHER_DOCKET
        )
        assert result["in_scope"] == 1
        assert "1 filing in scope" in result["note"]
        assert "1 filings" not in result["note"]
        assert "1 carries a passage" in result["note"]

        silent_one = _tool(
            session, corpus["analyst_id"], query="interconnection", docket=OTHER_DOCKET
        )
        assert "1 was searched and carries none" in silent_one["note"]


def test_a_docket_that_is_not_on_the_record_is_not_a_map_of_zeroes(corpus):
    """An empty scope is not a set of filings that said nothing."""
    with session_scope() as session:
        result = _tool(
            session, corpus["analyst_id"], query="curtailment", docket="no-such-docket"
        )
        assert result["versions"] == []
        assert result["found"] == 0
        assert result["silent"] == 0
        assert result["in_scope"] == 0
        assert "no-such-docket" in result["note"]


def test_a_blank_docket_is_a_scope_that_answers_rather_than_a_stack_trace(corpus):
    """A model fills a field it has no value for. That must not raise.

    The query layer refuses a blank docket, and rightly: a docket somebody
    COMPUTED and got blank means they meant one and found none. A blank one from
    a model means it named none, so the tool folds it to "every docket" at the
    boundary and the note says which scope answered.
    """
    with session_scope() as session:
        result = _tool(session, corpus["analyst_id"], query="curtailment", docket="  ")
        assert result["ok"] is True
        assert result["in_scope"] == len(SCOPE) + 1, "the second docket is in scope too"
        assert "this company's record" in result["note"]


def test_the_tool_refuses_an_unscoped_call(corpus):
    with session_scope() as session:
        actor = _actor(session, corpus["analyst_id"])
        with pytest.raises(ValueError):
            chat_tools.coverage_map(
                session, company_id="", actor=actor, query="curtailment"
            )


# ---------------------------------------------------------------------------
# 8. Wired. A tool with no caller is a tool that does not exist.
# ---------------------------------------------------------------------------


def test_the_tool_is_in_the_registry_and_the_toolset():
    assert "coverage_map" in chat_tools.REGISTRY
    tool = chat_tools.REGISTRY["coverage_map"]
    assert tool.writes is False
    assert tool.permission == ""
    assert tool.arguments == frozenset({"query", "docket"})
    assert "coverage_map" in chat_agent.default_toolset()


def test_the_dispatcher_runs_it_with_identity_the_model_never_supplies(corpus):
    with session_scope() as session:
        actor = _actor(session, corpus["analyst_id"])
        result = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="coverage_map",
            arguments={"query": "curtailment", "docket": DOCKET},
        )
        assert result["ok"] is True
        assert result["tool"] == "coverage_map"

        refused = chat_tools.dispatch(
            session,
            company_id=COMPANY,
            actor=actor,
            tool="coverage_map",
            arguments={"query": "curtailment", "company_id": RIVAL},
        )
        assert refused["ok"] is False
        assert refused["reason_code"] == chat_tools.REASON_IDENTITY_ARGUMENT
        assert refused["halt_turn"] is True


def test_the_map_suggests_the_question_it_exists_to_answer(corpus):
    """The silent filings are the follow-up nobody thinks to type.

    Built from a REAL tool result rather than a hand-written dict. The hand-made
    one carried spans and no reason, which is not a shape this tool can produce,
    and it hid the defect the next test pins.
    """
    with session_scope() as session:
        result = _tool(session, corpus["analyst_id"], query="curtailment", docket=DOCKET)
    pills = chat_pills.from_results(
        [
            chat_pills.Consulted(
                tool="coverage_map",
                found=result["found"],
                withheld=result["withheld"],
                result=result,
            )
        ]
    )
    assert pills, "coverage_map contributes nothing to the pill tray"
    assert any("silent" in pill.label.lower() for pill in pills)
    # The one it names has to be a filing this result calls silent.
    named = [p for p in pills if p.label.startswith("Open ")]
    assert named and named[0].label == "Open Staff report"


def test_the_pill_tray_does_not_call_the_other_three_zeroes_silence(withheld):
    """The collapse this feature exists against, reintroduced at the last step.

    A coverage map has four kinds of row with no spans and only one is silence.
    A pill that branches on an empty span list labels the filing whose matching
    passage was REFUSED for carrying a withheld claim's citation as one that
    said nothing -- on a button, in the layer a person actually reads. The
    reason code is on the same row; the tray has to read it.
    """
    with session_scope() as session:
        result = _tool(
            session, withheld["analyst_id"], query="curtailment", docket=DOCKET
        )
    rows = {row["version_id"]: row for row in result["versions"]}
    assert rows["mep-v3"]["reason"] == chat_tools.VERSION_NOT_OFFERED
    assert rows["mep-v3"]["spans"] == []

    pills = chat_pills.from_results(
        [
            chat_pills.Consulted(
                tool="coverage_map",
                found=result["found"],
                withheld=result["withheld"],
                result=result,
            )
        ]
    )
    labels = [pill.label for pill in pills]
    assert "Open Final order" not in labels, (
        "the filing whose match was withheld was offered as a silent one"
    )
    assert "Open Staff report" in labels


def test_a_map_with_nothing_searched_suggests_no_silence_at_all(corpus):
    """Nothing was searched, so no filing may be called silent, not even on a button."""
    with session_scope() as session:
        result = _tool(session, corpus["analyst_id"], query="§§", docket=DOCKET)
    pills = chat_pills.from_results(
        [chat_pills.Consulted(tool="coverage_map", found=result["found"], result=result)]
    )
    assert not any("silent" in pill.label.lower() for pill in pills)
    assert not any(pill.label.startswith("Open ") for pill in pills)
