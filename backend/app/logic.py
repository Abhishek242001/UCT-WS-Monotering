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
