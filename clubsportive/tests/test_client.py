import datetime as dt
import json

import pytest
from conftest import FakeResponse

from poolbot.client import ApiError, AuthError, Outcome, classify, parse_api_datetime


def test_login_sends_basic_auth_and_body_then_caches_token(make_client, tmp_path):
    def handler(call):
        assert call.method == "POST"
        assert call.url == "https://api.example.test/v1/login"
        assert call.json == {"username": "swimmer@example.test", "password": "s3cret"}
        assert call.auth is not None  # HTTP Basic, as the app does it
        assert call.headers["x-public-facility-group"] == "BRANDEDAPPTEST-1"
        assert call.headers["x-nox-client-type"] == "APP_V5"
        assert "x-auth-token" not in call.headers
        return FakeResponse(200, {"login_response": "aaaa-bbbb"})

    client = make_client(handler)
    assert client.login() == "aaaa-bbbb"
    assert client.login() == "aaaa-bbbb"  # cached in memory, no second request
    assert len(client.fake.calls) == 1
    cached = json.loads((tmp_path / "token.json").read_text())
    assert cached == {"username": "swimmer@example.test", "token": "aaaa-bbbb"}


def test_token_cache_is_reused_by_a_new_client(make_client, tmp_path):
    make_client(lambda call: FakeResponse(200, {"login_response": "tok-1"})).login()
    fresh = make_client(lambda call: pytest.fail("should not have logged in again"))
    assert fresh.token == "tok-1"


def test_token_cache_ignored_for_a_different_account(make_client, tmp_path, endpoints):
    from poolbot.client import ClubSportiveClient

    make_client(lambda call: FakeResponse(200, {"login_response": "tok-1"})).login()
    other = ClubSportiveClient(
        endpoints, "someone-else@example.test", "pw", token_path=tmp_path / "token.json"
    )
    assert other.token is None


def test_later_calls_carry_the_token(make_client):
    seen = []

    def handler(call):
        seen.append(call)
        if call.url.endswith("/login"):
            return FakeResponse(200, {"login_response": "tok"})
        return FakeResponse(200, {"firstname": "Alessandro"})

    client = make_client(handler)
    assert client.me()["firstname"] == "Alessandro"
    assert seen[-1].headers["x-auth-token"] == "tok"


def test_bad_credentials_raise_auth_error(make_client):
    client = make_client(lambda call: FakeResponse(401, {"message": "bad credentials"}))
    with pytest.raises(AuthError, match="bad credentials"):
        client.login()


def test_login_without_a_token_in_the_body_is_an_error(make_client):
    client = make_client(lambda call: FakeResponse(200, {"unexpected": 1}))
    with pytest.raises(AuthError, match="no token"):
        client.login()


def test_expired_token_triggers_exactly_one_relogin(make_client):
    calls = []

    def handler(call):
        calls.append(call.url)
        if call.url.endswith("/login"):
            return FakeResponse(200, {"login_response": "fresh"})
        if len([c for c in calls if c.endswith("/v1/me")]) == 1:
            return FakeResponse(401, {"message": "token expired"})
        return FakeResponse(200, {"firstname": "A"})

    client = make_client(handler, use_token_cache=False)
    client.token = "stale"
    assert client.me()["firstname"] == "A"
    assert calls == ["https://api.example.test/v1/me",
                     "https://api.example.test/v1/login",
                     "https://api.example.test/v1/me"]


def test_server_errors_are_retried_then_reported(make_client):
    attempts = []

    def handler(call):
        if call.url.endswith("/login"):
            return FakeResponse(200, {"login_response": "t"})
        attempts.append(call)
        return FakeResponse(503, {"message": "maintenance"})

    client = make_client(handler)
    with pytest.raises(ApiError, match="maintenance"):
        client.me()
    assert len(attempts) == 3  # one try plus two retries


# ------------------------------------------------------------------------ date parsing


@pytest.mark.parametrize(
    "value, expected_utc",
    [
        ("2026-11-03T07:00:00Z", "2026-11-03T07:00:00+00:00"),
        ("2026-11-03T07:00:00+01:00", "2026-11-03T06:00:00+00:00"),
        ("2026-11-03 07:00:00", "2026-11-03T06:00:00+00:00"),  # naive = club local (CET)
        ("2026-11-03T07:00:00.123456789Z", "2026-11-03T07:00:00.123456+00:00"),
        (1793689200000, "2026-11-03T07:00:00+00:00"),
        ("1793689200", "2026-11-03T07:00:00+00:00"),
    ],
)
def test_parse_api_datetime(value, expected_utc):
    from zoneinfo import ZoneInfo

    parsed = parse_api_datetime(value, ZoneInfo("Europe/Amsterdam"))
    assert parsed.astimezone(dt.timezone.utc).isoformat() == expected_utc


@pytest.mark.parametrize("value", [None, "", "not a date", True, []])
def test_parse_api_datetime_rejects_junk(value):
    assert parse_api_datetime(value) is None


# ------------------------------------------------------------------- slot normalisation


def test_normalises_a_plain_list(make_client):
    client = make_client(lambda call: FakeResponse(200, {"login_response": "t"}))
    slots = client.normalise_slots(
        [
            {
                "id": 17,
                "name": "Lap Swim",
                "startDateTime": "2026-11-03T07:00:00Z",
                "endDateTime": "2026-11-03T07:45:00Z",
                "facilityName": "Clubsportive Zuidas",
                "roomName": "Pool",
                "capacity": 12,
                "bookedCount": 11,
                "bookableFrom": "2026-11-01T07:00:00Z",
                "bookingState": "BOOKABLE",
            }
        ]
    )
    assert len(slots) == 1
    slot = slots[0]
    assert slot.id == "17"  # ids are normalised to strings
    assert slot.title == "Lap Swim"
    assert slot.capacity == 12 and slot.booked == 11 and slot.free == 1
    assert slot.bookable_from.isoformat() == "2026-11-01T07:00:00+00:00"
    assert not slot.is_booked_by_me


def test_normalises_alternate_spellings_and_nested_roots(make_client):
    client = make_client(lambda call: FakeResponse(200, {"login_response": "t"}))
    payload = {
        "content": {
            "classes": [
                {
                    "classId": "abc",
                    "activity": {"name": "Zwemmen"},
                    "start": 1793689200000,
                    "maxParticipants": 10,
                    "currentParticipants": 2,
                    "myBookingState": "BOOKED",
                }
            ]
        }
    }
    slots = client.normalise_slots(payload)
    assert [s.id for s in slots] == ["abc"]
    assert slots[0].title == "Zwemmen"  # found one level down
    assert slots[0].free == 8
    assert slots[0].is_booked_by_me


def test_finds_slot_list_by_shape_when_the_key_is_unknown(make_client):
    client = make_client(lambda call: FakeResponse(200, {"login_response": "t"}))
    payload = {"weird": {"deeper": [{"id": "x", "startDate": "2026-11-03T07:00:00Z"}]}}
    assert [s.id for s in client.normalise_slots(payload)] == ["x"]


def test_slots_without_an_id_are_dropped(make_client):
    client = make_client(lambda call: FakeResponse(200, {"login_response": "t"}))
    assert client.normalise_slots([{"name": "no id here"}]) == []


def test_slots_url_templating(make_client):
    seen = []

    def handler(call):
        seen.append(call.url)
        if call.url.endswith("/login"):
            return FakeResponse(200, {"login_response": "t"})
        return FakeResponse(200, [])

    client = make_client(handler)
    client.slots(dt.date(2026, 11, 3), dt.date(2026, 11, 5))
    assert seen[-1] == "https://api.example.test/v1/classes?from=2026-11-03&to=2026-11-05"


# ---------------------------------------------------------------------- classification


@pytest.mark.parametrize(
    "status, payload, expected",
    [
        (200, {"bookingId": "b1"}, Outcome.BOOKED),
        (201, {}, Outcome.BOOKED),
        (200, {"message": "Added to the waiting list"}, Outcome.WAITLISTED),
        (409, {"message": "You have already booked this class"}, Outcome.ALREADY_BOOKED),
        (400, {"message": "Je hebt al geboekt"}, Outcome.ALREADY_BOOKED),
        (400, {"message": "Class is full"}, Outcome.FULL),
        (400, {"message": "No places left"}, Outcome.FULL),
        (409, {"message": "something unhelpful"}, Outcome.FULL),
        (400, {"message": "Booking not yet possible"}, Outcome.NOT_YET_OPEN),
        (400, {"message": "Nog niet beschikbaar"}, Outcome.NOT_YET_OPEN),
        (404, {}, Outcome.NOT_YET_OPEN),
        (400, {"message": "Weekly maximum reached"}, Outcome.LIMIT_REACHED),
        (401, {}, Outcome.REJECTED),
        (422, {"message": "nope"}, Outcome.REJECTED),
        (429, {}, Outcome.ERROR),
        (500, {}, Outcome.ERROR),
    ],
)
def test_classify(status, payload, expected):
    outcome, _ = classify(status, payload, json.dumps(payload))
    assert outcome is expected


def test_outcome_flags():
    assert Outcome.BOOKED.success and not Outcome.BOOKED.retryable
    assert Outcome.ALREADY_BOOKED.success
    assert Outcome.FULL.retryable and not Outcome.FULL.success
    assert Outcome.NOT_YET_OPEN.retryable
    assert Outcome.ERROR.retryable
    assert not Outcome.REJECTED.retryable
    assert not Outcome.LIMIT_REACHED.retryable  # stop: more attempts cannot help


# ----------------------------------------------------------------------------- booking


def test_book_posts_to_the_templated_url_and_reads_the_booking_id(make_client):
    seen = []

    def handler(call):
        seen.append(call)
        if call.url.endswith("/login"):
            return FakeResponse(200, {"login_response": "t"})
        return FakeResponse(200, {"bookingId": "bk-9"})

    client = make_client(handler)
    client.login()
    attempt = client.book("77")
    assert seen[-1].method == "POST"
    assert seen[-1].url == "https://api.example.test/v1/classes/77/booking"
    assert attempt.outcome is Outcome.BOOKED
    assert attempt.booking_id == "bk-9"


def test_dry_run_sends_nothing(make_client):
    client = make_client(lambda call: pytest.fail("no request expected"))
    attempt = client.book("77", dry_run=True)
    assert attempt.outcome is Outcome.BOOKED
    assert "dry run" in attempt.message


def test_network_failure_is_an_error_not_an_exception(make_client):
    import requests

    def handler(call):
        raise requests.ConnectionError("reset by peer")

    client = make_client(handler, use_token_cache=False)
    client.token = "t"
    attempt = client.book("77")
    assert attempt.outcome is Outcome.ERROR and attempt.outcome.retryable


def test_waitlist_requires_configuration(make_client, endpoints):
    from dataclasses import replace

    client = make_client(lambda call: FakeResponse(200, {}))
    client.ep = replace(endpoints, paths={**endpoints.paths, "waitlist": ""})
    assert client.join_waitlist("1").outcome is Outcome.ERROR


def test_cancel_templates_the_booking_id(make_client):
    seen = []

    def handler(call):
        seen.append(call.url)
        return FakeResponse(200, {"message": "cancelled"})

    client = make_client(handler, use_token_cache=False)
    client.token = "t"
    assert client.cancel(booking_id="bk-9").outcome is Outcome.BOOKED
    assert seen[-1] == "https://api.example.test/v1/bookings/bk-9/cancel"
    with pytest.raises(ValueError):
        client.cancel()


def test_extra_body_fields_are_templated(make_client, endpoints):
    from dataclasses import replace

    seen = []

    def handler(call):
        seen.append(call)
        return FakeResponse(200, {})

    client = make_client(handler, use_token_cache=False)
    client.token = "t"
    client.ep = replace(endpoints, bodies={"book": {"slotId": "{slot_id}", "source": "app"}})
    client.book("55")
    assert seen[-1].json == {"slotId": "55", "source": "app"}


# -------------------------------------------------------------------------- clock sync


def test_clock_offset_comes_from_the_date_header(make_client):
    # Server says 10:00:00; our fake local clock is exactly 10:00:05 -> offset -5s.
    import time

    server_date = "Sun, 01 Nov 2026 10:00:00 GMT"
    server_epoch = dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc).timestamp()

    def handler(call):
        if call.url.endswith("/login"):
            return FakeResponse(200, {"login_response": "t"})
        return FakeResponse(200, {}, headers={"Date": server_date})

    client = make_client(handler, use_token_cache=False)
    client.token = "t"
    original = time.time
    try:
        time.time = lambda: server_epoch + 5.0  # type: ignore[assignment]
        offset = client.sample_server_clock(samples=2)
    finally:
        time.time = original  # type: ignore[assignment]
    assert offset == pytest.approx(-5.0, abs=0.01)


def test_missing_date_header_leaves_the_offset_alone(make_client):
    client = make_client(lambda call: FakeResponse(200, {}), use_token_cache=False)
    client.token = "t"
    assert client.sample_server_clock(samples=1) == 0.0


def test_implausible_clock_offset_is_ignored(make_client, caplog):
    """A Date header hours out (cached proxy, stale CDN) must not move the strike."""
    def handler(call):
        return FakeResponse(200, {}, headers={"Date": "Sun, 01 Nov 2026 10:00:00 GMT"})

    client = make_client(handler, use_token_cache=False)
    client.token = "t"
    with caplog.at_level("WARNING"):
        offset = client.sample_server_clock(samples=1)
    assert offset == 0.0  # we keep our own clock
    assert "implausible" in caplog.text


def test_small_clock_offset_is_applied(make_client):
    import time

    server_epoch = dt.datetime.now(dt.timezone.utc).timestamp()

    def handler(call):
        import email.utils

        return FakeResponse(200, {}, headers={"Date": email.utils.formatdate(server_epoch, usegmt=True)})

    client = make_client(handler, use_token_cache=False)
    client.token = "t"
    original = time.time
    try:
        time.time = lambda: server_epoch - 4.0  # our clock is 4s behind
        offset = client.sample_server_clock(samples=2)
    finally:
        time.time = original
    assert offset == pytest.approx(4.0, abs=1.0)
