"""One Jinja environment for every screen, and the two things base.html reads
that no view can hand it.

WHY THIS MODULE EXISTS. Every view module used to build its own
`Jinja2Templates(directory=TEMPLATES_DIR)`. Sixteen objects, sixteen
environments, sixteen sets of globals -- and base.html is rendered by all of
them. The moment the shell needed one fact that a view could not put in its
context, that fact had to be registered sixteen times or it reached one screen
and silently vanished from the other fifteen. A masthead that draws its menu on
twelve screens and drops it on four is worse than one that never had a menu: the
first teaches the reader that the product is unreliable, the second teaches them
where to look instead. So the object is built HERE, once, and every view asks
this factory for it. The next global costs one edit rather than sixteen.

The alternative was to pass the fact down through the view context. It was
rejected because the context is already built in three different places --
app/web/views/proceedings.py::chrome(), app/web/views/projects.py::_chrome(),
and a literal dict in app/web/views/changes.py -- and any view that forgot would
lose the menu with no error anywhere. The failure would look exactly like "this
person has no admin rights", which is the one thing the menu must never say by
accident.

WHAT BASE.HTML READS FROM HERE.

  admin_url   str    where the administrative screens are indexed
  admin_menu  fn     admin_menu(request) -> the screens THIS viewer may open

THE MENU IS COMPUTED PER RENDER, NEVER CACHED ON THE SESSION, and that is the
whole reason app/web/deps.py::Principal carries no permissions. Its docstring
says it plainly: a copy taken at sign-in would still say "may approve" after the
grant was revoked mid-session. So the menu asks the live grant rows on every
page, through app/auth/policy.py::has() -- the function written for exactly this
question, "decide whether to draw a button". has() is never the gate. The gate is
require(), and each of the screens below already calls it; drawing or not drawing
a link changes nothing about who may open the screen behind it.

WHY A LINK IS HIDDEN RATHER THAN GREYED OUT. base.html greys out the two screens
that need a proceeding or a change in hand, because the reader can act on that
-- open a proceeding and the link lights up. A permission is not like that. A
greyed-out row of administrative screens tells somebody who cannot use them what
exists, on every page, for ever, and gives them nothing to do about it. The
refusal page at /admin is where a person who typed the URL is answered.

WHAT THIS DOES NOT DO. It does not gate anything. It does not audit -- has()
writes nothing by design, and a chain holding one row per hidden button is a
chain nobody reads. It does not tell the difference between "you hold nothing"
and "the database could not be read": both draw no menu, because absence is
denial applies to a menu exactly as it applies to a citation, and a menu drawn
on a guess is a link that 403s on click.
"""

from dataclasses import dataclass

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError

from app.auth import policy
from app.state.db import session_scope
from app.web import TEMPLATES_DIR
from app.web.deps import current_user

# Where the administrative screens are indexed. It lives here rather than in
# app/web/views/admin_index.py because the masthead needs it and the masthead
# cannot import a view module -- see the note on admin_screens() below. The view
# imports it from here, so there is still one spelling of the path.
ADMIN_URL = "/admin"

#: The names base.html expects to find in the environment. Listed so a test can
#: ask every templates object in the views package whether it can render the
#: shell, rather than a person remembering to check.
MASTHEAD_GLOBALS = ("admin_url", "admin_menu")

#: Where the answer is kept for the life of one request. A page renders the
#: masthead once, but a screen that grows a second copy -- a footer menu, a
#: mobile drawer -- must not run the permission read twice and must not be able
#: to get two answers to one question inside one response. Same reasoning, and
#: the same shape, as the principal cache in app/web/deps.py::current_user.
_CACHE_ATTRIBUTE = "strata_admin_menu"


@dataclass(frozen=True, slots=True)
class AdminScreen:
    """One administrative screen, and the permission that opens it.

    Frozen and detached from the database, for the reason Principal gives: this
    is read at render time, long after the session that could answer anything
    further has closed.

    permission is the code the screen's OWN module gates on, imported from that
    module rather than restated here. Two spellings of one code is how a link
    gets drawn for somebody the screen behind it will refuse -- and a link that
    answers 403 on click is worse than no link, because the reader cannot tell
    "you may not" from "it is broken" and files a bug about the wrong one.
    """

    key: str
    label: str
    url: str
    permission: str
    blurb: str


def admin_screens() -> tuple[AdminScreen, ...]:
    """Every administrative screen in the product, in the order an admin wants
    them. Not filtered by permission -- admin_menu() does that.

    THE IMPORTS ARE INSIDE THE FUNCTION AND THAT IS DELIBERATE. Every view
    module imports build_templates() from this module at import time. If this
    module imported those view modules at ITS import time the cycle would close
    and nothing would start. Deferring the import to call time breaks it: by the
    time a template renders, every view module is already in sys.modules and
    these six lines are dictionary lookups.

    The alternative was to write the URLs and permission codes out again here as
    literals. That is the failure this codebase refuses everywhere else -- one
    fact with two homes, free to drift -- and the drift would be silent and
    exactly the kind that matters: a renamed URL draws a link to a 404, a
    renamed permission draws a link for the wrong people.

    /admin/workflows is on this list although it already has a way in from
    /workflow. It belongs here because it is an administrative screen and this
    is where they live, and because it is the one entry gated by a DIFFERENT
    code -- workflow.manage rather than user.manage. A menu whose every row
    happens to share one permission is a menu that would pass its tests with the
    permission check deleted.
    """
    from app.web.views import admin as workflow_admin
    from app.web.views import feedback_review, integrations, permissions
    from app.web.views import shares_admin, users_admin

    return (
        AdminScreen(
            key="users",
            label="Logins",
            url=users_admin.USERS_URL,
            permission=users_admin.MANAGE,
            blurb=(
                "Every account in this workspace, when each of them last signed "
                "in, and the form that adds one."
            ),
        ),
        AdminScreen(
            key="permissions",
            label="Permissions",
            url=permissions.PERMISSIONS_URL,
            permission=permissions.MANAGE,
            blurb=(
                "What each person may do, grant by grant, and where the "
                "authority came from."
            ),
        ),
        AdminScreen(
            key="workflows",
            label="Approval routes",
            url=workflow_admin.WORKFLOWS_URL,
            permission=workflow_admin.MANAGE,
            blurb=(
                "Who is asked to approve what, and in which order. Drawn here; "
                "read by everybody on the approval route screen."
            ),
        ),
        AdminScreen(
            key="shares",
            label="Share register",
            url=shares_admin.SHARES_URL,
            permission=shares_admin.MANAGE,
            blurb=(
                "Every link handed to somebody outside the company, who made "
                "it, and what it still opens."
            ),
        ),
        AdminScreen(
            key="invites",
            label="Invitations",
            url=shares_admin.INVITES_URL,
            permission=shares_admin.MANAGE,
            blurb=(
                "Colleagues pulled onto a piece of work, and whether they ever "
                "took the link up."
            ),
        ),
        AdminScreen(
            key="sources",
            label="Sources",
            url=integrations.SOURCES_URL,
            permission=integrations.MANAGE,
            blurb=(
                "Where this workspace draws its filings from. Registering a "
                "source fetches nothing."
            ),
        ),
        AdminScreen(
            key="feedback",
            label="Feedback",
            url=feedback_review.FEEDBACK_URL,
            permission=feedback_review.PERMISSION,
            blurb=(
                "What people told the product it got wrong, newest first, with "
                "suspected assertion defects lifted to the top."
            ),
        ),
    )


def admin_menu(request: Request | None = None) -> tuple[AdminScreen, ...]:
    """The administrative screens this viewer may open. Empty means draw nothing.

    Called from base.html on every render, and from app/web/views/admin_index.py
    to build the page the masthead points at. One function, so the link and the
    page behind it cannot disagree about what a person may open -- which is the
    only way to promise that every link on the index opens.

    FOUR THINGS ANSWER THE EMPTY TUPLE and they are deliberately
    indistinguishable: nobody is signed in, the principal carries no company,
    the database cannot be read, and the viewer holds none of the codes. Each of
    them means the same thing to a masthead -- there is nothing this reader may
    be offered -- and a menu that guessed on any of them would draw a link that
    refuses on click. Absence is denial, applied to a menu.

    The database read is the one exception that is caught rather than allowed to
    fail. base.html renders on the no-tables path too: a fresh clone that has not
    been seeded draws every screen's "load the corpus" page, and a masthead that
    raised there would turn a state the product handles into a 500 on every URL.

    An UNKNOWN permission code is NOT caught. policy.has() raises on a code the
    product does not define, and that is a defect in admin_screens() above, not a
    fact about the reader. Swallowing it would hide the menu from everybody for
    ever while looking exactly like a correct refusal.
    """
    if not isinstance(request, Request):
        # A render with no request in context. Starlette always puts one there,
        # so this is not a state the running product reaches -- and answering it
        # with "no menu" rather than an exception keeps a caller that renders a
        # fragment on its own from taking down a page over a link.
        return ()

    cached = getattr(request.state, _CACHE_ATTRIBUTE, None)
    if cached is not None:
        return cached

    menu = _menu_for(request)
    setattr(request.state, _CACHE_ATTRIBUTE, menu)
    return menu


def _menu_for(request: Request) -> tuple[AdminScreen, ...]:
    principal = current_user(request)
    if principal is None or not principal.company_id or not principal.user_id:
        return ()

    screens = admin_screens()

    # Asked once per CODE rather than once per screen. Seven screens share three
    # codes between them today, and policy.has() runs two queries each time it
    # is called -- so the naive spelling would put fourteen queries behind every
    # page in the product to draw one link. dict.fromkeys keeps declaration
    # order, which only matters for reading a query log.
    codes = tuple(dict.fromkeys(screen.permission for screen in screens))

    try:
        with session_scope() as session:
            held = {
                code
                for code in codes
                if policy.has(session, principal.company_id, principal.user_id, code)
            }
    except SQLAlchemyError:
        return ()

    return tuple(screen for screen in screens if screen.permission in held)


def build_templates() -> Jinja2Templates:
    """The templates object every view renders through.

    A function rather than a module-level singleton, so each view keeps its own
    object and a test that reaches into one module's environment cannot change
    what another screen renders. What they share is the REGISTRATION, which is
    the part that was going wrong -- not the instance.
    """
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env.globals["admin_url"] = ADMIN_URL
    templates.env.globals["admin_menu"] = admin_menu
    return templates


__all__ = [
    "ADMIN_URL",
    "MASTHEAD_GLOBALS",
    "AdminScreen",
    "admin_menu",
    "admin_screens",
    "build_templates",
]
