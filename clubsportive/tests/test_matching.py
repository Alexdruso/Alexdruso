import datetime as dt

from poolbot.matching import blocked_reason, candidates_for, choose, pool_slots
from poolbot.state import Booking


def target(cfg, hour=7, minute=0, day=3):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=cfg.tz)


def test_filters_by_activity_and_location(cfg, pool_slot):
    slots = [
        pool_slot("a", title="Lap Swim"),
        pool_slot("b", title="Spinning"),
        pool_slot("c", title="Lap Swim", location="Trainmore Oost"),
    ]
    assert [s.id for s in pool_slots(cfg, slots)] == ["a"]


def test_dutch_titles_match(cfg, pool_slot):
    assert [s.id for s in pool_slots(cfg, [pool_slot("a", title="Banenzwemmen")])] == ["a"]


def test_closest_to_the_wished_time_wins(cfg, pool_slot):
    wish = cfg.wish("Tuesday early swim")
    slots = [
        pool_slot("early", start=target(cfg, 6, 45)),
        pool_slot("exact", start=target(cfg, 7, 0)),
        pool_slot("late", start=target(cfg, 7, 30)),
    ]
    ranked = candidates_for(cfg, wish, target(cfg), slots)
    assert [c.slot.id for c in ranked] == ["exact", "early", "late"]
    assert ranked[1].delta_minutes == -15


def test_tolerance_is_a_hard_boundary(cfg, pool_slot):
    wish = cfg.wish("Tuesday early swim")  # 07:00 +/- 30 min
    inside = pool_slot("in", start=target(cfg, 7, 30))
    outside = pool_slot("out", start=target(cfg, 7, 31))
    ids = [c.slot.id for c in candidates_for(cfg, wish, target(cfg), [inside, outside])]
    assert ids == ["in"]


def test_other_days_are_ignored(cfg, pool_slot):
    wish = cfg.wish("Tuesday early swim")
    wrong_day = pool_slot("x", start=target(cfg, 7, 0, day=4))
    assert candidates_for(cfg, wish, target(cfg), [wrong_day]) == []


def test_slots_without_a_start_are_skipped(cfg, pool_slot):
    wish = cfg.wish("Tuesday early swim")
    assert candidates_for(cfg, wish, target(cfg), [pool_slot("x", start=None)]) == []


def test_choose_explains_an_empty_schedule(cfg, state):
    wish = cfg.wish("Tuesday early swim")
    slot, why = choose(cfg, wish, target(cfg), [], state)
    assert slot is None
    assert "no slot matching" in why and "+/-30 min" in why


def test_choose_reports_the_chosen_slot(cfg, state, pool_slot):
    wish = cfg.wish("Tuesday early swim")
    slot, why = choose(cfg, wish, target(cfg), [pool_slot("a", start=target(cfg, 6, 45))], state)
    assert slot.id == "a"
    assert "-15 min from wish" in why


def test_already_booked_slots_are_skipped_in_favour_of_the_next(cfg, state, pool_slot):
    """With one_per_day off, a slot we already hold is skipped and the runner-up is taken."""
    import dataclasses

    from poolbot.config import Limits

    relaxed = dataclasses.replace(cfg, limits=Limits(max_bookings_per_week=3, one_per_day=False))
    wish = relaxed.wish("Tuesday early swim")
    state.record_booking(Booking(slot_id="exact", start=target(cfg)))
    slots = [pool_slot("exact", start=target(cfg)), pool_slot("early", start=target(cfg, 6, 45))]
    slot, _ = choose(relaxed, wish, target(cfg), slots, state)
    assert slot.id == "early"


def test_one_per_day_also_blocks_the_runner_up(cfg, state, pool_slot):
    """With one_per_day on (the default), holding any slot that day blocks the rest."""
    wish = cfg.wish("Tuesday early swim")
    state.record_booking(Booking(slot_id="exact", start=target(cfg)))
    slots = [pool_slot("exact", start=target(cfg)), pool_slot("early", start=target(cfg, 6, 45))]
    slot, why = choose(cfg, wish, target(cfg), slots, state)
    assert slot is None and "one_per_day" in why


def test_one_per_day_blocks_a_second_slot(cfg, state, pool_slot):
    state.record_booking(Booking(slot_id="other", start=target(cfg, 20, 0)))
    reason = blocked_reason(cfg, state, pool_slot("a", start=target(cfg)), target(cfg))
    assert "one_per_day" in reason


def test_weekly_cap_blocks(cfg, state, pool_slot):
    for i, day in enumerate((2, 4, 6)):  # three in the same ISO week, cap is 3
        state.record_booking(Booking(slot_id=f"b{i}", start=target(cfg, 7, 0, day=day)))
    reason = blocked_reason(cfg, state, pool_slot("a", start=target(cfg)), target(cfg))
    assert "weekly cap reached (3/3" in reason


def test_weekly_cap_counts_only_the_same_week(cfg, state, pool_slot):
    for i, day in enumerate((9, 10, 11)):  # next ISO week
        state.record_booking(Booking(slot_id=f"b{i}", start=target(cfg, 7, 0, day=day)))
    assert blocked_reason(cfg, state, pool_slot("a", start=target(cfg)), target(cfg)) is None


def test_blackout_blocks(cfg, state, pool_slot):
    day = dt.datetime(2026, 12, 29, 7, 0, tzinfo=cfg.tz)
    reason = blocked_reason(cfg, state, pool_slot("a", start=day), day)
    assert "blackout" in reason


def test_cancelled_bookings_stop_counting(cfg, state, pool_slot):
    state.record_booking(Booking(slot_id="gone", start=target(cfg, 20, 0), outcome="CANCELLED"))
    assert blocked_reason(cfg, state, pool_slot("a", start=target(cfg)), target(cfg)) is None


def test_choose_reports_why_every_candidate_was_blocked(cfg, state, pool_slot):
    wish = cfg.wish("Tuesday early swim")
    state.record_booking(Booking(slot_id="a", start=target(cfg)))
    slot, why = choose(cfg, wish, target(cfg), [pool_slot("a", start=target(cfg))], state)
    assert slot is None
    assert "every candidate is blocked" in why and "already booked" in why
