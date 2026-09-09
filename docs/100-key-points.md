# 100 Key Development Points
### Employee Identity-Aware Workstation Monitoring & Attendance System

This file is the single running checklist of every material decision made across the project's design discussions (Project 1 "EM Model" + Project 2 "ADAR" + all subsequent merges, optimizations, and new features). It is the source of truth used to update the main project documentation. Points are grouped by topic; numbering is sequential across the whole list.

---

## Project 1 — "EM Model" foundation (1–10)

1. Project 1 ("EM Model") is a FastAPI-based workstation occupancy detection system using YOLOv8s to detect people within camera-defined ROIs.
2. ROIs are stored as normalized (0.0–1.0) coordinates, making zone definitions resolution-independent across cameras.
3. Occupancy uses a debounced ACTIVE/VACANT state machine with a grace period (originally 3 seconds) to prevent flicker from momentary detection misses.
4. Live video is served via FFmpeg-encoded HLS (H.264, NVENC-or-libx264), independent of the occupancy decision loop.
5. Real-time status changes are pushed via Server-Sent Events (SSE), not polling.
6. PostgreSQL stores three original tables: `workstations`, `workstation_daily_analytics`, `workstation_vacancy_logs`.
7. Original API surface: 8 endpoints (streams start/stop/list/events, workstations save/check/delete, health check).
8. The project is multi-tenant by design (`org_id` + `cam_id` scoping throughout), suggesting a SaaS-style deployment model.
9. Project 1 ships no frontend UI — it is API-only; a separate frontend (e.g., React) is assumed.
10. Deployment uses Docker (CUDA-based image) with GitHub Actions auto-deploy to GPU EC2 instances.

## Project 2 — "ADAR" face recognition foundation (11–25)

11. Project 2 ("ADAR") is a distance-adaptive face recognition system: it fits a per-deployment SNR decay curve so confidence explicitly degrades (and is reported) with distance, instead of applying one fixed similarity threshold.
12. ADAR uses InsightFace (`buffalo_l` pack: SCRFD/RetinaFace detector + ArcFace r100 recognition) via `onnxruntime`, with a DirectML > CUDA > CPU provider fallback chain.
13. Enrollment captures 4 views per person: front, left, right, top — chosen specifically to give the matcher a reference for multiple head poses.
14. Dataset folder convention: `dataset/<person_id>/depth_XXXm/{front,left,right,top}.jpg`, parsed via a strict `depth_XXXm` regex.
15. Calibration (`calibrate.py`) fits decay parameters (`k`, `gamma`, `sigma0`) via nonlinear least-squares (`scipy.curve_fit`) on (depth, similarity) pairs, using each person's smallest captured depth as their reference embedding.
16. Two curve fits are produced: a "global" fit (all data) and a "near-range" fit (<10m, real data only); `api_server.py` prefers the near-range parameters (`near_k`, `near_gamma`, `near_sigma0`) whenever available.
17. More, denser real calibration data measurably improves the fit — documented before/after: `near_k` dropped from 0.414→0.213 and `gamma` properly separated from 0 to 0.627 once more real distances were captured.
18. A calibration group of 5–10 volunteers captured at 3–4 real distances is sufficient; the full enrolled population does **not** need multi-distance capture — only their single reference-distance enrollment.
19. Volunteer/employee folder names are not parsed for meaning by the code — any unique folder name works (real names, "CALIB-01", or employee IDs).
20. The matching function computes a distance estimate per person (via apparent face-width ratio to their calibrated reference width), corrects the embedding by an alpha factor, then computes an SNR-gated similarity — explicitly allowing "I don't know" instead of forcing a match.
21. Known, documented sources of estimate error: face-width variation between individuals (mitigated by per-person reference width), bounding-box jitter, head pose (apparent width narrows off-frontal), and uncorrected lens distortion.
22. ADAR's own README explicitly states it is a research/prototype, **not audited** for production security, adversarial robustness, or demographic bias — a real caveat carried into this merged project's compliance section.
23. Original ADAR API surface (`api_server.py`): `/detect`, `/health`, `/stream/start`, `/stream/stop`, `/stream` (MJPEG), `/report`, `/report/csv`.
24. ADAR ships supplementary tools: `calibrate.py`, `enroll_capture.py` (phone-as-remote-shutter enrollment), `adar_admin.py` (all-in-one admin panel), `fr_query.py`, `stream_tracker.py`, `rtsp.py`, `diagnose_gpu.py`.
25. ADAR's `requirements.txt` targets Windows + DirectML by default (any DirectX12 GPU, no separate CUDA toolkit needed) — a different assumed runtime environment than Project 1's CUDA-based Docker image, reconciled during the merge.

## `stream_tracker.py` / `fr_query.py` — the lazy-FR architecture (26–32)

26. `stream_tracker.py` and `fr_query.py` already implement the core "don't run FR every frame" idea: a cheap detector runs every frame (`detect_faces_only()`), while the expensive embedding+match (`identify_face()`) only fires per tracked face on a configurable interval (`fr_interval_sec`) — not continuously.
27. Even with that split, the actual CPU driver at ~10fps was diagnosed as the **detector** itself still running full `buffalo_l`/SCRFD at 640×640 on every frame — not the (correctly rare) identification calls.
28. The single biggest architectural fix identified: decouple face-recognition cadence entirely from video frame rate, triggering identification off the occupancy state machine's VACANT→ACTIVE transition plus a slow heartbeat (e.g., 30–60s) instead of a fixed per-track timer.
29. Secondary fixes: use a smaller/faster model (`buffalo_s` or SCRFD-500m at reduced `det_size`, e.g., 320×320) for the frequent per-frame pass; reserve the larger accurate backbone only for the rare identification calls.
30. Crop face detection to the workstation ROI (+padding) rather than the full frame — a synergy only possible once merged with Project 1's known desk rectangles.
31. The gallery-matching loop was found to be O(persons × total_gallery_entries) due to re-scanning the full gallery dict per candidate person; fixed by pre-grouping the gallery by `person_id` once at load time, reducing it to a single O(gallery_size) pass.
32. Recommended additional runtime tuning: explicit `onnxruntime` `intra_op`/`inter_op` thread caps (to avoid contention with YOLO + FFmpeg on the same box) and evaluating the OpenVINO execution provider on Intel CPUs.

## ArcFace → AdaFace evaluation (33–38)

33. AdaFace (a quality-adaptive margin loss family) was identified as a promising upgrade over plain ArcFace specifically because it's designed to stay more stable on low-quality, off-angle, longer-distance captures — exactly the conditions a ceiling/wall-mounted workstation camera produces.
34. AdaFace is not natively bundled in InsightFace's model zoo; adopting it requires exporting PyTorch weights to ONNX and wrapping it to match where `rec_model.get()` is currently called.
35. Switching recognition backbones invalidates the entire existing gallery — full re-enrollment is mandatory (embeddings from different models are not comparable).
36. Similarity-score distributions differ between backbones — `SNR_ACCEPT_THRESHOLD` and `DEFAULT_SIM_ACCEPT_THRESHOLD` must be re-calibrated from scratch after any backbone swap, not reused.
37. Backbone size choice matters: IR-50 (closer to `buffalo_s`'s CPU-friendliness) vs IR-101 (closer to `buffalo_l`'s accuracy) is a deliberate trade-off to make, not a default.
38. Decision status: AdaFace is a **recommended, evaluated** optimization path — not yet adopted in the current architecture.

## Camera placement, distance & pose guidance (39–48)

39. Recognition accuracy is governed by pixels-across-the-face, not distance in meters directly; ISO/IEC face-image-quality standards recommend ≥120px inter-eye distance (best practice) / ≥90px (minimum), with common vendor "positive ID" thresholds around 80–120px face width.
40. For a typical 1080p, ~70° HFOV fixed camera, the reliable zone is roughly 0.5–2.5m, a degraded-but-usable zone extends to ~4m, and beyond ~4m recognition becomes unreliable on standard hardware.
41. Pythagorean geometry applies directly to fixed-height mounted cameras: the minimum achievable slant distance to any subject equals the mount height itself — meaning mount height alone can put the **entire floor area** outside the "excellent" confidence zone.
42. Pose degrades accuracy differently by rotation axis: roll (least impact) < yaw/profile (moderate, usable to ~45°) < pitch (worst, steepest decline) — and critically, a face viewed **from above** (a ceiling-mounted, downward-looking camera) degrades more than the same angle viewed from below.
43. This means a steep top-down camera angle can hurt recognition as much as or more than distance alone — the two factors compound rather than being independent problems.
44. Practical mitigation #1: capture the "top" enrollment view at the **actual deployed camera's** downward angle, not a generic head-tilt — matching enrollment pose to deployment pose is free and high-leverage.
45. Practical mitigation #2: build the calibration set from real captures spanning the actual 3–6m operating band (not a broad generic range), since the near-range curve fit (point 16) is only as good as the real data feeding it.
46. Practical mitigation #3: trust and do not "tune away" the SNR gate's UNKNOWN outputs in the 3–6m borderline band — that's the system correctly reporting low confidence, not a bug to route around.
47. Practical mitigation #4 (identified gap, not yet fixed): lens distortion (barrel/pincushion, worst at wide-angle frame edges) is explicitly documented as uncorrected in ADAR's own algorithm notes — a one-time camera calibration + `cv2.undistort()` pass is a legitimate, currently-missing improvement.
48. Where possible, mounting cameras at a moderate downward angle (not near-vertical top-down) and/or lowering mount height keeps the effective pose and distance closer to the reliable zone.

## Compute & scaling analysis (49–52)

49. Detection cost (YOLO + face detector) depends only on frame size and number of visible faces — **not** on how many employees are enrolled; this does not change whether 2 or 500 people are in the gallery.
50. Matching cost at 50 enrolled employees is negligible in absolute terms (a few hundred 512-dim vector comparisons, sub-millisecond) even before the O(persons×gallery) loop fix — but the fix matters for future headcount growth.
51. Enrollment/calibration are one-time, offline costs (seconds, not a recurring runtime cost) — e.g., ~200 embedding extractions for 50 people × 4 views takes roughly 6–12 seconds total.
52. Storage cost is trivial at this scale (~400KB for 50 people's embeddings); pgvector is recommended not because current scale needs it, but so nearest-neighbor search stays indexed/fast if headcount grows into the hundreds.

## Terminology & the two-timer model (53–58)

53. "Login/Logout" is reserved exclusively for administrator/HR authentication into the management portal software.
54. "Sign-in/Sign-out" is reserved exclusively for employee physical presence, derived from camera detections — never a software action the employee performs.
55. Two independent timers exist per employee: an organization-wide presence timer (drives Sign-in/Sign-out, active on any camera) and a per-desk utilization timer (drives MATCH/MISMATCH/VACANT for one workstation) — both can be simultaneously "correct" even when they disagree.
56. Example resolved by the two-timer model: an employee helping in another department keeps their sign-in timer running while their home desk correctly shows VACANT — not a contradiction.
57. Four workstation identity states are canonical: MATCH, MISMATCH, UNKNOWN, VACANT.
58. Attendance state machine: `SIGNED_OUT → PRESENT` (first detection) `→ ON_BREAK` (scheduled window, time-based not detection-based) `→ PRESENT → SIGNED_OUT` (unscheduled gap beyond away-timeout), with manual admin override available at any state.

## Attendance scenario handling (59–69)

59. Sign-out is never marked at the moment of last detection — it's set retroactively once an away-timeout elapses, backdated to the actual last-seen timestamp.
60. A short grace period (e.g., 10–15 min) absorbs normal blind-spot gaps (restroom, stairwell) without triggering false sign-out; only exceeding the longer away-timeout (e.g., 45–60 min) counts as a real departure.
61. Scheduled breaks (lunch, etc.) pause counted work-duration deterministically by time window, regardless of whether the employee is actually detected during that window — never inferred from detection gaps alone.
62. Off-site/field work is **not** inferable from cameras and requires an explicit administrator exception record, with reason and acting-admin audit trail.
63. Camera/pipeline outages are treated as system-health events that **suspend** automatic sign-out logic for affected cameras, followed by manual reconciliation — never silently generating mass false sign-outs.
64. Late arrival / early departure / overtime are derived by comparing detected sign-in/out times against a configured shift schedule, not hardcoded.
65. Day classification (Full/Half/Absent) is threshold-based on **net** present duration (breaks already subtracted).
66. Multiple departures/returns in one day keep **one** summary sign-in/out pair for simple reporting, while every gap is separately logged in `attendance_segments` for detailed breakdowns.
67. An organization calendar (holidays, weekly-offs) suppresses attendance/absence computation on non-working days.
68. False negatives (system fails to detect a genuinely present employee) require an auditable manual correction flow — no fully-automated system is assumed to be perfect for HR-relevant data.
69. Presence is tracked per individual identity regardless of how many other people share a camera view/ROI simultaneously (tailgating-safe by design).

## Database schema (70–74)

70. The schema grew to **17 tables** across 3 functional groups: video/workstation config (3), employee identity & assignment (5), attendance/shifts/admin (9).
71. `workstation_assignments` and `employee_shift_assignment` are both kept as append-only history (`effective_from`/`effective_to`), never overwritten, so reassignments don't corrupt historical reports.
72. `employees.employee_id` is a TEXT primary key sourced from the HR system (not auto-generated), and employees carry an `active` soft-delete flag rather than being hard-deleted, preserving history after offboarding.
73. `pgvector` is the recommended storage type for face embeddings specifically so nearest-neighbor search is indexed at the database level, not looped in application code.
74. `admin_login_audit` is a fully separate table from `employee_attendance` — portal authentication events and physical presence events never share a table or logic path.

## API surface (75–77)

75. The full current API surface spans **35 endpoints across 9 groups**: video/streams, workstation/ROI, employee/enrollment, dataset upload & calibration, attendance (sign-in/out), admin auth, shift/break/calendar config, reporting, and system health.
76. `/reports/desk-utilization` (formerly loosely called "attendance") was deliberately renamed to avoid colliding with the new organization-wide Sign-in/Sign-out attendance concept — they measure genuinely different things.
77. The SSE endpoint (`/streams/events/{stream_id}`) emits one event per genuine occupancy/identity **change**, not per video frame — consistent with the event-driven design throughout.

## Setup UI workflow (78–79)

78. The end-to-end setup flow is: upload dataset zip → automatic structural validation (with per-file, per-person error reporting, not just pass/fail) → calibration/training job → drag-to-draw ROI creation on a live/snapshot frame → assign enrolled employee to each ROI → live dashboard.
79. Validation failures must name the exact person/file/issue (e.g., "EMP-1004 missing top.jpg in depth_001m") rather than a generic error, so the flow is self-service correctable.

## Deployment (80–83)

80. Local development uses fixed ports (backend 8001, frontend 8002); Lightning.ai Studios expose each port as a distinct **subdomain** (e.g., `8001-<studio-id>.lightning.ai`) rather than a path prefix.
81. The backend base URL is resolved **client-side** by inspecting the browser's own `window.location` and pattern-matching/swapping the port prefix — deliberately avoiding any dependency on server-only, less-documented platform environment variables that browser JS can't see anyway.
82. Because frontend and backend land on different subdomains, this is a genuine cross-origin setup: CORS must allow the exact frontend origin (never a wildcard) whenever credentials are used.
83. Session cookies need `SameSite=None; Secure` for the cross-subdomain HTTPS cloud environment, but that combination fails on plain-HTTP localhost — cookie config must branch by environment, not use one fixed setting everywhere.

## India market & compliance (84–86)

84. Target market is India; India's DPDP Act 2023 does **not** classify biometric data as a special/sensitive category (unlike GDPR Article 9), and Section 7(i) provides a legitimate-use basis for employment-purpose processing without mandatory per-instance consent.
85. Despite the more permissive statute, consent, a clear privacy notice, and a defined retention/deletion policy remain the recommended baseline — continuous workplace biometric monitoring is still a "contested, not fully settled" reading of Section 7(i) per practitioner commentary, and full DPDP compliance obligations phase in by 13 May 2027.
86. India-specific priority use cases (in order of fit): BPO/call-center seat-to-agent reconciliation (strongest fit, largest existing local competitor validation — Truein, OLOID, KENT CamAttendance, BioEnable), IT/ITES return-to-office verification, manufacturing operator certification, warehousing/logistics attribution, and BFSI compliance-zone auditing.

## Person detection model decision (87–90)

87. Project 1's original YOLOv8s choice is still viable but is no longer state-of-the-art; current (2026) alternatives evaluated include YOLO26 (Ultralytics, Jan 2026 — NMS-free, up to 43% faster CPU inference, better small-object detection) and RF-DETR (transformer-based, Apache-2.0, strong accuracy-per-millisecond and domain transfer).
88. A real licensing constraint was surfaced: YOLO26 and YOLOv12 are AGPL-3.0 (requiring open-sourcing derivative work or a paid commercial license for closed-source use), whereas RF-DETR (Apache 2.0) and RTMDet (MIT) are permissively licensed — a deliberate business decision, not just a benchmark decision.
89. Pose-estimation models (e.g., YOLOv8-pose, RF-DETR Pose, DETRPose) were evaluated as a possible **replacement** for plain person detection and explicitly **rejected** for that role: they cost more compute per frame, most lower-body keypoints are wasted since desks occlude them, and body-pose keypoints are too coarse to replace the dedicated face detector's landmarks needed for actual identification alignment anyway.
90. The base RT-DETR architecture has no native pose variant; pose-capable DETR-family options (DETRPose, RF-DETR Pose) are separate, purpose-built derivative models, not RT-DETR itself with a bolted-on head.

## Activity detection (new feature) (91–93)

91. A new, **independent** activity-classification track was added: 5 fixed classes (Sitting, Standing, Walking, Leaning/Bending, Standing-Up/Sitting-Down transition) plus a mandatory UNKNOWN fallback for low-confidence/occluded keypoints.
92. Activity detection is intentionally decoupled from both occupancy detection and identity recognition — it runs on its own reduced sampling cadence (not per-frame), using pose keypoints (RF-DETR Pose, given its surveillance-camera design focus and permissive license) purely for this feature, not for identity.
93. UNKNOWN is expected to be the single **most common** class in practice, not a rare edge case — most of the lower body is desk-occluded for most of the workday, and this limitation should be set as an expectation upfront, not discovered as a "bug" during testing.

## Performance & optimization strategy (94–96)

94. TensorRT is GPU-only (NVIDIA, Compute Capability 7.0+ recommended, 6.1+ for INT8) and is the primary target for the ≥20 FPS requirement on GPU-backed Lightning.ai cloud instances, typically delivering a documented 2–10x speedup over plain ONNX Runtime once the engine is built/cached.
95. OpenVINO is the equivalent optimization path for Intel-CPU-only environments (e.g., local development without a dedicated GPU); plain CPU-only ONNX Runtime without either accelerator is the least reliable path to sustaining 20 FPS and should not be assumed adequate for production.
96. The 20 FPS target applies specifically to the continuously-running stages (person detection, the lightweight per-frame face detector, HLS video encoding) — **not** to identification, which remains event-driven/low-frequency by design and has no FPS requirement of its own.

## Security (97–99)

97. Security responsibilities span three distinct layers that must each be addressed, not just one: (a) web/API layer — auth hardening, rate limiting, RBAC, input validation; (b) data layer — encryption at rest/in transit, secrets management, audit logging; (c) the biometric system itself — liveness/anti-spoofing against photo/video replay attacks, which is a materially different attack surface than a typical web app.
98. Bot/abuse mitigation for admin login specifically requires layered controls: per-IP and per-account rate limiting, progressive lockout after repeated failures, and CAPTCHA/challenge on suspicious patterns — a single control alone (e.g., just a strong password policy) is insufficient.
99. Every administrator override (`attendance_exceptions`) and every portal auth event (`admin_login_audit`) already carries an audit trail by design — security design should extend that same audit-everything principle to any new privileged action added later, not treat auditing as an afterthought per-feature.

## Testing (100)

100. A 300+ test `pytest` suite was scaffolded to serve as a running acceptance checklist against this specification — organized by API group, database constraints, attendance state-machine scenarios, face-recognition logic, activity detection, security, and performance/FPS benchmarks — with a companion `setup.sh` to provision the test environment consistently.
