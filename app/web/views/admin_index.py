"""One screen: where the administrative screens are, for the person who may use
them.

WHAT WAS BROKEN. Six screens were mounted, answered 200 to an administrator who
typed the URL, and appeared on no screen anybody could reach -- /users,
/permissions, /admin/shares, /admin/invites, /admin/sources and /admin/feedback.
The masthead carried six analyst screens and there was nowhere for an
administrative one to live. Every screen test on all six passed the whole time,
because a screen test mounts the router and asks for the path. A feature nobody
can navigate to is a feature nobody has.

WHY AN INDEX RATHER THAN SIX MORE ITEMS IN THE MASTHEAD, and this was the
decision worth arguing about.

  The masthead is a flat row of six. Twelve is not a menu, it is a wall of
  words, and the six an analyst uses all day would be sitting next to six they
  will never open. There is no dropdown anywhere in this product -- no menu
  script, no popover CSS, no keyboard handling for one -- so the second option
  was to build all of that, in a 48-hour build, for a nav.

  The screens do not share one permission and must not look as though they do.
  /admin/workflows is workflow.manage; the rest are user.manage today, and
  app/web/views/feedback_review.py says in its own docstring that its gate is a
  stand-in for a feedback.triage that does not exist yet. Six masthead items
  would each need their own permission read on every page of the product. One
  item needs one question -- may this person open ANY of them -- and this page
  answers the rest of it once, where there is room to say which code opens what.

  Room to grow. The next administrative screen is one entry in the registry in
  app/web/templating.py. It is not a redesign of the masthead, and nobody has to
  remember to link it from somewhere.

WHAT WAS GIVEN UP. One more click. An administrator reaching /users goes through
/admin the first time instead of straight there. Browsers remember URLs and this
product is not one somebody uses for ten seconds at a time, so the cost lands
once and the alternative cost lands on every page an analyst opens.

THE PAGE IS NOT GATED BY A PERMISSION AND THAT IS DELIBERATE. There is no code
that means "administrator" -- app/state/models.py refuses to add one, at length,
and the roles are a grid rather than a ladder. So this screen lists what the
viewer may open and refuses when that is nothing. A viewer holding
workflow.manage and nothing else sees one row, which is the honest answer.

NOTHING HERE IS AUDITED, and that is a decision rather than an omission.
app/auth/policy.py::has() writes nothing by design -- auditing every hidden
button would fill the chain with rows nobody decided, and a chain of noise is a
chain nobody reads. This page holds no data about any person and grants nothing.
The refusal worth recording is the one the SCREEN writes when somebody types its
URL, and each of the six already writes it through policy.require().
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import SQLAlchemyError

from app.state.db import session_scope
from app.web.templating import ADMIN_URL, admin_menu, build_templates
from app.web.views.proceedings import (
    chrome,
    current_company_id,
    unresolved_escalations,
)

router = APIRouter()
templates = build_templates()

TEMPLATE = "admin_index.html"

PAGE_TITLE = "Admin"

# The masthead marks this item current by this key. base.html reads it.
NAV_KEY = "admin"

# What a person who holds none of these codes is told. It names no screen and no
# permission on purpose: workflow_list.html makes the same argument about the
# count of routes it will not print, and it holds harder here. Listing the
# screens would tell somebody who may not open any of them exactly what the
# workspace can do and who to social-engineer for it, and there is nothing they
# could act on in return.
REFUSED = (
    "None of the administrative screens are yours to open. Each of them sits "
    "behind a permission, and you hold none of those. Ask an administrator -- "
    "this page lists what you can reach the moment a grant lands, without you "
    "signing out and in again."
)

# The list is who may do what in this workspace, at one remove. It does not
# belong in a shared cache, and the Referer of any link clicked from here should
# not carry the path either. Same three lines the screens below it send.
SCREEN_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}


def _waiting(company_id: str) -> int:
    """The nav's held-for-review count. Zero on a database with no tables.

    Read on the refusal path too: a masthead that drops its count on one screen
    tells the reader the queue emptied, and it did not. Every other admin screen
    in this codebase does the same thing for the same reason.
    """
    try:
        with session_scope() as session:
            return len(unresolved_escalations(session, company_id))
    except SQLAlchemyError:
        return 0


@router.get(ADMIN_URL, response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """The administrative screens this person may open, and nothing else.

    ONE FUNCTION DECIDES WHAT IS DRAWN HERE AND WHAT IS DRAWN IN THE MASTHEAD.
    admin_menu() is the same call base.html makes, and calling it twice in one
    request costs one read because it caches on request.state. That is not an
    optimisation, it is the guarantee: the link and the page behind it cannot
    disagree about what a person may open, so every link on this page opens.

    403 rather than 404 for somebody who holds nothing. The screen exists and
    they were refused it; a 404 would say the product has no such page, which is
    false, and would leave them looking for a URL they had already found.
    """
    entries = admin_menu(request)
    company_id = current_company_id()

    context = chrome(
        company_id,
        page_title=PAGE_TITLE,
        nav_active=NAV_KEY,
        review_count=_waiting(company_id),
    )
    context.update({"entries": entries, "refused": REFUSED})

    return templates.TemplateResponse(
        request,
        TEMPLATE,
        context,
        status_code=200 if entries else 403,
        headers=SCREEN_HEADERS,
    )


__all__ = ["ADMIN_URL", "NAV_KEY", "REFUSED", "router"]
