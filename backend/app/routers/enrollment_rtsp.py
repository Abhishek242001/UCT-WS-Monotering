"""
RTSP-based employee enrollment: point the server at a live camera (or a
video file, for demos/tests), preview it live in the browser, and capture
face crops at several views (front/left/right/top) and distances directly
from the stream instead of uploading photos one at a time.

Deliberately reuses the exact multi-distance dataset structure and
calibration pipeline already built for the zip-upload enrollment flow
(app/routers/dataset_calibration.py) rather than inventing a parallel one:
each capture is written into a depth_XXXm/<view>.jpg tree on disk, that
tree is re-zipped after every capture into the very same
EMP-ZIP-<employee_id>.zip path /employees/enroll_from_zip produces, and
registered in the same _employee_zip_uploads registry -- so
/calibration/run_all picks up RTSP-captured employees with no changes of
its own. Only the reference (closest declared) distance's embedding is
written to EmployeeFaceGallery, matching enroll_from_zip's own behavior.

Live preview and the actual capture are deliberately split: the preview
WebSocket only ever relays raw frames (cheap, so it can run continuously
while the admin positions the employee), and YOLO + face-embedding
inference only runs once, at the moment "Capture" is clicked -- the same
division of cost already used elsewhere (Live Stream's own preview vs.
its identification heartbeat).
"""
import asyncio
import base64
import os
import re
import shutil
import tempfile
import time
import zipfile

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Form, File, UploadFile, WebSocket, WebSocketDisconnect, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import database as db_module
from app.database import get_db
from app.models import Employee, EmployeeFaceGallery, AdminSession
from app.routers.admin_auth import require_admin, verify_org_access, get_valid_session
from app.routers.dataset_calibration import _data_dir, _parse_distances, _employee_zip_uploads, VALID_VIEWS
from app.vision import capture, enrollment_session, face_crop, face_embedder, yolo_detector

router = APIRouter(tags=["employee-enrollment-rtsp"])

DEPTH_TOLERANCE = 0.01
PREVIEW_MIN_INTERVAL_SECONDS = 0.2  # ~5fps -- enough to position a person, cheap enough not to saturate the connection
_DEPTH_DIR_RE = re.compile(r"^depth_(\d+(?:\.\d+)?)m$")


def _depth_folder_name(depth_m: float) -> str:
    return f"depth_{depth_m:g}m"


def _staging_dir(employee_id: str) -> str:
    return os.path.join(_data_dir(), "rtsp_captures", employee_id)


def _rezip_employee(employee_id: str) -> str:
    """Rebuilds EMP-ZIP-<employee_id>.zip from the employee's staging
    directory, flat (depth_XXXm folders at the zip root, with no wrapping
    employee-name folder) -- the layout dataset_calibration._resolve_dataset_root
    treats as the person folder itself, matching what a single zip upload
    of everything captured so far would have produced."""
    staging = _staging_dir(employee_id)
    zip_path = os.path.join(_data_dir(), f"EMP-ZIP-{employee_id}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for depth_dir in sorted(os.listdir(staging)):
            depth_path = os.path.join(staging, depth_dir)
            if not os.path.isdir(depth_path):
                continue
            for fname in sorted(os.listdir(depth_path)):
                zf.write(os.path.join(depth_path, fname), arcname=f"{depth_dir}/{fname}")
    return zip_path


def _org_locked_distances(org_id: int) -> list[float] | None:
    """The distance set already fixed for this org, if any employee has
    been enrolled via zip OR via RTSP capture so far -- both write into the
    same _employee_zip_uploads registry, so this mirrors
    dataset_calibration.get_declared_distances exactly, just checked here
    server-side before a capture is accepted."""
    for info in _employee_zip_uploads.values():
        if info["org_id"] == org_id:
            return info["declared_distances"]
    return None


@router.post("/enrollment/rtsp/start", status_code=201)
async def start_rtsp_session(org_id: int = Form(...), source: str = Form(...), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    if not source.strip():
        raise HTTPException(422, "source must not be empty")
    try:
        session = await run_in_threadpool(enrollment_session.start_session, source, org_id)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"session_id": session.session_id, "status": "connected", "source": capture.mask_credentials(source)}


@router.post("/enrollment/rtsp/stop")
async def stop_rtsp_session(org_id: int = Form(...), session_id: str = Form(...), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    session = enrollment_session.get_session(session_id)
    if not session or session.org_id != org_id:
        raise HTTPException(404, "Session not found")
    await run_in_threadpool(enrollment_session.stop_session, session_id)
    return {"status": "stopped", "session_id": session_id}


@router.post("/enrollment/rtsp/grab_frame")
async def grab_rtsp_frame(org_id: int = Form(...), session_id: str = Form(...), admin: AdminSession = Depends(require_admin)):
    """Freezes the current live frame for the manual-crop UI: returns the
    full frame (higher JPEG quality than the cheap live-preview stream,
    since this is what the admin will actually crop from) plus a
    best-effort suggested crop box from YOLO, if a person was found --
    still just a starting point the admin can drag/resize, never applied
    automatically. Used when automatic person/face detection at capture
    time isn't good enough (too far, bad angle) but a human can still see
    the face clearly and wants to crop it by hand."""
    verify_org_access(admin, org_id)
    session = enrollment_session.get_session(session_id)
    if not session or session.org_id != org_id:
        raise HTTPException(404, "RTSP session not found -- connect first")

    frame = await run_in_threadpool(session.get_frame)
    if frame is None:
        raise HTTPException(422, "No frame available yet from this source -- wait for the preview to show a frame and try again")

    h, w = frame.shape[:2]
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        cv2.imwrite(tmp.name, frame)
        detect_tmp_path = tmp.name
    try:
        people = await run_in_threadpool(yolo_detector.detect_people, detect_tmp_path)
    finally:
        os.unlink(detect_tmp_path)

    detected_box = None
    if people:
        person = max(people, key=lambda p: (p.x2 - p.x1) * (p.y2 - p.y1))
        pad = face_crop.DEFAULT_PADDING_FRACTION
        box_w, box_h = person.x2 - person.x1, person.y2 - person.y1
        detected_box = {
            "x1": max(0.0, person.x1 - box_w * pad), "y1": max(0.0, person.y1 - box_h * pad),
            "x2": min(1.0, person.x2 + box_w * pad), "y2": min(1.0, person.y2 + box_h * pad),
        }

    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise HTTPException(500, "Could not encode the current frame")
    encoded = base64.b64encode(buf).decode("ascii")
    return {"image": f"data:image/jpeg;base64,{encoded}", "width": w, "height": h, "detected_box": detected_box}


@router.get("/enrollment/rtsp/captures")
def list_rtsp_captures(org_id: int, employee_id: str, admin: AdminSession = Depends(require_admin)):
    """The distance-wise capture gallery shown next to the live preview --
    re-reads whatever is already on disk for this employee (see
    _staging_dir) so a reconnect or page reload doesn't lose the thumbnails,
    without needing any new persistent state beyond the files already
    written by a capture."""
    verify_org_access(admin, org_id)
    info = _employee_zip_uploads.get(employee_id)
    if not info or info["org_id"] != org_id:
        return {"captures": {}}

    staging = _staging_dir(employee_id)
    if not os.path.isdir(staging):
        return {"captures": {}}

    captures: dict[str, dict[str, str]] = {}
    for depth_dir in sorted(os.listdir(staging)):
        depth_path = os.path.join(staging, depth_dir)
        m = _DEPTH_DIR_RE.match(depth_dir)
        if not m or not os.path.isdir(depth_path):
            continue
        views = {}
        for fname in sorted(os.listdir(depth_path)):
            view = os.path.splitext(fname)[0]
            if view not in VALID_VIEWS:
                continue
            with open(os.path.join(depth_path, fname), "rb") as f:
                views[view] = f"data:image/jpeg;base64,{base64.b64encode(f.read()).decode('ascii')}"
        captures[m.group(1)] = views
    return {"captures": captures}


class RtspCaptureDeleteBody(BaseModel):
    org_id: int
    employee_id: str
    view: str
    depth_m: float


@router.delete("/enrollment/rtsp/captures")
def delete_rtsp_capture(req: RtspCaptureDeleteBody, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Removes one captured view/distance from disk (and from the
    EmployeeFaceGallery row it produced, if it was the reference-distance
    capture for that view) -- lets the admin discard a bad capture from
    the gallery shown next to the live preview instead of it being
    permanent the moment "Capture" succeeds."""
    verify_org_access(admin, req.org_id)
    if req.view not in VALID_VIEWS:
        raise HTTPException(422, f"view must be one of {VALID_VIEWS}")

    info = _employee_zip_uploads.get(req.employee_id)
    if not info or info["org_id"] != req.org_id:
        raise HTTPException(404, "No RTSP captures found for this employee")

    depth_dir = os.path.join(_staging_dir(req.employee_id), _depth_folder_name(req.depth_m))
    file_path = os.path.join(depth_dir, f"{req.view}.jpg")
    if not os.path.isfile(file_path):
        raise HTTPException(404, "That capture does not exist")
    os.remove(file_path)
    if not os.listdir(depth_dir):
        os.rmdir(depth_dir)

    declared = info.get("declared_distances") or []
    reference_depth = min(declared) if declared else None
    gallery_row_removed = False
    if reference_depth is not None and abs(req.depth_m - reference_depth) <= DEPTH_TOLERANCE:
        row = db.query(EmployeeFaceGallery).filter_by(employee_id=req.employee_id, view=req.view).first()
        if row:
            db.delete(row)
            db.commit()
            gallery_row_removed = True

    staging = _staging_dir(req.employee_id)
    remaining = [d for d in os.listdir(staging) if os.path.isdir(os.path.join(staging, d))] if os.path.isdir(staging) else []
    if not remaining:
        # Nothing left captured for this employee at all -- drop the zip
        # and its registry entry rather than leaving an empty shell around
        # that /calibration/run_all would otherwise still iterate over.
        if os.path.isfile(info["zip_path"]):
            os.remove(info["zip_path"])
        shutil.rmtree(staging, ignore_errors=True)
        _employee_zip_uploads.pop(req.employee_id, None)
    else:
        info["zip_path"] = _rezip_employee(req.employee_id)

    return {
        "status": "deleted", "employee_id": req.employee_id, "view": req.view,
        "depth_m": req.depth_m, "gallery_row_removed": gallery_row_removed,
    }


@router.post("/enrollment/rtsp/capture", status_code=201)
async def capture_rtsp_enrollment(
    org_id: int = Form(...), session_id: str = Form(...), employee_id: str = Form(...), name: str = Form(...),
    view: str = Form(...), depth_m: float = Form(...), distances: str = Form(...),
    department: str | None = Form(None), force_no_face: bool = Form(False),
    crop_image: UploadFile | None = File(None),
    db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin),
):
    """Two ways to arrive at the crop that gets saved:

    - crop_image omitted (the default "Capture" button): automatic, as
      before -- grabs the current live frame and crops to the largest
      YOLO-detected person. Fails with 422 if no person is found at all.
    - crop_image provided (the "Adjust crop manually" flow): the admin has
      already frozen a frame via /enrollment/rtsp/grab_frame, drawn/resized
      a crop box over it in the browser, and cropped it client-side --
      this bypasses YOLO entirely, since a human has already located the
      face. This is the escape hatch for a frame automatic detection
      can't handle (too far, bad angle, side lighting) but a person can
      still clearly see.

    Either way, face-embedding extraction still runs on the resulting
    crop. If it fails (no face detected by the recognition backend) and
    force_no_face is NOT set, this still fails with 422, same as the
    original behavior -- a caller has to explicitly opt into saving a
    faceless crop (force_no_face=true), so accidentally-bad captures don't
    silently end up in the dataset. When force_no_face IS set and
    detection fails, the crop is still saved (to disk / the calibration
    zip) so the capture isn't lost, but EmployeeFaceGallery is never
    updated from it -- there is no embedding to store."""
    verify_org_access(admin, org_id)
    if view not in VALID_VIEWS:
        raise HTTPException(422, f"view must be one of {VALID_VIEWS}")

    session = enrollment_session.get_session(session_id)
    if not session or session.org_id != org_id:
        raise HTTPException(404, "RTSP session not found -- connect first")

    try:
        declared = _parse_distances(distances)
    except ValueError as e:
        raise HTTPException(422, str(e))

    locked = _org_locked_distances(org_id)
    if locked is not None and sorted(locked) != sorted(declared):
        raise HTTPException(
            422,
            f"Distances locked for this organization at {locked} (set by the first employee enrolled "
            f"this way, whether by zip or RTSP capture) -- every employee must use the same set.",
        )
    if not any(abs(depth_m - d) <= DEPTH_TOLERANCE for d in declared):
        raise HTTPException(422, f"depth_m {depth_m} is not one of the declared distances {declared}")

    if crop_image is not None:
        raw = await crop_image.read()
        decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            raise HTTPException(422, "Could not decode the uploaded crop image")
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            cv2.imwrite(tmp.name, decoded)
            crop_path = tmp.name
    else:
        frame = await run_in_threadpool(session.get_frame)
        if frame is None:
            raise HTTPException(422, "No frame available yet from this source -- wait for the preview to show a frame and try again")

        # YOLO detection goes through a temp file, same convention as
        # every other real per-frame caller in this codebase
        # (stream_worker.py, video_export.py, workstations.py) rather than
        # relying on undocumented ndarray support in the detector
        # wrapper's typed interface.
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            cv2.imwrite(tmp.name, frame)
            detect_tmp_path = tmp.name
        try:
            people = await run_in_threadpool(yolo_detector.detect_people, detect_tmp_path)
        finally:
            os.unlink(detect_tmp_path)

        if not people:
            raise HTTPException(
                422,
                "No person detected in the current frame -- stand where the preview shows you, "
                "or use 'Adjust crop manually' to crop the face by hand",
            )
        person = max(people, key=lambda p: (p.x2 - p.x1) * (p.y2 - p.y1))
        crop_path = face_crop.crop_person_region(frame, person)

    face_detected = True
    embedding, width_px = None, None
    try:
        embedding, width_px = await run_in_threadpool(face_embedder.extract_embedding, crop_path)
    except ValueError as e:
        if not force_no_face:
            os.unlink(crop_path)
            raise HTTPException(422, str(e))
        face_detected = False

    employee = db.get(Employee, employee_id)
    if not employee:
        employee = Employee(employee_id=employee_id, org_id=org_id, name=name, department=department, active=True)
        db.add(employee)
        db.flush()
    elif employee.org_id != org_id:
        # Same cross-tenant guard as /employees/enroll and
        # /employees/enroll_from_zip -- employee_id is a globally unique
        # primary key, so without this check a caller could attach RTSP
        # captures to another organization's existing employee record.
        os.unlink(crop_path)
        raise HTTPException(409, f"employee_id '{employee_id}' already exists under a different org_id")
    else:
        employee.name = name
        if department:
            employee.department = department

    depth_dir = os.path.join(_staging_dir(employee_id), _depth_folder_name(depth_m))
    os.makedirs(depth_dir, exist_ok=True)
    dest_path = os.path.join(depth_dir, f"{view}.jpg")
    shutil.move(crop_path, dest_path)

    zip_path = await run_in_threadpool(_rezip_employee, employee_id)
    _employee_zip_uploads[employee_id] = {
        "org_id": org_id, "zip_path": zip_path, "declared_distances": declared, "unexpected_folders": [],
    }

    reference_depth = min(declared)
    gallery_updated = False
    if face_detected and abs(depth_m - reference_depth) <= DEPTH_TOLERANCE:
        existing = db.query(EmployeeFaceGallery).filter_by(employee_id=employee_id, view=view).first()
        if existing:
            existing.set_embedding(embedding)
            existing.reference_width_px = width_px
            existing.reference_depth_m = reference_depth
        else:
            row = EmployeeFaceGallery(employee_id=employee_id, view=view, reference_depth_m=reference_depth, reference_width_px=width_px)
            row.set_embedding(embedding)
            db.add(row)
        gallery_updated = True

    db.commit()

    with open(dest_path, "rb") as f:
        captured_b64 = base64.b64encode(f.read()).decode("ascii")

    views_captured = sorted({r.view for r in db.query(EmployeeFaceGallery).filter_by(employee_id=employee_id).all()})
    return {
        "status": "captured", "employee_id": employee_id, "view": view, "depth_m": depth_m,
        "face_detected": face_detected, "gallery_updated": gallery_updated, "reference_distance_m": reference_depth,
        "distances": declared, "views_captured": views_captured, "calibration_ready": len(declared) >= 2,
        "face_backend": face_embedder.backend_name(),
        "captured_image": f"data:image/jpeg;base64,{captured_b64}",
    }


@router.websocket("/ws/enrollment/rtsp/{session_id}")
async def enrollment_rtsp_preview(websocket: WebSocket, session_id: str, token: str = Query(...)):
    """Server->client-only push of raw frames (no detection -- see module
    docstring), on its own router for the same reason as
    routers/streams_ws.py: browsers cannot set the Authorization header on
    a WebSocket handshake, so the session token travels as a query param
    instead and is validated with the same get_valid_session() logic."""
    db = db_module.SessionLocal()
    try:
        admin_session = get_valid_session(token, db)
    finally:
        db.close()
    if not admin_session:
        await websocket.close(code=4401)
        return

    session = enrollment_session.get_session(session_id)
    if not session:
        await websocket.close(code=4404)
        return
    if admin_session.role != "SUPER_ADMIN" and admin_session.org_id != session.org_id:
        await websocket.close(code=4403)
        return

    await websocket.accept()
    relay_task = asyncio.ensure_future(_relay_preview_frames(websocket, session))
    disconnect_task = asyncio.ensure_future(_watch_for_disconnect(websocket))
    try:
        done, pending = await asyncio.wait({relay_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass


async def _relay_preview_frames(websocket: WebSocket, session: "enrollment_session.EnrollmentSession") -> None:
    while True:
        started = time.monotonic()
        frame = await run_in_threadpool(session.get_frame)
        if frame is None:
            if enrollment_session.get_session(session.session_id) is None:
                await websocket.send_json({"type": "stopped"})
                return
            await asyncio.sleep(PREVIEW_MIN_INTERVAL_SECONDS)
            continue
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            encoded = base64.b64encode(buf).decode("ascii")
            await websocket.send_json({"type": "frame", "image": f"data:image/jpeg;base64,{encoded}"})
        elapsed = time.monotonic() - started
        if elapsed < PREVIEW_MIN_INTERVAL_SECONDS:
            await asyncio.sleep(PREVIEW_MIN_INTERVAL_SECONDS - elapsed)


async def _watch_for_disconnect(websocket: WebSocket) -> None:
    while True:
        await websocket.receive_text()
