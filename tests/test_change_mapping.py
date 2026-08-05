"""What a change bears on: the proposal, the confirmation, and the screen.

THE HOLE THIS FILE GUARDS. The build brief asks the product to map each change
to the obligations it affects and route an action from there. Everything
downstream of that mapping existed and was tested -- app/state/routing.py can
walk a mapping to an owner and refuses in fifteen named ways when it cannot --
and the mapping itself was never written by anything. change_obligations held
zero rows after a full seed, so resolve_change_owner answered ROUTE_NO_OBLIGATION
for every change in the product, and the wedge this thing is named for could not
complete once.

THE ONE RULE EVERY TEST BELOW IS ABOUT. A lexical overlap between a docket
passage and a company duty is a CANDIDATE, never a finding. ADR-008 reserves the
semantic join for embeddings that are not built, and the words the regulator
uses are deliberately not the words the company uses -- data/company_context.json
says so in a field called note_for_semantic_join, obligation by obligation. So
the proposer offers, a person decides, and the row records which of them it was.
The assertions that matter are the ones that would fail if a proposal ever
started to read like a decision:

  a candidate carries the words that produced it, so a weak one looks weak;
  an obligation the words did not reach is REPORTED, with the reason, rather
    than left out -- "not found by these words" and "unaffected" are different
    sentences and only the first one is true;
  a stored mapping says whether a person or the pipeline put it there, and the
    screen renders the two in different shapes rather than with a flag.

Offline throughout. No model runs anywhere in this path, which is the point:
every proposal here is a comparison a reviewer can check by hand against the
corpus.
"""

import json
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.audit import event_count
from app.state.db import init_db, session_scope
from app.state.identity import create_user, ensure_system_roles, user_by_email
from app.state.mapping import (
    MATCH_NOT_SEARCHED,
    MATCH_NO_OVERLAP,
    MATCH_ONE_WORD,
    MAX_CANDIDATES,
    MIN_MATCHED_TERMS,
    PROPOSAL_NO_CHANGE,
    PROPOSAL_NO_OBLIGATIONS,
    PROPOSAL_NO_PASSAGES,
    confirm_obligation_for_change,
    propose_obligations_for_change,
    refuse_a_short_proposal,
    terms_of,
    words_of,
)
from app.state.models import (
    AUTHOR_ANALYST,
    AUTHOR_SYSTEM,
    ROLE_ADMIN,
    ROLE_ANALYST,
    AuditEvent,
    Change,
    ChangeObligation,
    Obligation,
)
from app.web.views.changes import (
    LABEL_CANDIDATE,
    LABEL_CONFIRMED,
    LABEL_PROPOSED,
    MISSED_NOTE,
    UNCONFIRMED_ROUTING,
)
from app.state.routing import (
    ACTION_OBLIGATION_MAPPED,
    ROUTE_NO_OBLIGATION,
    ROUTE_OK,
    map_change_to_obligation,
    obligations_for_change,
    resolve_change_owner,
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

COMPANY = "MEP"
RIVAL = "RIVAL"

RIVAL_EMAIL = "mapping-analyst@rival.example"
RIVAL_PASSWORD = "rival-mapping-password"

# The flagship case, and the corpus names it that way itself. Section 5.2 moves
# from 100% customer-pays to a shared allocation, and OBL-005 is the internal
# document that was correct against v1 and is wrong against v2 without anyone
# editing it. The company's wording and the docket's share seven words here,
# which is as strong as lexical overlap gets on this corpus.
FLAGSHIP = "CHG-v1-v2-004"
FLAGSHIP_OBLIGATION = "OBL-005"

# Section 7.1: the compliance date moves from March 2027 to June 2027. OBL-002
# is the duty it bears on, and company_context.json says in as many words that
# the move "is invisible to a lexical match against this obligation's wording".
# So the proposer must NOT offer OBL-002 here, and a person must still be able
# to map it. Both halves are asserted below; together they are the argument for
# proposing rather than asserting.
INVISIBLE = "CHG-v1-v2-006"
INVISIBLE_OBLIGATION = "OBL-002"

# The document title changing from "NOTICE OF PROPOSED RULEMAKING" to "REVISED
# PROPOSED RULE". Nothing in the company's duties uses those words, so this is
# the change where every obligation comes back as an explicit zero.
NO_CANDIDATE = "CHG-v1-v2-000"

CONTEXT = json.loads(
    (pathlib.Path(__file__).resolve().parents[1] / "data" / "company_context.json")
    .read_text(encoding="utf-8")
)


def _obligations(session) -> None:
    """The company's own duties, in the company's own wording, from the corpus.

    Read from data/company_context.json rather than typed here. Every assertion
    in this file turns on the exact words of an obligation, so a test carrying
    its own copy of them would keep passing after somebody edited the corpus --
    which is the drift these tests exist to catch.
    """
    from app.state.routing import ensure_obligation

    people = {
        account.display_name: user_by_email(session, COMPANY, account.email)
        for account in demo_account_list()
    }
    for row in CONTEXT["obligations"]:
        owner = people.get(row.get("owner_name") or "")
        ensure_obligation(
            session,
            COMPANY,
            obligation_id=row["id"],
            title=row["internal_wording"],
            owner_user_id=owner.id if owner is not None else None,
            project_id=None,
            source_document_ref=row.get("source_document_id"),
            actor="system:test",
        )


@pytest.fixture
def seeded():
    """The corpus, the accounts, and the company's duties. One tenant."""
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        _obligations(session)
    yield


def _mapping_events(session):
    """Every obligation.mapped row this company holds, in the order written."""
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.company_id == COMPANY)
        .filter(AuditEvent.action == ACTION_OBLIGATION_MAPPED)
        .order_by(AuditEvent.seq)
        .all()
    )


def _analyst_email() -> str:
    return next(
        account.email
        for account in demo_account_list()
        if account.role == ROLE_ANALYST
    )


def _admin_email() -> str:
    return next(
        account.email for account in demo_account_list() if account.role == ROLE_ADMIN
    )


# ---------------------------------------------------------------------------
# The proposer
# ---------------------------------------------------------------------------


def test_the_proposer_offers_the_obligation_the_words_point_at(seeded):
    """The flagship case, with the words that produced it.

    Not merely that OBL-005 comes back: that it comes back FIRST, that the
    matched words are the ones a person would recognise, and that the span
    handed over is a real passage of the filing rather than a summary of one.
    """
    with session_scope() as session:
        proposal = propose_obligations_for_change(
            session, COMPANY, change_id=FLAGSHIP
        )

        assert proposal.reason == ""
        assert proposal.candidates, "the flagship change proposed nothing"
        top = proposal.candidates[0]
        assert top.obligation_id == FLAGSHIP_OBLIGATION
        assert {"network", "upgrade", "costs"} <= set(top.matched_terms)
        assert top.span is not None
        assert top.span.text, "a candidate with no span cannot be judged"
        assert "Network Upgrade" in top.span.text


def test_a_candidate_carries_the_words_that_produced_it(seeded):
    """Every candidate says WHY, and the why is checkable against the source.

    A score would not do. bm25 says a passage uses a query's words often; it
    cannot say which words, and a number a reader cannot take apart is a number
    they have to trust. Each matched term must actually appear in the span the
    candidate points at, or the explanation is decoration.
    """
    with session_scope() as session:
        proposal = propose_obligations_for_change(
            session, COMPANY, change_id=FLAGSHIP
        )
        for candidate in proposal.candidates:
            assert candidate.matched_terms
            assert len(candidate.matched_terms) >= MIN_MATCHED_TERMS
            words = words_of(candidate.span.text)
            for term in candidate.matched_terms:
                assert any(word.startswith(term) for word in words), (
                    f"{candidate.obligation_id} was proposed on {term!r}, which "
                    "does not start a word in the span it points at"
                )


def test_a_term_matches_a_word_and_never_the_middle_of_one(seeded):
    """The substring rule was tried first and it is wrong here.

    OBL-001 carries the word "work". Section 5.2 is about NETWORK upgrade costs,
    and "work" is a substring of "network" -- so a substring test proposes the
    security-posting duty against every passage in the corpus that mentions a
    network upgrade, with "work" printed underneath as the reason. A reader can
    see through that, which is the only way it was caught; nothing failed.

    The prefix is what FTS5 itself does with a trailing star, so this keeps the
    same rule retrieval applies rather than inventing a second opinion about
    what matching means. The near miss is asserted by name because the general
    check above passes under either rule for any term that happens to also start
    a word somewhere in the span.
    """
    with session_scope() as session:
        proposal = propose_obligations_for_change(
            session, COMPANY, change_id=FLAGSHIP
        )
        row = next(
            row for row in proposal.obligations if row.obligation_id == "OBL-001"
        )
        assert "work" in terms_of(row.title), (
            "the corpus no longer carries the word this test is about"
        )
        assert row.span is not None
        assert "network" in words_of(row.span.text), (
            "the span this test is about no longer mentions a network"
        )
        assert "work" not in row.matched_terms, (
            "a term matched the middle of another word, so the proposer is "
            "using a substring test where it must use a prefix"
        )


def test_an_obligation_the_words_missed_is_reported_rather_than_dropped(seeded):
    """The half most likely to be built wrong, and ADR-79 already argues it.

    A proposal that lists two candidates and says nothing about the other six
    duties reads exactly like a complete answer over a company with two duties.
    Every obligation in scope gets a row, and a row with no candidate carries
    the reason it has none -- which is a statement about the words searched and
    never about the subject.
    """
    with session_scope() as session:
        proposal = propose_obligations_for_change(
            session, COMPANY, change_id=FLAGSHIP
        )
        stored = {row.id for row in session.query(Obligation).filter(
            Obligation.company_id == COMPANY
        )}
        assert {row.obligation_id for row in proposal.obligations} == stored
        assert proposal.in_scope == len(stored)

        silent = [row for row in proposal.obligations if not row.matched_terms]
        assert silent, "no obligation was missed, so this proves nothing"
        for row in silent:
            assert row.reason in (MATCH_NO_OVERLAP, MATCH_ONE_WORD)
            assert row.note
            assert "not affected" not in row.note.casefold()


def test_the_duty_the_corpus_hides_from_a_lexical_match_is_not_proposed(seeded):
    """The negative case the corpus was built to produce, asserted as a feature.

    company_context.json says the section 7.1 deadline move is invisible to a
    lexical match against OBL-002's wording. A proposer that offered it anyway
    would be guessing, and the guess would be right by luck on this corpus and
    wrong on the next one.
    """
    with session_scope() as session:
        proposal = propose_obligations_for_change(
            session, COMPANY, change_id=INVISIBLE
        )
        assert INVISIBLE_OBLIGATION not in {
            row.obligation_id for row in proposal.candidates
        }
        missed = next(
            row
            for row in proposal.obligations
            if row.obligation_id == INVISIBLE_OBLIGATION
        )
        assert missed.reason in (MATCH_NO_OVERLAP, MATCH_ONE_WORD)


def test_one_word_in_common_is_not_a_candidate(seeded):
    """A shared word is a coincidence; the second one is what earns a glance.

    And the two are told apart in the record rather than folded: an obligation
    that shared one word with the change is a different fact from one that
    shared none, and a reader deciding whether the threshold is set right needs
    to see the near misses.
    """
    with session_scope() as session:
        seen = set()
        for change in session.query(Change).filter(Change.company_id == COMPANY):
            proposal = propose_obligations_for_change(
                session, COMPANY, change_id=change.id
            )
            seen.update(row.reason for row in proposal.obligations)
            for row in proposal.candidates:
                assert len(row.matched_terms) >= MIN_MATCHED_TERMS
        assert MATCH_ONE_WORD in seen, (
            "no near miss anywhere in the corpus, so the distinction between a "
            "coincidence and a candidate is not being exercised"
        )


def test_the_candidate_list_is_capped_and_the_accounting_is_not(seeded):
    """The cap limits what a person is asked to read, never what they are told."""
    with session_scope() as session:
        for change in session.query(Change).filter(Change.company_id == COMPANY):
            proposal = propose_obligations_for_change(
                session, COMPANY, change_id=change.id
            )
            assert len(proposal.candidates) <= MAX_CANDIDATES
            assert len(proposal.obligations) == proposal.in_scope


def test_a_change_whose_offsets_reach_no_passage_says_nothing_was_searched(seeded):
    """Nothing searched is not nothing found, and the rows say which.

    A change whose offsets fall outside every stored passage has not been read
    by anybody. Reporting its obligations as unmatched would be the product
    asserting silence it never listened for.
    """
    with session_scope() as session:
        change = session.query(Change).filter(Change.company_id == COMPANY).first()
        change.before_start = None
        change.before_end = None
        change.after_start = 10**9
        change.after_end = 10**9 + 10
        session.flush()

        proposal = propose_obligations_for_change(
            session, COMPANY, change_id=change.id
        )
        assert proposal.reason == PROPOSAL_NO_PASSAGES
        assert proposal.candidates == ()
        assert proposal.searched == 0
        assert {row.reason for row in proposal.obligations} == {MATCH_NOT_SEARCHED}
        session.rollback()


def test_a_company_with_no_obligations_is_told_that_rather_than_shown_nothing(seeded):
    """An empty scope is a fact about the scope, not about the change.

    The same distinction app/state/search.py draws with NOTHING_IN_SCOPE. A
    company that has not recorded its duties yet must not be shown "no
    obligation is affected by this change".
    """
    with session_scope() as session:
        session.query(Obligation).filter(Obligation.company_id == COMPANY).delete()
        session.flush()
        proposal = propose_obligations_for_change(
            session, COMPANY, change_id=FLAGSHIP
        )
        assert proposal.reason == PROPOSAL_NO_OBLIGATIONS
        assert proposal.obligations == ()
        assert proposal.in_scope == 0
        session.rollback()


def test_an_unknown_change_is_a_refusal_and_not_an_empty_proposal(seeded):
    with session_scope() as session:
        proposal = propose_obligations_for_change(
            session, COMPANY, change_id="CHG-does-not-exist"
        )
        assert proposal.reason == PROPOSAL_NO_CHANGE
        assert proposal.obligations == ()


def test_the_proposal_is_deterministic(seeded):
    """Twice, same answer. No model, no ranking that can move under the reader."""
    with session_scope() as session:
        first = propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        second = propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        assert [row.obligation_id for row in first.candidates] == [
            row.obligation_id for row in second.candidates
        ]
        assert [row.matched_terms for row in first.candidates] == [
            row.matched_terms for row in second.candidates
        ]


def test_the_proposer_writes_nothing(seeded):
    with session_scope() as session:
        before = event_count(session, COMPANY)
        mappings = session.query(ChangeObligation).count()
        propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        assert event_count(session, COMPANY) == before
        assert session.query(ChangeObligation).count() == mappings


def test_the_proposal_is_scoped_to_one_company(seeded):
    """Another tenant asking about this change gets a refusal, not a proposal.

    Change ids are printed on screens and in links, so the interesting call is
    the one where the id is real and the scope is wrong.
    """
    with session_scope() as session:
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival analyst",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )
        proposal = propose_obligations_for_change(
            session, RIVAL, change_id=FLAGSHIP
        )
        assert proposal.reason == PROPOSAL_NO_CHANGE
        assert proposal.obligations == ()
        assert proposal.candidates == ()


def test_an_unscoped_proposal_is_refused(seeded):
    with session_scope() as session:
        for scope in (None, "", "   ", "MEP%", " MEP"):
            with pytest.raises(ValueError):
                propose_obligations_for_change(
                    session, scope, change_id=FLAGSHIP
                )


def test_a_short_proposal_is_refused_rather_than_returned():
    """The invariant, enforced rather than trusted.

    A proposal with an obligation missing reads as a complete proposal over a
    smaller company. There is nothing in the shape to notice, which is why this
    raises instead of returning.
    """
    from app.state.mapping import ObligationMatch

    rows = (
        ObligationMatch(
            obligation_id="OBL-001",
            title="a duty",
            project_id=None,
            document_ref=None,
            matched_terms=(),
            span=None,
            mapped=False,
            mapped_by_kind="",
            reason=MATCH_NO_OVERLAP,
            note="a sentence",
        ),
    )
    refuse_a_short_proposal(rows, ("OBL-001",))
    with pytest.raises(RuntimeError):
        refuse_a_short_proposal(rows, ("OBL-001", "OBL-002"))
    with pytest.raises(RuntimeError):
        refuse_a_short_proposal(rows + rows, ("OBL-001",))


def test_a_stored_mapping_is_reported_with_who_put_it_there(seeded):
    """The proposal says what is already recorded, and by whom.

    Without this the screen would offer a person a candidate they had already
    confirmed, and a second confirmation of the same pair would look like a
    second judgement.
    """
    with session_scope() as session:
        map_change_to_obligation(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            mapped_by="system:test",
            mapped_by_kind=AUTHOR_SYSTEM,
        )
        proposal = propose_obligations_for_change(
            session, COMPANY, change_id=FLAGSHIP
        )
        row = next(
            row
            for row in proposal.obligations
            if row.obligation_id == FLAGSHIP_OBLIGATION
        )
        assert row.mapped is True
        assert row.mapped_by_kind == AUTHOR_SYSTEM


# ---------------------------------------------------------------------------
# The confirmation
# ---------------------------------------------------------------------------


def test_confirming_a_candidate_records_the_person_and_not_the_pipeline(seeded):
    """The whole reason mapped_by_kind exists, asserted on the row itself."""
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        row = confirm_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )
        assert row.mapped_by_kind == AUTHOR_ANALYST
        assert row.company_id == COMPANY
        assert [o.id for o in obligations_for_change(session, COMPANY, FLAGSHIP)] == [
            FLAGSHIP_OBLIGATION
        ]


def test_a_confirmation_is_audited_under_the_vocabulary_that_already_exists(seeded):
    """One spelling of obligation.mapped, and the row names the person.

    A second spelling would write an event that verifies perfectly and that no
    query for the first one ever returns.
    """
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        confirm_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )
        events = _mapping_events(session)
        assert len(events) == 1
        assert events[0].subject_id == FLAGSHIP
        assert events[0].actor_user_id == analyst.id
        assert AUTHOR_ANALYST in events[0].reason


def test_a_person_may_map_what_the_words_did_not_find_and_the_record_says_so(seeded):
    """The case the whole design is for.

    OBL-002 is not proposed for the section 7.1 deadline move, because the two
    do not share words. A person who knows the duty maps it anyway, and the
    audit row records that the mapping came from them rather than from an
    overlap -- which is the difference between a tool that only ever confirms
    itself and one a person can correct.
    """
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        confirm_obligation_for_change(
            session,
            COMPANY,
            change_id=INVISIBLE,
            obligation_id=INVISIBLE_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )
        assert INVISIBLE_OBLIGATION in {
            o.id for o in obligations_for_change(session, COMPANY, INVISIBLE)
        }
        reason = _mapping_events(session)[0].reason
        assert "not proposed" in reason


def test_confirming_a_candidate_the_pipeline_stored_records_the_person(seeded):
    """The dead button, and the reason it was dead.

    map_change_to_obligation is idempotent on the pair, so the first version of
    the confirmation delegated the whole job to it and a person confirming a
    candidate the SEED had already written got nothing: no row changed, no chain
    entry, and a page that still read "proposed by the pipeline, not confirmed".
    Every seeded candidate in the workspace had a button that did nothing.

    Three things are asserted, and the third is the one that keeps the record
    honest: the row now names the person, the chain gained a second event rather
    than having its first one edited, and the pipeline's original event is still
    there saying the pipeline proposed it.
    """
    with session_scope() as session:
        map_change_to_obligation(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            mapped_by="system:seed",
            mapped_by_kind=AUTHOR_SYSTEM,
        )
        analyst = user_by_email(session, COMPANY, _analyst_email())
        row = confirm_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )

        assert row.mapped_by_kind == AUTHOR_ANALYST
        assert analyst.email in row.mapped_by
        assert session.query(ChangeObligation).count() == 1

        events = _mapping_events(session)
        assert len(events) == 2, "the promotion did not append its own decision"
        assert "system:seed" in events[0].reason or events[0].actor == "system:seed"
        assert AUTHOR_ANALYST in events[1].reason
        assert events[1].actor_user_id == analyst.id


def test_confirming_the_same_pair_twice_is_a_double_click(seeded):
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        for _ in range(2):
            confirm_obligation_for_change(
                session,
                COMPANY,
                change_id=FLAGSHIP,
                obligation_id=FLAGSHIP_OBLIGATION,
                actor=f"person:{analyst.email}",
                actor_user_id=analyst.id,
            )
        assert session.query(ChangeObligation).count() == 1
        assert len(_mapping_events(session)) == 1


def test_a_confirmation_cannot_cross_a_tenant_boundary(seeded):
    with session_scope() as session:
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival analyst",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )
        before = event_count(session, COMPANY)
        with pytest.raises(ValueError):
            confirm_obligation_for_change(
                session,
                RIVAL,
                change_id=FLAGSHIP,
                obligation_id=FLAGSHIP_OBLIGATION,
                actor="person:rival",
            )
        assert session.query(ChangeObligation).count() == 0
        assert event_count(session, COMPANY) == before


def test_the_wedge_completes_once_a_mapping_exists(seeded):
    """change to obligation to owner, which is what none of this could do.

    Before the mapping the routing layer refuses with ROUTE_NO_OBLIGATION, which
    is correct and useless. After it, the change has a named accountable person.
    Both halves are asserted here so the second cannot be read as an accident of
    the corpus.
    """
    with session_scope() as session:
        before = resolve_change_owner(session, COMPANY, change_id=FLAGSHIP)
        assert before.reason_code == ROUTE_NO_OBLIGATION
        assert before.user_id is None

        analyst = user_by_email(session, COMPANY, _analyst_email())
        confirm_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )

        after = resolve_change_owner(session, COMPANY, change_id=FLAGSHIP)
        assert after.reason_code == ROUTE_OK
        assert after.user_id is not None
        assert after.obligation_ids == (FLAGSHIP_OBLIGATION,)


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


@pytest.fixture
def signed_in(seeded):
    """The assembled application, signed in as the analyst who may confirm."""
    with session_scope() as session:
        ensure_system_roles(session)
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/login",
        data={"email": _analyst_email(), "password": DEMO_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_the_change_screen_shows_the_candidates_and_the_words_behind_them(signed_in):
    page = signed_in.get(f"/changes/{FLAGSHIP}").text
    assert FLAGSHIP_OBLIGATION in page
    assert "Matched on" in page
    assert "network" in page
    assert "upgrade" in page
    assert LABEL_CANDIDATE in page
    # The confirm control, which is what makes it a candidate rather than a
    # finding: something a person is asked about, not something already decided.
    assert f'value="{FLAGSHIP_OBLIGATION}"' in page


def test_the_change_screen_names_the_obligations_the_words_did_not_reach(signed_in):
    """Absence is on the page, not left off it."""
    page = signed_in.get(f"/changes/{NO_CANDIDATE}").text
    assert "OBL-001" in page
    assert MATCH_NO_OVERLAP in page
    assert MISSED_NOTE.split(".")[0] in page


def test_a_proposed_mapping_does_not_read_as_a_confirmed_one(signed_in):
    """The two states are different shapes on the page, not one shape and a flag.

    The strings are imported from the view rather than typed here. A screen test
    that carries its own copy of the words keeps passing after the page stops
    saying them, which is the drift that makes a screen test worthless.
    """
    with session_scope() as session:
        map_change_to_obligation(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            mapped_by="system:seed",
            mapped_by_kind=AUTHOR_SYSTEM,
        )
    page = signed_in.get(f"/changes/{FLAGSHIP}").text
    assert LABEL_CONFIRMED not in page
    assert LABEL_PROPOSED in page
    assert LABEL_CANDIDATE in page


def test_an_owner_reached_through_an_unconfirmed_mapping_is_not_stated_as_one(
    signed_in,
):
    """The page must not contradict itself, and it did.

    resolve_change_owner does not read mapped_by_kind, so a mapping the pipeline
    proposed walks to an owner exactly as a confirmed one does. Found on a real
    page: a Kentucky vegetation-management budget table shares the words
    "project" and "budget" with the cost-allocation duty, and the screen printed
    "Sarah Lindqvist owns OBL-005" immediately above a block saying that same
    mapping was proposed and not confirmed. Two adjacent sentences, and the
    confident one on top.

    Both halves are asserted, because a caveat printed over a CONFIRMED mapping
    would be the opposite error -- refusing to state accountability somebody has
    actually taken.
    """
    with session_scope() as session:
        map_change_to_obligation(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            mapped_by="system:seed",
            mapped_by_kind=AUTHOR_SYSTEM,
        )
    page = signed_in.get(f"/changes/{FLAGSHIP}").text
    assert ROUTE_OK in page, "the fixture no longer routes, so this proves nothing"
    assert UNCONFIRMED_ROUTING in page

    signed_in.post(
        f"/changes/{FLAGSHIP}/obligations",
        data={"obligation_id": FLAGSHIP_OBLIGATION},
        follow_redirects=False,
    )
    confirmed = signed_in.get(f"/changes/{FLAGSHIP}").text
    assert ROUTE_OK in confirmed
    assert UNCONFIRMED_ROUTING not in confirmed


def test_confirming_from_the_screen_writes_the_mapping_and_comes_back(signed_in):
    response = signed_in.post(
        f"/changes/{FLAGSHIP}/obligations",
        data={"obligation_id": FLAGSHIP_OBLIGATION},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/changes/{FLAGSHIP}"
    with session_scope() as session:
        rows = session.query(ChangeObligation).all()
        assert len(rows) == 1
        assert rows[0].mapped_by_kind == AUTHOR_ANALYST
        assert _analyst_email() in rows[0].mapped_by

    page = signed_in.get(f"/changes/{FLAGSHIP}").text
    assert LABEL_CONFIRMED in page


def test_somebody_without_the_permission_is_refused_and_nothing_is_written(seeded):
    """The obligation owner approves; they do not decide what a change bears on.

    The refusal is a 403 with the reason, not a traceback: a control that
    crashes still stops the write and tells the operator the product broke.
    """
    with session_scope() as session:
        ensure_system_roles(session)
    client = TestClient(app, base_url="https://testserver")
    assert (
        client.post(
            "/login",
            data={"email": _admin_email(), "password": DEMO_PASSWORD},
            follow_redirects=False,
        ).status_code
        == 303
    )
    response = client.post(
        f"/changes/{FLAGSHIP}/obligations",
        data={"obligation_id": FLAGSHIP_OBLIGATION},
        follow_redirects=False,
    )
    assert response.status_code == 403
    with session_scope() as session:
        assert session.query(ChangeObligation).count() == 0


def test_confirming_a_change_that_is_not_this_companys_is_a_404(seeded):
    with session_scope() as session:
        ensure_system_roles(session)
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival analyst",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )
        rival = user_by_email(session, RIVAL, RIVAL_EMAIL)
        from app.state.identity import grant_role

        grant_role(
            session,
            RIVAL,
            user_id=rival.id,
            role_name=ROLE_ANALYST,
            actor="system:test",
        )

    client = TestClient(app, base_url="https://testserver")
    import pytest as _pytest

    patch = _pytest.MonkeyPatch()
    try:
        patch.setenv("STRATA_COMPANY_ID", RIVAL)
        assert (
            client.post(
                "/login",
                data={"email": RIVAL_EMAIL, "password": RIVAL_PASSWORD},
                follow_redirects=False,
            ).status_code
            == 303
        )
    finally:
        patch.undo()

    response = client.post(
        f"/changes/{FLAGSHIP}/obligations",
        data={"obligation_id": FLAGSHIP_OBLIGATION},
        follow_redirects=False,
    )
    assert response.status_code == 404
    with session_scope() as session:
        assert session.query(ChangeObligation).count() == 0


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------


def test_the_seed_leaves_a_workspace_where_the_wedge_completes(seeded):
    """Built is not connected, and a seeded demo is how this one gets connected.

    scripts/seed_demo_gaps.py exists because a freshly seeded workspace had
    sixteen empty tables and three of them were features that could not
    demonstrate. change_obligations was still one of them: the obligations
    landed, the mappings never did, and routing had a destination it could not
    reach. This asserts the seed's OUTPUT rather than its capability, which is
    the distinction that script was written about.
    """
    from seed_demo_gaps import seed_change_mappings

    seed_change_mappings()

    with session_scope() as session:
        rows = session.query(ChangeObligation).filter(
            ChangeObligation.company_id == COMPANY
        ).all()
        assert rows, "the seed wrote no mappings, so routing still has nowhere to go"
        kinds = {row.mapped_by_kind for row in rows}
        assert AUTHOR_SYSTEM in kinds, "no candidate is visible in the demo"
        assert AUTHOR_ANALYST in kinds, "no confirmed mapping is visible in the demo"

        routed = [
            row.change_id
            for row in rows
            if resolve_change_owner(
                session, COMPANY, change_id=row.change_id
            ).reason_code
            == ROUTE_OK
        ]
        assert routed, "no change in the seeded workspace reaches an owner"


def test_the_seed_runs_twice_without_writing_twice(seeded):
    from seed_demo_gaps import seed_change_mappings

    seed_change_mappings()
    with session_scope() as session:
        first = session.query(ChangeObligation).count()
        events = event_count(session, COMPANY)
    seed_change_mappings()
    with session_scope() as session:
        assert session.query(ChangeObligation).count() == first
        assert event_count(session, COMPANY) == events
