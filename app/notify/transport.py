"""Getting a message out, and saying plainly when it did not go.

THE SHAPE IS app/interpretation/propose.py'S, ON PURPOSE. That module put every
network call behind one injected protocol, kept the SDK import inside a factory,
and turned "nothing is configured" into a named fallback with a sentence
attached. The same three decisions apply here for the same three reasons, so
they are made the same way rather than a second way.

THE HONEST LIMIT, FIRST. SmtpTransport HAS NEVER SENT A MAIL FROM THIS
REPOSITORY. No relay has been configured and no credential has been held here,
so every test drives the fake in tests/test_notify.py. What is proven offline is
the part above the socket: the message that gets built, the headers it carries,
the fact that the token reaches no header, and every one of the three answers
below. What is NOT proven is anything only a mail server can answer -- that the
relay accepts this envelope, that STARTTLS is offered, that the credentials
work, how a rejection reads. Treat the class at the foot of this file as
unexercised code and everything above it as tested code, because that is what
they are.

THREE ANSWERS, NEVER TWO. A caller can tell sent from not-configured from
failed, and that distinction is the whole point of this file. An admin who
provisions a login and is told nothing assumes the person got an email; that
person never arrives, the admin waits, and the product looks like it lost
somebody. So a delivery that did not happen comes back with the sentence the
product must show, and the invitation link is still in the caller's hand to
deliver by other means.

NOTHING HERE IS AUDITED, AND THAT IS DELIBERATE. app/state/audit.py owns the
audited vocabulary and holds no ACTION_ constant for "a mail was sent". Inventing
a spelling of one in this file is exactly how the two constants that already
drifted got that way, and app/interpretation/propose.py refuses the same thing
for the same reason. A caller that wants delivery in the chain needs a new
constant in audit.py and a writer beside the invitation write -- a change to
files this module does not own. Whatever that writer records, it must never
record the link: the token is in it, and an append-only log cannot be edited.

WHAT THIS CANNOT DO. It cannot tell you the mail arrived. A relay that accepts
an envelope has promised to try, nothing more, and a bounce comes back hours
later to a mailbox this product does not read. "Sent" here means "a mail server
took it", and the difference matters when an admin asks why somebody never
showed up.
"""

import os
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Mapping, Protocol

from app.notify.messages import Message

# ---------------------------------------------------------------------------
# The three answers
# ---------------------------------------------------------------------------

#: A mail server took it. NOT a promise that anybody received it.
DELIVERY_SENT = "MAIL_SENT"
#: Named fallbacks, in the vocabulary app/interpretation/propose.py uses: each
#: one is a state the product must be able to say out loud.
FALLBACK_NOT_CONFIGURED = "MAIL_PATH_OFF_NOT_CONFIGURED"
FALLBACK_SEND_FAILED = "MAIL_PATH_OFF_SEND_FAILED"

DELIVERY_OUTCOMES = (DELIVERY_SENT, FALLBACK_NOT_CONFIGURED, FALLBACK_SEND_FAILED)

# ---------------------------------------------------------------------------
# Configuration
#
# Read from the environment because there is no settings table -- the same known
# gap app/state/invites.py names for its two switches and app/state/claims.py
# names for the confidence threshold. These are therefore global rather than per
# company, which is right for a relay and would be wrong for a policy.
# ---------------------------------------------------------------------------

MAIL_ENV_HOST = "STRATA_SMTP_HOST"
MAIL_ENV_PORT = "STRATA_SMTP_PORT"
MAIL_ENV_SENDER = "STRATA_MAIL_FROM"
MAIL_ENV_USERNAME = "STRATA_SMTP_USERNAME"
MAIL_ENV_PASSWORD = "STRATA_SMTP_PASSWORD"

#: Without both of these there is no mail path. A host with no sender is not a
#: half-configured mail path, it is no mail path: the alternative is inventing a
#: From address, and an invitation from an address nobody owns is a bounce at
#: best and a phish at worst.
MAIL_ENV_REQUIRED = (MAIL_ENV_HOST, MAIL_ENV_SENDER)

#: Submission. STARTTLS is compulsory on it, see SmtpTransport.
DEFAULT_SMTP_PORT = 587
#: Implicit TLS. The one port where the socket is wrapped before a word is said.
IMPLICIT_TLS_PORT = 465

ANNOUNCEMENT_NOT_CONFIGURED = (
    "No mail was sent: this deployment has no mail transport configured, so "
    "nobody has been told about this invitation. The link is still valid and "
    f"you can pass it on yourself. To send mail from the product, set "
    f"{MAIL_ENV_HOST} and {MAIL_ENV_SENDER} (and {MAIL_ENV_USERNAME} with "
    f"{MAIL_ENV_PASSWORD} if the relay asks for them) and try again."
)
ANNOUNCEMENT_SEND_FAILED = (
    "The mail was NOT sent: the mail server refused it or could not be "
    "reached, so nobody has been told about this invitation. The link is still "
    "valid and you can pass it on yourself. The error was: {error}"
)


class MailTransport(Protocol):
    """One mail out: four strings in, nothing back.

    Deliberately narrow, like the Transport protocol in
    app/interpretation/propose.py. Everything above it -- composing, escaping,
    the expiry sentence, the three answers below -- is pure and runs in CI, and
    everything that touches a socket is on the far side of this one method. That
    is what makes the fake in tests/test_notify.py a fair stand-in rather than a
    mock of something more interesting.

    It returns None and raises on failure rather than returning a status. A
    transport that returned False would tempt a caller to ignore it; an
    exception is caught in exactly one place, deliver(), which turns it into the
    announced fallback.
    """

    def send(self, *, to: str, subject: str, text: str, html: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Delivery:
    """What happened to one message, and the sentence to show for it.

    IT CARRIES NO BODY. The body holds the acceptance link and the link holds the
    raw token, so a Delivery in a log line, a traceback or a template context
    must not be able to print it. Only the address and the subject are here, and
    neither ever contains the token -- tests/test_notify.py asserts that of the
    subject at composition time and of this object here.

    `announcement` is set on every outcome, not only the failures. A screen that
    has to branch to find out whether there is anything to say will eventually
    forget on one path.
    """

    outcome: str
    announcement: str
    to: str
    subject: str
    error: str | None = None

    @property
    def sent(self) -> bool:
        return self.outcome == DELIVERY_SENT

    @property
    def configured(self) -> bool:
        """False only when there is no mail path at all. A failure is not this."""
        return self.outcome != FALLBACK_NOT_CONFIGURED


def deliver(message: Message, transport: MailTransport | None) -> Delivery:
    """Hand a message to a transport, or say why nobody got it.

    THE TRANSPORT IS INJECTED AND MAY BE None, and None is a first-class answer
    rather than an error. transport_from_environment() returns None on a
    deployment with no relay -- which is every reviewer's laptop -- and the
    product must stay usable there while being honest that the mail did not go.

    The except is broad on purpose, as it is in judge_materiality(). The transport
    is injected, so its failures are not a closed set this module can name: a
    refused connection, a rejected recipient, an expired certificate, a relay
    that took the connection and dropped it. Every one of them means the same
    thing to the person reading the screen -- nobody was told -- and the
    alternative is a stack trace where a sentence belongs. The error text is
    carried, not swallowed.
    """
    if transport is None:
        return Delivery(
            outcome=FALLBACK_NOT_CONFIGURED,
            announcement=ANNOUNCEMENT_NOT_CONFIGURED,
            to=message.to,
            subject=message.subject,
        )

    try:
        transport.send(
            to=message.to,
            subject=message.subject,
            text=message.text,
            html=message.html,
        )
    except Exception as error:  # noqa: BLE001 -- see the docstring
        return Delivery(
            outcome=FALLBACK_SEND_FAILED,
            announcement=ANNOUNCEMENT_SEND_FAILED.format(error=error),
            to=message.to,
            subject=message.subject,
            error=str(error),
        )

    return Delivery(
        outcome=DELIVERY_SENT,
        announcement=(
            f"The invitation was sent to {message.to}. A mail server took it, "
            "which is not the same as it arriving: if they do not see it within "
            "a few minutes, send them the link another way rather than "
            "provisioning a second login."
        ),
        to=message.to,
        subject=message.subject,
    )


# ---------------------------------------------------------------------------
# Building the mail
# ---------------------------------------------------------------------------


def email_message(message: Message, *, sender: str) -> EmailMessage:
    """A multipart/alternative mail: plain text first, html second.

    A module-level function rather than a method, so the one part of the SMTP
    path that can be exercised without a socket is exercised. The order is the
    standard's and is load-bearing: a client shows the LAST part it can render,
    so html second means a graphical client gets the html and a text client gets
    the text, and putting them the other way round quietly gives everybody the
    plain text.

    NO HEADER CARRIES THE TOKEN. Subject, To and From are composed from an
    address, a company name and a product name; the link exists only in the two
    bodies. Headers travel further than bodies do -- through relay logs, bounce
    messages, mailing list archives and whatever a mail gateway keeps -- so a
    test asserts the absence rather than the docstring claiming it.

    THE ENCODING IS LEFT TO THE STANDARD LIBRARY, which picks quoted-printable
    because the link is longer than a text line may be. The raw source then
    carries a soft break inside the link and every client rejoins it on decode;
    a test decodes both parts and finds the link whole. Forcing 8bit to keep the
    source pretty would need the relay to advertise 8BITMIME, which is a
    property of somebody else's mail server and not one to bet a credential
    link on.
    """
    built = EmailMessage()
    built["From"] = sender
    built["To"] = message.to
    built["Subject"] = message.subject
    built.set_content(message.text)
    built.add_alternative(message.html, subtype="html")
    return built


# ---------------------------------------------------------------------------
# The environment
# ---------------------------------------------------------------------------


def missing_settings(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Which variables a mail path needs and this environment does not have.

    Empty means configured. A screen showing a deployment's health can print
    this; deliver() does not need it, because the announcement names the whole
    set rather than the missing part -- one sentence to keep true instead of a
    sentence per combination.

    A username with no password counts as missing, and so does a password with
    no username. Half a credential is not a permissive default to fall back
    from: it is somebody who edited one line of a config and stopped.
    """
    values = os.environ if env is None else env
    absent = [
        name for name in MAIL_ENV_REQUIRED if not str(values.get(name, "")).strip()
    ]
    username = str(values.get(MAIL_ENV_USERNAME, "")).strip()
    password = str(values.get(MAIL_ENV_PASSWORD, "")).strip()
    if username and not password:
        absent.append(MAIL_ENV_PASSWORD)
    if password and not username:
        absent.append(MAIL_ENV_USERNAME)
    return tuple(absent)


def transport_from_environment(
    env: Mapping[str, str] | None = None,
) -> MailTransport | None:
    """A real transport when a relay is configured, None when it is not.

    None IS NOT AN ERROR AND IS NOT SILENT. It is the input deliver() turns into
    the announced fallback, which is what a reviewer running `make run` with no
    relay sees: the invitation is written, the link is on screen, and the
    product says nobody was emailed.

    AN UNREADABLE PORT RAISES rather than falling back to 587. The same rule
    app/state/invites.py applies to its switches: a value nobody can read is not
    a value nobody set, and quietly substituting the default would send mail to
    a port the operator did not choose. Missing is a decision nobody made;
    unreadable is a decision somebody got wrong, and only one of those is safe
    to answer for them.

    No connection is opened here. The client is built at send time inside
    SmtpTransport, so constructing this costs nothing and reaches nothing --
    which is what lets a view ask "is there a mail path" while rendering a page.
    """
    values = os.environ if env is None else env
    # The cheapest true statement first. With no host and no sender there is no
    # mail path, and a stray port variable on a deployment that never meant to
    # send mail is not worth a raised page. The port is only a decision once
    # somebody has decided to send.
    if missing_settings(values):
        return None

    raw_port = str(values.get(MAIL_ENV_PORT, "")).strip()
    port = DEFAULT_SMTP_PORT
    if raw_port:
        unreadable = ValueError(
            f"{MAIL_ENV_PORT}={raw_port!r} is not a port number. Refusing to "
            f"fall back to {DEFAULT_SMTP_PORT}: a mail path on a port nobody "
            "chose is worse than no mail path, which announces itself."
        )
        try:
            port = int(raw_port)
        except ValueError:
            raise unreadable from None
        # A number is not yet a port. 0 and 70000 both parse and neither can be
        # connected to, and the failure would arrive an hour later as a socket
        # error on the one mail somebody was waiting for.
        if not 1 <= port <= 65535:
            raise unreadable

    return SmtpTransport(
        host=str(values[MAIL_ENV_HOST]).strip(),
        port=port,
        sender=str(values[MAIL_ENV_SENDER]).strip(),
        username=str(values.get(MAIL_ENV_USERNAME, "")).strip(),
        password=str(values.get(MAIL_ENV_PASSWORD, "")).strip(),
    )


class SmtpTransport:
    """One SMTP conversation. UNEXERCISED: see the module docstring.

    THE CONNECTION IS ENCRYPTED, AUTHENTICATED, OR THERE IS NO CONNECTION. On
    465 the socket is wrapped before a word is said; on every other port the
    session starts in the clear and STARTTLS is demanded immediately. A server
    that will not offer it gets an exception, which deliver() turns into the
    announced failure -- it does NOT get the mail in plaintext. Both calls are
    given ssl.create_default_context(), so the certificate chain and the
    hostname are checked and a relay that cannot prove who it is is refused the
    same way. That second half was missing until 2026-08-04 and the sentence
    said "encrypted" alone, which was true and was not the claim anybody read it
    as: unauthenticated TLS hands the whole session to whoever is on the path.
    The refusal is the point: the body holds a link that sets a password on an
    account whose role is already granted, and it is the single most valuable
    string this product ever puts on a wire.

    THE CLIENT IS BUILT PER SEND AND CLOSED AFTER. No pooling, no long-lived
    connection. This path sends one mail when an admin presses a button, so a
    held connection would be idle machinery that fails in its own way -- a relay
    that timed the socket out while nobody was looking -- for no gain anybody
    can measure.

    smtplib IS IMPORTED INSIDE THE METHOD, following the SDK import in
    app/interpretation/propose.py. Importing app.notify then costs nothing and
    opens nothing, and tests/test_notify.py measures that in a fresh process
    rather than trusting this sentence.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = DEFAULT_SMTP_PORT,
        sender: str,
        username: str = "",
        password: str = "",
        timeout: int = 30,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password
        # A send that hangs holds the request that asked for it. Half a minute
        # is long for a relay and short for a person watching a page.
        self._timeout = timeout

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        import smtplib  # noqa: PLC0415 -- see the class docstring
        import ssl  # noqa: PLC0415 -- imported here for the same reason

        built = email_message(
            Message(to=to, subject=subject, text=text, html=html),
            sender=self._sender,
        )

        # ENCRYPTED IS NOT ENOUGH; IT HAS TO BE THE RIGHT SERVER. Passing no
        # context made Python fall back to ssl._create_stdlib_context(), which
        # is verify_mode=CERT_NONE and check_hostname=False -- so the session
        # was encrypted to whoever answered, and anything on the path could
        # present any certificate, terminate the TLS and read both the
        # credential link and this account's relay password. The docstring above
        # already called that body the most valuable string this product puts on
        # a wire; until 2026-08-04 the code did not act like it. This context
        # verifies the chain and the hostname, so a relay that cannot prove who
        # it is gets an exception and deliver() announces the failure -- the
        # same refusal the class already makes for a server that will not
        # offer STARTTLS at all.
        #
        # THERE IS NO SETTING THAT TURNS THIS OFF, AND THE OPERATOR WHO WOULD
        # WANT ONE DOES NOT NEED IT. The case is a private relay holding a
        # certificate from a company's own authority, and the answer is already
        # in the standard library: create_default_context loads the trust store
        # and honours SSL_CERT_FILE and SSL_CERT_DIR, so pointing either at the
        # company's CA trusts that relay and keeps the chain and the hostname
        # checked. A STRATA_ variable that skipped verification instead would be
        # this defect restored with a flag on it, and principle 26 says what a
        # switch like that is: the deployment that set it once, in a hurry, has
        # no way to notice it is still set. If one is ever added it announces
        # itself in the log on every send, and that sentence is a poor second to
        # not having it. tests/test_notify.py asserts the absence.
        context = ssl.create_default_context()

        if self._port == IMPLICIT_TLS_PORT:
            client = smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=context
            )
        else:
            client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        try:
            if self._port != IMPLICIT_TLS_PORT:
                # Raises SMTPNotSupportedError when the server does not offer
                # it. Left to raise: see the class docstring.
                client.starttls(context=context)
                client.ehlo()
            if self._username:
                client.login(self._username, self._password)
            client.send_message(built)
        finally:
            # quit() can itself raise on a connection that already died, and a
            # failure to say goodbye must not replace the real error above it.
            try:
                client.quit()
            except Exception:  # noqa: BLE001 -- see above
                client.close()


__all__ = [
    "ANNOUNCEMENT_NOT_CONFIGURED",
    "ANNOUNCEMENT_SEND_FAILED",
    "DEFAULT_SMTP_PORT",
    "DELIVERY_OUTCOMES",
    "DELIVERY_SENT",
    "FALLBACK_NOT_CONFIGURED",
    "FALLBACK_SEND_FAILED",
    "IMPLICIT_TLS_PORT",
    "MAIL_ENV_HOST",
    "MAIL_ENV_PASSWORD",
    "MAIL_ENV_PORT",
    "MAIL_ENV_REQUIRED",
    "MAIL_ENV_SENDER",
    "MAIL_ENV_USERNAME",
    "Delivery",
    "MailTransport",
    "SmtpTransport",
    "deliver",
    "email_message",
    "missing_settings",
    "transport_from_environment",
]
