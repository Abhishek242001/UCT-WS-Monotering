# UCT Workstation Monitor

Employee Identity-Aware Workstation Monitoring & Attendance System — a real,
runnable implementation of the design documented in `docs/`.

## What's actually real here — read this first

This is a genuine, working application, not a mockup. As of this build:

- **Real FastAPI backend** with real SQLite persistence (swap to PostgreSQL
  via `DATABASE_URL` any time — the ORM models work unmodified).
- **Real YOLOv8 person detection** — verified against a real photo (2
  people correctly detected). `yolov8n.pt` is bundled so it works offline.
- **Real RBAC enforcement, including genuine multi-tenant org-scoping** —
  every route except the root health check and `/admin/login` requires a
  valid admin session, and every org-scoped endpoint additionally verifies
  the session's own `org_id` matches what it's being asked to touch (not
  just whatever `org_id` the client claims). This was added and verified
  via a full data-leakage audit — see "Multi-tenant data-leakage audit"
  below for exactly what was found and fixed.
- **Real face recognition (InsightFace)** — verified working in this
  build, not just stubbed. Two different people's faces produced cosine
  similarity **-0.03** (correctly dissimilar); the same face re-extracted
  produced **1.0**. It activates automatically once
  `requirements-face-recognition.txt` is installed — no code changes.
- **Real live-stream processing** — a genuine background worker
  (`app/vision/stream_worker.py`) opens an actual video source (RTSP URL
  or a local file — OpenCV handles both identically) and runs the
  event-driven detection loop from the project documentation: YOLO
  occupancy tracking on every frame, but identification firing only on a
  VACANT→ACTIVE transition or a heartbeat — never every frame. Verified
  end-to-end via real HTTP calls against a bundled demo video: 20/20
  frames processed, exactly one identification event fired during the
  occupied period (not per-frame), and the final state correctly reflected
  the video's ending (person left → VACANT).
- **Real video-upload "Run AI Analysis" feature** — upload a recorded
  video (with a genuine upload-progress bar, not a fake timer) and it runs
  through the **exact same** `start_worker()` code path a live RTSP camera
  uses — not a lookalike implementation. Results push live to the browser
  over a real WebSocket as analysis happens, with sensible defaults
  (org/camera ID, an auto-provisioned full-frame demo desk) so a sales
  demo works with zero prior setup, while still accepting explicit
  overrides. Built for exactly the use case of showing a prospect what the
  system does before they've installed a camera.
- **Real duplicate-video detection** — re-uploading a video you've already
  uploaded (even under a different filename) is detected by content hash
  (SHA-256, computed client-side via the Web Crypto API **before** any
  bytes are sent) and offers a "select this video" option instead of
  wasting bandwidth and storage on a redundant upload. Scoped per-org: two
  different organizations uploading the same stock demo video are treated
  as separate uploads, since ownership/history isn't shared. Verified with
  real tests, including that it's content-based, not filename-based.
- **Verified real concurrent multi-organization usage** — two different
  orgs running full video-analysis pipelines (enroll → workstation → 
  upload → analyze → live WebSocket results) genuinely *at the same time*,
  not just sequential calls with different org_ids. Confirmed zero
  cross-contamination (each org's employee correctly matched to their own
  desk under concurrent load) and measured real partial parallelism: two
  concurrent runs completed in **8.7s** versus **6.5s** for one run alone
  — meaningfully faster than the ~13s two fully-serialized runs would take,
  though not the ~6.5s of perfect parallelism either. See "Concurrency &
  multi-tenant scaling" below for exactly why, and what it means for real
  production load.
- **Real admin authentication** — PBKDF2 password hashing, session
  tokens, and account lockout after 5 failed attempts.
- **Real attendance state machine** — the exact `logic.py` functions
  verified by 476 tests are the same functions running live.
- **A working end-to-end pipeline you can click through**: enroll a photo
  → assign to a workstation → run detection (single frame, or a full live
  stream from the new "Live Stream" tab) → see a real, persisted result.

**What is NOT fully real, stated plainly:**

- **No RTSP camera was available to test against** — verified instead
  against a real local video file (OpenCV's `VideoCapture` treats an RTSP
  URL and a file path identically, so the code path is the same either
  way, but a genuine network camera has its own failure modes — dropped
  frames, reconnects — that a file source can't fully exercise). The
  worker includes retry tolerance for transient read failures for exactly
  this reason.
- **No HLS video encoding** — `streams/start` really opens and processes
  the source now, but doesn't encode a viewable video feed back out.
- **No TensorRT/OpenVINO acceleration wired in** — needs real target
  hardware to configure and benchmark.
- **Single-role RBAC** — every protected route requires *an* admin
  session, but there's only one role (`HR_ADMIN`) right now, not
  differentiated viewer/admin permissions.

Nothing here pretends to be more finished than it is — every simplification
above is called out in code comments at the exact place it matters.

## Quick start

```bash
./setup.sh                          # installs everything, runs both test suites
./run_backend.sh                    # terminal 1 — backend on :8001
./run_frontend.sh                   # terminal 2 — frontend on :8002
```

Then open **http://localhost:8002**. The backend prints a default admin
login to its console on first start.

To activate real face recognition (verified working — see above):
```bash
./setup.sh --with-face-recognition
```
This downloads a ~124MB model automatically on first use (one-time,
requires internet access).

To try the live-stream pipeline immediately without a real camera, use the
**Live Stream** tab and point it at `../sample_data/demo-camera-feed.mp4`
— a bundled synthetic video (empty desk → someone sits down → they leave)
built specifically to exercise the full occupancy/identification cycle.

## Project layout

```
backend/            Real FastAPI application
  app/
    main.py          entrypoint, CORS, default-admin seeding
    database.py       SQLAlchemy engine/session
    models.py         ORM models, all 17 tables
    logic.py          pure business logic (same file verified by 476 tests)
    security.py       password hashing, sessions, lockout
    routers/          the 9 API groups, each requiring admin auth via
                       a router-level dependency (except health + login)
    vision/
      yolo_detector.py    real YOLOv8 wrapper
      face_embedder.py    pluggable real-InsightFace / documented-stub
      stream_worker.py    real event-driven video-processing worker
      event_bus.py        thread-safe live-event bridge (worker thread -> WebSocket)
  routers/
    streams.py             live-stream start/stop/list, shared start_worker()
    streams_ws.py           WebSocket for live analysis results
    videos.py                video upload + "Run AI Analysis"
  yolov8n.pt          bundled weight file (works offline)
  requirements.txt
  requirements-face-recognition.txt

frontend/            Static dashboard (no build step)
  index.html           login, ROI drawing, enrollment, "Try Detection",
                        "Live Stream", "Video Analysis" (upload + live
                        WebSocket results), attendance

tests/
  design-acceptance-suite/   476 tests against pure logic + a mock API
                              contract (the original acceptance checklist)
  real-app-smoke/             30 tests against the ACTUAL backend above —
                               RBAC enforcement, the real stream worker,
                               and the video-upload analysis + live
                               WebSocket feature, all against a synthetic
                               but genuinely real video

docs/                Full project documentation (Word doc, key-points
                      checklist, design brief, brand assets)

sample_data/
  test-photo.jpg           two real people, for enrollment/detection demos
  demo-camera-feed.mp4     a synthetic but real video for the Live Stream tab
```

## Multi-tenant data-leakage audit (this revision)

A systematic end-to-end review, prompted by an explicit request to check
for data leakage and inconsistency, found and fixed a real vulnerability
class across nearly every router: **endpoints trusted whatever `org_id`
the client sent, instead of deriving it from who was actually logged in.**
Every individual fix below stems from that one root cause.

**The root-cause fix**: `AdminUser` / `AdminSession` now carry a real
`org_id`. A new `verify_org_access(session, org_id)` helper (in
`admin_auth.py`) is called at the top of every org-scoped endpoint and
rejects the request with `403` if the session's `org_id` doesn't match.
A `SUPER_ADMIN` role exists as the one deliberate exception (`org_id`
`NULL`) for genuinely cross-org operations — the bootstrap account is
seeded as `SUPER_ADMIN` so existing demo flows keep working unchanged,
and a new `POST /admin/users` endpoint (SUPER_ADMIN-only) lets you create
real, org-scoped `HR_ADMIN` accounts to see the restriction actually apply.

**Applied consistently across all 7 routers that touch org-scoped data**
(`workstations.py`, `employees.py`, `attendance.py`,
`shifts_breaks_calendar.py`, `dataset_calibration.py`, `videos.py`,
`reports_and_health.py`) — every endpoint in all seven now calls
`verify_org_access()`. `streams_ws.py`'s WebSocket handler now checks the
connecting admin's org against the stream's org before accepting the
connection (previously it only checked that the token was valid at all).

**Individual cross-tenant bugs found and fixed along the way** (each was a
real, exploitable gap, not a hypothetical):
- `employees.py` `enroll` could silently attach gallery photos to another
  org's *existing* employee record by reusing their `employee_id`.
- `employees.py` `delete` had no ownership check — any admin could delete
  any org's employee.
- `workstations.py` `assign` could assign another org's employee to your
  workstation.
- `attendance.py` `record_detection` could write attendance data against
  an employee who wasn't yours; `record_exception` had **no** employee
  check at all, org or otherwise.
- `shifts_breaks_calendar.py` `create_break` could attach a break to
  another org's shift; `assign_shift` checked that both the employee and
  shift existed *somewhere*, never that either belonged to the right org.
- `dataset_calibration.py` `run_calibration` could reference another org's
  uploaded dataset; `validate_dataset` / `calibration_status` had no
  owner-check mechanism at all (fixed by deriving the check from the
  record's own stored `org_id` rather than bolting on a redundant
  client-supplied parameter).
- `videos.py` — uploaded videos had **no owner at all** (`org_id` wasn't
  even collected at upload time). Now required at upload, and `analyze`
  defaults to the video's own org rather than a hardcoded global default.
- `streams.py` `list_streams` returned **every org's** active streams,
  with **RTSP credentials embedded in the source URL exposed in plain
  text** (`rtsp://user:pass@host` is a common way cameras are addressed,
  per the reference handler this feature was modeled on). `stop_stream`
  had no ownership check either. All three are fixed — credentials are
  now masked (`rtsp://***:***@host`) before any response is sent, and both
  endpoints are org-scoped.

**A schema/naming audit** also found and fixed:
- `EmployeeFaceGallery.embedding` was named `embedding_json` in the ORM
  model but `embedding` everywhere else (docs, test schema, helper method
  names) — renamed to match.
- `admin_users` and `admin_sessions` existed in the real implementation
  but were missing from Section 7 of the project documentation — added,
  along with the `org_id`/`SUPER_ADMIN` additions from this audit.
- A real functional gap: `record_detection()` always called the
  attendance state machine with `break_windows=[]`, hardcoded — meaning
  `ON_BREAK` could never actually be reached even though
  `shifts`/`break_schedules` had working CRUD and the underlying logic
  was fully correct and unit-tested. Fixed with a real lookup (an
  employee's current shift → that shift's break schedule) and a
  regression test proving a mid-shift detection during a configured lunch
  window now genuinely returns `ON_BREAK`. One related edge case was found
  and documented rather than silently left: an employee's very first
  detection of the day still skips the break check (see "Known
  simplifications" below).

**Also found and fixed while re-verifying everything above**: a real race
condition in the test suite itself — a `StreamWorker` background thread
left running past the end of one test could collide with the *next*
test's fresh database engine during teardown, intermittently crashing the
process with `RuntimeError: deque mutated during iteration`. Fixed by
stopping and joining any leftover worker threads before each test's
database is reset. Confirmed stable across repeated full runs.

**Test coverage for all of the above**: new regression tests cover org-
scoped admin creation, and rejection of cross-org access on employee
enrollment, video upload, video analysis, and the live-results WebSocket —
proving the fixes actually work, not just that the code compiles.

**The frontend needed the same audit.** Fixing the backend's org-scoping
is only real if the UI can actually use it — a follow-up pass found that 7
of the frontend's core actions (Dashboard, Workstations save/check/assign,
Employee enroll/list, Attendance check, Video upload) had `org_id`
**hardcoded to `1` directly in the JavaScript, with no input field at
all** — meaning any org-scoped `HR_ADMIN` account (the exact feature this
audit just built) would get `403`s across most of the app. Fixed by
replacing every scattered/hardcoded org reference with one global "Org ID"
field in the header: it auto-locks to the logged-in account's own org for
an `HR_ADMIN` (there's no other valid value for them anyway) and stays
editable for `SUPER_ADMIN`. Verified end-to-end against the real backend:
a fresh org-5 `HR_ADMIN` account can now enroll an employee, save a
workstation, assign it, and see it on the dashboard — every one of those
would have silently targeted org 1 before this fix.

While in there, the same pass found that `showMsg()` and several
`innerHTML` assignments inserted admin-supplied text (employee names/IDs,
workstation names, stream source URLs, uploaded filenames) into the page
**without escaping it** — a real stored/reflected XSS path on an internal
tool, not a hygiene nitpick, since a malicious value could reach
`sessionToken` in another admin's browser. Added a shared `escapeHtml()`
helper and applied it everywhere user-controlled text reaches the DOM.

## End-to-end dry run (this revision) — a real bug found, not a formality

A full linear dry run was run against a real server instance, walking
through every major user journey in the order a person would actually hit
them — not isolated unit tests, one continuous narrative: unauthenticated
rejection → login/lockout → create an org-scoped admin → cross-org write
rejected → draw a workstation ROI → enroll an employee → cross-org
employee_id collision rejected → assign → dashboard → Try Detection →
upload a video → duplicate-detect it three ways (same content, different
content, different org) → run AI analysis and watch it live over
WebSocket → start/list/stop a live stream with cross-org isolation checks
→ attendance sign-in/break/sign-in → shifts/breaks/calendar → reports →
system health → dataset upload/validation → employee deletion. **55 of 55
checks passed — but the first full run caught a real, previously-unfound
bug** that no isolated test had exercised, precisely because isolated
tests didn't give that employee any history first:

**`DELETE /employees/delete` crashed with a `FOREIGN KEY constraint
failed` error the moment an employee had any real history** — a
workstation assignment, a single attendance record, a shift assignment,
anything. Only `EmployeeFaceGallery` cascades on `employee_id` deletion;
`workstation_assignments`, `employee_attendance`, `attendance_segments`,
`employee_shift_assignment`, and `attendance_exceptions` all reference it
without cascading, so a hard `db.delete(employee)` was rejected by SQLite
the instant any of those rows existed — which is the *normal* case for
this system, not an edge case, since generating exactly that history is
the whole point of the product. This had been present since the delete
endpoint was first built and was never caught, because every prior test
of it used a freshly-enrolled employee with no history.

The fix also corrects a real documentation/implementation mismatch found
in the same pass: `100-key-points.md` (#72) already documented `Employee`
as having a soft-delete `active` flag "preserving history after
offboarding," but the actual endpoint performed a hard delete, directly
contradicting that stated design. Fixed to match the documented intent:
`DELETE /employees/delete` now hard-deletes only the biometric gallery
data (the actual privacy-sensitive "right to be forgotten" concern) and
soft-deletes the employee record (`active=False`), preserving their
attendance/assignment history. `GET /employees/list` now defaults to
active employees only (an admin who just deleted someone expects them
gone from the normal list), with a new `include_inactive=true` option for
an HR-audit view. A permanent regression test
(`test_delete_employee_with_real_history_does_not_crash`) now enrolls an
employee, gives them a workstation assignment, a shift assignment, and an
attendance record — then deletes them and confirms both the deletion
succeeds and the historical records survive it.

A stray, empty `tests/real-app-smoke/data.db` file was also found
committed in the previous package (harmless — 0 bytes — but real clutter
that shouldn't ship) and removed; the cleanup step was broadened to search
for `*.db` anywhere in the tree rather than only the specific paths it had
been checking before.

## Concurrency & multi-tenant scaling — what was actually tested

This was checked with a real test, not assumed: two organizations
(`org_id=301`, `org_id=302`), each with their own org-scoped `HR_ADMIN`
account, ran a full pipeline — enroll an employee, save and assign a
workstation, upload a video, start analysis, and consume live results over
a WebSocket — **at the same time**, via genuine concurrent threads, not
sequential requests that happen to use different org_ids.

**Correctness: confirmed clean.** Org A's employee was correctly matched
to org A's desk, org B's to org B's — zero cross-contamination — despite
both runs sharing the same YOLO and InsightFace model instances and
writing to the same SQLite database file at the same time. This is now a
permanent regression test
(`test_concurrent_multi_org_analysis_no_cross_contamination`), not just a
one-off check.

**Performance: real but partial parallelism, and here's why.** Two
concurrent full pipelines completed in **8.7 seconds**; one alone took
**6.5 seconds**. If the system were fully serialized under the hood,
two runs would take roughly 2× a single run (~13s); if it achieved
perfect parallelism, two runs would finish in the same time as one
(~6.5s). The actual result sits meaningfully closer to the parallel end,
but real contention exists, from two sources:

- **A single shared YOLO/InsightFace model instance** (`app/vision/
  yolo_detector.py`, `app/vision/face_embedder.py`) is used by every
  concurrent `StreamWorker`, so heavy inference calls from two threads
  compete for the same model object and CPU cores, even though Python's
  GIL is released during the actual C++/native compute inside PyTorch and
  onnxruntime.
- **SQLite allows only one writer at a time.** Every `StreamWorker`
  thread writes its detection events to the same database file; under
  concurrent load, one thread's write transaction briefly blocks the
  other's.

**What this means practically:**
- For a **small number of simultaneous orgs/streams on one server
  process** (the realistic footprint for a pilot or a modest deployment),
  this works correctly today, with a real (if partial) throughput benefit
  from concurrency — verified, not theoretical.
- For **real production multi-tenant scale** (many organizations, many
  simultaneous live streams), two specific changes would matter, and both
  are already anticipated elsewhere in this documentation rather than
  being new advice: switch `DATABASE_URL` to PostgreSQL (removes SQLite's
  single-writer bottleneck; the ORM models already work unmodified), and
  revisit the single shared model instance in favor of either multiple
  model instances or a proper inference queue — the same territory as the
  TensorRT/OpenVINO acceleration discussion in the project documentation.
- **One more real limit worth naming explicitly**: `_workers` (active
  streams), `_videos` (uploaded videos), `_uploads`/`_jobs` (dataset
  calibration) are all plain in-memory Python dicts, scoped to a single
  server process. They are not shared across multiple server instances
  behind a load balancer — calling `/streams/stop` on a *different*
  process than the one that started that stream would not find it. This
  is fine for a single-process deployment (what this project ships as)
  and is a real architectural constraint to resolve (e.g. moving this
  registry into Redis or the database itself) before running multiple
  backend processes for the same deployment.

## Known simplifications worth fixing before any real deployment

- Multi-role RBAC (viewer vs. admin) isn't modeled — only one role exists.
- `record_detection` / `simulate_detection` are still directly reachable
  from the frontend for demo purposes; in a hardened deployment these
  should only be callable by the internal stream-processing worker.
- CORS origin, cookie security flags, and rate limiting beyond login
  lockout are not yet environment-branched per Section 10.3 of the docs.
- The calibration curve fit in `dataset_calibration.py` is a simplified
  single global fit, not the full near/global split in the original ADAR
  design.
- No HLS output — the live stream processes real video but doesn't
  produce a viewable feed back out.
- **Three tables are schema-only, not yet populated**: `workstation_daily_analytics`,
  `workstation_vacancy_logs`, and `workstation_identity_daily_analytics` exist
  (documented in Section 7) to support daily/aggregate reporting, but no rollup
  job writes to them yet — `/reports/desk-utilization` currently derives its
  numbers directly from raw `workstation_identity_events` rows instead. Building
  the rollup job is a reasonable next step once real event volume makes
  per-request aggregation too slow.
- **Day-length thresholds are hardcoded**, not read from an employee's
  actual assigned shift: `close_stale_sessions()` classifies a day as
  FULL/HALF/ABSENT using fixed 7-hour/4-hour thresholds regardless of
  what that employee's shift record actually specifies. Scheduled
  *breaks* are correctly looked up per-employee via `shifts`/`break_schedules`
  (fixed in this revision — see below); day-length classification using
  the same per-shift data is the natural next step.

## Database audit (this revision)

A systematic re-check across the ORM models, the documented schema, the
SQLite test mirror, and every router that touches the database found and
fixed three real issues, plus flagged one for the list above:

1. **Column naming mismatch**: `EmployeeFaceGallery.embedding` was named
   `embedding_json` in the ORM model but `embedding` everywhere else
   (the documentation, the test schema, the getter/setter method names).
   Renamed the column to match.
2. **Two undocumented tables**: `admin_users` and `admin_sessions` were
   added to the real implementation for authentication but were never
   added to Section 7 of the project documentation. Added them.
3. **A real functional gap**: `record_detection()` always called the
   attendance state machine with `break_windows=[]`, hardcoded — meaning
   `ON_BREAK` could never actually be reached through the real endpoint,
   even though `shifts`/`break_schedules` had working CRUD and the
   underlying state-machine logic was fully correct and unit-tested. Added
   the real lookup (an employee's current shift assignment → that shift's
   break schedule) and a regression test proving a mid-shift detection
   during a configured lunch window now genuinely returns `ON_BREAK`.
   **One related edge case found while verifying this fix and not yet
   handled**: an employee's very *first* detection of the day always
   creates the attendance record with `status="PRESENT"` directly,
   without consulting break windows at all — so if someone's first-ever
   sighting of the day happened to fall inside a scheduled break, they'd
   be marked PRESENT instead of ON_BREAK for that moment. Unlikely in
   practice (it assumes no earlier arrival was ever detected that day),
   but a real gap, not a hypothetical one.

## License / attribution

`sample_data/test-photo.jpg` is the "Zidane" test image bundled with the
Ultralytics YOLO package, commonly used across the YOLO ecosystem for
exactly this kind of demo purpose. `sample_data/demo-camera-feed.mp4` was
generated from it for this project.
