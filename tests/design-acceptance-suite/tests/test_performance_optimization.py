"""
Performance/optimization tests, split the same way as test_security.py:

1. Pure arithmetic/decision-logic tests that run today (FPS budget math,
   hardware-path selection logic) -- no GPU or real model required.
2. Tests marked `skip` that require actual hardware, a trained model, and
   a TensorRT/OpenVINO runtime to produce a real number -- these exist as
   the acceptance checklist for the >=20 FPS requirement and should be
   un-skipped once real models and target hardware are available.
"""
import pytest


TARGET_FPS = 20


def frame_budget_ms(fps: int) -> float:
    return 1000.0 / fps


def select_acceleration_path(has_nvidia_gpu: bool, has_intel_cpu: bool) -> str:
    """Mirrors the documented decision: TensorRT for NVIDIA GPU targets
    (e.g. Lightning.ai cloud instances), OpenVINO for Intel-CPU-only local
    development, plain ONNX Runtime CPU as the least-reliable fallback."""
    if has_nvidia_gpu:
        return "TensorRT"
    if has_intel_cpu:
        return "OpenVINO"
    return "ONNXRuntime-CPU"


# --- FPS budget arithmetic ----------------------------------------------------

def test_frame_budget_at_20fps_is_50ms():
    assert frame_budget_ms(TARGET_FPS) == pytest.approx(50.0)


@pytest.mark.parametrize("fps,expected_ms", [(10, 100.0), (20, 50.0), (30, 33.333), (60, 16.667)])
def test_frame_budget_various_fps_targets(fps, expected_ms):
    assert frame_budget_ms(fps) == pytest.approx(expected_ms, rel=1e-3)


def test_stage_latency_sum_must_not_exceed_frame_budget():
    # Illustrative per-stage latency budget for the continuously-running
    # stages only (detection + lightweight face detector); identification
    # is event-driven and explicitly excluded from the 20fps budget.
    detection_ms = 22.0
    lightweight_face_detect_ms = 8.0
    overhead_ms = 5.0
    total = detection_ms + lightweight_face_detect_ms + overhead_ms
    assert total <= frame_budget_ms(TARGET_FPS)


@pytest.mark.parametrize("detection_ms,face_ms,overhead_ms,should_fit", [
    (20, 8, 5, True),
    (36, 10, 5, False),
    (40, 5, 5, True),
    (45, 5, 5, False),
])
def test_various_stage_latency_combinations_against_20fps_budget(detection_ms, face_ms, overhead_ms, should_fit):
    total = detection_ms + face_ms + overhead_ms
    assert (total <= frame_budget_ms(TARGET_FPS)) is should_fit


# --- acceleration-path selection logic ----------------------------------------

def test_nvidia_gpu_selects_tensorrt():
    assert select_acceleration_path(has_nvidia_gpu=True, has_intel_cpu=False) == "TensorRT"


def test_nvidia_gpu_preferred_even_if_intel_cpu_also_present():
    assert select_acceleration_path(has_nvidia_gpu=True, has_intel_cpu=True) == "TensorRT"


def test_intel_cpu_without_gpu_selects_openvino():
    assert select_acceleration_path(has_nvidia_gpu=False, has_intel_cpu=True) == "OpenVINO"


def test_no_accelerator_falls_back_to_onnxruntime_cpu():
    assert select_acceleration_path(has_nvidia_gpu=False, has_intel_cpu=False) == "ONNXRuntime-CPU"


@pytest.mark.parametrize("has_gpu,has_intel,expected", [
    (True, False, "TensorRT"),
    (True, True, "TensorRT"),
    (False, True, "OpenVINO"),
    (False, False, "ONNXRuntime-CPU"),
])
def test_acceleration_path_selection_matrix(has_gpu, has_intel, expected):
    assert select_acceleration_path(has_gpu, has_intel) == expected


def test_identification_is_excluded_from_the_fps_requirement():
    # Documented design: identification is event-driven (state change +
    # heartbeat), not per-frame, so it carries no FPS requirement of its
    # own -- it is fine for a single identification call to take far
    # longer than one frame budget.
    identification_latency_ms = 300  # e.g. a larger accuracy-focused backbone
    assert identification_latency_ms > frame_budget_ms(TARGET_FPS)  # allowed, by design


# ---------------------------------------------------------------------------
# Real-hardware benchmarks -- pending actual models + target hardware
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="Requires a real trained YOLO model and NVIDIA GPU hardware to benchmark TensorRT engine FPS.")
def test_tensorrt_engine_sustains_20fps_on_target_gpu():
    ...


@pytest.mark.skip(reason="Requires OpenVINO runtime and target Intel CPU hardware for a real benchmark.")
def test_openvino_sustains_acceptable_fps_on_target_cpu():
    ...


@pytest.mark.skip(reason="Requires the real combined pipeline (YOLO + lightweight face detector) under load to measure true end-to-end FPS.")
def test_combined_pipeline_sustains_20fps_end_to_end():
    ...


@pytest.mark.skip(reason="Requires measuring first-run TensorRT engine build/cache time against a real model file.")
def test_tensorrt_engine_cache_avoids_rebuild_on_subsequent_runs():
    ...


@pytest.mark.skip(reason="Requires real hardware to compare FP16 vs INT8 TensorRT accuracy/speed trade-off.")
def test_int8_quantization_meets_accuracy_floor_while_improving_fps():
    ...


@pytest.mark.skip(reason="Requires multiple concurrent camera streams on real hardware to test the shared-model-singleton design under load.")
def test_shared_model_instance_sustains_fps_target_across_multiple_streams():
    ...


@pytest.mark.skip(reason="Requires real ROI-cropped vs full-frame detection benchmark on representative footage.")
def test_roi_cropped_detection_is_faster_than_full_frame_detection():
    ...
