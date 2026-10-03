# Document the GPU install path, so it doesn't silently regress

**Target branch: `local`**, on top of everything already applied.

## What this is for

Today's session got your machine onto GPU for both YOLO (torch) and
InsightFace (onnxruntime) -- a real, confirmed FPS win. But none of that
was reflected in `requirements.txt`, which still only pulls in whatever
CPU-only torch build ultralytics resolves by default on Windows. A fresh
`pip install -r requirements.txt` on this same machine, or a teammate's,
would silently reinstall CPU-only torch and undo today's work with no
error or warning.

## What changed

**`backend/requirements.txt`** -- no dependency changes, just documentation:
- A note at the top explaining the CPU-vs-GPU split and pointing to the
  new `requirements-gpu.txt`.
- A note next to `numpy` recording that numpy 2.x + current torch is
  verified NOT to hit the once-common ABI mismatch, so nobody re-litigates
  that question from scratch next time.
- A note next to `opencv-python-headless` documenting the exact
  double-install conflict with `opencv-python` that actually happened on
  this machine today, so it's caught by reading the file next time,
  not rediscovered by debugging.
- A note next to `onnxruntime` warning against having both it and
  `onnxruntime-gpu` installed simultaneously, same class of conflict.

**`backend/requirements-gpu.txt`** (new) -- the actual CUDA install,
documented rather than hard-pinned, since the correct CUDA build depends
on each machine's driver:
```
--extra-index-url https://download.pytorch.org/whl/cu128
torch
torchvision
onnxruntime-gpu
```
Includes the exact steps to check which CUDA build a given machine
needs (`nvidia-smi`'s reported version is a ceiling, not a target), and
the disk-space warning this session ran into directly (pip's temp
extraction uses the system temp drive, not necessarily the project's
drive).

## Not a code change

Nothing in `backend/app/` is touched by this change -- it's purely
dependency documentation, reflecting what this session actually did and
verified, step by step, on your real machine. Re-running the test suite
isn't necessary for this change specifically, but re-running it after any
future `pip install -r requirements-gpu.txt` on a fresh machine is a good
way to confirm the GPU path actually worked before trusting it.
