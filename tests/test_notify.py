"""The mail path: what it says, what it refuses to say, and when it is off.

EVERY TEST HERE RUNS WITH NO CREDENTIALS AND NO NETWORK, and the first test
measures that rather than trusting it. The transport is an injected dependency
behind a small protocol, exactly as app/interpretation/propose.py does it, so
the fake below stands in for a mail server and smtplib is never imported.

The tests are grouped by the thing they defend:

  offline      importing the package opens nothing, and nothing is configured
               by accident.
  announcing   a caller can tell sent from not configured from failed. An admin
               who is told nothing assumes the person got an email, and that
               person never arrives.
  the mail     who invited them, which company, what to do, the link, and when
               it expires -- and nothing else, because an address a human typed
               sometimes reaches the wrong human.
  the token    it appears in the link and nowhere else. Not the subject, not a
               repr, not a log line.
"""

import inspect
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.notify import (
    ANNOUNCEMENT_NOT_CONFIGURED,
    ANNOUNCEMENT_SEND_FAILED,
    DELIVERY_SENT,
    FALLBACK_NOT_CONFIGURED,
    FALLBACK_SEND_FAILED,
    MAIL_ENV_HOST,
    MAIL_ENV_PORT,
    MAIL_ENV_SENDER,
    MAIL_ENV_PASSWORD,
    MAIL_ENV_USERNAME,
    Delivery,
    Message,
    SmtpTransport,
    accept_link,
    deliver,
    email_message,
    invitation_message,
    missing_settings,
    resent_invitation_message,
    transport_from_environment,
)

# A token shaped like the one app/state/invites.py mints: url-safe, 32 bytes.
TOKEN = "Zt7Qw-3sK1Lm9pXv_0aB4cD8eF6gH2iJ5kL7mN9oP1Q"
BASE = "https://strata.mep.example"
LINK = f"{BASE}/invite/accept/{TOKEN}"
COMPANY = "Meridian Energy Partners"
INVITER = "Ada Ng"
EXPIRES = datetime(2026, 8, 5, 9, 14, tzinfo=timezone.utc)


class RecordingTransport:
    """A mail server that keeps what it was handed. No network, ever."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})


class ExplodingTransport:
    def send(self, *, to: str, subject: str, text: str, html: str) -> None:
        raise OSError("connection refused by mail relay")


def a_message(**overrides) -> Message:
    fields = {
        "to_email": "priya.nandakumar@mep.example",
        "company_name": COMPANY,
        "invited_by": INVITER,
        "accept_url": LINK,
        "expires_at": EXPIRES,
    }
    fields.update(overrides)
    return invitation_message(**fields)


# ----------------------------------------------------------------- offline --


def test_importing_notify_does_not_import_smtplib(repo_root: Path):
    """Measured in a fresh process, like tests/test_propose.py does for the SDK.

    The whole suite rests on this path costing nothing at import. A top-level
    `import smtplib` would put a mail client one import away from every module
    that renders a page.
    """
    probe = "import sys, app.notify; print('\\n'.join(sorted(sys.modules)))"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = result.stdout.split()
    assert "app.notify" in loaded
    assert "smtplib" not in loaded


def test_nothing_configured_is_no_transport():
    assert transport_from_environment({}) is None
    assert transport_from_environment({MAIL_ENV_HOST: "  "}) is None


def test_half_configured_is_no_transport_and_names_what_is_missing():
    """A host with no sender is not a mail path. Never guess a From address."""
    env = {MAIL_ENV_HOST: "smtp.mep.example"}
    assert transport_from_environment(env) is None
    assert missing_settings(env) == (MAIL_ENV_SENDER,)

    # A username with no password is the same defect from the other side.
    paired = {
        MAIL_ENV_HOST: "smtp.mep.example",
        MAIL_ENV_SENDER: "strata@mep.example",
        MAIL_ENV_USERNAME: "strata",
    }
    assert transport_from_environment(paired) is None
    assert missing_settings(paired) == (MAIL_ENV_PASSWORD,)


def test_a_full_configuration_builds_a_transport_and_still_opens_nothing():
    env = {
        MAIL_ENV_HOST: "smtp.mep.example",
        MAIL_ENV_SENDER: "strata@mep.example",
    }
    assert missing_settings(env) == ()
    transport = transport_from_environment(env)
    assert isinstance(transport, SmtpTransport)
    assert "smtplib" not in sys.modules


@pytest.mark.parametrize("port", ["five eight seven", "0", "70000", "-1"])
def test_an_unreadable_port_is_refused_rather_than_defaulted(port: str):
    env = {
        MAIL_ENV_HOST: "smtp.mep.example",
        MAIL_ENV_SENDER: "strata@mep.example",
        MAIL_ENV_PORT: port,
    }
    with pytest.raises(ValueError) as raised:
        transport_from_environment(env)
    assert MAIL_ENV_PORT in str(raised.value)


def test_a_stray_port_on_a_deployment_that_sends_no_mail_is_not_an_error():
    """No host and no sender is no mail path, whatever else is lying around."""
    assert transport_from_environment({MAIL_ENV_PORT: "five eight seven"}) is None


# --------------------------------------------------------------- announcing --


def test_with_no_transport_the_product_says_so():
    """The failure this test exists for: an admin told nothing assumes it sent."""
    delivery = deliver(a_message(), None)

    assert delivery.sent is False
    assert delivery.outcome == FALLBACK_NOT_CONFIGURED
    assert delivery.announcement == ANNOUNCEMENT_NOT_CONFIGURED
    # It has to open by denying the send. An announcement that leads with the
    # configuration and mentions the failure later is one an admin skims past.
    assert delivery.announcement.startswith("No mail was sent")
    assert delivery.configured is False
    # It has to name the fix, or the admin reads it as bad news with no action.
    assert MAIL_ENV_HOST in delivery.announcement
    assert MAIL_ENV_SENDER in delivery.announcement


def test_a_failure_is_not_the_same_answer_as_not_configured():
    delivery = deliver(a_message(), ExplodingTransport())

    assert delivery.sent is False
    assert delivery.outcome == FALLBACK_SEND_FAILED
    assert delivery.outcome != FALLBACK_NOT_CONFIGURED
    assert "connection refused by mail relay" in delivery.error
    assert "connection refused by mail relay" in delivery.announcement
    assert delivery.announcement != ANNOUNCEMENT_NOT_CONFIGURED
    assert ANNOUNCEMENT_SEND_FAILED.split("{")[0] in delivery.announcement


def test_the_three_outcomes_are_three_different_words():
    assert len({DELIVERY_SENT, FALLBACK_NOT_CONFIGURED, FALLBACK_SEND_FAILED}) == 3


def test_a_sent_message_reaches_the_transport_once_and_says_it_was_sent():
    transport = RecordingTransport()
    message = a_message()
    delivery = deliver(message, transport)

    assert delivery.sent is True
    assert delivery.outcome == DELIVERY_SENT
    assert delivery.error is None
    assert delivery.to == message.to
    assert len(transport.sent) == 1
    assert transport.sent[0]["to"] == message.to
    assert transport.sent[0]["subject"] == message.subject
    assert transport.sent[0]["text"] == message.text
    assert transport.sent[0]["html"] == message.html


def test_a_delivery_never_carries_the_body():
    """So a screen or a log line holding one cannot print the link."""
    delivery = deliver(a_message(), RecordingTransport())
    printed = repr(delivery)
    assert TOKEN not in printed
    assert not hasattr(delivery, "text")
    assert not hasattr(delivery, "html")


# ------------------------------------------------------------------ the mail --


def test_the_invitation_says_who_which_company_what_to_do_and_when_it_dies():
    message = a_message()

    for body in (message.text, message.html):
        assert INVITER in body
        assert COMPANY in body
        assert LINK in body
        assert "password" in body.lower()
        # The actual time, not "24 hours". A link that fails silently at hour
        # 25 is a support ticket and a distrustful first impression.
        assert "5 August 2026" in body
        assert "09:14" in body
        assert "UTC" in body

    assert COMPANY in message.subject
    assert message.to == "priya.nandakumar@mep.example"


def test_both_a_plain_text_and_an_html_alternative_are_built():
    message = a_message()
    assert message.text.strip()
    assert message.html.strip()
    assert "<" not in message.text
    assert "<a href=" in message.html


def test_the_invitation_cannot_be_given_anything_that_would_leak():
    """The signature is the control. There is no parameter for a docket.

    A misaddressed invitation must teach a stranger nothing beyond that a
    company uses Strata and somebody was invited. The way to guarantee that is
    to leave the composer no way to be handed the rest: no project, no docket,
    no claim, no other people, no counts -- and not the recipient's own name,
    which on a mistyped address names a colleague to a stranger.
    """
    allowed = {"to_email", "company_name", "invited_by", "accept_url", "expires_at"}
    for composer in (invitation_message, resent_invitation_message):
        taken = set(inspect.signature(composer).parameters)
        assert taken == allowed, f"{composer.__name__} takes {taken - allowed}"


def test_the_mail_names_no_project_no_docket_and_no_count():
    message = a_message()
    body = f"{message.subject}\n{message.text}\n{message.html}".lower()
    for leak in ("docket", "proceeding", "obligation", "escalation", "claim"):
        assert leak not in body
    assert not re.search(r"\b\d+\s+(items?|claims?|people|users?)\b", body)


def test_the_html_loads_nothing_from_another_host():
    message = a_message()
    html = message.html

    for forbidden in ("<img", "<script", "<link", "<iframe", "@import", "url("):
        assert forbidden not in html.lower()
    # The acceptance link is the only address in the document.
    assert set(re.findall(r"https?://[^\s\"'<>]+", html)) == {LINK}


def test_a_company_name_with_markup_in_it_is_escaped():
    message = a_message(company_name='Ada & Sons <ops@x>')
    assert "<ops@x>" not in message.html
    assert "&lt;ops@x&gt;" in message.html
    assert "&amp;" in message.html
    # The plain text keeps what the admin typed.
    assert "Ada & Sons <ops@x>" in message.text


def test_the_resend_says_the_earlier_link_has_stopped_working():
    first = a_message()
    again = resent_invitation_message(
        to_email="priya.nandakumar@mep.example",
        company_name=COMPANY,
        invited_by=INVITER,
        accept_url=LINK,
        expires_at=EXPIRES,
    )

    assert again.subject != first.subject
    for body in (again.text, again.html):
        assert "stopped working" in body
        assert LINK in body
        assert "5 August 2026" in body
    # Still the same rules about the rest.
    assert set(re.findall(r"https?://[^\s\"'<>]+", again.html)) == {LINK}


def test_the_expiry_must_carry_a_timezone():
    with pytest.raises(ValueError):
        a_message(expires_at=datetime(2026, 8, 5, 9, 14))


def test_an_address_nothing_can_read_is_refused_rather_than_repaired():
    with pytest.raises(ValueError):
        a_message(to_email="priya at mep.example")
    with pytest.raises(ValueError):
        a_message(to_email="")


def test_a_missing_link_or_a_bare_path_is_refused():
    """Absence is denial. The composer never invents where the link points."""
    for bad in ("", "   ", "/invite/accept/" + TOKEN, "javascript:alert(1)"):
        with pytest.raises(ValueError):
            a_message(accept_url=bad)


def test_an_empty_company_or_inviter_is_refused():
    with pytest.raises(ValueError):
        a_message(company_name="  ")
    with pytest.raises(ValueError):
        a_message(invited_by="")


def test_a_newline_in_a_name_cannot_reach_a_header():
    """A company name goes into the Subject. A subject with a newline is a Bcc.

    Refused at composition rather than at send: the email package refuses the
    header too, but that refusal arrives as a delivery failure long after the
    message was built and shown, and it names the wrong problem.
    """
    for poisoned in (
        "Meridian\nBcc: attacker@evil.example",
        "Meridian\r\nBcc: attacker@evil.example",
    ):
        with pytest.raises(ValueError) as raised:
            a_message(company_name=poisoned)
        assert "control character" in str(raised.value)
        with pytest.raises(ValueError):
            a_message(invited_by=poisoned)


def test_accept_link_joins_one_way_and_quotes_the_token():
    assert accept_link(BASE, "/invite/accept", TOKEN) == LINK
    assert accept_link(BASE + "/", "invite/accept", TOKEN) == LINK
    assert accept_link(BASE, "/invite/accept", "a b/c") == (
        f"{BASE}/invite/accept/a%20b%2Fc"
    )
    with pytest.raises(ValueError):
        accept_link("", "/invite/accept", TOKEN)
    with pytest.raises(ValueError):
        accept_link(BASE, "/invite/accept", "")
    # A host with no scheme is caught here, where the caller can see which
    # argument was wrong, rather than inside the composer three lines later.
    with pytest.raises(ValueError):
        accept_link("strata.mep.example", "/invite/accept", TOKEN)


# ----------------------------------------------------------------- the token --


def test_the_token_appears_only_inside_the_link():
    message = a_message()

    assert TOKEN not in message.subject
    assert TOKEN not in message.to
    for body in (message.text, message.html):
        assert TOKEN in body
        assert TOKEN not in body.replace(LINK, "")


def test_a_message_does_not_print_its_body():
    """repr lands in tracebacks and log lines. The link must not travel there."""
    message = a_message()
    printed = repr(message)

    assert TOKEN not in printed
    assert LINK not in printed
    # It still identifies itself, or the redaction has cost a reader the row.
    assert message.to in printed
    assert "Message(" in printed


def test_the_built_email_carries_both_parts_and_no_token_in_a_header():
    """The one part of SmtpTransport that can be exercised offline."""
    message = a_message()
    built = email_message(message, sender="Strata <strata@mep.example>")

    assert built["To"] == message.to
    assert built["Subject"] == message.subject
    assert built["From"] == "Strata <strata@mep.example>"
    assert built.get_content_type() == "multipart/alternative"

    parts = [part.get_content_type() for part in built.iter_parts()]
    assert parts == ["text/plain", "text/html"]

    for name, value in built.items():
        assert TOKEN not in value, f"the token reached the {name} header"


def test_the_link_survives_being_encoded_for_the_wire():
    """The plain text is long enough to be quoted-printable. It must decode back.

    The url is 84 characters, so the text part is encoded and the raw source
    carries a soft break inside the link. Every client rejoins it on decode --
    and this test is what says so, because a link that arrives broken is a
    credential link nobody can use and a first impression nobody recovers.
    """
    built = email_message(a_message(), sender="strata@mep.example")
    text_part, html_part = list(built.iter_parts())

    assert LINK in text_part.get_content()
    assert LINK in html_part.get_content()


def test_the_transport_protocol_stays_narrow():
    """Everything the network touches is behind this one method."""
    signature = inspect.signature(SmtpTransport.send)
    assert list(signature.parameters) == ["self", "to", "subject", "text", "html"]


def test_a_refused_link_never_repeats_what_it_was_given():
    """The refusal must not carry the token out of the link and into a log.

    THE BUG THIS GUARDS. The message interpolated `cleaned.split(':')[0]` to
    name the scheme it was handed -- which is the WHOLE STRING when there is no
    colon, and a relative acceptance path is exactly the mistake the guard
    exists to catch. So `/invite/accept/<token>` put the live token in a
    ValueError, and a ValueError reaches a traceback, a log line and an error
    page. The package rule is that the token appears in the link and nowhere
    else, and a secret written into a log cannot be taken out again.
    """
    relative = "/invite/accept/" + TOKEN
    for bad in (relative, "invite/accept/" + TOKEN, "javascript:alert('" + TOKEN + "')"):
        with pytest.raises(ValueError) as raised:
            a_message(accept_url=bad)
        said = str(raised.value)
        assert TOKEN not in said, "the refusal carried the token"
        assert bad not in said
        # It still has to be actionable, or the redaction has cost the caller
        # the one thing they needed: which argument was wrong and what it wants.
        assert "accept_url" in said
        for scheme in ("https://", "http://"):
            assert scheme in said

    # The same guard reached through the other door. accept_link builds the
    # string and hands it here, so a caller with a base_url that is really a
    # bare hostname must not have their token read back to them either.
    with pytest.raises(ValueError) as raised:
        accept_link("strata.mep.example", "/invite/accept", TOKEN)
    assert TOKEN not in str(raised.value)


# ------------------------------------------------------------------- the wire --
#
# The socket half of SmtpTransport, exercised as far as it can be without a mail
# server: the client is built here, and what it is built WITH is the whole of
# this section. See the module docstring in app/notify/transport.py -- the class
# is unexercised code, and these tests do not change that. They pin the two
# arguments that decide whether anybody on the path can read the invitation.


class _FakeSmtpClient:
    """Stands in for smtplib.SMTP and smtplib.SMTP_SSL. It reaches nothing.

    It records the context it was opened with, and the one starttls() was given,
    because those two are what the tests below are about. Every other method is
    a no-op that keeps send() walking to the end.
    """

    opened: list[dict] = []

    def __init__(self, host, port, timeout=None, context=None, **rest):
        self.context = context
        _FakeSmtpClient.opened.append(
            {"host": host, "port": port, "kind": "implicit", "context": context}
        )

    def starttls(self, *, context=None, **rest):
        self.context = context
        _FakeSmtpClient.opened.append(
            {"host": None, "port": None, "kind": "starttls", "context": context}
        )

    def ehlo(self, *args):
        return None

    def login(self, username, password):
        return None

    def send_message(self, built):
        return None

    def quit(self):
        return None

    def close(self):
        return None


@pytest.fixture
def opened(monkeypatch):
    """Every client SmtpTransport builds, and how. No socket is opened."""
    import smtplib

    _FakeSmtpClient.opened = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtpClient)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSmtpClient)
    return _FakeSmtpClient.opened


@pytest.mark.parametrize("port,kind", [(465, "implicit"), (587, "starttls"), (25, "starttls")])
def test_the_relay_has_to_prove_who_it_is_before_the_token_goes_out(opened, port, kind):
    """Encrypted was never the claim. It has to be encrypted TO THE RIGHT SERVER.

    THE BUG THIS GUARDS. SMTP_SSL and starttls were called with no ssl context,
    so Python fell back to ssl._create_stdlib_context(): verify_mode=CERT_NONE
    and check_hostname=False. Anybody between this host and the relay could
    present any certificate, terminate the TLS, and read the invitation body --
    which the module's own docstring calls the single most valuable string this
    product ever puts on a wire -- along with the relay password. The session was
    encrypted to whoever answered, which is not the same fact at all.

    Nothing has leaked, because nothing has ever sent from this repository. That
    is luck rather than a control, and it is why this test exists before the
    first relay is configured rather than after.
    """
    import ssl

    SmtpTransport(host="relay.mep.example", port=port, sender="s@mep.example").send(
        to="priya.nandakumar@mep.example", subject="hello", text="t", html="<p>h</p>"
    )

    protected = [row for row in opened if row["kind"] == kind]
    assert protected, f"nothing was opened with {kind}"
    for row in protected:
        context = row["context"]
        assert context is not None, "no ssl context: Python falls back to CERT_NONE"
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED, "the certificate is not checked"
        assert context.check_hostname is True, "any certificate would do"


def test_no_connection_is_ever_opened_without_a_verifying_context(opened):
    """The class, not the line: whichever door, the context comes with it.

    Two calls take a context and both were missing one. A test that pinned only
    the port it was written for would let the other regress, and the next port
    somebody adds would arrive with no test at all -- so this asserts over every
    client the transport built, whatever built it.
    """
    import ssl

    for port in (25, 465, 587, 2525):
        SmtpTransport(host="relay.mep.example", port=port, sender="s@mep.example").send(
            to="priya.nandakumar@mep.example", subject="hello", text="t", html="<p>h</p>"
        )

    # The plain SMTP open on a STARTTLS port carries no context and cannot: the
    # socket is in the clear until starttls() wraps it. That row is the one
    # exception and it is named here rather than passed over, because a test
    # that quietly skipped a row would be the hole it is meant to close.
    unprotected = [
        row
        for row in opened
        if row["context"] is None and row["kind"] == "implicit" and row["port"] != 465
    ]
    assert len(unprotected) == 3, "the clear-text opens are the three STARTTLS ports"

    verifying = [row for row in opened if row["context"] is not None]
    assert len(verifying) == 4, "one verified handshake per send, and no more"
    for row in verifying:
        assert row["context"].verify_mode == ssl.CERT_REQUIRED
        assert row["context"].check_hostname is True


def test_there_is_no_switch_that_turns_the_checking_off(opened):
    """No environment variable may weaken the handshake, and none does.

    An operator on a private relay does not need one: ssl.create_default_context
    reads SSL_CERT_FILE and SSL_CERT_DIR, so a private certificate authority is
    trusted by pointing the standard variables at it, and the chain is still
    verified. A named STRATA_ variable that skipped verification would be the
    fallback principle 26 refuses -- and it would be the defect this test was
    written for, restored with a flag on it.
    """
    import ssl

    import app.notify.transport as transport

    switches = [
        name
        for name in dir(transport)
        if name.startswith("MAIL_ENV_") or "TLS" in name or "VERIFY" in name
    ]
    assert "MAIL_ENV_TLS_VERIFY" not in switches
    assert not [name for name in switches if "VERIFY" in name.upper()]

    SmtpTransport(host="relay.mep.example", port=465, sender="s@mep.example").send(
        to="priya.nandakumar@mep.example", subject="hello", text="t", html="<p>h</p>"
    )
    context = opened[0]["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
