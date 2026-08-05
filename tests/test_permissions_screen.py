"""The permissions matrix: who holds what, how they got it, and what conflicts.

WHAT THIS SCREEN IS FOR, AND WHICH TEST HOLDS EACH PART.

  "Any permission to any person."      -> the grant and revoke tests
  "Where a company composes a set that
   is not one of the defaults, the
   company names it."                  -> the custom role tests
  "Do not forbid a conflict. Show it." -> the conflict register tests, which are
                                          the ones worth reading
  "Refusals are explained."            -> the ceiling tests

THE TEST THIS FILE EXISTS FOR is test_a_held_conflict_is_named_and_permitted.
The product's central judgement here is that holding both sides of a separation
is the company's decision and the product's job is to record it. A screen that
refused the arrangement would be unusable in a four-person team; a screen that
said nothing would be useless in a large one. So the register names the person,
names the pair, prints the sentence that argues why anybody minds, and says how
each side was obtained -- and blocks nothing.

THE CELL STATE IS UNIT-TESTED SEPARATELY FROM THE PAGE, and that split is not
tidiness. A cell has eight states, four of which are the record and the
permission gate disagreeing, and half of those cannot be produced in a seeded
database at all -- a live role grant the check does not return is a defect, not
a fixture. So the eight combinations of (held by role, held directly, the check
honours it, the account is active) are tested against a pure function, and the
page is tested for the ones a real workspace can reach.

WHAT IS NOT TESTED HERE. Anything app/state/permissions.py decides. That module
refuses an unknown code, a blank reason, a system role name, an edit of a system
role and a grant above the caller's ceiling, and tests/test_permissions.py holds
all of it. What these tests hold is that the screen calls it, prints its refusal
rather than a paraphrase, and writes nothing of its own when it does.

Offline, no API key, no network. https://testserver, because the session cookie
is marked Secure and a client on http drops it in silence.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.seed import DEMO_PASSWORD, demo_account_list, ensure_accounts
from app.state import identity
from app.state import permissions as store
from app.state.audit import ACTION_ACCESS_DENIED, verify_chain
from app.state.db import init_db, session_scope
from app.state.identity import (
    create_user,
    ensure_system_roles,
    permissions_for_user,
    user_by_email,
)
from app.state.models import (
    PERMISSION_CODES,
    ROLE_ADMIN,
    ROLE_ANALYST,
    ROLE_OBLIGATION_OWNER,
    STATUS_SUSPENDED,
    SYSTEM_ROLE_NAMES,
    AuditEvent,
    Role,
    RolePermission,
    User,
    UserPermission,
)
from app.web import deps
from app.web.deps import install_auth
from app.web.views import auth as auth_view
from app.web.views import permissions as screen

COMPANY = "MEP"
RIVAL = "RIVAL"

SCREEN_URL = screen.PERMISSIONS_URL

APPROVE = "action.approve"
PROPOSE = "action.propose"


# --------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def default_tenant(monkeypatch):
    """The default company, whatever the shell exported."""
    monkeypatch.delenv(deps.COMPANY_ENV, raising=False)
    monkeypatch.delenv(deps.COMPANY_NAME_ENV, raising=False)


@pytest.fixture
def anonymous() -> TestClient:
    """The screen behind the guard the product installs.

    The corpus is not loaded. Nothing on this screen reads a proceeding, a
    change or a claim.
    """
    init_db()
    with session_scope() as session:
        ensure_accounts(session)

    app = FastAPI()
    app.include_router(auth_view.router)
    app.include_router(screen.router)
    install_auth(app)
    return TestClient(app, base_url="https://testserver")


def _email(role: str) -> str:
    """The seeded account holding a role, read from the corpus rather than typed."""
    return next(
        account.email for account in demo_account_list() if account.role == role
    )


def _sign_in(client: TestClient, email: str, password: str = DEMO_PASSWORD):
    return client.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


@pytest.fixture
def admin(anonymous: TestClient) -> TestClient:
    """Signed in as the seeded account holding user.manage."""
    assert _sign_in(anonymous, _email(ROLE_ADMIN)).status_code == 303
    return anonymous


@pytest.fixture
def analyst(anonymous: TestClient) -> TestClient:
    """Signed in as somebody who may not administer people."""
    assert _sign_in(anonymous, _email(ROLE_ANALYST)).status_code == 303
    return anonymous


# ---------------------------------------------------------------- helpers


def _user(email: str, company_id: str = COMPANY) -> User:
    with session_scope() as session:
        person = user_by_email(session, company_id, email)
        assert person is not None, f"{email} has no account in {company_id}"
        return person


def _grant_directly(
    email: str,
    code: str,
    *,
    reason: str = "covering the director while she is away",
    revoked: bool = False,
) -> None:
    """Write a direct grant straight into the table.

    NOT through the screen, and not through a write function that may not be in
    this build yet. The read half of this feature has to be provable before the
    write half exists, or neither can be finished.
    """
    with session_scope() as session:
        person = user_by_email(session, COMPANY, email)
        granter = user_by_email(session, COMPANY, _email(ROLE_ADMIN))
        now = datetime.now(timezone.utc)
        session.add(
            UserPermission(
                company_id=COMPANY,
                user_id=person.id,
                code=code,
                granted_by_user_id=granter.id,
                granted_at=now - timedelta(days=2),
                reason=reason,
                revoked_at=now if revoked else None,
                revoked_by_user_id=granter.id if revoked else None,
            )
        )


def _effective(email: str) -> frozenset[str]:
    """What the permission check itself says this person holds, right now."""
    with session_scope() as session:
        person = user_by_email(session, COMPANY, email)
        return permissions_for_user(session, COMPANY, person.id)


def _section(body: str, marker: str) -> str:
    """Everything from a section's own id to the end of that section."""
    assert marker in body, f"{marker} is not on the page"
    return body.split(marker, 1)[1].split("</section>", 1)[0]


def _block_for(body: str, email: str) -> str:
    """The one matrix row that mentions this address.

    Read out of the matrix section rather than the whole page, because the same
    address is in the exceptions table and in the two menus on the forms below.
    """
    matrix = _section(body, screen.MATRIX_ANCHOR)
    rows = [part for part in matrix.split("<tr") if email in part]
    assert rows, f"{email} is not in the matrix"
    assert len(rows) == 1, f"{email} appears in {len(rows)} matrix rows"
    return rows[0]


def _denials() -> list[AuditEvent]:
    with session_scope() as session:
        return (
            session.query(AuditEvent)
            .filter(AuditEvent.action == ACTION_ACCESS_DENIED)
            .all()
        )


def _live_rows() -> list[UserPermission]:
    with session_scope() as session:
        return (
            session.query(UserPermission)
            .filter(UserPermission.revoked_at.is_(None))
            .all()
        )


def _composed() -> list[Role]:
    """The roles this company composed for itself. Never the system three."""
    with session_scope() as session:
        return store.composed_roles(session, COMPANY)


def _codes_on(role_id: str) -> set[str]:
    with session_scope() as session:
        return {
            code
            for (code,) in session.query(RolePermission.permission_id).filter(
                RolePermission.role_id == role_id
            )
        }


# ------------------------------------------------------------------- the gate


def test_the_screen_refuses_an_anonymous_request(anonymous):
    """It is a screen about other people's authority. The wall is in front of it."""
    response = anonymous.get(SCREEN_URL, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_an_analyst_cannot_read_the_matrix_and_the_refusal_is_recorded(analyst):
    """A refusal is a fact in the chain, not a blank page."""
    before = len(_denials())
    response = analyst.get(SCREEN_URL)

    assert response.status_code == 403
    assert screen.MANAGE in response.text
    assert len(_denials()) == before + 1


def test_an_analyst_cannot_grant_a_permission(analyst):
    response = analyst.post(
        screen.GRANT_URL,
        data={"user_id": "whoever", "code": APPROVE, "reason": "because"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert not _live_rows()


def test_an_analyst_cannot_compose_a_role(analyst):
    response = analyst.post(
        screen.ROLES_URL,
        data={"name": "Legal reviewer", "description": "", "codes": [APPROVE]},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_the_administrator_gets_the_screen(admin):
    assert admin.get(SCREEN_URL).status_code == 200


def test_the_screen_is_never_cached(admin):
    """A page listing who may approve what does not belong in a shared cache."""
    headers = admin.get(SCREEN_URL).headers
    assert headers["Cache-Control"] == "no-store"


# ----------------------------------------------------------------- the matrix


def test_every_person_and_every_permission_is_on_the_page(admin):
    body = admin.get(SCREEN_URL).text
    for account in demo_account_list():
        assert account.email in body, f"{account.email} is not in the matrix"
    for code in PERMISSION_CODES:
        assert code in body, f"{code} is not a column"


def test_another_companys_people_are_not_in_the_matrix(admin):
    with session_scope() as session:
        ensure_system_roles(session)
        create_user(
            session,
            RIVAL,
            email="analyst@rival.example",
            display_name="A rival analyst",
            password="rival-analyst-password",
            actor="system:test",
        )

    body = admin.get(SCREEN_URL).text
    assert "analyst@rival.example" not in body
    assert "A rival analyst" not in body


def test_a_cell_held_through_a_role_names_the_role(admin):
    """The whole point of the two weights: where a cell came from is a fact."""
    row = _block_for(admin.get(SCREEN_URL).text, _email(ROLE_OBLIGATION_OWNER))
    assert screen.SOURCE_ROLE in row
    assert ROLE_OBLIGATION_OWNER in row


def test_a_direct_grant_reads_differently_from_a_role_grant(admin):
    """Two weights, not two colours: the cell says which, in a word.

    The analyst holds action.propose through their role and action.approve
    through nothing but this row. Both cells are held; they must not render the
    same, because revoking the role changes one of them and not the other.
    """
    who = _email(ROLE_ANALYST)
    _grant_directly(who, APPROVE, reason="signs while the director is away")

    row = _block_for(admin.get(SCREEN_URL).text, who)
    role_cell = row.split(PROPOSE, 1)[1][:400]
    direct_cell = row.split(APPROVE, 1)[1][:400]

    assert screen.SOURCE_ROLE in role_cell
    assert screen.SOURCE_DIRECT in direct_cell
    assert screen.CELL_CLASS_ROLE in role_cell
    assert screen.CELL_CLASS_DIRECT in direct_cell
    assert screen.CELL_CLASS_ROLE != screen.CELL_CLASS_DIRECT


def test_a_direct_grant_carries_the_reason_somebody_gave_for_it(admin):
    """An exception nobody explained can be counted and cannot be reviewed."""
    _grant_directly(_email(ROLE_ANALYST), APPROVE, reason="Indiana counsel, docket 44-2")
    assert "Indiana counsel, docket 44-2" in admin.get(SCREEN_URL).text


def test_a_revoked_direct_grant_is_not_held(admin):
    """Revoked, never deleted -- and never read as live."""
    who = _email(ROLE_ANALYST)
    _grant_directly(who, APPROVE, reason="ended in March", revoked=True)

    row = _block_for(admin.get(SCREEN_URL).text, who)
    cell = row.split(APPROVE, 1)[1][:400]
    assert screen.SOURCE_DIRECT not in cell


def test_a_direct_grant_the_check_does_not_honour_says_so(admin):
    """The announcement, computed against the check rather than against a guess.

    Whether permissions_for_user unions the direct grants depends on work this
    branch does not own. So the test asks the function and requires the page to
    agree with it: honoured and silent, or not honoured and saying which.
    """
    who = _email(ROLE_ANALYST)
    _grant_directly(who, APPROVE, reason="deputising for the director")

    honoured = APPROVE in _effective(who)
    row = _block_for(admin.get(SCREEN_URL).text, who)

    assert screen.SOURCE_DIRECT in row
    assert (screen.DIRECT_NOT_HONOURED in row) is not honoured


def test_a_suspended_account_holds_nothing_and_the_row_says_why(admin):
    """Suspension is a whole control or it is half a control."""
    who = _email(ROLE_OBLIGATION_OWNER)
    _grant_directly(who, "threshold.set", reason="acting administrator in July")
    with session_scope() as session:
        person = user_by_email(session, COMPANY, who)
        person.status = STATUS_SUSPENDED

    row = _block_for(admin.get(SCREEN_URL).text, who)
    assert STATUS_SUSPENDED in row
    assert screen.INACTIVE_NOTE in row


# ------------------------------------------------- the cell, on its own


@pytest.mark.parametrize(
    "roles, direct, effective, active, source, note",
    [
        # Held through a role and the check agrees. The ordinary case.
        ((ROLE_ANALYST,), False, True, True, screen.SOURCE_ROLE, ""),
        # Held directly and the check agrees.
        ((), True, True, True, screen.SOURCE_DIRECT, ""),
        # Held both ways. Revoking the role would not take it away, and the cell
        # has to say so or somebody will think it did.
        ((ROLE_ANALYST,), True, True, True, screen.SOURCE_BOTH, screen.BOTH_NOTE),
        # Held and not effective, because the account is not active. By design.
        ((ROLE_ANALYST,), False, False, False, screen.SOURCE_ROLE, screen.INACTIVE_NOTE),
        ((), True, False, False, screen.SOURCE_DIRECT, screen.INACTIVE_NOTE),
        # Held directly, account active, and the check does not return it. The
        # union read is not in this build, and the screen must not imply it is.
        ((), True, False, True, screen.SOURCE_DIRECT, screen.DIRECT_NOT_HONOURED),
        # A live role grant the check does not honour is a defect, not a design.
        ((ROLE_ANALYST,), False, False, True, screen.SOURCE_ROLE, screen.ROLE_NOT_HONOURED),
        # The check grants what no row on this page explains.
        ((), False, True, True, screen.SOURCE_NONE, screen.GRANTED_ELSEWHERE),
        # Held by nobody, through nothing. The only silent empty cell.
        ((), False, False, True, screen.SOURCE_NONE, ""),
    ],
)
def test_the_cell_says_which_of_the_eight_states_it_is(
    roles, direct, effective, active, source, note
):
    """No blank cell is allowed to stand for two different facts."""
    assert screen.cell_state(
        role_names=roles, direct=direct, effective=effective, active=active
    ) == (source, note)


# ------------------------------------------------------- the conflict register


def test_a_held_conflict_is_named_and_permitted(admin):
    """The judgement the whole feature rests on. Named, explained, not blocked.

    The analyst proposes through their role. Give them approval directly and
    they hold both sides of the control docs/security.html rests on. The screen
    must name them, name the pair, print the argument, say how each side was
    obtained -- and refuse nothing.
    """
    who = _email(ROLE_ANALYST)
    _grant_directly(who, APPROVE, reason="the only other person is on leave")

    body = admin.get(SCREEN_URL).text
    register = _section(body, screen.CONFLICT_ANCHOR)

    assert who in register
    assert PROPOSE in register and APPROVE in register
    # The sentence that argues the pair, from the vocabulary in models.py --
    # not a shorter one written on this screen.
    argument = next(
        why for left, right, why in screen.CONFLICT_PAIRS
        if {left, right} == {PROPOSE, APPROVE}
    )
    assert argument[:60] in register
    # How each side was obtained: one through a role, one directly.
    assert ROLE_ANALYST in register
    assert screen.SOURCE_DIRECT in register
    # And it is permitted.
    assert screen.CONFLICT_STANCE in body


def test_the_conflict_register_is_not_dressed_as_an_error(admin):
    """Shouting at somebody for an arrangement they chose switches a control off."""
    _grant_directly(_email(ROLE_ANALYST), APPROVE)
    body = admin.get(SCREEN_URL).text

    register = _section(body, screen.CONFLICT_ANCHOR)
    assert 'role="alert"' not in register
    assert "alarm" not in register
    assert screen.CONFLICT_STANCE in register


def test_no_conflict_held_is_said_out_loud_rather_than_left_blank(admin):
    """An empty panel and a panel nobody computed look identical."""
    body = admin.get(SCREEN_URL).text
    register = _section(body, screen.CONFLICT_ANCHOR)

    assert screen.NO_CONFLICTS in register
    # The declared pairs are listed even when nobody holds one, so a reader can
    # see what was looked for.
    for left, right, _why in screen.CONFLICT_PAIRS:
        assert left in register and right in register


def test_a_conflict_held_entirely_through_roles_is_registered_too(admin):
    """Nothing about this depends on a grant being direct.

    An obligation owner approves. Grant them the admin role as well and they
    hold user.manage and action.approve -- a pair this repository has already
    argued about, and one no direct grant was involved in.
    """
    who = _email(ROLE_OBLIGATION_OWNER)
    with session_scope() as session:
        person = user_by_email(session, COMPANY, who)
        identity.grant_role(
            session,
            COMPANY,
            user_id=person.id,
            role_name=ROLE_ADMIN,
            actor="system:test",
        )

    register = _section(admin.get(SCREEN_URL).text, screen.CONFLICT_ANCHOR)
    assert who in register
    assert "user.manage" in register and APPROVE in register


def test_the_register_can_be_taken_away_as_a_file(admin):
    """Exportable is part of the promise: an auditor asks for it in a month."""
    _grant_directly(_email(ROLE_ANALYST), APPROVE, reason="deputising")

    response = admin.get(screen.EXPORT_URL)
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert response.headers["Cache-Control"] == "no-store"

    body = response.text
    assert _email(ROLE_ANALYST) in body
    assert APPROVE in body
    assert "deputising" in body


def test_the_export_is_behind_the_same_permission_as_the_screen(analyst):
    assert analyst.get(screen.EXPORT_URL).status_code == 403


# --------------------------------------------------------- explained refusals


def test_a_permission_the_administrator_does_not_hold_cannot_be_granted(admin):
    """The ceiling, said in words rather than drawn as a dead cell.

    The admin role deliberately holds no approval permission, so the one
    permission an administrator most wants to hand out is the one they may not.
    A greyed cell with no sentence is how somebody decides the product is broken.
    """
    body = admin.get(SCREEN_URL).text
    assert screen.ceiling_refusal(APPROVE) in body
    assert APPROVE in body

    # And the menu does not offer it, so the sentence is the explanation for
    # something the reader can see rather than an answer to a question they
    # never asked.
    form = _section(body, screen.GRANT_ANCHOR)
    assert f'<option value="{APPROVE}"' not in form
    assert '<option value="threshold.set"' in form


def test_granting_a_permission_the_administrator_lacks_writes_nothing(admin):
    """The ceiling holds through the form as well as in what the form offers.

    The refusal comes from app/state/permissions.py and is printed in its words.
    The screen adds nothing to it except the sentence already under the grid,
    which is what a reader saw before they tried.
    """
    person = _user(_email(ROLE_ANALYST))
    response = admin.post(
        screen.GRANT_URL,
        data={"user_id": person.id, "code": APPROVE, "reason": "we need it"},
    )

    assert response.status_code == 200
    assert not _live_rows()
    assert screen.ceiling_refusal(APPROVE) in response.text
    # The write layer's own sentence, not a paraphrase of it.
    assert "do not hold" in response.text


def test_a_grant_with_no_reason_writes_nothing(admin):
    """Required means non-empty. The write layer says so and the screen prints it."""
    person = _user(_email(ROLE_ANALYST))
    response = admin.post(
        screen.GRANT_URL,
        data={"user_id": person.id, "code": "audit.read", "reason": "   "},
    )
    assert not _live_rows()
    assert "a reason is required" in response.text
    # And the sentence explaining why is on the page before anybody types.
    assert screen.GRANT_NEEDS_REASON in response.text


def test_a_code_the_product_does_not_define_is_refused_by_name(admin):
    person = _user(_email(ROLE_ANALYST))
    response = admin.post(
        screen.GRANT_URL,
        data={"user_id": person.id, "code": "action.bless", "reason": "because"},
    )
    assert not _live_rows()
    assert "action.bless" in response.text


# ------------------------------------------------------- giving and taking back


def test_a_grant_through_the_screen_writes_the_row_and_shows_it(admin):
    """End to end: the form, the row, and the cell that reads differently.

    audit.read rather than action.approve, because the admin role deliberately
    holds no approval permission and nobody may hand on what they do not hold.
    That is the ceiling being real rather than drawn.
    """
    person = _user(_email(ROLE_ANALYST))
    response = admin.post(
        screen.GRANT_URL,
        data={
            "user_id": person.id,
            "code": "audit.read",
            "reason": "reviewing the chain for the November filing",
        },
    )
    assert response.status_code == 200

    rows = _live_rows()
    assert len(rows) == 1
    assert rows[0].user_id == person.id
    assert rows[0].code == "audit.read"
    assert rows[0].reason == "reviewing the chain for the November filing"
    assert rows[0].granted_by_user_id == _user(_email(ROLE_ADMIN)).id

    block = _block_for(response.text, person.email)
    cell = block.split("audit.read", 1)[1][:400]
    assert screen.SOURCE_DIRECT in cell
    assert screen.CELL_CLASS_DIRECT in cell
    # And the reason is on the page, where an auditor reads it.
    assert "reviewing the chain for the November filing" in response.text


def test_taking_a_grant_back_keeps_the_row_and_marks_it_whole(admin):
    """Revoked, never deleted, and revoked_at and revoked_by written together."""
    person = _user(_email(ROLE_ANALYST))
    admin.post(
        screen.GRANT_URL,
        data={"user_id": person.id, "code": "audit.read", "reason": "November filing"},
    )
    response = admin.post(
        screen.REVOKE_URL,
        data={
            "user_id": person.id,
            "code": "audit.read",
            "reason": "the filing went out",
        },
    )

    assert response.status_code == 200
    assert not _live_rows()
    with session_scope() as session:
        row = session.query(UserPermission).one()
        assert row.revoked_at is not None
        assert row.revoked_by_user_id == _user(_email(ROLE_ADMIN)).id

    block = _block_for(response.text, person.email)
    cell = block.split("audit.read", 1)[1][:400]
    assert screen.SOURCE_DIRECT not in cell


def test_taking_one_back_with_no_reason_leaves_it_in_place(admin):
    """The same rule in the other direction, and for the same audit reason."""
    person = _user(_email(ROLE_ANALYST))
    admin.post(
        screen.GRANT_URL,
        data={"user_id": person.id, "code": "audit.read", "reason": "November filing"},
    )
    response = admin.post(
        screen.REVOKE_URL,
        data={"user_id": person.id, "code": "audit.read", "reason": ""},
    )
    assert len(_live_rows()) == 1
    assert screen.REVOKE_NEEDS_REASON in response.text


def test_a_permission_held_twice_says_so_and_is_still_in_force(admin):
    """The case a single mark would get wrong in both directions.

    The analyst holds claim.read through their role. Give it to them directly as
    well and the cell must say both -- because revoking the role would NOT take
    it away, and somebody who read one mark would think it had. It is also
    perfectly in force, so the export must not read the note as a doubt.
    """
    person = _user(_email(ROLE_ANALYST))
    admin.post(
        screen.GRANT_URL,
        data={
            "user_id": person.id,
            "code": "claim.read",
            "reason": "keeps it if the analyst role is taken away in the handover",
        },
    )

    block = _block_for(admin.get(SCREEN_URL).text, person.email)
    cell = block.split("claim.read", 1)[1][:500]
    assert screen.CELL_CLASS_BOTH in cell
    assert screen.BOTH_NOTE in cell

    line = next(
        row
        for row in admin.get(screen.EXPORT_URL).text.splitlines()
        if person.email in row and "claim.read" in row
    )
    assert ",yes," in line, f"a permission held two ways read as not in force: {line}"


def test_a_grant_that_creates_a_conflict_is_warned_about_first_then_registered(admin):
    """The whole judgement, end to end, in one test.

    The obligation owner approves. Giving them user.manage puts both sides of a
    declared pair on one desk -- and the product says so BEFORE the grant, makes
    it anyway, and names them afterwards. Warned, permitted, recorded.
    """
    person = _user(_email(ROLE_OBLIGATION_OWNER))

    ahead = admin.get(
        f"{SCREEN_URL}?grant={person.id}&code=user.manage"
    ).text
    assert "user.manage" in ahead and APPROVE in ahead
    argument = next(
        why
        for left, right, why in screen.CONFLICT_PAIRS
        if {left, right} == {"user.manage", APPROVE}
    )
    assert argument[:60] in ahead

    after = admin.post(
        screen.GRANT_URL,
        data={
            "user_id": person.id,
            "code": "user.manage",
            "reason": "covering the VP for two weeks",
        },
    ).text
    assert len(_live_rows()) == 1

    register = _section(after, screen.CONFLICT_ANCHOR)
    assert person.email in register
    assert "user.manage" in register and APPROVE in register
    assert screen.SOURCE_DIRECT in register
    assert ROLE_OBLIGATION_OWNER in register


# --------------------------------------------------------------- custom roles


def test_every_role_this_company_can_grant_is_listed_with_what_it_holds(admin):
    section = _section(admin.get(SCREEN_URL).text, screen.ROLES_ANCHOR)
    for name in SYSTEM_ROLE_NAMES:
        assert name in section
    assert "user.manage" in section


def test_a_system_role_is_not_editable_and_the_screen_says_why(admin):
    """It offers the copy instead, which is the honest version of the same act."""
    section = _section(admin.get(SCREEN_URL).text, screen.ROLES_ANCHOR)
    assert screen.SYSTEM_ROLE_NOT_EDITABLE in section
    assert screen.COPY_OFFER in section


def test_editing_a_system_role_is_refused_and_changes_nothing(admin):
    """Refused, and answered with the offer rather than with a dead end."""
    before = _codes_on("role-analyst")

    response = admin.post(
        screen.role_edit_url("role-analyst"),
        data={"description": "narrower", "reason": "tidying", "codes": ["claim.read"]},
    )
    assert response.status_code == 200
    assert screen.SYSTEM_ROLE_NOT_EDITABLE in response.text
    assert screen.COPY_OFFER in response.text
    assert _codes_on("role-analyst") == before


def test_a_composed_role_may_not_take_a_system_name(admin):
    """A role called analyst holding a different set is worse than no role."""
    response = admin.post(
        screen.ROLES_URL,
        data={
            "name": ROLE_ANALYST,
            "description": "ours",
            "reason": "we want a narrower one",
            "codes": ["claim.read"],
        },
    )
    assert not _composed()
    # The write layer's refusal, printed. The screen's own sentence about the
    # rule is on the compose form either way.
    assert "system role" in response.text
    assert screen.NAME_IS_TAKEN in response.text


def test_a_composed_role_needs_a_name(admin):
    response = admin.post(
        screen.ROLES_URL,
        data={
            "name": "   ",
            "description": "",
            "reason": "because",
            "codes": ["claim.read"],
        },
    )
    assert not _composed()
    assert "name" in response.text


def test_composing_a_role_writes_it_and_puts_it_on_the_page(admin):
    """The company names its own set, and the page says who composed it and when."""
    response = admin.post(
        screen.ROLES_URL,
        data={
            "name": "Indiana regulatory counsel",
            "description": "Reads the filings for one jurisdiction.",
            "reason": "the Indiana docket needs a reader who is not an analyst",
            "codes": ["claim.read", "change.read"],
        },
    )
    assert response.status_code == 200

    composed = _composed()
    assert [role.name for role in composed] == ["Indiana regulatory counsel"]
    assert composed[0].company_id == COMPANY
    assert composed[0].created_by_user_id == _user(_email(ROLE_ADMIN)).id
    assert composed[0].created_at is not None
    # Composed from scratch. NULL means exactly that and never "unknown".
    assert composed[0].derived_from_role_id is None
    assert _codes_on(composed[0].id) == {"claim.read", "change.read"}

    section = _section(response.text, screen.ROLES_ANCHOR)
    assert "Indiana regulatory counsel" in section
    assert "Reads the filings for one jurisdiction." in section


def test_a_copy_records_the_role_it_was_started_from(admin):
    """A fork the company named and dated, not a copy the product performed."""
    admin.post(
        screen.ROLES_URL,
        data={
            "name": "Deputy administrator",
            "description": "The VP is away for a fortnight.",
            "reason": "holiday cover, agreed with the VP",
            "codes": ["user.manage", "audit.read"],
            "derived_from_role_id": "role-admin",
        },
    )
    composed = _composed()
    assert len(composed) == 1
    assert composed[0].derived_from_role_id == "role-admin"

    section = _section(admin.get(SCREEN_URL).text, screen.ROLES_ANCHOR)
    assert "started from admin" in section


def test_a_copy_of_a_role_holding_more_than_you_do_is_refused(admin):
    """Copying a permission set is granting it, so the ceiling reaches forks too."""
    response = admin.post(
        screen.ROLES_URL,
        data={
            "name": "Second approver",
            "description": "",
            "reason": "we are short-handed",
            "codes": ["claim.read", APPROVE],
            "derived_from_role_id": "role-obligation_owner",
        },
    )
    assert not _composed()
    assert APPROVE in response.text


def test_the_copy_form_offers_the_system_roles_codes_to_start_from(admin):
    """Copying a role starts from what that role holds, not from an empty set."""
    body = admin.get(f"{SCREEN_URL}?copy=role-obligation_owner").text
    assert screen.COPYING_FROM.format(name=ROLE_OBLIGATION_OWNER) in body
    # The checkbox for a code that role holds is ticked before anybody types.
    form = _section(body, screen.COMPOSE_ANCHOR)
    approve_box = form.split(f'value="{APPROVE}"', 1)[1][:120]
    assert "checked" in approve_box


def test_a_composed_role_can_be_edited_and_the_set_is_asserted_whole(admin):
    """Editing replaces the set. A code unticked is removed, not left behind."""
    admin.post(
        screen.ROLES_URL,
        data={
            "name": "Filing reader",
            "description": "Reads filings.",
            "reason": "the filings clerk needs read access",
            "codes": ["claim.read", "change.read"],
        },
    )
    role_id = _composed()[0].id

    response = admin.post(
        screen.role_edit_url(role_id),
        data={
            "description": "Reads filings and nothing else.",
            "reason": "change.read was more than the job needs",
            "codes": ["claim.read"],
        },
    )
    assert response.status_code == 200
    assert _codes_on(role_id) == {"claim.read"}


def test_another_companys_role_cannot_be_edited_through_this_screen(admin):
    """Not visible here, not editable here, and the two answers are the same one."""
    with session_scope() as session:
        session.add(
            Role(id="role-rival-thing", company_id=RIVAL, name="Their role")
        )

    response = admin.post(
        screen.role_edit_url("role-rival-thing"),
        data={"description": "mine now", "reason": "why not", "codes": ["claim.read"]},
    )
    assert response.status_code == 200
    assert "role-rival-thing" in response.text
    with session_scope() as session:
        assert session.get(Role, "role-rival-thing").description == ""


def test_a_role_that_holds_no_declared_pair_says_so(admin):
    """The column is never blank: nothing found and nothing looked for differ."""
    section = _section(admin.get(SCREEN_URL).text, screen.ROLES_ANCHOR)
    assert screen.NO_CONFLICT_IN_SET in section


def test_the_audit_chain_still_verifies_after_all_of_it(admin):
    """Every write on this screen goes through the chain, and it stays intact."""
    person = _user(_email(ROLE_ANALYST))
    admin.post(
        screen.GRANT_URL,
        data={"user_id": person.id, "code": "audit.read", "reason": "November filing"},
    )
    admin.post(
        screen.REVOKE_URL,
        data={"user_id": person.id, "code": "audit.read", "reason": "it went out"},
    )
    admin.post(
        screen.ROLES_URL,
        data={
            "name": "Filing reader",
            "description": "Reads filings.",
            "reason": "the clerk needs read access",
            "codes": ["claim.read"],
        },
    )
    admin.post(
        screen.GRANT_URL,
        data={"user_id": person.id, "code": APPROVE, "reason": "refused, on purpose"},
    )

    with session_scope() as session:
        assert verify_chain(session, COMPANY)


# ---------------------------------------------------------------- responsive


def test_the_template_hardcodes_no_width_wider_than_a_phone():
    """tests/test_responsive.py scans inline attributes and the stylesheet.

    This template carries its rules in its own head, which neither of those
    scans reaches, so the rule is asserted here rather than by nobody.
    """
    text = (screen.TEMPLATE_DIR / screen.TEMPLATE).read_text()
    offenders = [
        width
        for width in re.findall(r"(?:min-)?width:\s*([0-9]{3,})px", text)
        if int(width) >= 400
    ]
    assert not offenders, f"fixed widths wider than a phone: {offenders}"


def test_the_matrix_becomes_one_block_per_person_on_a_narrow_screen(admin):
    """A matrix is the worst case for a phone, and scrolling it sideways is not
    an answer: the name scrolls off the left and the ticked box you are looking
    at belongs to nobody you can see.

    So the template stacks it. The rule is asserted rather than the look, which
    is all a test without a browser can honestly claim.
    """
    text = (screen.TEMPLATE_DIR / screen.TEMPLATE).read_text()
    assert re.search(r"@media\s*\(max-width:\s*6[0-9]rem\)", text), (
        "the matrix has no narrow-screen rule"
    )
    assert "data-label" in text, "a stacked cell with no label names nothing"

    body = admin.get(SCREEN_URL).text
    assert "<table" in body, "the stylesheet's own table rules need a table"
    assert 'data-label="action.approve"' in body
    assert screen.NARROW_NOTE in body


# ------------------------------------------ the wiring nobody in this branch owns


def test_the_router_owns_paths_no_other_screen_claims():
    """/permissions sits clear of /admin, /users, /workflow, /chat, /s and /invite."""
    import importlib
    import pkgutil

    import app.web.views as views

    mine = {
        (method, route.path)
        for route in screen.router.routes
        for method in sorted(getattr(route, "methods", ()) or ())
    }
    assert mine

    for info in pkgutil.iter_modules(views.__path__):
        if info.name == "permissions":
            continue
        module = importlib.import_module(f"app.web.views.{info.name}")
        if not hasattr(module, "router"):
            continue
        for route in module.router.routes:
            for method in sorted(getattr(route, "methods", ()) or ()):
                assert (method, route.path) not in mine, (
                    f"{info.name} already claims {method} {route.path}"
                )
