# Changelog — CORS fix + backend/frontend logging

Project: UCT-WS-Monotering
Prepared for: Gudsky / Abhishek Kumar Shukla
All timestamps in IST (Asia/Kolkata).

This records every change made in this session, in the order investigated,
so it can be checked off against your local copy and the git history.

---

## 1. Investigation — 2026-09-18, 09:12 IST

**Reported symptoms:**
- CORS policy error on frontend.
- No backend log file and no frontend log file, so the exact failure
  couldn't be pinned down.

**Files analyzed:** full repo extracted from `UCT-WS-Monotering.zip`
(FastAPI backend in `backend/app/`, single-file frontend in
`frontend/index.html`).

**Root cause found (confirmed by running the real backend and calling it
with curl from matching and mismatched Origin headers):**

`backend/app/main.py` configured CORS with a single allowed origin:
```python
_frontend_origin = os.environ.get("FRONTEND_ORIGIN", "http://localhost:8002")
app.add_middleware(CORSMiddleware, allow_origins=[_frontend_origin], ...)
```
No `.env` file existed in the project (only `.env.example`), so the
backend was always running with the default `http://localhost:8002` as
the ONLY allowed origin. Any other origin — in particular a Lightning.ai
Studio frontend, which is served from a different subdomain than the
backend (e.g. `https://8002-<studio-id>.lightning.ai` vs
`https://8001-<studio-id>.lightning.ai`) — is a genuine cross-origin
request and gets silently rejected by the browser. This matches what
your own project docs already describe
(`docs/100-key-points.md`, points 80–83).

**Second, independently confirmed bug (found directly in your uploaded
files via the `file` command, not hypothetical):**

`run_backend.sh` and `setup.sh` (and `LICENSE`) had **CRLF line
endings** in the zip you provided — almost certainly from being edited
on Windows at some point. If these are pushed to git as-is and pulled on
Lightning's Linux environment, `./run_backend.sh` fails immediately with:
```
/usr/bin/env: 'bash\r': No such file or directory
```
because the shebang line itself (`#!/usr/bin/env bash`) has a trailing
`\r`. If this happened to you, the backend never starts at all — which
alone would explain **both** "no backend log file" (nothing ever ran)
and a browser error that looks like CORS (the browser reports a
connection failure/CORS-style error either way, since `fetch()` gives no
detail on *why* a request failed).

**Self-correction, for the record:** In my previous message I incorrectly
claimed `GET /` was not implemented anywhere and added a new root route.
That was wrong — `backend/app/routers/streams.py` already defines
`@router.get("/")` returning `{"status": "ok", "service":
"workstation-monitoring"}`, and it was already working correctly. I
removed the duplicate route I had mistakenly added in `main.py` once I
re-checked and found the existing one. No functional issue there.

---

## 2. Backend changes

### 2.1 `backend/app/logging_config.py` — **new file**, 2026-09-18, 09:15 IST

- Adds `setup_logging()`, which configures the ROOT Python logger with:
  - A `RotatingFileHandler` writing to `backend/logs/backend.log`
    (5 MB per file, 3 backups kept).
  - A console `StreamHandler` (so terminal output is unchanged).
- Also re-points uvicorn's own loggers (`uvicorn`, `uvicorn.error`,
  `uvicorn.access`) at the same two handlers. Uvicorn configures those
  with `propagate=False` by default, so without this step uvicorn's own
  request/access logs would keep bypassing the file and stay
  console-only.
- **Why:** previously the only logging anywhere in the backend was a
  console-only `StreamHandler` on the `"websockets"` logger inside
  `routers/streams_ws.py` — nothing was ever written to disk, so once a
  terminal was closed or scrolled past, there was no record of what the
  backend had actually seen.

### 2.2 `backend/app/main.py` — **modified**, 2026-09-18, 09:18 IST

1. **`setup_logging()` is now called first thing**, before any router is
   imported (routers/streams_ws.py grabs a logger at import time, so
   this has to run before that import happens).
2. **`FRONTEND_ORIGIN` now accepts a comma-separated list of origins**,
   e.g.:
   ```
   FRONTEND_ORIGIN=http://localhost:8002,https://8002-<studio-id>.lightning.ai
   ```
   parsed into a Python list and passed to `CORSMiddleware(allow_origins=...)`.
   This is still an explicit allow-list (never a wildcard `"*"`, per
   your own docs' Section 10.3 rule about credentials) — it just lets
   local dev and a Lightning Studio both be allowed at the same time,
   so you don't have to edit `.env` every time you switch between them.
3. **Added `RequestLoggingMiddleware`** (`starlette.middleware.base.BaseHTTPMiddleware`),
   registered *after* `CORSMiddleware` so it wraps outside it and still
   sees rejected preflight (`OPTIONS`) requests. For every request it
   logs, to `backend/logs/backend.log`:
   - method, path, response status, duration in ms
   - the browser's `Origin` header (or `-` if none was sent)
   - **whether the response actually carried an
     `Access-Control-Allow-Origin` header** — this is the single fastest
     way to tell, from the log file alone, whether a browser "CORS
     error" is really an origin mismatch. Example log lines (from live
     testing, see §4 below):
     ```
     GET / -> 200 (0.5ms) origin=https://8002-someid.lightning.ai cors_header_present=NO (check FRONTEND_ORIGIN)
     GET / -> 200 (0.4ms) origin=http://localhost:8002 cors_header_present=yes
     ```
4. **Startup log line** added: `Allowed CORS origins (FRONTEND_ORIGIN): [...]`
   printed once at boot, so you can confirm at a glance what the running
   backend actually allowed.
5. Removed a duplicate `@app.get("/")` I had mistakenly added earlier in
   this session (see the self-correction note in §1) — the existing one
   in `routers/streams.py` already covers this.

### 2.3 `.env.example` — **modified**, 2026-09-18, 09:24 IST

- Updated the `FRONTEND_ORIGIN` comment to document the new
  comma-separated format, with a concrete Lightning.ai example.
- Added an optional `LOG_DIR` variable (defaults to `backend/logs` if
  unset).

### 2.4 `run_backend.sh`, `setup.sh`, `LICENSE` — **line-ending fix**, 2026-09-18, 09:31 IST

- Converted CRLF → LF (`sed -i 's/\r$//'`) in all three files, which had
  CRLF in the zip you uploaded (confirmed with `file <name>`).
- No content/logic change — only line endings.

### 2.5 `.gitattributes` — **new file**, 2026-09-18, 09:33 IST

- Adds `*.sh text eol=lf`, so Git normalizes shell scripts to LF on
  commit/checkout regardless of the committer's editor/OS settings —
  prevents the CRLF issue in §2.4 from recurring.

### 2.6 `.gitignore` — **new file**, 2026-09-18, 09:34 IST

- Did not exist in the project before this. Added to exclude:
  `.env`, `backend/data.db`, `backend/logs/`, `backend/enrollment_photos/`,
  `backend/datasets/`, `backend/uploaded_videos/`, `__pycache__/`,
  `.venv/`, editor/OS cruft.
- **Why it matters for you specifically:** without this, if/when you
  create a real `.env` (which can contain `DEFAULT_ADMIN_PASSWORD`) or
  the new `backend/logs/backend.log`, either could get committed and
  pushed to git by accident.

---

## 3. Frontend changes — `frontend/index.html`, 2026-09-18, 09:40 IST

Browsers do not allow JavaScript to write an arbitrary file to disk on
its own, so a true "frontend log file" isn't directly possible from a
static page. What was added instead:

1. **In-memory + `localStorage`-persisted debug log buffer**
   (`appLog`, capped at 500 entries), fed by wrapping
   `console.log` / `console.info` / `console.warn` / `console.error`.
   Persisting to `localStorage` means the log survives a page reload.
2. **`downloadAppLog()`** — builds a `Blob` from the buffered entries
   plus a header (page origin, resolved backend API URL, generation
   time) and triggers a browser download as
   `uct-frontend-log-<timestamp>.txt`. Wired to two new links:
   - "Download debug log" under the **login form** (visible even before
     signing in, which is exactly when a CORS failure blocks everything).
   - "Download debug log" in the **header**, next to the existing
     `backendStatus` indicator, for use once signed in.
3. **`getBackendBaseUrl()` resolution is now logged immediately on
   page load**, and shown as text under the login form ("Backend
   target: …") — so you can see at a glance exactly which backend URL
   the frontend computed, and compare it directly against what you set
   in the backend's `FRONTEND_ORIGIN`.
4. **`api()` now logs every request/response**, and on a network-level
   failure logs a specific explanation: `fetch()` throws the same
   generic `TypeError` for both a CORS rejection and a real network
   failure (the browser deliberately hides which one it was from JS),
   so the log now explicitly says to check the browser's Network/Console
   tab for a "blocked by CORS policy" message, and states what
   `FRONTEND_ORIGIN` needs to include.
5. **`checkBackend()`'s error was previously silently discarded**
   (`catch { ... }` with no parameter) — it now logs the actual error
   message via `console.error`, so it shows up in the downloadable log
   too.

---

## 4. Verification performed — 2026-09-18, 09:20–09:49 IST

All done by actually running the code, not by inspection alone:

| Check | Result |
|---|---|
| `python3 -c "from app.main import app, _frontend_origins"` with a comma-separated `FRONTEND_ORIGIN` | Loaded correctly; origins parsed into a list |
| Backend started via the **actual** `./run_backend.sh` (with `--reload`, exactly as you will run it) after the CRLF fix | Started cleanly, no "bad interpreter" error |
| `curl` to `/` with **no** Origin header | `200 {"status":"ok","service":"workstation-monitoring"}` |
| `curl` OPTIONS preflight from the **allowed** origin (`http://localhost:8002`) | `200`, `cors_header_present=yes` in the log |
| `curl` OPTIONS preflight from a **mismatched**, Lightning-style origin | `400`, `cors_header_present=NO (check FRONTEND_ORIGIN)` in the log |
| `curl` simple GET from the mismatched origin | Backend still returns `200` server-side (expected — CORS blocking happens in the browser, not the server), but log clearly flags the missing CORS header |
| `backend/logs/backend.log` contents after the above | Confirmed all of the above lines present and correctly formatted |
| New frontend JS | Extracted and checked with `node --check` (syntax OK); logging-buffer logic unit-tested in Node with stubbed `localStorage`/`window` (buffered entries persisted correctly) |

**Not yet verified:** your two pytest suites
(`tests/design-acceptance-suite`, `tests/real-app-smoke`) were not run in
this session — the sandbox used for verification didn't have enough disk
space for `torch`/`ultralytics`, so only the lightweight dependencies
(fastapi, uvicorn, sqlalchemy, opencv-python-headless) were installed.
Run `./setup.sh` locally/on Lightning to get full coverage including
those suites.

---

## 5. What you need to do — local vs Lightning.ai

**Local:** nothing changes. Defaults already match — frontend
auto-detects `http://localhost:8001`, backend defaults to allowing
`http://localhost:8002`.

**Lightning.ai:** create `.env` in the repo root (copy from
`.env.example`) and set:
```
FRONTEND_ORIGIN=http://localhost:8002,https://8002-<your-studio-id>.lightning.ai
```
(`<your-studio-id>` comes from the URL bar once `run_frontend.sh` is
running there). **No frontend code change is needed** —
`getBackendBaseUrl()` in `frontend/index.html` already derives the
backend's subdomain from the browser's own URL automatically.

So: only a backend-side `.env` change, and only when moving to
Lightning.

---

## 6. Files delivered this session

```
UCT-WS-Monotering/
├── .env.example                    (modified)
├── .gitattributes                  (new)
├── .gitignore                      (new)
├── run_backend.sh                  (line-ending fix)
├── setup.sh                        (line-ending fix)
├── backend/
│   └── app/
│       ├── main.py                 (modified)
│       └── logging_config.py       (new)
└── frontend/
    └── index.html                  (modified)
```

`LICENSE` was also line-ending-fixed but is not included in the
downloads (no content change, low risk if you'd rather just fix it
yourself with `dos2unix LICENSE` or re-save it in LF mode).

## 7. Suggested workflow from here

1. Download the files above, replace them in your local checkout.
2. `git add -A && git status` — review the diff once before committing.
3. `git commit -m "Fix CORS origin handling; add backend/frontend debug logging"`
4. `git push`
5. On Lightning: `git pull`, create/update `.env` as in §5, run
   `./setup.sh` (or just restart the backend if already set up) and
   `./run_backend.sh` / `./run_frontend.sh`.
6. If it still fails: open `backend/logs/backend.log` and check the
   `cors_header_present` field on the failing request, and/or download
   the frontend debug log from the login screen — between the two you
   should be able to see exactly which origin was sent and whether the
   backend allowed it.
