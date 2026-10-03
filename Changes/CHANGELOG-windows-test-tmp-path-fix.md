# Fix hardcoded /tmp path in test_occupancy_hysteresis.py

**Target branch: `local`**, on top of the gallery-cache-and-recognition-fix
change already applied.

## What broke

`test_single_frame_blip_produces_no_identification_event` failed on your
machine: `[stream_worker] Could not open source: /tmp/hysteresis_blip_test.mp4`
logged during the test, and zero events recorded instead of the expected
one MATCH.

Not caused by the gallery-cache/recognition changes -- this is a
pre-existing bug in the test file itself: `path = "/tmp/hysteresis_blip_test.mp4"`
is a Unix-style absolute path, which doesn't resolve to a writable
location on Windows. `cv2.VideoWriter` silently wrote nowhere useful,
then `cv2.VideoCapture` failed to open that same path, so the stream
thread never processed a single frame. Same class of bug already fixed
once before in `reports_and_health.py`'s health check
(`/tmp/_health_check_mp4v.mp4` -> `tempfile.gettempdir()`) -- this one
had just never been exercised on a real Windows machine until now, since
this suite previously only ran on Lightning (Linux).

Checked the rest of the test suite and app code for the same pattern --
this was the only hardcoded `/tmp/...` path anywhere.

## Fix

`tests/real-app-smoke/test_occupancy_hysteresis.py` now builds the path
via `os.path.join(tempfile.gettempdir(), "hysteresis_blip_test.mp4")`,
same pattern as the existing health-check fix.

## Verified

Confirmed in my sandbox that the test now gets past opening the video
source (fails only on the pre-existing `ModuleNotFoundError: ultralytics`
gap, same as every other test needing a real model here) -- the actual
assertion logic was never the problem, only the path. You have real
`ultralytics`/`insightface` installed, so this should now pass for real
on your machine. Please confirm.
