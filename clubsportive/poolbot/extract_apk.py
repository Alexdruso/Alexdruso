"""Pull the app's API constants out of the Clubsportive APK.

The Clubsportive app is a Flutter build, so the interesting strings live in the string pool
of ``lib/<abi>/libapp.so`` (plus the DEX files and bundled assets). This module reads them
out of the archive in memory -- nothing is unpacked to disk and nothing from the APK is ever
executed -- and writes an ``endpoints.toml`` skeleton with its best guesses.

    python -m poolbot.extract_apk ~/Downloads/clubsportive.apk -o endpoints.toml

Download the APK from APKMirror or APKPure, or pull it off your own phone:

    adb shell pm path com.mysports.branded.clubsportive
    adb pull <each listed path>

``.xapk``/``.apks`` bundles and split APKs are handled: pass the bundle, or pass several
files at once.
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger("poolbot.extract")

MEMBER_PATTERNS = (
    re.compile(r"lib/[^/]+/lib(app|flutter)\.so$"),
    re.compile(r"classes\d*\.dex$"),
    re.compile(r"assets/.*\.(json|dart|txt|js|properties|xml)$"),
    re.compile(r"res/raw/.*$"),
    re.compile(r"AndroidManifest\.xml$"),
)
NESTED_PATTERN = re.compile(r"\.(apk|zip)$", re.IGNORECASE)

MAX_MEMBER_BYTES = 320 * 1024 * 1024

FACILITY_RE = re.compile(r"BRANDEDAPP[A-Za-z0-9_\-]{2,80}")
CLIENT_TYPE_RE = re.compile(r"APP_V\d+")
VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\b")
URL_RE = re.compile(r"https://[a-zA-Z0-9][a-zA-Z0-9.\-]{2,100}\.[a-zA-Z]{2,24}(?:/[A-Za-z0-9._~%\-/{}:]*)?")
PATH_KEYWORDS = (
    "class", "course", "slot", "appointment", "book", "reserv", "waitlist", "waiting",
    "schedul", "swim", "pool", "login", "logout", "session", "customer", "member",
    "profile", "checkin", "qrcode", "facility", "studio", "calendar",
)
PATH_RE = re.compile(
    r"/(?:[A-Za-z0-9._~\-]+/){0,6}[A-Za-z0-9._~\-{}]*(?:"
    + "|".join(PATH_KEYWORDS)
    + r")[A-Za-z0-9._~\-/{}]*",
    re.IGNORECASE,
)
ASCII_RUN_RE = re.compile(rb"[\x20-\x7e]{6,}")

# Endpoint buckets and the keywords that identify them. A path is scored against every
# bucket (a keyword in the last segment counts for more than one buried mid-path) and lands
# in its best-scoring bucket, so "/classes/{slot_id}/booking" is a booking endpoint rather
# than a schedule one, and "/bookings/{id}/cancel" is a cancellation rather than a booking.
BUCKETS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("waitlist", ("waitlist", "waiting")),
    ("cancel", ("cancel", "unbook", "unregister", "withdraw")),
    ("login", ("login", "signin", "authenticate", "token")),
    ("me", ("me", "profile", "self", "account", "customer")),
    ("book", ("book", "booking", "reserve", "reservation", "register", "signup", "enrol",
              "enroll", "participate")),
    ("slots", ("classes", "class", "courses", "course", "schedule", "calendar", "slots",
               "appointments", "timetable")),
)
# Buckets whose endpoint acts on one slot/booking, so a placeholder is a good sign.
ITEM_BUCKETS = ("book", "cancel", "waitlist")


def _segments(path: str) -> list[str]:
    """Path segments, query string stripped."""
    return [s for s in path.split("?", 1)[0].split("/") if s]


def bucket_score(path: str, keywords: tuple[str, ...], bucket: str) -> int:
    """How well a path fits a bucket. Keywords near the end of the path weigh more."""
    segments = _segments(path)
    if not segments:
        return 0
    low = [s.casefold() for s in segments]
    score = 0
    for keyword in keywords:
        if any(keyword == s for s in low):
            score = max(score, 4 if low[-1] == keyword else 2)
        elif any(keyword in s for s in low):
            where = max(i for i, s in enumerate(low) if keyword in s)
            score = max(score, 3 if where == len(low) - 1 else 1)
    if not score:
        return 0
    has_placeholder = "{" in path
    if bucket in ITEM_BUCKETS:
        score += 1 if has_placeholder else 0
    else:
        score -= 2 if has_placeholder else 0
    return score


@dataclass
class Findings:
    facility_groups: Counter[str] = field(default_factory=Counter)
    client_types: Counter[str] = field(default_factory=Counter)
    versions: Counter[str] = field(default_factory=Counter)
    urls: Counter[str] = field(default_factory=Counter)
    hosts: Counter[str] = field(default_factory=Counter)
    paths: Counter[str] = field(default_factory=Counter)
    members_scanned: list[str] = field(default_factory=list)

    def merge_text(self, text: str) -> None:
        for match in FACILITY_RE.findall(text):
            self.facility_groups[match] += 1
        for match in CLIENT_TYPE_RE.findall(text):
            self.client_types[match] += 1
        for match in URL_RE.findall(text):
            url = match.rstrip("/.,;:\"')")
            self.urls[url] += 1
            host = url.split("//", 1)[1].split("/", 1)[0]
            self.hosts[host] += 1
        for match in PATH_RE.findall(text):
            path = match.rstrip("/.,;:\"')")
            if 3 <= len(path) <= 120 and not path.startswith("//"):
                self.paths[path] += 1

    def merge_versions_near(self, text: str) -> None:
        """Version numbers are noisy; only trust ones near an APP_V marker."""
        for marker in CLIENT_TYPE_RE.finditer(text):
            window = text[max(0, marker.start() - 120) : marker.end() + 120]
            for version in VERSION_RE.findall(window):
                self.versions[version] += 1

    @property
    def best_host(self) -> str | None:
        if not self.hosts:
            return None
        def score(item: tuple[str, int]) -> tuple[int, int, int]:
            host, count = item
            api_bonus = 2 if host.startswith("api.") or ".api." in host else 0
            api_bonus += 1 if "api" in host else 0
            path_bonus = 1 if any(u.startswith(f"https://{host}/") for u in self.urls) else 0
            return (api_bonus + path_bonus, count, -len(host))
        return max(self.hosts.items(), key=score)[0]

    def bucketed_paths(self) -> dict[str, list[str]]:
        """Assign every candidate path to its best-fitting bucket, best candidate first."""
        scored: dict[str, list[tuple[int, int, str]]] = {name: [] for name, _ in BUCKETS}
        for path, count in self.paths.items():
            best_bucket, best_score = None, 0
            for name, keywords in BUCKETS:
                score = bucket_score(path, keywords, name)
                if score > best_score:
                    best_bucket, best_score = name, score
            if best_bucket is not None:
                scored[best_bucket].append((best_score, count, path))
        return {
            name: [path for _, _, path in sorted(items, key=lambda t: (-t[0], -t[1], len(t[2])))]
            for name, items in scored.items()
        }


def _interesting(name: str) -> bool:
    return any(p.search(name) for p in MEMBER_PATTERNS)


def iter_members(path: Path, depth: int = 0) -> Iterator[tuple[str, bytes]]:
    """Yield (name, bytes) for scannable members, descending into nested APKs once."""
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"{path}: not a readable APK/XAPK/ZIP ({exc})")
    with archive:
        yield from _iter_zip(archive, f"{path.name}", depth)


def _iter_zip(archive: zipfile.ZipFile, prefix: str, depth: int) -> Iterator[tuple[str, bytes]]:
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if NESTED_PATTERN.search(name) and depth < 2:
            if info.file_size > MAX_MEMBER_BYTES:
                continue
            try:
                nested_bytes = archive.read(info)
                with zipfile.ZipFile(io.BytesIO(nested_bytes)) as nested:
                    yield from _iter_zip(nested, f"{prefix}!{name}", depth + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                log.debug("skipping nested %s: %s", name, exc)
            continue
        if not _interesting(name):
            continue
        if info.file_size > MAX_MEMBER_BYTES:
            log.warning("skipping %s (%.0f MB)", name, info.file_size / 1e6)
            continue
        try:
            yield f"{prefix}!{name}", archive.read(info)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            log.debug("could not read %s: %s", name, exc)


def extract_strings(data: bytes, min_len: int = 6) -> str:
    """Printable ASCII runs plus a UTF-16LE pass, joined with newlines."""
    parts = [m.group().decode("ascii", "ignore") for m in ASCII_RUN_RE.finditer(data)]
    # UTF-16LE strings show up as every-other-NUL; decode cheaply and keep printable runs.
    if b"\x00" in data[:4096]:
        try:
            wide = data.decode("utf-16-le", "ignore")
        except (UnicodeDecodeError, ValueError):  # pragma: no cover
            wide = ""
        parts.extend(
            run for run in re.findall(r"[\x20-\x7e]{%d,}" % min_len, wide)
        )
    return "\n".join(parts)


def scan(paths: Iterable[Path]) -> Findings:
    findings = Findings()
    for path in paths:
        for name, data in iter_members(path):
            text = extract_strings(data)
            if not text:
                continue
            findings.members_scanned.append(f"{name} ({len(data) / 1e6:.1f} MB)")
            findings.merge_text(text)
            findings.merge_versions_near(text)
    return findings


# ------------------------------------------------------------------------------- output


def _pick(bucket: list[str], host: str | None, fallback: str) -> tuple[str, bool]:
    """Best path for a bucket; second value says whether it is a real find."""
    if not bucket:
        return fallback, False
    return bucket[0], True


def render_toml(findings: Findings) -> str:
    host = findings.best_host
    base = f"https://{host}" if host else "https://REPLACE-ME"
    facility = (
        findings.facility_groups.most_common(1)[0][0] if findings.facility_groups else "REPLACE-ME"
    )
    client_type = (
        findings.client_types.most_common(1)[0][0] if findings.client_types else "APP_V5"
    )
    version = findings.versions.most_common(1)[0][0] if findings.versions else "3.68.0"
    buckets = findings.bucketed_paths()

    login, login_ok = _pick(buckets["login"], host, "/v1/login")
    me, me_ok = _pick(buckets["me"], host, "/v1/me")
    slots, slots_ok = _pick(buckets["slots"], host, "/v1/classes")
    book, book_ok = _pick(buckets["book"], host, "/v1/classes/{slot_id}/book")
    cancel, cancel_ok = _pick(buckets["cancel"], host, "/v1/bookings/{booking_id}/cancel")
    waitlist, waitlist_ok = _pick(buckets["waitlist"], host, "")

    if slots_ok and "{" not in slots and "?" not in slots:
        slots = slots + "?from={date_from}&to={date_to}"

    def todo(ok: bool, note: str = "guessed -- verify against a real request") -> str:
        return "" if ok else f"   # TODO {note}"

    book_note = todo(book_ok)
    if book_ok and "{slot_id}" not in book:
        book_note = "   # TODO no slot placeholder found; add {slot_id} where the id belongs"
    cancel_note = todo(cancel_ok)
    if cancel_ok and "{" not in cancel:
        cancel_note = "   # TODO add {booking_id} or {slot_id} where the id belongs"

    lines = [
        "# Generated by poolbot.extract_apk. Check every value before relying on it:",
        "# the extractor reads strings out of the APK, it cannot see how they are combined.",
        "#",
        f"# Scanned: {len(findings.members_scanned)} member(s)",
        f"# Hosts seen: {', '.join(h for h, _ in findings.hosts.most_common(8)) or 'none'}",
        "",
        f'base_url = "{base}"{todo(bool(host))}',
        "",
        "[headers]",
        f'facility_group = "{facility}"{todo(bool(findings.facility_groups))}',
        f'client_type = "{client_type}"',
        f'client_version = "{version}"',
        'user_agent = "okhttp/4.12.0"',
        "",
        "[paths]",
        "# {date_from}/{date_to} (YYYY-MM-DD), {slot_id} and {booking_id} are substituted.",
        f'login = "{login}"{todo(login_ok)}',
        f'me = "{me}"{todo(me_ok)}',
        f'slots = "{slots}"{todo(slots_ok)}',
        f'book = "{book}"{book_note}',
        f'cancel = "{cancel}"{cancel_note}',
        f'waitlist = "{waitlist}"{todo(waitlist_ok, "no waiting-list endpoint found; leave empty if there is none")}',
        'time = ""',
        "",
        "[request]",
        'book = "POST"',
        'cancel = "POST"',
        'slots = "GET"',
        "",
        "[body]",
        "# Extra JSON for a booking request, e.g. book = { slotId = \"{slot_id}\" }",
        "book = {}",
        "",
        "# ---------------------------------------------------------------------------",
        "# Every candidate path the APK contained, most frequent first. If a guess above",
        "# is wrong, the right answer is probably in here.",
    ]
    for path, count in findings.paths.most_common(80):
        lines.append(f"#   {count:>4}x  {path}")
    if findings.urls:
        lines.append("#")
        lines.append("# Full URLs seen:")
        for url, count in findings.urls.most_common(30):
            lines.append(f"#   {count:>4}x  {url}")
    return "\n".join(lines) + "\n"


def report(findings: Findings) -> str:
    out = [f"Scanned {len(findings.members_scanned)} member(s):"]
    out += [f"  {m}" for m in findings.members_scanned[:12]]
    if len(findings.members_scanned) > 12:
        out.append(f"  ... and {len(findings.members_scanned) - 12} more")
    out.append("")
    out.append("Facility group (x-public-facility-group):")
    out += [f"  {c:>4}x  {v}" for v, c in findings.facility_groups.most_common(5)] or ["  none found"]
    out.append("Client type / version:")
    out += [f"  {c:>4}x  {v}" for v, c in findings.client_types.most_common(3)] or ["  none found"]
    out += [f"  {c:>4}x  version {v}" for v, c in findings.versions.most_common(3)]
    out.append("Hosts:")
    out += [f"  {c:>4}x  {v}" for v, c in findings.hosts.most_common(10)] or ["  none found"]
    out.append(f"Best guess base_url: https://{findings.best_host}" if findings.best_host
               else "Best guess base_url: NONE -- scan failed, try a different split APK")
    out.append("")
    buckets = findings.bucketed_paths()
    for name, _ in BUCKETS:
        found = buckets[name]
        out.append(f"{name:>9}: " + (found[0] if found else "(nothing matched)"))
        for extra in found[1:4]:
            out.append(f"{'':>11}" + extra)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m poolbot.extract_apk",
        description="Extract the Clubsportive app's API constants from its APK.",
    )
    parser.add_argument("apk", nargs="+", type=Path, help="APK / XAPK / APKS file(s)")
    parser.add_argument("-o", "--output", type=Path, help="write an endpoints.toml here")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )
    for path in args.apk:
        if not path.exists():
            parser.error(f"{path} does not exist")

    findings = scan(args.apk)
    print(report(findings))

    if not findings.members_scanned:
        print(
            "\nNothing scannable found. If this was an .xapk/.apks bundle, try passing the "
            "individual split APKs (especially split_config.arm64_v8a.apk).",
            file=sys.stderr,
        )
        return 2

    if args.output:
        if args.output.exists() and not args.force:
            print(f"\n{args.output} exists; pass --force to overwrite", file=sys.stderr)
            return 1
        args.output.write_text(render_toml(findings))
        print(f"\nWrote {args.output} -- review every TODO in it before going live.")
    else:
        print("\n(pass -o endpoints.toml to write the config skeleton)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
