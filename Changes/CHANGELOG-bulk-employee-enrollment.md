# Changelog — Bulk Employee Enrollment (zip-per-employee)

Session: 2026-09-18. Scope: wire up multi-distance, multi-employee face
enrollment on the Employees tab, plus a "Train model" button, per the
workflow discussed with the user.

## Context — what was already there

Before touching anything, the backend was inspected directly
(`backend/app/routers/dataset_calibration.py`). It already fully
implements the workflow the user described as their "second approach":

- `POST /employees/enroll_from_zip` — one zip per employee, declared
  distances (`"0.5,1,2,3,4,5"`), matches `depth_XXXm/` folders against
  them, enrolls all 4 views (front/left/right/top) from the closest
  matching distance, keeps the zip on disk for later calibration.
- `POST /calibration/run_all` — fits the distance-decay curve
  (`near_k`, `near_sigma0`) across every employee enrolled via the zip
  flow so far ("train model").
- `GET /employees/enrolled_distances/{org_id}` — reports the distance
  set already fixed for an org, so every employee's zip is checked
  against the same distances instead of each admin typing their own.
- `Employee.department` already exists as a DB column and is accepted
  by `enroll_from_zip`.

**None of this was reachable from the UI.** `frontend/index.html` had
zero references to `enroll_from_zip`, `calibration/run_all`, or
`enrolled_distances` — only the older single-photo `/employees/enroll`
form was wired up. There was also no test coverage
(`grep -rl "enroll_from_zip" tests/` → no matches) and the
`docs/enrollment-workflow.md` referenced in the backend's own comments
does not exist in the repo.

This session's work is therefore almost entirely **frontend**, plus one
small backend gap fix (below). It does not change any face-matching,
detection, or calibration logic — that code was read, tested against
the live server, and used as-is.

## Changes

### `backend/app/routers/employees.py`
- `GET /employees/list` now includes `department` in each employee's
  JSON. Previously it queried employees normally but dropped this
  field from the response, even though the DB column and
  `enroll_from_zip` both already populate it — the enrolled-employees
  table had no way to display it.

### `frontend/index.html` — Employees tab
Two new cards added between the existing single-photo enroll form and
the "Enrolled employees" table:

1. **"Bulk enroll from a dataset zip"**
   - Employee ID / Name / Department fields, one zip file input.
   - Distances field: calls `GET /employees/enrolled_distances/{org_id}`
     each time the Employees tab is opened
     (`loadDeclaredDistances()`). If the org already has a locked
     distance set, it's shown read-only and reused automatically; if
     not, the admin types a comma-separated list (e.g. `0.5,1,2,3,4,5`)
     which becomes the fixed set for every later employee in that org.
   - A `<pre>` block shows the expected zip folder layout
     (`<folder>/depth_XXXm/{front,left,right,top}.jpg`), matching the
     structure of the sample dataset the user provided.
   - Submits to `POST /employees/enroll_from_zip`; the response is
     rendered in full — views enrolled, views skipped (with reason),
     unexpected/ignored distance folders, and whether the employee has
     enough distances to contribute to calibration
     (`calibration_ready`).
2. **"Train model"** — one button, calls `POST /calibration/run_all`
   then immediately `GET /calibration/status/{job_id}` (the job
   actually completes synchronously server-side despite the
   `"status": "queued"` response shape — confirmed by reading
   `run_calibration_all()`), and shows the fitted `near_k` /
   `near_sigma0`, how many people were enrolled, how many only had one
   distance (and so didn't contribute), and the data-point count.

The existing single-photo `/employees/enroll` form and its "Enroll this
view" button are unchanged and still work — this is an additional
option, not a replacement. The "Enrolled employees" table gained a
Department column.

`nav` button for "Employees" now also calls `loadDeclaredDistances()`
on click, so the distance-lock state is current every time the tab is
opened.

### CSS
Added `#zipExample` style for the folder-structure `<pre>` block
(monospace, light background, horizontal scroll for long paths) —
matches the existing `.card` / `.hint` visual language, no new colors
introduced.

## Verification

1. **Syntax**: extracted the `<script>` block and ran `node --check`
   after every edit (2 passes — once after the main change, once after
   the department-blank-field fix below). `python3 -m py_compile` on
   `employees.py`. Both clean.
2. **Live end-to-end run** — started the actual backend
   (`uvicorn app.main:app`, fresh SQLite DB), logged in as the seeded
   default admin, and called the real endpoints with `curl` using a
   zip built from the user's own sample dataset
   (`dataset/Abhishek/`, all 6 depth folders):
   - `GET /employees/enrolled_distances/1` before any enrollment →
     `{"distances": null}` ✓
   - `POST /employees/enroll_from_zip` (EMP-2001, distances
     `0.5,1,2,3,4,5`) → `status: enrolled`, all 4 views enrolled,
     `calibration_ready: true`, `department: "Engineering"` ✓
   - `GET /employees/enrolled_distances/1` after → distances now
     locked to `[0.5, 1.0, 2.0, 3.0, 4.0, 5.0]` ✓
   - `GET /employees/list?org_id=1` → department field now present in
     the response ✓
   - Second employee (EMP-2002) enrolled with only one matching
     distance folder → `calibration_ready: false` ✓ (confirms the
     "needs more distances" UI message path is reachable)
   - `POST /calibration/run_all` → `job_id` returned, then
     `GET /calibration/status/{job_id}` → `COMPLETED`, `near_k: 0.2`,
     `people_enrolled: 2`, `people_with_single_distance_only:
     ["EMP-2002"]`, `calibration_data_points: 20` — matches the exact
     shape the new `runCalibrationAll()` JS expects.
3. Ran against the **real face embedder in `stub` mode** (same
   limitation as the earlier session — `insightface`/`torch` could not
   be installed in this sandbox due to disk space). All enrollment
   logic, distance matching, and calibration math were exercised for
   real; face-detection quality itself was not.
4. Test DB / uploaded photos / zips created during testing were
   deleted before packaging the deliverable; no test artifacts are
   included in the delivered files.

## Remaining Issues / Not Done

- **`docs/enrollment-workflow.md` still doesn't exist**, despite being
  referenced by a comment in `dataset_calibration.py`. Worth writing
  if this is meant to be the primary enrollment path going forward.
- **No automated tests** for `enroll_from_zip` / `run_all` /
  `enrolled_distances` exist anywhere in `tests/` — this session
  verified behavior manually against a live server, which is evidence
  the endpoints work as coded, but there's nothing to catch a
  regression later. Recommend adding pytest coverage before this
  becomes the primary enrollment path.
- **Multi-person-per-frame face matching is a separate, larger change**
  — discussed with the user but explicitly out of scope for this
  session. Today `workstations/simulate_detection` extracts and
  matches only one face per frame even though YOLO detects all
  people present. Not touched here.
- **Real-world FPS at production scale is a hardware/deployment
  question**, not a code question — not something this session could
  answer without knowing the target GPU/CPU. Flagged to the user as
  such rather than guessed at.
- **Face-detection quality at 2m+ distances remains unverified**
  against the real `insightface` backend (only `stub` was available in
  this sandbox) — carried over from the earlier session's finding
  that far-distance sample images drop to ~32–60px and may not embed
  reliably under a real model. The `views_skipped` / `calibration_ready`
  UI added this session will surface this correctly once real
  detection is active (it already did so in test #2's
  single-distance-only case above), but the underlying image-quality
  concern itself has not been re-checked.
- Blank Department is now correctly omitted from the form submission
  (stored as `NULL`) rather than sent as an empty string — fixed
  during this session, verified via the second JS syntax check, not
  re-verified with a live `curl` call (low risk, straightforward
  `if (department)` guard).
