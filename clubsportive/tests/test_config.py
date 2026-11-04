import datetime as dt

import pytest

from poolbot.config import (
    ConfigError,
    load_config,
    load_endpoints,
    load_env,
    require_env,
)


def write(tmp_path, text, name="config.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


BASE = """
timezone: Europe/Amsterdam
release_offset_hours: 48
match:
  activity_patterns: ["lap swim"]
wishes:
  - name: Tuesday
    weekday: tue
    start: "07:00"
"""


def test_loads_minimal_config(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert cfg.tz.key == "Europe/Amsterdam"
    assert cfg.release_offset == dt.timedelta(hours=48)
    assert [w.name for w in cfg.wishes] == ["Tuesday"]
    assert cfg.wishes[0].weekday == 1
    assert cfg.wishes[0].start == dt.time(7, 0)


def test_missing_file_explains_itself(tmp_path):
    with pytest.raises(ConfigError, match="copy config.example.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_no_wishes_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="no wishes"):
        load_config(write(tmp_path, "timezone: Europe/Amsterdam\nwishes: []\n"))


@pytest.mark.parametrize(
    "broken, match",
    [
        ("  - {name: x, weekday: funday, start: '07:00'}", "not a weekday"),
        ("  - {name: x, weekday: tue, start: 'half seven'}", "HH:MM"),
        ("  - {name: x, weekday: tue, start: '07:00', tolerance_minutes: -5}", ">= 0"),
        ("  - {name: Tuesday, weekday: wed, start: '08:00'}", "duplicate wish name"),
    ],
)
def test_rejects_bad_wishes(tmp_path, broken, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, BASE + broken + "\n"))


def test_rejects_missing_wish_keys(tmp_path):
    text = "timezone: Europe/Amsterdam\nwishes:\n  - {name: x, start: '07:00'}\n"
    with pytest.raises(ConfigError, match="missing required key 'weekday'"):
        load_config(write(tmp_path, text))


def test_rejects_hammering_interval(tmp_path):
    text = BASE + "strike:\n  attempt_interval_ms: 10\n"
    with pytest.raises(ConfigError, match="hammer"):
        load_config(write(tmp_path, text))


def test_rejects_bad_regex_and_timezone(tmp_path):
    with pytest.raises(ConfigError, match="bad regex"):
        load_config(write(tmp_path, BASE.replace('["lap swim"]', '["swim(("]')))
    with pytest.raises(ConfigError, match="unknown timezone"):
        load_config(write(tmp_path, BASE.replace("Europe/Amsterdam", "Mars/Olympus")))


def test_blackout_ranges_and_single_days(tmp_path):
    text = BASE + 'blackouts: ["2026-12-24..2027-01-02", "2026-11-11"]\n'
    cfg = load_config(write(tmp_path, text))
    assert cfg.is_blacked_out(dt.date(2026, 12, 31))
    assert cfg.is_blacked_out(dt.date(2026, 11, 11))
    assert not cfg.is_blacked_out(dt.date(2026, 11, 12))


def test_reversed_blackout_range_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="ends before it starts"):
        load_config(write(tmp_path, BASE + 'blackouts: ["2027-01-02..2026-12-24"]\n'))


def test_wish_lookup_is_case_insensitive(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    assert cfg.wish("tuesday").name == "Tuesday"
    with pytest.raises(ConfigError, match="no wish named"):
        cfg.wish("friday")


def test_match_rules():
    import re

    from poolbot.config import MatchRules

    rules = MatchRules(activity_patterns=(re.compile("lap swim", re.I),), location="Zuidas")
    assert rules.matches_activity("Lap Swim 45min")
    assert not rules.matches_activity("Spinning")
    assert rules.matches_location("Clubsportive Zuidas")
    assert not rules.matches_location("Clubsportive Oost")
    assert MatchRules().matches_activity("anything")  # no patterns = match all


# ----------------------------------------------------------------------- endpoints.toml

GOOD_TOML = """
base_url = "https://api.example.test/"
[headers]
facility_group = "BRANDEDAPPX-1"
[paths]
login = "/v1/login"
slots = "/v1/classes?from={date_from}&to={date_to}"
book = "/v1/classes/{slot_id}/booking"
waitlist = ""
[request]
book = "POST"
[fields]
id = ["theId"]
"""


def test_loads_endpoints(tmp_path):
    ep = load_endpoints(write(tmp_path, GOOD_TOML, "endpoints.toml"))
    assert ep.base_url == "https://api.example.test"  # trailing slash trimmed
    assert ep.path("login") == "/v1/login"
    assert ep.method("book") == "POST"
    assert ep.method("slots") == "GET"  # default
    assert ep.field_names("id") == ("theId",)
    assert ep.field_names("title")  # defaults still present
    assert ep.has_path("book") and not ep.has_path("waitlist")
    assert ep.headers()["x-public-facility-group"] == "BRANDEDAPPX-1"
    assert ep.headers()["x-nox-client-type"] == "APP_V5"


def test_endpoints_placeholders_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="base_url is still a placeholder"):
        load_endpoints(write(tmp_path, GOOD_TOML.replace("https://api.example.test/", "https://REPLACE-ME"), "e.toml"))
    with pytest.raises(ConfigError, match="facility_group is still a placeholder"):
        load_endpoints(write(tmp_path, GOOD_TOML.replace("BRANDEDAPPX-1", "REPLACE-ME"), "e2.toml"))
    with pytest.raises(ConfigError, match="must be https"):
        load_endpoints(write(tmp_path, GOOD_TOML.replace("https://api", "http://api"), "e3.toml"))


def test_missing_path_names_the_fix(tmp_path):
    ep = load_endpoints(write(tmp_path, GOOD_TOML, "endpoints.toml"))
    with pytest.raises(ConfigError, match="extract_apk"):
        ep.path("cancel")


# ------------------------------------------------------------------------------ .env


def test_load_env_does_not_clobber_the_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text('# comment\n\nCS_USERNAME="a@b.c"\nexport CS_PASSWORD=hunter2\n')
    monkeypatch.setenv("CS_USERNAME", "already-set")
    loaded = load_env(path)
    assert loaded == {"CS_USERNAME": "a@b.c", "CS_PASSWORD": "hunter2"}
    import os

    assert os.environ["CS_USERNAME"] == "already-set"
    assert os.environ["CS_PASSWORD"] == "hunter2"
    load_env(path, override=True)
    assert os.environ["CS_USERNAME"] == "a@b.c"


def test_load_env_rejects_junk(tmp_path):
    path = tmp_path / ".env"
    path.write_text("this is not an assignment\n")
    with pytest.raises(ConfigError, match="expected KEY=value"):
        load_env(path)


def test_require_env_lists_everything_missing(monkeypatch):
    monkeypatch.delenv("CS_USERNAME", raising=False)
    monkeypatch.delenv("CS_PASSWORD", raising=False)
    with pytest.raises(ConfigError) as exc:
        require_env("CS_USERNAME", "CS_PASSWORD")
    assert "CS_USERNAME" in str(exc.value) and "CS_PASSWORD" in str(exc.value)
