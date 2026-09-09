from datetime import datetime, time as dtime, timedelta

import pytest

from src.logic import (
    should_mark_signed_out, is_within_grace_period, is_within_break_window,
    BreakWindow, next_attendance_status, AttendanceStatus, classify_day,
    DayClassification, compute_net_present_seconds,
)


# --- should_mark_signed_out ----------------------------------------------------

@pytest.mark.parametrize("gap_minutes,timeout_seconds,expected", [
    (10, 3600, False),
    (61, 3600, True),
    (59, 3600, False),
    (0, 60, False),
])
def test_should_mark_signed_out(gap_minutes, timeout_seconds, expected):
    last_seen = datetime(2026, 9, 6, 9, 0, 0)
    now = last_seen + timedelta(minutes=gap_minutes)
    assert should_mark_signed_out(last_seen, now, timeout_seconds) is expected


def test_should_mark_signed_out_rejects_now_before_last_seen():
    last_seen = datetime(2026, 9, 6, 9, 0, 0)
    now = last_seen - timedelta(minutes=5)
    with pytest.raises(ValueError):
        should_mark_signed_out(last_seen, now, 3600)


# --- is_within_grace_period -----------------------------------------------------

@pytest.mark.parametrize("gap,grace,expected", [
    (0, 600, True),
    (600, 600, True),
    (601, 600, False),
    (5000, 600, False),
])
def test_is_within_grace_period(gap, grace, expected):
    assert is_within_grace_period(gap, grace) is expected


def test_grace_period_rejects_negative_gap():
    with pytest.raises(ValueError):
        is_within_grace_period(-1, 600)


# --- is_within_break_window -----------------------------------------------------

LUNCH = BreakWindow(start=dtime(13, 0), end=dtime(13, 45))
TEA = BreakWindow(start=dtime(16, 0), end=dtime(16, 15))


@pytest.mark.parametrize("t,expected", [
    (dtime(12, 59), False),
    (dtime(13, 0), True),
    (dtime(13, 20), True),
    (dtime(13, 45), True),
    (dtime(13, 46), False),
    (dtime(16, 5), True),
    (dtime(18, 0), False),
])
def test_is_within_break_window(t, expected):
    assert is_within_break_window(t, [LUNCH, TEA]) is expected


def test_no_break_windows_never_matches():
    assert is_within_break_window(dtime(13, 0), []) is False


# --- next_attendance_status (the full state machine) ----------------------------

def test_signed_out_stays_signed_out_with_no_detection():
    status = next_attendance_status(
        AttendanceStatus.SIGNED_OUT, detected_now=False, current_time=dtime(10, 0),
        break_windows=[], gap_seconds=0, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.SIGNED_OUT


def test_first_detection_transitions_to_present():
    status = next_attendance_status(
        AttendanceStatus.SIGNED_OUT, detected_now=True, current_time=dtime(9, 15),
        break_windows=[], gap_seconds=0, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.PRESENT


def test_break_window_forces_on_break_even_if_detected():
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=True, current_time=dtime(13, 10),
        break_windows=[LUNCH], gap_seconds=0, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.ON_BREAK


def test_break_window_forces_on_break_even_without_detection():
    # Scheduled breaks are time-based, not detection-based -- must pause
    # regardless of whether the employee happens to be seen during it.
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=False, current_time=dtime(13, 10),
        break_windows=[LUNCH], gap_seconds=2700, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.ON_BREAK


def test_short_gap_within_grace_period_holds_present():
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=False, current_time=dtime(11, 0),
        break_windows=[], gap_seconds=300, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.PRESENT


def test_long_gap_beyond_timeout_signs_out():
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=False, current_time=dtime(11, 0),
        break_windows=[], gap_seconds=4000, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.SIGNED_OUT


def test_gap_between_grace_and_timeout_holds_current_status():
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=False, current_time=dtime(11, 0),
        break_windows=[], gap_seconds=1800, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.PRESENT


def test_department_move_scenario_does_not_sign_out():
    # Detected on a different camera/department -> still "detected_now=True"
    # for the org-wide timer, regardless of which camera saw them.
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=True, current_time=dtime(11, 0),
        break_windows=[], gap_seconds=0, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.PRESENT


def test_reappearing_after_break_window_ends_goes_back_to_present():
    status = next_attendance_status(
        AttendanceStatus.ON_BREAK, detected_now=True, current_time=dtime(13, 50),
        break_windows=[LUNCH], gap_seconds=0, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.PRESENT


@pytest.mark.parametrize("current_time", [dtime(9, 0), dtime(12, 59), dtime(13, 46), dtime(17, 0)])
def test_outside_break_windows_break_does_not_apply(current_time):
    status = next_attendance_status(
        AttendanceStatus.PRESENT, detected_now=True, current_time=current_time,
        break_windows=[LUNCH], gap_seconds=0, grace_period_seconds=600, away_timeout_seconds=3600,
    )
    assert status == AttendanceStatus.PRESENT


# --- classify_day ----------------------------------------------------------------

@pytest.mark.parametrize("seconds,full,half,expected", [
    (8 * 3600, 7 * 3600, 4 * 3600, DayClassification.FULL),
    (5 * 3600, 7 * 3600, 4 * 3600, DayClassification.HALF),
    (1 * 3600, 7 * 3600, 4 * 3600, DayClassification.ABSENT),
    (0, 7 * 3600, 4 * 3600, DayClassification.ABSENT),
    (7 * 3600, 7 * 3600, 4 * 3600, DayClassification.FULL),
    (4 * 3600, 7 * 3600, 4 * 3600, DayClassification.HALF),
])
def test_classify_day(seconds, full, half, expected):
    assert classify_day(seconds, full, half) == expected


def test_classify_day_rejects_negative_seconds():
    with pytest.raises(ValueError):
        classify_day(-1, 7 * 3600, 4 * 3600)


# --- compute_net_present_seconds --------------------------------------------------

def test_compute_net_present_seconds_subtracts_breaks():
    sign_in = datetime(2026, 9, 6, 9, 0, 0)
    sign_out = datetime(2026, 9, 6, 18, 0, 0)
    net = compute_net_present_seconds(sign_in, sign_out, break_seconds=45 * 60)
    assert net == 9 * 3600 - 45 * 60


def test_compute_net_present_seconds_never_negative():
    sign_in = datetime(2026, 9, 6, 9, 0, 0)
    sign_out = datetime(2026, 9, 6, 9, 10, 0)
    net = compute_net_present_seconds(sign_in, sign_out, break_seconds=3600)
    assert net == 0


def test_compute_net_present_seconds_rejects_sign_out_before_sign_in():
    sign_in = datetime(2026, 9, 6, 18, 0, 0)
    sign_out = datetime(2026, 9, 6, 9, 0, 0)
    with pytest.raises(ValueError):
        compute_net_present_seconds(sign_in, sign_out, 0)


@pytest.mark.parametrize("hours,break_minutes", [(8, 0), (8, 30), (8, 60), (12, 45), (4, 15)])
def test_compute_net_present_seconds_various_shift_lengths(hours, break_minutes):
    sign_in = datetime(2026, 9, 6, 9, 0, 0)
    sign_out = sign_in + timedelta(hours=hours)
    net = compute_net_present_seconds(sign_in, sign_out, break_minutes * 60)
    assert net == hours * 3600 - break_minutes * 60
