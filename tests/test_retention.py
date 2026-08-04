"""The retention schedule, and the four claims it is only worth making if they hold.

These tests are written against app/state/retention.py before it exists, and
each of them pins one sentence the privacy page is about to say out loud.

**Every table has a window and a reason.** A schedule that covers most of the
schema is a schedule nobody can rely on, because the table it missed is the one
holding the address. The first test walks Base.metadata and refuses a table with
no rule. A table added next week fails it until somebody says how long its rows
live -- which is the point: the failure lands on the person who added the table,
not on whoever reads the privacy page a year later.

**A rule that names a mechanism names one that exists.** The whole argument of
this product is that a claim without something behind it should refuse to assert
itself. A retention schedule is a page full of claims about code, so the code is
resolved by import rather than trusted.

**The dry run predicts the destructive run.** Rule for rule, row for row. A dry
run that reports something other than what the real run does is worse than no
dry run, because somebody will approve a deletion on the strength of it.

**The audit row outlives the record it names.** That is the honest shape of
erasure against an append-only log, and it is tested from both sides: the row
survives and the chain still verifies, and the subject it names no longer
resolves. The address a failed sign-in recorded is pinned as unreachable,
because it is, and a test is the only place that stays true.

Rows are built directly rather than through the writers that normally make them.
These tests are about windows, audit rows and what survives, and routing a chat
turn through the agent to get a row would test the agent.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, select, text

from app.state.audit import AuditTamperError, event_count, record_event, verify_chain
from app.state.db import init_db, session_scope
from app.state.models import (
    FEEDBACK_KIND_FEEDBACK,
    INVITE_ACCEPTED,
    INVITE_KIND_HANDOFF,
    RATING_DOWN,
    SHARE_SUBJECT_CLAIM,
    AuditEvent,
    Base,
    ChatMessage,
    ChatSession,
    Feedback,
    Invitation,
    LoginSession,
    ShareLink,
    ShareOpen,
)
from app.state.retention import (
    ACTION_RETENTION_PURGED,
    ACTION_RETENTION_REDACTED,
    IP_PURGED,
    PURGE_ORDER,
    SCHEDULE,
    Rule,
    purge_all,
    purge_chat_transcripts,
    purge_feedback,
    purge_invitations,
    purge_login_sessions,
    purge_share_opens,
    redact_share_open_addresses,
    rule_for,
    rules_for_table,
)

COMPANY = "MEP"
RIVAL = "RIVAL"

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

# An address from the documentation range, so nothing here looks like a real
# person's connection. It is planted so the tests can prove where it does and
# does not end up.
VISITOR_IP = "203.0.113.7"
SIGNIN_IP = "198.51.100.22"


def _days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


# ---------------------------------------------------------------------------
# Rows, built by hand. See the module docstring.
# ---------------------------------------------------------------------------


def _session_row(session, *, sid: str, company: str, expires: datetime) -> LoginSession:
    row = LoginSession(
        id=sid,
        user_id="USR-1",
        company_id=company,
        token_hash=f"hash-{sid}",
        created_at=expires - timedelta(hours=12),
        expires_at=expires,
        last_seen_at=expires - timedelta(hours=1),
        ip=SIGNIN_IP,
        user_agent="Mozilla/5.0 (test)",
    )
    session.add(row)
    return row


def _share_with_open(
    session, *, link_id: str, company: str, opened: datetime, ip: str = VISITOR_IP
) -> ShareOpen:
    link = session.get(ShareLink, link_id)
    if link is None:
        link = ShareLink(
            id=link_id,
            company_id=company,
            token_hash=f"hash-{link_id}",
            subject_type=SHARE_SUBJECT_CLAIM,
            subject_id="CLM-1",
            created_by_user_id="USR-1",
            created_at=opened - timedelta(days=1),
            expires_at=opened + timedelta(days=7),
            open_count=0,
        )
        session.add(link)
    link.open_count = link.open_count + 1
    link.last_opened_at = opened
    row = ShareOpen(share_id=link_id, opened_at=opened, ip=ip, verified=True)
    session.add(row)
    session.flush()
    return row


def _conversation(
    session, *, chat_id: str, company: str, last_turn: datetime, turns: int = 2
) -> list[ChatMessage]:
    session.add(
        ChatSession(
            id=chat_id,
            company_id=company,
            user_id="USR-1",
            started_at=last_turn - timedelta(minutes=5),
            last_turn_at=last_turn,
        )
    )
    messages = []
    for ordinal in range(1, turns + 1):
        message = ChatMessage(
            id=f"{chat_id}-M{ordinal}",
            session_id=chat_id,
            company_id=company,
            ordinal=ordinal,
            role="person" if ordinal % 2 else "clerk",
            text=f"turn {ordinal} of {chat_id}",
            created_at=last_turn,
            withheld_count=None if ordinal % 2 else 0,
        )
        session.add(message)
        messages.append(message)
    session.flush()
    return messages


def _feedback_row(
    session, *, fid: str, company: str, created: datetime, message_id: str | None = None
) -> Feedback:
    row = Feedback(
        id=fid,
        company_id=company,
        user_id="USR-1",
        kind=FEEDBACK_KIND_FEEDBACK,
        rating=RATING_DOWN,
        comment="the answer read as if it had checked something it had not",
        surface="chat",
        chat_message_id=message_id,
        created_at=created,
    )
    session.add(row)
    session.flush()
    return row


def _invitation_row(session, *, iid: str, company: str, expires: datetime) -> Invitation:
    row = Invitation(
        id=iid,
        company_id=company,
        email="priya.nandakumar@meridian.example",
        invited_by_user_id="USR-1",
        invited_at=expires - timedelta(days=7),
        token_hash=f"hash-{iid}",
        expires_at=expires,
        accepted_at=expires - timedelta(days=6),
        status=INVITE_ACCEPTED,
        # A handoff, so the row names the item that justified it and satisfies
        # ck_invitations_handoff_names_its_item. A provisioning row would leave
        # both NULL; retention treats the two alike, because both hold an
        # address and both are measured from the moment they stopped working.
        kind=INVITE_KIND_HANDOFF,
        subject_type="obligation",
        subject_id="OBL-004",
    )
    session.add(row)
    session.flush()
    return row


def _everything_old(session, company: str = COMPANY) -> None:
    """One row per enforced rule, old enough that every window has passed."""
    _session_row(session, sid="LS-old", company=company, expires=_days_ago(400))
    _share_with_open(session, link_id="SHL-old", company=company, opened=_days_ago(400))
    _conversation(session, chat_id="CHT-old", company=company, last_turn=_days_ago(400))
    _feedback_row(session, fid="FB-old", company=company, created=_days_ago(400))
    _invitation_row(session, iid="INV-old", company=company, expires=_days_ago(400))
    session.flush()


# ---------------------------------------------------------------------------
# The schedule itself
# ---------------------------------------------------------------------------


def test_every_table_in_the_schema_has_a_window_and_a_reason():
    scheduled = {rule.table for rule in SCHEDULE}
    mapped = set(Base.metadata.tables)
    missing = sorted(mapped - scheduled)
    assert not missing, (
        "these tables hold rows and the schedule does not say how long for: "
        f"{missing}. A table with no window is a table nobody decided about, "
        "and the privacy page would be describing a schedule with a hole in it."
    )
    invented = sorted(scheduled - mapped)
    assert not invented, (
        f"the schedule names tables that do not exist: {invented}. A rule for a "
        "table nobody can find is a claim with nothing behind it."
    )


def test_every_rule_gives_a_reason_and_the_reason_is_a_sentence():
    for rule in SCHEDULE:
        assert rule.reason.strip(), f"{rule.id} states a window and no reason"
        assert len(rule.reason.split()) >= 6, (
            f"{rule.id}: '{rule.reason}' is a label, not a reason. A window with "
            "no reason is a number somebody will change without thinking."
        )
        assert rule.window.strip(), f"{rule.id} states no window"


def test_a_rule_that_names_a_mechanism_names_one_that_exists():
    for rule in SCHEDULE:
        if not rule.mechanism:
            assert rule.resolve_mechanism() is None
            continue
        assert callable(rule.resolve_mechanism()), (
            f"{rule.id} says {rule.mechanism} carries it out and that does not "
            "resolve to anything callable. A schedule naming a control nobody "
            "wrote is the defect this product argues against."
        )


def test_a_rule_with_no_mechanism_claims_no_clock_and_a_clock_implies_a_mechanism():
    for rule in SCHEDULE:
        if rule.days is not None:
            assert rule.mechanism, (
                f"{rule.id} states {rule.days} days and names nothing that runs. "
                "A number with nobody counting is a promise."
            )
        if not rule.mechanism:
            assert rule.days is None, f"{rule.id} counts days with no mechanism"
            assert not rule.clock, f"{rule.id} names a clock with no mechanism"


def test_every_purge_in_this_module_is_in_the_run_order():
    named = {
        rule.mechanism.rpartition(".")[2]
        for rule in SCHEDULE
        if rule.mechanism.startswith("app.state.retention.")
    }
    ordered = {function.__name__ for function in PURGE_ORDER}
    assert named == ordered, (
        "the schedule and the run order disagree about which purges exist: "
        f"schedule {sorted(named)}, run order {sorted(ordered)}. purge_all would "
        "then skip a rule the schedule promises."
    )


def test_the_audit_log_is_scheduled_as_never_purged():
    rules = rules_for_table("audit_events")
    assert len(rules) == 1
    assert rules[0].days is None
    assert rules[0].window.lower() == "never"
    assert rules[0].mechanism, "the refusal is a mechanism and must be named"


def test_the_audit_log_refuses_the_deletion_its_rule_describes():
    """The one rule in the schedule carried out by a refusal rather than a run."""
    init_db()
    with session_scope() as session:
        entry = record_event(
            session,
            company_id=COMPANY,
            actor="analyst@mep.example",
            action="claim.verified",
            subject_type="claim",
            subject_id="CLM-1",
            reason="citation matched source",
        )
        session.flush()
        with pytest.raises(AuditTamperError):
            session.delete(entry)
            session.flush()
        session.rollback()


def test_rule_for_refuses_a_name_nobody_wrote():
    with pytest.raises(KeyError):
        rule_for("login_sessions.forever")


# ---------------------------------------------------------------------------
# Dry run by default
# ---------------------------------------------------------------------------


def test_a_purge_is_a_dry_run_unless_the_caller_says_otherwise():
    init_db()
    with session_scope() as session:
        _everything_old(session)
        before = event_count(session, COMPANY)

        reports = purge_all(session, company_id=COMPANY, now=NOW)

        assert reports, "purge_all reported nothing at all"
        for report in reports:
            assert report.dry_run is True
            assert report.removed == 0
        assert session.get(LoginSession, "LS-old") is not None
        assert session.get(ChatSession, "CHT-old") is not None
        assert session.get(Feedback, "FB-old") is not None
        assert session.get(Invitation, "INV-old") is not None
        assert event_count(session, COMPANY) == before, (
            "a dry run wrote an audit row. Nothing happened, and a log saying "
            "otherwise is the log lying about a deletion."
        )


def test_a_dry_run_still_says_what_would_go():
    init_db()
    with session_scope() as session:
        _everything_old(session)
        reports = {r.rule.id: r for r in purge_all(session, company_id=COMPANY, now=NOW)}
        assert reports["login_sessions.expired"].matched == 1
        assert reports["chat_sessions.transcript"].matched == 1
        assert reports["feedback.stored"].matched == 1
        assert reports["invitations.spent"].matched == 1
        assert reports["share_opens.stored"].matched == 1


def test_a_non_boolean_flag_cannot_arm_the_destructive_run():
    """dry_run=0 is falsey and would otherwise delete. It is refused instead."""
    init_db()
    with session_scope() as session:
        _everything_old(session)
        for flag in (0, "", None, "no"):
            with pytest.raises(ValueError):
                purge_login_sessions(
                    session, company_id=COMPANY, now=NOW, dry_run=flag
                )
        assert session.get(LoginSession, "LS-old") is not None


def test_the_dry_run_predicts_the_destructive_run_rule_for_rule():
    init_db()
    with session_scope() as session:
        _everything_old(session)
        # A conversation somebody complained about, so the hold-back is in play
        # on both passes rather than only on one.
        messages = _conversation(
            session, chat_id="CHT-held", company=COMPANY, last_turn=_days_ago(400)
        )
        _feedback_row(
            session,
            fid="FB-recent",
            company=COMPANY,
            created=_days_ago(10),
            message_id=messages[0].id,
        )

        predicted = [
            (r.rule.id, r.matched, tuple(h.row_id for h in r.held))
            for r in purge_all(session, company_id=COMPANY, now=NOW)
        ]
        actual = [
            (r.rule.id, r.matched, tuple(h.row_id for h in r.held))
            for r in purge_all(session, company_id=COMPANY, now=NOW, dry_run=False)
        ]
        assert predicted == actual


# ---------------------------------------------------------------------------
# The destructive run
# ---------------------------------------------------------------------------


def test_the_purge_writes_its_audit_row_before_it_deletes():
    """Observed from inside the delete, not inferred from the order of the code."""
    init_db()
    seen: list[int] = []

    def _watch(mapper, connection, target):
        seen.append(
            connection.execute(
                text("SELECT COUNT(*) FROM audit_events WHERE action = :a"),
                {"a": ACTION_RETENTION_PURGED},
            ).scalar_one()
        )

    event.listen(LoginSession, "after_delete", _watch)
    try:
        with session_scope() as session:
            _session_row(session, sid="LS-old", company=COMPANY, expires=_days_ago(400))
            session.flush()
            purge_login_sessions(session, company_id=COMPANY, now=NOW, dry_run=False)
    finally:
        event.remove(LoginSession, "after_delete", _watch)

    assert seen == [1], (
        "the row went before the log did. A deletion nobody recorded cannot be "
        "told apart from a breach."
    )


def test_the_audit_row_names_the_rule_the_count_and_the_cutoff():
    init_db()
    with session_scope() as session:
        _session_row(session, sid="LS-old", company=COMPANY, expires=_days_ago(400))
        session.flush()
        report = purge_login_sessions(
            session, company_id=COMPANY, now=NOW, dry_run=False
        )
        assert report.removed == 1

        entry = session.execute(
            select(AuditEvent)
            .where(AuditEvent.company_id == COMPANY)
            .where(AuditEvent.action == ACTION_RETENTION_PURGED)
        ).scalar_one()
        assert entry.subject_id == "login_sessions.expired"
        assert "login_sessions" in entry.reason
        assert "1" in entry.reason
        assert report.cutoff.isoformat() in entry.reason


def test_the_audit_row_carries_no_address_and_no_name_out_of_the_rows_it_purged():
    init_db()
    with session_scope() as session:
        _everything_old(session)
        purge_all(session, company_id=COMPANY, now=NOW, dry_run=False)

        rows = (
            session.execute(
                select(AuditEvent).where(AuditEvent.company_id == COMPANY)
            )
            .scalars()
            .all()
        )
        written = " ".join(f"{r.actor} {r.reason} {r.subject_id}" for r in rows)
        for secret in (VISITOR_IP, SIGNIN_IP, "priya.nandakumar@meridian.example"):
            assert secret not in written, (
                f"{secret} was copied into an append-only log by the code that "
                "deleted it. The log cannot be redacted afterwards, so a purge "
                "that quotes what it purged has moved the data rather than "
                "removed it."
            )


def test_running_the_purge_twice_is_safe_and_the_second_run_logs_nothing():
    init_db()
    with session_scope() as session:
        _everything_old(session)
        first = purge_all(session, company_id=COMPANY, now=NOW, dry_run=False)
        assert sum(r.removed for r in first) > 0
        after_first = event_count(session, COMPANY)

        second = purge_all(session, company_id=COMPANY, now=NOW, dry_run=False)
        assert all(r.matched == 0 for r in second)
        assert all(r.removed == 0 for r in second)
        assert event_count(session, COMPANY) == after_first, (
            "the second pass wrote a row saying it purged nothing. A log of "
            "non-events is a log nobody reads."
        )
        assert verify_chain(session, COMPANY) is True


def test_a_row_inside_its_window_is_left_alone():
    init_db()
    with session_scope() as session:
        _session_row(session, sid="LS-fresh", company=COMPANY, expires=_days_ago(89))
        _session_row(session, sid="LS-old", company=COMPANY, expires=_days_ago(91))
        session.flush()
        report = purge_login_sessions(
            session, company_id=COMPANY, now=NOW, dry_run=False
        )
        assert report.removed == 1
        assert session.get(LoginSession, "LS-fresh") is not None
        assert session.get(LoginSession, "LS-old") is None


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_an_unscoped_purge_is_refused_rather_than_run_across_every_tenant():
    init_db()
    with session_scope() as session:
        for run in PURGE_ORDER:
            with pytest.raises(ValueError):
                run(session, company_id="", now=NOW, dry_run=False)
        with pytest.raises(ValueError):
            purge_all(session, company_id="", now=NOW, dry_run=False)


def test_one_company_s_purge_does_not_reach_another_s_rows():
    init_db()
    with session_scope() as session:
        _everything_old(session, company=COMPANY)
        _session_row(session, sid="LS-rival", company=RIVAL, expires=_days_ago(400))
        _share_with_open(
            session, link_id="SHL-rival", company=RIVAL, opened=_days_ago(400)
        )
        _conversation(
            session, chat_id="CHT-rival", company=RIVAL, last_turn=_days_ago(400)
        )
        _feedback_row(session, fid="FB-rival", company=RIVAL, created=_days_ago(400))
        _invitation_row(session, iid="INV-rival", company=RIVAL, expires=_days_ago(400))
        session.flush()

        purge_all(session, company_id=COMPANY, now=NOW, dry_run=False)

        assert session.get(LoginSession, "LS-rival") is not None
        assert session.get(ChatSession, "CHT-rival") is not None
        assert session.get(Feedback, "FB-rival") is not None
        assert session.get(Invitation, "INV-rival") is not None
        rival_opens = (
            session.execute(
                select(ShareOpen).where(ShareOpen.share_id == "SHL-rival")
            )
            .scalars()
            .all()
        )
        assert len(rival_opens) == 1
        assert rival_opens[0].ip == VISITOR_IP
        assert event_count(session, RIVAL) == 0


# ---------------------------------------------------------------------------
# What survives, and what does not
# ---------------------------------------------------------------------------


def test_the_audit_row_naming_a_purged_record_survives_and_points_at_nothing():
    """The whole design, in one test.

    A decision recorded about a chat turn stays in the log after the turn is
    gone. The reader still learns that the record existed, when, who touched it
    and what they did. They cannot learn what it said.
    """
    init_db()
    with session_scope() as session:
        messages = _conversation(
            session, chat_id="CHT-old", company=COMPANY, last_turn=_days_ago(400)
        )
        subject = messages[0].id
        record_event(
            session,
            company_id=COMPANY,
            actor="analyst@mep.example",
            action="claim.escalated",
            subject_type="chat_message",
            subject_id=subject,
            reason="the reply asserted a claim whose citation had not been checked",
        )
        session.flush()

        purge_chat_transcripts(session, company_id=COMPANY, now=NOW, dry_run=False)

        assert session.get(ChatMessage, subject) is None, "the turn should be gone"

        entry = session.execute(
            select(AuditEvent)
            .where(AuditEvent.company_id == COMPANY)
            .where(AuditEvent.action == "claim.escalated")
        ).scalar_one()
        # What a reader still learns.
        assert entry.subject_id == subject
        assert entry.subject_type == "chat_message"
        assert entry.actor == "analyst@mep.example"
        assert entry.occurred_at is not None
        # What they cannot: the words. Nothing in the log holds them.
        assert "turn 1 of CHT-old" not in entry.reason
        # And the chain is still evidence.
        assert verify_chain(session, COMPANY) is True


def test_the_address_a_failed_sign_in_recorded_cannot_be_reached_by_any_purge():
    """The limit, pinned rather than left for a buyer to find."""
    init_db()
    with session_scope() as session:
        record_event(
            session,
            company_id=COMPANY,
            actor="person:stranger@elsewhere.example",
            action="login.failed",
            subject_type="user",
            subject_id="stranger@elsewhere.example",
            reason="no account for that address",
            ip=SIGNIN_IP,
        )
        session.flush()
        _everything_old(session)

        purge_all(session, company_id=COMPANY, now=NOW, dry_run=False)

        entry = session.execute(
            select(AuditEvent)
            .where(AuditEvent.company_id == COMPANY)
            .where(AuditEvent.action == "login.failed")
        ).scalar_one()
        assert entry.actor == "person:stranger@elsewhere.example"
        assert entry.subject_id == "stranger@elsewhere.example"
        assert entry.ip == SIGNIN_IP
        assert verify_chain(session, COMPANY) is True


def test_a_redacted_address_says_it_was_taken_out_rather_than_never_recorded():
    init_db()
    with session_scope() as session:
        old = _share_with_open(
            session, link_id="SHL-1", company=COMPANY, opened=_days_ago(120)
        )
        fresh = _share_with_open(
            session, link_id="SHL-1", company=COMPANY, opened=_days_ago(10)
        )
        blank = _share_with_open(
            session, link_id="SHL-1", company=COMPANY, opened=_days_ago(200), ip=""
        )
        session.flush()

        report = redact_share_open_addresses(
            session, company_id=COMPANY, now=NOW, dry_run=False
        )
        assert report.removed == 1
        assert old.ip == IP_PURGED
        assert old.ip != "", (
            "a blank would read as 'the server recorded none', which models.py "
            "says is a gap in the caller. A deletion has to say it was one."
        )
        assert fresh.ip == VISITOR_IP
        assert blank.ip == ""
        # The row itself is evidence and stays.
        assert old.verified is True
        assert old.opened_at is not None

        entry = session.execute(
            select(AuditEvent)
            .where(AuditEvent.company_id == COMPANY)
            .where(AuditEvent.action == ACTION_RETENTION_REDACTED)
        ).scalar_one()
        assert entry.subject_id == "share_opens.address"


def test_a_redaction_run_twice_finds_nothing_the_second_time():
    init_db()
    with session_scope() as session:
        _share_with_open(
            session, link_id="SHL-1", company=COMPANY, opened=_days_ago(120)
        )
        session.flush()
        redact_share_open_addresses(
            session, company_id=COMPANY, now=NOW, dry_run=False
        )
        again = redact_share_open_addresses(
            session, company_id=COMPANY, now=NOW, dry_run=False
        )
        assert again.matched == 0
        assert again.removed == 0


def test_purging_an_open_leaves_the_link_s_counter_alone_and_the_report_says_so():
    init_db()
    with session_scope() as session:
        _share_with_open(
            session, link_id="SHL-1", company=COMPANY, opened=_days_ago(400)
        )
        session.flush()
        link = session.get(ShareLink, "SHL-1")
        assert link.open_count == 1

        report = purge_share_opens(
            session, company_id=COMPANY, now=NOW, dry_run=False
        )
        assert report.removed == 1
        assert link.open_count == 1, (
            "decrementing the counter would erase the fact that the link was "
            "opened, which is the one thing worth keeping about it"
        )
        assert link.last_opened_at is not None
        assert "open_count" in report.detail


def test_a_transcript_with_a_complaint_against_it_is_held_back_and_says_which_row():
    init_db()
    with session_scope() as session:
        messages = _conversation(
            session, chat_id="CHT-held", company=COMPANY, last_turn=_days_ago(400)
        )
        _conversation(
            session, chat_id="CHT-free", company=COMPANY, last_turn=_days_ago(400)
        )
        _feedback_row(
            session,
            fid="FB-recent",
            company=COMPANY,
            created=_days_ago(10),
            message_id=messages[0].id,
        )
        session.flush()

        report = purge_chat_transcripts(
            session, company_id=COMPANY, now=NOW, dry_run=False
        )
        assert report.removed == 1
        assert session.get(ChatSession, "CHT-free") is None
        assert session.get(ChatSession, "CHT-held") is not None
        assert [h.row_id for h in report.held] == ["CHT-held"]
        assert "FB-recent" in report.held[0].reason


def test_the_held_back_transcript_goes_once_the_complaint_has_gone():
    init_db()
    with session_scope() as session:
        messages = _conversation(
            session, chat_id="CHT-held", company=COMPANY, last_turn=_days_ago(400)
        )
        _feedback_row(
            session,
            fid="FB-old",
            company=COMPANY,
            created=_days_ago(400),
            message_id=messages[0].id,
        )
        session.flush()

        first = purge_all(session, company_id=COMPANY, now=NOW, dry_run=False)
        chat = next(r for r in first if r.rule.table == "chat_sessions")
        assert chat.removed == 0
        assert session.get(Feedback, "FB-old") is None

        second = purge_all(session, company_id=COMPANY, now=NOW, dry_run=False)
        chat = next(r for r in second if r.rule.table == "chat_sessions")
        assert chat.removed == 1
        assert session.get(ChatSession, "CHT-held") is None


def test_purging_a_conversation_takes_its_turns_with_it():
    init_db()
    with session_scope() as session:
        _conversation(
            session,
            chat_id="CHT-old",
            company=COMPANY,
            last_turn=_days_ago(400),
            turns=4,
        )
        session.flush()
        report = purge_chat_transcripts(
            session, company_id=COMPANY, now=NOW, dry_run=False
        )
        assert report.removed == 1
        assert "4" in report.detail
        left = (
            session.execute(
                select(ChatMessage).where(ChatMessage.session_id == "CHT-old")
            )
            .scalars()
            .all()
        )
        assert left == []


def test_a_conversation_nobody_ever_spoke_in_is_measured_from_when_it_opened():
    init_db()
    with session_scope() as session:
        session.add(
            ChatSession(
                id="CHT-silent",
                company_id=COMPANY,
                user_id="USR-1",
                started_at=_days_ago(400),
                last_turn_at=None,
            )
        )
        session.flush()
        report = purge_chat_transcripts(
            session, company_id=COMPANY, now=NOW, dry_run=False
        )
        assert report.removed == 1


def test_feedback_and_invitations_go_on_their_own_clocks():
    init_db()
    with session_scope() as session:
        _feedback_row(session, fid="FB-old", company=COMPANY, created=_days_ago(400))
        _feedback_row(session, fid="FB-new", company=COMPANY, created=_days_ago(200))
        _invitation_row(session, iid="INV-old", company=COMPANY, expires=_days_ago(120))
        _invitation_row(session, iid="INV-new", company=COMPANY, expires=_days_ago(30))
        session.flush()

        assert purge_feedback(
            session, company_id=COMPANY, now=NOW, dry_run=False
        ).removed == 1
        assert purge_invitations(
            session, company_id=COMPANY, now=NOW, dry_run=False
        ).removed == 1
        assert session.get(Feedback, "FB-new") is not None
        assert session.get(Invitation, "INV-new") is not None


# ---------------------------------------------------------------------------
# The distinction the privacy page turns on
# ---------------------------------------------------------------------------


def test_nothing_in_the_product_calls_any_of_this_yet():
    """The claim on the privacy page, checked rather than remembered.

    When somebody wires a scheduler, this test fails. That is the point: the
    page says "the functions exist and nothing runs them", and the day that
    stops being true the page has to change in the same commit.
    """
    root = Path(__file__).resolve().parent.parent
    # An import of this module, not the word. app/seed.py writes a demo
    # obligation about a records retention period, which is corpus text and not
    # a caller, and a test that cannot tell the two apart would fail for a
    # reason nobody could act on.
    imports = re.compile(r"^\s*(?:from|import)\s+[\w. ,]*\bretention\b", re.MULTILINE)
    callers = []
    for path in sorted((root / "app").rglob("*.py")):
        if path.name == "retention.py":
            continue
        if imports.search(path.read_text(encoding="utf-8")):
            callers.append(str(path.relative_to(root)))
    assert callers == [], (
        f"{callers} now reach the retention module. If a scheduler was wired up, "
        "deploy/site/privacy.html still says nothing runs these, and that "
        "sentence is now false. Change the page in this commit."
    )


def test_the_privacy_page_states_the_schedule_and_that_nothing_runs_it():
    root = Path(__file__).resolve().parent.parent
    page = (root / "deploy" / "site" / "privacy.html").read_text(encoding="utf-8")
    assert "How long each thing is kept" in page
    assert "app/state/retention.py" in page
    assert "nothing calls them" in page
    for rule in SCHEDULE:
        if rule.days is None:
            continue
        assert f"{rule.days} days" in page, (
            f"{rule.id} runs on {rule.days} days and the page does not say so"
        )


def test_a_rule_is_frozen_so_a_window_cannot_be_moved_at_a_call_site():
    rule = rule_for("login_sessions.expired")
    assert isinstance(rule, Rule)
    with pytest.raises(Exception):
        rule.days = 3650
