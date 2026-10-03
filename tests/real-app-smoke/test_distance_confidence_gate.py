"""
Tests the face-width confidence gate now actually wired into
decide_match_status() (app/logic.py). This was previously built
(classify_confidence_zone, face_width_px_at_distance) but never connected
to the live decision function -- these tests exercise the gate itself,
independent of any vision/DB code, so they need no real YOLO/InsightFace
model and run anywhere.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.logic import decide_match_status, MatchStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Backward compatibility: omitting face_width_px reproduces prior behavior
# exactly -- this must not change for any existing caller that hasn't been
# updated to pass a width yet.
# ---------------------------------------------------------------------------

def test_omitting_face_width_px_is_unchanged_from_prior_behavior():
    assert decide_match_status(True, 0.5, 10.0, True) == MatchStatus.MATCH
    assert decide_match_status(True, 0.5, 10.0, False) == MatchStatus.MISMATCH
    assert decide_match_status(True, 0.3, 10.0, True) == MatchStatus.UNKNOWN  # below default 0.45


def test_face_width_px_none_is_the_same_as_omitting_it():
    assert decide_match_status(True, 0.5, 10.0, True, face_width_px=None) == MatchStatus.MATCH


# ---------------------------------------------------------------------------
# Reliable zone (>=80px): behaves exactly like the no-width-gate path.
# ---------------------------------------------------------------------------

def test_reliable_zone_match_at_default_threshold():
    assert decide_match_status(True, 0.5, 10.0, True, face_width_px=80) == MatchStatus.MATCH


def test_reliable_zone_mismatch_when_not_assigned():
    assert decide_match_status(True, 0.5, 10.0, False, face_width_px=120) == MatchStatus.MISMATCH


def test_reliable_zone_below_default_threshold_is_unknown():
    assert decide_match_status(True, 0.40, 10.0, True, face_width_px=100) == MatchStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Degraded zone (50-79px): the default 0.45 similarity is NOT enough
# anymore -- this is the actual fix for "two different people both match
# the same stored employee" once their faces are small/distant.
# ---------------------------------------------------------------------------

def test_degraded_zone_rejects_similarity_that_would_pass_at_full_size():
    # 0.50 clears the old flat 0.45 threshold but must NOT clear the
    # stricter 0.60 the degraded zone now requires.
    assert decide_match_status(True, 0.50, 10.0, True, face_width_px=65) == MatchStatus.UNKNOWN


def test_degraded_zone_matches_once_similarity_clears_the_stricter_bar():
    assert decide_match_status(True, 0.65, 10.0, True, face_width_px=65) == MatchStatus.MATCH


def test_degraded_zone_boundary_at_exactly_80px_is_reliable_not_degraded():
    # classify_confidence_zone treats 80 as the reliable boundary (>=80).
    assert decide_match_status(True, 0.50, 10.0, True, face_width_px=80) == MatchStatus.MATCH


def test_degraded_zone_boundary_at_exactly_50px_is_degraded_not_unreliable():
    assert decide_match_status(True, 0.99, 10.0, True, face_width_px=50) == MatchStatus.MATCH
    assert decide_match_status(True, 0.50, 10.0, True, face_width_px=50) == MatchStatus.UNKNOWN


def test_degraded_zone_never_loosens_a_custom_stricter_sim_threshold():
    # If a caller already passed a sim_threshold stricter than 0.60, the
    # degraded zone must not loosen it back down to 0.60.
    status = decide_match_status(True, 0.65, 10.0, True, sim_threshold=0.70, face_width_px=65)
    assert status == MatchStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Unreliable zone (<50px): always UNKNOWN, no matter how high the raw
# similarity looks -- this is the core of the fix, since a tiny/blurry
# crop can coincidentally score very high against the wrong person.
# ---------------------------------------------------------------------------

def test_unreliable_zone_is_always_unknown_even_at_near_perfect_similarity():
    assert decide_match_status(True, 0.99, 50.0, True, face_width_px=20) == MatchStatus.UNKNOWN


def test_unreliable_zone_boundary_just_under_50px():
    assert decide_match_status(True, 0.99, 50.0, True, face_width_px=49.9) == MatchStatus.UNKNOWN


# ---------------------------------------------------------------------------
# The width gate must not bypass the existing VACANT / missing-data /
# SNR early-outs -- it only ever makes the decision stricter, never looser.
# ---------------------------------------------------------------------------

def test_not_occupied_is_vacant_regardless_of_face_width():
    assert decide_match_status(False, 0.99, 50.0, True, face_width_px=200) == MatchStatus.VACANT


def test_missing_similarity_is_unknown_regardless_of_face_width():
    assert decide_match_status(True, None, None, True, face_width_px=200) == MatchStatus.UNKNOWN


def test_reliable_zone_still_respects_the_snr_gate():
    # High similarity, large face, but SNR below threshold -- must still
    # be UNKNOWN. The width gate only adds restrictions, it doesn't
    # remove the existing ones.
    assert decide_match_status(True, 0.99, 1.0, True, face_width_px=200, snr_threshold=3.0) == MatchStatus.UNKNOWN
