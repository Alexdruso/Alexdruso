"""Result mails over SMTP, with an optional calendar invite for a won slot."""

from __future__ import annotations

import datetime as dt
import logging
import os
import smtplib
import ssl
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Sequence

from .client import Slot
from .config import Config
from .scheduler import StrikeResult

log = logging.getLogger("poolbot.notify")


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int = 587
    user: str | None = None
    password: str | None = None
    sender: str = ""
    recipients: tuple[str, ...] = ()
    starttls: bool = True

    @classmethod
    def from_env(cls) -> "MailSettings | None":
        host = os.environ.get("SMTP_HOST", "").strip()
        if not host:
            return None
        to = [a.strip() for a in os.environ.get("SMTP_TO", "").split(",") if a.strip()]
        sender = os.environ.get("SMTP_FROM", "").strip() or os.environ.get("SMTP_USER", "").strip()
        if not to or not sender:
            log.warning("SMTP_HOST is set but SMTP_TO/SMTP_FROM are not; mail disabled")
            return None
        port = int(os.environ.get("SMTP_PORT", "587") or 587)
        starttls_env = os.environ.get("SMTP_STARTTLS")
        starttls = (starttls_env.strip() not in ("0", "false", "no")) if starttls_env else port != 465
        return cls(
            host=host,
            port=port,
            user=os.environ.get("SMTP_USER") or None,
            password=os.environ.get("SMTP_PASS") or None,
            sender=sender,
            recipients=tuple(to),
            starttls=starttls,
        )


def build_ics(slot: Slot, tz_name: str = "Europe/Amsterdam") -> str:
    """A minimal single-event iCalendar file for a booked session."""
    start = slot.start
    end = slot.end or (start + dt.timedelta(hours=1) if start else None)
    if start is None or end is None:
        return ""

    def stamp(moment: dt.datetime) -> str:
        return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    summary = slot.title or "Pool slot"
    location = ", ".join(x for x in (slot.location, slot.room) if x) or "Clubsportive"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//poolbot//clubsportive//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@poolbot",
        f"DTSTAMP:{stamp(dt.datetime.now(dt.timezone.utc))}",
        f"DTSTART:{stamp(start)}",
        f"DTEND:{stamp(end)}",
        f"SUMMARY:{summary}",
        f"LOCATION:{location}",
        f"DESCRIPTION:Booked automatically by poolbot (slot {slot.id})",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(lines) + "\r\n"


def render(result: StrikeResult, cfg: Config) -> tuple[str, str]:
    """Subject and plain-text body for one strike result."""
    tz = cfg.tz
    when = result.strike.target.astimezone(tz)
    if result.success:
        subject = f"[poolbot] {result.outcome.value.lower()}: {when:%a %d %b %H:%M} pool slot"
    else:
        subject = f"[poolbot] FAILED {result.strike.wish.name} -- {when:%a %d %b %H:%M}"

    lines = [
        f"Wish:      {result.strike.wish}",
        f"Session:   {when:%A %d %B %Y %H:%M} ({tz.key})",
        f"Released:  {result.strike.release.astimezone(tz):%A %d %B %H:%M:%S}",
        f"Outcome:   {result.outcome.value}",
        f"Attempts:  {result.attempts}",
    ]
    if result.slot is not None:
        lines.append(f"Slot:      {result.slot.describe(tz)} (id {result.slot.id})")
        if result.slot.free is not None:
            lines.append(f"Places:    {result.slot.free} free of {result.slot.capacity}")
    if result.first_attempt_at and result.booked_at:
        took = (result.booked_at - result.first_attempt_at).total_seconds()
        lines.append(f"Took:      {took:.2f}s from first attempt to confirmation")
    if result.message:
        lines.append(f"API said:  {result.message}")
    if result.dry_run:
        lines.append("")
        lines.append("** DRY RUN -- no booking request was sent. **")
    if result.log:
        lines += ["", "Timeline:", *(f"  {entry}" for entry in result.log)]
    if not result.success:
        lines += [
            "",
            "Nothing was booked. Worth checking by hand in the app, and `poolbot slots`",
            "for what else is free that day.",
        ]
    return subject, "\n".join(lines) + "\n"


def send(
    subject: str,
    body: str,
    settings: MailSettings,
    *,
    ics: str | None = None,
    ics_name: str = "pool-slot.ics",
    timeout: float = 20.0,
) -> bool:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.sender
    msg["To"] = ", ".join(settings.recipients)
    msg.set_content(body)
    if ics:
        msg.add_attachment(
            ics.encode("utf-8"), maintype="text", subtype="calendar", filename=ics_name
        )
    try:
        if settings.starttls:
            with smtplib.SMTP(settings.host, settings.port, timeout=timeout) as server:
                server.starttls(context=ssl.create_default_context())
                if settings.user and settings.password:
                    server.login(settings.user, settings.password)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(
                settings.host, settings.port, timeout=timeout, context=ssl.create_default_context()
            ) as server:
                if settings.user and settings.password:
                    server.login(settings.user, settings.password)
                server.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        log.error("could not send mail: %s", exc)
        return False
    log.info("emailed %s: %s", ", ".join(settings.recipients), subject)
    return True


def notify_result(result: StrikeResult, cfg: Config, settings: MailSettings | None = None) -> bool:
    """Email a strike result, honouring the notify.* config switches."""
    if result.success and not cfg.notify.on_success:
        return False
    if not result.success and not cfg.notify.on_failure:
        return False
    settings = settings or MailSettings.from_env()
    if settings is None:
        log.info("no SMTP settings; result was: %s", result.summary(cfg))
        return False
    subject, body = render(result, cfg)
    ics = None
    if result.success and cfg.notify.attach_ics and result.slot is not None:
        ics = build_ics(result.slot, cfg.tz.key) or None
    return send(subject, body, settings, ics=ics)


def notify_text(subject: str, body: str, settings: MailSettings | None = None) -> bool:
    settings = settings or MailSettings.from_env()
    if settings is None:
        log.info("no SMTP settings; not sending: %s", subject)
        return False
    return send(subject, body, settings)
