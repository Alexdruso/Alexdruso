import datetime as dt

from poolbot.client import Outcome, Slot
from poolbot.notify import MailSettings, build_ics, notify_result, render
from poolbot.scheduler import Strike, StrikeResult, release_moment


def make_result(cfg, outcome=Outcome.BOOKED, attempts=2, with_slot=True):
    target = dt.datetime(2026, 11, 3, 7, 0, tzinfo=cfg.tz)
    slot = Slot(
        id="42",
        title="Lap Swim 45min",
        start=target,
        end=target + dt.timedelta(minutes=45),
        location="Clubsportive Zuidas",
        room="Pool",
        capacity=12,
        booked=12,
    )
    return StrikeResult(
        strike=Strike(cfg.wishes[0], target, release_moment(cfg, target)),
        outcome=outcome,
        slot=slot if with_slot else None,
        attempts=attempts,
        message="ok",
        first_attempt_at=dt.datetime(2026, 11, 1, 6, 59, 59, tzinfo=dt.timezone.utc),
        booked_at=dt.datetime(2026, 11, 1, 7, 0, 0, 400000, tzinfo=dt.timezone.utc),
    )


def test_success_mail_mentions_the_slot_and_timing(cfg):
    subject, body = render(make_result(cfg), cfg)
    assert "booked" in subject and "Tue 03 Nov 07:00" in subject
    assert "Lap Swim 45min" in body
    assert "Attempts:  2" in body
    assert "1.40s from first attempt" in body  # 06:59:59.0 -> 07:00:00.4


def test_failure_mail_says_what_to_do(cfg):
    subject, body = render(make_result(cfg, Outcome.FULL), cfg)
    assert subject.startswith("[poolbot] FAILED")
    assert "Nothing was booked" in body


def test_dry_run_is_flagged_loudly(cfg):
    result = make_result(cfg)
    result.dry_run = True
    _, body = render(result, cfg)
    assert "DRY RUN" in body


def test_mail_renders_without_a_slot(cfg):
    subject, body = render(make_result(cfg, Outcome.ERROR, with_slot=False), cfg)
    assert "Session:   Tuesday 03 November 2026 07:00" in body
    assert "Slot:" not in body


def test_ics_has_one_event_in_utc(cfg):
    ics = build_ics(make_result(cfg).slot)
    assert ics.count("BEGIN:VEVENT") == 1
    assert "DTSTART:20261103T060000Z" in ics  # 07:00 CET
    assert "DTEND:20261103T064500Z" in ics
    assert "SUMMARY:Lap Swim 45min" in ics
    assert ics.endswith("\r\n")


def test_ics_is_skipped_without_a_start(cfg):
    assert build_ics(Slot(id="1", title="x", start=None)) == ""


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_TO", "a@b.c, d@e.f")
    monkeypatch.setenv("SMTP_FROM", "me@b.c")
    settings = MailSettings.from_env()
    assert settings.recipients == ("a@b.c", "d@e.f")
    assert settings.port == 587 and settings.starttls is True

    monkeypatch.setenv("SMTP_PORT", "465")
    assert MailSettings.from_env().starttls is False  # implicit TLS on 465
    monkeypatch.setenv("SMTP_STARTTLS", "1")
    assert MailSettings.from_env().starttls is True


def test_no_settings_without_a_host(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert MailSettings.from_env() is None


def test_incomplete_settings_are_refused(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.delenv("SMTP_TO", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    assert MailSettings.from_env() is None


def test_notify_honours_the_config_switches(cfg, monkeypatch):
    import dataclasses

    from poolbot.config import NotifyCfg

    sent = []
    monkeypatch.setattr("poolbot.notify.send", lambda *a, **k: sent.append(a) or True)
    settings = MailSettings(host="h", sender="a@b.c", recipients=("d@e.f",))

    quiet_success = dataclasses.replace(cfg, notify=NotifyCfg(on_success=False, on_failure=True))
    assert notify_result(make_result(cfg), quiet_success, settings) is False
    assert sent == []

    assert notify_result(make_result(cfg), cfg, settings) is True
    assert len(sent) == 1

    quiet_failure = dataclasses.replace(cfg, notify=NotifyCfg(on_success=True, on_failure=False))
    assert notify_result(make_result(cfg, Outcome.FULL), quiet_failure, settings) is False


def test_send_reports_smtp_failure_instead_of_raising(cfg, monkeypatch):
    import smtplib

    from poolbot.notify import send

    def explode(*args, **kwargs):
        raise smtplib.SMTPConnectError(421, "nope")

    monkeypatch.setattr(smtplib, "SMTP", explode)
    settings = MailSettings(host="h", sender="a@b.c", recipients=("d@e.f",))
    assert send("s", "b", settings) is False
