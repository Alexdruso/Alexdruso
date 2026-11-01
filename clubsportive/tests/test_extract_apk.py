import zipfile

import pytest

from poolbot.extract_apk import bucket_score, extract_strings, render_toml, scan

PLANTED = [
    b"https://api.mysports.example/nox/v2/",
    b"BRANDEDAPPCSPNLDONOTDELETE-7f3c21",
    b"APP_V5",
    b"3.68.0",
    b"/nox/v2/login",
    b"/nox/v2/customer/self",
    b"/nox/v2/classes",
    b"/nox/v2/classes/{slot_id}/booking",
    b"/nox/v2/bookings/{booking_id}/cancel",
    b"/nox/v2/classes/{slot_id}/waitlist",
    b"https://assets.cdn.example/logo.png",
]


@pytest.fixture
def fake_apk(tmp_path):
    path = tmp_path / "clubsportive.apk"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("lib/arm64-v8a/libapp.so", b"\x00".join(PLANTED) + b"\x00")
        z.writestr("classes.dex", b"padding/nox/v2/classes/{slot_id}/booking\x00APP_V5\x003.68.0")
        z.writestr("resources.arsc", b"not scanned")
    return path


@pytest.fixture
def fake_xapk(tmp_path, fake_apk):
    path = tmp_path / "bundle.xapk"
    with zipfile.ZipFile(path, "w") as z:
        z.write(fake_apk, "split_config.arm64_v8a.apk")
        z.writestr("manifest.json", b"{}")
    return path


def test_finds_the_build_constants(fake_apk):
    findings = scan([fake_apk])
    assert findings.facility_groups.most_common(1)[0][0] == "BRANDEDAPPCSPNLDONOTDELETE-7f3c21"
    assert findings.client_types.most_common(1)[0][0] == "APP_V5"
    assert findings.versions.most_common(1)[0][0] == "3.68.0"
    assert findings.best_host == "api.mysports.example"
    assert len(findings.members_scanned) == 2  # the .so and the .dex, not resources.arsc


def test_classifies_every_endpoint(fake_apk):
    buckets = scan([fake_apk]).bucketed_paths()
    assert buckets["login"][0] == "/nox/v2/login"
    assert buckets["me"][0] == "/nox/v2/customer/self"
    assert buckets["slots"][0] == "/nox/v2/classes"
    assert buckets["book"][0] == "/nox/v2/classes/{slot_id}/booking"
    assert buckets["cancel"][0] == "/nox/v2/bookings/{booking_id}/cancel"
    assert buckets["waitlist"][0] == "/nox/v2/classes/{slot_id}/waitlist"


@pytest.mark.parametrize(
    "path, winner",
    [
        ("/v2/classes/{slot_id}/booking", "book"),
        ("/v2/bookings/{booking_id}/cancel", "cancel"),
        ("/v2/classes", "slots"),
        ("/v2/classes/{slot_id}/waitlist", "waitlist"),
        ("/v2/login", "login"),
    ],
)
def test_bucket_scoring_prefers_the_last_segment(path, winner):
    from poolbot.extract_apk import BUCKETS

    scores = {name: bucket_score(path, keywords, name) for name, keywords in BUCKETS}
    assert max(scores, key=lambda k: scores[k]) == winner


def test_renders_a_usable_toml(fake_apk, tmp_path):
    text = render_toml(scan([fake_apk]))
    assert 'base_url = "https://api.mysports.example"' in text
    assert 'facility_group = "BRANDEDAPPCSPNLDONOTDELETE-7f3c21"' in text
    assert 'slots = "/nox/v2/classes?from={date_from}&to={date_to}"' in text
    assert "TODO" not in text.split("# ----")[0]  # nothing guessed in the live section

    out = tmp_path / "endpoints.toml"
    out.write_text(text)
    from poolbot.config import load_endpoints

    ep = load_endpoints(out)  # the generated file must actually load
    assert ep.path("book") == "/nox/v2/classes/{slot_id}/booking"
    assert ep.facility_group.startswith("BRANDEDAPP")


def test_marks_guesses_with_todo(tmp_path):
    path = tmp_path / "bare.apk"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("lib/arm64-v8a/libapp.so", b"nothing useful in here at all, really")
    text = render_toml(scan([path]))
    assert text.count("TODO") >= 4
    assert "REPLACE-ME" in text


def test_descends_into_xapk_bundles(fake_xapk):
    findings = scan([fake_xapk])
    assert findings.best_host == "api.mysports.example"
    assert any("split_config" in m for m in findings.members_scanned)


def test_rejects_a_file_that_is_not_an_archive(tmp_path):
    path = tmp_path / "not.apk"
    path.write_bytes(b"definitely not a zip")
    with pytest.raises(SystemExit, match="not a readable"):
        scan([path])


def test_extract_strings_handles_utf16(tmp_path):
    wide = "https://wide.example/v2/login".encode("utf-16-le")
    assert "https://wide.example/v2/login" in extract_strings(wide)


def test_cli_writes_the_file_and_refuses_to_clobber(fake_apk, tmp_path, capsys):
    from poolbot.extract_apk import main

    out = tmp_path / "endpoints.toml"
    assert main([str(fake_apk), "-o", str(out)]) == 0
    assert out.exists()
    assert main([str(fake_apk), "-o", str(out)]) == 1  # no --force
    assert main([str(fake_apk), "-o", str(out), "--force"]) == 0
    assert "BRANDEDAPP" in capsys.readouterr().out


def test_cli_reports_an_empty_scan(tmp_path, capsys):
    from poolbot.extract_apk import main

    path = tmp_path / "empty.apk"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("resources.arsc", b"x")
    assert main([str(path)]) == 2
    assert "Nothing scannable" in capsys.readouterr().err
