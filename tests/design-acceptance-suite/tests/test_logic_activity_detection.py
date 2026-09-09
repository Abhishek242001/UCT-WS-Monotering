import pytest

from src.logic import classify_activity, Activity


FULL_CONF = {"hip": 0.9, "knee": 0.9, "ankle": 0.9, "shoulder": 0.9}


def test_walking_when_moving_regardless_of_torso_angle():
    result = classify_activity(FULL_CONF, torso_angle_deg=5, is_moving=True)
    assert result == Activity.WALKING


@pytest.mark.parametrize("angle", [31, 45, 60, 90])
def test_leaning_when_torso_angle_large_and_not_moving(angle):
    result = classify_activity(FULL_CONF, torso_angle_deg=angle, is_moving=False)
    assert result == Activity.LEANING


@pytest.mark.parametrize("angle", [-31, -45, -60])
def test_leaning_detected_symmetrically_for_negative_angles(angle):
    result = classify_activity(FULL_CONF, torso_angle_deg=angle, is_moving=False)
    assert result == Activity.LEANING


@pytest.mark.parametrize("angle", [11, 15, 20, 30])
def test_transitioning_in_the_mid_angle_band(angle):
    result = classify_activity(FULL_CONF, torso_angle_deg=angle, is_moving=False)
    assert result == Activity.TRANSITIONING


@pytest.mark.parametrize("angle", [0, 5, 10])
def test_upright_torso_and_not_moving_is_sitting_or_standing(angle):
    result = classify_activity(FULL_CONF, torso_angle_deg=angle, is_moving=False)
    assert result in (Activity.SITTING, Activity.STANDING)


def test_seated_hint_selects_sitting():
    conf = dict(FULL_CONF, seated_hint=0.9)
    result = classify_activity(conf, torso_angle_deg=0, is_moving=False)
    assert result == Activity.SITTING


def test_no_seated_hint_selects_standing():
    result = classify_activity(FULL_CONF, torso_angle_deg=0, is_moving=False)
    assert result == Activity.STANDING


@pytest.mark.parametrize("missing_key", ["hip", "knee", "ankle", "shoulder"])
def test_unknown_when_any_required_keypoint_is_low_confidence(missing_key):
    conf = dict(FULL_CONF)
    conf[missing_key] = 0.1
    result = classify_activity(conf, torso_angle_deg=0, is_moving=False)
    assert result == Activity.UNKNOWN


def test_unknown_when_all_keypoints_missing():
    result = classify_activity({}, torso_angle_deg=0, is_moving=False)
    assert result == Activity.UNKNOWN


def test_unknown_takes_priority_even_when_moving():
    # A confident "walking" verdict still requires confident keypoints --
    # a system that can't see the legs shouldn't claim to see them walking.
    conf = {"hip": 0.9, "knee": 0.1, "ankle": 0.9, "shoulder": 0.9}
    result = classify_activity(conf, torso_angle_deg=0, is_moving=True)
    assert result == Activity.UNKNOWN


@pytest.mark.parametrize("min_conf,keypoint_value,expected_unknown", [
    (0.5, 0.49, True),
    (0.5, 0.5, False),
    (0.5, 0.51, False),
    (0.8, 0.7, True),
    (0.8, 0.85, False),
])
def test_confidence_threshold_is_configurable(min_conf, keypoint_value, expected_unknown):
    conf = {"hip": keypoint_value, "knee": 0.99, "ankle": 0.99, "shoulder": 0.99}
    result = classify_activity(conf, torso_angle_deg=0, is_moving=False, min_conf=min_conf)
    assert (result == Activity.UNKNOWN) is expected_unknown


def test_occlusion_realistic_desk_scenario_yields_unknown():
    # Mirrors the documented real-world expectation: seated at a desk,
    # hips/knees/ankles are occluded below the desk surface, so the
    # classifier should honestly report UNKNOWN rather than guess.
    desk_occluded_conf = {"hip": 0.05, "knee": 0.0, "ankle": 0.0, "shoulder": 0.95}
    result = classify_activity(desk_occluded_conf, torso_angle_deg=0, is_moving=False)
    assert result == Activity.UNKNOWN


@pytest.mark.parametrize("activity", list(Activity))
def test_all_activity_enum_values_are_strings(activity):
    assert isinstance(activity.value, str)


def test_activity_enum_has_exactly_six_members():
    # 5 general activities + UNKNOWN, per the documented design.
    assert len(list(Activity)) == 6
