"""Confirming and rejecting a proposed mapping, from the row up to the screen.

THE DEAD END ADR-87 ACCEPTED ON PURPOSE, AND WHAT CLOSES IT. ADR-87 made
resolve_change_owner refuse to name an owner when the only mappings behind a
change were the pipeline's -- ROUTE_MAPPING_UNCONFIRMED -- and recorded the cost
in its own words: "confirming a mapping is still a database write with no
screen", so the product "refuses in the right place and offers no way to resolve
the refusal". Twenty-four of the twenty-six mappings in the demonstration corpus
are the proposer's, so that dead end is the common case rather than the edge.

Two acts clear it, and only two, and this file asserts both ends of each.

  CONFIRM. A person says the change bears on the duty. The row's mapped_by_kind
  becomes AUTHOR_ANALYST, the chain gains one event naming the account, and
  routing stops refusing -- ROUTE_MAPPING_UNCONFIRMED becomes ROUTE_OK with a
  user id on it. That last step is the one worth pinning: everything before it
  was already true when the refusal was written, and none of it was any use.

  REJECT. A person says it does not. This is the half that did not exist. A
  candidate a person threw away came back on the next render, so the only way to
  clear a wrong proposal was to confirm it -- the screen asked the same question
  for ever and the honest answer made it worse. Rejecting is now a recorded
  decision: it appends to the chain, it takes the row out of the proposal set,
  and it produces no owner.

WHAT A REJECTION IS NOT. It is not a delete. app/state/models.py::ChangeObligation
says nothing there unmaps, and this holds to it: a mapping the pipeline stored
keeps its row, keeps its timestamp and keeps its author, and the rejection is a
new event that says somebody disagreed. Nothing on either side of the argument is
erased, which is the property that makes "why did we not act on this" answerable
in a year.

WHAT THE FIRST VERSION OF THIS FILE TESTED, AND WHAT IT DID NOT. A reviewer
deleted the reject control out of change.html and 244 screen tests stayed green:
every test here posted to reject_url() directly, so the route was proved and the
BUTTON was proved by nothing. Dropping reject_url from the template context was
the same story -- five forms rendering action="" and posting back at a GET route,
47 tests green. That is the failure this project has a name for: a test that
verifies the capability and never the wiring.

So the screen half below reads the CONTROLS OFF THE RENDERED HTML and asks the
application which of them lead anywhere. Nothing in it types a URL that the page
is then asserted to contain; the pairs come out of the page and the routes come
out of app.routes.

Offline throughout. Nothing here runs a model.
"""

import ast
import pathlib
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts, load
from app.state.audit import ACTOR_USER, event_count, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import create_user, ensure_system_roles, grant_role, user_by_email
from app.state.mapping import (
    ACTION_MAPPING_REJECTED,
    PAIR_SEPARATOR,
    REJECT_AFTER_CONFIRMATION,
    REJECTED_UNPROPOSED,
    SUBJECT_CHANGE_OBLIGATION,
    confirm_obligation_for_change,
    mapping_subject_id,
    propose_obligations_for_change,
    reject_obligation_for_change,
)
from app.state.models import (
    AUTHOR_ANALYST,
    AUTHOR_SYSTEM,
    ROLE_ADMIN,
    ROLE_ANALYST,
    AuditEvent,
    ChangeObligation,
)
from app.state.routing import (
    ACTION_OBLIGATION_MAPPED,
    ROUTE_MAPPING_UNCONFIRMED,
    ROUTE_OK,
    map_change_to_obligation,
    mappings_for_change,
    resolve_change_owner,
)
from app.web.views.changes import (
    CONFIRMATION_IS_FINAL,
    LABEL_CANDIDATE,
    LABEL_HAD_MATCHED,
    LABEL_MATCHED_ON,
    LABEL_REJECTED,
    confirm_url,
    reject_url,
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

COMPANY = "MEP"
RIVAL = "RIVAL"

RIVAL_EMAIL = "decisions-analyst@rival.example"
RIVAL_PASSWORD = "rival-decisions-password"

# The flagship case: section 5.2 moves from customer-pays to a shared
# allocation, and OBL-005 is the duty whose internal wording shares seven words
# with it. It is also the case ADR-87 was written about, because OBL-005 has a
# live owner -- so a proposed mapping here reaches a real person and refuses,
# which is the exact state a confirmation has to clear.
FLAGSHIP = "CHG-v1-v2-004"
FLAGSHIP_OBLIGATION = "OBL-005"

# The moment every routing call in this file is asked about.
#
# PINNED, BECAUSE resolve_change_owner CAN TURN ON AN EXPIRY. It hands its clock
# down to the invitation liveness test, so a call that omits the moment reads the
# real clock and passes until the day it does not -- which is the whole argument
# of tests/test_clock_pinned.py, and that file fails on any test that forgets.
NOW = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)


def _obligations(session) -> None:
    """The company's own duties, in the company's own wording, from the corpus."""
    import json

    from app.state.routing import ensure_obligation

    context = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[1] / "data" / "company_context.json"
        ).read_text(encoding="utf-8")
    )
    people = {
        account.display_name: user_by_email(session, COMPANY, account.email)
        for account in demo_account_list()
    }
    for row in context["obligations"]:
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
    init_db()
    with session_scope() as session:
        load(session)
        ensure_accounts(session)
        ensure_system_roles(session)
        _obligations(session)
    yield


def _analyst_email() -> str:
    return next(
        account.email for account in demo_account_list() if account.role == ROLE_ANALYST
    )


def _admin_email() -> str:
    return next(
        account.email for account in demo_account_list() if account.role == ROLE_ADMIN
    )


def _events(session, action: str, company: str = COMPANY):
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.company_id == company)
        .filter(AuditEvent.action == action)
        .order_by(AuditEvent.seq)
        .all()
    )


def _propose_stored_candidate(session) -> None:
    """The pipeline's own mapping, which is what 24 of the 26 in the corpus are."""
    map_change_to_obligation(
        session,
        COMPANY,
        change_id=FLAGSHIP,
        obligation_id=FLAGSHIP_OBLIGATION,
        mapped_by="system:seed",
        mapped_by_kind=AUTHOR_SYSTEM,
    )


# ---------------------------------------------------------------------------
# Reading the controls off the page
#
# THE POINT IS THAT NOTHING HERE TRUSTS THE VIEW. A test that posts to
# reject_url() proves the route works and says nothing about whether any control
# on any page reaches it -- which is how a whole <form> block was deleted from
# change.html under a green suite. These two helpers turn a rendered page into
# the list of things a person can actually press.
# ---------------------------------------------------------------------------


class _Controls(HTMLParser):
    """Every form on a page: where it posts, how, and what it carries."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict] = []
        self._open: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag == "form":
            self._open = {
                # An action attribute that is absent and one that is empty are
                # the same thing to a browser -- both post at the page's own URL
                # -- so they are the same thing here.
                "action": values.get("action") or "",
                "method": (values.get("method") or "get").lower(),
                "fields": {},
            }
            self.forms.append(self._open)
        elif tag == "input" and self._open is not None:
            self._open["fields"][values.get("name") or ""] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._open = None


def _forms(page: str) -> list[dict]:
    parser = _Controls()
    parser.feed(page)
    return parser.forms


def _controls(page: str) -> set[tuple[str, str]]:
    """(where it posts, which duty it names) for every posting control on a page."""
    return {
        (form["action"], form["fields"].get("obligation_id", ""))
        for form in _forms(page)
        if form["method"] == "post"
    }


def _serves_post(path: str) -> bool:
    """Whether the assembled application answers a POST at this exact path.

    Read off app.routes rather than compared against a list of URLs typed here.
    A route renamed next month moves this answer with it, and an action the
    template failed to render at all -- the empty string -- matches nothing,
    which is the case that used to render five dead buttons in silence.
    """
    for route in app.routes:
        regex = getattr(route, "path_regex", None)
        methods = getattr(route, "methods", None) or ()
        if regex is not None and "POST" in methods and regex.match(path):
            return True
    return False


# ---------------------------------------------------------------------------
# Confirming: the refusal ADR-87 created, and the one act that clears it
# ---------------------------------------------------------------------------


def test_confirming_turns_the_routing_refusal_into_a_named_owner(seeded):
    """ROUTE_MAPPING_UNCONFIRMED to ROUTE_OK, which is the whole point of the screen.

    Both halves are asserted in one test on purpose. The refusal on its own is
    a product declining to answer, and the answer on its own proves nothing
    about the refusal it was supposed to clear -- it is the TRANSITION that says
    the analyst has a way out, and the transition is what nothing tested.
    """
    with session_scope() as session:
        _propose_stored_candidate(session)

        before = resolve_change_owner(session, COMPANY, change_id=FLAGSHIP, now=NOW)
        assert before.reason_code == ROUTE_MAPPING_UNCONFIRMED
        assert before.user_id is None
        assert before.candidate_user_ids, (
            "the refusal names nobody, so an analyst cannot tell who confirming "
            "it would hand the work to"
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

        after = resolve_change_owner(session, COMPANY, change_id=FLAGSHIP, now=NOW)
        assert after.reason_code == ROUTE_OK
        assert after.user_id == before.candidate_user_ids[0]
        assert after.obligation_ids == (FLAGSHIP_OBLIGATION,)


def test_confirming_appends_one_audit_row_that_names_the_person(seeded):
    """One row, not two and not none, and it carries the account rather than a label.

    actor is a display string and app/state/models.py says plainly that a
    display string is not an identity. The row has to carry actor_user_id, and
    it has to say a person acted, or the chain records the pipeline confirming
    its own proposal.
    """
    with session_scope() as session:
        _propose_stored_candidate(session)
        analyst = user_by_email(session, COMPANY, _analyst_email())
        before = len(_events(session, ACTION_OBLIGATION_MAPPED))

        confirm_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )

        events = _events(session, ACTION_OBLIGATION_MAPPED)
        assert len(events) == before + 1
        assert events[-1].actor_user_id == analyst.id
        assert events[-1].actor_kind == ACTOR_USER
        assert events[-1].subject_id == FLAGSHIP
        assert AUTHOR_ANALYST in events[-1].reason
        assert verify_chain(session, COMPANY)


# ---------------------------------------------------------------------------
# Rejecting: the half that did not exist
# ---------------------------------------------------------------------------


def test_rejecting_takes_the_candidate_out_of_the_proposal(seeded):
    """The analyst rejects the same row for ever without this.

    A rejection that leaves the candidate on the page is not a decision, it is a
    button. The row is still REPORTED -- absence is denial, and a duty that
    vanishes from the accounting is a duty the reader was never told about --
    but it is no longer offered.
    """
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        offered = {
            row.obligation_id
            for row in propose_obligations_for_change(
                session, COMPANY, change_id=FLAGSHIP
            ).candidates
        }
        assert FLAGSHIP_OBLIGATION in offered, "the fixture proves nothing"

        reject_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )

        after = propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        assert FLAGSHIP_OBLIGATION not in {
            row.obligation_id for row in after.candidates
        }
        row = next(
            row
            for row in after.obligations
            if row.obligation_id == FLAGSHIP_OBLIGATION
        )
        assert row.rejected is True
        assert row.offered is False
        assert row.rejected_by
        assert row.rejected_at is not None
        assert row.obligation_id in {r.obligation_id for r in after.rejected}
        assert after.in_scope == len(after.obligations), (
            "the accounting lost a duty, so a rejection is being hidden rather "
            "than recorded"
        )


def test_rejecting_produces_no_owner(seeded):
    """A rejection must not route, whichever side of it the mapping row is on.

    Both shapes are exercised: a candidate the proposer offered and never stored,
    and one the pipeline had already written into change_obligations. The second
    is the dangerous one -- the row survives the rejection by design, so
    something downstream could still walk it to a person.
    """
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        reject_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )
        unstored = resolve_change_owner(session, COMPANY, change_id=FLAGSHIP, now=NOW)
        assert unstored.user_id is None
        assert unstored.reason_code != ROUTE_OK

        _propose_stored_candidate(session)
        stored = resolve_change_owner(session, COMPANY, change_id=FLAGSHIP, now=NOW)
        assert stored.user_id is None
        assert stored.reason_code != ROUTE_OK


def test_rejecting_appends_one_audit_row_naming_the_person_and_both_ids(seeded):
    """The decision is in the chain, and it is findable by rows rather than prose.

    The subject is the PAIR, because that is what was decided. Recording it
    against the change alone would make "which mappings did we throw away" a
    question somebody answers by reading reason text, which is the failure
    app/state/routing.py names when it argues for two escalation codes instead
    of one.
    """
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        reject_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )

        events = _events(session, ACTION_MAPPING_REJECTED)
        assert len(events) == 1
        assert events[0].subject_type == SUBJECT_CHANGE_OBLIGATION
        assert FLAGSHIP in events[0].subject_id
        assert FLAGSHIP_OBLIGATION in events[0].subject_id
        assert events[0].actor_user_id == analyst.id
        assert events[0].actor_kind == ACTOR_USER
        assert analyst.email in events[0].actor
        assert verify_chain(session, COMPANY)


def test_a_rejection_says_what_it_threw_away(seeded):
    """The words the proposer matched on go in the reason, or the record is thin.

    "Somebody rejected OBL-005" is not reviewable. "Somebody rejected a
    candidate proposed on network, upgrade, costs" is: a reader can see what the
    machine offered and judge the person's answer to it, which is the same move
    the screen makes when it prints the matched words beside the button.
    """
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        proposal = propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        terms = next(
            row.matched_terms
            for row in proposal.candidates
            if row.obligation_id == FLAGSHIP_OBLIGATION
        )
        reject_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )
        reason = _events(session, ACTION_MAPPING_REJECTED)[0].reason
        for term in terms:
            assert term in reason


def test_rejecting_the_same_pair_twice_is_a_double_click(seeded):
    """Restating a decision is not a second decision.

    set_obligation_owner and confirm_obligation_for_change both take this line.
    A chain that gains a row every time somebody reloads a form is a chain
    nobody reads.
    """
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        for _ in range(2):
            reject_obligation_for_change(
                session,
                COMPANY,
                change_id=FLAGSHIP,
                obligation_id=FLAGSHIP_OBLIGATION,
                actor=f"person:{analyst.email}",
                actor_user_id=analyst.id,
            )
        assert len(_events(session, ACTION_MAPPING_REJECTED)) == 1


def test_rejecting_writes_no_row_and_deletes_none(seeded):
    """Nothing unmaps. The pipeline's row survives its own rejection.

    ChangeObligation says so in as many words: a mapping somebody later
    disagrees with is a fact about what was believed, and deleting it erases the
    reason an action was taken. The rejection is the new row that says so.
    """
    with session_scope() as session:
        _propose_stored_candidate(session)
        analyst = user_by_email(session, COMPANY, _analyst_email())
        reject_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )
        rows = mappings_for_change(session, COMPANY, FLAGSHIP)
        assert len(rows) == 1
        assert rows[0].mapped_by == "system:seed"
        assert rows[0].mapped_by_kind == AUTHOR_SYSTEM


def test_rejecting_a_mapping_a_person_confirmed_is_refused_by_name(seeded):
    """A confirmation is not withdrawn by the reject button, and it says why.

    Absence is denial in both directions: the refusal names its reason rather
    than quietly doing nothing, and it does not pretend the rejection landed.
    Taking back a confirmation is a different act with different consequences --
    work has already been routed on it -- and it is not built.
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
        before = event_count(session, COMPANY)
        with pytest.raises(ValueError) as refused:
            reject_obligation_for_change(
                session,
                COMPANY,
                change_id=FLAGSHIP,
                obligation_id=FLAGSHIP_OBLIGATION,
                actor=f"person:{analyst.email}",
                actor_user_id=analyst.id,
            )
        assert REJECT_AFTER_CONFIRMATION in str(refused.value)
        assert event_count(session, COMPANY) == before


def test_a_rejection_is_not_a_dead_end(seeded):
    """Somebody who rejects the right duty by mistake can still map it.

    This is the ADR-87 lesson applied to the cure rather than to the disease. A
    refusal with no way out converts a wrong answer into a dead end, and a
    rejection nobody can undo is exactly that shape one step further on. The
    person's own later judgement governs, and both decisions stay in the chain.
    """
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        reject_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )
        confirm_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )

        proposal = propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        row = next(
            row
            for row in proposal.obligations
            if row.obligation_id == FLAGSHIP_OBLIGATION
        )
        assert row.rejected is False
        assert row.mapped_by_kind == AUTHOR_ANALYST
        assert proposal.rejected == ()

        resolution = resolve_change_owner(session, COMPANY, change_id=FLAGSHIP, now=NOW)
        assert resolution.reason_code == ROUTE_OK
        assert len(_events(session, ACTION_MAPPING_REJECTED)) == 1, (
            "the rejection was erased rather than superseded"
        )


def test_an_unscoped_rejection_is_refused(seeded):
    """Every shape of a scope that is not one: absent, empty, padded, a wildcard.

    WHAT THIS DOES NOT PIN, said because it reads as though it does. The refusal
    can come from either of two places -- this function's own first line, or the
    proposal it calls three lines later -- and both are there. Delete the first
    and this test still passes, because the behaviour is unchanged. The line
    itself is pinned by
    test_every_public_writer_here_checks_its_scope_before_it_does_anything, which
    asks the syntax tree rather than the outcome.
    """
    with session_scope() as session:
        for scope in (None, "", "   ", "MEP%", " MEP"):
            with pytest.raises(ValueError):
                reject_obligation_for_change(
                    session,
                    scope,
                    change_id=FLAGSHIP,
                    obligation_id=FLAGSHIP_OBLIGATION,
                    actor="person:nobody",
                )


def test_a_rejection_cannot_cross_a_tenant_boundary(seeded):
    """MEP's mapping, RIVAL's scope. Nothing is read and nothing is written.

    Change ids and obligation ids are printed on screens and in links, so the
    interesting call is the one where both ids are real and the scope is wrong.
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
        before = event_count(session, COMPANY)
        with pytest.raises(ValueError):
            reject_obligation_for_change(
                session,
                RIVAL,
                change_id=FLAGSHIP,
                obligation_id=FLAGSHIP_OBLIGATION,
                actor="person:rival",
            )
        assert event_count(session, COMPANY) == before
        assert _events(session, ACTION_MAPPING_REJECTED) == []
        assert _events(session, ACTION_MAPPING_REJECTED, company=RIVAL) == []

        # And the rejection MEP records is invisible to RIVAL, which is the
        # other half: a proposal that read another tenant's rejections would
        # withhold candidates for reasons this company never gave.
        analyst = user_by_email(session, COMPANY, _analyst_email())
        reject_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=FLAGSHIP_OBLIGATION,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )
        assert _events(session, ACTION_MAPPING_REJECTED, company=RIVAL) == []


def test_a_subject_id_that_could_name_two_pairs_is_refused():
    """The separator guard, which shipped with fifteen lines of reasoning and no test.

    The failure it stops is quiet and it is the worst shape available: two
    different pairs building ONE subject id, so a rejection of either reads as a
    rejection of both and a duty drops off somebody's page with a correct-looking
    audit row behind it. The first assertion below is the collision itself --
    proof that the two calls really would collide -- and the rest is the refusal.

    No database and no fixture. It is a string builder, and the guard is either
    in it or it is not.
    """
    left = (f"CHG-1{PAIR_SEPARATOR}OBL-1", "OBL-2")
    right = ("CHG-1", f"OBL-1{PAIR_SEPARATOR}OBL-2")
    assert (
        PAIR_SEPARATOR.join(left) == PAIR_SEPARATOR.join(right)
    ), "the two pairs do not collide, so this test is proving nothing"

    for change_id, obligation_id in (left, right):
        with pytest.raises(ValueError) as refused:
            mapping_subject_id(change_id, obligation_id)
        assert PAIR_SEPARATOR in str(refused.value), (
            "the refusal does not name the separator, so a reader cannot tell "
            "which half of the id is the problem"
        )

    for empty in ("", None):
        with pytest.raises(ValueError):
            mapping_subject_id(empty, "OBL-1")
        with pytest.raises(ValueError):
            mapping_subject_id("CHG-1", empty)

    assert (
        mapping_subject_id("CHG-1", "OBL-1") == f"CHG-1{PAIR_SEPARATOR}OBL-1"
    ), "a pair that names two clean ids still has to build its subject"


def test_rejecting_an_obligation_the_record_does_not_have_is_refused_by_name(seeded):
    """The state layer's own refusal, which the route's 404 was standing in front of.

    app/web/views/changes.py checks obligation_for_company before it calls this,
    so the HTTP test never reaches this line -- and a caller that is not the
    screen would have got an AttributeError deep inside instead of a sentence.
    Absence is denial at the layer that knows, not only at the one in front.
    """
    with session_scope() as session:
        with pytest.raises(ValueError) as refused:
            reject_obligation_for_change(
                session,
                COMPANY,
                change_id=FLAGSHIP,
                obligation_id="OBL-does-not-exist",
                actor="person:nobody",
            )
        assert "OBL-does-not-exist" in str(refused.value)
        assert _events(session, ACTION_MAPPING_REJECTED) == []


def test_a_rejected_duty_the_words_never_reached_is_not_a_missed_duty(seeded):
    """Two opposite facts, kept apart. The one Proposal.missed exists to separate.

    "The words did not reach this duty" and "a person read it and said no" are
    statements about how much attention a duty has had, and a screen that printed
    them under one heading would tell the reader nobody had looked. The duty is
    chosen off the proposal rather than named here, so this keeps testing the
    thing it is about when the corpus moves.

    It also walks the only branch of the audit reason nothing else reaches:
    rejecting a duty the words never proposed says so in the chain, rather than
    naming an empty list of matched terms.
    """
    with session_scope() as session:
        analyst = user_by_email(session, COMPANY, _analyst_email())
        before = propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        assert before.missed, (
            "the words reached every duty this company has, so there is no "
            "unproposed duty to reject and this test proves nothing"
        )
        unreached = before.missed[0].obligation_id

        reject_obligation_for_change(
            session,
            COMPANY,
            change_id=FLAGSHIP,
            obligation_id=unreached,
            actor=f"person:{analyst.email}",
            actor_user_id=analyst.id,
        )

        after = propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        assert unreached in {row.obligation_id for row in after.rejected}
        assert unreached not in {row.obligation_id for row in after.missed}, (
            "a duty somebody read and turned down is being reported as a duty "
            "nobody looked at"
        )
        assert unreached in {row.obligation_id for row in after.obligations}
        assert after.in_scope == len(after.obligations)
        assert REJECTED_UNPROPOSED in _events(session, ACTION_MAPPING_REJECTED)[0].reason


def test_every_public_writer_here_checks_its_scope_before_it_does_anything():
    """The scope check is the FIRST line, and that is the rule this pins.

    tests/test_tenancy_derived.py already asks whether a scoped function reaches
    _require_scope at all, and it follows calls to answer -- so deleting the
    check out of reject_obligation_for_change passes there, because the proposal
    it calls three lines later makes the same check. That is defence in depth
    working as intended and it is also why nothing failed when the line went.

    This asks the stricter question the docstrings assume: no read, no write and
    no branch happens on a scope nobody has looked at. Derived by walking this
    module's own syntax tree, so a public writer added next month is asked too
    rather than added to a list somebody keeps.
    """
    source = pathlib.Path(__file__).resolve().parents[1] / "app" / "state" / "mapping.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    def _guards(function: ast.FunctionDef) -> bool:
        body = list(function.body)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        if not body or not isinstance(body[0], ast.Expr):
            return False
        call = body[0].value
        return (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "_require_scope"
            and len(call.args) == 1
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == "company_id"
        )

    asked = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_")
        and "company_id" in {argument.arg for argument in node.args.args}
    ]
    assert asked, "no public scoped function was found, so this proves nothing"
    unguarded = sorted(node.name for node in asked if not _guards(node))
    assert not unguarded, (
        f"{unguarded} do not call _require_scope(company_id) as their first "
        "statement, so a scope nobody checked reaches a read or a write first"
    )


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


@pytest.fixture
def signed_in(seeded):
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/login",
        data={"email": _analyst_email(), "password": DEMO_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_the_screen_shows_the_words_the_proposer_matched_on(signed_in):
    """Every word behind every candidate is on the page, derived from the proposal.

    NOT A HAND-TYPED WORD LIST. A screen test carrying its own copy of "network"
    keeps passing after the proposer stops matching on it, and the whole claim
    of this section is that a confirm button with no evidence behind it just
    moves the guess to a human and calls it confirmed.

    AND NOT `term in page` EITHER, WHICH IS WHAT THIS USED TO ASSERT. The matched
    words are lifted out of the change's own text and the page quotes that text a
    few lines above, so every one of them was on the page whatever the candidate
    block rendered. A reviewer emptied the term list on every candidate -- the
    page then printed "No word in this change reaches this duty's wording" under
    duties that had matched on three -- and this test passed. It now asserts the
    SENTENCE the page builds out of those words, which nothing else on the page
    can supply.
    """
    page = signed_in.get(f"/changes/{FLAGSHIP}").text
    with session_scope() as session:
        proposal = propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        candidates = [(row.obligation_id, row.matched_terms) for row in proposal.candidates]

    assert candidates, "the flagship change offered nothing, so this proves nothing"
    for obligation_id, terms in candidates:
        assert obligation_id in page
        assert terms, f"{obligation_id} was offered with no words behind it"
        assert f"{LABEL_MATCHED_ON} {', '.join(terms)}." in page, (
            f"{obligation_id} was proposed on {list(terms)} and the page does not "
            "say so, so the reader is asked to confirm a guess they cannot see"
        )


def test_rejecting_from_the_screen_clears_the_candidate_and_says_who(signed_in):
    """Every candidate rejected, and the Candidates block is gone rather than reprinted.

    The failure this guards is the one that makes the whole screen pointless: a
    reject that renders the same list again, so the analyst answers the same
    question on every visit and nothing they did was recorded.
    """
    # EVERY OFFERED ROW, NOT proposal.candidates. The shortlist is capped at
    # MAX_CANDIDATES and the screen deliberately renders every offered duty --
    # a row hidden by a display cap is a duty the reader was never told about --
    # so rejecting the top three would leave the fourth on the page and this
    # test would be asserting the cap rather than the rejection.
    with session_scope() as session:
        offered = [
            row.obligation_id
            for row in propose_obligations_for_change(
                session, COMPANY, change_id=FLAGSHIP
            ).obligations
            if row.offered
        ]
    assert offered

    for obligation_id in offered:
        response = signed_in.post(
            reject_url(FLAGSHIP),
            data={"obligation_id": obligation_id},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/changes/{FLAGSHIP}"

    page = signed_in.get(f"/changes/{FLAGSHIP}").text
    assert LABEL_CANDIDATE not in page
    assert LABEL_REJECTED in page
    assert _analyst_email() in page
    for obligation_id in offered:
        assert obligation_id in page

    # The words survive the rejection on the page as well as in the chain. A
    # rejected duty printed as a bare id is as unreviewable as a candidate
    # printed as a bare id: the reader cannot tell a duty turned down over seven
    # shared words from one turned down over two.
    #
    # THE SENTENCE, NOT THE WORDS. Asserting `term in page` passed while the
    # rejected block printed "No word in this change reached this duty's
    # wording" under duties that had matched on three of them -- the words were
    # elsewhere on the page, in the quotation of the change they came out of.
    with session_scope() as session:
        proposal = propose_obligations_for_change(session, COMPANY, change_id=FLAGSHIP)
        rejected = [(row.obligation_id, row.matched_terms) for row in proposal.rejected]
    assert any(terms for _, terms in rejected), (
        "nothing rejected carried any words, so this proves nothing"
    )
    for obligation_id, terms in rejected:
        if not terms:
            continue
        assert f"{LABEL_HAD_MATCHED} {', '.join(terms)}." in page, (
            f"{obligation_id} was turned down after matching on {list(terms)} "
            "and the page does not say what was turned down"
        )


def test_rejecting_a_change_that_is_not_this_companys_is_a_404(seeded):
    """The same body an unknown id gets, and nothing written either side."""
    with session_scope() as session:
        create_user(
            session,
            RIVAL,
            email=RIVAL_EMAIL,
            display_name="A rival analyst",
            password=RIVAL_PASSWORD,
            actor="system:test",
        )
        rival = user_by_email(session, RIVAL, RIVAL_EMAIL)
        grant_role(
            session,
            RIVAL,
            user_id=rival.id,
            role_name=ROLE_ANALYST,
            actor="system:test",
        )
        before = event_count(session, COMPANY)

    client = TestClient(app, base_url="https://testserver")
    patch = pytest.MonkeyPatch()
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
        reject_url(FLAGSHIP),
        data={"obligation_id": FLAGSHIP_OBLIGATION},
        follow_redirects=False,
    )
    assert response.status_code == 404
    with session_scope() as session:
        assert event_count(session, COMPANY) == before
        assert _events(session, ACTION_MAPPING_REJECTED) == []


def test_somebody_without_the_permission_cannot_reject(seeded):
    """Deciding what a change does NOT bear on is the same judgement as deciding
    that it does, so it sits behind the same permission.

    The admin holds user.manage and not action.propose. A 403 with a reason,
    never a traceback.
    """
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
        reject_url(FLAGSHIP),
        data={"obligation_id": FLAGSHIP_OBLIGATION},
        follow_redirects=False,
    )
    assert response.status_code == 403
    with session_scope() as session:
        assert _events(session, ACTION_MAPPING_REJECTED) == []


def test_rejecting_an_obligation_this_company_does_not_have_is_a_404(signed_in):
    response = signed_in.post(
        reject_url(FLAGSHIP),
        data={"obligation_id": "OBL-does-not-exist"},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_the_screen_refuses_to_reject_a_confirmed_mapping_and_says_so(signed_in):
    """The state refusal reaches the person as a decision, not a 500.

    An uncaught ValueError out of the state layer is a traceback where a
    sentence belongs, and tests/test_tenancy_derived.py found exactly that
    omission on another route.
    """
    confirmed = signed_in.post(
        confirm_url(FLAGSHIP),
        data={"obligation_id": FLAGSHIP_OBLIGATION},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303

    response = signed_in.post(
        reject_url(FLAGSHIP),
        data={"obligation_id": FLAGSHIP_OBLIGATION},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert REJECT_AFTER_CONFIRMATION in response.text
    with session_scope() as session:
        rows = session.query(ChangeObligation).all()
        assert len(rows) == 1
        assert rows[0].mapped_by_kind == AUTHOR_ANALYST


# ---------------------------------------------------------------------------
# The controls, read off the page
#
# EVERY TEST ABOVE POSTS TO A URL. That proves the route and says nothing about
# the button, which is how the reject <form> was deleted out of change.html under
# 244 green screen tests, and how five forms came to render action="" with 47
# green. These four ask the page.
# ---------------------------------------------------------------------------


def test_every_offered_candidate_carries_both_answers_on_the_page(signed_in):
    """Two controls per candidate, on the page, pointing at two different routes.

    THE DUTIES COME OUT OF THE PROPOSAL AND THE CONTROLS OUT OF THE HTML, and
    the test is the join between them. A screen that offers a duty and only the
    agreeing answer is the screen this whole section was built to replace: the
    honest answer costs the reader their afternoon, the flattering one clears the
    page, and the record fills up with agreement.
    """
    page = signed_in.get(f"/changes/{FLAGSHIP}").text
    with session_scope() as session:
        offered = [
            row.obligation_id
            for row in propose_obligations_for_change(
                session, COMPANY, change_id=FLAGSHIP
            ).obligations
            if row.offered
        ]
    assert offered, "the flagship change offered nothing, so this proves nothing"

    controls = _controls(page)
    for obligation_id in offered:
        assert (confirm_url(FLAGSHIP), obligation_id) in controls, (
            f"{obligation_id} is offered and the page carries no control that "
            "confirms it"
        )
        assert (reject_url(FLAGSHIP), obligation_id) in controls, (
            f"{obligation_id} is offered and the page carries no control that "
            "turns it down, so the only answer a reader can give is yes"
        )


def test_every_rejected_duty_keeps_a_way_back_on_the_page(signed_in):
    """The escape hatch is a control, not an argument in a docstring.

    A rejection nobody can undo is the ADR-87 dead end one step further along: a
    wrong answer converted into a permanent one. The state layer lets a person
    map a rejected duty anyway; this asserts the page lets them.
    """
    with session_scope() as session:
        offered = [
            row.obligation_id
            for row in propose_obligations_for_change(
                session, COMPANY, change_id=FLAGSHIP
            ).obligations
            if row.offered
        ]
    assert offered

    for obligation_id in offered:
        assert (
            signed_in.post(
                reject_url(FLAGSHIP),
                data={"obligation_id": obligation_id},
                follow_redirects=False,
            ).status_code
            == 303
        )

    controls = _controls(signed_in.get(f"/changes/{FLAGSHIP}").text)
    for obligation_id in offered:
        assert (confirm_url(FLAGSHIP), obligation_id) in controls, (
            f"{obligation_id} was rejected and the page offers no way back, so a "
            "mistake is now permanent"
        )
        assert (reject_url(FLAGSHIP), obligation_id) not in controls, (
            f"{obligation_id} is already rejected and the page still asks"
        )


def test_every_control_on_the_change_screen_posts_at_a_route_the_app_serves(signed_in):
    """No dead buttons, in any of the states this screen has.

    THE FAILURE THIS CATCHES IS SILENT. Drop reject_url out of the template
    context and Jinja renders action="" -- no error, no warning, a button that
    posts back at the GET route it came from and answers 405. The page looks
    exactly right. Both halves are derived: the actions come out of the rendered
    HTML and the routes out of app.routes on the assembled application.

    Three states, because the controls differ between them and a guard on the
    first render would have missed the rejected block entirely.
    """
    pages = [signed_in.get(f"/changes/{FLAGSHIP}").text]

    assert (
        signed_in.post(
            reject_url(FLAGSHIP),
            data={"obligation_id": FLAGSHIP_OBLIGATION},
            follow_redirects=False,
        ).status_code
        == 303
    )
    pages.append(signed_in.get(f"/changes/{FLAGSHIP}").text)

    assert (
        signed_in.post(
            confirm_url(FLAGSHIP),
            data={"obligation_id": FLAGSHIP_OBLIGATION},
            follow_redirects=False,
        ).status_code
        == 303
    )
    pages.append(signed_in.get(f"/changes/{FLAGSHIP}").text)

    seen = 0
    for page in pages:
        for form in _forms(page):
            if form["method"] != "post":
                continue
            seen += 1
            assert form["action"], (
                "a control on the change screen posts at nothing, which a "
                f"browser sends back to the page itself: {form['fields']}"
            )
            assert _serves_post(form["action"]), (
                f"{form['action']!r} is not a path this application answers a "
                "POST at"
            )
    assert seen, "no posting control was found on any render, so this proves nothing"


def test_the_screen_never_offers_a_control_the_product_will_refuse(signed_in):
    """The one-way door is a door the page stops drawing, not one it hides behind.

    Mapping a rejected duty anyway is a CONFIRMATION, and this product does not
    take a confirmation back -- app/state/mapping.py refuses that by name and the
    route answers 409. So the screen must stop offering to reject the pair at the
    moment it starts refusing to. A page that kept the control would be asking
    for an answer it has already decided not to accept.

    The controls are driven rather than typed: the confirmation below is posted
    at whatever action the page's own "map it anyway" form names.
    """
    assert (
        signed_in.post(
            reject_url(FLAGSHIP),
            data={"obligation_id": FLAGSHIP_OBLIGATION},
            follow_redirects=False,
        ).status_code
        == 303
    )

    rejected = signed_in.get(f"/changes/{FLAGSHIP}").text
    way_back = [
        form
        for form in _forms(rejected)
        if form["method"] == "post"
        and form["fields"].get("obligation_id") == FLAGSHIP_OBLIGATION
    ]
    assert len(way_back) == 1, (
        "a rejected duty should carry exactly one control, the way back: "
        f"{[form['action'] for form in way_back]}"
    )
    assert (
        signed_in.post(
            way_back[0]["action"],
            data={"obligation_id": FLAGSHIP_OBLIGATION},
            follow_redirects=False,
        ).status_code
        == 303
    )

    refused = signed_in.post(
        reject_url(FLAGSHIP),
        data={"obligation_id": FLAGSHIP_OBLIGATION},
        follow_redirects=False,
    )
    assert refused.status_code == 409
    assert (reject_url(FLAGSHIP), FLAGSHIP_OBLIGATION) not in _controls(
        signed_in.get(f"/changes/{FLAGSHIP}").text
    ), "the page offers a control the product answers 409 to"


def test_a_screen_that_offers_a_confirmation_says_what_a_confirmation_costs(signed_in):
    """The invitation and the refusal have to agree, and they did not.

    The rejected block told the reader "map the duty anyway and the later
    judgement governs". Press it and the later judgement is not one of several,
    it is the LAST one the product will take: rejecting the pair afterwards is
    409, and there is no control on the page for it. The advice walked a person
    into the exact dead end ADR-87 was written about, in the words of a way out.

    Wherever a confirm control is drawn, the cost of pressing it is on the page.

    THE TWO STATES ARE PICKED SO THAT ONLY ONE BLOCK CAN BE ANSWERING. On the
    first render every confirm control belongs to the candidate list. After every
    offered duty is rejected the candidate list is gone from the page entirely,
    so a sentence found in the second render came out of the rejected block. A
    test that read both blocks at once would pass with the warning on either, and
    the rejected block is the one that had it wrong.
    """
    fresh = signed_in.get(f"/changes/{FLAGSHIP}").text
    assert confirm_url(FLAGSHIP) in {action for action, _ in _controls(fresh)}
    assert CONFIRMATION_IS_FINAL in fresh, (
        "the candidate list asks somebody to confirm a mapping and does not tell "
        "them that this product cannot take a confirmation back"
    )

    with session_scope() as session:
        offered = [
            row.obligation_id
            for row in propose_obligations_for_change(
                session, COMPANY, change_id=FLAGSHIP
            ).obligations
            if row.offered
        ]
    assert offered
    for obligation_id in offered:
        assert (
            signed_in.post(
                reject_url(FLAGSHIP),
                data={"obligation_id": obligation_id},
                follow_redirects=False,
            ).status_code
            == 303
        )

    rejected = signed_in.get(f"/changes/{FLAGSHIP}").text
    assert LABEL_CANDIDATE not in rejected, (
        "a candidate survived, so the sentence below could have come from the "
        "candidate note instead"
    )
    assert confirm_url(FLAGSHIP) in {action for action, _ in _controls(rejected)}
    assert CONFIRMATION_IS_FINAL in rejected, (
        "the rejected block offers a way back and does not say that walking "
        "through it is the last judgement the product will take on the pair"
    )
