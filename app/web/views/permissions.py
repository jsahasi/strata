"""Who may do what here: the matrix, the exceptions, and the conflicts held.

WHO OPENS THIS SCREEN AND WHAT THEY CAME FOR. Somebody holding `user.manage`,
with one of three jobs in hand: give one person one permission their role does
not carry, compose a bundle the three system roles cannot express and name it,
or answer an auditor asking who can approve what. All three are the same
question read at different widths, so they are one page rather than three.

WHY THE PRODUCT NEEDED THIS. There are three roles -- analyst, obligation_owner,
admin -- and data/real/ holds 102 filings by people whose titles they cannot
express: Analyst II, Manager, Director, Deputy Director, VP Pricing and
Planning, Indiana Regulatory Counsel, Legal Assistant. That shortage has already
produced a defect rather than an inconvenience: scripts/seed_route.py has a step
called "Legal review" and a step called "Officer signs the filing", and both are
assigned to role:admin because no legal role and no officer role exist. The
person who draws the route is the person who approves through it, which is the
exact hole the comment on `workflow.manage` in app/state/models.py warns about.
The route is a lie told by a vocabulary shortage, and a vocabulary is what fixes
it.

THE JUDGEMENT THIS SCREEN IS BUILT ON, AND IT IS THE WHOLE FEATURE: DO NOT
FORBID A CONFLICT, SHOW IT. If one person holds both `action.propose` and
`action.approve`, that is the company's decision. In a four-person regulatory
team the administrator may legitimately be the person who approves; there is
nobody else. In a large utility they must not be, and no table here can know
which. So the register names the person, names the pair, prints the argument
this repository already made about why anybody minds, says how each side was
obtained -- and blocks nothing. Prior restraint would make the product unusable
for the small team; silence would make it useless to the large one.
app/auth/policy.py takes the same shape with DEMO_SELF_APPROVAL: the downgrade
is permitted and it is never quiet.

THE REGISTER IS NOT COLOURED AS AN ERROR, on purpose. Shouting at somebody for
an arrangement they chose is how a control gets switched off. It states the
pair, states what it means for an audit, and leaves the decision where it
belongs.

WHAT A CELL MEANS, AND WHY TWO WEIGHTS RATHER THAN TWO COLOURS. A permission
held through a role and the same permission granted directly are different facts
with different futures: revoke the role and the first disappears while the
second stays. So the two are drawn with different fills AND labelled in words --
"through a role", "granted directly" -- because a reader with no colour vision,
or a printed copy of this page in a review pack, must still be able to tell them
apart. cell_state() below is the one place that decides, and it has eight
answers, not two.

WHERE THIS SCREEN GETS ITS TRUTH, AND WHY IT ASKS TWICE. The authority is
app/state/identity.py::permissions_for_user -- the same function the permission
gate calls. The record is the rows: live role grants, and the live
user_permissions rows app/state/permissions.py reads. A screen built from the
rows alone would draw a filled cell for a permission the gate refuses; a screen
built from the gate alone could not say where a permission came from. So it
reads both and, WHERE THEY DISAGREE, SAYS SO in the cell rather than picking a
winner. Four of cell_state's eight answers are that disagreement, and one of
them -- a live direct grant on an active account that the check does not return
-- would mean the union read had come apart. The screen would say so rather than
draw authority nobody has.

WHAT THIS SCREEN DOES NOT DECIDE. It writes nothing itself and validates almost
nothing. Every rule about who may grant what -- the ceiling that stops anybody
handing on a permission they do not hold, the reason that may not be blank, the
system role that may not be edited or silently forked, the code that is not in
the vocabulary -- is decided in app/state/permissions.py, checked once, audited
there, and PRINTED HERE IN THAT MODULE'S OWN WORDS. A check copied into this
view would be a second opinion about who may do what, and the day the two
disagree the screen is what somebody reads.

The one rule this screen does apply for itself is which controls it draws: a
cell for a permission this reader cannot hand on is not offered, and the reason
is on the page rather than left as a dead square. That is a rendering decision,
and the write layer refuses the same thing again for the request a forged form
would send.

THERE IS NO CSRF TOKEN ON THESE FORMS. SameSite=Lax on the session cookie is the
whole of the defence, exactly as app/web/views/auth.py concedes for the login
form and app/web/views/users_admin.py for provisioning. It stops a cross-site
POST in every browser this product supports, and it is not a token.
"""

import csv
import io
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# THE MODULE, NOT THE NAMES. app/web/views/users_admin.py gives the reasoning at
# length: app/auth/policy.py reads its approval mode at import and the tests
# reload it, so an `except` bound to a class captured at import time stops
# catching the refusal the reloaded module raises, and a 403 becomes a 500.
from app.auth import policy
from app.state.db import session_scope
from app.state.identity import (
    PERMISSION_DESCRIPTIONS,
    SYSTEM_ROLE_PERMISSIONS,
    permissions_for_role,
    permissions_for_user,
    role_grants_for_user,
    roles_for_company,
    user_for_company,
    users_for_company,
)
from app.state.models import (
    PERMISSION_CODES,
    SEGREGATION_CONFLICTS,
    STATUS_ACTIVE,
    STATUS_INVITED,
    STATUS_SUSPENDED,
    SYSTEM_ROLE_NAMES,
    conflicts_in,
    role_is_system,
)

# THE WRITE LAYER AND THE RECORD, UNDER AN ALIAS. It is imported as a module for
# the reason given above about reloads, and aliased because this file and that
# one share a name -- `permissions.grant` inside app/web/views/permissions.py
# would read as a call on itself.
#
# IMPORTED AT THE TOP RATHER THAN REACHED FOR LAZILY, unlike the mail layer in
# users_admin.py. That screen still provisions a login without app/notify and
# only loses the email. This one is made of that module: without it there is no
# direct-grant read, and a matrix rendered from role grants alone would show a
# smaller truth as the whole truth -- every exception in the company missing,
# with nothing on the page to say so.
from app.state import permissions as store
from app.web import TEMPLATES_DIR
from app.web.deps import current_user
from app.web.templating import build_templates

# The page shell and the tenant, decided in one place for every screen.
from app.web.views.proceedings import chrome, current_company_id, unresolved_escalations

router = APIRouter()
templates = build_templates()

TEMPLATE = "permissions.html"

# Read by the responsive guard in tests/test_permissions_screen.py. That file's
# scans reach inline style attributes and the stylesheet; this template carries
# its own rules in the head, which neither of them sees.
TEMPLATE_DIR = TEMPLATES_DIR
TEMPLATES_OWNED = (TEMPLATE,)

# /permissions, clear of /admin, /users, /workflow, /chat, /s and /invite. One
# noun, and no path here overlaps another router's -- there is a test.
PERMISSIONS_URL = "/permissions"
GRANT_URL = "/permissions/grant"
REVOKE_URL = "/permissions/revoke"
ROLES_URL = "/permissions/roles"
EXPORT_URL = "/permissions/register.csv"


def role_edit_url(role_id: str) -> str:
    """Where a composed role's save button posts. One spelling, route and form."""
    return f"{ROLES_URL}/{role_id}"


# The permission the whole screen sits behind -- reading it, and every write on
# it. Imported rather than restated, so this screen and the write layer cannot
# end up gating on two different strings.
#
# NO NEW PERMISSION CODE FOR THIS SCREEN, and that is a decision rather than an
# omission. Managing permissions IS user.manage. A `permission.grant` code
# beside it would be a second answer to the question that column already
# answers, and the day the two disagree the call site that asked the wrong one
# wins.
MANAGE = store.MANAGE

REFUSED = (
    f"{MANAGE} is required to open this screen. It lists what every person in "
    "this workspace may do and how they came to be able to do it, which is a "
    "record about other people. Ask an administrator."
)

# The list is who may approve what. It does not belong in a shared cache, and
# the Referer of any link clicked from here should not carry it either.
SCREEN_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


# ---------------------------------------------------------------------------
# The words that carry the distinctions this screen exists for
#
# Constants rather than literals in a template, because the tests assert on them
# and a screen whose meaning lives in a Jinja string is a screen whose meaning
# can be edited away in a diff nobody reads.
# ---------------------------------------------------------------------------

#: How a cell was obtained. Four values, and no cell renders without one. The
#: words, not app/state/permissions.py's VIA_ROLE and VIA_DIRECT: those two are
#: the stored vocabulary and these are the sentence a person reads.
SOURCE_ROLE = "through a role"
SOURCE_DIRECT = "granted directly"
SOURCE_BOTH = "through a role and granted directly"
SOURCE_NONE = "not held"

#: The two weights. Different classes, so the difference survives a printout and
#: a reader with no colour vision -- see the head of this module.
CELL_CLASS_ROLE = "pcell--role"
CELL_CLASS_DIRECT = "pcell--direct"
CELL_CLASS_BOTH = "pcell--both"
CELL_CLASS_NONE = "pcell--none"

#: Held both ways. Revoking the role does not take it away, and a reader who
#: thinks it did has been misled by a screen that showed one mark.
BOTH_NOTE = (
    "also granted directly, so revoking the role would not take this away"
)
#: Held on paper and not in force, because the account is not active. By design:
#: suspension reaches direct grants as well as role grants, or it is half a
#: control.
INACTIVE_NOTE = (
    "the account is not active, so nothing here is in force until it is"
)
#: A live direct grant the permission check does not return, on an active
#: account. Nothing about the design explains this one: the union read in
#: identity.py::permissions_for_user covers direct grants, so the record and the
#: gate have come apart and the gate is what runs.
DIRECT_NOT_HONOURED = (
    "this row exists and the permission check does not return it. The register "
    "and the gate disagree, and the gate is what runs -- so the person cannot "
    "actually do this. Read app/state/identity.py::permissions_for_user before "
    "trusting either half of this cell."
)
#: A live role grant the check does not return, on an active account.
ROLE_NOT_HONOURED = (
    "a live role grant carries this and the permission check does not return "
    "it. That is a defect rather than a design; read the grant rows before "
    "trusting either half of this cell."
)
#: The gate grants something no row on this page accounts for.
GRANTED_ELSEWHERE = (
    "the permission check returns this and no role grant or direct grant on "
    "this page explains it. Something outside this screen is granting it."
)

#: The stance on a held conflict. It is the product's central judgement, so it
#: is printed on the page rather than left in a docstring.
CONFLICT_STANCE = (
    "Nothing here is blocked. A small team may hold both sides of a separation "
    "because there is nobody else to hold them, and a product that refused "
    "would be a product that team cannot use. What an audit needs is that the "
    "arrangement is named, dated and readable, which is what this register is."
)
NO_CONFLICTS = (
    "Nobody in this workspace holds both sides of a declared pair. That is this "
    "screen having looked, not this screen having nothing to say: the pairs it "
    "checked are listed below."
)
CONFLICT_MEANING = (
    "An auditor reading this asks one question: who signed, and could the same "
    "person have produced what they signed. Each line below answers it in "
    "advance. app/auth/policy.py still refuses an approval by whoever touched "
    "the claim, so a pair listed here is a standing arrangement rather than a "
    "waived check."
)
#: Why a suspended person cannot be in the register. The grid above still shows
#: their marks, so the two together are the whole answer and neither alone is.
CONFLICT_ACTIVE_ONLY = (
    "This register covers active accounts. A suspended account holds nothing "
    "while it is suspended, so it cannot appear here -- and the grid above "
    "still shows what it would hold again the day it is reinstated."
)

#: The vocabulary, imported and not restated. app/state/models.py owns it, and a
#: second copy here would be a second taxonomy for a reader to reconcile.
CONFLICT_PAIRS = SEGREGATION_CONFLICTS

#: A grant naming a code the product no longer defines. conflicts_in() refuses
#: to report a clean bill of health over a vocabulary it does not recognise, and
#: it is right to; a screen must still render, so it says which codes it could
#: not read rather than either crashing or quietly checking the rest in silence.
UNKNOWN_CODE_NOTE = (
    "held permissions this product no longer defines: {codes}. No conflict was "
    "computed over them, because a clean answer over a vocabulary this build "
    "does not recognise reads as safety. The grants need migrating."
)

#: Where the tests and the template agree the sections are. One spelling.
CONFLICT_ANCHOR = 'id="conflicts"'
MATRIX_ANCHOR = 'id="matrix"'
GRANT_ANCHOR = 'id="grant"'
REVOKE_ANCHOR = 'id="take-back"'
COMPOSE_ANCHOR = 'id="compose"'
ROLES_ANCHOR = 'id="roles"'

#: What a composed role is for, and the rule about its name.
ROLE_NEEDS_NAME = (
    "A composed role needs a name of your own. The name is not a nicety: it is "
    "what every screen, every grant and every audit row will say afterwards."
)
NAME_IS_TAKEN = (
    "That name belongs to a system role. A role called analyst holding a "
    "different set from the one in every document and every other screen is "
    "worse than no role at all, so the product refuses the name rather than "
    "shadowing the meaning. Give this set a name of your own."
)
SYSTEM_ROLE_NOT_EDITABLE = (
    "A system role is not editable. It ships with the product, docs/security.html "
    "describes what it holds, and every tenant gets the same one -- so an edit "
    "here would change what a word means for people who never asked."
)
COPY_OFFER = (
    "Start a new role from this one instead. The copy takes your name for it "
    "and records where it came from, so an auditor comparing it against the "
    "default can see what you changed. A fork the company named is a decision; "
    "a fork the product performed quietly is an accident with a plausible label."
)
COPYING_FROM = (
    "Starting from {name}. Every permission that role holds is ticked below. "
    "Untick what this one should not have, and give it a name of your own."
)
NO_CONFLICT_IN_SET = "no declared pair inside this set"

#: WHY A ROLE CELL CARRIES NO TOGGLE. Said on the cell rather than left as a
#: silence, because a control that is absent with no explanation reads as a
#: control that is broken. Taking one code off one person from here would mean
#: editing what the role means for everybody who holds it, which is the change
#: this whole feature exists to make impossible by accident.
ROLE_CELL_NOTE = (
    "held through a role, so it is not taken back here. Grant or revoke the "
    "role on the logins screen, or compose a role that does not carry this."
)

#: A reason is required and required means non-empty. The write layer enforces
#: it; this is the sentence under the box that says why before anybody types.
GRANT_NEEDS_REASON = (
    "A direct grant needs a reason, and a blank one is not a reason. Every row "
    "here is an exception to the role grid a reviewer reads, and an exception "
    "nobody explained can be counted but cannot be reviewed."
)
REVOKE_NEEDS_REASON = (
    "Taking a permission back needs a reason too. The row is kept and marked "
    "rather than deleted, so what somebody could do in March stays answerable, "
    "and a revocation nobody explained is the same hole as a grant nobody "
    "explained."
)

#: WHAT A PHONE GETS INSTEAD OF THE GRID, said on the page rather than left for
#: somebody to discover. A matrix is the worst case for a narrow screen and
#: sideways scrolling is not an answer: the name scrolls off the left edge and
#: the reader is looking at a mark belonging to nobody they can see. So the grid
#: stacks into one block per person listing what that person holds, the empty
#: cells drop out, and the two forms below -- which are menus and a box -- are
#: the way to change anything at any width.
NARROW_NOTE = (
    "On a narrow screen this grid becomes one block per person, listing only "
    "what that person holds. A fourteen-column matrix scrolled sideways puts "
    "the name off the left edge and leaves you looking at a mark that belongs "
    "to nobody you can see. To give somebody a permission on a phone, use the "
    "form below: it works at any width."
)

EXPORT_NOTE = (
    "Take the register away as a file. It is the same rows this page shows, "
    "including who granted each exception and why, for the auditor who asks in "
    "six months."
)


def ceiling_refusal(code: str) -> str:
    """Why this administrator cannot hand on this permission, and what would fix it.

    THE CEILING IS NOT INVENTED HERE AND IS NOT ENFORCED HERE.
    app/state/permissions.py refuses the write and audits the attempt; this is
    the sentence that stops the cell being a dead square with no explanation. A
    greyed control with no reason is how a person concludes the product is
    broken, and the honest answer is short: somebody who holds the code has to
    make the grant.
    """
    carriers = tuple(
        name for name, codes in SYSTEM_ROLE_PERMISSIONS.items() if code in codes
    )
    if carriers:
        where = "the " + " or ".join(carriers) + " role carries it"
    else:
        where = (
            "no system role carries it, so it can only come from a composed role "
            "or from a direct grant somebody else makes"
        )
    return (
        f"You cannot grant {code}, because you do not hold it. Nobody hands on "
        f"authority they were never given. To grant it you would need {code} "
        f"yourself: {where}. Whoever holds it can make this grant, and that "
        "grant will show in the register on this page."
    )


# ---------------------------------------------------------------------------
# What one cell means
# ---------------------------------------------------------------------------


def cell_state(
    *,
    role_names: tuple[str, ...],
    direct: bool,
    effective: bool,
    active: bool,
) -> tuple[str, str]:
    """How this permission is held, and what has to be said about it. Pure.

    Eight answers, and only one of them is a silent empty cell. The inputs are
    the record -- live role grants, a live direct grant -- and the authority,
    which is what permissions_for_user returned. Where they disagree this
    function says which way, because a screen that quietly preferred one of them
    would be either drawing authority nobody has or hiding a grant somebody
    made.

    A pure function so the eight states can be tested without a database and
    without depending on the state of any one workspace. See the head of
    tests/test_permissions_screen.py.
    """
    by_role = bool(role_names)

    if by_role and direct:
        source = SOURCE_BOTH
    elif by_role:
        source = SOURCE_ROLE
    elif direct:
        source = SOURCE_DIRECT
    else:
        source = SOURCE_NONE

    if not by_role and not direct:
        # Nothing on this page accounts for it. Silence is right only when the
        # gate agrees that it is not held.
        return source, (GRANTED_ELSEWHERE if effective else "")

    if not effective:
        if not active:
            return source, INACTIVE_NOTE
        return source, (ROLE_NOT_HONOURED if by_role else DIRECT_NOT_HONOURED)

    return source, (BOTH_NOTE if source == SOURCE_BOTH else "")


def _conflicts_for(codes) -> tuple[tuple[tuple[str, str, str], ...], str]:
    """The pairs held, and a sentence about any code this build cannot read.

    conflicts_in() raises on a code outside PERMISSION_CODES rather than
    reporting no conflict for it, which is the right refusal and would be the
    wrong 500. So the unknown codes are held out, named, and the rest are
    checked -- and the naming is what stops the partial answer reading as a
    whole one.
    """
    held = set(codes)
    unknown = sorted(code for code in held if code not in PERMISSION_CODES)
    pairs = conflicts_in(held - set(unknown))
    if not unknown:
        return pairs, ""
    return pairs, UNKNOWN_CODE_NOTE.format(codes=", ".join(unknown))


def _how(grants) -> str:
    """The sentence naming where one half of a conflict came from.

    Built from app/state/permissions.py::Grant rows, which carry the source per
    grant -- so a code held through a role AND directly says both, in the order
    the register read them, rather than the screen choosing one to print.
    """
    said = []
    for grant in grants:
        if grant.via == store.VIA_ROLE:
            said.append(f"{SOURCE_ROLE}: {grant.role_name}")
        elif grant.reason:
            said.append(f"{SOURCE_DIRECT} ({grant.reason})")
        else:
            said.append(SOURCE_DIRECT)
    # Empty means the pair was reported over codes no grant on this page
    # explains, which cell_state says the same thing about in the grid.
    return "; ".join(said) if said else GRANTED_ELSEWHERE


# ---------------------------------------------------------------------------
# What the template renders
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cell:
    """One person against one permission. Never blank without meaning it."""

    code: str
    source: str
    css: str
    note: str
    #: The live role grants carrying this code. Empty where none do.
    roles: tuple[str, ...]
    #: The reason on the direct grant, and who made it. Empty where there is none.
    reason: str
    granted_by: str
    granted_at: str
    held: bool
    #: What the permission check says, which is the only field here that answers
    #: "can they do it today". Held and in force are different questions and a
    #: cell that folded them would report a suspended account as able to act.
    in_force: bool
    #: Whether this reader may hand this code on, and why not when they may not.
    grantable: bool
    refusal: str


@dataclass(frozen=True, slots=True)
class PersonRow:
    """One person, their roles, and one cell per permission the product defines."""

    user_id: str
    display_name: str
    email: str
    status: str
    status_badge: str
    active: bool
    roles: tuple[str, ...]
    cells: tuple[Cell, ...]
    held_count: int
    conflict_count: int
    note: str


@dataclass(frozen=True, slots=True)
class ConflictRow:
    """One person holding both sides of one declared pair, and how they got each."""

    user_id: str
    display_name: str
    email: str
    left_code: str
    right_code: str
    why: str
    left_how: str
    right_how: str


@dataclass(frozen=True, slots=True)
class ExceptionRow:
    """One direct grant, in the shape an auditor reads it."""

    user_id: str
    display_name: str
    email: str
    code: str
    reason: str
    granted_by: str
    granted_at: str
    honoured: bool
    note: str


@dataclass(frozen=True, slots=True)
class RoleRow:
    """One role this company can grant, and whether it may be edited."""

    role_id: str
    name: str
    description: str
    system: bool
    codes: tuple[str, ...]
    holders: int
    derived_from: str
    created_by: str
    created_at: str
    conflicts: tuple[tuple[str, str, str], ...]
    refusal: str


_STATUS_BADGE = {
    STATUS_ACTIVE: "badge--active",
    STATUS_INVITED: "badge--draft",
    STATUS_SUSPENDED: "badge--closed",
}


def _stamp(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------


def _role_codes(session: Session, company_id: str, roles) -> dict[str, frozenset[str]]:
    """role id -> the codes it carries, through the scoped read.

    permissions_for_role() resolves by name rather than by id, and that is safe
    here for a reason worth stating: roles_for_company returns the system roles
    and this company's own, and ck_roles_company_role_never_shadows_a_system_name
    stops a company reusing a system name -- so a name in that set names one row.
    """
    return {
        role.id: permissions_for_role(session, company_id, role.name)
        for role in roles
    }


def _people(
    session: Session,
    company_id: str,
    *,
    viewer_codes: frozenset[str],
) -> tuple[list[PersonRow], list[ConflictRow], list[ExceptionRow]]:
    """The matrix, the conflict register and the exception list, from one read.

    THE REGISTER IS NOT COMPUTED HERE. app/state/permissions.py::conflicts is the
    one place a conflict is decided, so the page and an export and whatever asks
    that module tomorrow cannot answer differently. What this function does is
    turn each Conflict into the two sentences a person reads.
    """
    users = users_for_company(session, company_id)
    names = {person.id: person.display_name for person in users}
    roles = roles_for_company(session, company_id)
    role_name = {role.id: role.name for role in roles}
    codes_of = _role_codes(session, company_id, roles)

    direct_by_user: dict[str, dict[str, object]] = {}
    for row in store.direct_grants_for_company(session, company_id):
        direct_by_user.setdefault(row.user_id, {})[row.code] = row

    held_pairs: dict[str, list[ConflictRow]] = {}
    for found in store.conflicts(session, company_id):
        held_pairs.setdefault(found.user_id, []).append(
            ConflictRow(
                user_id=found.user_id,
                display_name=found.display_name,
                email=found.email,
                left_code=found.left,
                right_code=found.right,
                why=found.why,
                left_how=_how(found.left_grants),
                right_how=_how(found.right_grants),
            )
        )

    people: list[PersonRow] = []
    conflicts: list[ConflictRow] = []
    exceptions: list[ExceptionRow] = []

    for person in users:
        active = person.status == STATUS_ACTIVE
        live = role_grants_for_user(
            session, company_id, person.id, include_revoked=False
        )
        by_code: dict[str, list[str]] = {}
        for grant in live:
            for code in codes_of.get(grant.role_id, frozenset()):
                by_code.setdefault(code, []).append(
                    role_name.get(grant.role_id, grant.role_id)
                )

        direct = direct_by_user.get(person.id, {})
        # The authority. Every cell is checked against it, and every
        # disagreement is printed rather than resolved.
        effective = permissions_for_user(session, company_id, person.id)

        cells: list[Cell] = []
        for code in PERMISSION_CODES:
            carried = tuple(sorted(set(by_code.get(code, ()))))
            grant = direct.get(code)
            source, note = cell_state(
                role_names=carried,
                direct=grant is not None,
                effective=code in effective,
                active=active,
            )
            css = {
                SOURCE_ROLE: CELL_CLASS_ROLE,
                SOURCE_DIRECT: CELL_CLASS_DIRECT,
                SOURCE_BOTH: CELL_CLASS_BOTH,
                SOURCE_NONE: CELL_CLASS_NONE,
            }[source]
            cells.append(
                Cell(
                    code=code,
                    source=source,
                    css=css,
                    note=note,
                    roles=carried,
                    reason=grant.reason if grant else "",
                    granted_by=(
                        names.get(grant.granted_by_user_id, grant.granted_by_user_id)
                        if grant
                        else ""
                    ),
                    granted_at=_stamp(grant.granted_at) if grant else "",
                    held=source != SOURCE_NONE,
                    in_force=code in effective,
                    grantable=code in viewer_codes,
                    refusal="" if code in viewer_codes else ceiling_refusal(code),
                )
            )

            if grant is not None:
                exceptions.append(
                    ExceptionRow(
                        user_id=person.id,
                        display_name=person.display_name,
                        email=person.email,
                        code=code,
                        reason=grant.reason,
                        granted_by=names.get(
                            grant.granted_by_user_id, grant.granted_by_user_id
                        ),
                        granted_at=_stamp(grant.granted_at),
                        honoured=code in effective,
                        note=note,
                    )
                )

        mine = held_pairs.get(person.id, [])
        conflicts.extend(mine)
        _pairs, unreadable = _conflicts_for(set(by_code) | set(direct))
        people.append(
            PersonRow(
                user_id=person.id,
                display_name=person.display_name,
                email=person.email,
                status=person.status,
                status_badge=_STATUS_BADGE.get(person.status, "badge--draft"),
                active=active,
                roles=tuple(
                    sorted(
                        {role_name.get(g.role_id, g.role_id) for g in live}
                    )
                ),
                cells=tuple(cells),
                held_count=sum(1 for cell in cells if cell.held),
                conflict_count=len(mine),
                note=" ".join(
                    part
                    for part in (("" if active else INACTIVE_NOTE), unreadable)
                    if part
                ),
            )
        )

    return people, conflicts, exceptions


def _roles(session: Session, company_id: str) -> list[RoleRow]:
    """Every role this company can grant, system ones first, with what they hold."""
    roles = roles_for_company(session, company_id)
    by_id = {role.id: role for role in roles}
    codes_of = _role_codes(session, company_id, roles)

    holders: dict[str, int] = {}
    for person in users_for_company(session, company_id):
        for grant in role_grants_for_user(
            session, company_id, person.id, include_revoked=False
        ):
            holders[grant.role_id] = holders.get(grant.role_id, 0) + 1

    rows: list[RoleRow] = []
    for role in roles:
        system = role_is_system(role)
        origin = getattr(role, "derived_from_role_id", None)
        author = getattr(role, "created_by_user_id", None)
        made = getattr(role, "created_at", None)
        person = user_for_company(session, company_id, author) if author else None
        pairs, _unreadable = _conflicts_for(codes_of.get(role.id, frozenset()))
        rows.append(
            RoleRow(
                role_id=role.id,
                name=role.name,
                description=role.description,
                system=system,
                codes=tuple(sorted(codes_of.get(role.id, frozenset()))),
                holders=holders.get(role.id, 0),
                derived_from=(
                    by_id[origin].name if origin and origin in by_id else (origin or "")
                ),
                created_by=person.display_name if person is not None else "",
                created_at=_stamp(made),
                conflicts=pairs,
                refusal=SYSTEM_ROLE_NOT_EDITABLE if system else "",
            )
        )
    rows.sort(key=lambda row: (not row.system, row.name))
    return rows


def _preview(person: PersonRow | None, code: str) -> tuple[tuple[str, str, str], ...]:
    """The conflicts a grant would create, said BEFORE it is made.

    Not a warning that stops anything -- nothing on this screen stops anything.
    It is the same register, one grant into the future, so a person choosing to
    put both sides of a separation on one desk does it knowing that is what they
    are doing. A product that only tells you afterwards has made the decision
    for you and then blamed you for it.
    """
    if person is None or code not in PERMISSION_CODES:
        return ()
    holds = {cell.code for cell in person.cells if cell.held}
    after, _unreadable = _conflicts_for(holds | {code})
    before, _also = _conflicts_for(holds)
    return tuple(pair for pair in after if pair not in before)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _user_id(request: Request) -> str:
    principal = current_user(request)
    return principal.user_id if principal is not None else ""


def _may_manage(session: Session, company_id: str, user_id: str) -> str | None:
    """None when allowed, else the reason. The refusal is audited on the way out.

    require() writes the audit row and then raises. The raise is caught HERE,
    inside the caller's session scope, so the row commits with the response.
    Catching it outside would roll the refusal back with everything else, and a
    denial nobody recorded is a denial nobody can review.
    """
    try:
        policy.require(session, company_id, user_id, MANAGE)
    except policy.PermissionDenied as denied:
        return denied.reason
    return None


def _waiting(session: Session, company_id: str) -> int:
    """The nav's held-for-review count. Zero on a database with no tables."""
    try:
        return len(unresolved_escalations(session, company_id))
    except SQLAlchemyError:
        return 0


def _shell(company_id: str, waiting: int) -> dict:
    """base.html's context, plus the URLs every form on this page posts to."""
    context = chrome(
        company_id,
        page_title="Permissions",
        # Not in the masthead. Marking one of the six as current while an
        # administrator is here would be a small lie in an aria-current
        # attribute.
        nav_active="",
        review_count=waiting,
    )
    context.update(
        {
            "permissions_url": PERMISSIONS_URL,
            "grant_url": GRANT_URL,
            "revoke_url": REVOKE_URL,
            "roles_url": ROLES_URL,
            "export_url": EXPORT_URL,
            "permission": MANAGE,
            "refused": REFUSED,
            "codes": PERMISSION_CODES,
            "code_words": PERMISSION_DESCRIPTIONS,
            "conflict_pairs": CONFLICT_PAIRS,
            "conflict_stance": CONFLICT_STANCE,
            "conflict_meaning": CONFLICT_MEANING,
            "conflict_active_only": CONFLICT_ACTIVE_ONLY,
            "no_conflicts": NO_CONFLICTS,
            "no_conflict_in_set": NO_CONFLICT_IN_SET,
            "system_not_editable": SYSTEM_ROLE_NOT_EDITABLE,
            "copy_offer": COPY_OFFER,
            "role_needs_name": ROLE_NEEDS_NAME,
            "name_is_taken": NAME_IS_TAKEN,
            "grant_needs_reason": GRANT_NEEDS_REASON,
            "revoke_needs_reason": REVOKE_NEEDS_REASON,
            "role_cell_note": ROLE_CELL_NOTE,
            "narrow_note": NARROW_NOTE,
            "export_note": EXPORT_NOTE,
            "system_role_names": SYSTEM_ROLE_NAMES,
        }
    )
    return context


def _forbidden(
    request: Request, company_id: str, reason: str, waiting: int
) -> HTMLResponse:
    """One template, two answers. A separate page would describe one screen twice."""
    context = _shell(company_id, waiting)
    context["reason"] = reason
    return templates.TemplateResponse(
        request, TEMPLATE, context, status_code=403, headers=SCREEN_HEADERS
    )


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def _page(
    request: Request,
    *,
    outcome_ok: bool = False,
    outcome_heading: str = "",
    outcome_text: str = "",
    grant_user: str = "",
    grant_code: str = "",
    take_user: str = "",
    take_code: str = "",
    copy_role: str = "",
    typed: dict | None = None,
) -> HTMLResponse:
    """Read everything and render it, with whatever just happened above it.

    ONE FUNCTION FOR EVERY ENTRY POINT -- the plain GET and the four writes --
    because all of them end on this page and none of them may show a different
    matrix from the one the GET shows. Everything is read AFTER the write, from
    the rows, so what is on screen is what is in the database rather than what
    the write layer said it did.
    """
    company_id = current_company_id()
    viewer = _user_id(request)
    reason, waiting = None, 0
    people, conflicts, exceptions, roles = [], [], [], []
    viewer_codes: frozenset[str] = frozenset()

    try:
        with session_scope() as session:
            waiting = _waiting(session, company_id)
            reason = _may_manage(session, company_id, viewer)
            if reason is None:
                viewer_codes = permissions_for_user(session, company_id, viewer)
                people, conflicts, exceptions = _people(
                    session, company_id, viewer_codes=viewer_codes
                )
                roles = _roles(session, company_id)
    except SQLAlchemyError as failure:
        # A database that cannot be read is a permission check that did not run,
        # and a check that did not run is not a check that passed. A matrix
        # rendered from a failed read would also be this screen asserting that
        # nobody holds anything, which is a claim and a false one.
        reason = (
            "this workspace's roles and grants could not be read, so nothing "
            f"confirmed that you hold {MANAGE} and nothing here could be shown "
            f"honestly ({failure.__class__.__name__})"
        )

    if reason is not None:
        return _forbidden(request, company_id, reason, waiting)

    started_from = next((row for row in roles if row.role_id == copy_role), None)
    target = next((row for row in people if row.user_id == grant_user), None)

    context = _shell(company_id, waiting)
    context.update(
        {
            "people": people,
            "conflicts": conflicts,
            "exceptions": exceptions,
            "roles": roles,
            "ungrantable": tuple(
                (code, ceiling_refusal(code))
                for code in PERMISSION_CODES
                if code not in viewer_codes
            ),
            # The same answer the other way round, so the menu can leave a code
            # out without the template doing arithmetic on a tuple.
            "grantable": tuple(
                code for code in PERMISSION_CODES if code in viewer_codes
            ),
            "outcome_ok": outcome_ok,
            "outcome_heading": outcome_heading,
            "outcome_text": outcome_text,
            "grant_user": grant_user,
            "grant_code": grant_code,
            "grant_person": target,
            "grant_preview": _preview(target, grant_code),
            "take_user": take_user,
            "take_code": take_code,
            # One entry per person rather than per grant, so somebody holding
            # two exceptions is not in the menu twice.
            "exception_people": tuple(
                dict.fromkeys(
                    (row.user_id, row.display_name, row.email) for row in exceptions
                )
            ),
            "copy_role": copy_role,
            "copy_from": started_from,
            "copying_from": (
                COPYING_FROM.format(name=started_from.name) if started_from else ""
            ),
            "typed": typed or {},
        }
    )
    # 200 on a refusal from the write layer as well as on a success, which is
    # what users_admin.py does for the same reason: nobody was denied and
    # nothing failed. The refusal is a sentence at the top of a page that still
    # works, and the reader can act on it without leaving.
    return templates.TemplateResponse(
        request, TEMPLATE, context, headers=SCREEN_HEADERS
    )


@router.get(PERMISSIONS_URL, response_class=HTMLResponse)
def matrix(
    request: Request,
    grant: str = Query(default=""),
    code: str = Query(default=""),
    take: str = Query(default=""),
    copy: str = Query(default=""),
) -> HTMLResponse:
    """The matrix, the conflict register, the exceptions and the roles.

    The query parameters prefill a form rather than doing anything. A cell
    offering to give or take a permission is a link back to this page with the
    pair named, so the reason box gets its own moment on screen -- a required
    reason typed into a box the size of a tick is a reason nobody writes, and
    both directions need one.
    """
    return _page(
        request,
        grant_user=grant,
        grant_code=code if grant else "",
        take_user=take,
        take_code=code if take else "",
        copy_role=copy,
    )


# ---------------------------------------------------------------------------
# The writes
#
# EACH ONE DOES THREE THINGS AND NOT A FOURTH: check that this reader may open
# the screen at all, call app/state/permissions.py, and print what came back. No
# validation is repeated here. That module refuses an unknown code, a blank
# reason, a system role name, an edit of a system role and a grant above the
# caller's ceiling -- each with a sentence written for a person -- and it audits
# every refusal it makes. A copy of any of those checks in this file would be a
# second opinion about who may do what, and the screen is what somebody reads.
# ---------------------------------------------------------------------------


def _refuse(request: Request, heading: str, text: str, **prefill) -> HTMLResponse:
    return _page(
        request, outcome_ok=False, outcome_heading=heading, outcome_text=text, **prefill
    )


def _gate(request: Request) -> tuple[str, str, str | None, int]:
    """The company, the reader, why they were refused, and the review count."""
    company_id = current_company_id()
    viewer = _user_id(request)
    with session_scope() as session:
        waiting = _waiting(session, company_id)
        refused = _may_manage(session, company_id, viewer)
    return company_id, viewer, refused, waiting


@router.post(GRANT_URL, response_class=HTMLResponse)
def grant(
    request: Request,
    user_id: str = Form(default=""),
    code: str = Form(default=""),
    reason: str = Form(default=""),
) -> HTMLResponse:
    """Give one person one permission their role does not carry.

    FOR THE CASE A ROLE CANNOT EXPRESS: the deputy who signs while the director
    is away, the counsel who approves in one state and nowhere else. A standing
    bundle is a role, and composing one is the other half of this screen.

    THE CEILING IS THE SECURITY OF THE FEATURE AND IT IS NOT ENFORCED HERE.
    app/state/permissions.py refuses a grant of a code the actor does not hold,
    audits the attempt, and says so in words this screen prints. What this
    screen does is not offer the control in the first place and print the reason
    under the grid -- so the refusal is read before the click rather than after
    it.
    """
    company_id, viewer, refused, waiting = _gate(request)
    if refused is not None:
        return _forbidden(request, company_id, refused, waiting)

    prefill = {"grant_user": user_id, "grant_code": code, "typed": {"reason": reason}}
    heading, text, ok = "", "", True
    with session_scope() as session:
        try:
            written = store.grant(
                session,
                company_id,
                actor=viewer,
                user_id=user_id,
                code=code,
                reason=reason,
            )
            heading = f"{written.code} granted."
        except (TypeError, AttributeError) as mismatch:
            # The contract moved under us. Announced rather than raised: a 500
            # on an admin screen says nothing about which side changed.
            ok, heading = False, "The write layer did not accept this call."
            text = (
                f"app/state/permissions.py::grant did not accept the arguments "
                f"this screen passes ({mismatch})."
            )
        except (ValueError, policy.PermissionDenied) as refusal:
            # Its words, not a paraphrase. Why a grant is a duplicate, why a
            # reason is required, why a ceiling was hit -- those are decisions
            # this screen did not make.
            ok, heading, text = False, "That grant was refused.", str(refusal)

    return _page(
        request,
        outcome_ok=ok,
        outcome_heading=heading,
        outcome_text=text,
        **({} if ok else prefill),
    )


@router.post(REVOKE_URL, response_class=HTMLResponse)
def revoke(
    request: Request,
    user_id: str = Form(default=""),
    code: str = Form(default=""),
    reason: str = Form(default=""),
) -> HTMLResponse:
    """Take a direct grant back. The row stays and gains who and when.

    A REASON IS REQUIRED IN THIS DIRECTION TOO, which is why the cell links to a
    form instead of carrying a one-click button. Revoked-never-deleted is what
    keeps "what could this person do in March" answerable, and a revocation with
    nothing recorded about why leaves the same hole as a grant with nothing
    recorded about why.

    THERE IS NO CEILING ON THIS ONE, and the asymmetry is
    app/state/permissions.py's: handing authority out is the only direction that
    escalates, and an administrator who could not take back a permission they do
    not hold would be unable to close a hole they can see.
    """
    company_id, viewer, refused, waiting = _gate(request)
    if refused is not None:
        return _forbidden(request, company_id, refused, waiting)

    prefill = {"take_user": user_id, "take_code": code, "typed": {"reason": reason}}
    heading, text, ok = "", "", True
    with session_scope() as session:
        try:
            store.revoke(
                session,
                company_id,
                actor=viewer,
                user_id=user_id,
                code=code,
                reason=reason,
            )
            heading = f"{code} taken back."
        except (TypeError, AttributeError) as mismatch:
            ok, heading = False, "The write layer did not accept this call."
            text = (
                f"app/state/permissions.py::revoke did not accept the arguments "
                f"this screen passes ({mismatch})."
            )
        except (ValueError, policy.PermissionDenied) as refusal:
            ok, heading, text = False, "That revocation was refused.", str(refusal)

    return _page(
        request,
        outcome_ok=ok,
        outcome_heading=heading,
        outcome_text=text,
        **({} if ok else prefill),
    )


def _picked(raw: list[str]) -> tuple[str, ...]:
    """The codes a form sent, de-duplicated and sorted.

    Sorted so two people composing the same set produce the same set and a diff
    of two roles is readable. Nothing is dropped and nothing is validated: an
    unknown code goes to the write layer and comes back refused by name, because
    a role quietly missing a permission somebody ticked is worse than a role
    nobody wrote.
    """
    return tuple(sorted({code for code in raw if code}))


@router.post(ROLES_URL, response_class=HTMLResponse)
def compose_role(
    request: Request,
    name: str = Form(default=""),
    description: str = Form(default=""),
    reason: str = Form(default=""),
    codes: list[str] = Form(default=[]),
    derived_from_role_id: str = Form(default=""),
) -> HTMLResponse:
    """Compose a permission set and give it the company's own name.

    THE NAME IS THE FEATURE. A company that needs a bundle the three system
    roles cannot express gets one, and it has to name it, because a role called
    "analyst" holding a different set from every ADR and every other screen is
    worse than no role at all.

    A COPY RECORDS WHERE IT CAME FROM. derived_from_role_id is sent by the copy
    form and left empty by the compose form, and empty means COMPOSED FROM
    SCRATCH rather than unknown -- a fork that left it empty would be a false
    statement about where a permission set came from, and "our legal reviewer
    began as obligation_owner and dropped audit.read" is the first thing an
    auditor comparing a custom set against the defaults asks.

    TWO SENTENCES ARE ASKED FOR AND THEY ARE NOT THE SAME ONE. The description
    goes on the row and says what the role IS, for everybody who reads the
    register later; the reason goes into the audit chain and says why it was
    composed on that day.
    """
    company_id, viewer, refused, waiting = _gate(request)
    if refused is not None:
        return _forbidden(request, company_id, refused, waiting)

    prefill = {
        "copy_role": derived_from_role_id,
        "typed": {
            "name": name,
            "description": description,
            "reason": reason,
            "codes": list(codes),
        },
    }
    heading, text, ok = "", "", True
    with session_scope() as session:
        try:
            role = store.create_role(
                session,
                company_id,
                actor=viewer,
                name=name,
                codes=_picked(list(codes)),
                reason=reason,
                description=description,
                derived_from_role_id=derived_from_role_id.strip() or None,
            )
            heading = f"{role.name} composed."
            pairs, _unreadable = _conflicts_for(_picked(list(codes)))
            if pairs:
                text = (
                    "This set holds both sides of a declared pair. That is "
                    "permitted, it is written into the audit chain, and it is "
                    "in the register at the top of this page for everybody who "
                    "holds the role."
                )
        except (TypeError, AttributeError) as mismatch:
            ok, heading = False, "The write layer did not accept this call."
            text = (
                f"app/state/permissions.py::create_role did not accept the "
                f"arguments this screen passes ({mismatch})."
            )
        except (ValueError, policy.PermissionDenied) as refusal:
            ok, heading, text = False, "That role was refused.", str(refusal)

    return _page(
        request,
        outcome_ok=ok,
        outcome_heading=heading,
        outcome_text=text,
        **({} if ok else prefill),
    )


@router.post(ROLES_URL + "/{role_id}", response_class=HTMLResponse)
def edit_role(
    request: Request,
    role_id: str,
    description: str = Form(default=""),
    reason: str = Form(default=""),
    codes: list[str] = Form(default=[]),
) -> HTMLResponse:
    """Change what a role this company composed holds.

    A SYSTEM ROLE IS REFUSED TWICE, and the two refusals do different jobs. The
    write layer refuses because a rule that lives only in a screen is not a
    rule. This one refuses first so the answer can be the OFFER -- the copy
    form, prefilled with that role's codes -- rather than a sentence with
    nowhere to go. Neither of them forks quietly: an implicit copy-on-write is a
    fallback that does not announce itself, and the copy would need a name the
    product invented, which is exactly the role-called-analyst-that-is-not-analyst
    this whole feature refuses.

    THE ROLE IS NAMED BY ID IN THE URL AND BY NAME IN THE CALL. A form posts an
    id; app/state/permissions.py::edit_role takes a name. The id is resolved
    against roles_for_company here, which is the read that already scopes to
    this tenant, and another tenant's id resolves to nothing.
    """
    company_id, viewer, refused, waiting = _gate(request)
    if refused is not None:
        return _forbidden(request, company_id, refused, waiting)

    with session_scope() as session:
        role = next(
            (
                row
                for row in roles_for_company(session, company_id)
                if row.id == role_id
            ),
            None,
        )
        system = role is not None and role_is_system(role)
        label = role.name if role is not None else ""

    if role is None:
        return _refuse(
            request,
            "There is no such role here.",
            f"No role {role_id!r} is available to this workspace. Another "
            "tenant's role is not visible here and is not editable here.",
        )
    if system:
        return _refuse(
            request,
            f"{label} is a system role.",
            f"{SYSTEM_ROLE_NOT_EDITABLE} {COPY_OFFER}",
            copy_role=role_id,
        )

    heading, text, ok = "", "", True
    with session_scope() as session:
        try:
            store.edit_role(
                session,
                company_id,
                actor=viewer,
                name=label,
                reason=reason,
                codes=_picked(list(codes)),
                description=description,
            )
            heading = f"{label} saved."
        except (TypeError, AttributeError) as mismatch:
            ok, heading = False, "The write layer did not accept this call."
            text = (
                f"app/state/permissions.py::edit_role did not accept the "
                f"arguments this screen passes ({mismatch})."
            )
        except (ValueError, policy.PermissionDenied) as refusal:
            ok, heading, text = False, "That change was refused.", str(refusal)

    return _page(request, outcome_ok=ok, outcome_heading=heading, outcome_text=text)


# ---------------------------------------------------------------------------
# The export
# ---------------------------------------------------------------------------

EXPORT_COLUMNS = (
    "person",
    "email",
    "status",
    "permission",
    "held",
    "roles",
    "reason",
    "granted_by",
    "granted_at",
    "in_force",
    "conflicts",
)


@router.get(EXPORT_URL)
def export(request: Request) -> Response:
    """The register as a file: every permission held, how, and why.

    ONE ROW PER PERMISSION HELD, not one per person, because the question asked
    of this file is "who could approve" and a cell in a spreadsheet is where
    that gets answered. Codes nobody holds are left out; the matrix on the
    screen is where absence is read.

    Behind the same permission as the screen. An export that skipped the gate
    would be the screen's contents at a URL with no lock on it.
    """
    company_id = current_company_id()
    viewer = _user_id(request)

    with session_scope() as session:
        refused = _may_manage(session, company_id, viewer)
        if refused is None:
            viewer_codes = permissions_for_user(session, company_id, viewer)
            people, conflicts, _exceptions = _people(
                session, company_id, viewer_codes=viewer_codes
            )
    if refused is not None:
        return PlainTextResponse(
            f"{refused}. {REFUSED}", status_code=403, headers=SCREEN_HEADERS
        )

    pairs: dict[str, list[str]] = {}
    for row in conflicts:
        pairs.setdefault(row.user_id, []).append(f"{row.left_code}+{row.right_code}")

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for person in people:
        for cell in person.cells:
            if not cell.held:
                continue
            writer.writerow(
                [
                    person.display_name,
                    person.email,
                    person.status,
                    cell.code,
                    cell.source,
                    " ".join(cell.roles),
                    cell.reason,
                    cell.granted_by,
                    cell.granted_at,
                    # What the permission check says, which is the only column
                    # here that answers "can they do it today". Read from the
                    # gate rather than from the presence of a note: a cell held
                    # two ways carries a note and is perfectly in force.
                    "yes" if cell.in_force else f"no: {cell.note}",
                    " ".join(pairs.get(person.user_id, ())),
                ]
            )

    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={
            **SCREEN_HEADERS,
            "Content-Disposition": 'attachment; filename="permissions-register.csv"',
        },
    )


# ---------------------------------------------------------------------------
# HANDOFF -- what this screen needs from files it does not own
#
# app/main.py            `permissions` added to the import from app.web.views,
#                        and the router included beside users_admin:
#
#                          from app.web.views import (
#                              ...,
#                              permissions,
#                              ...,
#                          )
#                          app.include_router(permissions.router)
#
#                        UNTIL THAT LANDS, tests/test_app_wiring.py::
#                        test_every_router_in_the_views_package_is_mounted FAILS,
#                        and it is meant to: that test exists precisely so a
#                        router nobody mounted fails in the suite rather than
#                        404ing in front of a reviewer. The six paths --
#                        /permissions, /permissions/grant, /permissions/revoke,
#                        /permissions/roles, /permissions/roles/{role_id} and
#                        /permissions/register.csv -- collide with nothing; there
#                        is a test in this branch that checks every other router
#                        in the package.
#
# app/web/templates/base.html
#                        Nothing is required. This screen is not in the six-item
#                        masthead, for the reason base.html's own note gives: a
#                        nav item most people cannot reach renders as dead text.
#                        If an admin menu is added later, /permissions belongs in
#                        it beside /users, /admin/workflows and /admin/shares.
#
# app/state/permissions.py
#                        Nothing required; this screen calls the module as it
#                        publishes itself. Two notes for whoever owns it.
#
#                        edit_role takes a role NAME and _editable_role already
#                        accepts a role_id. A form posts an id, so this screen
#                        resolves the id to a name before calling. Exposing
#                        role_id on edit_role would remove that step and the
#                        window it leaves open, which is a rename between the
#                        read and the write.
#
#                        conflicts() skips a suspended account, correctly -- it
#                        holds nothing while suspended. The grid on this screen
#                        still shows what that account would hold again on
#                        reinstatement, and the register says on itself that it
#                        covers active accounts only. Those two together are the
#                        whole answer; either alone is a half of it.
#
# scripts/seed_route.py  THE DEFECT THIS FEATURE EXISTS FOR IS STILL THERE. Its
#                        "Legal review" and "Officer signs the filing" steps are
#                        both role:admin. Now that a company can compose roles,
#                        the seed should compose a legal reviewer and an officer
#                        and point those two steps at them -- otherwise the
#                        demonstration route still shows one account approving
#                        its own routing, and this screen's register will name
#                        whoever holds it.
# ---------------------------------------------------------------------------


__all__ = [
    "BOTH_NOTE",
    "CELL_CLASS_BOTH",
    "CELL_CLASS_DIRECT",
    "CELL_CLASS_NONE",
    "CELL_CLASS_ROLE",
    "COMPOSE_ANCHOR",
    "CONFLICT_ACTIVE_ONLY",
    "CONFLICT_ANCHOR",
    "CONFLICT_MEANING",
    "CONFLICT_PAIRS",
    "CONFLICT_STANCE",
    "COPYING_FROM",
    "COPY_OFFER",
    "DIRECT_NOT_HONOURED",
    "EXPORT_COLUMNS",
    "EXPORT_URL",
    "GRANTED_ELSEWHERE",
    "GRANT_ANCHOR",
    "GRANT_NEEDS_REASON",
    "GRANT_URL",
    "INACTIVE_NOTE",
    "MANAGE",
    "MATRIX_ANCHOR",
    "NAME_IS_TAKEN",
    "NARROW_NOTE",
    "NO_CONFLICTS",
    "NO_CONFLICT_IN_SET",
    "PERMISSIONS_URL",
    "REFUSED",
    "REVOKE_ANCHOR",
    "REVOKE_NEEDS_REASON",
    "REVOKE_URL",
    "ROLES_ANCHOR",
    "ROLES_URL",
    "ROLE_CELL_NOTE",
    "ROLE_NEEDS_NAME",
    "ROLE_NOT_HONOURED",
    "SCREEN_HEADERS",
    "SOURCE_BOTH",
    "SOURCE_DIRECT",
    "SOURCE_NONE",
    "SOURCE_ROLE",
    "SYSTEM_ROLE_NOT_EDITABLE",
    "TEMPLATE",
    "TEMPLATES_OWNED",
    "TEMPLATE_DIR",
    "UNKNOWN_CODE_NOTE",
    "Cell",
    "ConflictRow",
    "ExceptionRow",
    "PersonRow",
    "RoleRow",
    "cell_state",
    "ceiling_refusal",
    "compose_role",
    "edit_role",
    "export",
    "grant",
    "matrix",
    "revoke",
    "role_edit_url",
    "router",
    "templates",
]
