"""Authorisation, and the control that refuses people who hold the permission.

Six wrong implementations this suite exists to catch, because each of them
passes a casual read:

1. Approval that checks only the permission. Holding action.approve is the first
   gate, not the last one; the analyst who worked the claim is refused with the
   permission in their hand, and that refusal is the product's central security
   claim.
2. A control switched off by a typo. An unrecognised STRATA_APPROVAL_MODE must
   stop the process, not fall back to either mode -- the safe one silently, or
   the unsafe one worse.
3. A downgrade that hides. DEMO_SELF_APPROVAL permits, and it must say in the
   verdict and in the audit chain that the separation was waived.
4. A check that fails open. An unknown permission code, an unknown user, another
   tenant's claim and a suspended account must every one of them refuse.
5. A refusal that leaves no trace, or that breaks the chain it is written into.
6. A control that feeds on its own output. The refusal this module writes must
   not become the evidence that refuses the same person for ever.
"""

import importlib

import pytest

from app.auth import policy
from app.state.audit import (
    ACTION_ACCESS_DENIED,
    ACTION_ACTION_APPROVED,
    ACTION_APPROVAL_WAIVED,
    ACTION_ESCALATION_RESOLVED,
    ACTOR_USER,
    event_count,
    record_event,
    verify_chain,
)
from app.state.db import init_db, session_scope
from app.state.identity import (
    SYSTEM_ROLE_PERMISSIONS,
    create_user,
    ensure_system_roles,
    grant_role,
)
from app.state.models import (
    PERMISSION_CODES,
    AuditEvent,
    Change,
    Claim,
    DocumentVersion,
    Escalation,
    Proceeding,
)

COMPANY = "MEP"
RIVAL = "RIVAL"
PASSWORD = "correct-horse-battery-staple"
ADMIN = "admin@mep.example"

SOURCE = (
    "Each utility shall file a distribution system implementation plan within "
    "one hundred and twenty days of the effective date of this order."
)


# ---------------------------------------------------------------------------
# Fixtures: a claim with an escalation on it, and people who might approve it
# ---------------------------------------------------------------------------


def _seed_claim(session, company: str = COMPANY, tag: str = "a") -> tuple[str, str]:
    """One proceeding, one version, one change, one claim, one escalation.

    Built by hand rather than through the pipeline because this suite is about
    who may approve, not about how a claim gets written. The shape matters --
    the claim's change id and the escalation's claim id are what
    _resolve_target walks -- and nothing else here does.
    """
    version_id = f"ver-{company}-{tag}"
    change_id = f"chg-{company}-{tag}"
    claim_id = f"clm-{company}-{tag}"
    escalation_id = f"esc-{company}-{tag}"

    session.add(
        Proceeding(
            id=f"prc-{company}-{tag}",
            company_id=company,
            docket="24-M-0001",
            commission="NYPSC",
            subject="Distribution system implementation plans",
        )
    )
    session.add(
        DocumentVersion(
            id=version_id,
            company_id=company,
            docket="24-M-0001",
            label="Order, October",
            status="FINAL",
            source_text=SOURCE,
            source_sha256="0" * 64,
        )
    )
    session.add(
        Change(
            id=change_id,
            company_id=company,
            proceeding_id=f"prc-{company}-{tag}",
            from_version_id=version_id,
            to_version_id=version_id,
            change_type="modified",
            before_start=0,
            before_end=len(SOURCE),
            after_start=0,
            after_end=len(SOURCE),
            section="II",
            alignment_confidence=0.9,
            materiality=None,
            status="FINAL",
        )
    )
    session.add(
        Claim(
            id=claim_id,
            company_id=company,
            change_id=change_id,
            statement="The plan is due within 120 days.",
            citation_version_id=version_id,
            citation_start=0,
            citation_end=len(SOURCE),
            citation_quote=SOURCE,
            cited_occurrence=None,
            confidence_bp=9100,
        )
    )
    session.add(
        Escalation(
            id=escalation_id,
            company_id=company,
            claim_id=claim_id,
            reason_code="low_confidence",
            reason_text="Confidence below threshold.",
            detail="",
        )
    )
    session.flush()
    return claim_id, escalation_id


def _person(session, email: str, name: str, roles: tuple[str, ...], company=COMPANY):
    user = create_user(
        session,
        company,
        email=email,
        display_name=name,
        password=PASSWORD,
        actor=ADMIN,
    )
    for role in roles:
        grant_role(session, company, user_id=user.id, role_name=role, actor=ADMIN)
    return user


def _owner(session, email: str = "owner@mep.example", company=COMPANY):
    return _person(session, email, "Ruth Alvarez", ("obligation_owner",), company)


def _analyst(session, email: str = "analyst@mep.example", company=COMPANY):
    return _person(session, email, "Dana Okafor", ("analyst",), company)


def _denials(session, company: str = COMPANY) -> list[AuditEvent]:
    return (
        session.query(AuditEvent)
        .filter(AuditEvent.company_id == company)
        .filter(AuditEvent.action == ACTION_ACCESS_DENIED)
        .order_by(AuditEvent.seq)
        .all()
    )


@pytest.fixture
def demo_policy(monkeypatch):
    """The module re-imported under the demo downgrade, then put back.

    The mode is read at import, so a test that only sets the environment
    variable would be testing nothing. Reloading exercises the real path from
    the variable to the module, which is the path an operator uses.
    """
    monkeypatch.setenv(policy.ENV_APPROVAL_MODE, policy.DEMO_SELF_APPROVAL)
    module = importlib.reload(policy)
    try:
        yield module
    finally:
        monkeypatch.delenv(policy.ENV_APPROVAL_MODE, raising=False)
        importlib.reload(policy)


# ---------------------------------------------------------------------------
# The mode
# ---------------------------------------------------------------------------


def test_unset_means_the_safe_mode():
    # Doing nothing has to give you the control, not the demonstration.
    assert policy.APPROVAL_MODE == policy.SEGREGATED
    assert policy.approval_mode() == policy.SEGREGATED


def test_an_unrecognised_mode_stops_the_process_at_import(monkeypatch):
    # Including a near miss. "segregated" is the right word in the wrong case,
    # and accepting it would teach an operator that case does not matter --
    # which is exactly the belief that turns DEMO_SELF_APPROVAL into
    # demo_self_approval and a refusal into a silent pass.
    for bad in ("segregated", "DEMO", "OFF", "true"):
        monkeypatch.setenv(policy.ENV_APPROVAL_MODE, bad)
        try:
            with pytest.raises(ValueError) as error:
                importlib.reload(policy)
            assert policy.ENV_APPROVAL_MODE in str(error.value)
        finally:
            monkeypatch.delenv(policy.ENV_APPROVAL_MODE, raising=False)
            importlib.reload(policy)

    assert policy.APPROVAL_MODE == policy.SEGREGATED


def test_whitespace_around_a_mode_is_the_same_word(monkeypatch):
    monkeypatch.setenv(policy.ENV_APPROVAL_MODE, f"  {policy.DEMO_SELF_APPROVAL}\n")
    try:
        module = importlib.reload(policy)
        assert module.APPROVAL_MODE == policy.DEMO_SELF_APPROVAL
    finally:
        monkeypatch.delenv(policy.ENV_APPROVAL_MODE, raising=False)
        importlib.reload(policy)


# ---------------------------------------------------------------------------
# require and has
# ---------------------------------------------------------------------------


def test_the_gate_checks_the_code_the_role_grid_actually_grants():
    # This module names the permission as a string. If the vocabulary or the
    # grid moved and this string stayed, every approval would be refused with
    # no error to explain it, and the product would look broken rather than
    # misconfigured.
    assert policy.APPROVE in PERMISSION_CODES
    assert policy.APPROVE in SYSTEM_ROLE_PERMISSIONS["obligation_owner"]
    assert policy.APPROVE not in SYSTEM_ROLE_PERMISSIONS["analyst"]
    assert policy.APPROVE not in SYSTEM_ROLE_PERMISSIONS["admin"]


def test_an_unknown_permission_code_raises_rather_than_passing():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        owner = _owner(session)

        for bad in ("action.aprove", "", "claim.delete"):
            with pytest.raises(policy.UnknownPermission):
                policy.require(session, COMPANY, owner.id, bad)
            with pytest.raises(policy.UnknownPermission):
                policy.has(session, COMPANY, owner.id, bad)

        # The check never ran, so nothing was decided about this person and
        # nothing may be written claiming otherwise.
        assert _denials(session) == []


def test_require_lets_a_holder_through_and_writes_nothing():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        owner = _owner(session)
        before = event_count(session, COMPANY)

        assert policy.require(session, COMPANY, owner.id, "audit.read") is None
        assert event_count(session, COMPANY) == before


def test_require_will_not_gate_on_the_approval_permission():
    # The one code whose holder may still be forbidden the act. A gate that
    # answered "yes, they hold it" would retire segregation of duties without
    # anybody deciding to.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        owner = _owner(session)

        with pytest.raises(ValueError) as error:
            policy.require(session, COMPANY, owner.id, policy.APPROVE)

        assert "can_approve" in str(error.value)
        # It is not a refusal of the person, so nothing is written about them.
        assert _denials(session) == []
        # The rendering question is still answerable.
        assert policy.has(session, COMPANY, owner.id, policy.APPROVE) is True


def test_require_refuses_and_records_the_refusal():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        analyst = _analyst(session)

        with pytest.raises(policy.PermissionDenied) as error:
            policy.require(session, COMPANY, analyst.id, "audit.read")

        assert error.value.code == "audit.read"
        assert error.value.user_id == analyst.id
        assert analyst.email in error.value.reason

        rows = _denials(session)
        assert len(rows) == 1
        assert rows[0].subject_type == "permission"
        assert rows[0].subject_id == "audit.read"
        assert rows[0].actor_user_id == analyst.id
        assert rows[0].actor_kind == ACTOR_USER
        assert verify_chain(session, COMPANY) is True


def test_an_unknown_user_and_another_tenants_user_are_both_refused():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        theirs = _owner(session, email="owner@rival.example", company=RIVAL)

        for user_id in ("usr-nobody", theirs.id, ""):
            with pytest.raises(policy.PermissionDenied):
                policy.require(session, COMPANY, user_id, "audit.read")
            assert policy.has(session, COMPANY, user_id, "action.approve") is False

        # The refusal about another tenant's user belongs to the company that
        # refused it, and the rival's chain knows nothing about the attempt.
        assert len(_denials(session)) == 3
        assert _denials(session, RIVAL) == []
        assert verify_chain(session, COMPANY) is True


def test_has_answers_without_writing_anything():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        owner = _owner(session)
        analyst = _analyst(session)
        before = event_count(session, COMPANY)

        assert policy.has(session, COMPANY, owner.id, "action.approve") is True
        assert policy.has(session, COMPANY, analyst.id, "action.approve") is False
        assert policy.has(session, COMPANY, analyst.id, "action.propose") is True

        # A button nobody drew is not a decision. Auditing it would fill the
        # chain with rows that hide the refusals that matter.
        assert event_count(session, COMPANY) == before


def test_an_unscoped_check_is_refused_rather_than_answered():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        owner = _owner(session)
        for call in (policy.require, policy.has):
            with pytest.raises(ValueError):
                call(session, "", owner.id, "audit.read")


# ---------------------------------------------------------------------------
# can_approve: the control
# ---------------------------------------------------------------------------


def test_a_user_without_the_permission_cannot_approve():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, _ = _seed_claim(session)
        analyst = _analyst(session)

        allowed, reason = policy.can_approve(session, COMPANY, analyst.id, claim_id)

        assert allowed is False
        assert "action.approve" in reason
        assert verify_chain(session, COMPANY) is True


def test_the_person_who_worked_the_claim_is_refused_holding_the_permission():
    # The whole control, in one test. This user holds action.approve. They are
    # refused anyway, because the audit chain shows they already acted on the
    # claim the action follows from.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, escalation_id = _seed_claim(session)
        both = _person(
            session,
            "dana@mep.example",
            "Dana Okafor",
            ("analyst", "obligation_owner"),
        )

        assert policy.has(session, COMPANY, both.id, "action.approve") is True

        record_event(
            session,
            company_id=COMPANY,
            actor=both.email,
            action=ACTION_ESCALATION_RESOLVED,
            subject_type="escalation",
            subject_id=escalation_id,
            reason="refusal disputed; the quote is right and the offsets moved",
            actor_user_id=both.id,
            actor_kind=ACTOR_USER,
        )

        verdict = policy.can_approve(session, COMPANY, both.id, claim_id)

        assert verdict.allowed is False
        assert not verdict, "a refused verdict must be falsy, not merely a tuple"
        assert ACTION_ESCALATION_RESOLVED in verdict.reason
        assert escalation_id in verdict.reason
        assert "user id" in verdict.reason
        assert verify_chain(session, COMPANY) is True


def test_a_different_owner_who_never_touched_the_claim_may_approve():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, escalation_id = _seed_claim(session)
        analyst = _analyst(session)
        owner = _owner(session)

        record_event(
            session,
            company_id=COMPANY,
            actor=analyst.email,
            action=ACTION_ESCALATION_RESOLVED,
            subject_type="escalation",
            subject_id=escalation_id,
            reason="refusal disputed",
            actor_user_id=analyst.id,
            actor_kind=ACTOR_USER,
        )
        before = event_count(session, COMPANY)

        verdict = policy.can_approve(session, COMPANY, owner.id, claim_id)

        assert verdict.allowed is True
        assert bool(verdict) is True
        assert owner.email in verdict.reason
        # A clean allow is not a decision yet. The approval itself is the event
        # worth recording, and the caller writes that.
        assert event_count(session, COMPANY) == before


def test_the_escalation_id_and_the_claim_id_reach_the_same_verdict():
    # A screen built from the review queue holds an escalation id; one built
    # from the change holds a claim id. The same person must not be refused on
    # one page and allowed on the other.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, escalation_id = _seed_claim(session)
        both = _person(
            session, "dana@mep.example", "Dana Okafor", ("analyst", "obligation_owner")
        )
        record_event(
            session,
            company_id=COMPANY,
            actor=both.email,
            action=ACTION_ESCALATION_RESOLVED,
            subject_type="escalation",
            subject_id=escalation_id,
            reason="refusal disputed",
            actor_user_id=both.id,
            actor_kind=ACTOR_USER,
        )

        by_claim = policy.can_approve(session, COMPANY, both.id, claim_id)
        by_escalation = policy.can_approve(session, COMPANY, both.id, escalation_id)

        assert by_claim.allowed is False
        assert by_escalation.allowed is False


def test_an_act_on_the_change_underneath_the_claim_also_refuses():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, _ = _seed_claim(session)
        owner = _owner(session)

        # A materiality judgement is recorded against the change, not the
        # claim. The person who made it interpreted the change, which is the
        # exact act the control is about.
        record_event(
            session,
            company_id=COMPANY,
            actor=owner.email,
            action="change.materiality_set",
            subject_type="change",
            subject_id=f"chg-{COMPANY}-a",
            reason="marked material: a filing deadline moved",
            actor_user_id=owner.id,
            actor_kind=ACTOR_USER,
        )

        verdict = policy.can_approve(session, COMPANY, owner.id, claim_id)

        assert verdict.allowed is False
        assert "change.materiality_set" in verdict.reason


def test_an_action_code_this_module_has_never_heard_of_still_counts():
    # Authorship is a denylist, not an allowlist. A kind of work added next
    # month is treated as authorship until somebody argues in the code that it
    # is not -- the direction that refuses rather than the one that permits.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, _ = _seed_claim(session)
        owner = _owner(session)

        record_event(
            session,
            company_id=COMPANY,
            actor=owner.email,
            action="claim.rewritten_by_a_feature_from_next_month",
            subject_type="claim",
            subject_id=claim_id,
            reason="statement reworded",
            actor_user_id=owner.id,
            actor_kind=ACTOR_USER,
        )

        assert policy.can_approve(session, COMPANY, owner.id, claim_id).allowed is False


def test_a_row_naming_a_person_before_attribution_existed_still_refuses():
    # app/web/views/review.py wrote the reviewer's typed name into `actor` and
    # no user id at all. Those rows are still in the chain. Matching the name
    # is weaker evidence than an id and it is used only where the id is absent,
    # because it can only cause a refusal -- never an approval.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, escalation_id = _seed_claim(session)
        owner = _person(
            session, "ruth@mep.example", "Ruth Alvarez", ("obligation_owner",)
        )

        record_event(
            session,
            company_id=COMPANY,
            actor="  RUTH@mep.example ",
            action="escalation.approved",
            subject_type="escalation",
            subject_id=escalation_id,
            reason="refusal upheld",
        )

        verdict = policy.can_approve(session, COMPANY, owner.id, claim_id)

        assert verdict.allowed is False
        assert "actor name" in verdict.reason


def test_a_recorded_id_beats_a_shared_display_name():
    # Two people can share a mailbox and a name. Where the chain recorded an
    # identity, that identity settles the row; the weaker match must not
    # override it and refuse the wrong person.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, escalation_id = _seed_claim(session)
        analyst = _person(
            session, "dana@mep.example", "Dana Okafor", ("analyst",)
        )
        namesake = _person(
            session, "d.okafor@mep.example", "Dana Okafor", ("obligation_owner",)
        )

        record_event(
            session,
            company_id=COMPANY,
            actor="Dana Okafor",
            action=ACTION_ESCALATION_RESOLVED,
            subject_type="escalation",
            subject_id=escalation_id,
            reason="refusal disputed",
            actor_user_id=analyst.id,
            actor_kind=ACTOR_USER,
        )

        verdict = policy.can_approve(session, COMPANY, namesake.id, claim_id)
        assert verdict.allowed is True


def test_approving_does_not_turn_the_approver_into_an_author():
    # An approval is the decision this control governs, not a contribution to
    # the claim. Counting it would make the second approval anybody ever writes
    # on a claim impossible.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, _ = _seed_claim(session)
        owner = _owner(session)

        record_event(
            session,
            company_id=COMPANY,
            actor=owner.email,
            action=ACTION_ACTION_APPROVED,
            subject_type="claim",
            subject_id=claim_id,
            reason="approved: file the plan",
            actor_user_id=owner.id,
            actor_kind=ACTOR_USER,
        )

        assert policy.can_approve(session, COMPANY, owner.id, claim_id).allowed is True


def test_a_refusal_does_not_become_the_evidence_that_refuses_again():
    # The refusal this module writes names the user and the claim. If that row
    # counted as authorship, granting the missing role afterwards would fix
    # nothing: the control would be feeding on its own output.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, _ = _seed_claim(session)
        person = _analyst(session)

        first = policy.can_approve(session, COMPANY, person.id, claim_id)
        assert first.allowed is False

        grant_role(
            session,
            COMPANY,
            user_id=person.id,
            role_name="obligation_owner",
            actor=ADMIN,
        )

        second = policy.can_approve(session, COMPANY, person.id, claim_id)
        assert second.allowed is True


def test_a_suspended_owner_cannot_approve():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, _ = _seed_claim(session)
        owner = _owner(session)
        owner.status = "suspended"
        session.flush()

        verdict = policy.can_approve(session, COMPANY, owner.id, claim_id)

        assert verdict.allowed is False
        assert "suspended" in verdict.reason


def test_another_tenants_claim_is_not_approvable_and_leaks_nothing():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        theirs, _ = _seed_claim(session, company=RIVAL, tag="b")
        owner = _owner(session)

        verdict = policy.can_approve(session, COMPANY, owner.id, theirs)

        assert verdict.allowed is False
        # The same sentence an id that was never issued gets, apart from the id
        # the caller already had. A verdict that said "another company's claim"
        # would confirm the id exists.
        never = policy.can_approve(session, COMPANY, owner.id, "clm-never-issued")
        assert verdict.reason.replace(theirs, "ID") == never.reason.replace(
            "clm-never-issued", "ID"
        )


def test_a_user_without_the_permission_learns_nothing_about_which_ids_exist():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, _ = _seed_claim(session)
        analyst = _analyst(session)

        real = policy.can_approve(session, COMPANY, analyst.id, claim_id)
        invented = policy.can_approve(session, COMPANY, analyst.id, "clm-invented")

        assert real.allowed is False and invented.allowed is False
        assert real.reason == invented.reason


def test_every_refusal_is_in_the_chain_and_the_chain_still_verifies():
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, escalation_id = _seed_claim(session)
        analyst = _analyst(session)
        both = _person(
            session, "dana@mep.example", "Dana Okafor", ("analyst", "obligation_owner")
        )
        record_event(
            session,
            company_id=COMPANY,
            actor=both.email,
            action=ACTION_ESCALATION_RESOLVED,
            subject_type="escalation",
            subject_id=escalation_id,
            reason="refusal disputed",
            actor_user_id=both.id,
            actor_kind=ACTOR_USER,
        )

        policy.can_approve(session, COMPANY, analyst.id, claim_id)  # no permission
        policy.can_approve(session, COMPANY, both.id, claim_id)  # authored it
        policy.can_approve(session, COMPANY, both.id, "clm-nothing")  # no such claim
        with pytest.raises(policy.PermissionDenied):
            policy.require(session, COMPANY, analyst.id, "audit.read")

        rows = _denials(session)
        assert len(rows) == 4
        assert [row.subject_type for row in rows] == [
            "approval",
            "approval",
            "approval",
            "permission",
        ]
        assert {row.actor_kind for row in rows} == {ACTOR_USER}
        # One log, not two: the refusals sit in the same chain as the grants
        # and the escalation, and it still verifies end to end.
        assert verify_chain(session, COMPANY) is True


# ---------------------------------------------------------------------------
# The demo downgrade
# ---------------------------------------------------------------------------


def test_demo_mode_permits_and_names_the_downgrade(demo_policy):
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, escalation_id = _seed_claim(session)
        both = _person(
            session, "dana@mep.example", "Dana Okafor", ("analyst", "obligation_owner")
        )
        record_event(
            session,
            company_id=COMPANY,
            actor=both.email,
            action=ACTION_ESCALATION_RESOLVED,
            subject_type="escalation",
            subject_id=escalation_id,
            reason="refusal disputed",
            actor_user_id=both.id,
            actor_kind=ACTOR_USER,
        )

        verdict = demo_policy.can_approve(session, COMPANY, both.id, claim_id)

        assert verdict.allowed is True
        # It permits, and it says so in words a screen can put in front of the
        # person doing it: what was waived, by what setting, and what the real
        # verdict was.
        assert demo_policy.DEMO_SELF_APPROVAL in verdict.reason
        assert demo_policy.ENV_APPROVAL_MODE in verdict.reason
        assert demo_policy.SEGREGATED in verdict.reason
        assert ACTION_ESCALATION_RESOLVED in verdict.reason

        waivers = (
            session.query(AuditEvent)
            .filter(AuditEvent.company_id == COMPANY)
            .filter(AuditEvent.action == ACTION_APPROVAL_WAIVED)
            .all()
        )
        assert len(waivers) == 1
        assert waivers[0].actor_user_id == both.id
        assert waivers[0].subject_id == claim_id
        # A waiver is not an approval. Reading the two as one code would make a
        # switched-off control look like a clean sign-off a year later.
        assert waivers[0].action != ACTION_ACTION_APPROVED
        assert verify_chain(session, COMPANY) is True


def test_demo_mode_waives_the_separation_and_nothing_else(demo_policy):
    # The downgrade is about who may approve their own work. It is not a way to
    # approve without the permission, without an account, or on another
    # tenant's claim.
    init_db()
    with session_scope() as session:
        ensure_system_roles(session)
        claim_id, _ = _seed_claim(session)
        analyst = _analyst(session)
        owner = _owner(session)

        for user_id, target in (
            (analyst.id, claim_id),  # holds no action.approve
            ("usr-nobody", claim_id),  # no such account
            (owner.id, "clm-nothing"),  # no such claim
        ):
            verdict = demo_policy.can_approve(session, COMPANY, user_id, target)
            assert verdict.allowed is False, verdict.reason
        assert verify_chain(session, COMPANY) is True


def test_the_mode_goes_back_to_segregated_after_the_fixture():
    # The reload in the fixture is a real import, so a leak here would silently
    # switch the control off for every test that runs after it.
    assert policy.APPROVAL_MODE == policy.SEGREGATED
