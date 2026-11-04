"""Release-time arithmetic and the strike loop.

Slots are released ``release_offset_hours`` before the session starts. Two details decide
whether we win the race:

* the release instant is computed in absolute (UTC) terms, so a Europe/Amsterdam DST change
  between the booking moment and the session does not shift the strike by an hour;
* we fire against the *server's* clock, measured from its ``Date`` header, not the local one.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from .client import ApiError, BookAttempt, ClubSportiveClient, Outcome, Slot
from .config import Config, Wish
from .matching import choose
from .state import Booking, State

log = logging.getLogger("poolbot.scheduler")

Clock = Callable[[], float]
Sleeper = Callable[[float], None]

# Never sleep longer than this in one go, so a laptop suspend / NTP step cannot overshoot.
SLEEP_CHUNK = 30.0
# How often to re-poll the schedule while waiting for the slot to be published.
RESOLVE_INTERVAL = 1.0


# ------------------------------------------------------------------- release arithmetic


def local_target(cfg: Config, wish: Wish, day: dt.date) -> dt.datetime:
    """The wished wall-clock moment on a given day, in the club's timezone."""
    return dt.datetime.combine(day, wish.start, tzinfo=cfg.tz)


def next_target(cfg: Config, wish: Wish, after: dt.datetime) -> dt.datetime:
    """The next occurrence of a wish strictly after ``after``."""
    after_local = after.astimezone(cfg.tz)
    day = after_local.date()
    for _ in range(15):
        if day.weekday() == wish.weekday:
            moment = local_target(cfg, wish, day)
            if moment > after_local:
                return moment
        day += dt.timedelta(days=1)
    raise RuntimeError(f"could not find a next occurrence for {wish}")  # pragma: no cover


def release_moment(cfg: Config, target: dt.datetime) -> dt.datetime:
    """When booking opens for a session starting at ``target`` (absolute, UTC-based)."""
    return target.astimezone(dt.timezone.utc) - cfg.release_offset


@dataclass(frozen=True)
class Strike:
    """One scheduled attempt: a wish, the session it aims at, and when to pounce."""

    wish: Wish
    target: dt.datetime
    release: dt.datetime

    @property
    def release_epoch(self) -> float:
        return self.release.timestamp()

    def describe(self, cfg: Config) -> str:
        return (
            f"{self.wish.name}: session {self.target.astimezone(cfg.tz):%a %d %b %H:%M} "
            f"-- opens {self.release.astimezone(cfg.tz):%a %d %b %H:%M:%S %Z}"
        )


def plan(cfg: Config, now: dt.datetime | None = None, horizon_days: int = 14) -> list[Strike]:
    """Every strike due in the next ``horizon_days``, soonest release first."""
    now = now or cfg.now()
    horizon = now + dt.timedelta(days=horizon_days)
    strikes: list[Strike] = []
    for wish in cfg.wishes:
        cursor = now
        for _ in range(horizon_days + 1):
            target = next_target(cfg, wish, cursor)
            if target > horizon:
                break
            cursor = target
            if cfg.is_blacked_out(target.date()):
                continue
            strikes.append(Strike(wish, target, release_moment(cfg, target)))
    strikes.sort(key=lambda s: (s.release, s.wish.priority))
    return strikes


def next_strike(cfg: Config, now: dt.datetime | None = None, grace: float = 0.0) -> Strike | None:
    """The soonest strike whose release has not yet passed (plus a grace window)."""
    now = now or cfg.now()
    cutoff = now - dt.timedelta(seconds=grace)
    for strike in plan(cfg, now - dt.timedelta(days=1)):
        if strike.release >= cutoff and strike.target > now:
            return strike
    return None


def due_strikes(cfg: Config, now: dt.datetime | None = None, within: float = 900.0) -> list[Strike]:
    """Strikes whose release is within ``within`` seconds (for cron-style one-shot runs)."""
    now = now or cfg.now()
    out = []
    for strike in plan(cfg, now - dt.timedelta(days=1)):
        delta = (strike.release - now).total_seconds()
        if -cfg.strike.window_seconds <= delta <= within and strike.target > now:
            out.append(strike)
    return out


# ------------------------------------------------------------------------------ sleeping


def sleep_until(
    epoch: float, clock: Clock = time.time, sleeper: Sleeper = time.sleep, chunk: float = SLEEP_CHUNK
) -> None:
    """Sleep until a wall-clock epoch, in chunks, re-reading the clock each time."""
    while True:
        remaining = epoch - clock()
        if remaining <= 0:
            return
        sleeper(min(remaining, chunk))


def spin_until(epoch: float, clock: Clock = time.time, sleeper: Sleeper = time.sleep) -> None:
    """Like :func:`sleep_until` but tight at the end, for sub-second accuracy."""
    while True:
        remaining = epoch - clock()
        if remaining <= 0:
            return
        sleeper(min(remaining, 0.05) if remaining < 1.0 else min(remaining - 0.5, SLEEP_CHUNK))


# -------------------------------------------------------------------------- the strike


@dataclass
class StrikeResult:
    strike: Strike
    outcome: Outcome
    slot: Slot | None = None
    attempts: int = 0
    message: str = ""
    first_attempt_at: dt.datetime | None = None
    booked_at: dt.datetime | None = None
    dry_run: bool = False
    log: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.outcome.success

    def summary(self, cfg: Config) -> str:
        head = f"{self.outcome.value} -- {self.strike.wish.name}"
        if self.slot is not None:
            head += f" -- {self.slot.describe(cfg.tz)}"
        else:
            head += f" -- session {self.strike.target.astimezone(cfg.tz):%a %d %b %H:%M}"
        if self.attempts:
            head += f" after {self.attempts} attempt(s)"
        if self.message:
            head += f": {self.message}"
        return head


def _window_dates(cfg: Config, target: dt.datetime) -> tuple[dt.date, dt.date]:
    day = target.astimezone(cfg.tz).date()
    return day, day


def run_strike(
    client: ClubSportiveClient,
    cfg: Config,
    strike: Strike,
    state: State,
    *,
    dry_run: bool = False,
    clock: Clock = time.time,
    sleeper: Sleeper = time.sleep,
    slot: Slot | None = None,
) -> StrikeResult:
    """Wait for the release moment and book the best matching slot.

    Timeline, all relative to the release instant R (server clock):
    ``R-warmup`` log in and open the connection, ``R-clocksync`` measure the clock offset,
    ``R-prefetch`` start resolving the slot id, ``R-lead`` start firing.
    """
    s = cfg.strike
    result = StrikeResult(strike=strike, outcome=Outcome.ERROR, dry_run=dry_run, slot=slot)

    def note(msg: str, *args: object) -> None:
        text = msg % args if args else msg
        log.info(text)
        result.log.append(text)

    note("target %s", strike.describe(cfg))

    # 1. Warm up: log in and pay the TCP/TLS cost early.
    sleep_until(strike.release_epoch - s.warmup_seconds, clock, sleeper)
    if not client.warm_up():
        note("warm-up failed; carrying on and hoping the token is still good")

    # 2. Measure the server clock, then restate the release moment in local-clock terms.
    sleep_until(strike.release_epoch - s.clocksync_seconds, clock, sleeper)
    offset = client.sample_server_clock()
    note("server clock offset %+.3fs", offset)

    def local_epoch(server_epoch: float) -> float:
        """Convert a server-clock epoch to the local-clock epoch that coincides with it."""
        return server_epoch - offset

    release_local = local_epoch(strike.release_epoch)
    fire_at = release_local - s.lead_seconds
    deadline = release_local + s.window_seconds

    # 3. Resolve the slot id. Before release the session may not be listed at all, so this
    #    runs in the same loop as the booking attempts.
    date_from, date_to = _window_dates(cfg, strike.target)
    sleep_until(release_local - s.prefetch_seconds, clock, sleeper)

    def resolve() -> Slot | None:
        try:
            slots = client.slots(date_from, date_to)
        except ApiError as exc:
            log.debug("slot lookup failed: %s", exc)
            return None
        picked, why = choose(cfg, strike.wish, strike.target, slots, state)
        if picked is None:
            log.debug("no slot yet: %s", why)
            result.message = why
            return None
        note("slot resolved: %s", why)
        if picked.bookable_from is not None:
            actual = picked.bookable_from.timestamp()
            drift = actual - strike.release_epoch
            if abs(drift) > 2:
                note(
                    "API says bookable from %s (%+.0fs vs configured offset) -- trusting the API",
                    picked.bookable_from.astimezone(cfg.tz).strftime("%a %d %b %H:%M:%S"),
                    drift,
                )
            return picked
        return picked

    if result.slot is None:
        result.slot = resolve()

    # 4. Fire.
    spin_until(fire_at, clock, sleeper)
    attempt: BookAttempt | None = None
    last_resolve = 0.0
    while True:
        now = clock()
        if now > deadline:
            note("window closed after %.0fs", s.window_seconds)
            break
        if result.attempts >= s.max_attempts:
            note("hit max_attempts (%d)", s.max_attempts)
            break

        if result.slot is None:
            if now - last_resolve >= RESOLVE_INTERVAL:
                last_resolve = now
                result.slot = resolve()
            if result.slot is None:
                sleeper(min(RESOLVE_INTERVAL, max(deadline - clock(), 0.05)))
                continue

        result.attempts += 1
        if result.first_attempt_at is None:
            result.first_attempt_at = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
        attempt = client.book(result.slot.id, dry_run=dry_run)
        log.info("attempt %d -> %s", result.attempts, attempt)
        result.outcome = attempt.outcome
        result.message = attempt.message

        if attempt.outcome.success:
            result.booked_at = dt.datetime.now(dt.timezone.utc)
            note("booked on attempt %d (%.0fms)", result.attempts, attempt.elapsed * 1000)
            break
        if not attempt.outcome.retryable:
            note("giving up: %s", attempt)
            break
        sleeper(max(s.attempt_interval - attempt.elapsed, 0.0))

    # 5. Waiting list as a consolation prize.
    if (
        not result.outcome.success
        and result.slot is not None
        and result.outcome is Outcome.FULL
        and s.join_waitlist_on_full
        and client.ep.has_path("waitlist")
    ):
        wl = client.join_waitlist(result.slot.id, dry_run=dry_run)
        note("waiting list: %s", wl)
        if wl.outcome.success:
            result.outcome = Outcome.WAITLISTED
            result.message = wl.message

    # 6. Remember what happened.
    if result.success and result.slot is not None and not dry_run:
        state.record_booking(
            Booking(
                slot_id=result.slot.id,
                start=result.slot.start,
                title=result.slot.title,
                wish=strike.wish.name,
                booking_id=(attempt.booking_id if attempt else None),
                outcome=result.outcome.value,
                booked_at=result.booked_at,
            )
        )
    state.record_attempt(
        wish=strike.wish.name,
        target=strike.target.isoformat(),
        release=strike.release.isoformat(),
        slot_id=(result.slot.id if result.slot else None),
        outcome=result.outcome.value,
        attempts=result.attempts,
        message=result.message[:300],
        dry_run=dry_run,
    )
    if not dry_run:
        state.save()
    return result


def run_daemon(
    client: ClubSportiveClient,
    cfg: Config,
    state: State,
    *,
    notifier: Callable[[StrikeResult], None] | None = None,
    dry_run: bool = False,
    clock: Clock = time.time,
    sleeper: Sleeper = time.sleep,
    max_strikes: int | None = None,
    reload_config: Callable[[], Config] | None = None,
) -> list[StrikeResult]:
    """Sleep until the next release, strike, repeat. One long-lived process."""
    results: list[StrikeResult] = []
    while max_strikes is None or len(results) < max_strikes:
        if reload_config is not None:
            try:
                cfg = reload_config()
            except Exception as exc:  # pragma: no cover - keep the daemon alive
                log.warning("config reload failed, keeping the previous one: %s", exc)
        now = dt.datetime.fromtimestamp(clock(), tz=dt.timezone.utc)
        state.forget_past(now)
        strike = next_strike(cfg, now, grace=cfg.strike.window_seconds)
        if strike is None:
            log.info("nothing to book in the planning horizon; sleeping an hour")
            sleeper(3600)
            continue
        log.info("next up -- %s", strike.describe(cfg))
        try:
            result = run_strike(
                client, cfg, strike, state, dry_run=dry_run, clock=clock, sleeper=sleeper
            )
        except Exception as exc:  # pragma: no cover - a daemon must not die on one failure
            log.exception("strike failed: %s", exc)
            result = StrikeResult(
                strike=strike, outcome=Outcome.ERROR, message=str(exc), dry_run=dry_run
            )
        results.append(result)
        if notifier is not None:
            try:
                notifier(result)
            except Exception as exc:  # pragma: no cover
                log.warning("notification failed: %s", exc)
        # Step past this strike so the next loop does not pick it up again.
        sleeper(max(cfg.strike.window_seconds, 5.0))
    return results


def watch(
    client: ClubSportiveClient,
    cfg: Config,
    slot_id: str,
    *,
    date_from: dt.date,
    date_to: dt.date,
    interval: float = 60.0,
    max_checks: int = 2880,
    clock: Clock = time.time,
    sleeper: Sleeper = time.sleep,
    on_sample: Callable[[dt.datetime, Slot | None], None] | None = None,
) -> dict[str, object]:
    """Poll one slot until it becomes bookable, to measure the real release offset.

    Run this before trusting ``release_offset_hours``: it reports the measured gap between
    the moment the slot flips to bookable and the session start.
    """
    first_seen: dt.datetime | None = None
    became_bookable: dt.datetime | None = None
    slot: Slot | None = None
    for _ in range(max_checks):
        now = dt.datetime.fromtimestamp(clock(), tz=dt.timezone.utc)
        try:
            slot = client.find_slot(slot_id, date_from, date_to)
        except ApiError as exc:
            log.warning("lookup failed: %s", exc)
            slot = None
        if on_sample is not None:
            on_sample(now, slot)
        if slot is not None:
            if first_seen is None:
                first_seen = now
                log.info("slot appeared in the schedule at %s", now.astimezone(cfg.tz))
            bookable = slot.bookable_from is None or slot.bookable_from <= now
            free = slot.free is None or slot.free > 0
            if bookable and free:
                became_bookable = now
                log.info("slot is bookable at %s", now.astimezone(cfg.tz))
                break
        sleeper(interval)
    report: dict[str, object] = {
        "slot_id": slot_id,
        "first_seen": first_seen.isoformat() if first_seen else None,
        "bookable_at": became_bookable.isoformat() if became_bookable else None,
        "start": slot.start.isoformat() if slot and slot.start else None,
    }
    if became_bookable and slot and slot.start:
        measured = (slot.start - became_bookable).total_seconds() / 3600.0
        report["measured_offset_hours"] = round(measured, 3)
        report["configured_offset_hours"] = cfg.release_offset.total_seconds() / 3600.0
    return report
