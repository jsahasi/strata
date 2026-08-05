"""Retrieval over docket text, and the four ways it can quietly become a liability.

This suite is the argument for app/state/search.py. Each section pins one of the
failures that make a search index worse than no search index at all.

1. A CROSS-TENANT READ. An FTS5 virtual table is its own object and knows
   nothing about the join that carries tenancy in this schema. Passage has no
   company_id; it is scoped by DocumentVersion. An index queried on its own
   terms returns every company's rowids, and the product's central control is
   gone with one forgotten join. The first section holds the guard to a company
   id of None, "", 0, a SQL wildcard, a case variant, and a second company whose
   filing carries the very words being searched for.

2. A HIT WHOSE OFFSETS DO NOT VERIFY. The point of indexing Passage rather than
   an ad-hoc chunk is that a hit is already in citation shape. If retrieval ever
   hands back the text it indexed rather than the text the source holds, every
   candidate it produces fails the citation gate downstream, and the failure
   reads as a verifier bug rather than a retrieval one. The test that matters
   most in this file takes a hit out of the index and runs it through the real
   verifier against the real source.

3. AN INDEX THAT TOKENISES DIFFERENTLY FROM THE VERIFIER. Regulatory text
   arrives from PDF extraction carrying ligatures, soft hyphens, hyphenated line
   breaks and full-width digits. Fold them in the verifier and not in the index
   and the passage exists, verifies, and cannot be found -- a silent miss, which
   is the one outcome nobody can notice. Fold them in the index and not in the
   verifier and a footnote marker becomes a digit.

4. A STALE OR ABSENT INDEX ANSWERING ANYWAY. Fewer results with no announcement
   is worse here than no results at all, because the caller cannot tell "nothing
   matched" from "the index was empty". Every degraded answer in this file has
   to carry a named reason and has to be complete.

A fifth section covers the tool layer: a bm25 rank is a candidate and never
evidence, and a top-ranked passage must not carry a withheld claim into a reply.
"""

import subprocess
import sys

import pytest

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
    Passage,
    Proceeding,
    User,
)
from app.state.queries import all_passages_for_company
from app.verification.verifier import Citation, verify_citation

COMPANY = "MEP"
RIVAL = "RIVAL"
PASSWORD = "correct-horse-battery-staple"
ANALYST = "ana@mep.example"

# Written with escapes rather than the characters themselves. On the page a soft
# hyphen is invisible and a full-width digit looks like a digit, and a test whose
# input cannot be read from the source is a test nobody can maintain.
#
# Every one of these is a shape pdftotext really emits from a commission filing:
#   ﬁ   the fi ligature, from a typeset word
#   ­   a soft hyphen, the extractor's record of a line-break hint
#   ２   a full-width digit, from a document typeset for a CJK locale
#   -\n      a hyphen against a line break, from a word broken across lines
#   ²   a superscript two, which is a footnote marker and not a digit
MEP_SOURCE = (
    "SECTION 1. PURPOSE\n"
    "\n"
    "The utility shall ﬁle a distribution system implementation plan.\n"
    "\n"
    "The utility shall main­tain each feeder in good repair.\n"
    "\n"
    "Peak load is ２０ MW under the cost-\ncausation study.\n"
    "\n"
    "See § 5.4.1 of the order for the reporting schedule.\n"
    "\n"
    "Reserve margin is 20² basis points; see table 5, column 4, row 1.\n"
)

# The same words, another tenant. If scoping is wrong this is what comes back.
RIVAL_SOURCE = (
    "SECTION 1. PURPOSE\n"
    "\n"
    "RIVAL shall file a distribution system implementation plan of its own.\n"
    "\n"
    "RIVAL peak load is 20 MW and RIVAL will maintain each feeder.\n"
)


def _seed_corpus(session) -> None:
    ingest_version(
        session,
        version_id="mep-v1",
        company_id=COMPANY,
        docket="24-M-0001",
        label="Order, October",
        status="FINAL",
        source_text=MEP_SOURCE,
    )
    ingest_version(
        session,
        version_id="rival-v1",
        company_id=RIVAL,
        docket="99-X-0009",
        label="Order, October",
        status="FINAL",
        source_text=RIVAL_SOURCE,
    )
    session.flush()


@pytest.fixture
def corpus():
    """Two companies, one filing each, and a freshly built index over both.

    The index is dropped before and after. init_db() drops what SQLAlchemy's
    metadata knows about, and a virtual table is not in the metadata -- so
    without this the index outlives the tables it points into, which is exactly
    the staleness the fourth section tests deliberately.
    """
    init_db()
    search.drop_index(get_engine())
    with session_scope() as session:
        _seed_corpus(session)
    search.rebuild_index(get_engine())
    yield
    search.drop_index(get_engine())


@pytest.fixture
def unindexed():
    """The same corpus with no index at all. The state a fresh checkout is in."""
    init_db()
    search.drop_index(get_engine())
    with session_scope() as session:
        _seed_corpus(session)
    yield
    search.drop_index(get_engine())


def _texts(retrieval) -> list[str]:
    return [hit.text for hit in retrieval.hits]


def _search(session, query: str, company: str = COMPANY, **kwargs):
    return search.search_passages(
        session, company_id=company, query=query, **kwargs
    )


# ---------------------------------------------------------------------------
# 1. Tenant scoping. The index is not a way round the join.
# ---------------------------------------------------------------------------


def test_a_hit_never_crosses_a_company(corpus):
    """Both filings carry the query's words. Only one company's may come back."""
    with session_scope() as session:
        mine = _search(session, "distribution system implementation plan")
        assert mine.hits, "the owning company must find its own passage"
        assert all(hit.version_id == "mep-v1" for hit in mine.hits)
        assert not any("RIVAL" in text for text in _texts(mine))

        theirs = _search(session, "distribution system implementation plan", RIVAL)
        assert theirs.hits
        assert all(hit.version_id == "rival-v1" for hit in theirs.hits)
        assert not any("utility" in text for text in _texts(theirs))


def test_an_unscoped_search_is_refused_not_answered(corpus):
    """None, empty, an integer and a SQL wildcard must not behave as "all"."""
    with session_scope() as session:
        for value in (None, "", 0, "%", "_"):
            try:
                result = _search(session, "plan", value)
            except ValueError:
                continue
            assert result.hits == (), f"{value!r} behaved as a wildcard"


def test_a_company_id_of_the_wrong_case_is_a_different_company(corpus):
    """"mep" is not "MEP". A case-insensitive scope is a scope somebody can guess."""
    with session_scope() as session:
        assert _search(session, "plan", "mep").hits == ()
        assert _search(session, "plan", "Mep").hits == ()


def test_the_scan_fallback_is_scoped_too(unindexed):
    """The degraded path carries the same guard. A fallback that leaks is worse."""
    with session_scope() as session:
        mine = _search(session, "distribution system implementation plan")
        assert mine.source == search.SOURCE_SCAN
        assert mine.hits
        assert all(hit.version_id == "mep-v1" for hit in mine.hits)

        for value in (None, "", 0, "%"):
            try:
                result = _search(session, "plan", value)
            except ValueError:
                continue
            assert result.hits == (), f"{value!r} behaved as a wildcard on the scan"


def test_the_scoped_passage_read_refuses_an_unscoped_call():
    """The read the scan is built on, checked on its own."""
    init_db()
    with session_scope() as session:
        _seed_corpus(session)
        assert all_passages_for_company(session, COMPANY)
        for value in (None, "", 0, "%", "_", " MEP"):
            with pytest.raises(ValueError):
                all_passages_for_company(session, value)


# ---------------------------------------------------------------------------
# 2. A hit is already a citation. This is the test that matters most.
# ---------------------------------------------------------------------------


def test_every_hit_verifies_through_the_real_verifier(corpus):
    """Take a hit out of the index and cite it. The gate must pass it.

    Not a re-implementation of the check: this calls verify_citation, the same
    function every rendered claim goes through. If retrieval ever returns the
    text it indexed rather than the text the source holds, every candidate it
    produces fails here -- and in production it would fail one layer later,
    where it reads as a verifier defect rather than a retrieval one.
    """
    with session_scope() as session:
        source = (
            session.query(DocumentVersion)
            .filter(DocumentVersion.id == "mep-v1")
            .one()
            .source_text
        )
        retrieval = _search(session, "utility")
        assert retrieval.hits

        for hit in retrieval.hits:
            result = verify_citation(
                Citation(
                    version_id=hit.version_id,
                    char_start=hit.char_start,
                    char_end=hit.char_end,
                    quoted_text=hit.text,
                ),
                source,
            )
            assert result.verified, f"{hit.text!r} did not verify: {result.reason}"


def test_a_hit_found_only_because_of_folding_still_carries_raw_offsets(corpus):
    """The ligature passage is findable by "file" and still cites the raw bytes.

    This is the pair that breaks if the indexed text is ever handed back as the
    quote. The index holds "file"; the source holds the ligature. The hit has to
    carry the source's characters, or the citation minted from it is a citation
    to text nobody wrote.
    """
    with session_scope() as session:
        source = (
            session.query(DocumentVersion)
            .filter(DocumentVersion.id == "mep-v1")
            .one()
            .source_text
        )
        retrieval = _search(session, "file")
        assert retrieval.hits, "the folded form must be reachable by its plain spelling"

        hit = retrieval.hits[0]
        assert "ﬁ" in hit.text, "the hit carried the indexed text, not the source"
        assert source[hit.char_start:hit.char_end] == hit.text

        result = verify_citation(
            Citation(
                version_id=hit.version_id,
                char_start=hit.char_start,
                char_end=hit.char_end,
                quoted_text=hit.text,
            ),
            source,
        )
        assert result.verified


def test_the_scan_and_the_index_agree_on_which_passage_answers(unindexed):
    """Both paths must reach the same passage. Only the ordering differs."""
    with session_scope() as session:
        scanned = _search(session, "feeder")
        assert scanned.source == search.SOURCE_SCAN
        scanned_spans = {(h.version_id, h.char_start, h.char_end) for h in scanned.hits}

    search.rebuild_index(get_engine())
    with session_scope() as session:
        indexed = _search(session, "feeder")
        assert indexed.source == search.SOURCE_INDEX
        indexed_spans = {(h.version_id, h.char_start, h.char_end) for h in indexed.hits}

    assert indexed_spans == scanned_spans


# ---------------------------------------------------------------------------
# 3. Tokenisation. The index folds what the verifier folds, and nothing else.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        # A ligature. Raw, unicode61 keeps "file" spelled with U+FB01 as its own
        # token and a plain-ASCII query misses it entirely.
        ("file", "ﬁle"),
        # A soft hyphen. Raw, unicode61 splits "main<AD>tain" into two tokens and
        # "maintain" matches neither.
        ("maintain", "main­tain"),
        # Full-width digits. Raw, "20" and the full-width pair are different
        # tokens, so a query typed on an ASCII keyboard finds nothing.
        ("20 MW", "２０ MW"),
        # A hyphen against a line break, which normalize() closes to a bare
        # hyphen. Both spellings tokenise the same way, so this one is agreement
        # rather than repair -- it is here so a change that breaks it is caught.
        ("cost causation", "cost-\ncausation"),
    ],
)
def test_the_index_finds_what_the_verifier_would_fold(corpus, query, expected):
    """Every one of these is a silent miss if the index skips normalize()."""
    with session_scope() as session:
        retrieval = _search(session, query)
        assert retrieval.hits, f"{query!r} found nothing"
        assert any(expected in text for text in _texts(retrieval))


def test_the_index_keeps_the_distinction_the_verifier_keeps(corpus):
    """A superscript two is a footnote marker. It must not fold into a number.

    normalize() refuses to fold it, precisely so "20" beside a footnote marker
    does not become "202". An index that applied plain NFKC would join them, and
    a query for the number 202 would return a passage about 20 basis points.
    """
    with session_scope() as session:
        assert _search(session, "202").hits == ()
        assert _search(session, "20²").hits


def test_a_section_number_is_one_phrase_and_not_three_numbers(corpus):
    """"5.4.1" must not match a paragraph that merely mentions 5, 4 and 1.

    unicode61 splits on the stops, so the terms alone are three one-digit
    tokens -- which the last paragraph of the fixture happens to carry, in a
    table reference that has nothing to do with section 5.4.1.
    """
    with session_scope() as session:
        retrieval = _search(session, "5.4.1")
        assert retrieval.hits
        assert all("reporting schedule" in text for text in _texts(retrieval))
        assert not any("column 4" in text for text in _texts(retrieval))


@pytest.mark.parametrize(
    "query",
    ["NOT", "AND", "OR", "NEAR", 'a"b', "(plan", "plan*", "body:plan", "^plan", "-"],
)
def test_query_syntax_cannot_reach_the_index(corpus, query):
    """FTS5 has an expression language. None of it may be reachable from a query.

    Bare AND and NOT are syntax errors, an unbalanced quote raises, and a column
    filter would search a column nobody meant. Every one of these has to come
    back as an ordinary answer, never as an exception and never as everything.
    """
    with session_scope() as session:
        retrieval = _search(session, query)
        assert len(retrieval.hits) < 5, f"{query!r} behaved as a wildcard"


def test_a_query_with_no_searchable_words_says_so_rather_than_finding_nothing(corpus):
    """"§§§" holds no token. Returning zero hits would read as "nothing matched"."""
    with session_scope() as session:
        retrieval = _search(session, "§§§")
        assert retrieval.reason == search.REASON_NO_TERMS
        assert retrieval.terms == ()
        assert retrieval.hits == (), "nothing to rank on is not the same as everything"
        assert retrieval.note


# ---------------------------------------------------------------------------
# 4. Stale, missing, half-built. Every degraded answer names its reason.
# ---------------------------------------------------------------------------


def test_a_missing_index_announces_itself_and_still_answers_completely(unindexed):
    """No index is a normal state, not an error. It is never a silent zero."""
    with session_scope() as session:
        retrieval = _search(session, "feeder")
        assert retrieval.source == search.SOURCE_SCAN
        assert retrieval.reason == search.REASON_INDEX_MISSING
        assert retrieval.note
        assert "build_index" in retrieval.note
        assert retrieval.hits, "the fallback has to be complete, not just honest"
        assert all(hit.rank is None for hit in retrieval.hits)


def test_a_corpus_that_grew_since_the_build_is_stale_not_partly_answered(corpus):
    """The worst outcome is fewer results with no announcement. Pin against it."""
    with session_scope() as session:
        ingest_version(
            session,
            version_id="mep-v2",
            company_id=COMPANY,
            docket="24-M-0001",
            label="Order, December",
            status="FINAL",
            source_text="SECTION 2. TARIFF\n\nThe curtailment window closes at noon.\n",
        )

    with session_scope() as session:
        retrieval = _search(session, "curtailment")
        assert retrieval.source == search.SOURCE_SCAN
        assert retrieval.reason == search.REASON_INDEX_STALE
        assert retrieval.note
        assert retrieval.hits, "the new passage exists and must be found"


def test_another_companys_growth_does_not_make_my_index_stale(corpus):
    """Staleness is measured inside the scope being read, not across the estate."""
    with session_scope() as session:
        ingest_version(
            session,
            version_id="rival-v2",
            company_id=RIVAL,
            docket="99-X-0009",
            label="Order, December",
            status="FINAL",
            source_text="SECTION 2. TARIFF\n\nRIVAL curtailment closes at noon.\n",
        )

    with session_scope() as session:
        mine = _search(session, "feeder")
        assert mine.source == search.SOURCE_INDEX
        assert mine.reason == ""

        theirs = _search(session, "curtailment", RIVAL)
        assert theirs.source == search.SOURCE_SCAN
        assert theirs.reason == search.REASON_INDEX_STALE


def test_a_passage_edited_under_the_index_is_caught_by_its_fingerprint(corpus):
    """Row counts still agree, so only a per-row check can see this.

    Reachable in this repository the moment anything drops and recreates the
    passages table: SQLite reuses rowids, so a stale index row can point at a
    brand new passage with entirely different words.
    """
    with session_scope() as session:
        row = (
            session.query(Passage)
            .filter(Passage.version_id == "mep-v1")
            .filter(Passage.text.like("%feeder%"))
            .one()
        )
        row.text = "The utility shall replace each feeder before the winter peak."

    with session_scope() as session:
        retrieval = _search(session, "feeder")
        assert retrieval.reason == search.REASON_INDEX_STALE
        assert retrieval.source == search.SOURCE_SCAN
        assert retrieval.hits


def test_a_rebuild_that_fails_partway_leaves_the_old_index_standing(corpus, monkeypatch):
    """A derived corpus migrates all at once or not at all -- best practices 27.

    A half-written index answers every query, plausibly, and wrongly, and there
    is no error to notice. The rebuild therefore runs inside one transaction, so
    a failure anywhere in it leaves yesterday's index exactly as it was.
    """
    real = search.index_body
    calls = {"n": 0}

    def failing(text: str) -> str:
        calls["n"] += 1
        if calls["n"] > 2:
            raise RuntimeError("simulated failure part way through the rebuild")
        return real(text)

    monkeypatch.setattr(search, "index_body", failing)
    with pytest.raises(RuntimeError):
        search.rebuild_index(get_engine())
    monkeypatch.undo()

    with session_scope() as session:
        retrieval = _search(session, "feeder")
        assert retrieval.source == search.SOURCE_INDEX
        assert retrieval.hits


def test_rebuilding_twice_changes_nothing(corpus):
    first = search.rebuild_index(get_engine())
    second = search.rebuild_index(get_engine())
    assert first["indexed"] == second["indexed"]
    assert first["indexed"] > 0

    with session_scope() as session:
        assert _search(session, "feeder").source == search.SOURCE_INDEX


def test_the_build_step_is_a_script_a_person_can_run(unindexed, repo_root):
    """make test and make run are the contract; so is the command a doc names."""
    import os

    env = dict(os.environ)
    env["STRATA_DATABASE_URL"] = os.environ["STRATA_DATABASE_URL"]
    finished = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "build_index.py")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo_root),
    )
    assert finished.returncode == 0, finished.stderr
    assert "passage" in finished.stdout.lower()

    with session_scope() as session:
        assert _search(session, "feeder").source == search.SOURCE_INDEX


# ---------------------------------------------------------------------------
# 5. The tool layer. A rank is a candidate; the citation gate still decides.
# ---------------------------------------------------------------------------

VERIFIED_STATEMENT = "The plan is now due within sixty days."
WITHHELD_STATEMENT = "GAS MAINS MUST BE REPLACED WITHIN NINETY DAYS"

CHAT_SOURCE = (
    "SECTION 1. PURPOSE\n"
    "\n"
    "Each utility shall ﬁle a distribution system implementation plan "
    "within sixty days of the effective date of this order.\n"
    "\n"
    "The commission will publish a curtailment schedule before the winter peak.\n"
    "\n"
    "The utility shall publish the interconnection queue on the commission site.\n"
)


@pytest.fixture
def chat_world():
    """One change, two claims -- one that verifies and one that cannot."""
    init_db()
    search.drop_index(get_engine())
    with session_scope() as session:
        ensure_system_roles(session)
        ingest_version(
            session,
            version_id="chat-v1",
            company_id=COMPANY,
            docket="24-M-0001",
            label="Order, October",
            status="FINAL",
            source_text=CHAT_SOURCE,
        )
        session.add(
            Proceeding(
                id="prc-mep",
                company_id=COMPANY,
                docket="24-M-0001",
                commission="NYPSC",
                subject="Distribution system implementation plans",
            )
        )
        session.add(
            Change(
                id="chg-mep-1",
                company_id=COMPANY,
                proceeding_id="prc-mep",
                from_version_id="chat-v1",
                to_version_id="chat-v1",
                change_type="modified",
                before_start=0,
                before_end=len(CHAT_SOURCE),
                after_start=0,
                after_end=len(CHAT_SOURCE),
                section="1",
                alignment_confidence=0.94,
                materiality=None,
                status="FINAL",
            )
        )
        quote = "within sixty days"
        start = CHAT_SOURCE.index(quote)
        session.add(
            Claim(
                id="clm-good",
                company_id=COMPANY,
                change_id="chg-mep-1",
                statement=VERIFIED_STATEMENT,
                citation_version_id="chat-v1",
                citation_start=start,
                citation_end=start + len(quote),
                citation_quote=quote,
                cited_occurrence=None,
                confidence_bp=9000,
            )
        )
        # Cites the curtailment sentence -- the top-ranked passage for the query
        # below -- and misquotes it, so the gate must withhold it however well
        # its passage ranks.
        bad_quote = "a curtailment schedule"
        bad_start = CHAT_SOURCE.index(bad_quote)
        session.add(
            Claim(
                id="clm-bad",
                company_id=COMPANY,
                change_id="chg-mep-1",
                statement=WITHHELD_STATEMENT,
                citation_version_id="chat-v1",
                citation_start=bad_start,
                citation_end=bad_start + len(bad_quote),
                citation_quote="a curtailment moratorium",
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
                detail="quoted 'a curtailment moratorium'",
            )
        )
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
        session.commit()
        analyst_id = analyst.id
    search.rebuild_index(get_engine())
    yield {"analyst_id": analyst_id}
    search.drop_index(get_engine())


def _actor(session, user_id: str) -> User:
    return session.query(User).filter(User.id == user_id).one()


def test_a_top_ranked_passage_does_not_promote_a_withheld_claim(chat_world):
    """The one property the whole product rests on, put under retrieval pressure.

    "curtailment" ranks the passage the withheld claim cites at the top. A design
    that let a good rank stand in for a verified citation would return its
    statement here. The gate runs afterwards and it decides.
    """
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])
        result = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query="curtailment schedule"
        )
        for text in _strings(result):
            assert WITHHELD_STATEMENT not in text
            assert "NINETY DAYS" not in text
        assert result["withheld"] == 1


def test_retrieval_widens_which_claims_a_question_reaches(chat_world):
    """A claim whose own words miss the question, found through the text it cites.

    The claim says "the plan is now due within sixty days" and carries none of
    the words in the question. The passage it cites carries all of them. The
    substring scan over statements could never reach it; that is the whole
    reason for the index.
    """
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])
        result = chat_tools.search_claims(
            session,
            company_id=COMPANY,
            actor=actor,
            query="distribution system implementation plan",
        )
        assert [row["claim_id"] for row in result["claims"]] == ["clm-good"]


def test_the_tool_says_a_ranked_passage_is_a_candidate(chat_world):
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])
        result = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query="sixty days"
        )
        assert result["candidate_passages"]
        assert result["retrieval"]["source"] == search.SOURCE_INDEX
        assert "candidate" in result["note"]
        for row in result["candidate_passages"]:
            assert row["version_id"] == "chat-v1"
            assert row["is_evidence"] is False


def test_a_candidate_passage_carrying_a_withheld_span_is_not_handed_over(chat_world):
    """The excerpt beside a failed citation is dropped from chat, deliberately.

    app/chat/tools.py already refuses to send a withheld claim's source excerpt
    to the model. Retrieval must not become the second door into the same text
    just because the passage ranked well on its own.
    """
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])
        result = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query="curtailment schedule"
        )
        assert result["candidate_passages"] == []
        assert result["retrieval"]["withheld_spans"] == 1


def test_the_candidate_cap_is_counted_rather_than_hidden(chat_world, monkeypatch):
    """Passages cost context. What the budget leaves out is still said out loud."""
    monkeypatch.setattr(chat_tools, "MAX_CANDIDATE_PASSAGES", 1)
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])
        result = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query="the"
        )
        assert result["retrieval"]["found"] > 2
        assert len(result["candidate_passages"]) == 1
        assert result["omitted"] >= 1
        assert "not shown" in result["note"]


def test_the_tool_announces_a_degraded_retrieval(chat_world):
    """A reply built on the scan has to say so in the transcript, not only in a log."""
    search.drop_index(get_engine())
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])
        result = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query="sixty days"
        )
        assert result["retrieval"]["source"] == search.SOURCE_SCAN
        assert result["retrieval"]["reason"] == search.REASON_INDEX_MISSING
        assert search.REASON_INDEX_MISSING in result["note"] or "index" in result["note"]


def test_an_empty_query_ranks_nothing_rather_than_dumping_the_corpus(chat_world):
    """"Everything" is not a ranking. An empty question retrieves no candidates."""
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])
        result = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query=""
        )
        assert result["candidate_passages"] == []
        assert result["found"] == 1
        assert result["withheld"] == 1


def test_a_widening_cut_short_says_a_claim_may_be_missing(chat_world, monkeypatch):
    """The narrowing nobody would notice: a claim cited outside the passages used.

    Retrieval decides which claims a question reaches. Cap it and some claims are
    never looked at -- and the result would otherwise read exactly like a scope
    that holds nothing more.
    """
    monkeypatch.setattr(chat_tools, "MAX_RETRIEVED_PASSAGES", 1)
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])
        result = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query="the"
        )
        assert "More passages matched" in result["note"]


def test_a_scope_with_nothing_in_it_is_not_a_question_with_no_words(chat_world):
    """Two different silences, two different reasons. Folding them hides a bug."""
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])

        no_project = chat_tools.search_claims(
            session,
            company_id=COMPANY,
            actor=actor,
            query="curtailment",
            project_id="proj-does-not-exist",
        )
        assert no_project["retrieval"]["reason"] == chat_tools.RETRIEVAL_NOT_RUN

        no_words = chat_tools.search_claims(
            session, company_id=COMPANY, actor=actor, query="§§"
        )
        assert no_words["retrieval"]["reason"] == search.REASON_NO_TERMS


def test_a_search_still_cannot_reach_another_company(chat_world):
    with session_scope() as session:
        actor = _actor(session, chat_world["analyst_id"])
        with pytest.raises(ValueError):
            chat_tools.search_claims(
                session, company_id="", actor=actor, query="curtailment"
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
