import email.utils

import pytest
from conftest import FakeResponse, FakeSession

from poolbot import cli
from poolbot.client import ClubSportiveClient

CONFIG = """
timezone: Europe/Amsterdam
release_offset_hours: 48
match:
  activity_patterns: ["lap swim"]
wishes:
  - name: Tuesday early swim
    weekday: tue
    start: "07:00"
    tolerance_minutes: 30
limits:
  max_bookings_per_week: 3
strike:
  lead_seconds: 0
  window_seconds: 1
  attempt_interval_ms: 50
  max_attempts: 3
  prefetch_seconds: 0
  warmup_seconds: 0
  clocksync_seconds: 0
"""

ENDPOINTS = """
base_url = "https://api.example.test"
[headers]
facility_group = "BRANDEDAPPTEST-1"
[paths]
login = "/v1/login"
me = "/v1/me"
slots = "/v1/classes?from={date_from}&to={date_to}"
book = "/v1/classes/{slot_id}/booking"
cancel = "/v1/bookings/{booking_id}/cancel"
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(CONFIG)
    (tmp_path / "endpoints.toml").write_text(ENDPOINTS)
    (tmp_path / ".env").write_text("CS_USERNAME=a@b.c\nCS_PASSWORD=pw\n")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    return tmp_path


def argv(workspace, *args):
    return [
        "--config", str(workspace / "config.yaml"),
        "--endpoints", str(workspace / "endpoints.toml"),
        "--state", str(workspace / "state.json"),
        "--env", str(workspace / ".env"),
        *args,
    ]


@pytest.fixture
def fake_api(workspace, monkeypatch):
    """Route the CLI's client through a scripted fake session."""
    holder = {}

    def install(handler):
        def factory(endpoints, tz=None, **kwargs):
            session = FakeSession(handler)
            client = ClubSportiveClient(
                endpoints, "a@b.c", "pw", tz=tz, session=session,
                token_path=workspace / "token.json",
            )
            holder["client"] = client
            holder["session"] = session
            return client

        monkeypatch.setattr(cli, "client_from_env", factory)
        return holder

    return install


SLOT = {
    "id": "s1",
    "name": "Lap Swim",
    "startDateTime": "2026-11-03T07:00:00+01:00",
    "endDateTime": "2026-11-03T07:45:00+01:00",
    "facilityName": "Clubsportive Zuidas",
    "capacity": 12,
    "bookedCount": 4,
}


def default_handler(call):
    if call.url.endswith("/login"):
        return FakeResponse(200, {"login_response": "tok"})
    if "/classes?" in call.url:
        return FakeResponse(200, {"classes": [SLOT, {**SLOT, "id": "s2", "name": "Spinning"}]})
    if call.url.endswith("/booking"):
        return FakeResponse(200, {"bookingId": "bk-1"})
    if "/cancel" in call.url:
        return FakeResponse(200, {"message": "cancelled"})
    # A current Date header, as a real server sends: the measured skew is ~0.
    return FakeResponse(200, {"firstname": "Alessandro", "lastname": "Sanvito"},
                        headers={"Date": email.utils.formatdate(usegmt=True)})


def test_plan_needs_no_api(workspace, capsys):
    assert cli.main(argv(workspace, "plan", "--days", "7")) == 0
    out = capsys.readouterr().out
    assert "Tuesday early swim" in out and "books at" in out


def test_doctor_reports_login_and_clock(workspace, fake_api, capsys):
    fake_api(default_handler)
    assert cli.main(argv(workspace, "doctor")) == 0
    out = capsys.readouterr().out
    assert "login       ok" in out
    assert "Alessandro Sanvito" in out
    assert "clock skew" in out
    assert "BRANDEDAPPTEST-1" in out


def test_doctor_fails_loudly_on_bad_credentials(workspace, fake_api, capsys):
    fake_api(lambda call: FakeResponse(401, {"message": "wrong password"}))
    assert cli.main(argv(workspace, "doctor")) == 1
    assert "LOGIN FAILED" in capsys.readouterr().err


def test_slots_filters_to_the_pool_by_default(workspace, fake_api, capsys):
    fake_api(default_handler)
    assert cli.main(argv(workspace, "slots", "--date", "2026-11-03")) == 0
    out = capsys.readouterr().out
    assert "Lap Swim" in out and "Spinning" not in out
    assert "8 free" in out

    assert cli.main(argv(workspace, "slots", "--date", "2026-11-03", "--all")) == 0
    assert "Spinning" in capsys.readouterr().out


def test_slots_raw_dumps_json(workspace, fake_api, capsys):
    fake_api(default_handler)
    assert cli.main(argv(workspace, "slots", "--raw", "--date", "2026-11-03")) == 0
    assert '"bookedCount": 4' in capsys.readouterr().out


def test_book_then_cancel_updates_state(workspace, fake_api, capsys):
    fake_api(default_handler)
    assert cli.main(argv(workspace, "book", "--slot-id", "s1")) == 0
    assert "BOOKED" in capsys.readouterr().out

    from poolbot.state import State
    from zoneinfo import ZoneInfo

    state = State.load(workspace / "state.json", ZoneInfo("Europe/Amsterdam"))
    assert state.already_booked("s1")
    assert state.bookings[0].booking_id == "bk-1"

    assert cli.main(argv(workspace, "cancel", "--slot-id", "s1")) == 0
    state = State.load(workspace / "state.json", ZoneInfo("Europe/Amsterdam"))
    assert not state.already_booked("s1")


def test_dry_run_book_sends_nothing(workspace, fake_api, capsys):
    holder = fake_api(lambda call: FakeResponse(200, {"login_response": "tok"}))
    assert cli.main(argv(workspace, "--dry-run", "book", "--slot-id", "s1")) == 0
    assert "dry run" in capsys.readouterr().out
    assert not any(c.url.endswith("/booking") for c in holder["session"].calls)


def test_snipe_now_books_immediately(workspace, fake_api, capsys):
    fake_api(default_handler)
    code = cli.main(
        argv(workspace, "snipe", "--date", "2026-11-03", "--now", "--no-mail")
    )
    assert code == 0
    assert "BOOKED" in capsys.readouterr().out


def test_snipe_reports_failure_with_a_nonzero_exit(workspace, fake_api, capsys):
    def handler(call):
        if call.url.endswith("/booking"):
            return FakeResponse(400, {"message": "Class is full"})
        return default_handler(call)

    fake_api(handler)
    code = cli.main(argv(workspace, "snipe", "--date", "2026-11-03", "--now", "--no-mail"))
    assert code == 1
    assert "FULL" in capsys.readouterr().out


def test_history_shows_what_happened(workspace, fake_api, capsys):
    fake_api(default_handler)
    cli.main(argv(workspace, "book", "--slot-id", "s1"))
    capsys.readouterr()
    assert cli.main(argv(workspace, "history")) == 0
    out = capsys.readouterr().out
    assert "Lap Swim" in out and "slot s1" in out


def test_history_with_nothing_yet(workspace, capsys):
    assert cli.main(argv(workspace, "history")) == 0
    assert "no history yet" in capsys.readouterr().out


def test_missing_config_is_a_clear_exit_code(tmp_path, capsys):
    code = cli.main(["--config", str(tmp_path / "absent.yaml"), "plan"])
    assert code == 2
    assert "configuration problem" in capsys.readouterr().err


def test_placeholder_endpoints_are_refused(workspace, capsys):
    (workspace / "endpoints.toml").write_text(
        ENDPOINTS.replace("https://api.example.test", "https://REPLACE-ME")
    )
    assert cli.main(argv(workspace, "doctor")) == 2
    assert "placeholder" in capsys.readouterr().err


def test_extract_subcommand_is_wired_up(workspace, tmp_path, capsys):
    import zipfile

    apk = tmp_path / "app.apk"
    with zipfile.ZipFile(apk, "w") as z:
        z.writestr(
            "lib/arm64-v8a/libapp.so",
            b"https://api.found.example/v9/\x00BRANDEDAPPZZ-1\x00APP_V5\x00/v9/login\x00",
        )
    out = tmp_path / "generated.toml"
    assert cli.main(argv(workspace, "extract", str(apk), "-o", str(out))) == 0
    assert 'base_url = "https://api.found.example"' in out.read_text()


def test_default_paths_prefer_the_working_directory(tmp_path, monkeypatch):
    from poolbot.cli import ROOT, default_path

    monkeypatch.chdir(tmp_path)
    assert default_path("config.yaml") == ROOT / "config.yaml"  # nothing local
    (tmp_path / "config.yaml").write_text(CONFIG)
    assert default_path("config.yaml") == tmp_path / "config.yaml"


def test_sibling_files_follow_the_config(tmp_path):
    from poolbot.cli import build_parser, resolve_paths

    (tmp_path / "config.yaml").write_text(CONFIG)
    args = build_parser().parse_args(["--config", str(tmp_path / "config.yaml"), "plan"])
    resolve_paths(args)
    assert args.endpoints == tmp_path / "endpoints.toml"
    assert args.state == tmp_path / "state.json"
    assert args.env == tmp_path / ".env"


def test_explicit_paths_win(tmp_path):
    from poolbot.cli import build_parser, resolve_paths

    args = build_parser().parse_args(
        ["--config", str(tmp_path / "config.yaml"), "--state", "/var/lib/poolbot/s.json", "plan"]
    )
    resolve_paths(args)
    assert str(args.state) == "/var/lib/poolbot/s.json"
