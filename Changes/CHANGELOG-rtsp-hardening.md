# Changelog: RTSP hardening + Windows health-check fix

Base commit expected: the local-Windows-setup commit ("Local Windows setup:
.gitignore, .gitattributes, .bat launchers, fix .env.example defaults") or
anything after it. If `git log --oneline -3` does not show it, stop and tell
Claude.

**File and upload sources behave exactly as before** (Video Analysis, and a
file path typed into Live Stream). The new code only runs for `rtsp://` and
`rtsps://` sources, so Lightning is unaffected.

## Why

Reading the code showed that RTSP had never been tried on a camera, and that
the worker used a bare `cv2.VideoCapture` for it. Measured against a local
RTSP server (MediaMTX) before changing anything:

- A stalled camera made `cap.read()` block for 30 seconds or more per call.
- The old loop gave up for good after about one second of failed reads
  (5 tries, 0.2 s apart), so a camera reboot or a Wi-Fi blip ended the run.
- The worker sleeps between frames (0.3 s from the Live Stream tab) plus
  YOLO time, while a camera delivers about 25 frames/s. Unread frames pile
  up in OpenCV's buffer, so analysis drifts further behind real time.
- Errors printed and published to the browser contained the RTSP URL with
  the password in plain text.

## What changed

| File | Change |
|---|---|
| `backend/app/vision/capture.py` (new) | `open_frame_source()`: files get a plain `cv2.VideoCapture` as before. RTSP gets `RtspFrameSource`: a reader thread that keeps only the newest frame, open/read timeouts, stall detection, reconnect with backoff (1 s, 2 s, 4 s ... up to 30 s) until the stream is stopped |
| `backend/app/vision/stream_worker.py` | Opens sources through `capture.open_frame_source()`. Credentials are masked in the "could not open" print and event. Publishes a `source_status` event when the camera drops or returns |
| `frontend/js/tabs/livestream.js` | Live preview line shows "Camera connection lost. Reconnecting (attempt N)..." and "Camera reconnected." |
| `backend/app/routers/reports_and_health.py` | Health check wrote to a hard-coded `/tmp/...`, which does not exist on Windows, so the mp4v check always failed there. Now uses the OS temp folder |
| `backend/tools/check_rtsp.py` (new) | One command that says whether a camera is live, using the same opening code as the app. Never prints the password |
| `tests/real-app-smoke/test_rtsp_capture.py` (new) | 27 tests, no camera needed |

Settings (all optional environment variables; put them in `.env`, own line,
no comment after the value):

    RTSP_OPEN_TIMEOUT_MS=8000
    RTSP_READ_TIMEOUT_MS=5000
    RTSP_STALL_SECONDS=10          # no new frame for this long = reconnect
    RTSP_RECONNECT_INITIAL_SECONDS=1
    RTSP_RECONNECT_MAX_SECONDS=30
    RTSP_INITIAL_OPEN_ATTEMPTS=3   # tries at start before reporting failure
    RTSP_TRANSPORT=tcp             # or udp. Leave unset unless you have a reason

`RTSP_TRANSPORT` is unset by default because OpenCV already prefers TCP
(the OpenCV build I inspected contains that default), and your camera opened
fine without it. An `OPENCV_FFMPEG_CAPTURE_OPTIONS` value you set yourself is
never overridden.

## Behaviour to know

- The worker always analyses the **newest** frame the camera has delivered,
  not every frame. Lag no longer grows. Your camera's main stream is 1280x720.
- If the camera is not reachable at start, the app tries 3 times (about
  4 s of waiting between tries), then reports "Could not open source" as
  before, with the password masked.
- Once connected, a lost camera is retried forever with growing waits until
  you press Stop. Stopping while it is reconnecting can take up to the open
  timeout (8 s by default).
- Live Stream marks runs as live, so a matched face from the RTSP camera
  writes **real** attendance records, as it does for any Live Stream source.

## Verified here (no real Windows machine, no real camera)

- 27 unit tests pass (repeated 6 times, stable, about 4 s each). They cover
  source detection, password masking, timeouts passed to OpenCV, the optional
  transport setting being set only during open and restored, newest-frame
  delivery, reconnect with growing delays, stall detection, no false
  reconnect on the slow first read, stop, release and a failing callback.
- Real RTSP behaviour against a local MediaMTX server publishing a looped
  H.264 stream: normal reading, a slow consumer receives the newest frame,
  publisher killed and restarted (reconnected and frames resumed), server
  frozen for 9 s (stall detected, reconnected), bad URL (fails after the
  configured attempts), release is immediate.
- `StreamWorker` run with the model libraries stubbed: RTSP worker
  processed frames and stopped in 0.4 s; a bad `rtsp://admin:...@` URL
  leaked the password nowhere (log or event); a file source still ended with
  its normal `source_ended` reason.
- `check_rtsp.py` against the local server: live and steady, failure
  message, forced TCP.

**Not verified:** the Hikvision camera itself, Windows, the full
`tests/real-app-smoke` suite (needs ultralytics; not installable in my
sandbox), and the browser message on a real reconnect.

## Apply (Windows, the machine where you commit)

    cd /d D:\Download-D\uct-workstation
    tar -xf rtsp-hardening-changes.zip
    xcopy /S /Y /H "rtsp-hardening-changes\*" "git\UCT-WS-Monotering\"
    cd git\UCT-WS-Monotering
    git status
    git add -A
    git commit -m "RTSP hardening: newest-frame reader, timeouts, reconnect, masked credentials; fix /tmp health check"
    git push origin main

`git status` should list only: `backend/app/vision/capture.py`,
`backend/app/vision/stream_worker.py`,
`backend/app/routers/reports_and_health.py`, `backend/tools/check_rtsp.py`,
`frontend/js/tabs/livestream.js`,
`tests/real-app-smoke/test_rtsp_capture.py` and this changelog.

Lightning: `git pull`, restart the backend. Nothing else. Run the new tests
there too if you like: they need no camera.

## Check on your Windows machine (after `git pull` and a backend restart)

1. Is the camera live (paste your own URL, keep the quotes):

       conda activate uct-ws
       python backend\tools\check_rtsp.py "rtsp://admin:PASSWORD@192.168.1.101:554/Streaming/Channels/101" --seconds 15

   Expect `LIVE and steady.`

2. The new tests:

       cd tests\real-app-smoke
       pytest test_rtsp_capture.py -q

   Expect `27 passed`.

3. Start backend and frontend. Draw workstation regions for the camera ID
   you will use (from a 1280x720 snapshot), then in **Live Stream** set the
   source to the RTSP URL, the camera ID, and click Start stream. The
   preview should show frames.

4. Reconnect test: while it streams, unplug the camera's network cable for
   about 15 seconds. The preview line should change to "Camera connection
   lost. Reconnecting (attempt N)...". Plug it back in: "Camera reconnected."
   then "Live - frame N" again. Then press Stop.

5. Health check: sign in, open `http://localhost:8001/system/health` through
   the app (it needs your session) and confirm the mp4v check is no longer
   false because of the path.

## Log to send Claude

The output of step 1 and step 2, the terminal lines containing
`[stream_worker]` from step 4, a screenshot of the Live Stream preview during
the drop, and `git log --oneline -3`.

## Security note

The camera password has appeared in this chat and in your shell history.
Consider giving the app its own view-only camera user and changing the admin
password when testing is finished.
