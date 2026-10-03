"""
One-shot environment compatibility check for the torch / numpy / opencv /
ultralytics / insightface / onnxruntime stack this project depends on.

Run with the project's conda env active:
    python check_env_compatibility.py

This only reads your installed packages -- it changes nothing. Paste the
full output back and it'll pinpoint exactly what (if anything) is
mismatched, rather than guessing blind.
"""
import importlib.metadata as md
import warnings


def section(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def installed_version(name):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


# ---------------------------------------------------------------------------
# 1. Installed versions of everything relevant
# ---------------------------------------------------------------------------
section("Installed versions")
packages = [
    "torch", "torchvision", "numpy", "scipy",
    "opencv-python", "opencv-python-headless", "opencv-contrib-python",
    "ultralytics", "lap",
    "insightface", "onnxruntime", "onnxruntime-gpu",
    "fastapi", "sqlalchemy",
]
versions = {}
for pkg in packages:
    v = installed_version(pkg)
    versions[pkg] = v
    print(f"  {pkg:28s} {v if v else '(not installed)'}")

# ---------------------------------------------------------------------------
# 2. The #1 known conflict: both opencv-python AND opencv-python-headless
#    installed at once. pip does NOT detect this as a conflict -- both
#    packages provide the same `cv2` import name, and whichever one's
#    files land on disk "last" wins unpredictably.
# ---------------------------------------------------------------------------
section("OpenCV double-install check")
cv_variants = [p for p in ("opencv-python", "opencv-python-headless", "opencv-contrib-python")
               if versions.get(p)]
if len(cv_variants) > 1:
    print(f"  CONFLICT: {', '.join(cv_variants)} are ALL installed at once.")
    print("  Fix: pip uninstall all of them, then reinstall only "
          "opencv-python-headless (what requirements.txt actually wants):")
    print("    pip uninstall opencv-python opencv-python-headless opencv-contrib-python -y")
    print("    pip install opencv-python-headless>=4.9")
else:
    print(f"  OK: only {cv_variants[0] if cv_variants else '(none found)'} installed.")

# ---------------------------------------------------------------------------
# 3. The numpy/torch ABI mismatch this conversation is specifically about.
#    If torch was built against numpy 1.x but numpy 2.x is installed,
#    importing torch raises this exact UserWarning and silently disables
#    torch's fast numpy interop.
# ---------------------------------------------------------------------------
section("PyTorch / NumPy ABI compatibility")
numpy_abi_warning = None
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    try:
        import torch  # noqa: E402
        for w in caught:
            if "numpy" in str(w.message).lower():
                numpy_abi_warning = str(w.message)
    except Exception as e:
        print(f"  Could NOT import torch at all: {type(e).__name__}: {e}")
        torch = None

if numpy_abi_warning:
    print("  MISMATCH DETECTED:")
    print(f"    {numpy_abi_warning}")
    print("  This means torch's fast numpy<->tensor conversion is DISABLED")
    print("  right now -- this alone can show up as unexplained slowness")
    print("  in anything that round-trips frames between cv2 (numpy) and")
    print("  YOLO (torch), which is every frame of your detection pipeline.")
    print("  Fix is one of:")
    print(f"    pip install \"numpy<2\"        # matches an older torch build")
    print(f"    pip install -U torch torchvision   # or upgrade torch to a build with full numpy 2 support")
elif torch is not None:
    print("  OK: no numpy ABI warning raised on import.")

if torch is not None:
    print(f"\n  torch.__version__       = {torch.__version__}")
    print(f"  numpy version (via torch's own import) matches above: {versions.get('numpy')}")

# ---------------------------------------------------------------------------
# 4. GPU availability -- separate from the ABI issue, but usually the
#    SINGLE biggest factor in "low FPS" if you have an NVIDIA GPU that
#    torch isn't actually using (a CPU-only torch wheel is the most common
#    cause, and `pip install ultralytics` with no index-url will often
#    resolve exactly that unless you explicitly asked for a CUDA build).
# ---------------------------------------------------------------------------
section("GPU / CUDA availability (torch)")
if torch is not None:
    cuda_available = torch.cuda.is_available()
    print(f"  torch.cuda.is_available() = {cuda_available}")
    print(f"  torch.version.cuda        = {torch.version.cuda}")
    if cuda_available:
        print(f"  GPU detected: {torch.cuda.get_device_name(0)}")
    else:
        print("  No GPU detected by torch. If this machine HAS an NVIDIA GPU,")
        print("  this is almost certainly your real FPS bottleneck -- bigger")
        print("  than any numpy/torch ABI mismatch. You'd need the CUDA build")
        print("  of torch specifically (see pytorch.org's install selector),")
        print("  not just `pip install torch`.")

# ---------------------------------------------------------------------------
# 5. onnxruntime execution provider -- affects InsightFace embedding speed
#    the same way torch's CUDA availability affects YOLO speed.
# ---------------------------------------------------------------------------
section("ONNX Runtime execution providers (affects InsightFace speed)")
try:
    import onnxruntime as ort  # noqa: E402
    providers = ort.get_available_providers()
    print(f"  Available providers: {providers}")
    if "CUDAExecutionProvider" not in providers:
        print("  No CUDA provider available to onnxruntime -- face embedding")
        print("  extraction (InsightFace) is running on CPU. Separate from")
        print("  the YOLO/torch GPU question above, but same category of issue.")
except ImportError:
    print("  onnxruntime not installed or failed to import.")

section("Done")
print("Paste this entire output back and it'll be clear exactly what, if")
print("anything, needs to change -- no guessing required.")