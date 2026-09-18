import os
import re
import shutil
import tempfile
import uuid
import zipfile

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from scipy.optimize import curve_fit
from sqlalchemy.orm import Session

from app.database import get_db
from app.logic import alpha_decay, cosine_similarity
from app.vision import face_embedder
from app.routers.admin_auth import require_admin, verify_org_access
from app.models import AdminSession, Employee, EmployeeFaceGallery

router = APIRouter(tags=["dataset-calibration"])

def _data_dir() -> str:
    """Read at call time, not import time -- so this respects a
    DATASET_DIR set right before the request (e.g. by the test suite
    between tests), not just whatever was set the moment this module was
    first imported. The same fix pattern as app/database.py's
    reset_engine(), applied here for the same underlying reason."""
    return os.environ.get("DATASET_DIR", "./datasets")
DEPTH_RE = re.compile(r"^depth_(\d+(?:\.\d+)?)m$")
VALID_VIEWS = {"front", "left", "right", "top"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024

_uploads: dict[str, dict] = {}
_jobs: dict[str, dict] = {}

# Per-employee zip uploads from the guided bulk-enrollment flow, keyed by
# employee_id. Separate from _uploads (which is keyed by an opaque
# upload_id and used by the original multi-person-in-one-zip flow) because
# /calibration/run_all needs to look these up BY employee, re-deriving the
# multi-distance rows for every employee enrolled so far -- not just the
# most recently uploaded zip.
_employee_zip_uploads: dict[str, dict] = {}


@router.post("/dataset/upload", status_code=201)
async def upload_dataset(org_id: int = Form(...), file: UploadFile = File(...), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(422, "Only .zip uploads are accepted")

    os.makedirs(_data_dir(), exist_ok=True)
    upload_id = f"UPL-{uuid.uuid4().hex[:12]}"
    dest = os.path.join(_data_dir(), f"{upload_id}.zip")
    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                f.close()
                os.remove(dest)
                raise HTTPException(413, "Upload exceeds maximum allowed size")
            f.write(chunk)

    _uploads[upload_id] = {"org_id": org_id, "file_name": file.filename, "size_bytes": size, "zip_path": dest}
    return {"upload_id": upload_id, "status": "received", "file_name": file.filename, "size_bytes": size}


def _validate_extracted(root: str) -> dict:
    errors, warnings = [], []
    people_found, calib_volunteers_found, total_images, faces_not_detected = 0, 0, 0, 0

    person_dirs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    for person in person_dirs:
        person_path = os.path.join(root, person)
        depth_dirs = [d for d in os.listdir(person_path) if DEPTH_RE.match(d)]
        if not depth_dirs:
            errors.append({"person": person, "issue": "no valid depth_XXXm folders found"})
            continue

        people_found += 1
        if len(depth_dirs) > 1:
            calib_volunteers_found += 1

        for depth_dir in depth_dirs:
            depth_path = os.path.join(person_path, depth_dir)
            files_present = {os.path.splitext(f)[0] for f in os.listdir(depth_path)}
            missing = VALID_VIEWS - files_present
            for view in missing:
                warnings.append({"person": person, "depth_folder": depth_dir, "issue": f"missing {view}.jpg"})
            for view in files_present & VALID_VIEWS:
                total_images += 1
                img_path = next((os.path.join(depth_path, f) for f in os.listdir(depth_path)
                                  if os.path.splitext(f)[0] == view), None)
                if img_path and face_embedder.backend_name() == "insightface":
                    try:
                        face_embedder.extract_embedding(img_path)
                    except ValueError:
                        faces_not_detected += 1
                        warnings.append({"person": person, "depth_folder": depth_dir, "issue": f"no face detected in {view}"})

    status = "FAILED" if errors else "PASSED"
    return {
        "status": status, "errors": errors, "warnings": warnings,
        "summary": {"people_found": people_found, "calibration_volunteers_found": calib_volunteers_found,
                    "total_images": total_images, "faces_not_detected": faces_not_detected},
    }


def _resolve_dataset_root(tmp_dir: str) -> str:
    """Datasets may be zipped either as person-folders directly at the top
    level, or wrapped in one outer 'dataset/' folder. Distinguish the two
    by checking whether the single top-level entry's own children look
    like depth_XXXm folders (meaning it IS a person folder itself) rather
    than assuming any single top-level folder is a wrapper to unwrap."""
    entries = [e for e in os.listdir(tmp_dir) if not e.startswith("__MACOSX")]
    if len(entries) != 1 or not os.path.isdir(os.path.join(tmp_dir, entries[0])):
        return tmp_dir
    candidate = os.path.join(tmp_dir, entries[0])
    children = os.listdir(candidate)
    if any(DEPTH_RE.match(c) for c in children):
        return tmp_dir  # the single entry is itself a person folder -- don't unwrap
    return candidate  # the single entry is a wrapper folder -- unwrap one level


@router.get("/dataset/validate/{upload_id}")
def validate_dataset(upload_id: str, admin: AdminSession = Depends(require_admin)):
    if upload_id not in _uploads:
        raise HTTPException(404, "Upload not found")
    # Derived from the upload's own stored org_id rather than a
    # client-supplied one: this endpoint's URL only ever carried
    # upload_id, so previously ANY admin who knew (or brute-forced) a
    # upload_id could inspect any org's dataset contents.
    verify_org_access(admin, _uploads[upload_id]["org_id"])
    zip_path = _uploads[upload_id]["zip_path"]

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_dir)
        except zipfile.BadZipFile:
            raise HTTPException(422, "Uploaded file is not a valid zip archive")

        root = _resolve_dataset_root(tmp_dir)
        result = _validate_extracted(root)

    result["upload_id"] = upload_id
    _uploads[upload_id]["validation"] = result
    note = "" if face_embedder.backend_name() == "insightface" else " (face-detection checks skipped: running in stub embedding mode -- see backend/app/vision/face_embedder.py)"
    if note:
        result["warnings"].append({"person": "*", "depth_folder": "*", "issue": "face-detection validation skipped" + note})
    return result


@router.post("/calibration/run", status_code=202)
async def run_calibration(org_id: int = Form(...), upload_id: str = Form(...), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    if upload_id not in _uploads or _uploads[upload_id]["org_id"] != org_id:
        # Previously checked only that upload_id existed SOMEWHERE, never
        # that it actually belonged to the org_id given in this same
        # request -- the same cross-reference gap found and fixed
        # elsewhere in this audit (e.g. workstations/assign).
        raise HTTPException(404, "Upload not found")
    job_id = f"CAL-JOB-{uuid.uuid4().hex[:8]}"

    zip_path = _uploads[upload_id]["zip_path"]
    with tempfile.TemporaryDirectory() as tmp_dir:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_dir)
        root = _resolve_dataset_root(tmp_dir)

        rows = []  # (relative_depth, similarity)
        people_enrolled = 0
        for person in os.listdir(root):
            person_path = os.path.join(root, person)
            if not os.path.isdir(person_path):
                continue
            depth_dirs = sorted(
                [d for d in os.listdir(person_path) if DEPTH_RE.match(d)],
                key=lambda d: float(DEPTH_RE.match(d).group(1)),
            )
            if not depth_dirs:
                continue
            people_enrolled += 1
            if len(depth_dirs) < 2:
                continue  # needs multiple distances to contribute to the curve fit

            ref_depth = float(DEPTH_RE.match(depth_dirs[0]).group(1))
            ref_embeddings = {}
            for view in VALID_VIEWS:
                img = _find_view_image(os.path.join(person_path, depth_dirs[0]), view)
                if img:
                    try:
                        emb, _w = face_embedder.extract_embedding(img)
                        ref_embeddings[view] = emb
                    except ValueError:
                        pass

            for depth_dir in depth_dirs[1:]:
                depth = float(DEPTH_RE.match(depth_dir).group(1))
                for view, ref_emb in ref_embeddings.items():
                    img = _find_view_image(os.path.join(person_path, depth_dir), view)
                    if not img:
                        continue
                    try:
                        live_emb, _w = face_embedder.extract_embedding(img)
                        sim = cosine_similarity(live_emb, ref_emb)
                        rows.append((depth - ref_depth, sim))
                    except ValueError:
                        continue

    if len(rows) >= 3:
        depths = np.array([r[0] for r in rows])
        sims = np.array([r[1] for r in rows])
        try:
            popt, _ = curve_fit(alpha_decay, depths, sims, p0=[0.2], maxfev=2000)
            fitted_k = float(popt[0])
        except Exception:
            fitted_k = 0.2  # fallback default if the fit doesn't converge on this data
        residual_std = float(np.std(sims - np.array([alpha_decay(d, fitted_k) for d in depths])))
    else:
        fitted_k, residual_std = 0.2, 0.1  # documented defaults when insufficient multi-distance data was provided

    _jobs[job_id] = {
        "status": "COMPLETED", "org_id": org_id,
        "result": {"near_k": round(fitted_k, 5), "near_sigma0": round(residual_std, 5),
                   "near_gamma": 0.5, "people_enrolled": people_enrolled,
                   "calibration_data_points": len(rows), "face_backend": face_embedder.backend_name()},
    }
    return {"job_id": job_id, "status": "queued"}


def _find_view_image(depth_path: str, view: str):
    for f in os.listdir(depth_path):
        if os.path.splitext(f)[0] == view:
            return os.path.join(depth_path, f)
    return None


@router.get("/calibration/status/{job_id}")
def calibration_status(job_id: str, admin: AdminSession = Depends(require_admin)):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    verify_org_access(admin, job["org_id"])
    return {"job_id": job_id, "status": job["status"], "progress_percent": 100, "result": job["result"]}


# ---------------------------------------------------------------------------
# Guided bulk enrollment: one zip per employee, matched against a set of
# distances the admin declares up front (see docs/enrollment-workflow.md
# for the full rationale). This is deliberately separate from the original
# /dataset/upload + /calibration/run pair above (which expects ALL people
# bundled into a single zip and only ever produces calibration numbers,
# never writes to the Employee/EmployeeFaceGallery tables). This flow does
# both: it enrolls the employee's gallery photos immediately (so
# Workstations/Try Detection can use them right away) AND keeps the zip on
# disk so /calibration/run_all can later re-derive the distance-decay curve
# across everyone enrolled this way.
# ---------------------------------------------------------------------------

def _parse_distances(raw: str) -> list[float]:
    """Parses a comma-separated distance list like "0.5,1,2,3" typed by
    the admin on the enrollment page into a sorted list of floats. Raises
    ValueError (caller converts to HTTP 422) on anything that doesn't
    parse cleanly -- this list is what every uploaded zip's depth_XXXm
    folders are checked against, so a silently-wrong parse here would
    silently accept the wrong folders."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("distances must contain at least one value")
    try:
        values = sorted({float(p) for p in parts})
    except ValueError:
        raise ValueError(f"could not parse one or more distances from: {raw!r}")
    return values


def _depth_dir_matches_declared(depth_dirs: list[str], declared: list[float], tolerance: float = 0.01) -> tuple[list[str], list[str]]:
    """Splits depth_XXXm folder names into (matching, unexpected) against
    the admin-declared distance list, so the same fixed set of distances
    is enforced across every employee's zip rather than letting each
    upload introduce its own ad hoc set."""
    matching, unexpected = [], []
    for d in depth_dirs:
        depth_val = float(DEPTH_RE.match(d).group(1))
        if any(abs(depth_val - decl) <= tolerance for decl in declared):
            matching.append(d)
        else:
            unexpected.append(d)
    return matching, unexpected


@router.post("/employees/enroll_from_zip", status_code=201)
async def enroll_employee_from_zip(
    org_id: int = Form(...), employee_id: str = Form(...), name: str = Form(...),
    distances: str = Form(...), department: str | None = Form(None), file: UploadFile = File(...),
    db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin),
):
    """Enrolls ALL views (front/left/right/top) for ONE employee from a
    single zip, using the reference (smallest declared) distance's photos
    for the actual gallery embeddings -- closest-range photos are the
    highest quality and match the existing single-photo /employees/enroll
    behavior (which also has no built-in downscaling-robustness check).
    The zip is kept on disk so /calibration/run_all can later use every
    declared distance to fit the decay curve, not just the reference one.
    """
    verify_org_access(admin, org_id)
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(422, "Only .zip uploads are accepted")

    try:
        declared_distances = _parse_distances(distances)
    except ValueError as e:
        raise HTTPException(422, str(e))

    os.makedirs(_data_dir(), exist_ok=True)
    zip_path = os.path.join(_data_dir(), f"EMP-ZIP-{employee_id}.zip")
    size = 0
    with open(zip_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                f.close()
                os.remove(zip_path)
                raise HTTPException(413, "Upload exceeds maximum allowed size")
            f.write(chunk)

    with tempfile.TemporaryDirectory() as tmp_dir:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp_dir)
        except zipfile.BadZipFile:
            os.remove(zip_path)
            raise HTTPException(422, "Uploaded file is not a valid zip archive")

        root = _resolve_dataset_root(tmp_dir)
        # A per-employee zip should contain exactly the one person folder
        # (any name) or be the person folder itself -- reuse the same
        # unwrap logic as the multi-person flow, then treat whatever
        # single top-level folder results as this employee's own folder.
        entries = [e for e in os.listdir(root) if not e.startswith("__MACOSX")]
        person_root = root
        if len(entries) == 1 and os.path.isdir(os.path.join(root, entries[0])):
            person_root = os.path.join(root, entries[0])

        depth_dirs_all = [d for d in os.listdir(person_root) if DEPTH_RE.match(d)]
        if not depth_dirs_all:
            os.remove(zip_path)
            raise HTTPException(422, "No valid depth_XXXm folders found in the uploaded zip")

        matching, unexpected = _depth_dir_matches_declared(depth_dirs_all, declared_distances)
        if not matching:
            os.remove(zip_path)
            raise HTTPException(
                422,
                f"None of the zip's distance folders match the declared distances {declared_distances}. "
                f"Found: {sorted(depth_dirs_all)}",
            )

        matching_sorted = sorted(matching, key=lambda d: float(DEPTH_RE.match(d).group(1)))
        reference_dir = matching_sorted[0]
        reference_depth = float(DEPTH_RE.match(reference_dir).group(1))
        reference_path = os.path.join(person_root, reference_dir)

        employee = db.get(Employee, employee_id)
        if not employee:
            employee = Employee(employee_id=employee_id, org_id=org_id, name=name, department=department, active=True)
            db.add(employee)
            db.flush()
        elif employee.org_id != org_id:
            os.remove(zip_path)
            raise HTTPException(409, f"employee_id '{employee_id}' already exists under a different org_id")
        else:
            employee.name = name
            employee.department = department

        enrolled_views, skipped_views = [], []
        for view in sorted(VALID_VIEWS):
            img_path = _find_view_image(reference_path, view)
            if not img_path:
                skipped_views.append({"view": view, "issue": f"missing in {reference_dir}"})
                continue
            try:
                embedding, width_px = face_embedder.extract_embedding(img_path)
            except ValueError as e:
                skipped_views.append({"view": view, "issue": str(e)})
                continue

            existing = db.query(EmployeeFaceGallery).filter_by(employee_id=employee_id, view=view).first()
            if existing:
                existing.set_embedding(embedding)
                existing.reference_width_px = width_px
                existing.reference_depth_m = reference_depth
            else:
                row = EmployeeFaceGallery(employee_id=employee_id, view=view, reference_depth_m=reference_depth,
                                           reference_width_px=width_px)
                row.set_embedding(embedding)
                db.add(row)
            enrolled_views.append(view)

        if not enrolled_views:
            db.rollback()
            os.remove(zip_path)
            raise HTTPException(422, f"No usable views found at reference distance {reference_dir}: {skipped_views}")

        db.commit()

    _employee_zip_uploads[employee_id] = {
        "org_id": org_id, "zip_path": zip_path, "declared_distances": declared_distances,
        "unexpected_folders": unexpected,
    }

    return {
        "status": "enrolled", "employee_id": employee_id, "department": department,
        "reference_distance_m": reference_depth, "views_enrolled": enrolled_views,
        "views_skipped": skipped_views, "distance_folders_found": sorted(depth_dirs_all),
        "distance_folders_unexpected": unexpected, "face_backend": face_embedder.backend_name(),
        "calibration_ready": len(matching_sorted) >= 2,
    }


@router.post("/calibration/run_all", status_code=202)
async def run_calibration_all(org_id: int = Form(...), admin: AdminSession = Depends(require_admin)):
    """Runs the distance-decay calibration across every employee enrolled
    via /employees/enroll_from_zip for this org so far (not a single
    upload_id, unlike /calibration/run above) -- this is the "train
    model" button on the guided enrollment page: add employees one at a
    time, then run this once when ready, and again later after adding
    more."""
    verify_org_access(admin, org_id)
    org_employee_zips = {
        emp_id: info for emp_id, info in _employee_zip_uploads.items() if info["org_id"] == org_id
    }
    if not org_employee_zips:
        raise HTTPException(404, "No employees have been enrolled via zip upload for this org yet")

    job_id = f"CAL-JOB-{uuid.uuid4().hex[:8]}"
    rows = []  # (relative_depth, similarity)
    people_enrolled = 0
    people_with_single_distance = []

    for emp_id, info in org_employee_zips.items():
        with tempfile.TemporaryDirectory() as tmp_dir:
            with zipfile.ZipFile(info["zip_path"]) as zf:
                zf.extractall(tmp_dir)
            root = _resolve_dataset_root(tmp_dir)
            entries = [e for e in os.listdir(root) if not e.startswith("__MACOSX")]
            person_path = root
            if len(entries) == 1 and os.path.isdir(os.path.join(root, entries[0])):
                person_path = os.path.join(root, entries[0])

            depth_dirs_all = [d for d in os.listdir(person_path) if DEPTH_RE.match(d)]
            matching, _unexpected = _depth_dir_matches_declared(depth_dirs_all, info["declared_distances"])
            depth_dirs = sorted(matching, key=lambda d: float(DEPTH_RE.match(d).group(1)))
            if not depth_dirs:
                continue
            people_enrolled += 1
            if len(depth_dirs) < 2:
                people_with_single_distance.append(emp_id)
                continue  # needs multiple distances to contribute to the curve fit

            ref_depth = float(DEPTH_RE.match(depth_dirs[0]).group(1))
            ref_embeddings = {}
            for view in VALID_VIEWS:
                img = _find_view_image(os.path.join(person_path, depth_dirs[0]), view)
                if img:
                    try:
                        emb, _w = face_embedder.extract_embedding(img)
                        ref_embeddings[view] = emb
                    except ValueError:
                        pass

            for depth_dir in depth_dirs[1:]:
                depth = float(DEPTH_RE.match(depth_dir).group(1))
                for view, ref_emb in ref_embeddings.items():
                    img = _find_view_image(os.path.join(person_path, depth_dir), view)
                    if not img:
                        continue
                    try:
                        live_emb, _w = face_embedder.extract_embedding(img)
                        sim = cosine_similarity(live_emb, ref_emb)
                        rows.append((depth - ref_depth, sim))
                    except ValueError:
                        continue

    if len(rows) >= 3:
        depths = np.array([r[0] for r in rows])
        sims = np.array([r[1] for r in rows])
        try:
            popt, _ = curve_fit(alpha_decay, depths, sims, p0=[0.2], maxfev=2000)
            fitted_k = float(popt[0])
        except Exception:
            fitted_k = 0.2
        residual_std = float(np.std(sims - np.array([alpha_decay(d, fitted_k) for d in depths])))
    else:
        fitted_k, residual_std = 0.2, 0.1

    _jobs[job_id] = {
        "status": "COMPLETED", "org_id": org_id,
        "result": {
            "near_k": round(fitted_k, 5), "near_sigma0": round(residual_std, 5), "near_gamma": 0.5,
            "people_enrolled": people_enrolled, "people_with_single_distance_only": people_with_single_distance,
            "calibration_data_points": len(rows), "face_backend": face_embedder.backend_name(),
        },
    }
    return {"job_id": job_id, "status": "queued"}


@router.get("/employees/enrolled_distances/{org_id}")
def get_declared_distances(org_id: int, admin: AdminSession = Depends(require_admin)):
    """Returns the distance set already in use for this org, if any
    employee has been enrolled via zip yet -- lets the frontend
    pre-fill/lock the distances field so every subsequent employee's zip
    is checked against the SAME distances instead of each admin typing
    (and potentially mistyping) their own set."""
    verify_org_access(admin, org_id)
    for info in _employee_zip_uploads.values():
        if info["org_id"] == org_id:
            return {"distances": info["declared_distances"]}
    return {"distances": None}
