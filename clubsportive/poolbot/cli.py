"""Command line interface: poolbot doctor | slots | plan | snipe | daemon | ..."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .client import ApiError, ClubSportiveClient, Outcome, client_from_env
from .config import Config, ConfigError, load_config, load_endpoints, load_env
from .matching import pool_slots
from .notify import MailSettings, notify_result
from .scheduler import (
    Strike,
    StrikeResult,
    due_strikes,
    next_strike,
    next_target,
    plan,
    release_moment,
    run_daemon,
    run_strike,
    watch,
)
from .state import State

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("poolbot")


def default_path(name: str) -> Path:
    """Look in the working directory first, then next to the package.

    An editable install resolves to the checkout either way; a plain `pip install` would
    otherwise point the defaults at site-packages.
    """
    local = Path.cwd() / name
    return local if local.exists() else ROOT / name


def resolve_paths(args: argparse.Namespace) -> None:
    """endpoints.toml, state.json and .env default to sitting beside config.yaml.

    So the directory holding your config is the deployment directory, wherever it is.
    """
    home = Path(args.config).resolve().parent
    for attr, name in (
        ("endpoints", "endpoints.toml"),
        ("state", "state.json"),
        ("env", ".env"),
    ):
        if getattr(args, attr, None) is None:
            setattr(args, attr, home / name)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- bootstrap


class Context:
    """Everything a command needs: config, endpoints, state, and a logged-out client."""

    def __init__(self, args: argparse.Namespace, *, need_api: bool = True) -> None:
        load_env(args.env)
        self.cfg: Config = load_config(args.config)
        self.state = State.load(args.state, self.cfg.tz)
        self.dry_run: bool = bool(getattr(args, "dry_run", False))
        self.client: ClubSportiveClient | None = None
        if need_api:
            endpoints = load_endpoints(args.endpoints)
            self.client = client_from_env(endpoints, tz=self.cfg.tz)

    @property
    def api(self) -> ClubSportiveClient:
        if self.client is None:  # pragma: no cover - guarded by need_api
            raise ConfigError("this command needs API access")
        return self.client


# ---------------------------------------------------------------------------- commands


def cmd_doctor(args: argparse.Namespace) -> int:
    ctx = Context(args)
    cfg, api = ctx.cfg, ctx.api
    print(f"poolbot {__version__}")
    print(f"config      {args.config}")
    print(f"endpoints   {args.endpoints}  -> {api.ep.base_url}")
    print(f"facility    {api.ep.facility_group}")
    print(f"client      {api.ep.client_type} / {api.ep.client_version}")
    print(f"timezone    {cfg.tz.key}   release offset {cfg.release_offset}")
    print(f"state       {args.state}  ({len(ctx.state.bookings)} remembered booking(s))")
    print(f"token cache {api.token_path}")

    mail = MailSettings.from_env()
    print(f"email       {'-> ' + ', '.join(mail.recipients) if mail else 'not configured'}")

    try:
        api.login(force=args.relogin)
    except ApiError as exc:
        print(f"\nLOGIN FAILED: {exc}", file=sys.stderr)
        return 1
    print("\nlogin       ok")
    if api.ep.has_path("me"):
        try:
            me = api.me()
            if isinstance(me, dict):
                name = " ".join(
                    str(me.get(k)) for k in ("firstname", "firstName", "lastname", "lastName")
                    if me.get(k)
                )
                print(f"account     {name or json.dumps(me)[:120]}")
        except ApiError as exc:
            print(f"account     could not read profile: {exc}")

    offset = api.sample_server_clock()
    print(f"clock skew  {offset:+.3f}s (server minus local)")

    upcoming = next_strike(cfg, grace=cfg.strike.window_seconds)
    print(f"next strike {upcoming.describe(cfg) if upcoming else 'nothing in the horizon'}")
    return 0


def _slot_rows(ctx: Context, slots: Sequence, show_all: bool) -> None:
    cfg = ctx.cfg
    shown = slots if show_all else pool_slots(cfg, slots)
    if not shown:
        print("(no slots matched -- try --all to see everything the API returned)")
        return
    shown = sorted(shown, key=lambda s: (s.start is None, s.start))
    for slot in shown:
        marker = "*" if slot.is_booked_by_me or ctx.state.already_booked(slot.id) else " "
        free = "" if slot.free is None else f"{slot.free} free"
        opens = ""
        if slot.bookable_from is not None:
            opens = f"opens {slot.bookable_from.astimezone(cfg.tz):%a %H:%M:%S}"
        print(f"{marker} {slot.id:>12}  {slot.describe(cfg.tz):<54} {free:<9} {opens}")


def cmd_slots(args: argparse.Namespace) -> int:
    ctx = Context(args)
    start = dt.date.fromisoformat(args.date) if args.date else ctx.cfg.now().date()
    end = start + dt.timedelta(days=max(args.days - 1, 0))
    if args.raw:
        print(json.dumps(ctx.api.slots_raw(start, end), indent=2)[: args.raw_limit])
        return 0
    slots = ctx.api.slots(start, end)
    print(f"{len(slots)} slot(s) between {start} and {end}; * = you hold it\n")
    _slot_rows(ctx, slots, args.all)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    ctx = Context(args, need_api=False)
    cfg = ctx.cfg
    now = cfg.now()
    strikes = plan(cfg, now, horizon_days=args.days)
    if not strikes:
        print("nothing to book -- every target in the horizon is blacked out")
        return 0
    print(f"now {now:%a %d %b %H:%M:%S %Z}\n")
    print(f"{'wish':<24} {'session':<22} {'books at':<22} status")
    for strike in strikes:
        delta = (strike.release - now).total_seconds()
        if delta < -cfg.strike.window_seconds:
            status = "release passed"
        elif delta < 0:
            status = "open now"
        else:
            status = f"in {_human(delta)}"
        print(
            f"{strike.wish.name[:23]:<24} "
            f"{strike.target.astimezone(cfg.tz):%a %d %b %H:%M}      "
            f"{strike.release.astimezone(cfg.tz):%a %d %b %H:%M:%S}      {status}"
        )
    return 0


def _human(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def cmd_book(args: argparse.Namespace) -> int:
    ctx = Context(args)
    attempt = ctx.api.book(args.slot_id, dry_run=ctx.dry_run)
    print(attempt)
    if attempt.outcome.success and not ctx.dry_run:
        today = ctx.cfg.now().date()
        slot = ctx.api.find_slot(args.slot_id, today, today + dt.timedelta(days=14))
        from .state import Booking

        ctx.state.record_booking(
            Booking(
                slot_id=str(args.slot_id),
                start=slot.start if slot else None,
                title=slot.title if slot else "",
                wish="manual",
                booking_id=attempt.booking_id,
                outcome=attempt.outcome.value,
            )
        )
        ctx.state.save()
    return 0 if attempt.outcome.success else 1


def cmd_cancel(args: argparse.Namespace) -> int:
    ctx = Context(args)
    attempt = ctx.api.cancel(
        slot_id=args.slot_id, booking_id=args.booking_id, dry_run=ctx.dry_run
    )
    print(attempt)
    if attempt.outcome.success and not ctx.dry_run and args.slot_id:
        if ctx.state.drop_booking(args.slot_id):
            ctx.state.save()
    return 0 if attempt.outcome.success else 1


def _strike_for(ctx: Context, args: argparse.Namespace) -> Strike:
    cfg = ctx.cfg
    wish = cfg.wish(args.wish) if args.wish else cfg.wishes[0]
    if args.date:
        day = dt.date.fromisoformat(args.date)
        target = dt.datetime.combine(day, wish.start, tzinfo=cfg.tz)
    else:
        target = next_target(cfg, wish, cfg.now())
    release = release_moment(cfg, target)
    if args.now:
        release = cfg.now().astimezone(dt.timezone.utc)
    return Strike(wish, target, release)


def cmd_snipe(args: argparse.Namespace) -> int:
    ctx = Context(args)
    strike = _strike_for(ctx, args)
    log.info("snipe: %s", strike.describe(ctx.cfg))
    result = run_strike(ctx.api, ctx.cfg, strike, ctx.state, dry_run=ctx.dry_run)
    print(result.summary(ctx.cfg))
    if not args.no_mail:
        notify_result(result, ctx.cfg)
    return 0 if result.success else 1


def cmd_run_due(args: argparse.Namespace) -> int:
    ctx = Context(args)
    strikes = due_strikes(ctx.cfg, within=args.within)
    if not strikes:
        log.info("nothing due within %s", _human(args.within))
        return 0
    results: list[StrikeResult] = []
    for strike in strikes:
        result = run_strike(ctx.api, ctx.cfg, strike, ctx.state, dry_run=ctx.dry_run)
        print(result.summary(ctx.cfg))
        if not args.no_mail:
            notify_result(result, ctx.cfg)
        results.append(result)
    return 0 if all(r.success for r in results) else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    ctx = Context(args)
    log.info("poolbot %s daemon starting (dry_run=%s)", __version__, ctx.dry_run)
    config_path = args.config

    def reload_config() -> Config:
        return load_config(config_path)

    results = run_daemon(
        ctx.api,
        ctx.cfg,
        ctx.state,
        notifier=(None if args.no_mail else (lambda r: notify_result(r, ctx.cfg))),
        dry_run=ctx.dry_run,
        max_strikes=args.max_strikes,
        reload_config=reload_config,
    )
    return 0 if all(r.success for r in results) else 1


def cmd_watch(args: argparse.Namespace) -> int:
    ctx = Context(args)
    start = dt.date.fromisoformat(args.date) if args.date else ctx.cfg.now().date()
    end = start + dt.timedelta(days=max(args.days - 1, 0))
    report = watch(
        ctx.api,
        ctx.cfg,
        args.slot_id,
        date_from=start,
        date_to=end,
        interval=args.interval,
        max_checks=args.max_checks,
    )
    print(json.dumps(report, indent=2))
    measured = report.get("measured_offset_hours")
    if measured is not None:
        print(
            f"\nMeasured release offset: {measured}h "
            f"(config says {report.get('configured_offset_hours')}h)."
        )
        print("Set release_offset_hours to the measured value if they disagree.")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    ctx = Context(args, need_api=False)
    cfg = ctx.cfg
    if not ctx.state.bookings and not ctx.state.attempts:
        print("no history yet")
        return 0
    print("bookings:")
    for booking in ctx.state.bookings:
        when = booking.start.astimezone(cfg.tz).strftime("%a %d %b %H:%M") if booking.start else "?"
        print(f"  {booking.outcome:<15} {when:<18} {booking.title:<28} (slot {booking.slot_id})")
    print("\nlast attempts:")
    for entry in ctx.state.attempts[-args.limit :]:
        print(
            f"  {entry.get('at', '')[:19]}  {entry.get('outcome', ''):<14} "
            f"{entry.get('wish', ''):<24} {str(entry.get('message', ''))[:60]}"
        )
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    from .extract_apk import main as extract_main

    forwarded = [str(p) for p in args.apk]
    output = args.output or args.endpoints
    if output:
        forwarded += ["-o", str(output)]
    if args.force:
        forwarded += ["--force"]
    return extract_main(forwarded)


# ------------------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poolbot",
        description="Book Clubsportive pool slots the moment they are released.",
    )
    parser.add_argument("--version", action="version", version=f"poolbot {__version__}")
    parser.add_argument("--config", type=Path, default=default_path("config.yaml"))
    parser.add_argument("--endpoints", type=Path, default=None,
                        help="default: endpoints.toml beside config.yaml")
    parser.add_argument("--state", type=Path, default=None,
                        help="default: state.json beside config.yaml")
    parser.add_argument("--env", type=Path, default=None,
                        help="default: .env beside config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="do everything except send the booking request",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="check config, log in, measure clock skew")
    p.add_argument("--relogin", action="store_true", help="ignore the cached token")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("slots", help="list what the API offers")
    p.add_argument("--days", type=int, default=3)
    p.add_argument("--date", help="first day (YYYY-MM-DD), default today")
    p.add_argument("--all", action="store_true", help="do not filter to pool sessions")
    p.add_argument("--raw", action="store_true", help="dump the raw JSON response")
    p.add_argument("--raw-limit", type=int, default=20000)
    p.set_defaults(func=cmd_slots)

    p = sub.add_parser("plan", help="show upcoming wishes and when each one opens")
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("book", help="book one slot by id, right now")
    p.add_argument("--slot-id", required=True)
    p.set_defaults(func=cmd_book)

    p = sub.add_parser("cancel", help="cancel a booking (please do this if you cannot go)")
    p.add_argument("--slot-id")
    p.add_argument("--booking-id")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("snipe", help="run one strike: wait for the release, then book")
    p.add_argument("--wish", help="wish name from config.yaml (default: the first one)")
    p.add_argument("--date", help="session date (YYYY-MM-DD), default the next occurrence")
    p.add_argument("--now", action="store_true", help="do not wait; strike immediately")
    p.add_argument("--no-mail", action="store_true")
    p.set_defaults(func=cmd_snipe)

    p = sub.add_parser("run-due", help="run every strike opening soon (for cron)")
    p.add_argument("--within", type=float, default=900.0, help="seconds ahead to consider")
    p.add_argument("--no-mail", action="store_true")
    p.set_defaults(func=cmd_run_due)

    p = sub.add_parser("daemon", help="stay running and strike at every release")
    p.add_argument("--max-strikes", type=int, default=None)
    p.add_argument("--no-mail", action="store_true")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("watch", help="measure when a slot really becomes bookable")
    p.add_argument("--slot-id", required=True)
    p.add_argument("--date", help="first day to search (YYYY-MM-DD)")
    p.add_argument("--days", type=int, default=4)
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--max-checks", type=int, default=2880)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("history", help="what poolbot has booked and tried")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("extract", help="read API constants out of the app's APK")
    p.add_argument("apk", nargs="+", type=Path)
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="default: endpoints.toml beside config.yaml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_extract)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    resolve_paths(args)
    setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"configuration problem: {exc}", file=sys.stderr)
        return 2
    except (ApiError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
