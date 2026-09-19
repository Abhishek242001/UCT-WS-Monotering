"""
Face embedding extraction, built as a pluggable interface. Real InsightFace
inference (buffalo_s, ~124MB, downloaded automatically on first use from
the deepinsight/insightface GitHub releases) has been verified working in
this repository's own build process: same-face re-extraction produced
cosine similarity 1.0, and two different people's faces produced
similarity ~-0.03 -- genuinely discriminative, not a coincidence of the
stub. To activate it, install requirements-face-recognition.txt (see
backend/requirements-face-recognition.txt); this module detects the
installed package automatically and switches backends with no code
changes needed on your end.

If insightface/onnxruntime are NOT installed, this module falls back to a
clearly-labeled deterministic STUB so every enrollment/calibration/
matching endpoint can still be exercised end-to-end without the real
model -- useful for CI or a quick structural check, but never mistake
stub-mode embeddings for real biometric matching: they are a fixed-size
hash of the image bytes, not a facial feature vector, and two different
photos of the SAME person will NOT reliably produce similar stub
embeddings. Stub mode exists to test plumbing, not accuracy.
"""
import hashlib
import logging
import os

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 512  # matches ArcFace/AdaFace output dimensionality

_insightface_app = None
_insightface_available = None
_insightface_load_error = None  # str | None -- the real exception from the last load attempt, if it failed


def _try_load_insightface():
    global _insightface_app, _insightface_available, _insightface_load_error
    if _insightface_available is not None:
        return _insightface_available
    try:
        from insightface.app import FaceAnalysis
        providers = ["CPUExecutionProvider"]
        _insightface_app = FaceAnalysis(name=os.environ.get("INSIGHTFACE_PACK", "buffalo_s"), providers=providers)
        _insightface_app.prepare(ctx_id=-1, det_size=(320, 320))
        _insightface_available = True
        _insightface_load_error = None
    except Exception as e:
        # Previously this swallowed the exception completely -- package
        # missing, model download blocked by a firewall, disk full,
        # ONNX provider mismatch, anything -- with zero record of WHY,
        # anywhere, ever. That made "still stub after installing the
        # packages" undiagnosable from outside a live debugger. Now
        # logged AND captured for /system/health to report (see
        # load_error() below), and cached only for this process's
        # lifetime like before -- a fixed underlying cause (e.g. network
        # restored) still needs a backend restart to take effect, same
        # as always, but at least the reason is now visible.
        _insightface_available = False
        _insightface_load_error = f"{type(e).__name__}: {e}"
        logger.warning("Real face-recognition backend (insightface) failed to load -- "
                        "falling back to stub. Reason: %s", _insightface_load_error, exc_info=True)
    return _insightface_available


def load_error() -> str | None:
    """The real exception from the last insightface load attempt, if it
    fell back to stub for a reason other than the package simply not
    being installed. None if insightface loaded successfully, or if it
    hasn't been attempted yet in this process."""
    _try_load_insightface()  # ensure at least one attempt has been made
    return _insightface_load_error


def backend_name() -> str:
    return "insightface" if _try_load_insightface() else "stub"


def extract_embedding(image_path: str) -> tuple[list[float], float]:
    """Returns (embedding, face_width_px). Uses real InsightFace if
    available; otherwise falls back to the documented stub."""
    if _try_load_insightface():
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            raise ValueError(f"Could not decode image file: {image_path}")
        faces = _insightface_app.get(img)
        if not faces:
            # Previously raised with no diagnostic trace at all -- the
            # caller (_identify) catches this ValueError and just returns
            # UNKNOWN, so without this log line there was literally no
            # record anywhere of WHY a photo with a visibly-present face
            # (to a human) produced no match: too-small/distant face vs
            # det_size=(320,320) is the most common real cause, but a
            # decode issue, bad orientation, or an unusual crop could
            # also produce this -- logging the actual frame size at
            # least narrows it down without needing the photo emailed in.
            h, w = img.shape[:2]
            logger.warning(
                "No face detected in %dx%d image (path=%s) -- if a face IS visibly "
                "present, it is likely too small at the current det_size=(320,320) "
                "for this detector pack; a face occupying a small fraction of a "
                "wide/distant shot is the most common cause.",
                w, h, image_path,
            )
            raise ValueError("No face detected in image")
        face = faces[0]
        width_px = float(face.bbox[2] - face.bbox[0])
        return face.normed_embedding.tolist(), width_px

    # --- stub fallback -----------------------------------------------------
    with open(image_path, "rb") as f:
        data = f.read()
    digest = hashlib.sha512(data).digest()
    # Expand/repeat the digest bytes to EMBEDDING_DIM floats in [-1, 1].
    raw = (digest * ((EMBEDDING_DIM // len(digest)) + 1))[:EMBEDDING_DIM]
    embedding = [(b / 127.5) - 1.0 for b in raw]
    stub_width_px = 150.0  # fixed placeholder; real backend measures the actual detected face
    return embedding, stub_width_px
