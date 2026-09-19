// js/tabs/employees.js — Employees tab: single enrollment, listing,
// readiness check, bulk zip enrollment, and calibration training.

async function enrollEmployee() {
  const fd = new FormData();
  fd.append('org_id', getOrgId());
  fd.append('employee_id', document.getElementById('enrollId').value);
  fd.append('name', document.getElementById('enrollName').value);
  fd.append('view', document.getElementById('enrollView').value);
  fd.append('depth_m', 1.0);
  const photo = document.getElementById('enrollPhoto').files[0];
  if (!photo) { showMsg('enrollMsg', 'Choose a photo first.', false); return; }
  fd.append('photo', photo);
  try {
    const body = await api('/employees/enroll', { method:'POST', body: fd });
    showMsg('enrollMsg', `Enrolled ${body.employee_id}: views so far [${body.views_captured.join(', ')}] — face backend: ${body.face_backend}`);
    loadEmployees();
  } catch (e) { showMsg('enrollMsg', e.message, false); }
}

async function loadEmployees() {
  try {
    const body = await api('/employees/list?org_id=' + getOrgId());
    document.getElementById('employeesTable').innerHTML = body.employees.map(e =>
      `<tr><td>${escapeHtml(e.employee_id)}</td><td>${escapeHtml(e.name)}</td><td>${escapeHtml(e.department)||'—'}</td><td>${e.active?'yes':'no'}</td></tr>`
    ).join('') || '<tr><td colspan="4">None yet.</td></tr>';
  } catch (e) { console.error(e); }
}

async function runReadinessCheck() {
  const msgEl = document.getElementById('readinessMsg');
  const resultsEl = document.getElementById('readinessResults');
  msgEl.textContent = 'Checking…';
  resultsEl.style.display = 'none';
  let body;
  try {
    body = await api(`/reports/face-recognition-readiness?org_id=${getOrgId()}`);
  } catch (e) { msgEl.textContent = 'Failed: ' + e.message; return; }

  msgEl.textContent = `${body.employees_complete_count} / ${body.employees_total_count} employees fully enrolled · ${body.workstations_unassigned_count} workstation(s) unassigned`;

  document.getElementById('readinessEmployeesTable').innerHTML = body.employees.map(e => `<tr>
    <td>${escapeHtml(e.employee_id)} (${escapeHtml(e.name)})</td>
    <td>${e.views_enrolled.map(escapeHtml).join(', ') || '—'}</td>
    <td>${e.views_missing.map(escapeHtml).join(', ') || '—'}</td>
    <td>${e.views_with_null_embedding.map(escapeHtml).join(', ') || '—'}</td>
    <td><span class="badge ${e.complete ? 'MATCH' : 'MISMATCH'}">${e.complete ? 'ready' : 'incomplete'}</span></td>
  </tr>`).join('') || '<tr><td colspan="5">No active employees.</td></tr>';

  document.getElementById('readinessWorkstationsTable').innerHTML = body.workstations.map(w => `<tr>
    <td>${w.cam_id}</td><td>${escapeHtml(w.name)}</td>
    <td>${w.has_active_assignment ? escapeHtml(w.assigned_employee_id) : '<span class="badge MISMATCH">unassigned</span>'}</td>
  </tr>`).join('') || '<tr><td colspan="3">No workstations saved.</td></tr>';

  resultsEl.style.display = 'block';
}

// --- bulk zip enrollment ---
// The distance set (e.g. "0.5,1,2,3,4,5") is a per-org invariant: the
// FIRST zip enrolled for an org fixes it (via enroll_from_zip's own
// validation), and /employees/enrolled_distances/{org_id} reports back
// whatever was fixed so every later admin/employee reuses the same set
// instead of each zip declaring its own. We mirror that here by locking
// the input once the backend reports a non-null set, rather than letting
// the UI suggest distances can still be freely chosen after the first one.
let lockedDistances = null;

async function loadDeclaredDistances() {
  try {
    const body = await api('/employees/enrolled_distances/' + getOrgId());
    lockedDistances = body.distances;
  } catch (e) {
    console.error('loadDeclaredDistances failed:', e.message);
    lockedDistances = null;
  }
  renderDistanceField();
}

function renderDistanceField() {
  const wrap = document.getElementById('zipDistancesWrap');
  if (lockedDistances && lockedDistances.length) {
    wrap.innerHTML = `<div class="hint">Distances locked for this organization (set by the first employee enrolled this way): <b>${lockedDistances.map(d => d + 'm').join(', ')}</b>. Every zip's depth_XXXm folders are checked against this same list.</div>`;
  } else {
    wrap.innerHTML = `<label>Distances used in this dataset (comma-separated, meters)</label>
      <input id="zipDistancesInput" placeholder="e.g. 0.5,1,2,3,4,5">
      <p class="hint">This is the FIRST employee for this organization, so whatever you enter here fixes the distance set for every employee enrolled afterward.</p>`;
  }
}

function currentDistancesValue() {
  if (lockedDistances && lockedDistances.length) return lockedDistances.join(',');
  const el = document.getElementById('zipDistancesInput');
  return el ? el.value.trim() : '';
}

async function enrollEmployeeFromZip() {
  const employee_id = document.getElementById('zipEmpId').value.trim();
  const name = document.getElementById('zipEmpName').value.trim();
  if (!employee_id || !name) { showMsg('zipEnrollMsg', 'Employee ID and Name are required.', false); return; }
  const distances = currentDistancesValue();
  if (!distances) { showMsg('zipEnrollMsg', 'Enter the distances used in this dataset first.', false); return; }
  const zipFile = document.getElementById('zipEmpFile').files[0];
  if (!zipFile) { showMsg('zipEnrollMsg', 'Choose this employee\'s zip file first.', false); return; }

  const fd = new FormData();
  fd.append('org_id', getOrgId());
  fd.append('employee_id', employee_id);
  fd.append('name', name);
  const department = document.getElementById('zipEmpDept').value.trim();
  if (department) fd.append('department', department);
  fd.append('distances', distances);
  fd.append('file', zipFile);

  try {
    const body = await api('/employees/enroll_from_zip', { method: 'POST', body: fd });
    const parts = [`Enrolled ${body.employee_id} from reference distance ${body.reference_distance_m}m — views: [${body.views_enrolled.join(', ')}]`];
    if (body.views_skipped.length) parts.push(`skipped: ${body.views_skipped.map(v => `${v.view} (${v.issue})`).join('; ')}`);
    if (body.distance_folders_unexpected.length) parts.push(`ignored folders not matching declared distances: ${body.distance_folders_unexpected.join(', ')}`);
    parts.push(body.calibration_ready
      ? 'has 2+ distances — will contribute to "Train model".'
      : 'only 1 usable distance — add more depth_XXXm folders for this employee to contribute to calibration.');
    showMsg('zipEnrollMsg', parts.join(' | '), true);
    loadDeclaredDistances();
    loadEmployees();
  } catch (e) { showMsg('zipEnrollMsg', e.message, false); }
}

async function runCalibrationAll() {
  const fd = new FormData();
  fd.append('org_id', getOrgId());
  try {
    const { job_id } = await api('/calibration/run_all', { method: 'POST', body: fd });
    const status = await api('/calibration/status/' + job_id);
    const r = status.result;
    const parts = [`Model updated — near_k=${r.near_k}, near_sigma0=${r.near_sigma0}, people_enrolled=${r.people_enrolled}, data_points=${r.calibration_data_points}, face_backend=${r.face_backend}`];
    if (r.people_with_single_distance_only.length) parts.push(`needs more distances to contribute: ${r.people_with_single_distance_only.join(', ')}`);
    showMsg('trainMsg', parts.join(' | '), true);
  } catch (e) { showMsg('trainMsg', e.message, false); }
}
