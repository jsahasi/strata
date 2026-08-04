"""The digest migration: two hashing schemes in one chain, and neither forged.

The risk this file exists to hold down is not that attribution is missing. It is
that adding attribution quietly invalidates every row written before it. A
tamper-evident log that cannot verify its own history is worse than no log,
because a real alarm and a false one look identical, and after the first false
one nobody reads either.

So the rules under test are:

  * scheme 1 is frozen -- a hash computed before the change still matches, byte
    for byte, and there is a golden value here to prove it;
  * a chain that starts under scheme 1 and continues under scheme 2 verifies end
    to end;
  * attribution lives inside the digest, so renaming the actor on a row breaks
    the chain rather than rewriting history with a valid hash;
  * an unknown scheme refuses to verify instead of guessing one;
  * a failed login is recorded and carries no password material.
"""

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.state.audit import (
    ACTION_LOGIN_FAILED,
    ACTION_LOGIN_SUCCEEDED,
    ACTION_PROJECT_CREATED,
    ACTOR_MODEL,
    ACTOR_SYSTEM,
    ACTOR_USER,
    CURRENT_DIGEST_VERSION,
    DIGEST_V1,
    DIGEST_V2,
    AuditTamperError,
    _digest,
    _digest_for_row,
    migrate_audit_schema,
    record_event,
    verify_chain,
)
from app.state.db import init_db, session_scope
from app.state.models import AuditEvent

# ---------------------------------------------------------------------------
# Two hashes computed by the code as it stood BEFORE attribution existed, over
# the inputs below. They are pasted here, not recomputed, because that is the
# only way this test can tell "scheme 1 still works" from "scheme 1 still agrees
# with itself". If an edit to _digest breaks these, it has invalidated every
# audit row already written.
# ---------------------------------------------------------------------------

GOLDEN_SEQ1 = "c19805f05ca233abca53728eb80b48f520d65b040ca81f2012cf96dfae980a79"
GOLDEN_SEQ2 = "1702389c39e7b47419e7a9beaa1828554854e73a3ce05fe7fc9f901478383b47"

_T1 = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 8, 3, 12, 5, 0, tzinfo=timezone.utc)

_LEGACY_ONE = {
    "company_id": "MEP",
    "actor": "analyst@mep.example",
    "action": "claim.verified",
    "subject_type": "claim",
    "subject_id": "CLM-1",
    "reason": "citation matched source at 1970:2066",
    "citation": "v1:1970:2066",
    "occurred_at": _T1,
}
_LEGACY_TWO = {
    "company_id": "MEP",
    "actor": "owner@mep.example",
    "action": "action.approved",
    "subject_type": "action",
    "subject_id": "ACT-1",
    "reason": "comply by 2026-11-01",
    "citation": "v3:8418:9294",
    "occurred_at": _T2,
}


def _append_v1(session, fields: dict) -> AuditEvent:
    """Write a row the way the log wrote them before attribution existed.

    record_event cannot do this and must not learn how: the old scheme is for
    reading the past, not for producing more of it. This helper is the only
    place scheme-1 rows are created, and it exists so the tests can rebuild a
    genuine pre-migration chain rather than assert against a mock of one.
    """
    tail = session.execute(
        select(AuditEvent)
        .where(AuditEvent.company_id == fields["company_id"])
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()

    seq = 1 if tail is None else tail.seq + 1
    prev_hash = "" if tail is None else tail.entry_hash

    entry = AuditEvent(
        seq=seq,
        prev_hash=prev_hash,
        digest_version=DIGEST_V1,
        entry_hash=_digest(prev_hash=prev_hash, seq=seq, **fields),
        **fields,
    )
    session.add(entry)
    session.flush()
    return entry


def _rows(session, company_id="MEP") -> list[AuditEvent]:
    return (
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.company_id == company_id)
            .order_by(AuditEvent.seq)
        )
        .scalars()
        .all()
    )


def _overwrite(session, entry_id: int, **values) -> None:
    """Rewrite a stored row behind the ORM's back, the way an attacker would.

    The before_flush guard refuses this through the ORM; a core UPDATE goes
    round it. That is the whole reason the hash chain exists as a second
    mechanism, so the tamper tests have to use the path that actually works.
    """
    session.execute(
        AuditEvent.__table__.update().where(AuditEvent.id == entry_id).values(**values)
    )
    session.commit()
    session.expire_all()


# ---------------------------------------------------------------------------
# Scheme 1 is frozen
# ---------------------------------------------------------------------------


def test_the_v1_digest_still_matches_a_hash_frozen_before_the_change():
    assert _digest(prev_hash="", seq=1, **_LEGACY_ONE) == GOLDEN_SEQ1
    assert _digest(prev_hash=GOLDEN_SEQ1, seq=2, **_LEGACY_TWO) == GOLDEN_SEQ2


def test_a_chain_written_entirely_under_v1_still_verifies():
    init_db()
    with session_scope() as session:
        _append_v1(session, _LEGACY_ONE)
        _append_v1(session, _LEGACY_TWO)
        session.commit()

        events = _rows(session)
        assert [e.digest_version for e in events] == [DIGEST_V1, DIGEST_V1]
        assert [e.entry_hash for e in events] == [GOLDEN_SEQ1, GOLDEN_SEQ2]
        # Nothing invented an actor for a row that never recorded one.
        assert [e.actor_kind for e in events] == [None, None]
        assert [e.actor_user_id for e in events] == [None, None]
        assert verify_chain(session, "MEP") is True


def test_editing_a_v1_row_inside_a_mixed_chain_is_still_detected():
    init_db()
    with session_scope() as session:
        first = _append_v1(session, _LEGACY_ONE)
        record_event(
            session,
            company_id="MEP",
            actor="analyst@mep.example",
            action=ACTION_PROJECT_CREATED,
            subject_type="project",
            subject_id="PRJ-1",
            reason="opened after the order landed",
            actor_kind=ACTOR_USER,
            actor_user_id="USR-1",
        )
        session.commit()
        _overwrite(session, first.id, reason="citation did not match")

        with pytest.raises(AuditTamperError) as caught:
            verify_chain(session, "MEP")
        assert "seq 1" in str(caught.value)


# ---------------------------------------------------------------------------
# A chain that crosses the version boundary
# ---------------------------------------------------------------------------


def test_a_v1_chain_followed_by_v2_rows_verifies_end_to_end():
    init_db()
    with session_scope() as session:
        _append_v1(session, _LEGACY_ONE)
        _append_v1(session, _LEGACY_TWO)
        third = record_event(
            session,
            company_id="MEP",
            actor="analyst@mep.example",
            action=ACTION_LOGIN_SUCCEEDED,
            subject_type="user",
            subject_id="USR-1",
            reason="password accepted",
            actor_kind=ACTOR_USER,
            actor_user_id="USR-1",
            session_id="SES-1",
            ip="198.51.100.20",
        )
        session.commit()

        events = _rows(session)
        assert [e.digest_version for e in events] == [DIGEST_V1, DIGEST_V1, DIGEST_V2]
        # The join across the boundary is an ordinary copied value.
        assert third.prev_hash == GOLDEN_SEQ2
        assert verify_chain(session, "MEP") is True


def test_record_event_writes_v2_and_records_a_machine_by_default():
    init_db()
    with session_scope() as session:
        entry = record_event(
            session,
            company_id="MEP",
            actor="pipeline",
            action="ingest_version",
            subject_type="version",
            subject_id="v1",
            reason="loaded the corpus",
        )
        assert entry.digest_version == CURRENT_DIGEST_VERSION == DIGEST_V2
        # A caller that has not thought about attribution records a machine
        # rather than quietly claiming a person.
        assert entry.actor_kind == ACTOR_SYSTEM
        assert entry.actor_user_id is None
        assert verify_chain(session, "MEP") is True


def test_blank_attribution_is_stored_as_absent_rather_than_as_empty_text():
    # Two spellings of "not recorded" split every query over the column in two.
    init_db()
    with session_scope() as session:
        entry = record_event(
            session,
            company_id="MEP",
            actor="pipeline",
            action="ingest_version",
            subject_type="version",
            subject_id="v1",
            reason="loaded the corpus",
            session_id="",
            ip="",
        )
        assert entry.session_id is None
        assert entry.ip is None


# ---------------------------------------------------------------------------
# Attribution is inside the digest, not beside it
# ---------------------------------------------------------------------------


def _one_v2_event(session) -> AuditEvent:
    return record_event(
        session,
        company_id="MEP",
        actor="ann@mep.example",
        action="action.approved",
        subject_type="action",
        subject_id="ACT-1",
        reason="comply by 2026-11-01",
        actor_kind=ACTOR_USER,
        actor_user_id="USR-ANN",
        session_id="SES-ANN",
        ip="198.51.100.20",
    )


def test_editing_actor_user_id_on_a_v2_row_is_detected():
    # The proof that attribution is hashed rather than merely stored. A column
    # outside the digest could be rewritten and the chain would still verify,
    # which is the same as having no attribution at all.
    init_db()
    with session_scope() as session:
        entry = _one_v2_event(session)
        session.commit()
        _overwrite(session, entry.id, actor_user_id="USR-BOB")

        with pytest.raises(AuditTamperError) as caught:
            verify_chain(session, "MEP")
        assert "seq 1" in str(caught.value)


def test_reassigning_an_approval_to_another_real_user_breaks_the_chain():
    # Segregation of duties is only a control if the record of who approved
    # cannot be edited afterwards. Ann approves; someone rewrites the row to
    # say Bob approved, and leaves every other field alone.
    init_db()
    with session_scope() as session:
        record_event(
            session,
            company_id="MEP",
            actor="bob@mep.example",
            action=ACTION_LOGIN_SUCCEEDED,
            subject_type="user",
            subject_id="USR-BOB",
            reason="password accepted",
            actor_kind=ACTOR_USER,
            actor_user_id="USR-BOB",
            session_id="SES-BOB",
        )
        approval = _one_v2_event(session)
        session.commit()

        _overwrite(
            session,
            approval.id,
            actor="bob@mep.example",
            actor_user_id="USR-BOB",
            session_id="SES-BOB",
        )

        with pytest.raises(AuditTamperError) as caught:
            verify_chain(session, "MEP")
        assert "seq 2" in str(caught.value)


def test_editing_the_session_or_the_address_on_a_v2_row_is_detected():
    for field, forged in (("session_id", "SES-OTHER"), ("ip", "203.0.113.9")):
        init_db()
        with session_scope() as session:
            entry = _one_v2_event(session)
            session.commit()
            _overwrite(session, entry.id, **{field: forged})

            with pytest.raises(AuditTamperError):
                verify_chain(session, "MEP")


# ---------------------------------------------------------------------------
# The version field itself cannot be used as a way out
# ---------------------------------------------------------------------------


def test_downgrading_a_v2_row_to_v1_is_detected():
    # The obvious attack on a versioned digest: claim the row was hashed by the
    # scheme that does not cover the field you want to edit. The two schemes
    # hash different field sets, so the recomputed hash differs and the row
    # fails. Nothing else is needed to close this, and this test is why.
    init_db()
    with session_scope() as session:
        entry = _one_v2_event(session)
        session.commit()
        _overwrite(session, entry.id, digest_version=DIGEST_V1)

        with pytest.raises(AuditTamperError):
            verify_chain(session, "MEP")


def test_upgrading_a_v1_row_to_v2_is_detected():
    init_db()
    with session_scope() as session:
        entry = _append_v1(session, _LEGACY_ONE)
        session.commit()
        _overwrite(session, entry.id, digest_version=DIGEST_V2)

        with pytest.raises(AuditTamperError):
            verify_chain(session, "MEP")


def test_a_row_claiming_an_unknown_scheme_refuses_to_verify():
    # Absence is denial. A row that cannot say how it was hashed is not read
    # under a scheme somebody guessed for it.
    init_db()
    with session_scope() as session:
        entry = _one_v2_event(session)
        session.commit()
        _overwrite(session, entry.id, digest_version=99)

        with pytest.raises(AuditTamperError) as caught:
            verify_chain(session, "MEP")
        assert "99" in str(caught.value)


def test_the_table_itself_refuses_a_row_that_names_no_scheme():
    # Two layers, and this is the outer one: on a table built from the model,
    # digest_version is NOT NULL, so "no scheme" cannot be stored at all.
    init_db()
    with session_scope() as session:
        entry = _one_v2_event(session)
        session.commit()
        with pytest.raises(IntegrityError):
            session.execute(
                AuditEvent.__table__.update()
                .where(AuditEvent.id == entry.id)
                .values(digest_version=None)
            )
        session.rollback()
        assert verify_chain(session, "MEP") is True


def test_a_row_with_no_scheme_is_refused_rather_than_guessed():
    # And the inner layer, for the database this code did not create: a column
    # added by hand without the default leaves NULLs behind, and reading one of
    # those under either known scheme would be a guess.
    orphan = AuditEvent(
        company_id="MEP",
        seq=1,
        actor="ann@mep.example",
        action=ACTION_LOGIN_SUCCEEDED,
        subject_type="user",
        subject_id="USR-ANN",
        reason="password accepted",
        citation="",
        occurred_at=_T1,
        prev_hash="",
        entry_hash="0" * 64,
        digest_version=None,
    )
    with pytest.raises(AuditTamperError) as caught:
        _digest_for_row(orphan)
    assert "None" in str(caught.value)


# ---------------------------------------------------------------------------
# The refusals that stop a mistake before it becomes evidence
# ---------------------------------------------------------------------------


def test_the_update_and_delete_guards_still_fire():
    init_db()
    with session_scope() as session:
        _append_v1(session, _LEGACY_ONE)
        _one_v2_event(session)
        session.commit()

        for column, value in (("reason", "something else"), ("actor_user_id", "USR-X")):
            entry = _rows(session)[1]
            setattr(entry, column, value)
            with pytest.raises(AuditTamperError):
                session.flush()
            session.rollback()

        entry = _rows(session)[0]
        session.delete(entry)
        with pytest.raises(AuditTamperError):
            session.flush()
        session.rollback()

        assert verify_chain(session, "MEP") is True


def test_an_unknown_actor_kind_is_refused():
    init_db()
    with session_scope() as session:
        for kind in ("users", "human", "", "USER"):
            with pytest.raises(ValueError) as caught:
                record_event(
                    session,
                    company_id="MEP",
                    actor="ann@mep.example",
                    action=ACTION_LOGIN_SUCCEEDED,
                    subject_type="user",
                    subject_id="USR-ANN",
                    reason="password accepted",
                    actor_kind=kind,
                )
            assert "actor_kind" in str(caught.value)


def test_a_machine_actor_cannot_carry_a_user_id():
    # Naming a person on a row the machine wrote is a false attribution, and the
    # audit log is the last place to make one.
    init_db()
    with session_scope() as session:
        for kind in (ACTOR_SYSTEM, ACTOR_MODEL):
            with pytest.raises(ValueError):
                record_event(
                    session,
                    company_id="MEP",
                    actor="pipeline",
                    action="ingest_version",
                    subject_type="version",
                    subject_id="v1",
                    reason="loaded the corpus",
                    actor_kind=kind,
                    actor_user_id="USR-ANN",
                )


# ---------------------------------------------------------------------------
# A failed login leaves a trace, and the trace is safe to read
# ---------------------------------------------------------------------------


PASSWORD = "correct-horse-battery-staple"


def _row_as_text(session, entry_id: int) -> str:
    """Every stored value of one row, flattened. What a reader of the log sees."""
    row = session.execute(
        text("SELECT * FROM audit_events WHERE id = :id"), {"id": entry_id}
    ).mappings().one()
    return "\n".join(f"{key}={value!r}" for key, value in row.items())


def test_a_failed_login_is_recorded_and_carries_no_password_material():
    init_db()
    with session_scope() as session:
        entry = record_event(
            session,
            company_id="MEP",
            actor="ann@mep.example",
            action=ACTION_LOGIN_FAILED,
            subject_type="user",
            subject_id="USR-ANN",
            reason="password did not match",
            actor_kind=ACTOR_USER,
            actor_user_id="USR-ANN",
            ip="203.0.113.7",
        )
        session.commit()

        stored = _row_as_text(session, entry.id)
        assert PASSWORD not in stored
        # Nor a hash of it. A hash of a guessed password is still a guessable
        # password, sitting in a table that by design cannot be redacted later.
        assert hashlib.sha256(PASSWORD.encode()).hexdigest() not in stored
        assert "password did not match" in stored

        # The trace an attacker leaves has to be useful: which account, from
        # where, when, and that it failed.
        assert entry.action == ACTION_LOGIN_FAILED
        assert entry.subject_id == "USR-ANN"
        assert entry.ip == "203.0.113.7"
        assert entry.session_id is None, "a failed login starts no session"
        assert verify_chain(session, "MEP") is True


def test_a_failed_login_against_an_unknown_account_still_records_the_attempt():
    # There is no user id to attribute, and the row says so rather than
    # inventing one. Probing a list of addresses is exactly the pattern this
    # row exists to make visible.
    init_db()
    with session_scope() as session:
        entry = record_event(
            session,
            company_id="MEP",
            actor="nobody@mep.example",
            action=ACTION_LOGIN_FAILED,
            subject_type="user",
            subject_id="nobody@mep.example",
            reason="no account with that address",
            actor_kind=ACTOR_USER,
            ip="203.0.113.7",
        )
        session.commit()

        assert entry.actor_kind == ACTOR_USER
        assert entry.actor_user_id is None
        assert PASSWORD not in _row_as_text(session, entry.id)
        assert verify_chain(session, "MEP") is True


def test_probing_many_accounts_leaves_one_row_each_and_the_chain_holds():
    init_db()
    with session_scope() as session:
        for address in ("ann@mep.example", "bob@mep.example", "cat@mep.example"):
            record_event(
                session,
                company_id="MEP",
                actor=address,
                action=ACTION_LOGIN_FAILED,
                subject_type="user",
                subject_id=address,
                reason="no account with that address",
                actor_kind=ACTOR_USER,
                ip="203.0.113.7",
            )
        session.commit()

        failures = [e for e in _rows(session) if e.action == ACTION_LOGIN_FAILED]
        assert len(failures) == 3
        assert verify_chain(session, "MEP") is True


# ---------------------------------------------------------------------------
# One vocabulary
#
# A misspelt action code does not fail. It writes a valid row, correctly hashed,
# that no query for that action will ever return -- in the log and invisible,
# which is worse than missing. These two tests are what keeps the vocabulary in
# one place.
# ---------------------------------------------------------------------------


REQUIRED_ACTION_CODES = (
    "ACTION_LOGIN_SUCCEEDED",
    "ACTION_LOGIN_FAILED",
    "ACTION_LOGOUT",
    "ACTION_SESSION_EXPIRED",
    "ACTION_ROLE_GRANTED",
    "ACTION_ROLE_REVOKED",
    "ACTION_USER_CREATED",
    "ACTION_USER_SUSPENDED",
    "ACTION_PASSWORD_CHANGED",
    "ACTION_ACTION_APPROVED",
    "ACTION_ACTION_REJECTED",
    "ACTION_ESCALATION_RESOLVED",
    "ACTION_STEER_ISSUED",
    "ACTION_THRESHOLD_CHANGED",
    "ACTION_KNOWLEDGE_SUPERSEDED",
    "ACTION_PROJECT_CREATED",
)


def test_every_required_action_code_exists_and_none_share_a_string():
    from app.state import audit

    for name in REQUIRED_ACTION_CODES:
        assert hasattr(audit, name), f"{name} is gone; a caller importing it breaks"

    # Every ACTION_ constant in the module, not only the required ones, so a code
    # added later cannot quietly collide with one already in use.
    codes = {
        name: getattr(audit, name)
        for name in dir(audit)
        if name.startswith("ACTION_")
    }
    assert len(set(codes.values())) == len(codes), (
        "two constants resolve to the same code, so one of the two actions can "
        f"never be told apart from the other: {sorted(codes.items())}"
    )


def test_the_modules_that_write_codes_import_them_rather_than_restate_them():
    from app.state import audit, identity, review

    for module, names in (
        (
            identity,
            ("ACTION_USER_CREATED", "ACTION_ROLE_GRANTED", "ACTION_ROLE_REVOKED"),
        ),
        (review, ("ACTION_STEER_ISSUED",)),
    ):
        for name in names:
            assert getattr(module, name) is getattr(audit, name), (
                f"{module.__name__} holds its own {name}. Two spellings of one "
                "action code drift, and the row written under the wrong one is "
                "invisible to every query for the right one."
            )


# ---------------------------------------------------------------------------
# The schema migration on a database that predates attribution
#
# The tests above prove the code reads both schemes. This one proves an actual
# file written by the old code can be opened by the new code at all -- the
# columns have to arrive without touching the fields scheme 1 hashed.
# ---------------------------------------------------------------------------


_LEGACY_DDL = """
CREATE TABLE audit_events (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    company_id VARCHAR(64) NOT NULL,
    seq INTEGER NOT NULL,
    actor VARCHAR(256) NOT NULL,
    action VARCHAR(64) NOT NULL,
    subject_type VARCHAR(64) NOT NULL,
    subject_id VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    citation VARCHAR(256),
    occurred_at VARCHAR(32) NOT NULL,
    prev_hash VARCHAR(64),
    entry_hash VARCHAR(64) NOT NULL
)
"""

_LEGACY_INSERT = """
INSERT INTO audit_events
    (company_id, seq, actor, action, subject_type, subject_id, reason,
     citation, occurred_at, prev_hash, entry_hash)
VALUES
    (:company_id, :seq, :actor, :action, :subject_type, :subject_id, :reason,
     :citation, :occurred_at, :prev_hash, :entry_hash)
"""


def _legacy_database(tmp_path):
    """A SQLite file in the exact shape the log had before attribution."""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text(_LEGACY_DDL))
        for seq, (fields, prev, digest) in enumerate(
            (
                (_LEGACY_ONE, "", GOLDEN_SEQ1),
                (_LEGACY_TWO, GOLDEN_SEQ1, GOLDEN_SEQ2),
            ),
            start=1,
        ):
            connection.execute(
                text(_LEGACY_INSERT),
                {
                    **fields,
                    "seq": seq,
                    "prev_hash": prev,
                    "entry_hash": digest,
                    "occurred_at": fields["occurred_at"].isoformat(),
                },
            )
    return engine


def test_migrating_a_pre_attribution_database_leaves_its_rows_verifying(tmp_path):
    engine = _legacy_database(tmp_path)
    try:
        added = migrate_audit_schema(engine)
        assert added == (
            "actor_user_id",
            "actor_kind",
            "session_id",
            "ip",
            "digest_version",
            "reverts_event_id",
        )

        session = sessionmaker(bind=engine, future=True)()
        try:
            events = _rows(session)
            # Backfilled to the older scheme, because that is what wrote them.
            assert [e.digest_version for e in events] == [DIGEST_V1, DIGEST_V1]
            assert [e.entry_hash for e in events] == [GOLDEN_SEQ1, GOLDEN_SEQ2]
            assert [e.actor_kind for e in events] == [None, None]
            assert verify_chain(session, "MEP") is True

            # And the migrated log accepts new attributed rows on the same chain.
            third = record_event(
                session,
                company_id="MEP",
                actor="ann@mep.example",
                action=ACTION_LOGIN_SUCCEEDED,
                subject_type="user",
                subject_id="USR-ANN",
                reason="password accepted",
                actor_kind=ACTOR_USER,
                actor_user_id="USR-ANN",
                session_id="SES-ANN",
                ip="198.51.100.20",
            )
            session.commit()
            assert third.prev_hash == GOLDEN_SEQ2
            assert third.digest_version == DIGEST_V2
            assert verify_chain(session, "MEP") is True
        finally:
            session.close()
    finally:
        engine.dispose()


def test_the_migration_adds_nothing_the_second_time(tmp_path):
    engine = _legacy_database(tmp_path)
    try:
        assert migrate_audit_schema(engine)
        assert migrate_audit_schema(engine) == ()
    finally:
        engine.dispose()


def test_the_migration_reports_nothing_to_do_when_there_is_no_log(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    try:
        assert migrate_audit_schema(engine) == ()
    finally:
        engine.dispose()
