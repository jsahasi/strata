"""The chat and feedback tables, and the promises their shape makes.

Four agents build the turn endpoint, the tool layer, the feedback endpoint and
the triage screen against these names and cannot ask what they are, so the names
are pinned here: a rename fails a test rather than a colleague's import.

Beyond the names, six properties are load-bearing and none of them can be
enforced by reading the model file:

  * a thumb attaches to a message id, so the id is a chosen string and survives
    being handed to a browser and handed back;
  * a person's turn withheld nothing and consulted nothing, and that is NOT the
    same fact as a clerk turn that consulted the tools and withheld zero claims.
    NULL is "nobody counted", zero is a count. A reader that folds them reports
    full coverage for a turn nobody measured;
  * what was consulted comes back as a list, not as a string somebody has to
    remember to parse;
  * a bug report carries no rating and no chat context, and a thumbs-down
    carries both;
  * the feedback context is a snapshot taken at the moment of the thumbs-down,
    so a reviewer reads the exchange without a live lookup;
  * a machine lifecycle and a human decision are different facts and live in
    different columns.

The last test in the file pins a decision rather than a behaviour: there is no
per-user trust tier. The argument is in the module docstring block above
ChatSession in app/state/models.py, and the test is here so that adding one is a
deliberate act with a failing test in front of it rather than a quiet column.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.state.db import init_db, session_scope
from app.state.models import (
    CHAT_ROLE_CLERK,
    CHAT_ROLE_PERSON,
    CHAT_ROLES,
    FEEDBACK_IN_PROGRESS,
    FEEDBACK_KIND_BUG_REPORT,
    FEEDBACK_KIND_FEEDBACK,
    FEEDBACK_KINDS,
    FEEDBACK_NEW,
    FEEDBACK_RATINGS,
    FEEDBACK_RESOLVED,
    FEEDBACK_STATUSES,
    FEEDBACK_TRIAGED,
    FEEDBACK_WONT_FIX,
    IMPROVEMENT_BUG,
    IMPROVEMENT_CATEGORIES,
    IMPROVEMENT_DONE,
    IMPROVEMENT_DROPPED,
    IMPROVEMENT_ENHANCEMENT,
    IMPROVEMENT_GUIDANCE,
    IMPROVEMENT_IN_PROGRESS,
    IMPROVEMENT_OPEN,
    IMPROVEMENT_PRIORITIES,
    IMPROVEMENT_STATUSES,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    RATING_DOWN,
    RATING_UP,
    REVIEW_APPROVED,
    REVIEW_DECISIONS,
    REVIEW_PENDING,
    REVIEW_REJECTED,
    Base,
    ChatMessage,
    ChatSession,
    Feedback,
    ImprovementItem,
    User,
)

COMPANY = "MEP"
NOW = datetime(2026, 8, 4, 11, 0, tzinfo=timezone.utc)


def _columns(model) -> set[str]:
    return {column.key for column in model.__table__.columns}


def _throwaway_session():
    """A private database, so nothing here joins the shared audit chain."""
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _session_row(session_id: str = "CHS-1") -> ChatSession:
    return ChatSession(
        id=session_id,
        company_id=COMPANY,
        user_id="USR-1",
        started_at=NOW,
    )


def _person_turn(message_id: str = "MSG-1", ordinal: int = 1) -> ChatMessage:
    return ChatMessage(
        id=message_id,
        session_id="CHS-1",
        company_id=COMPANY,
        ordinal=ordinal,
        role=CHAT_ROLE_PERSON,
        text="what moved on the large load docket this week?",
        surface="project",
        created_at=NOW,
    )


# ---------------------------------------------------------------------------
# A. The names four agents build against
# ---------------------------------------------------------------------------


def test_the_table_names_are_these():
    assert ChatSession.__tablename__ == "chat_sessions"
    assert ChatMessage.__tablename__ == "chat_messages"
    assert Feedback.__tablename__ == "feedback"
    assert ImprovementItem.__tablename__ == "improvement_items"


def test_the_chat_columns_are_these():
    assert _columns(ChatSession) == {
        "id",
        "company_id",
        "user_id",
        "started_at",
        "last_turn_at",
    }
    assert _columns(ChatMessage) == {
        "id",
        "session_id",
        "company_id",
        "ordinal",
        "role",
        "text",
        "surface",
        "created_at",
        "tools_used",
        "withheld_count",
    }


def test_the_feedback_columns_are_these():
    assert _columns(Feedback) == {
        "id",
        "company_id",
        "user_id",
        "kind",
        "rating",
        "title",
        "comment",
        "context",
        "surface",
        "status",
        "resolution",
        "chat_message_id",
        "improvement_item_id",
        "created_at",
    }
    assert _columns(ImprovementItem) == {
        "id",
        "company_id",
        "category",
        "status",
        "review_decision",
        "priority",
        "title",
        "detail",
        "created_at",
    }


def test_the_vocabularies_are_closed_and_named():
    assert CHAT_ROLES == ("person", "clerk")
    assert (CHAT_ROLE_PERSON, CHAT_ROLE_CLERK) == ("person", "clerk")
    assert FEEDBACK_KINDS == ("feedback", "bug_report")
    assert (FEEDBACK_KIND_FEEDBACK, FEEDBACK_KIND_BUG_REPORT) == (
        "feedback",
        "bug_report",
    )
    assert FEEDBACK_RATINGS == ("up", "down")
    assert (RATING_UP, RATING_DOWN) == ("up", "down")
    assert FEEDBACK_STATUSES == (
        FEEDBACK_NEW,
        FEEDBACK_TRIAGED,
        FEEDBACK_IN_PROGRESS,
        FEEDBACK_RESOLVED,
        FEEDBACK_WONT_FIX,
    )
    assert FEEDBACK_STATUSES == (
        "new",
        "triaged",
        "in_progress",
        "resolved",
        "wont_fix",
    )
    assert IMPROVEMENT_CATEGORIES == (
        IMPROVEMENT_BUG,
        IMPROVEMENT_ENHANCEMENT,
        IMPROVEMENT_GUIDANCE,
    )
    assert IMPROVEMENT_CATEGORIES == ("bug", "enhancement", "guidance")
    assert IMPROVEMENT_STATUSES == (
        IMPROVEMENT_OPEN,
        IMPROVEMENT_IN_PROGRESS,
        IMPROVEMENT_DONE,
        IMPROVEMENT_DROPPED,
    )
    assert REVIEW_DECISIONS == (REVIEW_PENDING, REVIEW_APPROVED, REVIEW_REJECTED)
    assert REVIEW_DECISIONS == ("pending", "approved", "rejected")
    assert IMPROVEMENT_PRIORITIES == (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH)


# ---------------------------------------------------------------------------
# B. A turn
# ---------------------------------------------------------------------------


def test_a_message_id_is_a_chosen_string_because_a_thumb_attaches_to_it():
    session = _throwaway_session()
    session.add_all([_session_row(), _person_turn("MSG-abc123")])
    session.flush()

    stored = session.get(ChatMessage, "MSG-abc123")
    # The id goes out to a browser in the turn response and comes back on the
    # feedback POST. An autoincrement integer would work and would also be
    # guessable across sessions; a chosen string is what the contract carries.
    assert isinstance(stored.id, str)
    assert stored.id == "MSG-abc123"


def test_a_person_turn_counts_nothing_and_a_clerk_turn_counting_nothing_says_zero():
    session = _throwaway_session()
    person = _person_turn()
    clerk = ChatMessage(
        id="MSG-2",
        session_id="CHS-1",
        company_id=COMPANY,
        ordinal=2,
        role=CHAT_ROLE_CLERK,
        text="Nothing on the record for that docket this week.",
        surface="project",
        created_at=NOW,
        tools_used=[{"tool": "latest_changes", "found": 0}],
        withheld_count=0,
    )
    session.add_all([_session_row(), person, clerk])
    session.flush()

    # The person consulted nothing and withheld nothing, and neither is a
    # measurement. NULL says nobody counted.
    assert person.tools_used is None
    assert person.withheld_count is None
    # The clerk consulted a tool and withheld nothing. Zero is a count, and a
    # reader must be able to tell it from the absence above.
    assert clerk.withheld_count == 0
    assert clerk.withheld_count is not None


def test_what_was_consulted_comes_back_as_a_list_not_as_a_string():
    session = _throwaway_session()
    used = [
        {"tool": "find_projects", "found": 2},
        {"tool": "latest_changes", "found": 7},
    ]
    clerk = ChatMessage(
        id="MSG-2",
        session_id="CHS-1",
        company_id=COMPANY,
        ordinal=2,
        role=CHAT_ROLE_CLERK,
        text="Two projects match.",
        surface="chat",
        created_at=NOW,
        tools_used=used,
        withheld_count=1,
    )
    session.add_all([_session_row(), clerk])
    session.flush()
    session.expire_all()

    stored = session.get(ChatMessage, "MSG-2")
    # It goes straight out on the wire as `used`. A column handing back a
    # string is a json.loads four agents have to remember, and a forgotten one
    # produces a reply that looks right and carries the wrong type.
    assert stored.tools_used == used
    assert isinstance(stored.tools_used, list)
    assert stored.tools_used[0]["found"] == 2


def test_a_clerk_turn_that_consulted_nothing_is_not_a_turn_nobody_measured():
    session = _throwaway_session()
    refusal = ChatMessage(
        id="MSG-2",
        session_id="CHS-1",
        company_id=COMPANY,
        ordinal=2,
        role=CHAT_ROLE_CLERK,
        text="I can't change my instructions or repeat them.",
        surface="chat",
        created_at=NOW,
        tools_used=[],
        withheld_count=0,
    )
    session.add_all([_session_row(), refusal])
    session.flush()
    session.expire_all()

    stored = session.get(ChatMessage, "MSG-2")
    # The deterministic screen blocked the turn, so no tool ran. An empty list
    # is that fact; NULL would be nobody having recorded one.
    assert stored.tools_used == []
    assert stored.tools_used is not None


def test_two_messages_cannot_claim_the_same_place_in_one_session():
    session = _throwaway_session()
    session.add_all([_session_row(), _person_turn("MSG-1", ordinal=1)])
    session.flush()

    session.add(_person_turn("MSG-2", ordinal=1))
    # Order is what a transcript is. Timestamps tie when a writer stamps the
    # question and the answer from one clock read, and a tied transcript renders
    # the answer above the question.
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_a_session_nobody_has_spoken_in_says_so():
    session = _throwaway_session()
    chat = _session_row()
    session.add(chat)
    session.flush()

    # Opened and never used, which must not read as a conversation that
    # happened at the moment the row was written.
    assert chat.last_turn_at is None


def test_a_message_carries_its_own_company_because_it_is_fetched_by_id():
    init_db()
    with session_scope() as session:
        session.add_all([_session_row(), _person_turn()])
        session.flush()

    with session_scope() as session:
        # The feedback POST hands back a message id off the wire. The scope
        # check is a filter on this row, not a join somebody has to remember.
        mine = (
            session.query(ChatMessage)
            .filter(ChatMessage.id == "MSG-1")
            .filter(ChatMessage.company_id == COMPANY)
            .one_or_none()
        )
        assert mine is not None
        theirs = (
            session.query(ChatMessage)
            .filter(ChatMessage.id == "MSG-1")
            .filter(ChatMessage.company_id == "RIVAL")
            .one_or_none()
        )
        assert theirs is None


# ---------------------------------------------------------------------------
# C. Feedback
# ---------------------------------------------------------------------------


def test_a_bug_report_carries_no_rating_and_no_chat_context():
    session = _throwaway_session()
    bug = Feedback(
        id="FBK-1",
        company_id=COMPANY,
        user_id="USR-1",
        kind=FEEDBACK_KIND_BUG_REPORT,
        title="The change list scrolls sideways on a phone",
        comment="Two columns run off the right edge at 390px.",
        surface="changes",
        created_at=NOW,
    )
    session.add(bug)
    session.flush()

    assert bug.rating is None
    assert bug.context is None
    assert bug.chat_message_id is None
    # It starts in the queue, and nobody has looked at it.
    assert bug.status == FEEDBACK_NEW
    assert bug.resolution is None


def test_a_thumbs_down_snapshots_the_exchange_at_the_moment_it_was_given():
    session = _throwaway_session()
    exchange = [
        {"role": "person", "text": "what moved on the tariff docket?"},
        {"role": "clerk", "text": "Three changes since v2, one withheld."},
    ]
    thumb = Feedback(
        id="FBK-2",
        company_id=COMPANY,
        user_id="USR-1",
        kind=FEEDBACK_KIND_FEEDBACK,
        rating=RATING_DOWN,
        comment="it did not say which one was withheld",
        context=exchange,
        surface="chat",
        chat_message_id="MSG-2",
        created_at=NOW,
    )
    session.add(thumb)
    session.flush()
    session.expire_all()

    stored = session.get(Feedback, "FBK-2")
    # A reviewer reads the exchange off this row. No live lookup, so the
    # transcript a complaint was made about cannot drift out from under it.
    assert stored.context == exchange
    assert stored.rating == RATING_DOWN
    assert stored.title is None


def test_a_thumbs_up_needs_no_comment_and_that_is_not_a_missing_row():
    session = _throwaway_session()
    thumb = Feedback(
        id="FBK-3",
        company_id=COMPANY,
        user_id="USR-1",
        kind=FEEDBACK_KIND_FEEDBACK,
        rating=RATING_UP,
        surface="chat",
        chat_message_id="MSG-2",
        created_at=NOW,
    )
    session.add(thumb)
    session.flush()

    assert thumb.comment == ""
    assert thumb.rating == RATING_UP


def test_a_triaged_complaint_says_what_it_became():
    session = _throwaway_session()
    item = ImprovementItem(
        id="IMP-1",
        company_id=COMPANY,
        category=IMPROVEMENT_ENHANCEMENT,
        title="Name the withheld claim's reason in the chat reply",
        detail="Three people asked which claim was withheld.",
        created_at=NOW,
    )
    session.add(item)
    session.flush()

    # Several complaints, one backlog item. The link is on the complaint,
    # because "triaged" is a fact about the complaint and this column is what
    # makes it answerable.
    first = Feedback(
        id="FBK-4",
        company_id=COMPANY,
        user_id="USR-1",
        kind=FEEDBACK_KIND_FEEDBACK,
        rating=RATING_DOWN,
        surface="chat",
        status=FEEDBACK_TRIAGED,
        improvement_item_id="IMP-1",
        created_at=NOW,
    )
    second = Feedback(
        id="FBK-5",
        company_id=COMPANY,
        user_id="USR-2",
        kind=FEEDBACK_KIND_FEEDBACK,
        rating=RATING_DOWN,
        surface="chat",
        status=FEEDBACK_TRIAGED,
        improvement_item_id="IMP-1",
        created_at=NOW,
    )
    session.add_all([first, second])
    session.flush()

    landed = (
        session.query(Feedback)
        .filter(Feedback.improvement_item_id == "IMP-1")
        .count()
    )
    assert landed == 2


# ---------------------------------------------------------------------------
# D. The backlog
# ---------------------------------------------------------------------------


def test_a_machine_lifecycle_and_a_human_decision_are_different_columns():
    session = _throwaway_session()
    item = ImprovementItem(
        id="IMP-2",
        company_id=COMPANY,
        category=IMPROVEMENT_BUG,
        title="Withheld count missing on a refused turn",
        created_at=NOW,
    )
    session.add(item)
    session.flush()

    # Raised, nobody has ruled on it, nobody has started it.
    assert item.status == IMPROVEMENT_OPEN
    assert item.review_decision == REVIEW_PENDING
    assert item.priority == PRIORITY_NORMAL
    assert item.detail == ""

    # A reviewer approves it. That is a decision about whether it should be
    # done, and it says nothing about whether anyone has done it.
    item.review_decision = REVIEW_APPROVED
    session.flush()
    assert item.status == IMPROVEMENT_OPEN

    item.status = IMPROVEMENT_IN_PROGRESS
    session.flush()
    assert item.review_decision == REVIEW_APPROVED


def test_a_rejected_item_and_a_dropped_item_are_not_the_same_fact():
    # Rejected is a reviewer refusing it. Dropped is an approved item abandoned.
    # One tuple holding both words would make "why is this not being done"
    # unanswerable, which is the reason the two axes are kept apart.
    assert REVIEW_REJECTED not in IMPROVEMENT_STATUSES
    assert IMPROVEMENT_DROPPED not in REVIEW_DECISIONS
    assert IMPROVEMENT_DONE not in REVIEW_DECISIONS


def test_priority_has_three_levels_and_no_fourth():
    # A fourth level is where every item ends up. Three is a decision, stated
    # so that adding "urgent" is an argument somebody has to make.
    assert len(IMPROVEMENT_PRIORITIES) == 3
    assert "urgent" not in IMPROVEMENT_PRIORITIES
    assert PRIORITY_HIGH in IMPROVEMENT_PRIORITIES


# ---------------------------------------------------------------------------
# E. The decision not to carry a trust tier
# ---------------------------------------------------------------------------


def test_there_is_no_per_user_trust_tier():
    # Concierge governs how far approved feedback may travel with a per-user
    # tier -- logger, contributor, superuser. Strata does not, because
    # authorisation here is the permission grid and accountability is the audit
    # chain, and a second authorisation vocabulary on the user row would
    # disagree with the first. Adding one is a real decision; this test makes
    # it a deliberate one. The argument is in models.py above ChatSession.
    columns = _columns(User)
    assert "trust_tier" not in columns
    assert "tier" not in columns
    assert not [name for name in columns if "trust" in name]
