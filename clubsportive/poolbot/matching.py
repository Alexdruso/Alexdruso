"""Turn a wish ("Tuesday 07:00, give or take half an hour") into a concrete slot."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

from .client import Slot
from .config import Config, Wish
from .state import State

log = logging.getLogger("poolbot.matching")


@dataclass(frozen=True)
class Candidate:
    slot: Slot
    delta_minutes: float  # signed distance from the wished time

    @property
    def distance(self) -> float:
        return abs(self.delta_minutes)


def pool_slots(cfg: Config, slots: Iterable[Slot]) -> list[Slot]:
    """Keep only the slots that look like the pool sessions the user cares about."""
    out = []
    for slot in slots:
        if not cfg.match.matches_activity(slot.title, slot.room):
            continue
        if not cfg.match.matches_location(slot.location, slot.room, slot.title):
            continue
        out.append(slot)
    return out


def candidates_for(
    cfg: Config, wish: Wish, target: dt.datetime, slots: Iterable[Slot]
) -> list[Candidate]:
    """Slots on the target day within the wish's tolerance, closest-to-wished-time first."""
    target_local = target.astimezone(cfg.tz)
    found: list[Candidate] = []
    for slot in pool_slots(cfg, slots):
        if slot.start is None:
            continue
        local = slot.start.astimezone(cfg.tz)
        if local.date() != target_local.date():
            continue
        delta = (local - target_local).total_seconds() / 60.0
        if abs(delta) > wish.tolerance_minutes + 1e-9:
            continue
        found.append(Candidate(slot, delta))
    found.sort(key=lambda c: (c.distance, c.slot.start))
    return found


def blocked_reason(cfg: Config, state: State, slot: Slot, target: dt.datetime) -> str | None:
    """Why we must not book this slot, or None if it is fair game."""
    when = (slot.start or target).astimezone(cfg.tz)
    if cfg.is_blacked_out(when.date()):
        return f"{when:%Y-%m-%d} is in a blackout period"
    if state.already_booked(slot.id):
        return "already booked according to local state"
    if cfg.limits.one_per_day and state.has_booking_on(when.date()):
        return f"already hold a slot on {when:%Y-%m-%d} (limits.one_per_day)"
    cap = cfg.limits.max_bookings_per_week
    if cap is not None:
        held = state.count_in_week_of(when)
        if held >= cap:
            return f"weekly cap reached ({held}/{cap} in week of {when:%Y-%m-%d})"
    return None


def choose(
    cfg: Config, wish: Wish, target: dt.datetime, slots: Sequence[Slot], state: State
) -> tuple[Slot | None, str]:
    """Pick the best bookable slot for a wish, or explain why nothing fits."""
    candidates = candidates_for(cfg, wish, target, slots)
    if not candidates:
        return None, (
            f"no slot matching {wish} within +/-{wish.tolerance_minutes} min "
            f"on {target.astimezone(cfg.tz):%Y-%m-%d}"
        )
    skipped: list[str] = []
    for candidate in candidates:
        reason = blocked_reason(cfg, state, candidate.slot, target)
        if reason is None:
            return candidate.slot, (
                f"{candidate.slot.describe(cfg.tz)} ({candidate.delta_minutes:+.0f} min from wish)"
            )
        skipped.append(f"{candidate.slot.describe(cfg.tz)}: {reason}")
    return None, "every candidate is blocked -- " + "; ".join(skipped)
