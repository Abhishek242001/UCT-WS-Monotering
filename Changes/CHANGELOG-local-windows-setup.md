# Changelog: run the same code on Windows and on Lightning

Base commit expected: the item-12 design-pass commit (or `dfd72aa` plus
anything after it). Additive only: no Python, JS, HTML or CSS file changes,
so Lightning behaves exactly as it does today.

This zip supersedes `repo-hygiene-changes.zip`. If you never applied that
one, you do not need to. If you did, this overwrites its two files (same
paths) with a superset.

## What is in it

| File | Purpose |
|---|---|
| `.gitignore` | Keeps per-machine files (database, uploads, logs, `.venv`, `.env`, caches) out of git |
| `.gitattributes` | LF in the repo. `.sh` stays LF, the new `.bat` files stay CRLF |
| `.env.example` | Fixes two wrong defaults (below) and documents a multi-origin `FRONTEND_ORIGIN` |
| `run_backend.bat` | Windows equivalent of `run_backend.sh` (activates `.venv`, loads `.env`, starts uvicorn on 8001) |
| `run_frontend.bat` | Windows equivalent of `run_frontend.sh` (serves `frontend/` on 8002) |

## Why this must go in BEFORE you run the app locally

Running the backend creates `backend/data.db`, `backend/logs/`,
`backend/uploaded_videos/`, `backend/enrollment_photos/` and so on, inside
the repo folder. Your usual `git add -A` would commit and push them.
Lightning already has its own copies of those same paths, so its next
`git pull` would stop with "untracked working tree files would be
overwritten", or worse, replace Lightning's database. `.gitignore` prevents
both. So: apply this zip, push, pull on Lightning, and only then set up
local.

## Two `.env.example` defaults were wrong

- `YOLO_MODEL_PATH=yolov8n.pt` is the plain detector. The code's own
  default is `yolov8n-pose.pt`. If a `.env` copied from the old example is
  loaded, the pose keypoints disappear and standing/sitting/walking
  classification silently turns off.
- `IDENTIFY_HEARTBEAT_SECONDS=30` while the code default is 60.

**Check Lightning once** (setup.sh copies `.env.example` to `.env` if none
existed, so yours may hold the old value):

    cd ~/UCT-WS-Monotering
    grep -n "YOLO_MODEL_PATH\|IDENTIFY_HEARTBEAT" .env

No `.env`, or a line saying `yolov8n-pose.pt`, is fine. If it says
`yolov8n.pt`, change it to `yolov8n-pose.pt` and restart the backend.
`.env` is never committed, so this only affects that machine.

## Apply (Windows), then push

    cd /d D:\Download-D\uct-workstation
    tar -xf local-setup-changes.zip
    xcopy /S /Y /H "local-setup-changes\*" "git\UCT-WS-Monotering\"
    cd git\UCT-WS-Monotering
    git status
    git add -A
    git commit -m "Local Windows setup: .gitignore, .gitattributes, .bat launchers, fix .env.example defaults"
    git push origin main

`git status` should list only: `.gitignore`, `.gitattributes`,
`.env.example` (modified), `run_backend.bat`, `run_frontend.bat`,
`Changes/CHANGELOG-local-windows-setup.md`.
(`/H` copies hidden-attribute files; dot-files are fine either way.)

## Lightning check

    git pull
    git log --oneline -1
    git status

Expect a clean tree. Nothing needs restarting for this zip. Your data and
`.env` are untouched.

## Local Windows setup (after the push)

Use Python 3.12 (same as Lightning). Ports 8001 and 8002 must be free.

    cd /d D:\Download-D\uct-workstation\git\UCT-WS-Monotering
    python -m venv .venv
    .venv\Scripts\activate
    python -m pip install --upgrade pip
    pip install -r backend\requirements.txt
    copy .env.example .env
    run_backend.bat

Second terminal:

    cd /d D:\Download-D\uct-workstation\git\UCT-WS-Monotering
    run_frontend.bat

Open `http://localhost:8002` (use `localhost`, not `127.0.0.1`). The
frontend finds the backend at `http://localhost:8001` by itself. The
backend prints the admin credentials on first start; the default password
is `ChangeMe123!` unless you changed `.env`.

Confirm `.env` was picked up: the backend log line
`Allowed CORS origins (FRONTEND_ORIGIN): ['http://localhost:8002']` appears
at startup.

### Known snags on Windows

- **`insightface` fails to install.** It often needs Microsoft C++ Build
  Tools on Windows. Either install those and re-run the pip command, or
  skip it for now:

      findstr /v /b "insightface" backend\requirements.txt > %TEMP%\req-noface.txt
      pip install -r %TEMP%\req-noface.txt

  Without it, face matching uses the built-in stub, so identity results
  are placeholders. Detection, tracking, activity, attendance and the UI
  still work. Do accuracy testing on Lightning.
- **`lap` fails to build.** Run `pip install lapx`. It provides the same
  `lap` module.
- **CPU only.** Expect slow YOLO on a weak machine. In Video Analysis use
  a short clip and raise "Run detection every N frames".
- **`.bat` files are untested on Windows.** I cannot run `cmd.exe` here.
  If one misbehaves, use the manual commands: activate `.venv`, `cd backend`,
  `python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload`, and
  in another terminal `cd frontend` then `python -m http.server 8002`.

### Each machine has its own data

Local starts with an empty database: enroll employees and workstations
again (or copy files by hand if you choose to). Nothing syncs between
Lightning and local except code.

## Compare local with Lightning

    cd tests\real-app-smoke
    mkdir ..\..\logs
    pytest -v 2>&1 | Tee-Object ..\..\logs\local-smoke.txt

(run in PowerShell; cmd has no `tee`). The pass count should match your
Lightning run.

## Log to send Claude

`logs\local-smoke.txt`, the first 15 lines the backend prints, and
`git log --oneline -3` from both machines.
