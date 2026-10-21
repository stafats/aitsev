"""Outgoing email: SMTP in production, the log (and an in-memory outbox) in development."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from aitsev.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    async def send(self, email: OutgoingEmail) -> None: ...


class LogEmailSender:
    """Development sender: logs the message and keeps it for tests."""

    def __init__(self) -> None:
        self.outbox: list[OutgoingEmail] = []

    async def send(self, email: OutgoingEmail) -> None:
        self.outbox.append(email)
        log.info("Email to %s: %s\n%s", email.to, email.subject, email.body)


class SmtpEmailSender:
    def __init__(
        self, host: str, port: int, username: str | None, password: str | None, sender: str
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender

    def _send_sync(self, email: OutgoingEmail) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = email.to
        message["Subject"] = email.subject
        message.set_content(email.body)
        context = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=30) as smtp:
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(message)
            return
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls(context=context)
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)

    async def send(self, email: OutgoingEmail) -> None:
        await asyncio.to_thread(self._send_sync, email)


def sender_from_settings(settings: Settings) -> EmailSender:
    if settings.smtp_host:
        return SmtpEmailSender(
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_username,
            settings.smtp_password.get_secret_value() if settings.smtp_password else None,
            settings.email_from,
        )
    if settings.environment != "development":
        log.warning("AITSEV_SMTP_HOST is not set: account emails are only written to the log")
    return LogEmailSender()
