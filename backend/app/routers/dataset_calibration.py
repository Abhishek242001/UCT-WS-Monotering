import os
import re
import shutil
import tempfile
import uuid
import zipfile

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from scipy.optimize import curve_fit

from app.logic import alpha_decay, cosine_similarity
from app.vision import face_embedder
from app.routers.admin_auth import require_admin, verify_org_access
from app.models import AdminSession

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
