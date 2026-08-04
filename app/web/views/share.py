"""The shared page: /s/<token>. The only route in this product with no session.

READ app/web/deps.py BEFORE THIS FILE. Every other screen sits behind
AuthMiddleware, which refuses an anonymous request before any view runs and
sends it to /login. That guard is installed once, in front of everything, so a
screen written next month is protected by existing rather than by somebody
remembering a decorator. This route deliberately steps outside it, and what
replaces the guard is written down in app/state/sharing.py: an unguessable
token stored only as a digest, one artifact and never a list, an expiry that
cannot be absent, a revocation, a tenant switch, and an audit row for every
creation, open and withdrawal.

WHY THE HOLE IS WORTH IT. The person who has to approve acting on a regulatory
change sits in Legal or Rates or Engineering and has no account here. Today the
analyst pastes a quote into an email and the recipient takes it on faith. This
page sends the claim with its evidence attached and re-checks the evidence in
front of them, which is the difference between a quote and a citation.

THE PAGE RE-VERIFIES AT OPEN TIME. Nothing rendered is stored and served back.
open_share() re-reads the stored source at the cited offsets on every request,
so a source edited after the link went out takes the statement off the page on
the very next open, with no job in between. A recipient watching a claim
withdraw itself is the best demonstration this product has.

IF IT DOES NOT VERIFY, THE STATEMENT IS NOT HERE. The template iterates
`verified` and `withheld` separately and there is no flag to branch on, because
WithheldClaim has no statement field and, being slotted, cannot be given one.
A template mistake on this page cannot leak an assertion; it can only fail to
render. That rule holds on every surface and it matters most on the one nobody
had to sign in to reach.

ONE ARTIFACT, AND NO WAY OUT OF IT. No masthead, no nav, no company name, no
sibling claims, no counts. The page does not extend base.html for exactly that
reason -- base.html renders six screens' worth of links, and a share that let a
reader walk into the product would be an access-control failure wearing a
stylesheet. The only links on the page are the stylesheet and the sign-in call
to action.

THREE HEADERS, EACH DOING WORK.
  Referrer-Policy: no-referrer -- the token is in the path, so a browser
    following the sign-in link would otherwise put a working share link in the
    Referer header of a request to another page.
  Cache-Control: no-store -- an expired or revoked link must not be answered
    from a shared cache after it dies, and a verdict computed at open time must
    not be replayed as though it were computed now.
  X-Robots-Tag: noindex -- with the meta tag in the template. A crawler that
    reached a token would put a claim and its source in a search index.

WHAT THIS FILE DOES NOT DO. It does not mint links and it does not revoke them.
Those are acts by a signed-in person and belong on the authenticated screens,
against app/state/sharing.py. Keeping them out means every route in this module
is a GET, and the surface that steps outside the session guard has nothing on it
that changes anything a person can point at.
"""

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.state.claims import VerifiedClaim, WithheldClaim
from app.state.db import session_scope
from app.state.sharing import (
    SHARE_PATH_PREFIX,
    SHARE_UNAVAILABLE,
    SHARE_UNAVAILABLE_TEXT,
    OpenedShare,
    open_share,
)
from app.web import TEMPLATES_DIR
from app.web.views.changes import SOURCE_UNREADABLE, coordinate, source_window

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

TEMPLATE = "shared_claim.html"

# Sent on both answers. See the module docstring for what each one stops.
SAFETY_HEADERS = {
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
}

# What the page says it is, in the words a recipient needs.
WATERMARK = "Shared view"
WATERMARK_NOTE = (
    "This page is a single shared item. It re-checked the quotation against the "
    "source document when you opened it."
)
NOTHING_TO_SHOW = (
    "There is nothing to show for this item. It could not be read when you "
    "opened this page, so no statement is made about it."
)

CALL_TO_ACTION = "Sign in to Strata"
CALL_TO_ACTION_NOTE = "No account? Ask {sharer} for access."


@dataclass(frozen=True, slots=True)
class ClaimPanel:
    """A verified claim and the source extract that stands behind it.

    Holds the VerifiedClaim itself rather than a copy of its fields, so there is
    one definition of what a claim carries. The window's marked span is the real
    bytes at the cited offsets, read from the stored source during this request
    -- never the quote the claim supplied, which would prove nothing.
    """

    claim: VerifiedClaim
    coordinate: str
    source_label: str
    window: object


@dataclass(frozen=True, slots=True)
class RefusalPanel:
    """A claim the product will not make, and why.

    Carries the WithheldClaim untouched. That type has no statement field and no
    __dict__ to attach one to, and this wrapper adds none: nothing on the way to
    the template can put back what the refusal dropped.
    """

    claim: WithheldClaim
    coordinate: str
    source_reads: str


def _panels(opened: OpenedShare) -> tuple[list[ClaimPanel], list[RefusalPanel]]:
    shown = [
        ClaimPanel(
            claim=claim,
            coordinate=coordinate(
                claim.citation_version_id, claim.citation_start, claim.citation_end
            ),
            source_label=opened.sources[claim.citation_version_id].label,
            window=source_window(
                opened.sources[claim.citation_version_id].text,
                claim.citation_start,
                claim.citation_end,
                version_id=claim.citation_version_id,
                version_label=opened.sources[claim.citation_version_id].label,
            ),
        )
        # No filter here on purpose. open_share refuses the whole page when a
        # verified claim has no source behind it, so a claim reaching this loop
        # always has one -- and a filter would turn that impossible case into a
        # claim quietly missing from a page whose job is to show claims.
        for claim in opened.verified_claims
    ]
    refused = [
        RefusalPanel(
            claim=claim,
            coordinate=coordinate(
                claim.citation_version_id, claim.citation_start, claim.citation_end
            ),
            source_reads=claim.source_excerpt or SOURCE_UNREADABLE,
        )
        for claim in opened.withheld_claims
    ]
    return shown, refused


def _client_ip(request: Request) -> str:
    """The address this request came from, as far as this process can tell.

    A proxy in front of the application makes it the proxy's address, and
    nothing here can tell the difference. No forwarded header is trusted: one
    the client sets is a value the client chose, and an audit row saying an open
    came from wherever the visitor typed is worse than one saying it came from
    the proxy.
    """
    client = request.client
    return client.host if client is not None else ""


def _unavailable(request: Request) -> HTMLResponse:
    """One response for expired, revoked and unknown alike.

    Byte for byte the same in all three cases: no token, no id, no timestamp,
    nothing computed from the request. A caller who could tell a withdrawn link
    from one that never existed would hold a probe for which tokens are real,
    and this page is reachable by anybody.
    """
    return templates.TemplateResponse(
        request,
        TEMPLATE,
        {
            "page_title": "Link not available",
            "available": False,
            "unavailable_heading": SHARE_UNAVAILABLE,
            "unavailable_text": SHARE_UNAVAILABLE_TEXT,
            "call_to_action": CALL_TO_ACTION,
        },
        status_code=404,
        headers=SAFETY_HEADERS,
    )


@router.get(SHARE_PATH_PREFIX + "/{token}", response_class=HTMLResponse)
def shared_item(request: Request, token: str) -> HTMLResponse:
    """One claim or one change, re-verified now, for somebody with no account.

    The write inside a GET is deliberate and is the feature: every open records
    what the recipient was shown, so "what did Legal see on Tuesday" has an
    answer a year later. session_scope commits it.
    """
    with session_scope() as session:
        opened = open_share(session, token=token, ip=_client_ip(request))
        if opened is None:
            return _unavailable(request)

        shown, refused = _panels(opened)
        context = {
            "page_title": WATERMARK,
            "available": True,
            "watermark": WATERMARK,
            "watermark_note": WATERMARK_NOTE,
            "nothing_to_show": NOTHING_TO_SHOW,
            "heading": opened.heading,
            "source_label": opened.source_label,
            "shared_by": opened.shared_by,
            "shared_at": opened.shared_at,
            "expires_at": opened.expires_at,
            "opened_at": opened.opened_at,
            "verified": shown,
            "withheld": refused,
            "call_to_action": CALL_TO_ACTION,
            "call_to_action_note": CALL_TO_ACTION_NOTE.format(
                sharer=opened.shared_by or "the person who sent this"
            ),
        }

    return templates.TemplateResponse(
        request, TEMPLATE, context, headers=SAFETY_HEADERS
    )


__all__ = [
    "CALL_TO_ACTION",
    "ClaimPanel",
    "RefusalPanel",
    "SAFETY_HEADERS",
    "TEMPLATE",
    "WATERMARK",
    "router",
    "shared_item",
]
