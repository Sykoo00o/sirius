"""Lifecycle emails: trial receipt, cancellation confirmation, pre-billing notice.

Checklist slot 5 makes the receipt a *disclosure record*: amount charged, renewal amount,
renewal date, a one-click cancel link, and a reply address somebody reads. That is why these
builders are pure functions returning an `Email` rather than templates reaching for a mail
library: the disclosure content is testable by asserting on a string, and the sentence an
email quotes is `claims.price_line`, the same one the checkout screen renders, so the receipt
cannot word the offer differently from the page the customer agreed on.

Transport is chosen at the boundary, not inside the builders:

  * `FUNNEL_SMTP_URL` set   -> real delivery over SMTP. The URL is parsed by hand (stdlib
    only, no `yagmail`, no `aiosmtplib`) because the funnel runs on the engine's venv, which
    has neither, and pulling a dependency in to save eleven lines is how a standalone unit
    stops being standalone.
  * unset                   -> write an `.eml` file to `FUNNEL_EMAIL_OUTBOX` and log. Local
    rehearsal, and the file is the actual RFC-822 message a real send would have produced, so
    what was reviewed is what would have been delivered.

Mail never blocks a customer-facing action. `send_all` reports per-message failure instead of
raising: a cancellation that succeeded but could not email *still succeeded*, and a 500 on the
cancel button because an SMTP host timed out would be a worse compliance problem than the
missing email. The dashboard repeats everything the email says, which is what makes a lost
email recoverable rather than a disclosure gap.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from urllib.parse import unquote, urlparse

from config import FunnelSettings
from funnel_copy import Copy

log = logging.getLogger("funnel.mail")


@dataclass(frozen=True)
class Email:
    """One message, in the shape both transports need.

    `text` only, no HTML part. A receipt whose disclosure lives in HTML can be unreadable in
    a plain-text client, and checklist slot 5 does not care how the amount is *styled*, only
    that the email carries it.
    """

    to: str
    subject: str
    text: str
    cc_reply_to: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def to_message(self, *, sender: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = sender
        message["To"] = self.to
        message["Subject"] = self.subject
        # Outbox filenames sort by this header, and the stdlib only fills it in at
        # *send* time, which an outbox write never reaches.
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid()
        if self.cc_reply_to:
            message["Reply-To"] = self.cc_reply_to
        for key, value in self.headers.items():
            message[key] = value
        message.set_content(self.text)
        return message


@dataclass(frozen=True)
class SendResult:
    """What happened to one message. `transport` says where it went, which is the first
    thing an operator asks when a customer says "I never got an email"."""

    to: str
    subject: str
    ok: bool
    transport: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def _footer(settings: FunnelSettings, cancel_url: str = "") -> str:
    # The fallback is substituted as a whole URL, never appended to: `{url}/account/cancel`
    # with a caller that already passed a full cancel URL produced
    # `http://host/account/cancel/account/cancel`, and a broken cancel link in the receipt is
    # precisely the checklist-5 failure this email exists to prevent.
    lines = [
        "",
        f"{settings.brand_name}",
        f"Questions? Reply to this email: {settings.support_email or 'the address in our footer'}",
        f"Cancel any time in one click: "
        f"{cancel_url or f'{settings.public_url}/account/cancel'}",
        f"Terms: {settings.public_url}/policy/terms",
        f"Refund policy: {settings.public_url}/policy/refund",
    ]
    return "\n".join(lines)


def trial_receipt_email(copy: Copy, *, email: str, cancel_url: str,
                        card_note: str = "") -> Email:
    """The paid-trial receipt. This is the disclosure record, so every checklist-5 item is
    on its own line rather than woven into prose a reader skims past.

    `card_note` is the brand and last four ("visa ending 4242"). It earns its place because
    an unrecognised line item is, per the corpus, most of what small-B disputes actually
    are: naming the card is cheaper than arbitrating the dispute.
    """
    subject = (f"Your {copy.settings.brand_name} trial: {copy.trial_amount} today, "
               f"renews {copy.renewal_date}")
    paragraphs = [
        "Hi there,",
        "Your trial is active. Here is exactly what you agreed to:",
        f"  {copy.price_line}",
        "\n".join([
            f"Charged today:      {copy.trial_amount}",
            f"Trial runs until:   {copy.renewal_date}",
            f"Renews on:          {copy.renewal_date} at {copy.rebill_per_month}",
            f"Cancel any time:    {cancel_url}",
        ]),
        copy.renewal_warning(),
    ]
    if card_note:
        paragraphs.append(f"Paid with: {card_note}.")
    paragraphs.append(
        "You can cancel in one click from your account page. No phone call, no email hoops, "
        "no cancellation fee. Cancelling keeps the days you already paid for.")
    paragraphs.append(
        f"Your plan is on your account page, and it is exportable whenever you want it. If a "
        f"charge from us on your statement does not look familiar, reply here before "
        f"escalating it: a refund inside {copy.refund_days} days is quicker for you than a "
        f"dispute is for either of us.")
    body = "\n\n".join(paragraphs)
    return Email(to=email, subject=subject, text=body + _footer(copy.settings, cancel_url),
                 cc_reply_to=copy.settings.support_email,
                 headers={"X-Funnel-Email": "trial-receipt"})


def cancellation_email(copy: Copy, *, email: str, access_until: str,
                       reactivated: bool = False, plan_note: str = "") -> Email:
    """Cancellation confirmation. Subject leads with the outcome, because a customer who
    clicked cancel and got "thanks for staying!" as the subject line files a dispute on the
    next charge without reading the body."""
    if reactivated:
        subject = f"Kept active: your {copy.settings.brand_name} subscription continues"
    else:
        subject = f"Cancelled: your {copy.settings.brand_name} subscription"
    lines = ["Hi there,", ""]
    if reactivated:
        lines += [
            "You cancelled, and then chose to keep going, so billing continues.",
            f"{copy.next_bill_heading()}",
            "",
        ]
    else:
        lines += [
            "Your subscription is cancelled. Nothing further will be charged.",
            "",
            f"Access continues until:  {access_until}",
            f"Final charge date:       already paid, nothing more before {access_until}",
            f"Reactivate any time:     {copy.settings.public_url}/dashboard",
            "",
        ]
    if plan_note:
        lines += [plan_note, ""]
    lines += [
        "Your plan stays exportable whatever you do next.",
    ]
    return Email(
        to=email, subject=subject,
        text="\n".join(lines) + _footer(copy.settings),
        cc_reply_to=copy.settings.support_email,
        headers={"X-Funnel-Email": "cancel-reactivated" if reactivated else "cancellation"})


def renewal_notice_email(copy: Copy, *, email: str, cancel_url: str) -> Email:
    """The day −2 notice (spec §2.1). The cheapest fraud-flag reducer there is, so the
    amount and the date are the first two lines of the body, not the third paragraph."""
    subject = f"{copy.settings.brand_name} renews on {copy.renewal_date}: {copy.rebill_per_month}"
    body = "\n".join([
        "Hi there,",
        "",
        f"Your subscription renews in two days.",
        "",
        f"Date:   {copy.renewal_date}",
        f"Amount: {copy.rebill_per_month}",
        "",
        "Nothing you need to do. If you would rather not continue, cancelling now stops this "
        "renewal and keeps your access until the period you already paid for ends.",
        "",
        f"Cancel in one click: {cancel_url}",
    ])
    return Email(to=email, subject=subject, text=body + _footer(copy.settings, cancel_url),
                 cc_reply_to=copy.settings.support_email,
                 headers={"X-Funnel-Email": "renewal-notice"})


def decline_email(copy: Copy, *, email: str, retry_on: str) -> Email:
    """Card declined, one retry coming (spec §2.1: one retry at +3 days, then stop)."""
    subject = f"We could not take your {copy.settings.brand_name} payment"
    body = "\n".join([
        "Hi there,",
        "",
        f"Your card was declined for the {copy.rebill_per_month} charge. Your access stays "
        f"open while we sort it out.",
        "",
        f"We will try the card on file once more on {retry_on}. After that we stop rather "
        "than retry repeatedly, so if the card was the problem, updating it on your account "
        "page is all it takes.",
        "",
        f"Update or cancel: {copy.settings.public_url}/dashboard",
    ])
    return Email(to=email, subject=subject, text=body + _footer(copy.settings),
                 cc_reply_to=copy.settings.support_email,
                 headers={"X-Funnel-Email": "decline"})


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class Mailer:
    """Send, or write to the outbox. Chosen once, at construction, from config."""

    def __init__(self, settings: FunnelSettings) -> None:
        self.settings = settings
        self.sender = settings.email_from or "billing@localhost"
        self.outbox = settings.email_outbox_abspath
        self.transport = "smtp" if settings.smtp_url else "outbox"
        #: Live from the constructor through `main.py`'s shutdown, so pooled SMTP
        #: connections do not leak a socket per restart on reload.
        self._smtp: smtplib.SMTP | None = None

    def send(self, email: Email) -> SendResult:
        """Deliver one message, never raising. Callers act on `SendResult.ok`."""
        if not email.to or "@" not in email.to:
            # No address is not a transport failure, and reporting it as one invites a retry
            # loop. Saying so plainly is what makes the outbox not look like lost mail.
            return SendResult(email.to, email.subject, False, "none",
                              "no recipient address on file")
        try:
            if self.transport == "smtp":
                self._send_smtp(email)
            else:
                path = self._write_outbox(email)
                return SendResult(email.to, email.subject, True, "outbox", str(path))
        except Exception as exc:  # transport errors are data, reported, not propagated
            log.warning("email to %s (%s) failed: %s", email.to, email.subject, exc)
            return SendResult(email.to, email.subject, False, self.transport,
                              f"{type(exc).__name__}: {exc}")
        return SendResult(email.to, email.subject, True, self.transport, "sent")

    def send_all(self, emails: list[Email]) -> list[SendResult]:
        """Send several, reporting each. Never raises, so a route can send and respond."""
        return [self.send(email) for email in emails]

    def _write_outbox(self, email: Email) -> object:
        self.outbox.mkdir(parents=True, exist_ok=True)
        message = email.to_message(sender=self.sender)
        # Colon-free and strictly ascending, so two messages sent in the same second stay
        # ordered by filename: the Date header is second-resolution, and a receipt written
        # after its cancellation email can otherwise sort before it.
        stamp = f"{message['Date'].replace(':', '')}.{time.time_ns() % 1000:03d}"
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in email.to)[:60]
        kind = email.headers.get("X-Funnel-Email", "message")
        path = self.outbox / f"{stamp}-{kind}-{safe}.eml"
        path.write_bytes(bytes(message))
        log.info("outbox email written: %s (%s)", path, email.subject)
        return path

    # ---- SMTP --------------------------------------------------------------

    def _parse_url(self) -> tuple[str, int, bool, str, str]:
        """`smtp://user:pass@host:port` or `smtps://host:465`. Password may be percent-encoded."""
        parsed = urlparse(self.settings.smtp_url)
        if parsed.scheme not in {"smtp", "smtps"}:
            raise ValueError("FUNNEL_SMTP_URL must start with smtp:// or smtps://")
        port = parsed.port or (465 if parsed.scheme == "smtps" else 587)
        use_tls = parsed.scheme == "smtps"
        # STARTTLS on the submission port stays plaintext until upgraded, which is what a
        # `smtp://…:587` URL means; scheme decides encryption, nothing else does.
        return (parsed.hostname or "localhost", port, use_tls,
                unquote(parsed.username or ""), unquote(parsed.password or ""))

    def _send_smtp(self, email: Email) -> None:
        host, port, use_tls, user, password = self._parse_url()
        message = email.to_message(sender=self.sender)
        client = self._smtp
        if client is None:
            if use_tls:
                client = smtplib.SMTP_SSL(host, port, timeout=20,
                                          context=ssl.create_default_context())
            else:
                client = smtplib.SMTP(host, port, timeout=20)
                client.ehlo()
                if client.has_extn("starttls"):
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
            if user:
                client.login(user, password)
            self._smtp = client
        try:
            client.send_message(message, from_addr=self.sender, to_addrs=email.to)
        except Exception:
            # A pooled connection that threw is not safe to reuse: drop it so the next
            # message negotiates a fresh one instead of inheriting a half-written socket.
            self.close()
            raise

    def close(self) -> None:
        client, self._smtp = self._smtp, None
        if client is None:
            return
        try:
            client.quit()
        except Exception:
            pass


def sender_header(settings: FunnelSettings) -> str:
    """Display name plus address, so the statement descriptor and the inbox agree (slot 9)."""
    address = settings.email_from or "billing@localhost"
    return formataddr((settings.brand_name, address))
