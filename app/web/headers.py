"""Security headers, on every response, from one place.

WHAT WAS MEASURED. `curl -sD - https://strata.sudama.ai/login` came back with
alt-svc, content-type, date, server and content-length, and nothing else. No
frame refusal. No nosniff. No HSTS. Four admin views set Referrer-Policy by
hand -- admin_index.py, users_admin.py, permissions.py, share.py -- and the rest
of the product sent nothing at all.

THE ATTACK THIS IS BUILT AGAINST, so the choices below are answerable. This
product's whole claim is that a NAMED PERSON approved something, and it keeps a
hash-chained log to say so. Put /escalations in an invisible frame under a page
somebody wants to click, line the approve button up under the thing they came
for, and a real approval arrives from a real person who meant to click something
else. The audit chain then records, quite faithfully and quite unfalsifiably,
that they approved it. A trustworthy log of a click nobody meant to make is
worse than no log, because it is evidence. Clickjacking is the threat that
picked these headers; everything else here is cheap and went in beside it.

WHY MIDDLEWARE AND NOT A DECORATOR. Fifty-odd routes, and the four that already
set a header set only one of the five. A route added next month is protected by
existing rather than by somebody remembering -- the same argument app/web/deps.py
makes for the session guard, arrived at by the same route: a rule applied per
call site is a rule with holes in it, and nobody knows which ones.

WHY RAW ASGI AND NOT BaseHTTPMiddleware. This wraps `send`, so it stamps the
response start message whatever produced it -- a template, a redirect from the
session guard, a StaticFiles hit, a FileResponse. BaseHTTPMiddleware would buffer
and re-emit, and it does not see the mounted static app the same way.

WHERE IT IS INSTALLED, AND WHY THE ORDER MATTERS. app/main.py calls
install_security_headers(app) AFTER install_auth(app). Starlette inserts each
added middleware at the front of the stack, so the last one added is the
OUTERMOST -- which puts this outside the session guard. That is load-bearing: an
anonymous request to a guarded path is answered by the guard's 303 and never
reaches a view, and a redirect a browser follows is a response a browser can be
made to frame. Install it the other way round and every anonymous response goes
out bare. tests/test_security_headers.py asserts the order.

WHAT IT DOES NOT COVER. An unhandled exception is turned into a 500 by
Starlette's ServerErrorMiddleware, which sits outside everything here, so that
one response goes out unstamped. Left alone rather than papered over: moving
this outside the error handler would mean owning the error path too, and a 500
page carries no approval button.
"""

from starlette.requests import Request

from app.web.deps import LOOPBACK_HOSTS, is_loopback_plaintext

#: Enforced. Deliberately narrow: four directives whose cost was measured
#: against the templates rather than assumed.
#:
#: frame-ancestors 'none' -- the modern spelling of the frame refusal, and the
#:   only one that covers a nested frame. X-Frame-Options below is the old one.
#: base-uri 'self' -- no template carries a <base> tag, and an injected one
#:   would silently repoint every relative URL on the page, including the form
#:   that posts an approval.
#: object-src 'none' -- no template carries <object> or <embed>.
#: form-action 'self' -- every form in app/web/templates posts to a relative
#:   path. Checked, not assumed.
#:
#: NOT script-src and NOT style-src. See CSP_REPORT_ONLY.
CSP = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self'"

#: Reported, not enforced. This is the policy the product should end up serving,
#: sent in the header that makes a browser log a violation and load the page
#: anyway.
#:
#: WHAT A SWEEP OF app/web/templates FOUND, at the time this was written, so the
#: next person tightens it knowing rather than guessing:
#:
#:   inline event handlers (onclick, onsubmit, on*):  0
#:   inline <script> blocks that execute:             0
#:   <script type="application/json"> data blocks:    3
#:       workflow_edit.html x2, _tour.html x1. Not executed by the browser and
#:       so not a script-src violation, but they are what a reader mistakes for
#:       inline script when they grep.
#:   <script src="...">, all same-origin under /static: 4
#:       change.html, workflow_edit.html, _tour.html, _clerk.html
#:   inline <style> blocks:                           8
#:       invite_accept, shared_claim, shares_admin, integrations, change,
#:       invites_admin, users_admin, permissions
#:   style="..." attributes:                          2
#:       review_centre.html, project_detail.html -- both set a CSS custom
#:       property from a computed number, which is why they are attributes
#:   external hosts referenced by any template or asset: 0
#:
#: So script-src 'self' would pass today and style-src 'self' would break eight
#: screens. Both are report-only regardless, because the count above is a
#: measurement of one afternoon and a policy that silently breaks the product
#: hours before it is shown is worse than no policy. Enforcing script-src is the
#: cheap next step; style-src wants those eight blocks moved into strata.css
#: first, and the two attributes are a nonce or a small named class.
#:
#: There is no report-uri. Violations land in the browser console, which is
#: where the person tightening this will be looking. Adding a collector is a
#: decision about running a service, not about this file.
CSP_REPORT_ONLY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "form-action 'self'"
)

#: One year, and subdomains with it. Sent only where it can be honoured -- see
#: hsts_applies. No `preload`: that hands the domain to a list shipped inside
#: browsers, removal takes months, and it is not a decision to make as a side
#: effect of adding a header.
HSTS_HEADER = "Strict-Transport-Security"
HSTS = "max-age=31536000; includeSubDomains"

#: Sent on every response, whatever the status and whatever produced it.
#:
#: X-Frame-Options DENY beside frame-ancestors on purpose. They are the old and
#: the new spelling of one refusal, they are both honoured, and a browser that
#: reads only one of them is still a browser somebody approves from. DENY rather
#: than SAMEORIGIN because nothing in this product frames itself.
#:
#: X-Content-Type-Options nosniff -- the product serves uploaded-looking bytes
#: back on the source-window path and a browser that sniffs one of them into
#: HTML runs it on this origin.
#:
#: Referrer-Policy no-referrer -- the same value the four admin views already
#: chose, generalised. Paths here carry share tokens, invitation tokens and
#: escalation ids, and a Referer header hands them to whatever a person clicks
#: through to.
ALWAYS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP,
    "Content-Security-Policy-Report-Only": CSP_REPORT_ONLY,
}


def hsts_applies(request: Request) -> bool:
    """Whether this response may claim the origin is https-only. Two refusals.

    PLAIN HTTP. A browser is required to ignore HSTS off a plaintext response,
    so sending it there is a claim the transport cannot carry. deps.py already
    owns the question of what "plain http to this machine" means --
    is_loopback_plaintext -- and this reuses it rather than writing a second
    answer that can drift from the cookie's.

    LOOPBACK, EITHER SCHEME. `make run` serves http://127.0.0.1:8000, and a
    developer who has put a local certificate in front of their own machine gets
    the https spelling instead. Pin either and the browser sends every future
    request to localhost:8000 over https for a year -- including the next
    project served on that port, which then looks broken for reasons nothing on
    screen explains. deps.py stops at the http half because a Secure cookie on
    loopback https is harmless; a pin is not, so this goes one step further and
    says why here rather than changing the shared helper underneath the cookie.

    Behind nginx the scheme is the forwarded one: deploy/entrypoint.sh runs
    uvicorn with --proxy-headers, so a TLS request terminated at the proxy
    arrives here as https and is answered as https.
    """
    if is_loopback_plaintext(request):
        return False
    if (request.url.hostname or "").lower() in LOOPBACK_HOSTS:
        return False
    return request.url.scheme == "https"


def headers_for(scope) -> dict[str, str]:
    """The headers this response should carry. ALWAYS, plus HSTS where it holds."""
    headers = dict(ALWAYS)
    if hsts_applies(Request(scope)):
        headers[HSTS_HEADER] = HSTS
    return headers


def header_pairs(
    existing: list[tuple[bytes, bytes]], extra: dict[str, str]
) -> list[tuple[bytes, bytes]]:
    """The raw ASGI header list with the missing ones filled in. Set, never overwrite.

    A view that set a header chose that value, and four of them already do --
    app/web/views/share.py sends Referrer-Policy beside Cache-Control and
    X-Robots-Tag for reasons its own docstring argues. Appending instead of
    filling in would put two Referrer-Policy lines on that response, which is
    the shape of a middleware nobody has read the output of.
    """
    present = {name.lower() for name, _ in existing}
    return list(existing) + [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in extra.items()
        if name.lower().encode("latin-1") not in present
    ]


class SecurityHeadersMiddleware:
    """Stamp every http response on its way out. See the module docstring."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # lifespan and websocket. Neither has response headers, and
            # building a Request out of either raises.
            await self.app(scope, receive, send)
            return

        extra = headers_for(scope)

        async def send_stamped(message):
            if message["type"] == "http.response.start":
                message["headers"] = header_pairs(message.get("headers") or [], extra)
            await send(message)

        await self.app(scope, receive, send_stamped)


def install_security_headers(app) -> None:
    """Put the headers in front of an application. Called by app/main.py.

    A function rather than a line in main.py for the reason install_auth is one:
    the test files that assemble their own application install exactly what the
    product installs, and a guard the product has that the test does not is a
    guard nobody has tested.

    CALL THIS AFTER install_auth. Starlette inserts at the front, so whatever is
    added last is outermost, and this has to be outside the session guard for
    the guard's own 303 to be stamped.
    """
    app.add_middleware(SecurityHeadersMiddleware)


__all__ = [
    "ALWAYS",
    "CSP",
    "CSP_REPORT_ONLY",
    "HSTS",
    "HSTS_HEADER",
    "SecurityHeadersMiddleware",
    "header_pairs",
    "headers_for",
    "hsts_applies",
    "install_security_headers",
]
