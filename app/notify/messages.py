"""The invitation mail itself. Written on the assumption it reaches the wrong person.

WHY THAT ASSUMPTION. The address is typed by a human into a form, and
app/state/invites.py refuses to invent one precisely because a guessed address
bounced on this project. An address that does not bounce and is simply somebody
else's is the case nothing can catch, and this is the only file that decides
what such a stranger reads. So the mail says the least that still lets the right
person act: who invited them, which company, what to do, the link, and when it
stops working.

WHAT IS DELIBERATELY NOT IN IT, AND WHY EACH ONE IS OUT.

  The recipient's name. On a mistyped address it names a colleague to a
  stranger, and it buys the right recipient nothing -- they know who they are.
  There is no parameter for it, so no caller can add one back.
  No project, no docket, no proceeding, no claim text. The mail is about an
  account, not about work, and the work is what a competitor would want.
  No other people. The inviter is the single exception, and it is a deliberate
  cost: a stranger learns that Ada Ng works at that company. Without it the
  right recipient cannot tell a real invitation from a phish, which is the
  larger risk.
  No role, and no count of anything -- not accounts, not items, not invitations
  sent. "You are one of 40 users" and "you will be an approver" are both facts
  about the company's shape.

THE SIGNATURE IS THE CONTROL. The two composers take five arguments between
them and nothing else, so leaking more is not a matter of remembering not to:
there is nowhere to put it. tests/test_notify.py asserts the parameter set.

THE EXPIRY IS STATED WITH THE ACTUAL TIME. A credential link that fails
silently at hour 25 costs a support ticket and a first impression, so the mail
names the moment in UTC rather than saying "24 hours" and leaving the reader to
work out when it was sent. The month name comes from a tuple below rather than
strftime, so the sentence does not change with the server's locale.

THE HTML IS BARE. No image, no tracking pixel, no external font, no stylesheet,
no script -- nothing loaded from another host at all. A remote image in an
invitation tells whoever hosts it when the mail was opened and from where, and
the mail also has to render for somebody whose client blocks all of it. Every
value interpolated into it is escaped, because a company display name is typed
by an admin and passes through here unread.

THE PLAIN TEXT IS NOT AN AFTERTHOUGHT. Both parts are built for every message.
A plaintext-only mail wrapped badly on a phone in this project's own history,
and an html-only mail is unreadable in a client that shows source.

NOTHING HERE SENDS ANYTHING. This module composes; app/notify/transport.py
delivers. A Message is inert, and the split is what lets every rule above be
tested with no credentials and no network.
"""

import html as html_escaping
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote

# The one address rule in this codebase, imported rather than restated. The mail
# has to go to the string the account is keyed by; a second normaliser here
# would be free to disagree with the unique index in app/state/models.py.
from app.state.identity import normalise_email

# The product's name, as a stranger may read it. The one thing a misaddressed
# invitation is allowed to teach them beyond "somebody was invited".
PRODUCT_NAME = "Strata"

# Only http and https. A mail client will happily render javascript: and data:
# as a link, and the composer must not be the thing that carries one to an
# inbox. Absence is denial: a link nothing here can vouch for is refused.
LINK_SCHEMES = ("https://", "http://")

# Month names as data, so the sentence reads the same on every machine.
# strftime("%B") follows the process locale, and a mail that says "5 août 2026"
# because a server was configured in French is a defect nobody sees until a
# recipient reports it.
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

SUBJECT_INVITATION = "Set up your {product} login for {company}"
SUBJECT_RESEND = "A new link to set up your {product} login for {company}"


@dataclass(frozen=True, slots=True, repr=False)
class Message:
    """One mail, both parts, ready for a transport. It carries no secret twice.

    THE REPR IS REDACTED ON PURPOSE. The body holds the acceptance link, and the
    link holds the raw token -- the only moment that token exists outside the
    recipient's inbox. A dataclass repr would put it in every traceback, every
    log line and every debugger frame that touched a Message, and a secret
    written into a log cannot be taken out again. So repr names the message and
    withholds it, and tests/test_notify.py asserts that.

    There is no sender field. Which address the product sends from is one
    decision, and it belongs to the transport that holds the credentials, not to
    every caller composing a mail.
    """

    to: str
    subject: str
    text: str
    html: str

    def __repr__(self) -> str:
        return (
            f"Message(to={self.to!r}, subject={self.subject!r}, "
            f"text=<{len(self.text)} characters withheld>, "
            f"html=<{len(self.html)} characters withheld>)"
        )


# ---------------------------------------------------------------------------
# Guards. Every one of them refuses rather than repairs.
# ---------------------------------------------------------------------------


def _required(value: str, field: str) -> str:
    """Present, a string, and carrying no control character. Otherwise refused.

    THE CONTROL CHARACTER CHECK IS A HEADER INJECTION CHECK. company_name reaches
    the Subject line, and a newline in a subject is where a second header goes --
    a Bcc to somebody else, on a mail that carries a credential link. Python's
    email package refuses such a header too, and that refusal comes at send time,
    turning a poisoned value into an unrelated-looking delivery failure after the
    message has already been composed and shown on a screen. Refusing here means
    the Message never exists.
    """
    if not value or not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field} is required; a mail that cannot say {field} is not one this "
            "product will send"
        )
    cleaned = value.strip()
    if any(character < " " or character == "\x7f" for character in cleaned):
        raise ValueError(
            f"{field} carries a control character. Refused rather than stripped: "
            "a newline in this value is how a second header reaches a mail that "
            "holds a credential link, and repairing it would hide who tried."
        )
    return cleaned


def _checked_link(url: str) -> str:
    """An absolute http(s) link, or nothing.

    A path with no host is refused rather than joined to a guess at where this
    deployment lives. Nothing in this package knows the product's public
    address -- there is no companies table and no site setting -- so inventing
    one would be the fallback that does not announce itself, on the one link
    that sets a password. accept_link() is where a caller that does know builds
    it.
    """
    cleaned = _required(url, "accept_url")
    if not cleaned.lower().startswith(LINK_SCHEMES):
        # THE REFUSAL SAYS NOTHING BACK ABOUT WHAT IT WAS GIVEN, and that is the
        # fix rather than the style. This read `cleaned.split(':')[0]` to name
        # the offending scheme -- which is the whole string when there is no
        # colon, and a relative acceptance path is exactly the mistake this
        # guard exists to catch. So `/invite/accept/<token>` put the live token
        # into a ValueError, and from there into a traceback and a log. The
        # package rule is that the token goes in the link and nowhere else: not
        # in the subject, not in a header, not in a repr, not in a log line. A
        # secret written into a log cannot be taken out again, so the caller is
        # told what was required and never what they sent.
        raise ValueError(
            f"accept_url must begin with one of {', '.join(LINK_SCHEMES)}. A "
            "relative path would be joined to a guess at where this deployment "
            "lives, and a mail client renders any scheme it is given. The value "
            "is not repeated here because it may carry the invitation token."
        )
    if any(character.isspace() for character in cleaned):
        raise ValueError("accept_url cannot contain whitespace")
    return cleaned


def _expiry_words(moment: datetime) -> str:
    """The expiry as a person reads it: 5 August 2026 at 09:14 UTC.

    Always UTC, and it always says UTC. The recipient is in some other office,
    and a time with no zone on it is a time they will get wrong by an hour on
    the one link that expires in a day.

    A naive timestamp raises, matching every other write path in this codebase:
    the reader is in some other timezone and a moment with no zone on it is a
    number nobody can act on.
    """
    if not isinstance(moment, datetime):
        raise ValueError("expires_at must be a datetime")
    if moment.tzinfo is None:
        raise ValueError(
            "expires_at must carry a timezone; a naive timestamp is a defect, "
            "and this one is printed for somebody in another country"
        )
    utc = moment.astimezone(timezone.utc)
    return f"{utc.day} {MONTHS[utc.month - 1]} {utc.year} at {utc:%H:%M} UTC"


def accept_link(base_url: str, path: str, token: str) -> str:
    """Join a deployment's address, the acceptance route and a token. One spelling.

    THE PATH HAS NO DEFAULT, and that is not an oversight. The route that
    accepts an invitation belongs to the web layer, not here, and a default
    written in this file would be this module's guess at somebody else's URL --
    right until the route moved, at which point every invitation would carry a
    dead link and nothing would say so.

    The token is quoted with nothing safe, so a token containing a slash cannot
    walk out of the path segment it was put in.

    The result goes through the same check the composer applies, so a base_url
    that is not an absolute http(s) address fails here, where the caller can see
    which argument they got wrong, rather than three lines later inside a mail
    nobody can build.
    """
    site = _required(base_url, "base_url").rstrip("/")
    route = _required(path, "path").strip("/")
    secret = _required(token, "token")
    return _checked_link(f"{site}/{route}/{quote(secret, safe='')}")


# ---------------------------------------------------------------------------
# The two mails
# ---------------------------------------------------------------------------

# TWO MAILS, THREE STRINGS APART. The lead sentence and the line above the link
# differ; the link, the expiry sentence and the warning are shared, because they
# describe the same act. Two copies of the expiry sentence is how one of them
# ends up still saying seven days after the clock was shortened to a day.
@dataclass(frozen=True, slots=True)
class _Variant:
    subject: str
    lead: str
    call: str


_INVITATION = _Variant(
    subject=SUBJECT_INVITATION,
    lead="{inviter} invited you to {product} for {company}.",
    call="To start, set a password:",
)
_RESEND = _Variant(
    subject=SUBJECT_RESEND,
    lead=(
        "{inviter} has sent you a new {product} link for {company}. "
        "The earlier link has stopped working."
    ),
    call="Set a password here:",
)

_EXPIRY = (
    "The link works until {expiry}. After that it stops working and you need "
    "a fresh one: ask {inviter} to send another."
)
_WARNING = (
    "If you were not expecting this, do not follow the link. Whoever holds it "
    "can set the password on a new account, so tell the sender it reached the "
    "wrong person."
)

# Plain text wraps here. Narrow enough to survive a quoted reply and a phone,
# which is the failure that put the html alternative in this file in the first
# place. THE LINK IS NEVER PUT THROUGH THIS: a wrapped url is a broken url, and
# a broken credential link reads to the recipient as a broken product.
TEXT_WIDTH = 72

# Bare on purpose: no image, no font, no stylesheet, no script, nothing from
# another host. The styles are inline and few -- a mail client strips a <style>
# block often enough that anything load-bearing has to be on the element.
_HTML = """\
<html>
<body style="margin:0;padding:24px;background:#ffffff;color:#111111;\
font-family:system-ui,-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;\
font-size:16px;line-height:1.5;">
<p style="margin:0 0 16px;">{lead}</p>
<p style="margin:0 0 16px;">{call}</p>
<p style="margin:0 0 16px;"><a href="{url}" style="color:#0b5fff;">\
Set your password</a></p>
<p style="margin:0 0 16px;font-size:14px;word-break:break-all;">{url}</p>
<p style="margin:0 0 16px;">{expiry}</p>
<p style="margin:0;font-size:14px;color:#555555;">{warning}</p>
</body>
</html>
"""


def _wrapped(paragraph: str) -> str:
    """One paragraph, wrapped for a text client. Long words stay whole."""
    return textwrap.fill(
        paragraph,
        width=TEXT_WIDTH,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _compose(
    variant: _Variant,
    *,
    to_email: str,
    company_name: str,
    invited_by: str,
    accept_url: str,
    expires_at: datetime,
) -> Message:
    """Both mails, once. The two composers differ by the variant they pass.

    Every refusal happens before a single character is composed, so a caller
    that gave a bad address never holds a half-built mail with a live link in
    it.
    """
    address = normalise_email(to_email)
    company = _required(company_name, "company_name")
    inviter = _required(invited_by, "invited_by")
    url = _checked_link(accept_url)
    expiry = _expiry_words(expires_at)

    lead = variant.lead.format(
        inviter=inviter, product=PRODUCT_NAME, company=company
    )
    expiry_line = _EXPIRY.format(expiry=expiry, inviter=inviter)
    text = "\n\n".join(
        (
            _wrapped(lead),
            _wrapped(variant.call),
            url,
            _wrapped(expiry_line),
            _wrapped(_WARNING),
        )
    ) + "\n"

    # THE VALUES ARE ESCAPED, NOT THE COMPOSED STRING. A company display name is
    # typed by an admin, reaches here unread, and is about to be rendered as
    # markup in somebody else's mail client. Escaping the result instead would
    # double-encode it -- "Ada &amp; Sons" would arrive as "Ada &amp;amp; Sons"
    # -- and the templates themselves hold no markup to protect.
    escape = html_escaping.escape
    html = _HTML.format(
        lead=variant.lead.format(
            inviter=escape(inviter), product=PRODUCT_NAME, company=escape(company)
        ),
        call=escape(variant.call),
        url=escape(url, quote=True),
        expiry=_EXPIRY.format(expiry=escape(expiry), inviter=escape(inviter)),
        warning=escape(_WARNING),
    )

    return Message(
        to=address,
        subject=variant.subject.format(product=PRODUCT_NAME, company=company),
        text=text,
        html=html,
    )


def invitation_message(
    *,
    to_email: str,
    company_name: str,
    invited_by: str,
    accept_url: str,
    expires_at: datetime,
) -> Message:
    """The first link somebody gets. Compose it, then hand it to a transport.

    expires_at is the invitation's own expiry -- Invitation.expires_at from the
    row app/state/invites.py just wrote, which is INVITE_PROVISION_TTL_HOURS
    after it was minted. It is passed rather than computed here so the mail
    cannot say a different time from the one the product will enforce.

    accept_url is the whole link, built by whoever owns the acceptance route.
    See accept_link().
    """
    return _compose(
        _INVITATION,
        to_email=to_email,
        company_name=company_name,
        invited_by=invited_by,
        accept_url=accept_url,
        expires_at=expires_at,
    )


def resent_invitation_message(
    *,
    to_email: str,
    company_name: str,
    invited_by: str,
    accept_url: str,
    expires_at: datetime,
) -> Message:
    """The replacement link, which says the last one is dead.

    IT SAYS SO BECAUSE IT IS TRUE. resend() in app/state/invites.py mints a new
    token and moves every earlier invitation to superseded, so the previous link
    stopped working the instant this one was written -- not at its own expiry. A
    recipient holding both mails will otherwise try the first one, and a
    credential link that fails with the one sentence a dead invitation gets is
    exactly the moment somebody decides this product is broken.

    invited_by is whoever pressed resend, which may not be whoever provisioned
    the login. The row records the same thing.
    """
    return _compose(
        _RESEND,
        to_email=to_email,
        company_name=company_name,
        invited_by=invited_by,
        accept_url=accept_url,
        expires_at=expires_at,
    )


__all__ = [
    "LINK_SCHEMES",
    "MONTHS",
    "PRODUCT_NAME",
    "SUBJECT_INVITATION",
    "SUBJECT_RESEND",
    "TEXT_WIDTH",
    "Message",
    "accept_link",
    "invitation_message",
    "resent_invitation_message",
]
