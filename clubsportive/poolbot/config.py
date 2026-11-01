"""Configuration: the YAML preference file, the endpoints.toml API description, and .env."""

from __future__ import annotations

import datetime as dt
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

PLACEHOLDER = "REPLACE-ME"

_WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class ConfigError(Exception):
    """Raised for anything wrong in config.yaml / endpoints.toml / .env."""


# --------------------------------------------------------------------------- preferences


@dataclass(frozen=True)
class Wish:
    """One recurring weekly slot the user wants."""

    name: str
    weekday: int  # 0 = Monday
    start: dt.time
    tolerance_minutes: int = 0
    priority: int = 100

    @property
    def weekday_name(self) -> str:
        return WEEKDAY_NAMES[self.weekday]

    def __str__(self) -> str:
        return f"{self.name} ({self.weekday_name} {self.start:%H:%M})"


@dataclass(frozen=True)
class MatchRules:
    activity_patterns: tuple[re.Pattern[str], ...] = ()
    location: str | None = None

    def matches_activity(self, *texts: str | None) -> bool:
        if not self.activity_patterns:
            return True
        haystack = " ".join(t for t in texts if t)
        return any(p.search(haystack) for p in self.activity_patterns)

    def matches_location(self, *texts: str | None) -> bool:
        if not self.location:
            return True
        haystack = " ".join(t for t in texts if t).casefold()
        return self.location.casefold() in haystack


@dataclass(frozen=True)
class Limits:
    max_bookings_per_week: int | None = None
    one_per_day: bool = False


@dataclass(frozen=True)
class StrikeCfg:
    lead_seconds: float = 2.0
    window_seconds: float = 90.0
    attempt_interval_ms: int = 250
    max_attempts: int = 60
    prefetch_seconds: float = 5.0
    warmup_seconds: float = 60.0
    clocksync_seconds: float = 45.0
    join_waitlist_on_full: bool = True

    @property
    def attempt_interval(self) -> float:
        return self.attempt_interval_ms / 1000.0


@dataclass(frozen=True)
class NotifyCfg:
    on_success: bool = True
    on_failure: bool = True
    attach_ics: bool = True


@dataclass(frozen=True)
class Config:
    tz: ZoneInfo
    release_offset: dt.timedelta
    match: MatchRules
    wishes: tuple[Wish, ...]
    limits: Limits = Limits()
    strike: StrikeCfg = StrikeCfg()
    blackouts: tuple[tuple[dt.date, dt.date], ...] = ()
    notify: NotifyCfg = NotifyCfg()

    def wish(self, name: str) -> Wish:
        for w in self.wishes:
            if w.name.casefold() == name.casefold():
                return w
        known = ", ".join(repr(w.name) for w in self.wishes)
        raise ConfigError(f"no wish named {name!r}; configured wishes: {known}")

    def is_blacked_out(self, day: dt.date) -> bool:
        return any(lo <= day <= hi for lo, hi in self.blackouts)

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=self.tz)


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a mapping, got {type(value).__name__}")
    return value


def _parse_time(value: Any, where: str) -> dt.time:
    if isinstance(value, dt.time):
        return value
    if isinstance(value, dt.datetime):
        return value.timetz()
    text = str(value).strip()
    for fmt in ("%H:%M", "%H:%M:%S", "%H.%M"):
        try:
            return dt.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ConfigError(f"{where}: cannot read {value!r} as a HH:MM time")


def _parse_weekday(value: Any, where: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{where}: {value!r} is not a weekday")
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ConfigError(f"{where}: weekday {value} out of range 0-6")
    key = str(value).strip().casefold()
    if key in _WEEKDAYS:
        return _WEEKDAYS[key]
    raise ConfigError(f"{where}: {value!r} is not a weekday (use mon..sun)")


def _parse_date(text: str, where: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text.strip())
    except ValueError as exc:
        raise ConfigError(f"{where}: {text!r} is not a YYYY-MM-DD date") from exc


def _parse_blackouts(raw: Any) -> tuple[tuple[dt.date, dt.date], ...]:
    if not raw:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ConfigError("blackouts must be a list")
    out: list[tuple[dt.date, dt.date]] = []
    for i, item in enumerate(raw):
        where = f"blackouts[{i}]"
        if isinstance(item, dt.date):
            out.append((item, item))
            continue
        text = str(item).strip()
        if ".." in text:
            lo_s, _, hi_s = text.partition("..")
            lo, hi = _parse_date(lo_s, where), _parse_date(hi_s, where)
            if hi < lo:
                raise ConfigError(f"{where}: range ends before it starts")
            out.append((lo, hi))
        else:
            day = _parse_date(text, where)
            out.append((day, day))
    return tuple(out)


def _compile_patterns(raw: Any) -> tuple[re.Pattern[str], ...]:
    if not raw:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    pats = []
    for i, item in enumerate(raw):
        try:
            pats.append(re.compile(str(item), re.IGNORECASE))
        except re.error as exc:
            raise ConfigError(f"match.activity_patterns[{i}]: bad regex {item!r}: {exc}") from exc
    return tuple(pats)


def load_config(path: str | Path) -> Config:
    """Read and validate the YAML preference file."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"{path} not found -- copy config.example.yaml to {path.name} and edit it"
        )
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    raw = _require_mapping(raw, str(path))

    tzname = str(raw.get("timezone") or "Europe/Amsterdam")
    try:
        tz = ZoneInfo(tzname)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"unknown timezone {tzname!r}") from exc

    hours = raw.get("release_offset_hours", 48)
    try:
        offset = dt.timedelta(hours=float(hours))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"release_offset_hours: {hours!r} is not a number") from exc
    if offset <= dt.timedelta(0):
        raise ConfigError("release_offset_hours must be positive")

    m = _require_mapping(raw.get("match"), "match")
    match = MatchRules(
        activity_patterns=_compile_patterns(m.get("activity_patterns")),
        location=(str(m["location"]).strip() or None) if m.get("location") else None,
    )

    wishes_raw = raw.get("wishes")
    if not wishes_raw:
        raise ConfigError("no wishes configured -- there is nothing to book")
    if not isinstance(wishes_raw, Sequence) or isinstance(wishes_raw, (str, bytes)):
        raise ConfigError("wishes must be a list")
    wishes: list[Wish] = []
    seen: set[str] = set()
    for i, item in enumerate(wishes_raw):
        where = f"wishes[{i}]"
        w = _require_mapping(item, where)
        for key in ("weekday", "start"):
            if key not in w:
                raise ConfigError(f"{where}: missing required key {key!r}")
        name = str(w.get("name") or f"wish-{i + 1}").strip()
        if name.casefold() in seen:
            raise ConfigError(f"{where}: duplicate wish name {name!r}")
        seen.add(name.casefold())
        tol = w.get("tolerance_minutes", 0)
        try:
            tol = int(tol)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{where}.tolerance_minutes: {tol!r} is not a number") from exc
        if tol < 0:
            raise ConfigError(f"{where}.tolerance_minutes must be >= 0")
        wishes.append(
            Wish(
                name=name,
                weekday=_parse_weekday(w["weekday"], f"{where}.weekday"),
                start=_parse_time(w["start"], f"{where}.start"),
                tolerance_minutes=tol,
                priority=int(w.get("priority", i + 1)),
            )
        )

    lim = _require_mapping(raw.get("limits"), "limits")
    max_week = lim.get("max_bookings_per_week")
    limits = Limits(
        max_bookings_per_week=int(max_week) if max_week not in (None, "", False) else None,
        one_per_day=bool(lim.get("one_per_day", False)),
    )

    s = _require_mapping(raw.get("strike"), "strike")
    defaults = StrikeCfg()
    strike = StrikeCfg(
        lead_seconds=float(s.get("lead_seconds", defaults.lead_seconds)),
        window_seconds=float(s.get("window_seconds", defaults.window_seconds)),
        attempt_interval_ms=int(s.get("attempt_interval_ms", defaults.attempt_interval_ms)),
        max_attempts=int(s.get("max_attempts", defaults.max_attempts)),
        prefetch_seconds=float(s.get("prefetch_seconds", defaults.prefetch_seconds)),
        warmup_seconds=float(s.get("warmup_seconds", defaults.warmup_seconds)),
        clocksync_seconds=float(s.get("clocksync_seconds", defaults.clocksync_seconds)),
        join_waitlist_on_full=bool(s.get("join_waitlist_on_full", defaults.join_waitlist_on_full)),
    )
    if strike.attempt_interval_ms < 50:
        raise ConfigError("strike.attempt_interval_ms below 50 would hammer the API; raise it")
    if strike.max_attempts < 1:
        raise ConfigError("strike.max_attempts must be >= 1")

    n = _require_mapping(raw.get("notify"), "notify")
    notify = NotifyCfg(
        on_success=bool(n.get("on_success", True)),
        on_failure=bool(n.get("on_failure", True)),
        attach_ics=bool(n.get("attach_ics", True)),
    )

    return Config(
        tz=tz,
        release_offset=offset,
        match=match,
        wishes=tuple(wishes),
        limits=limits,
        strike=strike,
        blackouts=_parse_blackouts(raw.get("blackouts")),
        notify=notify,
    )


# ------------------------------------------------------------------------------ endpoints


@dataclass(frozen=True)
class Endpoints:
    """Everything we know about the app's HTTP API, loaded from endpoints.toml."""

    base_url: str
    facility_group: str
    client_type: str = "APP_V5"
    client_version: str = "3.68.0"
    user_agent: str = "okhttp/4.12.0"
    paths: Mapping[str, str] = field(default_factory=dict)
    methods: Mapping[str, str] = field(default_factory=dict)
    bodies: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    fields: Mapping[str, Sequence[str]] = field(default_factory=dict)

    def path(self, name: str) -> str:
        value = str(self.paths.get(name) or "").strip()
        if not value or PLACEHOLDER in value:
            raise ConfigError(
                f"endpoints.toml has no usable [paths].{name}; run "
                f"`python -m poolbot.extract_apk <apk>` or fill it in by hand"
            )
        return value

    def has_path(self, name: str) -> bool:
        value = str(self.paths.get(name) or "").strip()
        return bool(value) and PLACEHOLDER not in value

    def method(self, name: str, default: str = "GET") -> str:
        return str(self.methods.get(name) or default).upper()

    def body(self, name: str) -> Mapping[str, Any]:
        return self.bodies.get(name) or {}

    def field_names(self, name: str) -> tuple[str, ...]:
        return tuple(self.fields.get(name) or ())

    def headers(self) -> dict[str, str]:
        return {
            "x-public-facility-group": self.facility_group,
            "x-nox-client-type": self.client_type,
            "x-nox-client-version": self.client_version,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }


DEFAULT_FIELDS: dict[str, tuple[str, ...]] = {
    "slots_root": ("slots", "classes", "items", "content", "data", "results"),
    "id": ("id", "slotId", "classId", "uuid", "appointmentId"),
    "title": ("name", "title", "activityName", "className", "description"),
    "start": ("startDateTime", "startDate", "start", "startTime", "from", "dateTimeFrom"),
    "end": ("endDateTime", "endDate", "end", "endTime", "to", "dateTimeTo"),
    "location": ("facilityName", "studioName", "locationName", "facility", "location", "studio"),
    "room": ("roomName", "room", "areaName", "area"),
    "capacity": ("capacity", "maxParticipants", "maxCapacity", "places"),
    "booked": ("bookedCount", "currentParticipants", "participants", "bookingCount", "taken"),
    "bookable_from": ("bookableFrom", "bookableFromDateTime", "bookingStartDate", "openFrom"),
    "state": ("bookingState", "state", "status", "myBookingState"),
    "booking_id": ("bookingId", "customerBookingId", "reservationId", "id"),
}


def load_endpoints(path: str | Path) -> Endpoints:
    """Read and validate endpoints.toml."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"{path} not found -- generate it with "
            f"`python -m poolbot.extract_apk /path/to/clubsportive.apk -o {path.name}` "
            f"or copy endpoints.example.toml and fill it in"
        )
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    base = str(raw.get("base_url") or "").strip().rstrip("/")
    if not base or PLACEHOLDER in base:
        raise ConfigError(f"{path}: base_url is still a placeholder")
    if not base.startswith("https://"):
        raise ConfigError(f"{path}: base_url must be https://, got {base!r}")

    h = _require_mapping(raw.get("headers"), "headers")
    facility = str(h.get("facility_group") or "").strip()
    if not facility or PLACEHOLDER in facility:
        raise ConfigError(
            f"{path}: [headers].facility_group is still a placeholder -- this is the "
            f"BRANDEDAPP... constant from the APK"
        )

    fields = dict(DEFAULT_FIELDS)
    for key, value in _require_mapping(raw.get("fields"), "fields").items():
        if isinstance(value, str):
            value = [value]
        fields[key] = tuple(str(v) for v in value)

    bodies: dict[str, Mapping[str, Any]] = {}
    for key, value in _require_mapping(raw.get("body"), "body").items():
        bodies[key] = _require_mapping(value, f"body.{key}")

    return Endpoints(
        base_url=base,
        facility_group=facility,
        client_type=str(h.get("client_type") or "APP_V5"),
        client_version=str(h.get("client_version") or "3.68.0"),
        user_agent=str(h.get("user_agent") or "okhttp/4.12.0"),
        paths={k: str(v) for k, v in _require_mapping(raw.get("paths"), "paths").items()},
        methods={k: str(v) for k, v in _require_mapping(raw.get("request"), "request").items()},
        bodies=bodies,
        fields=fields,
    )


# ------------------------------------------------------------------------------------ env


def load_env(path: str | Path, override: bool = False) -> dict[str, str]:
    """Minimal .env reader -- avoids a python-dotenv dependency.

    Existing environment variables win unless ``override`` is set, so secrets can also come
    from a systemd ``EnvironmentFile`` or the shell.
    """
    path = Path(path)
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        if not sep:
            raise ConfigError(f"{path}:{lineno}: expected KEY=value, got {line!r}")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def require_env(*names: str) -> list[str]:
    """Fetch required environment variables, with one clear error listing what is missing."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ConfigError(
            "missing required environment variable(s): "
            + ", ".join(missing)
            + " -- set them in clubsportive/.env (see .env.example)"
        )
    return [os.environ[n] for n in names]
