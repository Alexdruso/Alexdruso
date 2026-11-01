"""Local booking history: idempotency, weekly caps, and an audit trail."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

log = logging.getLogger("poolbot.state")

MAX_ATTEMPT_LOG = 200


@dataclass
class Booking:
    slot_id: str
    start: dt.datetime | None
    title: str = ""
    wish: str = ""
    booking_id: str | None = None
    outcome: str = "BOOKED"
    booked_at: dt.datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "start": self.start.isoformat() if self.start else None,
            "title": self.title,
            "wish": self.wish,
            "booking_id": self.booking_id,
            "outcome": self.outcome,
            "booked_at": (self.booked_at or dt.datetime.now(dt.timezone.utc)).isoformat(),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Booking":
        def _dt(value: Any) -> dt.datetime | None:
            if not value:
                return None
            try:
                return dt.datetime.fromisoformat(str(value))
            except ValueError:
                return None

        return cls(
            slot_id=str(data.get("slot_id", "")),
            start=_dt(data.get("start")),
            title=str(data.get("title") or ""),
            wish=str(data.get("wish") or ""),
            booking_id=data.get("booking_id"),
            outcome=str(data.get("outcome") or "BOOKED"),
            booked_at=_dt(data.get("booked_at")),
        )


@dataclass
class State:
    """Everything poolbot remembers between runs. Plain JSON, safe to delete."""

    path: Path
    tz: ZoneInfo
    bookings: list[Booking] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ io

    @classmethod
    def load(cls, path: str | Path, tz: ZoneInfo) -> "State":
        path = Path(path)
        if not path.exists():
            return cls(path=path, tz=tz)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); starting with empty state", path, exc)
            return cls(path=path, tz=tz)
        return cls(
            path=path,
            tz=tz,
            bookings=[Booking.from_json(b) for b in data.get("bookings", [])],
            attempts=list(data.get("attempts", [])),
        )

    def save(self) -> None:
        payload = {
            "bookings": [b.to_json() for b in self.bookings],
            "attempts": self.attempts[-MAX_ATTEMPT_LOG:],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover
            pass
        tmp.replace(self.path)

    # -------------------------------------------------------------- queries

    def _local_date(self, moment: dt.datetime | None) -> dt.date | None:
        if moment is None:
            return None
        return moment.astimezone(self.tz).date()

    def active_bookings(self) -> Iterator[Booking]:
        for booking in self.bookings:
            if booking.outcome in ("BOOKED", "ALREADY_BOOKED", "WAITLISTED"):
                yield booking

    def already_booked(self, slot_id: str) -> bool:
        return any(b.slot_id == str(slot_id) for b in self.active_bookings())

    def has_booking_on(self, day: dt.date) -> bool:
        return any(self._local_date(b.start) == day for b in self.active_bookings())

    def count_in_week_of(self, moment: dt.datetime) -> int:
        """How many slots we hold in the same ISO week as ``moment`` (local)."""
        target = moment.astimezone(self.tz).isocalendar()[:2]
        return sum(
            1
            for b in self.active_bookings()
            if b.start and b.start.astimezone(self.tz).isocalendar()[:2] == target
        )

    # -------------------------------------------------------------- writers

    def record_booking(self, booking: Booking) -> None:
        self.bookings = [b for b in self.bookings if b.slot_id != booking.slot_id]
        self.bookings.append(booking)
        self.bookings.sort(key=lambda b: (b.start or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))

    def drop_booking(self, slot_id: str) -> bool:
        before = len(self.bookings)
        self.bookings = [b for b in self.bookings if b.slot_id != str(slot_id)]
        return len(self.bookings) != before

    def record_attempt(self, **fields: Any) -> None:
        entry = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), **fields}
        self.attempts.append(entry)
        del self.attempts[:-MAX_ATTEMPT_LOG]

    def forget_past(self, before: dt.datetime | None = None) -> int:
        """Drop bookings for sessions that have already happened."""
        cutoff = before or dt.datetime.now(dt.timezone.utc)
        keep = [b for b in self.bookings if not b.start or b.start >= cutoff]
        dropped = len(self.bookings) - len(keep)
        self.bookings = keep
        return dropped
