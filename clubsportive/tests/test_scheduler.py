import dataclasses
import datetime as dt

import pytest
from conftest import FakeClock

from poolbot.client import Outcome
from poolbot.config import StrikeCfg
from poolbot.scheduler import (
    Strike,
    due_strikes,
    local_target,
    next_strike,
    next_target,
    plan,
    release_moment,
    run_daemon,
    run_strike,
    sleep_until,
    spin_until,
    watch,
)
from poolbot.state import Booking

UTC = dt.timezone.utc


# ------------------------------------------------------------- release-time arithmetic


def test_next_target_finds_the_coming_tuesday(cfg):
    wish = cfg.wish("Tuesday early swim")
    now = dt.datetime(2026, 11, 1, 12, 0, tzinfo=cfg.tz)  # a Sunday
    assert next_target(cfg, wish, now) == dt.datetime(2026, 11, 3, 7, 0, tzinfo=cfg.tz)


def test_next_target_skips_today_once_the_time_has_passed(cfg):
    wish = cfg.wish("Tuesday early swim")
    same_day_late = dt.datetime(2026, 11, 3, 8, 0, tzinfo=cfg.tz)
    assert next_target(cfg, wish, same_day_late).date() == dt.date(2026, 11, 10)
    same_day_early = dt.datetime(2026, 11, 3, 6, 0, tzinfo=cfg.tz)
    assert next_target(cfg, wish, same_day_early).date() == dt.date(2026, 11, 3)


def test_release_is_exactly_the_offset_before_the_session(cfg):
    target = dt.datetime(2026, 11, 3, 7, 0, tzinfo=cfg.tz)
    assert release_moment(cfg, target) == target.astimezone(UTC) - dt.timedelta(hours=48)


def test_release_is_absolute_across_the_autumn_dst_change(cfg):
    """DST ended 2026-10-25 03:00 CEST. 48h before a 09:30 CET session is 10:30 CEST,
    not 09:30 -- getting this wrong means showing up an hour late to the race."""
    session = dt.datetime(2026, 10, 25, 9, 30, tzinfo=cfg.tz)
    assert session.utcoffset() == dt.timedelta(hours=1)  # CET
    release = release_moment(cfg, session)
    local = release.astimezone(cfg.tz)
    assert local == dt.datetime(2026, 10, 23, 10, 30, tzinfo=cfg.tz)
    assert local.utcoffset() == dt.timedelta(hours=2)  # CEST
    assert (session - release) == dt.timedelta(hours=48)


def test_release_is_absolute_across_the_spring_dst_change(cfg):
    """DST starts 2027-03-28 02:00 CET -> 03:00 CEST, so 48h before an 07:00 CEST session
    on the 29th is 06:00 CET on the 27th -- an hour earlier in wall-clock terms."""
    session = dt.datetime(2027, 3, 29, 7, 0, tzinfo=cfg.tz)
    assert session.utcoffset() == dt.timedelta(hours=2)  # CEST
    release = release_moment(cfg, session)
    local = release.astimezone(cfg.tz)
    assert local == dt.datetime(2027, 3, 27, 6, 0, tzinfo=cfg.tz)
    assert local.utcoffset() == dt.timedelta(hours=1)  # CET
    assert (session - release) == dt.timedelta(hours=48)


def test_local_target_uses_club_time(cfg):
    assert local_target(cfg, cfg.wishes[0], dt.date(2026, 11, 3)).tzinfo is cfg.tz


def test_plan_is_ordered_by_release_and_skips_blackouts(cfg):
    now = dt.datetime(2026, 11, 1, 12, 0, tzinfo=cfg.tz)
    strikes = plan(cfg, now, horizon_days=14)
    assert strikes == sorted(strikes, key=lambda s: s.release)
    assert all(not cfg.is_blacked_out(s.target.date()) for s in strikes)
    # Three wishes a week, two weeks.
    assert len(strikes) == 6

    christmas = dt.datetime(2026, 12, 22, 12, 0, tzinfo=cfg.tz)
    dates = {s.target.date() for s in plan(cfg, christmas, horizon_days=14)}
    assert not any(dt.date(2026, 12, 24) <= d <= dt.date(2027, 1, 2) for d in dates)


def test_next_strike_ignores_releases_that_have_passed(cfg):
    now = dt.datetime(2026, 11, 1, 12, 0, tzinfo=cfg.tz)  # Tue 03 Nov opened at 07:00
    upcoming = next_strike(cfg, now)
    assert upcoming.target == dt.datetime(2026, 11, 5, 19, 0, tzinfo=cfg.tz)


def test_next_strike_keeps_one_that_just_opened_within_the_grace_window(cfg):
    release = dt.datetime(2026, 11, 1, 7, 0, tzinfo=cfg.tz)
    just_after = release + dt.timedelta(seconds=30)
    upcoming = next_strike(cfg, just_after, grace=90)
    assert upcoming.target == dt.datetime(2026, 11, 3, 7, 0, tzinfo=cfg.tz)


def test_due_strikes_window(cfg):
    now = dt.datetime(2026, 11, 3, 18, 50, tzinfo=cfg.tz)  # Thu 19:00 opens Tue 19:00
    assert [s.wish.name for s in due_strikes(cfg, now, within=900)] == ["Thursday evening swim"]
    assert due_strikes(cfg, now, within=60) == []


# --------------------------------------------------------------------------- sleeping


def test_sleep_until_chunks_and_stops(cfg):
    clock = FakeClock(1000.0)
    sleep_until(1075.0, clock.time, clock.sleep, chunk=30.0)
    assert clock.time() == pytest.approx(1075.0)
    assert clock.slept == [30.0, 30.0, 15.0]


def test_sleep_until_in_the_past_returns_at_once(cfg):
    clock = FakeClock(1000.0)
    sleep_until(900.0, clock.time, clock.sleep)
    assert clock.slept == []


def test_spin_until_tightens_near_the_target(cfg):
    clock = FakeClock(1000.0)
    spin_until(1002.0, clock.time, clock.sleep)
    assert clock.time() >= 1002.0
    assert clock.slept[0] == pytest.approx(1.5)  # coarse first
    assert clock.slept[-1] <= 0.05  # then fine


# ------------------------------------------------------------------------ the strike


def make_strike(cfg, release_epoch: float) -> Strike:
    release = dt.datetime.fromtimestamp(release_epoch, tz=UTC)
    return Strike(cfg.wishes[0], release + cfg.release_offset, release)


@pytest.fixture
def quick_cfg(cfg):
    """Shorter waits so a strike runs in a handful of fake-clock steps."""
    return dataclasses.replace(
        cfg,
        strike=StrikeCfg(
            lead_seconds=1.0,
            window_seconds=10.0,
            attempt_interval_ms=250,
            max_attempts=10,
            prefetch_seconds=2.0,
            warmup_seconds=8.0,
            clocksync_seconds=5.0,
            join_waitlist_on_full=False,
        ),
    )


def test_books_on_the_first_attempt(quick_cfg, state, stub_client, pool_slot):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    slot = pool_slot("s1", start=strike.target)
    client = stub_client(slots=[slot], outcomes=[Outcome.BOOKED], clock=clock)

    result = run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)

    assert result.outcome is Outcome.BOOKED
    assert result.attempts == 1
    assert client.warmed
    # Fired one lead_second before the release, not after it.
    assert client.book_calls[0][1] == pytest.approx(1099.0, abs=0.05)
    assert state.already_booked("s1")
    assert state.bookings[0].wish == quick_cfg.wishes[0].name


def test_retries_while_full_and_wins_a_released_place(quick_cfg, state, stub_client, pool_slot):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)],
        outcomes=[Outcome.FULL, Outcome.FULL, Outcome.BOOKED],
        clock=clock,
    )
    result = run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert result.outcome is Outcome.BOOKED
    assert result.attempts == 3
    gaps = [b - a for (_, a), (_, b) in zip(client.book_calls, client.book_calls[1:])]
    assert all(g == pytest.approx(0.25, abs=0.01) for g in gaps)  # honours attempt_interval


def test_retries_while_not_yet_open(quick_cfg, state, stub_client, pool_slot):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)],
        outcomes=[Outcome.NOT_YET_OPEN, Outcome.BOOKED],
        clock=clock,
    )
    result = run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert result.outcome is Outcome.BOOKED and result.attempts == 2


def test_stops_dead_on_a_terminal_refusal(quick_cfg, state, stub_client, pool_slot):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)],
        outcomes=[Outcome.LIMIT_REACHED, Outcome.BOOKED],
        clock=clock,
    )
    result = run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert result.outcome is Outcome.LIMIT_REACHED
    assert result.attempts == 1  # did not keep hammering
    assert not state.bookings


def test_gives_up_at_max_attempts(quick_cfg, state, stub_client, pool_slot):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)],
        outcomes=[Outcome.FULL] * 50,
        clock=clock,
    )
    result = run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert result.outcome is Outcome.FULL
    assert result.attempts == quick_cfg.strike.max_attempts
    assert any("max_attempts" in line for line in result.log)


def test_gives_up_when_the_window_closes(quick_cfg, state, stub_client, pool_slot):
    slow = dataclasses.replace(
        quick_cfg, strike=dataclasses.replace(quick_cfg.strike, max_attempts=10_000)
    )
    clock = FakeClock(1000.0)
    strike = make_strike(slow, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)], outcomes=[Outcome.FULL] * 10_000, clock=clock
    )
    result = run_strike(client, slow, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert result.outcome is Outcome.FULL
    assert any("window closed" in line for line in result.log)
    assert clock.time() >= 1100.0 + slow.strike.window_seconds


def test_fires_against_the_server_clock(quick_cfg, state, stub_client, pool_slot):
    """Server is 3s ahead of us, so we must fire 3s earlier by our own clock."""
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)],
        outcomes=[Outcome.BOOKED],
        clock=clock,
        clock_offset=3.0,
    )
    run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert client.book_calls[0][1] == pytest.approx(1100.0 - 3.0 - 1.0, abs=0.05)


def test_waits_for_a_slot_that_is_published_only_at_release(quick_cfg, state, stub_client, pool_slot):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)],
        outcomes=[Outcome.BOOKED],
        slots_visible_from=1101.5,  # appears a second and a half after release
        clock=clock,
    )
    result = run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert result.outcome is Outcome.BOOKED
    assert client.book_calls[0][1] >= 1101.5


def test_reports_when_the_slot_never_appears(quick_cfg, state, stub_client):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    client = stub_client(slots=[], outcomes=[Outcome.BOOKED], clock=clock)
    result = run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert not result.success
    assert client.book_calls == []
    assert "no slot matching" in result.message


def test_respects_limits_already_in_state(quick_cfg, state, stub_client, pool_slot):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    slot = pool_slot("s1", start=strike.target)
    state.record_booking(Booking(slot_id="s1", start=strike.target))
    client = stub_client(slots=[slot], outcomes=[Outcome.BOOKED], clock=clock)
    result = run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert client.book_calls == []  # never asks for something we already hold
    assert not result.success


def test_falls_back_to_the_waiting_list(cfg, state, stub_client, pool_slot):
    waitlisting = dataclasses.replace(
        cfg,
        strike=StrikeCfg(
            lead_seconds=1.0, window_seconds=2.0, attempt_interval_ms=250, max_attempts=3,
            prefetch_seconds=2.0, warmup_seconds=8.0, clocksync_seconds=5.0,
            join_waitlist_on_full=True,
        ),
    )
    clock = FakeClock(1000.0)
    strike = make_strike(waitlisting, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)], outcomes=[Outcome.FULL] * 3, clock=clock
    )
    result = run_strike(client, waitlisting, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert result.outcome is Outcome.WAITLISTED
    assert client.waitlist_calls == ["s1"]
    assert state.bookings[0].outcome == "WAITLISTED"


def test_dry_run_sends_no_booking_and_writes_no_state(quick_cfg, state, stub_client, pool_slot, tmp_path):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)], outcomes=[Outcome.BOOKED], clock=clock
    )
    result = run_strike(
        client, quick_cfg, strike, state, dry_run=True, clock=clock.time, sleeper=clock.sleep
    )
    assert result.dry_run and result.outcome is Outcome.BOOKED
    assert state.bookings == []
    assert not state.path.exists()


def test_notes_when_the_api_disagrees_about_the_release_time(quick_cfg, state, stub_client, pool_slot):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    slot = pool_slot(
        "s1",
        start=strike.target,
        bookable_from=dt.datetime.fromtimestamp(1100.0 + 3600, tz=UTC),
    )
    client = stub_client(slots=[slot], outcomes=[Outcome.BOOKED], clock=clock)
    result = run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert any("bookable from" in line for line in result.log)


def test_attempt_is_always_logged_to_state(quick_cfg, state, stub_client, pool_slot):
    clock = FakeClock(1000.0)
    strike = make_strike(quick_cfg, 1100.0)
    client = stub_client(
        slots=[pool_slot("s1", start=strike.target)], outcomes=[Outcome.REJECTED], clock=clock
    )
    run_strike(client, quick_cfg, strike, state, clock=clock.time, sleeper=clock.sleep)
    assert state.attempts[-1]["outcome"] == "REJECTED"
    assert state.attempts[-1]["wish"] == quick_cfg.wishes[0].name


# ----------------------------------------------------------------------------- daemon


def test_daemon_runs_one_strike_and_notifies(quick_cfg, state, stub_client, pool_slot, monkeypatch):
    clock = FakeClock(dt.datetime(2026, 11, 1, 6, 59, 0, tzinfo=quick_cfg.tz).timestamp())
    target = dt.datetime(2026, 11, 3, 7, 0, tzinfo=quick_cfg.tz)
    client = stub_client(slots=[pool_slot("s1", start=target)], outcomes=[Outcome.BOOKED], clock=clock)
    sent = []
    results = run_daemon(
        client,
        quick_cfg,
        state,
        notifier=sent.append,
        clock=clock.time,
        sleeper=clock.sleep,
        max_strikes=1,
    )
    assert [r.outcome for r in results] == [Outcome.BOOKED]
    assert len(sent) == 1 and sent[0].success


def test_daemon_survives_a_strike_that_explodes(quick_cfg, state, stub_client, monkeypatch):
    clock = FakeClock(dt.datetime(2026, 11, 1, 6, 59, 0, tzinfo=quick_cfg.tz).timestamp())
    client = stub_client(clock=clock)

    def boom(*args, **kwargs):
        raise RuntimeError("API changed shape")

    monkeypatch.setattr("poolbot.scheduler.run_strike", boom)
    results = run_daemon(client, quick_cfg, state, clock=clock.time, sleeper=clock.sleep, max_strikes=1)
    assert results[0].outcome is Outcome.ERROR
    assert "API changed shape" in results[0].message


# ------------------------------------------------------------------------------ watch


def test_watch_measures_the_real_release_offset(cfg, stub_client, pool_slot):
    start = dt.datetime(2026, 11, 3, 7, 0, tzinfo=cfg.tz)
    clock = FakeClock((start - dt.timedelta(hours=48, seconds=120)).timestamp())
    appears_at = (start - dt.timedelta(hours=48)).timestamp()
    client = stub_client(
        slots=[pool_slot("s1", start=start)], slots_visible_from=appears_at, clock=clock
    )
    report = watch(
        client, cfg, "s1", date_from=start.date(), date_to=start.date(),
        interval=30.0, max_checks=20, clock=clock.time, sleeper=clock.sleep,
    )
    assert report["measured_offset_hours"] == pytest.approx(48.0, abs=0.02)
    assert report["configured_offset_hours"] == 48.0
