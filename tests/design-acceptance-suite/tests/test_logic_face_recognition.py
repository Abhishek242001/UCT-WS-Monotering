import math
import pytest

from src.logic import (
    estimate_depth_from_facewidth, alpha_decay, noise_sigma, signal_to_noise_ratio,
    cosine_similarity, decide_match_status, MatchStatus, slant_distance,
    face_width_px_at_distance, focal_length_px, classify_confidence_zone,
    group_gallery_by_person, match_single_pass,
)


# --- estimate_depth_from_facewidth ------------------------------------------

@pytest.mark.parametrize("live_w,ref_w,ref_d,expected", [
    (100, 100, 1.0, 1.0),      # same width as reference -> same depth
    (50, 100, 1.0, 2.0),       # half the width -> twice as far
    (200, 100, 1.0, 0.5),      # double the width -> half as far
    (206, 206, 1.0, 1.0),
    (100, 206, 1.0, 2.06),
])
def test_estimate_depth_from_facewidth(live_w, ref_w, ref_d, expected):
    assert estimate_depth_from_facewidth(live_w, ref_w, ref_d) == pytest.approx(expected, rel=1e-6)


@pytest.mark.parametrize("live_w,ref_w,ref_d", [(0, 100, 1.0), (-5, 100, 1.0)])
def test_estimate_depth_rejects_nonpositive_live_width(live_w, ref_w, ref_d):
    with pytest.raises(ValueError):
        estimate_depth_from_facewidth(live_w, ref_w, ref_d)


@pytest.mark.parametrize("ref_w,ref_d", [(0, 1.0), (100, 0), (-1, 1.0), (100, -1)])
def test_estimate_depth_rejects_nonpositive_reference(ref_w, ref_d):
    with pytest.raises(ValueError):
        estimate_depth_from_facewidth(100, ref_w, ref_d)


# --- alpha_decay -------------------------------------------------------------

def test_alpha_decay_is_one_at_zero_relative_depth():
    assert alpha_decay(0.0, k=0.3) == pytest.approx(1.0)


@pytest.mark.parametrize("relative_depth,k", [(1.0, 0.2), (2.0, 0.2), (5.0, 0.1), (0.5, 0.4)])
def test_alpha_decay_monotonically_decreases_with_distance(relative_depth, k):
    a0 = alpha_decay(0.0, k)
    a1 = alpha_decay(relative_depth, k)
    assert a1 < a0


def test_alpha_decay_rejects_negative_depth():
    with pytest.raises(ValueError):
        alpha_decay(-1.0, k=0.2)


@pytest.mark.parametrize("k", [0.0, 0.1, 0.5, 1.0, 2.0])
def test_alpha_decay_formula_matches_exponential(k):
    d = 3.0
    assert alpha_decay(d, k) == pytest.approx(math.exp(-k * d))


# --- noise_sigma -------------------------------------------------------------

@pytest.mark.parametrize("d,sigma0,gamma,expected", [
    (0.0, 0.1, 0.5, 0.1),
    (2.0, 0.1, 0.5, 0.2),
    (4.0, 0.1, 0.0, 0.1),  # gamma=0 -> constant noise regardless of distance
])
def test_noise_sigma_linear_growth(d, sigma0, gamma, expected):
    assert noise_sigma(d, sigma0, gamma) == pytest.approx(expected)


@pytest.mark.parametrize("sigma0,gamma", [(-0.1, 0.5), (0.1, -0.5)])
def test_noise_sigma_rejects_negative_params(sigma0, gamma):
    with pytest.raises(ValueError):
        noise_sigma(1.0, sigma0, gamma)


# --- signal_to_noise_ratio ----------------------------------------------------

def test_snr_decreases_as_distance_increases():
    snr_near = signal_to_noise_ratio(similarity=0.8, relative_depth_m=0.5, k=0.2, sigma0=0.1, gamma=0.5)
    snr_far = signal_to_noise_ratio(similarity=0.8, relative_depth_m=4.0, k=0.2, sigma0=0.1, gamma=0.5)
    assert snr_far < snr_near


def test_snr_higher_similarity_gives_higher_snr():
    low = signal_to_noise_ratio(similarity=0.3, relative_depth_m=1.0, k=0.2, sigma0=0.1, gamma=0.5)
    high = signal_to_noise_ratio(similarity=0.9, relative_depth_m=1.0, k=0.2, sigma0=0.1, gamma=0.5)
    assert high > low


# --- cosine_similarity --------------------------------------------------------

@pytest.mark.parametrize("a,b,expected", [
    ([1, 0, 0], [1, 0, 0], 1.0),
    ([1, 0, 0], [0, 1, 0], 0.0),
    ([1, 0, 0], [-1, 0, 0], -1.0),
    ([1, 1], [1, 1], 1.0),
    ([1, 2, 3], [4, 5, 6], 32 / (math.sqrt(14) * math.sqrt(77))),
])
def test_cosine_similarity(a, b, expected):
    assert cosine_similarity(a, b) == pytest.approx(expected, rel=1e-6)


def test_cosine_similarity_rejects_zero_vector():
    with pytest.raises(ValueError):
        cosine_similarity([0, 0, 0], [1, 2, 3])


# --- decide_match_status -------------------------------------------------------

def test_vacant_when_not_occupied():
    assert decide_match_status(False, 0.9, 10.0, True) == MatchStatus.VACANT


def test_unknown_when_similarity_missing():
    assert decide_match_status(True, None, None, None) == MatchStatus.UNKNOWN


@pytest.mark.parametrize("similarity,snr", [(0.2, 10.0), (0.9, 1.0), (0.3, 2.0)])
def test_unknown_when_below_thresholds(similarity, snr):
    status = decide_match_status(True, similarity, snr, True, sim_threshold=0.45, snr_threshold=3.0)
    assert status == MatchStatus.UNKNOWN


def test_match_when_confident_and_assigned():
    assert decide_match_status(True, 0.8, 10.0, True) == MatchStatus.MATCH


def test_mismatch_when_confident_but_not_assigned():
    assert decide_match_status(True, 0.8, 10.0, False) == MatchStatus.MISMATCH


@pytest.mark.parametrize("sim_threshold,snr_threshold", [(0.3, 1.0), (0.6, 5.0), (0.45, 3.0)])
def test_thresholds_are_configurable(sim_threshold, snr_threshold):
    # Just above both thresholds should always yield a decision, not UNKNOWN.
    status = decide_match_status(True, sim_threshold + 0.05, snr_threshold + 1.0, True,
                                  sim_threshold=sim_threshold, snr_threshold=snr_threshold)
    assert status == MatchStatus.MATCH


# --- camera placement / confidence zones --------------------------------------

@pytest.mark.parametrize("height,horizontal,expected", [
    (3.0, 0.0, 3.0),
    (3.0, 4.0, 5.0),
    (0.0, 5.0, 5.0),
    (3.0, 5.196152, 6.0),
])
def test_slant_distance(height, horizontal, expected):
    assert slant_distance(height, horizontal) == pytest.approx(expected, rel=1e-4)


def test_slant_distance_rejects_negative_inputs():
    with pytest.raises(ValueError):
        slant_distance(-1.0, 2.0)


@pytest.mark.parametrize("distance_m,expected_zone", [
    (0.5, "reliable"), (1.0, "reliable"), (2.5, "reliable"),
])
def test_confidence_zone_reliable_range(distance_m, expected_zone):
    focal = focal_length_px(1920, 70)
    width = face_width_px_at_distance(0.15, focal, distance_m)
    assert classify_confidence_zone(width) == expected_zone


@pytest.mark.parametrize("distance_m", [5.0, 6.0, 8.0])
def test_confidence_zone_unreliable_at_long_range(distance_m):
    focal = focal_length_px(1920, 70)
    width = face_width_px_at_distance(0.15, focal, distance_m)
    assert classify_confidence_zone(width) in ("degraded", "unreliable")


def test_face_width_decreases_with_distance():
    focal = focal_length_px(1920, 70)
    w1 = face_width_px_at_distance(0.15, focal, 1.0)
    w2 = face_width_px_at_distance(0.15, focal, 2.0)
    assert w2 < w1


def test_face_width_rejects_zero_distance():
    with pytest.raises(ValueError):
        face_width_px_at_distance(0.15, 1000.0, 0.0)


# --- single-pass gallery matching (the O(n) complexity fix) ------------------

def test_group_gallery_by_person():
    gallery = {
        "EMP-1__front": [1, 0, 0],
        "EMP-1__left": [0.9, 0.1, 0],
        "EMP-2__front": [0, 1, 0],
    }
    grouped = group_gallery_by_person(gallery)
    assert set(grouped.keys()) == {"EMP-1", "EMP-2"}
    assert len(grouped["EMP-1"]) == 2
    assert len(grouped["EMP-2"]) == 1


def test_match_single_pass_finds_best_match():
    gallery = {
        "EMP-1__front": [1, 0, 0],
        "EMP-2__front": [0, 1, 0],
        "EMP-3__front": [0, 0, 1],
    }
    grouped = group_gallery_by_person(gallery)
    result = match_single_pass([0, 0.95, 0.05], grouped)
    assert result[0] == "EMP-2"


def test_match_single_pass_empty_gallery_returns_none():
    assert match_single_pass([1, 0, 0], {}) is None


@pytest.mark.parametrize("n_people", [1, 5, 50, 200])
def test_match_single_pass_scales_to_many_people(n_people):
    gallery = {}
    for i in range(n_people):
        vec = [0.0] * n_people
        vec[i] = 1.0
        gallery[f"EMP-{i}__front"] = vec
    grouped = group_gallery_by_person(gallery)
    target = [0.0] * n_people
    target[n_people // 2] = 1.0
    result = match_single_pass(target, grouped)
    assert result[0] == f"EMP-{n_people // 2}"
