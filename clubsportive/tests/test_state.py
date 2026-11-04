import datetime as dt

from poolbot.state import Booking, State


def when(cfg, day=3, hour=7):
    return dt.datetime(2026, 11, day, hour, 0, tzinfo=cfg.tz)


def test_roundtrips_through_disk(tmp_path, cfg):
    path = tmp_path / "state.json"
    state = State(path=path, tz=cfg.tz)
    state.record_booking(
        Booking(slot_id="7", start=when(cfg), title="Lap Swim", wish="Tue", booking_id="bk1")
    )
    state.record_attempt(wish="Tue", outcome="BOOKED")
    state.save()

    reloaded = State.load(path, cfg.tz)
    assert [b.slot_id for b in reloaded.bookings] == ["7"]
    assert reloaded.bookings[0].booking_id == "bk1"
    assert reloaded.bookings[0].start == when(cfg)
    assert reloaded.attempts[-1]["outcome"] == "BOOKED"


def test_state_file_is_not_world_readable(tmp_path, cfg):
    path = tmp_path / "state.json"
    state = State(path=path, tz=cfg.tz)
    state.save()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_corrupt_state_does_not_crash(tmp_path, cfg):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert State.load(path, cfg.tz).bookings == []


def test_missing_state_starts_empty(tmp_path, cfg):
    assert State.load(tmp_path / "nothing.json", cfg.tz).bookings == []


def test_rebooking_the_same_slot_replaces_the_record(cfg, state):
    state.record_booking(Booking(slot_id="7", start=when(cfg), outcome="BOOKED"))
    state.record_booking(Booking(slot_id="7", start=when(cfg), outcome="WAITLISTED"))
    assert len(state.bookings) == 1
    assert state.bookings[0].outcome == "WAITLISTED"


def test_bookings_are_kept_in_chronological_order(cfg, state):
    state.record_booking(Booking(slot_id="late", start=when(cfg, day=5)))
    state.record_booking(Booking(slot_id="early", start=when(cfg, day=3)))
    assert [b.slot_id for b in state.bookings] == ["early", "late"]


def test_queries(cfg, state):
    state.record_booking(Booking(slot_id="7", start=when(cfg)))
    assert state.already_booked("7") and not state.already_booked("8")
    assert state.has_booking_on(dt.date(2026, 11, 3))
    assert not state.has_booking_on(dt.date(2026, 11, 4))
    assert state.count_in_week_of(when(cfg, day=5)) == 1  # same ISO week
    assert state.count_in_week_of(when(cfg, day=12)) == 0


def test_week_boundary_is_iso(cfg, state):
    state.record_booking(Booking(slot_id="sun", start=when(cfg, day=1)))  # Sunday, week 44
    assert state.count_in_week_of(when(cfg, day=2)) == 0  # Monday starts week 45
    assert state.count_in_week_of(when(cfg, day=1)) == 1


def test_drop_and_forget(cfg, state):
    state.record_booking(Booking(slot_id="7", start=when(cfg)))
    state.record_booking(Booking(slot_id="8", start=when(cfg, day=10)))
    assert state.drop_booking("7") and not state.drop_booking("nope")
    assert state.forget_past(when(cfg, day=20)) == 1
    assert state.bookings == []


def test_attempt_log_is_bounded(cfg, state):
    from poolbot.state import MAX_ATTEMPT_LOG

    for i in range(MAX_ATTEMPT_LOG + 25):
        state.record_attempt(i=i)
    assert len(state.attempts) == MAX_ATTEMPT_LOG
    assert state.attempts[-1]["i"] == MAX_ATTEMPT_LOG + 24
