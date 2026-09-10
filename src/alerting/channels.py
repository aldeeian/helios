"""Delivery channels for alerts.

Design rule: **the file channel is always on.** Slack and email can fail for
reasons outside the pipeline's control (expired token, rate limit, DNS), and an
alert that vanishes because a webhook 500'd is worse than no alerting at all.
Every alert lands in data/alerts/ first; remote channels are best-effort on top,
and their failures are reported, never raised into the caller's flow.

Configuration is entirely environment-driven (see .env.example):

    HELIOS_SLACK_WEBHOOK_URL   Slack incoming webhook
    RESEND_API_KEY             Resend transactional email
    HELIOS_ALERT_FROM          sender address (Resend/SMTP)
    HELIOS_ALERT_TO            comma-separated recipients
    HELIOS_SMTP_HOST/PORT/USER/PASSWORD   plain SMTP alternative to Resend
"""

from __future__ import annotations

import datetime as dt
import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Protocol

from .. import config

ALERTS_DIR = config.DATA_DIR / "alerts"

# Levels ordered by increasing urgency; used to filter what each channel takes.
LEVELS = ("info", "warning", "critical")


@dataclass
class Alert:
    """One notification, rendered once and delivered to every channel."""

    subject: str
    body_text: str
    level: str = "warning"
    body_html: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"level must be one of {LEVELS}, got {self.level!r}")


@dataclass
class Delivery:
    """Outcome of one channel attempt — success and failure look the same shape
    so the CLI can print a uniform report and callers can assert on it."""

    channel: str
    ok: bool
    detail: str


class Sender(Protocol):
    name: str

    def send(self, alert: Alert) -> Delivery: ...


# --- file (always on) -----------------------------------------------------

class FileSender:
    """Write the alert to data/alerts/ as JSON. The durable record of record."""

    name = "file"

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory or ALERTS_DIR)

    def send(self, alert: Alert) -> Delivery:
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
        path = self.directory / f"{stamp}_{alert.level}.json"
        path.write_text(json.dumps({
            "timestamp": dt.datetime.now().isoformat(),
            "level": alert.level,
            "subject": alert.subject,
            "body": alert.body_text,
            "metadata": alert.metadata,
        }, indent=2, default=str), encoding="utf-8")
        return Delivery(self.name, True, str(path))


# --- Slack ----------------------------------------------------------------

class SlackSender:
    """Post to a Slack incoming webhook.

    Slack renders its own subset of markdown, so the plain-text body is sent
    inside a code-free mrkdwn block rather than the HTML version.
    """

    name = "slack"

    def __init__(self, webhook_url: str, timeout: float = 10.0) -> None:
        self.webhook_url = webhook_url
        self.timeout = timeout

    def send(self, alert: Alert) -> Delivery:
        emoji = {"info": ":information_source:", "warning": ":warning:",
                 "critical": ":rotating_light:"}[alert.level]
        payload = {
            "text": f"{emoji} *{alert.subject}*",
            "blocks": [
                {"type": "header",
                 "text": {"type": "plain_text", "text": alert.subject[:150]}},
                {"type": "section",
                 "text": {"type": "mrkdwn", "text": alert.body_text[:2900]}},
            ],
        }
        req = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return Delivery(self.name, 200 <= resp.status < 300,
                                f"HTTP {resp.status}")
        except (urllib.error.URLError, OSError) as e:
            return Delivery(self.name, False, f"{type(e).__name__}: {e}")


# --- email: Resend --------------------------------------------------------

class ResendSender:
    """Send via Resend's REST API. Chosen over SMTP for cloud deploys where
    outbound port 587 is commonly blocked."""

    name = "resend"
    API_URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str, sender: str, recipients: list[str],
                 timeout: float = 15.0) -> None:
        self.api_key = api_key
        self.sender = sender
        self.recipients = recipients
        self.timeout = timeout

    def send(self, alert: Alert) -> Delivery:
        payload = {
            "from": self.sender,
            "to": self.recipients,
            "subject": alert.subject,
            "text": alert.body_text,
        }
        if alert.body_html:
            payload["html"] = alert.body_html
        req = urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
                return Delivery(self.name, True, f"id={body.get('id', '?')}")
        except urllib.error.HTTPError as e:
            # Surface the status code — 403 here almost always means the Resend
            # sandbox sender can only mail the account owner.
            return Delivery(self.name, False,
                            f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        except (urllib.error.URLError, OSError) as e:
            return Delivery(self.name, False, f"{type(e).__name__}: {e}")


# --- email: SMTP ----------------------------------------------------------

class SMTPSender:
    """Plain SMTP with STARTTLS, for self-hosted or corporate mail relays."""

    name = "smtp"

    def __init__(self, host: str, port: int, sender: str, recipients: list[str],
                 username: str | None = None, password: str | None = None,
                 timeout: float = 20.0) -> None:
        self.host, self.port = host, port
        self.sender, self.recipients = sender, recipients
        self.username, self.password = username, password
        self.timeout = timeout

    def send(self, alert: Alert) -> Delivery:
        msg = EmailMessage()
        msg["Subject"] = alert.subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(alert.body_text)
        if alert.body_html:
            msg.add_alternative(alert.body_html, subtype="html")
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as s:
                s.starttls(context=ssl.create_default_context())
                if self.username and self.password:
                    s.login(self.username, self.password)
                s.send_message(msg)
            return Delivery(self.name, True, f"{len(self.recipients)} recipient(s)")
        except (smtplib.SMTPException, OSError) as e:
            return Delivery(self.name, False, f"{type(e).__name__}: {e}")


# --- wiring ---------------------------------------------------------------

def _recipients() -> list[str]:
    raw = os.environ.get("HELIOS_ALERT_TO", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def build_senders() -> list[Sender]:
    """Assemble the channel list from the environment.

    FileSender is unconditional. Remote channels are added only when fully
    configured — a half-set credential silently doing nothing is a worse
    failure mode than an obviously-absent channel.
    """
    senders: list[Sender] = [FileSender()]

    webhook = os.environ.get("HELIOS_SLACK_WEBHOOK_URL", "").strip()
    if webhook:
        senders.append(SlackSender(webhook))

    to = _recipients()
    sender_addr = os.environ.get("HELIOS_ALERT_FROM", "").strip()
    resend_key = os.environ.get("RESEND_API_KEY", "").strip()
    smtp_host = os.environ.get("HELIOS_SMTP_HOST", "").strip()

    if to and sender_addr:
        if resend_key:
            senders.append(ResendSender(resend_key, sender_addr, to))
        elif smtp_host:
            senders.append(SMTPSender(
                smtp_host,
                int(os.environ.get("HELIOS_SMTP_PORT", "587")),
                sender_addr, to,
                os.environ.get("HELIOS_SMTP_USER") or None,
                os.environ.get("HELIOS_SMTP_PASSWORD") or None))
    return senders


def send(alert: Alert, senders: list[Sender] | None = None) -> list[Delivery]:
    """Deliver one alert to every channel, collecting outcomes.

    A channel raising is contained here: alerting must never be the reason a
    pipeline run fails, since by definition it runs when something is already
    wrong.
    """
    results = []
    for s in senders if senders is not None else build_senders():
        try:
            results.append(s.send(alert))
        except Exception as e:  # channel bug, not a delivery failure
            results.append(Delivery(getattr(s, "name", "?"), False,
                                    f"unhandled {type(e).__name__}: {e}"))
    return results
