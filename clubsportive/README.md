# poolbot — Clubsportive pool slot sniper

Pool slots at Clubsportive open exactly 48 hours before the session and are gone in seconds.
`poolbot` sits on a machine you leave running, wakes up a minute before the release, and sends
the booking request the instant the slot opens — against the *club's* clock, not yours.

```
$ poolbot plan
now Sun 01 Nov 15:44:09 CET

wish                     session                books at               status
Tuesday early swim       Tue 03 Nov 07:00      Sun 01 Nov 07:00:00      release passed
Thursday evening swim    Thu 05 Nov 19:00      Tue 03 Nov 19:00:00      in 2d 3h
Saturday morning swim    Sat 07 Nov 09:30      Thu 05 Nov 09:30:00      in 3d 17h
```

## How it works

The Clubsportive app (`com.mysports.branded.clubsportive`) is a white-label build of the
**MySports / Sport Alliance (Magicline)** platform, the same codebase as the Trainmore app.
There is no web booking portal, so poolbot speaks the app's own HTTP API:

* `POST` the credentials with HTTP Basic auth *and* in the JSON body;
* the response carries a UUID which goes in an `x-auth-token` header from then on (it does
  not expire, so poolbot caches it);
* three build constants ride along on every request: `x-public-facility-group`,
  `x-nox-client-type: APP_V5`, `x-nox-client-version`.

**The API is not documented, and this code has not yet been run against the real one.** Every
URL, HTTP verb and JSON field name lives in `endpoints.toml`, which step 2 generates from the
APK. If a guess in there is wrong, you change a line of config, not code.

Two details decide whether you win the race:

* **absolute release times** — the release instant is `session start − 48h` computed in UTC, so
  a Europe/Amsterdam DST change between booking and swimming does not shift the strike by an
  hour (it really does move the wall-clock release time: a 09:30 CET session on 25 October
  2026 opens at **10:30** CEST on the 23rd);
* **the server's clock** — poolbot reads the API's `Date` header a minute before the release
  and fires relative to that, so a laptop running half a second slow does not cost you the
  slot. An implausible offset (over 2 minutes, i.e. a cached `Date` header) is ignored.

## 1. Install

Python 3.11+ on a machine that stays awake (small VPS, Raspberry Pi, always-on desktop).

```bash
git clone <this repo> /opt/poolbot && cd /opt/poolbot/clubsportive
python3 -m venv /opt/poolbot/.venv
/opt/poolbot/.venv/bin/pip install -e .          # gives you the `poolbot` command
# or, without installing: pip install -r requirements.txt && python -m poolbot ...
```

Make sure the clock is synchronised — `timedatectl` should say
`System clock synchronized: yes`. poolbot corrects for skew, but starting from NTP is better.

## 2. Discover the API (once)

Get the APK, then let poolbot read the constants out of it. Either download it (APKMirror,
APKPure — search `com.mysports.branded.clubsportive`) or pull it off your own phone:

```bash
adb shell pm path com.mysports.branded.clubsportive
adb pull /data/app/.../base.apk
adb pull /data/app/.../split_config.arm64_v8a.apk    # the Flutter strings live in here
```

```bash
poolbot extract base.apk split_config.arm64_v8a.apk -o endpoints.toml
```

It prints what it found — the `BRANDEDAPP…` facility group, the API host, and its best guess
for each endpoint — and writes `endpoints.toml` with every other candidate path appended as
comments. **Read the file and fix anything marked `# TODO`.** `.xapk`/`.apks` bundles work
too; nothing is unpacked to disk and nothing from the APK is executed.

If the APK gives you the constants but the endpoint paths look wrong, a ten-minute traffic
capture settles it: run [HTTP Toolkit](https://httptoolkit.com/) or mitmproxy against the app,
book something by hand, and copy the real paths into `endpoints.toml`. There is no certificate
pinning, so a proxy works without patching the app.

## 3. Configure

```bash
cp config.example.yaml config.yaml && $EDITOR config.yaml   # wishes, limits, strike timing
cp .env.example .env && chmod 600 .env && $EDITOR .env      # credentials + SMTP
```

`config.yaml` holds your weekly wishes in priority order, each with a tolerance (take 06:45 if
07:00 does not exist that week), plus `limits` (`max_bookings_per_week`, `one_per_day`),
`blackouts` for holidays, and the `strike` timings. `.env` holds the account credentials and
SMTP settings; both files, and `state.json`, are gitignored.

## 4. Verify before trusting it

Read paths first, then a write on a slot nobody wants, and only then the real thing.

```bash
poolbot doctor                      # config + login + profile + clock skew + next strike
poolbot slots --days 3              # the pool schedule as the API sees it
poolbot slots --days 1 --raw        # raw JSON, for fixing [fields] in endpoints.toml
poolbot plan                        # which wish, which session, exact release instant
poolbot --dry-run book --slot-id X  # everything except the request
poolbot book --slot-id X            # a real booking on a quiet slot...
poolbot cancel --slot-id X          # ...released again straight away
pytest                              # 169 logic tests, no network
```

If `slots` comes back empty but `--all` shows sessions, your `match.activity_patterns` do not
match the real titles. If `--all` is empty too, the `[fields]` key names in `endpoints.toml`
do not match the real JSON — `--raw` shows you the truth, and you add the right spelling to
the front of the relevant list.

### Calibrate the 48 hours before going live

The 48-hour window is what the club says, not something this code has measured. Confirm it:

```bash
poolbot watch --slot-id <a not-yet-open slot> --interval 60
```

It polls until the slot flips to bookable and prints the measured offset. Do that on two or
three slots; if the real figure differs (for instance everything is published at midnight for
the whole day), set `release_offset_hours` to what you measured. When the API itself reports a
`bookableFrom` time, poolbot trusts that over the config and says so in the log.

## 5. Run it

```bash
cp systemd/poolbot.service /etc/systemd/system/   # edit User= and the paths first
systemctl daemon-reload && systemctl enable --now poolbot
journalctl -fu poolbot
```

One long-lived process: it computes the next release across all wishes, sleeps until a minute
before, strikes, and repeats. It re-reads `config.yaml` each round, so editing your wishes does
not need a restart. A crash in one strike does not kill the daemon.

Prefer cron? `poolbot run-due --within 900` books anything opening in the next 15 minutes, so
`*/10 * * * *` works — a timer firing a few minutes late is exactly what the daemon avoids,
though, which is why it is the default.

`poolbot snipe --wish "Saturday morning swim"` runs a single strike in the foreground, which is
the easiest way to watch one happen. Add `--now` to skip the wait and book immediately.

## Email

Every strike sends one mail: outcome, slot, attempt count, how long it took, and the API's own
message on failure, with a calendar invite attached when it worked. Set `SMTP_*` in `.env`
(Gmail needs an [App Password](https://myaccount.google.com/apppasswords), not your login
password). `notify.on_success` / `notify.on_failure` turn each off, and no SMTP config at all
means results only go to the log.

## Being a decent member about it

This books one spot on your own paid membership — nothing more. Keep it that way:

* one account, one slot per session, no second identity;
* `attempt_interval_ms` defaults to 250 ms with a hard 60-attempt cap, which is about as much
  traffic as a person refreshing the app. Don't lower it;
* **cancel what you cannot use.** `poolbot cancel --slot-id X` puts the slot back in the pool
  and keeps you clear of Clubsportive's no-show penalty. A bot that hoards slots it does not
  swim is the thing that gets booking automation banned for everyone;
* automating the app is very likely against Clubsportive's terms of service. The realistic
  risk is a rate limit or a sternly worded email, but it is your membership on the line.

## Layout

| path | what it is |
| --- | --- |
| `poolbot/extract_apk.py` | reads the API constants out of the APK |
| `poolbot/client.py` | auth, schedule, book/cancel, clock sampling, response normalising |
| `poolbot/config.py` | `config.yaml`, `endpoints.toml`, `.env` loading and validation |
| `poolbot/matching.py` | wish → concrete slot, and the limit checks |
| `poolbot/scheduler.py` | release arithmetic, the strike loop, the daemon, `watch` |
| `poolbot/state.py` | booking history, weekly caps, idempotency |
| `poolbot/notify.py` | result mail and the `.ics` invite |
| `poolbot/cli.py` | the `poolbot` command |
| `endpoints.example.toml` | the API description template, with field-name candidates |
| `systemd/poolbot.service` | hardened unit for the daemon |
