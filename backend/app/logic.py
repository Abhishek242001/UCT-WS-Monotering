"""
Core business logic for the Employee Identity-Aware Workstation Monitoring
& Attendance System.

These are deliberately pure, dependency-light functions extracted from the
project's design discussions (ADAR distance-adaptive face recognition,
the attendance state machine, and activity classification). They are unit
tested directly (see tests/test_logic_*.py) independent of any web
framework or database, so the core algorithms can be verified in isolation.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Face recognition: distance estimation, decay/SNR gate, confidence zones
# ---------------------------------------------------------------------------

class MatchStatus(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"
    VACANT = "VACANT"


def estimate_depth_from_facewidth(live_width_px: float, ref_width_px: float, ref_depth_m: float) -> float:
    """Pinhole-camera approximation: apparent width is inversely proportional
    to distance, so depth scales with the ratio of reference to live width.
    """
    if live_width_px <= 0:
        raise ValueError("live_width_px must be positive")
    if ref_width_px <= 0 or ref_depth_m <= 0:
        raise ValueError("ref_width_px and ref_depth_m must be positive")
    return ref_depth_m * (ref_width_px / live_width_px)


def alpha_decay(relative_depth_m: float, k: float) -> float:
    """Similarity decay factor as a function of distance beyond the
    reference depth. alpha(0) == 1 (no correction needed at the reference
    distance); alpha decreases as relative_depth grows.
    """
    if relative_depth_m < 0:
        raise ValueError("relative_depth_m must be >= 0")
    return math.exp(-k * relative_depth_m)


def noise_sigma(relative_depth_m: float, sigma0: float, gamma: float) -> float:
    """Linear noise-growth model: noise increases with distance."""
    if sigma0 < 0 or gamma < 0:
        raise ValueError("sigma0 and gamma must be >= 0")
    return sigma0 * (1.0 + gamma * max(relative_depth_m, 0.0))


def signal_to_noise_ratio(similarity: float, relative_depth_m: float, k: float, sigma0: float, gamma: float) -> float:
    """SNR = corrected signal strength / estimated noise at this distance."""
    signal = similarity * alpha_decay(relative_depth_m, k)
    noise = noise_sigma(relative_depth_m, sigma0, gamma)
    if noise <= 0:
        raise ValueError("noise must be positive")
    return signal / noise


def cosine_similarity(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        raise ValueError("zero-norm vector")
    return dot / (norm_a * norm_b)


def decide_match_status(
    occupancy_present: bool,
    best_similarity: Optional[float],
    best_snr: Optional[float],
    is_assigned_employee: Optional[bool],
    sim_threshold: float = 0.45,
    snr_threshold: float = 3.0,
) -> MatchStatus:
    """Central decision function for the four workstation identity states.
    Kept deliberately conservative: a low SNR always yields UNKNOWN, even
    if the raw similarity looks superficially high, per the "honesty over
    false confidence" design principle documented for ADAR.
    """
    if not occupancy_present:
        return MatchStatus.VACANT
    if best_similarity is None or best_snr is None:
        return MatchStatus.UNKNOWN
    if best_similarity < sim_threshold or best_snr < snr_threshold:
        return MatchStatus.UNKNOWN
    return MatchStatus.MATCH if is_assigned_employee else MatchStatus.MISMATCH


# --- Camera placement / confidence-zone helpers -----------------------------

def slant_distance(mount_height_m: float, horizontal_offset_m: float) -> float:
    """Pythagorean slant distance from a fixed-height camera to a point on
    the floor at a given horizontal offset."""
    if mount_height_m < 0 or horizontal_offset_m < 0:
        raise ValueError("distances must be >= 0")
    return math.sqrt(mount_height_m ** 2 + horizontal_offset_m ** 2)


def face_width_px_at_distance(real_face_width_m: float, focal_length_px: float, distance_m: float) -> float:
    if distance_m <= 0:
        raise ValueError("distance_m must be positive")
    return (real_face_width_m * focal_length_px) / distance_m


def focal_length_px(sensor_width_px: float, horizontal_fov_deg: float) -> float:
    return sensor_width_px / (2 * math.tan(math.radians(horizontal_fov_deg) / 2))


def classify_confidence_zone(face_width_px: float) -> str:
    """Buckets matching the guidance table developed during camera-placement
    analysis: reliable / usable-degraded / unreliable."""
    if face_width_px >= 80:
        return "reliable"
    if face_width_px >= 50:
        return "degraded"
    return "unreliable"


# --- Single-pass gallery matching (the O(n) complexity fix) -----------------

def group_gallery_by_person(gallery: dict) -> dict:
    """gallery keys look like '<person_id>__<view>'. Groups into
    {person_id: [(view, embedding), ...]} in a single pass, replacing the
    earlier O(persons x gallery_size) nested-scan implementation."""
    grouped: dict = {}
    for key, embedding in gallery.items():
        person_id, _, view = key.partition("__")
        grouped.setdefault(person_id, []).append((view, embedding))
    return grouped


def match_single_pass(corrected_embedding, grouped_gallery: dict):
    """Returns (person_id, view, similarity) for the best match, or None if
    the gallery is empty. O(gallery_size) total, not O(persons x gallery)."""
    best = None
    for person_id, views in grouped_gallery.items():
        for view, ref_embedding in views:
            sim = cosine_similarity(corrected_embedding, ref_embedding)
            if best is None or sim > best[2]:
                best = (person_id, view, sim)
    return best


# ---------------------------------------------------------------------------
# Attendance state machine
# ---------------------------------------------------------------------------

class AttendanceStatus(str, Enum):
    SIGNED_OUT = "SIGNED_OUT"
    PRESENT = "PRESENT"
    ON_BREAK = "ON_BREAK"


class DayClassification(str, Enum):
    FULL = "FULL"
    HALF = "HALF"
    ABSENT = "ABSENT"


def should_mark_signed_out(last_seen_at: datetime, now: datetime, away_timeout_seconds: int) -> bool:
    if now < last_seen_at:
        raise ValueError("now must not be before last_seen_at")
    return (now - last_seen_at).total_seconds() > away_timeout_seconds


def is_within_grace_period(gap_seconds: float, grace_period_seconds: int) -> bool:
    if gap_seconds < 0:
        raise ValueError("gap_seconds must be >= 0")
    return gap_seconds <= grace_period_seconds


@dataclass(frozen=True)
class BreakWindow:
    start: dtime
    end: dtime


def is_within_break_window(current_time: dtime, windows: list[BreakWindow]) -> bool:
    for w in windows:
        if w.start <= current_time <= w.end:
            return True
    return False


def next_attendance_status(
    current_status: AttendanceStatus,
    detected_now: bool,
    current_time: dtime,
    break_windows: list[BreakWindow],
    gap_seconds: float,
    grace_period_seconds: int,
    away_timeout_seconds: int,
) -> AttendanceStatus:
    """One step of the attendance state machine described in the
    documentation: SIGNED_OUT -> PRESENT -> ON_BREAK -> PRESENT -> SIGNED_OUT.
    Scheduled breaks are time-based and override detection; unscheduled gaps
    only cause a sign-out once they exceed the away-timeout, with a grace
    period absorbing short blind-spot gaps first.
    """
    if is_within_break_window(current_time, break_windows):
        return AttendanceStatus.ON_BREAK

    if detected_now:
        return AttendanceStatus.PRESENT

    if current_status == AttendanceStatus.SIGNED_OUT:
        return AttendanceStatus.SIGNED_OUT

    if is_within_grace_period(gap_seconds, grace_period_seconds):
        return current_status  # ignore short blind-spot gaps

    if gap_seconds > away_timeout_seconds:
        return AttendanceStatus.SIGNED_OUT

    return current_status


def classify_day(net_present_seconds: int, full_day_threshold_seconds: int, half_day_threshold_seconds: int) -> DayClassification:
    if net_present_seconds < 0:
        raise ValueError("net_present_seconds must be >= 0")
    if net_present_seconds >= full_day_threshold_seconds:
        return DayClassification.FULL
    if net_present_seconds >= half_day_threshold_seconds:
        return DayClassification.HALF
    return DayClassification.ABSENT


def compute_net_present_seconds(sign_in: datetime, sign_out: datetime, break_seconds: int) -> int:
    if sign_out < sign_in:
        raise ValueError("sign_out must not be before sign_in")
    total = int((sign_out - sign_in).total_seconds())
    return max(total - break_seconds, 0)


# ---------------------------------------------------------------------------
# Activity classification (5 classes + UNKNOWN)
# ---------------------------------------------------------------------------

class Activity(str, Enum):
    SITTING = "SITTING"
    STANDING = "STANDING"
    WALKING = "WALKING"
    LEANING = "LEANING"
    TRANSITIONING = "TRANSITIONING"
    UNKNOWN = "UNKNOWN"


MIN_KEYPOINT_CONFIDENCE = 0.5


def classify_activity(keypoint_confidence: dict, torso_angle_deg: float, is_moving: bool, min_conf: float = MIN_KEYPOINT_CONFIDENCE) -> Activity:
    """Rule-based activity classifier operating on a single sample's pose
    keypoints. Deliberately conservative: any required keypoint below
    min_conf falls back to UNKNOWN rather than guessing, consistent with
    the project's "honesty over false confidence" principle applied to
    face recognition as well.

    keypoint_confidence keys expected: 'hip', 'knee', 'ankle', 'shoulder'.
    torso_angle_deg: 0 == perfectly vertical torso.
    """
    required = ("hip", "knee", "ankle", "shoulder")
    if any(keypoint_confidence.get(k, 0.0) < min_conf for k in required):
        return Activity.UNKNOWN

    if is_moving:
        return Activity.WALKING

    if abs(torso_angle_deg) > 30:
        return Activity.LEANING

    if 10 < abs(torso_angle_deg) <= 30:
        return Activity.TRANSITIONING

    # torso roughly vertical; distinguish sitting vs standing by knee angle
    # (a fuller implementation would use knee-hip-ankle joint angle; kept
    # simple here since this function is a rule-based baseline, not the
    # final production classifier).
    return Activity.SITTING if keypoint_confidence.get("seated_hint", 0.0) >= 0.5 else Activity.STANDING


# ---------------------------------------------------------------------------
# Adapting a real detector's keypoints (yolo_detector.PersonDetection,
# COCO's left/right-named pairs) into the joint-level inputs
# classify_activity() above expects. Kept here, not in yolo_detector.py,
# so this module stays free of any dependency on the vision layer -- it
# takes a plain dict, same as classify_activity() itself.
# ---------------------------------------------------------------------------

def combine_side_pair(keypoints: dict, left_name: str, right_name: str) -> tuple[float, float, float]:
    """Returns (x, y, confidence) for a joint reported as a COCO
    left/right pair -- same (x, y, confidence) order PersonDetection.keypoints
    itself uses -- picking whichever side has higher confidence: either
    side being visible is enough evidence the joint exists (a desk, an
    off-angle pose, or the camera's own viewpoint commonly occludes one
    side but not the other), and using ONE side's actual position avoids
    averaging a confident position together with a low-confidence,
    potentially meaningless one. Missing keys default to (0.0, 0.0, 0.0)."""
    left = keypoints.get(left_name, (0.0, 0.0, 0.0))
    right = keypoints.get(right_name, (0.0, 0.0, 0.0))
    return left if left[2] >= right[2] else right


def activity_inputs_from_coco_keypoints(keypoints: dict) -> tuple[dict, float]:
    """Converts the 17 COCO-named keypoints yolo_detector.PersonDetection
    carries into the (keypoint_confidence, torso_angle_deg) inputs
    classify_activity() expects, which are joint-level (one shoulder, one
    hip, ...), not side-level.

    torso_angle_deg is measured from the shoulder-midpoint to
    hip-midpoint vector, 0 == perfectly vertical (matching
    classify_activity()'s documented convention), using image
    coordinates where y increases downward.

    Known, deliberately unaddressed limitation: this does NOT compute a
    'seated_hint' -- no validated geometric heuristic for it exists yet
    (a hip/knee-angle-based guess would be unvalidated against real
    footage, and per point 93 knee/ankle keypoints are usually
    desk-occluded for a seated employee anyway, giving little to
    validate it against). Until one is added and validated,
    classify_activity() will resolve an upright, stationary person to
    STANDING rather than SITTING even when actually seated -- this is a
    known gap, not a silently-accepted wrong answer.
    """
    shoulder_x, shoulder_y, shoulder_c = combine_side_pair(keypoints, "left_shoulder", "right_shoulder")
    hip_x, hip_y, hip_c = combine_side_pair(keypoints, "left_hip", "right_hip")
    _, _, knee_c = combine_side_pair(keypoints, "left_knee", "right_knee")
    _, _, ankle_c = combine_side_pair(keypoints, "left_ankle", "right_ankle")

    keypoint_confidence = {"shoulder": shoulder_c, "hip": hip_c, "knee": knee_c, "ankle": ankle_c}

    if shoulder_c <= 0.0 or hip_c <= 0.0:
        return keypoint_confidence, 0.0  # no usable torso vector -- classify_activity() will UNKNOWN on confidence regardless

    dx = hip_x - shoulder_x
    dy = hip_y - shoulder_y
    torso_angle_deg = math.degrees(math.atan2(dx, dy)) if (dx or dy) else 0.0
    return keypoint_confidence, torso_angle_deg


# ---------------------------------------------------------------------------
# Item 13: the original flicker issue this whole project's most recent
# round of work started from. Occupancy itself -- not just identity
# matching -- was observed flipping VACANT/ACTIVE rapidly frame to frame
# on real footage. A pure, shared state-transition function here (rather
# than duplicated logic in both stream_worker.py and video_export.py, the
# same two places every other per-frame concern in this project has
# needed keeping in sync) so both the live pipeline and the downloaded-
# video export get identical, single-tested debounce behavior.
# ---------------------------------------------------------------------------

# Requires this many CONSECUTIVE calls with a new raw status before it's
# committed as a real transition; a flip that reverts before reaching
# this doesn't count at all. Documented placeholder, not tuned against
# real footage yet -- 3 is a reasonable starting point, not a measured
# constant. Lives here (not in stream_worker.py) so both stream_worker.py
# and video_export.py -- the two places occupancy is computed -- use the
# exact same value without one importing a constant from the other.
OCCUPANCY_HYSTERESIS_FRAMES = int(__import__("os").environ.get("OCCUPANCY_HYSTERESIS_FRAMES", 3))


def apply_occupancy_hysteresis(committed: dict, pending: dict, pending_count: dict,
                                key, raw_status: str, hysteresis_frames: int) -> str:
    """Returns the COMMITTED status for `key` (a workstation name) --
    which only changes once raw_status has held consistently for
    hysteresis_frames consecutive calls, filtering out single-frame
    flips. A flip that reverts before reaching the threshold doesn't
    count at all and doesn't affect the committed status.

    `committed`, `pending`, `pending_count` are the caller's own state
    dicts, keyed by `key`, mutated in place -- this function has no
    dependency on where that state actually lives (a StreamWorker
    instance's attributes, or a batch video export's local variables),
    only that the SAME three dicts are passed on every call for the same
    logical stream of frames.

    The very first observation for a key commits immediately -- there's
    no prior committed state to protect from flicker yet, and delaying
    the first ever ACTIVE/VACANT read would only add startup latency for
    no real benefit.
    """
    current = committed.get(key)
    if current is None:
        committed[key] = raw_status
        pending[key] = raw_status
        pending_count[key] = 1
        return raw_status

    if raw_status == current:
        # Either genuinely stable, or a pending flip attempt just
        # reverted back to the committed status before reaching
        # threshold -- either way, reset the pending tracker so a LATER
        # flip attempt has to start counting from zero again, not
        # continue an old, already-abandoned attempt.
        pending[key] = raw_status
        pending_count[key] = 1
        return current

    if pending.get(key) == raw_status:
        pending_count[key] = pending_count.get(key, 0) + 1
    else:
        pending[key] = raw_status
        pending_count[key] = 1

    if pending_count[key] >= hysteresis_frames:
        committed[key] = raw_status
        return raw_status
    return current  # not yet consistent enough to commit -- keep reporting the old status
