"""HTTP client for the Clubsportive (MySports / Magicline white-label) app API.

Auth works as documented for the sibling Trainmore app: POST the credentials with HTTP
Basic auth *and* in the JSON body, get back a UUID, then send that UUID as ``x-auth-token``
on every later call. Three build constants travel as headers on every request
(``x-public-facility-group``, ``x-nox-client-type``, ``x-nox-client-version``).

Because the API is not documented, every path, HTTP verb and JSON field name is read from
``endpoints.toml`` (see :mod:`poolbot.config`), and slot objects are normalised through a
list of candidate key spellings rather than hard-coded ones.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPBasicAuth

from .config import ConfigError, Endpoints

log = logging.getLogger("poolbot.client")

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 5.0
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

# A real clock difference between us and the club is seconds, not minutes. Anything
# larger is far more likely a cached/proxied Date header, and acting on it would park
# the strike hours away from the release, so we keep our own clock instead.
MAX_PLAUSIBLE_CLOCK_OFFSET = 120.0

# Keyword -> outcome, matched case-insensitively against the response body. Dutch and
# English, because the API answers in whichever language the account is set to.
FULL_WORDS = ("full", "no places", "no more places", "capacity", "sold out", "volzet",
              "vol ", "bezet", "ausgebucht", "no free", "no available")
NOT_OPEN_WORDS = ("not yet", "too early", "not bookable", "booking period", "not open",
                  "nog niet", "te vroeg", "niet beschikbaar", "noch nicht", "window")
ALREADY_WORDS = ("already booked", "already registered", "already reserved", "duplicate",
                 "al geboekt", "al ingeschreven", "bereits")
LIMIT_WORDS = ("limit", "maximum", "too many", "contingent", "quota", "exceeded",
               "not allowed", "bundle", "credits")
WAITLIST_WORDS = ("waitlist", "waiting list", "wachtlijst", "warteliste")


class Outcome(str, Enum):
    """Result of a booking attempt, classified so the strike loop knows what to do next."""

    BOOKED = "BOOKED"
    WAITLISTED = "WAITLISTED"
    ALREADY_BOOKED = "ALREADY_BOOKED"
    FULL = "FULL"
    NOT_YET_OPEN = "NOT_YET_OPEN"
    LIMIT_REACHED = "LIMIT_REACHED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"

    @property
    def success(self) -> bool:
        return self in (Outcome.BOOKED, Outcome.WAITLISTED, Outcome.ALREADY_BOOKED)

    @property
    def retryable(self) -> bool:
        """FULL is retryable: inside the strike window someone else's spot can be released."""
        return self in (Outcome.FULL, Outcome.NOT_YET_OPEN, Outcome.ERROR)


@dataclass
class BookAttempt:
    outcome: Outcome
    status: int | None = None
    message: str = ""
    booking_id: str | None = None
    elapsed: float = 0.0
    payload: Any = None

    def __str__(self) -> str:
        bits = [self.outcome.value]
        if self.status:
            bits.append(f"HTTP {self.status}")
        if self.message:
            bits.append(self.message[:200])
        return " | ".join(bits)


@dataclass
class Slot:
    """A bookable session, normalised out of whatever shape the API returns."""

    id: str
    title: str
    start: dt.datetime | None
    end: dt.datetime | None = None
    location: str | None = None
    room: str | None = None
    capacity: int | None = None
    booked: int | None = None
    bookable_from: dt.datetime | None = None
    state: str | None = None
    booking_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def free(self) -> int | None:
        if self.capacity is None or self.booked is None:
            return None
        return max(self.capacity - self.booked, 0)

    @property
    def is_booked_by_me(self) -> bool:
        if not self.state:
            return False
        s = self.state.casefold()
        return any(w in s for w in ("booked", "reserved", "confirm", "geboekt", "attending"))

    def describe(self, tz: ZoneInfo | None = None) -> str:
        when = "time unknown"
        if self.start is not None:
            local = self.start.astimezone(tz) if tz else self.start
            when = local.strftime("%a %d %b %H:%M")
        bits = [when, self.title or "(untitled)"]
        if self.room:
            bits.append(self.room)
        if self.capacity is not None:
            bits.append(f"{self.booked if self.booked is not None else '?'}/{self.capacity}")
        return "  ".join(bits)


# ------------------------------------------------------------------------------- parsing


_EPOCH_RE = re.compile(r"^\d{10,13}$")


def parse_api_datetime(value: Any, tz: ZoneInfo | None = None) -> dt.datetime | None:
    """Parse the date formats these APIs use: ISO 8601 (with Z or offset) or epoch ms."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz or dt.timezone.utc)
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if _EPOCH_RE.match(text):
        return parse_api_datetime(int(text), tz)
    candidate = text.replace("Z", "+00:00")
    # Trim fractional seconds longer than microseconds, which fromisoformat rejects on 3.11.
    candidate = re.sub(r"(\.\d{6})\d+", r"\1", candidate)
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            log.debug("could not parse datetime %r", value)
            return None
    if parsed.tzinfo is None:
        # A naive timestamp from the API is club-local time.
        parsed = parsed.replace(tzinfo=tz or dt.timezone.utc)
    return parsed


def _first(obj: Mapping[str, Any], names: Iterable[str]) -> Any:
    """Pick the first present, non-empty value among candidate key spellings (any depth 1)."""
    for name in names:
        if name in obj and obj[name] not in (None, "", []):
            return obj[name]
    # Also look one level down, e.g. {"activity": {"name": ...}} or {"class": {...}}.
    for value in obj.values():
        if isinstance(value, Mapping):
            for name in names:
                if name in value and value[name] not in (None, "", []):
                    return value[name]
    return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _body_text(payload: Any, raw: str) -> str:
    """Best-effort human message out of an error body."""
    if isinstance(payload, Mapping):
        for key in ("message", "error", "detail", "errorMessage", "title", "code",
                    "localizedMessage", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(payload)[:500]
    if isinstance(payload, list) and payload:
        return json.dumps(payload)[:500]
    return (raw or "").strip()[:500]


def classify(status: int, payload: Any, raw: str) -> tuple[Outcome, str]:
    """Map an HTTP response for a booking attempt onto an :class:`Outcome`."""
    message = _body_text(payload, raw)
    haystack = f"{message} {raw}".casefold()
    if 200 <= status < 300:
        if any(w in haystack for w in WAITLIST_WORDS):
            return Outcome.WAITLISTED, message
        return Outcome.BOOKED, message
    if status in (401, 403):
        return Outcome.REJECTED, message or "not authorised"
    if status == 429:
        return Outcome.ERROR, message or "rate limited"
    if status >= 500:
        return Outcome.ERROR, message or f"server error {status}"
    if any(w in haystack for w in ALREADY_WORDS):
        return Outcome.ALREADY_BOOKED, message
    if any(w in haystack for w in NOT_OPEN_WORDS):
        return Outcome.NOT_YET_OPEN, message
    if any(w in haystack for w in FULL_WORDS):
        return Outcome.FULL, message
    if any(w in haystack for w in LIMIT_WORDS):
        return Outcome.LIMIT_REACHED, message
    if status == 404:
        # The slot is not published yet -- treat as "not open" so we keep trying.
        return Outcome.NOT_YET_OPEN, message or "slot not found"
    if status == 409:
        return Outcome.FULL, message or "conflict"
    return Outcome.REJECTED, message or f"HTTP {status}"


def default_token_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "poolbot" / "token.json"


# -------------------------------------------------------------------------------- client


class ClubSportiveClient:
    """Thin, dependency-light client over the app API."""

    def __init__(
        self,
        endpoints: Endpoints,
        username: str,
        password: str,
        *,
        tz: ZoneInfo | None = None,
        session: Any | None = None,
        token_path: str | Path | None = None,
        use_token_cache: bool = True,
    ) -> None:
        self.ep = endpoints
        self.username = username
        self.password = password
        self.tz = tz or ZoneInfo("Europe/Amsterdam")
        self.session = session or requests.Session()
        self.token: str | None = None
        self.token_path = Path(token_path) if token_path else default_token_path()
        self.use_token_cache = use_token_cache
        self.clock_offset = 0.0  # server clock minus local clock, in seconds
        self._last_request_url: str | None = None
        if use_token_cache:
            self.token = self._read_cached_token()

    # ----------------------------------------------------------------- plumbing

    def url(self, template: str, **params: Any) -> str:
        path = template
        for key, value in params.items():
            path = path.replace("{" + key + "}", str(value))
        if path.startswith("http"):
            return path
        return f"{self.ep.base_url}/{path.lstrip('/')}"

    def _headers(self, with_token: bool = True) -> dict[str, str]:
        headers = self.ep.headers()
        if with_token and self.token:
            headers["x-auth-token"] = self.token
        return headers

    def _send(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        auth: Any = None,
        with_token: bool = True,
        timeout: tuple[float, float] = TIMEOUT,
    ) -> requests.Response:
        self._last_request_url = url
        log.debug("%s %s", method, url)
        return self.session.request(
            method,
            url,
            headers=self._headers(with_token=with_token),
            json=json_body,
            auth=auth,
            timeout=timeout,
        )

    @staticmethod
    def _payload(response: Any) -> tuple[Any, str]:
        raw = ""
        try:
            raw = response.text or ""
        except Exception:  # pragma: no cover - defensive
            raw = ""
        try:
            return response.json(), raw
        except Exception:
            return None, raw

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        retries: int = 2,
        relogin: bool = True,
    ) -> Any:
        """Request expecting JSON back; retries transient failures, re-logs in once on 401."""
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                response = self._send(method, url, json_body=json_body)
            except requests.RequestException as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ApiError(f"{method} {url} failed: {exc}") from exc
            if response.status_code in (401, 403) and relogin:
                log.info("token rejected (HTTP %s); logging in again", response.status_code)
                self.login(force=True)
                return self._request_json(
                    method, url, json_body=json_body, retries=retries, relogin=False
                )
            if response.status_code >= 500 and attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            payload, raw = self._payload(response)
            if not (200 <= response.status_code < 300):
                raise ApiError(
                    f"{method} {url} -> HTTP {response.status_code}: {_body_text(payload, raw)}"
                )
            return payload
        raise ApiError(f"{method} {url} failed: {last_error}")

    # --------------------------------------------------------------------- auth

    def _read_cached_token(self) -> str | None:
        try:
            data = json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return None
        if data.get("username") != self.username:
            return None
        token = data.get("token")
        return str(token) if token else None

    def _cache_token(self, token: str) -> None:
        if not self.use_token_cache:
            return
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.token_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"username": self.username, "token": token}))
            os.chmod(tmp, 0o600)
            tmp.replace(self.token_path)
        except OSError as exc:  # pragma: no cover - cache is best effort
            log.debug("could not cache token: %s", exc)

    def login(self, force: bool = False) -> str:
        """Log in and keep the token. The token does not expire, so it is cached on disk."""
        if self.token and not force:
            return self.token
        url = self.url(self.ep.path("login"))
        try:
            response = self._send(
                "POST",
                url,
                json_body={"username": self.username, "password": self.password},
                auth=HTTPBasicAuth(self.username, self.password),
                with_token=False,
                timeout=(CONNECT_TIMEOUT, 10.0),
            )
        except requests.RequestException as exc:
            raise ApiError(f"login request failed: {exc}") from exc
        payload, raw = self._payload(response)
        if not (200 <= response.status_code < 300):
            raise AuthError(
                f"login failed (HTTP {response.status_code}): {_body_text(payload, raw)}"
            )
        token = None
        if isinstance(payload, Mapping):
            token = _first(payload, ("login_response", "token", "authToken", "accessToken",
                                     "sessionId", "x-auth-token"))
        elif isinstance(payload, str) and payload.strip():
            token = payload.strip().strip('"')
        if not token:
            raise AuthError(f"login succeeded but no token in response: {raw[:300]!r}")
        self.token = str(token)
        self._cache_token(self.token)
        log.info("logged in as %s", self.username)
        return self.token

    def me(self) -> Any:
        self.login()
        return self._request_json("GET", self.url(self.ep.path("me")))

    def warm_up(self) -> bool:
        """Open the TCP+TLS connection (and validate the token) before a strike."""
        try:
            self.login()
            path = self.ep.path("me") if self.ep.has_path("me") else self.ep.path("login")
            self._send("GET", self.url(path))
            return True
        except (ApiError, AuthError, requests.RequestException) as exc:
            log.warning("warm-up failed: %s", exc)
            return False

    # -------------------------------------------------------------- clock sync

    def sample_server_clock(self, samples: int = 3) -> float:
        """Measure ``server clock - local clock`` in seconds, keeping the fastest sample.

        The strike fires against the club's clock, not ours: a few hundred milliseconds of
        skew is the difference between first in the queue and "fully booked".
        """
        path = "time" if self.ep.has_path("time") else ("me" if self.ep.has_path("me") else "login")
        url = self.url(self.ep.path(path))
        best_rtt = float("inf")
        offset = self.clock_offset
        for _ in range(max(1, samples)):
            try:
                sent = time.time()
                response = self._send("GET", url)
                received = time.time()
            except requests.RequestException as exc:
                log.debug("clock sample failed: %s", exc)
                continue
            header = response.headers.get("Date") if hasattr(response, "headers") else None
            if not header:
                continue
            try:
                server = email.utils.parsedate_to_datetime(header).timestamp()
            except (TypeError, ValueError):
                continue
            rtt = received - sent
            if rtt < best_rtt:
                best_rtt = rtt
                # The Date header has 1s granularity; assume it was stamped mid-flight.
                offset = server - (sent + rtt / 2.0)
        if best_rtt == float("inf"):
            log.warning("could not sample server clock; assuming local clock is right")
        elif abs(offset) > MAX_PLAUSIBLE_CLOCK_OFFSET:
            log.warning(
                "ignoring implausible server clock offset %+.1fs (over %.0fs); the Date header "
                "is probably cached or proxied. Using the local clock.",
                offset,
                MAX_PLAUSIBLE_CLOCK_OFFSET,
            )
        else:
            self.clock_offset = offset
            log.info("server clock offset %+.3fs (best rtt %.0fms)", offset, best_rtt * 1000)
        return self.clock_offset

    # -------------------------------------------------------------------- slots

    def slots_raw(self, date_from: dt.date, date_to: dt.date) -> Any:
        self.login()
        url = self.url(
            self.ep.path("slots"),
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )
        return self._request_json(self.ep.method("slots", "GET"), url)

    def slots(self, date_from: dt.date, date_to: dt.date) -> list[Slot]:
        payload = self.slots_raw(date_from, date_to)
        return self.normalise_slots(payload)

    def normalise_slots(self, payload: Any) -> list[Slot]:
        items = self._find_slot_list(payload)
        slots = [self.normalise_slot(item) for item in items if isinstance(item, Mapping)]
        return [s for s in slots if s is not None]

    def _find_slot_list(self, payload: Any) -> Sequence[Any]:
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, Mapping):
            return []
        for key in self.ep.field_names("slots_root"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, Mapping):
                nested = self._find_slot_list(value)
                if nested:
                    return nested
        # Fall back to a breadth-first hunt for the first list of slot-shaped dicts.
        queue: list[Any] = [payload]
        id_keys = set(self.ep.field_names("id"))
        start_keys = set(self.ep.field_names("start"))
        while queue:
            current = queue.pop(0)
            if isinstance(current, Mapping):
                queue.extend(current.values())
            elif isinstance(current, list):
                dicts = [x for x in current if isinstance(x, Mapping)]
                if dicts and any(
                    (set(d) & id_keys) and (set(d) & start_keys) for d in dicts
                ):
                    return current
                queue.extend(current)
        return []

    def normalise_slot(self, item: Mapping[str, Any]) -> Slot | None:
        slot_id = _first(item, self.ep.field_names("id"))
        if slot_id in (None, ""):
            return None
        title = _first(item, self.ep.field_names("title"))
        return Slot(
            id=str(slot_id),
            title=str(title) if title else "",
            start=parse_api_datetime(_first(item, self.ep.field_names("start")), self.tz),
            end=parse_api_datetime(_first(item, self.ep.field_names("end")), self.tz),
            location=_opt_str(_first(item, self.ep.field_names("location"))),
            room=_opt_str(_first(item, self.ep.field_names("room"))),
            capacity=_as_int(_first(item, self.ep.field_names("capacity"))),
            booked=_as_int(_first(item, self.ep.field_names("booked"))),
            bookable_from=parse_api_datetime(
                _first(item, self.ep.field_names("bookable_from")), self.tz
            ),
            state=_opt_str(_first(item, self.ep.field_names("state"))),
            booking_id=_opt_str(_first(item, self.ep.field_names("booking_id"))),
            raw=item,
        )

    def find_slot(self, slot_id: str, date_from: dt.date, date_to: dt.date) -> Slot | None:
        for slot in self.slots(date_from, date_to):
            if slot.id == str(slot_id):
                return slot
        return None

    # ------------------------------------------------------------------ booking

    def _templated_body(self, key: str, slot_id: str) -> Any:
        body = self.ep.body(key)
        if not body:
            return None
        out: dict[str, Any] = {}
        for k, v in body.items():
            out[k] = v.replace("{slot_id}", str(slot_id)) if isinstance(v, str) else v
        return out

    def book(self, slot_id: str, *, dry_run: bool = False) -> BookAttempt:
        """One booking attempt. Never raises for an API refusal -- it classifies it."""
        if dry_run:
            return BookAttempt(Outcome.BOOKED, status=None, message="dry run: not sent")
        url = self.url(self.ep.path("book"), slot_id=slot_id)
        started = time.time()
        try:
            response = self._send(
                self.ep.method("book", "POST"),
                url,
                json_body=self._templated_body("book", slot_id),
            )
        except requests.RequestException as exc:
            return BookAttempt(Outcome.ERROR, message=str(exc), elapsed=time.time() - started)
        payload, raw = self._payload(response)
        outcome, message = classify(response.status_code, payload, raw)
        booking_id = None
        if isinstance(payload, Mapping):
            booking_id = _opt_str(_first(payload, self.ep.field_names("booking_id")))
        return BookAttempt(
            outcome=outcome,
            status=response.status_code,
            message=message,
            booking_id=booking_id,
            elapsed=time.time() - started,
            payload=payload,
        )

    def join_waitlist(self, slot_id: str, *, dry_run: bool = False) -> BookAttempt:
        if not self.ep.has_path("waitlist"):
            return BookAttempt(Outcome.ERROR, message="no waitlist endpoint configured")
        if dry_run:
            return BookAttempt(Outcome.WAITLISTED, message="dry run: not sent")
        url = self.url(self.ep.path("waitlist"), slot_id=slot_id)
        started = time.time()
        try:
            response = self._send(
                self.ep.method("waitlist", "POST"),
                url,
                json_body=self._templated_body("waitlist", slot_id),
            )
        except requests.RequestException as exc:
            return BookAttempt(Outcome.ERROR, message=str(exc), elapsed=time.time() - started)
        payload, raw = self._payload(response)
        outcome, message = classify(response.status_code, payload, raw)
        if outcome is Outcome.BOOKED:
            outcome = Outcome.WAITLISTED
        return BookAttempt(outcome, response.status_code, message,
                           elapsed=time.time() - started, payload=payload)

    def cancel(self, *, slot_id: str | None = None, booking_id: str | None = None,
               dry_run: bool = False) -> BookAttempt:
        if not (slot_id or booking_id):
            raise ValueError("cancel needs a slot_id or a booking_id")
        if dry_run:
            return BookAttempt(Outcome.BOOKED, message="dry run: not sent")
        url = self.url(
            self.ep.path("cancel"),
            slot_id=slot_id or "",
            booking_id=booking_id or slot_id or "",
        )
        started = time.time()
        try:
            response = self._send(self.ep.method("cancel", "POST"), url)
        except requests.RequestException as exc:
            return BookAttempt(Outcome.ERROR, message=str(exc), elapsed=time.time() - started)
        payload, raw = self._payload(response)
        outcome, message = classify(response.status_code, payload, raw)
        return BookAttempt(outcome, response.status_code, message,
                           elapsed=time.time() - started, payload=payload)


def _opt_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, Mapping):
        for key in ("name", "title", "value", "id"):
            if key in value:
                return str(value[key])
        return None
    return str(value)


class ApiError(Exception):
    """A request to the app API failed in a way we cannot interpret."""


class AuthError(ApiError):
    """Login was refused."""


def client_from_env(endpoints: Endpoints, tz: ZoneInfo | None = None, **kwargs: Any) -> ClubSportiveClient:
    from .config import require_env

    username, password = require_env("CS_USERNAME", "CS_PASSWORD")
    return ClubSportiveClient(endpoints, username, password, tz=tz, **kwargs)
