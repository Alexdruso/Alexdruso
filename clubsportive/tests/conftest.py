"""Shared fixtures: a fake HTTP session and a stub client, so no test touches the network."""

from __future__ import annotations

import datetime as dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poolbot.client import BookAttempt, ClubSportiveClient, Outcome, Slot  # noqa: E402
from poolbot.config import Endpoints, load_config  # noqa: E402
from poolbot.state import State  # noqa: E402

EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.yaml"


# ------------------------------------------------------------------ fake HTTP plumbing


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: Any = None
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.payload is not None and not self.text:
            self.text = json.dumps(self.payload)

    def json(self) -> Any:
        if self.payload is None:
            raise ValueError("no json")
        return self.payload


@dataclass
class Call:
    method: str
    url: str
    headers: Mapping[str, str]
    json: Any
    auth: Any


class FakeSession:
    """Stands in for requests.Session; `handler(call)` decides what comes back."""

    def __init__(self, handler: Callable[[Call], FakeResponse]) -> None:
        self.handler = handler
        self.calls: list[Call] = []

    def request(self, method, url, headers=None, json=None, auth=None, timeout=None):
        call = Call(method.upper(), url, dict(headers or {}), json, auth)
        self.calls.append(call)
        return self.handler(call)


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    """load_env() writes to os.environ by design, so keep tests from leaking into each other."""
    for name in (
        "CS_USERNAME", "CS_PASSWORD",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "SMTP_TO",
        "SMTP_STARTTLS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def endpoints() -> Endpoints:
    from poolbot.config import DEFAULT_FIELDS

    return Endpoints(
        base_url="https://api.example.test",
        facility_group="BRANDEDAPPTEST-1",
        client_type="APP_V5",
        client_version="9.9.9",
        paths={
            "login": "/v1/login",
            "me": "/v1/me",
            "slots": "/v1/classes?from={date_from}&to={date_to}",
            "book": "/v1/classes/{slot_id}/booking",
            "cancel": "/v1/bookings/{booking_id}/cancel",
            "waitlist": "/v1/classes/{slot_id}/waitlist",
        },
        methods={"book": "POST", "cancel": "POST", "slots": "GET"},
        bodies={},
        fields=DEFAULT_FIELDS,
    )


@pytest.fixture
def make_client(endpoints, tmp_path):
    def factory(handler: Callable[[Call], FakeResponse], **kwargs) -> ClubSportiveClient:
        session = FakeSession(handler)
        client = ClubSportiveClient(
            endpoints,
            "swimmer@example.test",
            "s3cret",
            session=session,
            token_path=tmp_path / "token.json",
            **kwargs,
        )
        client.fake = session  # type: ignore[attr-defined]
        return client

    return factory


@pytest.fixture
def cfg():
    return load_config(EXAMPLE_CONFIG)


@pytest.fixture
def state(tmp_path, cfg) -> State:
    return State(path=tmp_path / "state.json", tz=cfg.tz)


# ------------------------------------------------------------------------- stub client


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self, start: float) -> None:
        self.t = float(start)
        self.slept: list[float] = []

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += max(float(seconds), 0.0)


class StubClient:
    """Implements just enough of ClubSportiveClient for the strike loop."""

    def __init__(
        self,
        endpoints: Endpoints,
        *,
        slots: list[Slot] | None = None,
        outcomes: list[Outcome] | None = None,
        slots_visible_from: float | None = None,
        clock: FakeClock | None = None,
        clock_offset: float = 0.0,
    ) -> None:
        self.ep = endpoints
        self._slots = slots or []
        self._outcomes = list(outcomes or [Outcome.BOOKED])
        self._slots_visible_from = slots_visible_from
        self._clock = clock
        self.clock_offset = clock_offset
        self.book_calls: list[tuple[str, float]] = []
        self.waitlist_calls: list[str] = []
        self.warmed = False

    def warm_up(self) -> bool:
        self.warmed = True
        return True

    def sample_server_clock(self, samples: int = 3) -> float:
        return self.clock_offset

    def slots(self, date_from, date_to):
        if self._slots_visible_from is not None and self._clock is not None:
            if self._clock.time() < self._slots_visible_from:
                return []
        return list(self._slots)

    def find_slot(self, slot_id, date_from, date_to):
        return next((s for s in self.slots(date_from, date_to) if s.id == str(slot_id)), None)

    def book(self, slot_id, *, dry_run: bool = False) -> BookAttempt:
        now = self._clock.time() if self._clock else 0.0
        self.book_calls.append((str(slot_id), now))
        outcome = self._outcomes.pop(0) if self._outcomes else Outcome.FULL
        return BookAttempt(outcome, status=200 if outcome.success else 409, message=outcome.value)

    def join_waitlist(self, slot_id, *, dry_run: bool = False) -> BookAttempt:
        self.waitlist_calls.append(str(slot_id))
        return BookAttempt(Outcome.WAITLISTED, status=200, message="on the list")


@pytest.fixture
def stub_client(endpoints):
    def factory(**kwargs) -> StubClient:
        return StubClient(endpoints, **kwargs)

    return factory


_UNSET = object()


@pytest.fixture
def pool_slot(cfg):
    """Build a pool Slot; pass start=None explicitly for a slot with no start time."""

    def factory(slot_id: str = "s1", start: Any = _UNSET, title: str = "Lap Swim", **kwargs) -> Slot:
        if start is _UNSET:
            start = dt.datetime(2026, 11, 3, 7, 0, tzinfo=cfg.tz)
        kwargs.setdefault("location", "Clubsportive Zuidas")
        return Slot(id=slot_id, title=title, start=start, **kwargs)

    return factory
