"""The feedback loop: the store, the review screen, and the one thing neither may do.

Five questions, in the order they matter.

1. **Can feedback change a verdict?** No, and that is the point of the file. A
   person disagreeing with an answer is a row. It is not a claim asserting
   itself, not an escalation resolving, and not a citation verifying. The tests
   below check the behaviour and then read the module's own source for a write
   to those tables, because the next edit is where this gets lost.

2. **Does a wrong answer route differently from a broken button?** A complaint
   that the product asserted something its source does not support is the most
   serious defect this system can have, and it must not land in a backlog beside
   a misaligned button. It is referred instead, and a referred row can only ever
   become a high-priority bug -- never a low-priority nicety.

3. **Is the reviewer shown what the person was looking at?** A triage screen
   that shows a complaint and not the exchange it is about cannot be used to
   judge the complaint. The screen renders the frozen snapshot, the surface, what
   Clerk said, what it consulted, and how many claims it withheld.

4. **Is a missing count reported rather than assumed?** A clerk turn with a NULL
   withheld_count is a writer defect. The screen says so. It never prints zero.

5. **Is it one tenant, and is the gate real?** Another company's rows are not
   listed and its ids are 404. Somebody without the permission is refused with a
   reason, and the refusal lands in the audit chain.

Under those, four guards against the ways this screen could mislead quietly: a
snapshot missing the answer is recovered and declared rather than rendered
short, a snapshot in an unknown shape says so rather than 500ing the queue, a
capped page says it is capped, and every sentence the screen prints is checked
to survive HTML escaping -- because a constant carrying an apostrophe reaches
the page as `&#39;` and neither a test nor a reviewer searching for those words
would find them.

WHAT IS NOT TESTED HERE. The screen tests assemble their own application from
the routers, the stylesheet mount and the session guard, rather than importing
app/main.py, so the screen can be exercised on its own -- the reason
tests/test_screens.py gives for the same choice. app/main.py does mount this
router, and tests/test_app_wiring.py is what asserts it; that assertion is not
repeated here.

There is no link to this screen in base.html's nav. Reaching it means typing the
path. That is a handoff, not a decision, and it is stated rather than left to be
discovered.

Offline, no API key, no network. https://testserver, because the session cookie
is marked Secure and a client on http drops it in silence.
"""

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts
from app.state import feedback as store
from app.state.audit import verify_chain
from app.state.db import get_engine, session_scope
from app.state.models import (
    CHAT_ROLE_CLERK,
    CHAT_ROLE_PERSON,
    FEEDBACK_KIND_BUG_REPORT,
    FEEDBACK_KIND_FEEDBACK,
    FEEDBACK_NEW,
    FEEDBACK_RESOLVED,
    FEEDBACK_WONT_FIX,
    IMPROVEMENT_BUG,
    IMPROVEMENT_ENHANCEMENT,
    IMPROVEMENT_GUIDANCE,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    RATING_DOWN,
    RATING_UP,
    ROLE_ADMIN,
    ROLE_ANALYST,
    AuditEvent,
    Base,
    ChatMessage,
    ChatSession,
    Escalation,
    Feedback,
    ImprovementItem,
    User,
)
from app.web import STATIC_DIR, STATIC_URL_PATH
from app.web.deps import install_auth
from app.web.views import auth as auth_view
from app.web.views import feedback_review as view

COMPANY = "MEP"
RIVAL = "RIVAL"

ANALYST_ID = "USR-ANALYST"
REVIEWER_ID = "USR-REVIEWER"
REVIEWER = "reviewer@mep.example"


# --------------------------------------------------------------- fixtures


@pytest.fixture
def schema():
    """Tables, no rows."""
    engine = get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


def _user(session, *, user_id: str, company_id: str, email: str) -> User:
    """A row that satisfies the foreign keys. No password path, so no scrypt."""
    person = User(
        id=user_id,
        company_id=company_id,
        email=email,
        display_name=email.split("@")[0],
        password_hash="unused",
        password_salt="unused",
        kdf_params="{}",
        status="active",
    )
    session.add(person)
    session.flush()
    return person


def _conversation(
    session,
    *,
    company_id: str = COMPANY,
    user_id: str = ANALYST_ID,
    session_id: str = "CHS-1",
    question: str = "What moved on the large load docket?",
    answer: str = "Two changes since v2. One claim is withheld.",
    tools_used=None,
    withheld_count: int = 1,
    surface: str = "project/PRJ-1",
    message_id: str = "CHM-2",
) -> str:
    """One question and one answer. Returns the clerk message id.

    Timestamps are stamped from one clock read on purpose, so the ordering the
    store relies on has to come from ordinal and not from created_at.
    """
    moment = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    session.add(
        ChatSession(
            id=session_id,
            company_id=company_id,
            user_id=user_id,
            started_at=moment,
            last_turn_at=moment,
        )
    )
    session.flush()
    session.add(
        ChatMessage(
            id=f"{message_id}-Q",
            session_id=session_id,
            company_id=company_id,
            ordinal=1,
            role=CHAT_ROLE_PERSON,
            text=question,
            surface=surface,
            created_at=moment,
            tools_used=None,
            withheld_count=None,
        )
    )
    session.add(
        ChatMessage(
            id=message_id,
            session_id=session_id,
            company_id=company_id,
            ordinal=2,
            role=CHAT_ROLE_CLERK,
            text=answer,
            surface=surface,
            created_at=moment,
            tools_used=(
                [{"tool": "latest_changes", "found": 2}]
                if tools_used is None
                else tools_used
            ),
            withheld_count=withheld_count,
        )
    )
    session.flush()
    return message_id


@pytest.fixture
def workspace(schema):
    """One company, one analyst, one reviewer, one exchange with Clerk."""
    with session_scope() as session:
        _user(session, user_id=ANALYST_ID, company_id=COMPANY, email="analyst@mep.example")
        _user(session, user_id=REVIEWER_ID, company_id=COMPANY, email=REVIEWER)
        _conversation(session)
    return "CHM-2"


# ------------------------------------------------------- the standing rule


def test_the_module_states_that_it_cannot_change_a_verdict():
    """The sentence is in the docstring, so nobody has to read the code for it.

    Whitespace is flattened first: the rule is the words, and a line break the
    formatter moved must not be able to fail this.
    """
    text = " ".join((store.__doc__ or "").lower().split())
    assert "nothing here may edit a claim" in text
    assert "resolve an escalation" in text
    assert "change a verdict" in text
    assert "only the source agreeing with the quote makes a claim assert" in text


def test_the_source_writes_to_no_claim_and_no_escalation():
    """Fix the class, not the line: a future edit fails here rather than in prod.

    Reading the source rather than the behaviour, because a behaviour test only
    covers the paths it happens to walk. The store may READ an escalation to
    count it; it may not construct one, and it may not touch the two columns
    that close one.
    """
    source = inspect.getsource(store)
    for forbidden in (
        "Escalation(",
        "resolved_at",
        "resolved_by",
        "Claim(",
        ".verified =",
        ".statement =",
        "verify_citation",
    ):
        assert forbidden not in source, f"{forbidden} has no business in the store"


def test_referring_a_complaint_neither_creates_nor_closes_an_escalation(workspace):
    """The behaviour behind the rule above, walked end to end."""
    with session_scope() as session:
        session.add(
            Escalation(
                id="ESC-1",
                company_id=COMPANY,
                claim_id="CLM-1",
                reason_code="quote_mismatch",
                reason_text="the quote is not at the cited offsets",
            )
        )
        session.flush()
        row = store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            message_id=workspace,
            rating=RATING_DOWN,
            comment="It said the tariff applies to 20 MW. The order never says that.",
        )
        store.refer_for_verification(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            note="Reads like a claim asserted on a citation that does not verify.",
        )

    with session_scope() as session:
        assert session.query(Escalation).count() == 1
        held = session.query(Escalation).one()
        assert held.resolved_at is None
        assert held.resolved_by is None


# ------------------------------------------------------------ recording


def test_a_thumb_writes_a_row_scoped_to_the_company(workspace):
    with session_scope() as session:
        row = store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            message_id=workspace,
            rating=RATING_UP,
        )
        assert row.company_id == COMPANY
        assert row.user_id == ANALYST_ID
        assert row.kind == FEEDBACK_KIND_FEEDBACK
        assert row.rating == RATING_UP
        assert row.status == FEEDBACK_NEW
        assert row.chat_message_id == workspace
        assert row.title is None


def test_a_thumb_takes_its_surface_from_the_message(workspace):
    """The feedback POST carries no surface. This is the only place it exists."""
    with session_scope() as session:
        row = store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            message_id=workspace,
            rating=RATING_DOWN,
            comment="wrong",
        )
        assert row.surface == "project/PRJ-1"


def test_a_thumb_freezes_the_exchange(workspace):
    """A snapshot, not a lookup. The rated turn is in it -- see the store docstring."""
    with session_scope() as session:
        row = store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            message_id=workspace,
            rating=RATING_DOWN,
            comment="wrong",
        )
        captured = row.context

    assert isinstance(captured, list), "context is a JSON column, not a string"
    assert [turn["role"] for turn in captured] == [CHAT_ROLE_PERSON, CHAT_ROLE_CLERK]
    assert captured[-1]["text"].startswith("Two changes since v2")
    assert captured[-1]["withheld_count"] == 1
    assert captured[-1]["tools_used"] == [{"tool": "latest_changes", "found": 2}]


def test_the_snapshot_does_not_move_when_the_conversation_does(workspace):
    with session_scope() as session:
        row = store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            message_id=workspace,
            rating=RATING_DOWN,
            comment="wrong",
        )
        feedback_id = row.id

    with session_scope() as session:
        session.add(
            ChatMessage(
                id="CHM-3",
                session_id="CHS-1",
                company_id=COMPANY,
                ordinal=3,
                role=CHAT_ROLE_PERSON,
                text="Try again.",
                surface="project/PRJ-1",
            )
        )

    with session_scope() as session:
        row = store.feedback_row(
            session, company_id=COMPANY, feedback_id=feedback_id
        )
        assert len(row.context) == 2


def test_a_trimmed_snapshot_says_it_was_trimmed(workspace):
    """A snapshot that quietly starts mid-conversation reads as the whole of it."""
    with session_scope() as session:
        for ordinal in range(3, 3 + store.CONTEXT_TURNS + 4):
            session.add(
                ChatMessage(
                    id=f"CHM-{ordinal}",
                    session_id="CHS-1",
                    company_id=COMPANY,
                    ordinal=ordinal,
                    role=CHAT_ROLE_CLERK if ordinal % 2 == 0 else CHAT_ROLE_PERSON,
                    text=f"turn {ordinal}",
                    surface="project/PRJ-1",
                    tools_used=[] if ordinal % 2 == 0 else None,
                    withheld_count=0 if ordinal % 2 == 0 else None,
                )
            )
        session.flush()
        last = 3 + store.CONTEXT_TURNS + 3
        row = store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            message_id=f"CHM-{last if last % 2 == 0 else last - 1}",
            rating=RATING_DOWN,
            comment="wrong",
        )
        captured = row.context

    assert len(captured) == store.CONTEXT_TURNS + 1
    assert captured[0]["role"] == store.OMITTED_ROLE
    assert "were not captured" in captured[0]["text"]
    assert captured[0]["role"] not in (CHAT_ROLE_PERSON, CHAT_ROLE_CLERK)


def test_a_thumb_on_another_tenants_message_is_refused(workspace):
    with session_scope() as session:
        _user(session, user_id="USR-RIVAL", company_id=RIVAL, email="a@rival.example")
        _conversation(
            session,
            company_id=RIVAL,
            user_id="USR-RIVAL",
            session_id="CHS-R",
            message_id="CHM-R",
        )

    with session_scope() as session:
        with pytest.raises(ValueError, match="no such chat message"):
            store.record_thumb(
                session,
                company_id=COMPANY,
                user_id=ANALYST_ID,
                message_id="CHM-R",
                rating=RATING_DOWN,
            )


def test_a_thumb_on_a_persons_own_turn_is_refused(workspace):
    """Rating your own question is not feedback about the product."""
    with session_scope() as session:
        with pytest.raises(ValueError, match="clerk"):
            store.record_thumb(
                session,
                company_id=COMPANY,
                user_id=ANALYST_ID,
                message_id="CHM-2-Q",
                rating=RATING_DOWN,
            )


def test_a_rating_outside_the_vocabulary_is_refused(workspace):
    with session_scope() as session:
        with pytest.raises(ValueError, match="rating"):
            store.record_thumb(
                session,
                company_id=COMPANY,
                user_id=ANALYST_ID,
                message_id=workspace,
                rating="meh",
            )


def test_a_bug_report_carries_a_title_and_no_rating(workspace):
    with session_scope() as session:
        row = store.record_bug_report(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            title="The citation chip does not open",
            detail="Clicking the chip on the change screen does nothing in Safari.",
            surface="change/CHG-4",
        )
        assert row.kind == FEEDBACK_KIND_BUG_REPORT
        assert row.rating is None
        assert row.title == "The citation chip does not open"
        assert row.comment.startswith("Clicking the chip")
        assert row.context is None
        assert row.chat_message_id is None
        assert row.surface == "change/CHG-4"


def test_a_bug_report_needs_a_title(workspace):
    with session_scope() as session:
        with pytest.raises(ValueError, match="title"):
            store.record_bug_report(
                session,
                company_id=COMPANY,
                user_id=ANALYST_ID,
                title="   ",
                detail="something is broken",
                surface="change/CHG-4",
            )


def test_submitting_is_not_an_audit_event(workspace):
    """A decision belongs in the chain. A complaint is a row that decides nothing.

    Pinned because the opposite is the tempting default, and it would let any
    signed-in person append without limit to the one table that cannot be
    trimmed. See the store docstring.
    """
    with session_scope() as session:
        store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            message_id=workspace,
            rating=RATING_DOWN,
            comment="wrong",
        )
        store.record_bug_report(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            title="a button",
            detail="broken",
            surface="review",
        )

    with session_scope() as session:
        assert session.query(AuditEvent).count() == 0


# ------------------------------------------------------------- the queue


def test_the_queue_is_newest_first_and_company_scoped(workspace):
    with session_scope() as session:
        _user(session, user_id="USR-RIVAL", company_id=RIVAL, email="a@rival.example")
        base = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
        for index in range(3):
            row = store.record_bug_report(
                session,
                company_id=COMPANY,
                user_id=ANALYST_ID,
                title=f"complaint {index}",
                detail="",
                surface="review",
            )
            row.created_at = base + timedelta(hours=index)
        theirs = store.record_bug_report(
            session,
            company_id=RIVAL,
            user_id="USR-RIVAL",
            title="not ours",
            detail="",
            surface="review",
        )
        theirs.created_at = base + timedelta(hours=9)

    with session_scope() as session:
        rows = store.feedback_for_company(session, company_id=COMPANY)
        assert [row.title for row in rows] == [
            "complaint 2",
            "complaint 1",
            "complaint 0",
        ]


def test_an_unscoped_queue_read_is_refused():
    with session_scope() as session:
        with pytest.raises(ValueError, match="company_id is required"):
            store.feedback_for_company(session, company_id="")


def test_another_tenants_feedback_id_resolves_to_nothing(workspace):
    with session_scope() as session:
        _user(session, user_id="USR-RIVAL", company_id=RIVAL, email="a@rival.example")
        theirs = store.record_bug_report(
            session,
            company_id=RIVAL,
            user_id="USR-RIVAL",
            title="not ours",
            detail="",
            surface="review",
        )
        their_id = theirs.id

    with session_scope() as session:
        assert store.feedback_row(session, company_id=COMPANY, feedback_id=their_id) is None


# -------------------------------------------------------------- the screen test


def test_the_deterministic_screen_flags_an_assertion_complaint():
    for words in (
        "It made that up. The order never says 20 MW.",
        "This is a misquote of paragraph 14.",
        "The citation is wrong -- that text is not in the source.",
        "It should have been withheld, not asserted.",
    ):
        assert store.suspect_of_assertion(title="", comment=words)


def test_the_deterministic_screen_leaves_a_broken_button_alone():
    for words in (
        "The citation chip does not open in Safari.",
        "The table scrolls sideways on my laptop.",
        "Could the review queue remember my filter?",
    ):
        assert not store.suspect_of_assertion(title="", comment=words)


def test_the_screen_is_a_screen_and_not_a_verdict():
    """It misses. That is stated rather than discovered.

    A complaint phrased without any of the words the screen knows is a real
    wrong-answer report and the screen returns nothing for it. Absence of a flag
    is the screen finding nothing, never evidence the answer was right, and the
    review screen says so on the page.
    """
    assert not store.suspect_of_assertion(
        title="", comment="This is not right and I do not believe the second line."
    )
    assert "not a verdict" in (store.suspect_of_assertion.__doc__ or "").lower()


# ------------------------------------------------------------- triage


def _complaint(session, *, comment="It made that up.", title=None):
    if title is not None:
        return store.record_bug_report(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            title=title,
            detail=comment,
            surface="review",
        )
    return store.record_thumb(
        session,
        company_id=COMPANY,
        user_id=ANALYST_ID,
        message_id="CHM-2",
        rating=RATING_DOWN,
        comment=comment,
    )


def test_triage_creates_an_item_and_links_the_complaint_to_it(workspace):
    with session_scope() as session:
        row = _complaint(session, title="The chip does not open", comment="in Safari")
        item = store.triage(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            category=IMPROVEMENT_BUG,
            title="Citation chip is dead in Safari",
            detail="Reported once.",
            priority=PRIORITY_NORMAL,
        )
        feedback_id, item_id = row.id, item.id

    with session_scope() as session:
        row = store.feedback_row(session, company_id=COMPANY, feedback_id=feedback_id)
        assert row.status == store.FEEDBACK_TRIAGED
        assert row.improvement_item_id == item_id
        item = session.get(ImprovementItem, item_id)
        assert item.company_id == COMPANY
        assert item.category == IMPROVEMENT_BUG
        assert item.priority == PRIORITY_NORMAL


def test_many_complaints_can_be_triaged_into_one_item(workspace):
    with session_scope() as session:
        first = _complaint(session, title="chip", comment="dead")
        item = store.triage(
            session,
            company_id=COMPANY,
            feedback_id=first.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            category=IMPROVEMENT_BUG,
            title="Citation chip is dead in Safari",
        )
        second = _complaint(session, title="chip again", comment="still dead")
        store.triage(
            session,
            company_id=COMPANY,
            feedback_id=second.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            item_id=item.id,
        )
        ids = (first.id, second.id, item.id)

    with session_scope() as session:
        assert session.query(ImprovementItem).count() == 1
        for feedback_id in ids[:2]:
            row = store.feedback_row(
                session, company_id=COMPANY, feedback_id=feedback_id
            )
            assert row.improvement_item_id == ids[2]


def test_triage_is_audited(workspace):
    with session_scope() as session:
        row = _complaint(session, title="chip", comment="dead")
        store.triage(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            category=IMPROVEMENT_BUG,
            title="Citation chip is dead",
        )

    with session_scope() as session:
        events = session.query(AuditEvent).all()
        assert [event.action for event in events] == [store.ACTION_FEEDBACK_TRIAGED]
        assert events[0].actor_user_id == REVIEWER_ID
        assert verify_chain(session, COMPANY)


def test_triage_refuses_a_category_outside_the_vocabulary(workspace):
    with session_scope() as session:
        row = _complaint(session, title="chip", comment="dead")
        with pytest.raises(ValueError, match="category"):
            store.triage(
                session,
                company_id=COMPANY,
                feedback_id=row.id,
                actor=REVIEWER,
                actor_user_id=REVIEWER_ID,
                category="annoyance",
                title="something",
            )


def test_triage_refuses_another_tenants_item(workspace):
    with session_scope() as session:
        theirs = ImprovementItem(
            id="IMP-RIVAL",
            company_id=RIVAL,
            category=IMPROVEMENT_BUG,
            title="theirs",
        )
        session.add(theirs)
        session.flush()
        row = _complaint(session, title="chip", comment="dead")
        with pytest.raises(ValueError, match="no such improvement item"):
            store.triage(
                session,
                company_id=COMPANY,
                feedback_id=row.id,
                actor=REVIEWER,
                actor_user_id=REVIEWER_ID,
                item_id="IMP-RIVAL",
            )


# ------------------------------------------------- the wrong-answer route


def test_a_referral_marks_the_row_and_says_so_in_words(workspace):
    with session_scope() as session:
        row = _complaint(session)
        store.refer_for_verification(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            note="Claim on the second line may rest on a citation that fails.",
        )
        feedback_id = row.id

    with session_scope() as session:
        row = store.feedback_row(session, company_id=COMPANY, feedback_id=feedback_id)
        assert store.is_referred(row)
        assert row.improvement_item_id is None
        assert row.resolution.startswith(store.REFERRAL_PREFIX)
        assert "citation" in row.resolution


def test_a_referral_is_audited_under_its_own_action(workspace):
    with session_scope() as session:
        row = _complaint(session)
        store.refer_for_verification(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            note="look at it",
        )

    with session_scope() as session:
        events = session.query(AuditEvent).all()
        assert [event.action for event in events] == [store.ACTION_FEEDBACK_REFERRED]
        assert events[0].action != store.ACTION_FEEDBACK_TRIAGED
        assert verify_chain(session, COMPANY)


def test_a_referred_row_may_only_become_a_high_priority_bug(workspace):
    """The one downgrade that must be impossible.

    Filing "the product asserted a claim its source does not support" as a
    low-priority nicety is how the defect the product exists to prevent gets
    lost in a backlog.
    """
    with session_scope() as session:
        row = _complaint(session)
        store.refer_for_verification(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            note="look at it",
        )
        for category, priority in (
            (IMPROVEMENT_GUIDANCE, PRIORITY_HIGH),
            (IMPROVEMENT_ENHANCEMENT, PRIORITY_HIGH),
            (IMPROVEMENT_BUG, PRIORITY_LOW),
            (IMPROVEMENT_BUG, PRIORITY_NORMAL),
        ):
            with pytest.raises(ValueError, match="referred"):
                store.triage(
                    session,
                    company_id=COMPANY,
                    feedback_id=row.id,
                    actor=REVIEWER,
                    actor_user_id=REVIEWER_ID,
                    category=category,
                    title="something",
                    priority=priority,
                )

        item = store.triage(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            category=IMPROVEMENT_BUG,
            title="Claim asserted on a citation that does not verify",
            priority=PRIORITY_HIGH,
        )
        assert item.priority == PRIORITY_HIGH


def test_referring_twice_is_refused_rather_than_dropped(workspace):
    with session_scope() as session:
        row = _complaint(session)
        store.refer_for_verification(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            note="first",
        )
        with pytest.raises(ValueError, match="already referred"):
            store.refer_for_verification(
                session,
                company_id=COMPANY,
                feedback_id=row.id,
                actor=REVIEWER,
                actor_user_id=REVIEWER_ID,
                note="second",
            )


def test_referring_a_closed_row_is_refused(workspace):
    with session_scope() as session:
        row = _complaint(session, title="chip", comment="dead")
        store.resolve(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            resolution="Fixed in the stylesheet.",
        )
        with pytest.raises(ValueError, match="closed"):
            store.refer_for_verification(
                session,
                company_id=COMPANY,
                feedback_id=row.id,
                actor=REVIEWER,
                actor_user_id=REVIEWER_ID,
                note="look again",
            )


def test_a_referral_needs_words(workspace):
    with session_scope() as session:
        row = _complaint(session)
        with pytest.raises(ValueError, match="note"):
            store.refer_for_verification(
                session,
                company_id=COMPANY,
                feedback_id=row.id,
                actor=REVIEWER,
                actor_user_id=REVIEWER_ID,
                note="  ",
            )


# ------------------------------------------------------------- resolving


def test_resolving_needs_words(workspace):
    with session_scope() as session:
        row = _complaint(session, title="chip", comment="dead")
        with pytest.raises(ValueError, match="resolution"):
            store.resolve(
                session,
                company_id=COMPANY,
                feedback_id=row.id,
                actor=REVIEWER,
                actor_user_id=REVIEWER_ID,
                resolution="",
            )


def test_resolving_can_close_as_wont_fix(workspace):
    with session_scope() as session:
        row = _complaint(session, title="chip", comment="dead")
        closed = store.resolve(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            resolution="Safari 14 is out of support.",
            status=FEEDBACK_WONT_FIX,
        )
        assert closed.status == FEEDBACK_WONT_FIX


def test_resolving_refuses_a_status_that_is_not_a_closure(workspace):
    with session_scope() as session:
        row = _complaint(session, title="chip", comment="dead")
        with pytest.raises(ValueError, match="close"):
            store.resolve(
                session,
                company_id=COMPANY,
                feedback_id=row.id,
                actor=REVIEWER,
                actor_user_id=REVIEWER_ID,
                resolution="words",
                status=FEEDBACK_NEW,
            )


def test_closing_a_referral_uses_its_own_action_code(workspace):
    """Who closed a suspected assertion defect, and what did they say they checked."""
    with session_scope() as session:
        row = _complaint(session)
        store.refer_for_verification(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            note="second line looks unsupported",
        )
        store.resolve(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            resolution="Re-read the source at the cited offsets. The quote is exact.",
        )

    with session_scope() as session:
        actions = [event.action for event in session.query(AuditEvent).all()]
        assert actions == [
            store.ACTION_FEEDBACK_REFERRED,
            store.ACTION_REFERRAL_CLOSED,
        ]
        closing = session.query(AuditEvent).order_by(AuditEvent.seq.desc()).first()
        # The referral note survives in the chain even though the row's
        # resolution now carries the closure.
        assert "second line looks unsupported" in closing.reason
        assert verify_chain(session, COMPANY)


def test_closing_an_ordinary_row_uses_the_ordinary_action_code(workspace):
    with session_scope() as session:
        row = _complaint(session, title="chip", comment="dead")
        store.resolve(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            resolution="Fixed.",
        )

    with session_scope() as session:
        actions = [event.action for event in session.query(AuditEvent).all()]
        assert actions == [store.ACTION_FEEDBACK_RESOLVED]


def test_the_chain_verifies_after_the_whole_lifecycle(workspace):
    with session_scope() as session:
        row = _complaint(session)
        store.refer_for_verification(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            note="check it",
        )
        store.triage(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            category=IMPROVEMENT_BUG,
            title="Claim asserted on a failing citation",
            priority=PRIORITY_HIGH,
        )
        store.resolve(
            session,
            company_id=COMPANY,
            feedback_id=row.id,
            actor=REVIEWER,
            actor_user_id=REVIEWER_ID,
            resolution="Verifier fixed; regression guard added.",
        )

    with session_scope() as session:
        assert verify_chain(session, COMPANY)
        assert session.query(AuditEvent).count() == 3


# -------------------------------------------------------- reading a turn


def test_a_clerk_turn_with_no_count_is_reported_not_assumed_zero(workspace):
    """Absence is denial, at the one place it is easy to paper over."""
    with session_scope() as session:
        session.add(
            ChatMessage(
                id="CHM-9",
                session_id="CHS-1",
                company_id=COMPANY,
                ordinal=9,
                role=CHAT_ROLE_CLERK,
                text="Nothing on the record for that.",
                surface="review",
                tools_used=[],
                withheld_count=None,
            )
        )
        session.flush()
        row = store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=ANALYST_ID,
            message_id="CHM-9",
            rating=RATING_DOWN,
            comment="unhelpful",
        )
        captured = row.context

    assert captured[-1]["withheld_count"] is None
    assert store.withheld_words(None) == store.COUNT_MISSING
    assert store.withheld_words(0) != store.COUNT_MISSING
    assert "0" in store.withheld_words(0)


# =========================================================== the screen


def _email(role: str) -> str:
    return next(
        account.email for account in demo_account_list() if account.role == role
    )


@pytest.fixture
def screen_app():
    """The screen assembled here rather than imported from app/main.py.

    Nothing wires this router into the product yet -- that file belongs to
    another agent -- so the tests build what the product will build: the router,
    the login routes the gate needs, the stylesheet mount base.html links, and
    the same session guard install_auth() puts in front of every screen.
    """
    app = FastAPI()
    app.mount(STATIC_URL_PATH, StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth_view.router)
    app.include_router(view.router)
    install_auth(app)
    return app


@pytest.fixture
def seeded_accounts(schema):
    with session_scope() as session:
        ensure_accounts(session)


@pytest.fixture
def anonymous(screen_app, seeded_accounts) -> TestClient:
    return TestClient(screen_app, base_url="https://testserver")


def _sign_in(client: TestClient, email: str):
    return client.post(
        "/login",
        data={"email": email, "password": DEMO_PASSWORD},
        follow_redirects=False,
    )


@pytest.fixture
def reviewer(anonymous: TestClient) -> TestClient:
    """Signed in as the account holding the permission the screen gates on."""
    assert _sign_in(anonymous, _email(ROLE_ADMIN)).status_code == 303
    return anonymous


@pytest.fixture
def outsider(anonymous: TestClient) -> TestClient:
    """Signed in as somebody who does not hold it."""
    assert _sign_in(anonymous, _email(ROLE_ANALYST)).status_code == 303
    return anonymous


@pytest.fixture
def complaint(seeded_accounts):
    """One thumbs-down about a wrong answer, and one ordinary bug report.

    Depends on the accounts and NOT on a signed-in client. `reviewer` and
    `outsider` sign in through the same TestClient, so a fixture that pulled one
    of them in would sign that person in again after the test had chosen the
    other, and the refusal tests would quietly run as an administrator.
    """
    with session_scope() as session:
        person = session.query(User).filter(User.company_id == COMPANY).first()
        _conversation(session, user_id=person.id)
        thumb = store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=person.id,
            message_id="CHM-2",
            rating=RATING_DOWN,
            comment="It made that up. The order never says 20 MW.",
        )
        bug = store.record_bug_report(
            session,
            company_id=COMPANY,
            user_id=person.id,
            title="Citation chip does not open",
            detail="Nothing happens in Safari.",
            surface="change/CHG-4",
        )
        return thumb.id, bug.id


def test_every_sentence_the_screen_prints_survives_escaping():
    """Fix the class: the reason the two recovery notices were unfindable once.

    Jinja escapes what it renders, so a constant carrying an apostrophe reaches
    the page as `&#39;` and every check for it fails -- including a reviewer
    searching the page for the words they were told to look for. Rather than
    matching escaped forms in the tests, the rule is that these sentences use no
    character escaping touches.
    """
    from html import escape

    for name in (
        "REFUSED",
        "STAND_IN",
        "SUSPECT_BANNER",
        "SCREEN_IS_NOT_A_VERDICT",
        "REFERRAL_NOTE",
        "NOTHING_YET",
        "SNAPSHOT_THIN",
        "ANSWER_MISSING",
    ):
        words = getattr(view, name)
        assert escape(words, quote=True) == words, name
    for words in (store.COUNT_MISSING, store.REFERRAL_PREFIX, store.REFERRAL_TAIL):
        assert escape(words, quote=True) == words
    assert escape(view.truncated_words(2, 4), quote=True) == view.truncated_words(2, 4)


def test_the_screen_refuses_somebody_without_the_permission(outsider, complaint):
    response = outsider.get(view.FEEDBACK_URL)
    assert response.status_code == 403
    body = response.text
    assert view.PERMISSION in body
    assert "It made that up" not in body


def test_the_refusal_lands_in_the_audit_chain(outsider, complaint):
    with session_scope() as session:
        before = session.query(AuditEvent).count()
    outsider.get(view.FEEDBACK_URL)
    with session_scope() as session:
        after = session.query(AuditEvent).all()
        assert len(after) == before + 1
        assert after[-1].action == "access.denied"
        assert verify_chain(session, COMPANY)


def test_the_screen_shows_what_the_person_was_looking_at(reviewer, complaint):
    body = reviewer.get(view.FEEDBACK_URL).text
    assert "It made that up" in body
    # The surface, the question, and Clerk's own words.
    assert "project/PRJ-1" in body
    assert "What moved on the large load docket?" in body
    assert "Two changes since v2" in body
    # What it consulted, and what it withheld.
    assert "latest_changes" in body


def test_the_screen_puts_a_suspected_assertion_defect_first(reviewer, complaint):
    body = reviewer.get(view.FEEDBACK_URL).text
    thumb_at = body.index("It made that up")
    bug_at = body.index("Citation chip does not open")
    assert thumb_at < bug_at
    assert view.SUSPECT_BANNER in body


def test_the_screen_says_the_flag_is_not_a_verdict(reviewer, complaint):
    body = reviewer.get(view.FEEDBACK_URL).text
    assert view.SCREEN_IS_NOT_A_VERDICT in body


def test_the_screen_names_the_permission_it_is_standing_in_for(reviewer, complaint):
    """The interim gate announces itself rather than passing as the real one."""
    body = reviewer.get(view.FEEDBACK_URL).text
    assert view.WANTED_PERMISSION in body
    assert view.PERMISSION in body


def test_the_screen_announces_a_missing_withheld_count(reviewer):
    with session_scope() as session:
        person = session.query(User).filter(User.company_id == COMPANY).first()
        _conversation(session, user_id=person.id, withheld_count=None)
        store.record_thumb(
            session,
            company_id=COMPANY,
            user_id=person.id,
            message_id="CHM-2",
            rating=RATING_DOWN,
            comment="unhelpful",
        )
    body = reviewer.get(view.FEEDBACK_URL).text
    assert store.COUNT_MISSING in body


def test_triage_from_the_screen_writes_the_item(reviewer, complaint):
    _, bug_id = complaint
    response = reviewer.post(
        f"{view.FEEDBACK_URL}/{bug_id}/triage",
        data={
            "category": IMPROVEMENT_BUG,
            "priority": PRIORITY_NORMAL,
            "title": "Citation chip is dead in Safari",
            "detail": "One report.",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_scope() as session:
        item = session.query(ImprovementItem).one()
        assert item.title == "Citation chip is dead in Safari"
        assert verify_chain(session, COMPANY)


def test_referral_from_the_screen_marks_the_row(reviewer, complaint):
    thumb_id, _ = complaint
    response = reviewer.post(
        f"{view.FEEDBACK_URL}/{thumb_id}/refer",
        data={"note": "The second line may rest on a citation that fails."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_scope() as session:
        row = store.feedback_row(session, company_id=COMPANY, feedback_id=thumb_id)
        assert store.is_referred(row)
        assert session.query(Escalation).filter(
            Escalation.claim_id == thumb_id
        ).count() == 0


def test_the_screen_refuses_a_write_from_somebody_without_the_permission(
    outsider, complaint
):
    thumb_id, _ = complaint
    response = outsider.post(
        f"{view.FEEDBACK_URL}/{thumb_id}/refer",
        data={"note": "let me in"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    with session_scope() as session:
        row = store.feedback_row(session, company_id=COMPANY, feedback_id=thumb_id)
        assert not store.is_referred(row)


def test_another_tenants_row_is_a_404_on_every_write(reviewer, complaint):
    with session_scope() as session:
        _user(session, user_id="USR-RIVAL", company_id=RIVAL, email="a@rival.example")
        theirs = store.record_bug_report(
            session,
            company_id=RIVAL,
            user_id="USR-RIVAL",
            title="not ours",
            detail="",
            surface="review",
        )
        their_id = theirs.id

    for path, payload in (
        ("triage", {"category": IMPROVEMENT_BUG, "title": "x", "priority": PRIORITY_NORMAL}),
        ("refer", {"note": "x"}),
        ("resolve", {"resolution": "x", "status": FEEDBACK_RESOLVED}),
    ):
        response = reviewer.post(
            f"{view.FEEDBACK_URL}/{their_id}/{path}",
            data=payload,
            follow_redirects=False,
        )
        assert response.status_code == 404, path

    with session_scope() as session:
        row = store.feedback_row(session, company_id=RIVAL, feedback_id=their_id)
        assert row.status == FEEDBACK_NEW
        assert session.query(ImprovementItem).count() == 0


def _thin_row(session, person_id: str, message_id: str = "CHM-2"):
    """A Feedback row in app/web/views/chat.py's shape: no clerk turn captured.

    Written straight to the row rather than through the store, because that is
    what the other writer does. The screen has to survive it.
    """
    row = Feedback(
        id="FBK-THIN",
        company_id=COMPANY,
        user_id=person_id,
        kind=FEEDBACK_KIND_FEEDBACK,
        rating=RATING_DOWN,
        title=None,
        comment="It made that up.",
        context=[
            {"role": CHAT_ROLE_PERSON, "text": "What moved on the large load docket?", "ordinal": 1}
        ],
        surface="project/PRJ-1",
        chat_message_id=message_id,
    )
    session.add(row)
    session.flush()
    return row


def test_an_answer_missing_from_the_snapshot_is_recovered_and_declared(reviewer):
    """A complaint with no answer beside it cannot be judged, so the screen fills it."""
    with session_scope() as session:
        person = session.query(User).filter(User.company_id == COMPANY).first()
        _conversation(session, user_id=person.id)
        _thin_row(session, person.id)

    body = reviewer.get(view.FEEDBACK_URL).text
    assert view.SNAPSHOT_THIN in body
    assert "Two changes since v2" in body
    assert "latest_changes" in body


def test_an_answer_that_cannot_be_recovered_is_said_to_be_missing(reviewer):
    """Never a complaint rendered against a question alone, as though that were all."""
    with session_scope() as session:
        person = session.query(User).filter(User.company_id == COMPANY).first()
        _thin_row(session, person.id, message_id="CHM-GONE")

    body = reviewer.get(view.FEEDBACK_URL).text
    assert view.ANSWER_MISSING in body
    assert view.SNAPSHOT_THIN not in body


def test_a_referred_row_is_offered_only_the_one_honest_outcome(reviewer, complaint):
    """The form must not offer a choice the store will refuse."""
    thumb_id, _ = complaint
    reviewer.post(
        f"{view.FEEDBACK_URL}/{thumb_id}/refer",
        data={"note": "check the second line"},
        follow_redirects=False,
    )
    body = reviewer.get(view.FEEDBACK_URL).text
    assert view.REFERRED_TRIAGE in body
    # The referral control is gone -- pressing it again could only be refused.
    assert body.count("What must somebody re-read") == 1  # the other row only
    # And the pair the form now posts is the pair the store accepts.
    response = reviewer.post(
        f"{view.FEEDBACK_URL}/{thumb_id}/triage",
        data={
            "category": IMPROVEMENT_BUG,
            "priority": PRIORITY_HIGH,
            "title": "Claim asserted on a citation that does not verify",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_scope() as session:
        assert session.query(ImprovementItem).one().priority == PRIORITY_HIGH


def test_a_snapshot_in_an_unknown_shape_says_so_and_keeps_the_queue_up(reviewer):
    """One malformed row must not take every other complaint down with it."""
    with session_scope() as session:
        person = session.query(User).filter(User.company_id == COMPANY).first()
        session.add(
            Feedback(
                id="FBK-ODD",
                company_id=COMPANY,
                user_id=person.id,
                kind=FEEDBACK_KIND_FEEDBACK,
                rating=RATING_DOWN,
                comment="wrong",
                context=["a bare string, not a turn"],
                surface="review",
            )
        )
        store.record_bug_report(
            session,
            company_id=COMPANY,
            user_id=person.id,
            title="an ordinary complaint",
            detail="",
            surface="review",
        )

    response = reviewer.get(view.FEEDBACK_URL)
    assert response.status_code == 200
    assert view.UNREADABLE_TURN in response.text
    assert "an ordinary complaint" in response.text


def test_a_full_snapshot_needs_no_recovery_notice(reviewer, complaint):
    body = reviewer.get(view.FEEDBACK_URL).text
    assert view.SNAPSHOT_THIN not in body
    assert view.ANSWER_MISSING not in body


def test_an_empty_queue_says_so(reviewer):
    body = reviewer.get(view.FEEDBACK_URL).text
    assert view.NOTHING_YET in body


def test_the_band_keeps_newest_first_inside_it(reviewer, seeded_accounts):
    """The ranking lifts a band. It must not shuffle the order inside one."""
    base = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
    with session_scope() as session:
        person = session.query(User).filter(User.company_id == COMPANY).first()
        for index in range(3):
            row = store.record_bug_report(
                session,
                company_id=COMPANY,
                user_id=person.id,
                title=f"plain complaint {index}",
                detail="the table scrolls sideways",
                surface="review",
            )
            row.created_at = base + timedelta(hours=index)

    body = reviewer.get(view.FEEDBACK_URL).text
    positions = [body.index(f"plain complaint {index}") for index in range(3)]
    assert positions == sorted(positions, reverse=True)


def test_a_capped_queue_says_it_is_capped(reviewer, seeded_accounts, monkeypatch):
    """A page that stops at the cap and says nothing reads as the end of the list."""
    monkeypatch.setattr(view, "QUEUE_LIMIT", 2)
    with session_scope() as session:
        person = session.query(User).filter(User.company_id == COMPANY).first()
        for index in range(4):
            store.record_bug_report(
                session,
                company_id=COMPANY,
                user_id=person.id,
                title=f"complaint {index}",
                detail="",
                surface="review",
            )

    body = reviewer.get(view.FEEDBACK_URL).text
    assert view.truncated_words(2, 4) in body


def test_a_whole_queue_says_nothing_about_a_cap(reviewer, complaint):
    body = reviewer.get(view.FEEDBACK_URL).text
    assert "Showing the newest" not in body


def test_a_refused_write_leaves_the_row_alone(reviewer, complaint):
    """The store refuses a referred row a low priority. Nothing partial survives."""
    thumb_id, _ = complaint
    assert (
        reviewer.post(
            f"{view.FEEDBACK_URL}/{thumb_id}/refer",
            data={"note": "check the second line"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    response = reviewer.post(
        f"{view.FEEDBACK_URL}/{thumb_id}/triage",
        data={
            "category": IMPROVEMENT_GUIDANCE,
            "priority": PRIORITY_LOW,
            "title": "quietly downgraded",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    with session_scope() as session:
        assert session.query(ImprovementItem).count() == 0
        row = store.feedback_row(session, company_id=COMPANY, feedback_id=thumb_id)
        assert row.improvement_item_id is None
        assert store.is_referred(row)
        assert verify_chain(session, COMPANY)
