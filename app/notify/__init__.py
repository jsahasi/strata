"""Telling somebody outside the product that something happened to them.

Today that is one thing: the invitation an admin provisions in
app/state/invites.py, and the replacement link a resend mints. Nothing else here
sends anything, and nothing here writes to the database.

TWO FILES, AND THE SPLIT IS THE DESIGN.

  messages.py composes. It is pure text: no network, no session, no clock of its
  own. Every rule about what an invitation may and may not say is enforced there
  and tested there, which is only possible because composing a mail and sending
  one are different acts.
  transport.py delivers, behind an injected protocol, and turns the two ways
  that fails into named fallbacks with a sentence attached.

THE PRODUCT COULD NOT SEND MAIL AT ALL BEFORE THIS PACKAGE, and it still cannot
on a machine with no relay configured. That is the state a reviewer will be in,
so it is the state this package is honest about first: transport_from_environment
returns None, deliver() answers FALLBACK_NOT_CONFIGURED, and the announcement
tells the admin the link exists and nobody was emailed. An admin who provisions a
login and is told nothing assumes the person got mail; that person never
arrives, and the product has lost somebody in a way nothing on screen explains.

HOW A CALLER USES IT, IN FOUR LINES.

    outcome = provision_login(session, company_id=..., actor=admin.id, ...)
    if outcome.provisioned:
        message = invitation_message(
            to_email=outcome.invitation.email,
            company_name=<the tenant's display name>,
            invited_by=<the admin's display_name>,
            accept_url=accept_link(<this deployment's address>,
                                   <the acceptance route>, outcome.token),
            expires_at=outcome.invitation.expires_at,
        )
        delivery = deliver(message, transport_from_environment())

`delivery.sent` is the only thing that may be rendered as "we emailed them".
`delivery.announcement` is what to show otherwise, and it is worth showing on
success too: a relay accepting an envelope is not the same as anybody receiving
it.

THE TOKEN IS THE POINT OF ALL THE CARE HERE. outcome.token exists for exactly
one moment -- app/state/invites.py stores only its sha256 -- and this package is
where it becomes a link in somebody's inbox. It goes in the link and nowhere
else: not in the subject, not in a header, not in a repr, not in a log line, and
not in the audit chain, which cannot be edited afterwards.
"""

from app.notify.messages import (
    LINK_SCHEMES,
    MONTHS,
    PRODUCT_NAME,
    SUBJECT_INVITATION,
    SUBJECT_RESEND,
    Message,
    accept_link,
    invitation_message,
    resent_invitation_message,
)
from app.notify.transport import (
    ANNOUNCEMENT_NOT_CONFIGURED,
    ANNOUNCEMENT_SEND_FAILED,
    DEFAULT_SMTP_PORT,
    DELIVERY_OUTCOMES,
    DELIVERY_SENT,
    FALLBACK_NOT_CONFIGURED,
    FALLBACK_SEND_FAILED,
    IMPLICIT_TLS_PORT,
    MAIL_ENV_HOST,
    MAIL_ENV_PASSWORD,
    MAIL_ENV_PORT,
    MAIL_ENV_REQUIRED,
    MAIL_ENV_SENDER,
    MAIL_ENV_USERNAME,
    Delivery,
    MailTransport,
    SmtpTransport,
    deliver,
    email_message,
    missing_settings,
    transport_from_environment,
)

__all__ = [
    "ANNOUNCEMENT_NOT_CONFIGURED",
    "ANNOUNCEMENT_SEND_FAILED",
    "DEFAULT_SMTP_PORT",
    "DELIVERY_OUTCOMES",
    "DELIVERY_SENT",
    "FALLBACK_NOT_CONFIGURED",
    "FALLBACK_SEND_FAILED",
    "IMPLICIT_TLS_PORT",
    "LINK_SCHEMES",
    "MAIL_ENV_HOST",
    "MAIL_ENV_PASSWORD",
    "MAIL_ENV_PORT",
    "MAIL_ENV_REQUIRED",
    "MAIL_ENV_SENDER",
    "MAIL_ENV_USERNAME",
    "MONTHS",
    "PRODUCT_NAME",
    "SUBJECT_INVITATION",
    "SUBJECT_RESEND",
    "Delivery",
    "MailTransport",
    "Message",
    "SmtpTransport",
    "accept_link",
    "deliver",
    "email_message",
    "invitation_message",
    "missing_settings",
    "resent_invitation_message",
    "transport_from_environment",
]
