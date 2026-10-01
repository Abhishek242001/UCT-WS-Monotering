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

// Renders the same locked/unlocked distance UI into both the zip-upload
// card and the RTSP-capture card below -- they share one org-wide
// distance set (see _org_locked_distances on the backend), so whichever
// flow enrolls first fixes it for the other too.
function renderDistanceField() {
  _renderDistanceFieldInto('zipDistancesWrap', 'zipDistancesInput');
  _renderDistanceFieldInto('rtspDistancesWrap', 'rtspDistancesInput');
  populateRtspDistanceOptions();
}

// A handful of common distance sets the admin can pick instead of typing
// -- "distance can be text in number only or can drop" -- the free-text
// input still works (sanitized to digits/commas/dots as they type, see
// _sanitizeDistancesInput below) but this covers the common cases in
// one click.
const RTSP_DISTANCE_PRESETS = ['0.5,1,2,3', '1,2,3', '0.5,1,1.5,2,2.5,3', '1,2,3,4,5'];

function _renderDistanceFieldInto(wrapId, inputId) {
  const wrap = document.getElementById(wrapId);
  if (!wrap) return;
  if (lockedDistances && lockedDistances.length) {
    wrap.innerHTML = `<div class="hint">Distances locked for this organization (set by the first employee enrolled this way): <b>${lockedDistances.map(d => d + 'm').join(', ')}</b>. Every capture is checked against this same list.</div>`;
  } else {
    const presetOptions = RTSP_DISTANCE_PRESETS.map(p => `<option value="${escapeHtml(p)}">${escapeHtml(p)} m</option>`).join('');
    wrap.innerHTML = `<label>Distances to use (numbers only, comma-separated, meters)</label>
      <div class="row">
        <div><input id="${inputId}" placeholder="e.g. 0.5,1,2,3,4,5" inputmode="decimal"
               oninput="_sanitizeDistancesInput('${inputId}')"></div>
        <div>
          <select onchange="document.getElementById('${inputId}').value=this.value; this.selectedIndex=0; populateRtspDistanceOptions();">
            <option value="">— or pick a common set —</option>
            ${presetOptions}
          </select>
        </div>
      </div>
      <p class="hint">This is the FIRST employee for this organization, so whatever you enter here fixes the distance set for every employee enrolled afterward (by zip or by RTSP capture).</p>`;
  }
}

// Keeps the distances field to digits, commas, dots and spaces as the
// admin types -- "distance can be text in number only" -- rather than
// validating only on submit.
function _sanitizeDistancesInput(inputId) {
  const el = document.getElementById(inputId);
  if (!el) return;
  const cleaned = el.value.replace(/[^0-9.,\s]/g, '');
  if (cleaned !== el.value) el.value = cleaned;
  populateRtspDistanceOptions();
}

function currentDistancesValue() {
  return _currentDistancesValueFrom('zipDistancesInput');
}

function _currentDistancesValueFrom(inputId) {
  if (lockedDistances && lockedDistances.length) return lockedDistances.join(',');
  const el = document.getElementById(inputId);
  return el ? el.value.trim() : '';
}

function populateRtspDistanceOptions() {
  const select = document.getElementById('rtspDistance');
  if (!select) return;
  const raw = _currentDistancesValueFrom('rtspDistancesInput');
  const values = raw.split(',').map(s => s.trim()).filter(Boolean);
  const previous = select.value;
  select.innerHTML = values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}m</option>`).join('')
    || '<option value="">enter distances above</option>';
  if (values.includes(previous)) select.value = previous;
  _updateRtspInstruction();
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

// --- RTSP-camera enrollment: connect, live-preview over a WebSocket
// (raw frames only -- no detection, kept cheap so it can run continuously
// while the admin positions the employee), then capture one view+distance
// at a time. Each capture runs real YOLO person detection + face-embedding
// extraction server-side and feeds the same depth_XXXm dataset structure
// (and therefore the same "Train model" pipeline) as the zip-upload flow.
let _rtspSessionId = null;
let _rtspPreviewWs = null;
let _rtspCapturedSet = new Set(); // "view@distance" strings captured so far this connection, for the checklist below

function _closeRtspPreviewSocket() {
  if (_rtspPreviewWs) {
    _rtspPreviewWs.onclose = null; // deliberate close, not a drop -- don't let onclose fire a stale message
    _rtspPreviewWs.close();
    _rtspPreviewWs = null;
  }
}

async function connectRtspEnrollment() {
  // Name the employee before opening the camera -- every capture on this
  // connection is attributed to whoever is entered here, so locking these
  // fields in for the life of the connection (see below) avoids someone
  // being captured under the wrong employee_id mid-session.
  const employee_id = document.getElementById('rtspEmpId').value.trim();
  const name = document.getElementById('rtspEmpName').value.trim();
  if (!employee_id || !name) { showMsg('rtspConnectMsg', 'Enter the Employee ID and Name first.', false); return; }
  const source = document.getElementById('rtspEnrollSource').value.trim();
  if (!source) { showMsg('rtspConnectMsg', 'Enter an RTSP URL (or server-side file path) first.', false); return; }

  const fd = new FormData();
  fd.append('org_id', getOrgId());
  fd.append('source', source);
  try {
    const body = await api('/enrollment/rtsp/start', { method: 'POST', body: fd });
    _rtspSessionId = body.session_id;
    showMsg('rtspConnectMsg', `Connected to ${body.source}.`, true);
    document.getElementById('rtspConnectBtn').style.display = 'none';
    document.getElementById('rtspDisconnectBtn').style.display = '';
    document.getElementById('rtspEmpId').disabled = true;
    document.getElementById('rtspEmpName').disabled = true;
    document.getElementById('rtspEmpDept').disabled = true;
    document.getElementById('rtspEnrollSource').disabled = true;
    document.getElementById('rtspEnrollPanel').style.display = 'block';
    cancelManualCropRtsp();
    await loadDeclaredDistances();
    await _loadRtspGalleryFromServer(employee_id); // also seeds _rtspCapturedSet from disk, so a reconnect doesn't forget what's already captured
    _advanceRtspSelection();
    _connectRtspPreview(_rtspSessionId);
  } catch (e) {
    showMsg('rtspConnectMsg', e.message, false);
    document.getElementById('rtspPreviewMsg').textContent = 'Could not connect: ' + e.message;
  }
}

function _connectRtspPreview(sessionId) {
  _closeRtspPreviewSocket();
  const img = document.getElementById('rtspPreviewImg');
  const msg = document.getElementById('rtspPreviewMsg');
  img.removeAttribute('src');
  msg.textContent = 'Connecting to live preview…';

  const ws = new WebSocket(`${getBackendWsUrl()}/ws/enrollment/rtsp/${sessionId}?token=${sessionToken}`);
  _rtspPreviewWs = ws;
  ws.onmessage = (evt) => {
    const data = JSON.parse(evt.data);
    if (data.type === 'frame') {
      img.src = data.image;
      msg.textContent = 'Live — position the employee, then capture.';
    } else if (data.type === 'stopped') {
      msg.textContent = 'Source stopped.';
    }
  };
  ws.onerror = () => { msg.textContent = 'Live preview connection error.'; };
  ws.onclose = () => { msg.textContent = 'Live preview disconnected.'; };
}

async function disconnectRtspEnrollment() {
  _closeRtspPreviewSocket();
  if (_rtspSessionId) {
    const fd = new FormData();
    fd.append('org_id', getOrgId());
    fd.append('session_id', _rtspSessionId);
    try { await api('/enrollment/rtsp/stop', { method: 'POST', body: fd }); } catch (e) { console.error(e); }
  }
  _rtspSessionId = null;
  document.getElementById('rtspConnectBtn').style.display = '';
  document.getElementById('rtspDisconnectBtn').style.display = 'none';
  document.getElementById('rtspEmpId').disabled = false;
  document.getElementById('rtspEmpName').disabled = false;
  document.getElementById('rtspEmpDept').disabled = false;
  document.getElementById('rtspEnrollSource').disabled = false;
  document.getElementById('rtspEnrollPanel').style.display = 'none';
  document.getElementById('rtspPreviewImg').removeAttribute('src');
  document.getElementById('rtspPreviewMsg').textContent = 'Not connected yet. Fill in the employee details and an RTSP URL above, then click "Connect & preview".';
  cancelManualCropRtsp();
  _rtspGalleryData = {};
  _renderRtspGallery();
  showMsg('rtspConnectMsg', 'Disconnected.', true);
}

// Every capture always goes through the crop-review step (startRtspCapture
// -> the editor's Confirm button -> _submitRtspCapture) so the admin sees
// exactly what will be saved and can adjust or discard it first -- never
// straight from "Capture" to saved. See _initRtspCropCanvas below for how
// the review box is pre-filled from automatic detection when available.
async function _submitRtspCapture(cropBlob) {
  if (!_rtspSessionId) { showMsg('rtspCaptureMsg', 'Connect to a source first.', false); return false; }
  const employee_id = document.getElementById('rtspEmpId').value.trim();
  const name = document.getElementById('rtspEmpName').value.trim();
  if (!employee_id || !name) { showMsg('rtspCaptureMsg', 'Employee ID and Name are required.', false); return false; }
  const distances = _currentDistancesValueFrom('rtspDistancesInput');
  if (!distances) { showMsg('rtspCaptureMsg', 'Enter the distances to use first.', false); return false; }
  const view = document.getElementById('rtspView').value;
  const depth_m = document.getElementById('rtspDistance').value;
  if (!depth_m) { showMsg('rtspCaptureMsg', 'Choose a distance for this capture.', false); return false; }
  const forceNoFace = document.getElementById('rtspForceNoFace').checked;

  const fd = new FormData();
  fd.append('org_id', getOrgId());
  fd.append('session_id', _rtspSessionId);
  fd.append('employee_id', employee_id);
  fd.append('name', name);
  const department = document.getElementById('rtspEmpDept').value.trim();
  if (department) fd.append('department', department);
  fd.append('view', view);
  fd.append('depth_m', depth_m);
  fd.append('distances', distances);
  fd.append('force_no_face', forceNoFace ? 'true' : 'false');
  if (cropBlob) fd.append('crop_image', cropBlob, 'crop.jpg');

  try {
    const body = await api('/enrollment/rtsp/capture', { method: 'POST', body: fd });
    _rtspCapturedSet.add(`${body.view}@${body.depth_m}`);
    const parts = [`Captured ${body.view} at ${body.depth_m}m for ${body.employee_id}`];
    if (!body.face_detected) {
      parts.push('no face was detected in this crop — saved anyway, but the face gallery was NOT updated from it.');
    } else {
      parts.push(body.gallery_updated
        ? 'reference distance — face gallery updated.'
        : `saved for calibration only (reference distance is ${body.reference_distance_m}m).`);
    }
    showMsg('rtspCaptureMsg', parts.join(' — '), true);
    _setRtspGalleryThumb(body.depth_m, body.view, body.captured_image);
    _advanceRtspSelection();
    loadDeclaredDistances();
    loadEmployees();
    return true;
  } catch (e) { showMsg('rtspCaptureMsg', e.message, false); return false; }
}

// --- distance-wise capture gallery, shown to the right of the stream ---
let _rtspGalleryData = {}; // { "1": { front: dataUrl, left: dataUrl, ... }, "2": {...} }

async function _loadRtspGalleryFromServer(employee_id) {
  _rtspGalleryData = {};
  _rtspCapturedSet = new Set();
  try {
    const body = await api(`/enrollment/rtsp/captures?org_id=${getOrgId()}&employee_id=${encodeURIComponent(employee_id)}`);
    _rtspGalleryData = body.captures || {};
    for (const depth of Object.keys(_rtspGalleryData)) {
      for (const view of Object.keys(_rtspGalleryData[depth])) {
        _rtspCapturedSet.add(`${view}@${depth}`);
      }
    }
  } catch (e) { console.error('load rtsp captures failed:', e.message); }
  _renderRtspGallery();
}

function _setRtspGalleryThumb(depth_m, view, dataUrl) {
  const key = String(depth_m);
  if (!_rtspGalleryData[key]) _rtspGalleryData[key] = {};
  _rtspGalleryData[key][view] = dataUrl;
  _renderRtspGallery();
}

function _renderRtspGallery() {
  const el = document.getElementById('rtspCapturedGallery');
  if (!el) return;
  const depths = Object.keys(_rtspGalleryData).sort((a, b) => parseFloat(a) - parseFloat(b));
  if (!depths.length) { el.textContent = 'Nothing captured yet.'; return; }
  el.innerHTML = depths.map(depth => {
    const views = _rtspGalleryData[depth];
    const thumbs = RTSP_VIEWS_ORDER.map(v => views[v]
      ? `<div style="text-align:center;position:relative;width:64px">
           <img src="${views[v]}" alt="${escapeHtml(v)}" style="width:64px;height:64px;object-fit:cover;border-radius:6px;border:1px solid var(--rule)">
           <button type="button" title="Delete this capture" onclick="deleteRtspCapture('${escapeHtml(depth)}','${escapeHtml(v)}')"
             style="position:absolute;top:-6px;right:-6px;width:20px;height:20px;border-radius:50%;border:none;background:var(--error, #A82219);color:#fff;font-size:12px;line-height:20px;padding:0;cursor:pointer">✕</button>
           <div style="font-size:11px">${escapeHtml(v)}</div>
         </div>`
      : `<div style="text-align:center;opacity:.4"><div style="width:64px;height:64px;border:1px dashed var(--rule);border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:10px">—</div><div style="font-size:11px">${escapeHtml(v)}</div></div>`
    ).join('');
    return `<div style="margin-bottom:12px"><div style="font-weight:600;margin-bottom:4px">${escapeHtml(depth)}m</div><div style="display:flex;gap:6px;flex-wrap:wrap">${thumbs}</div></div>`;
  }).join('');
}

async function deleteRtspCapture(depth, view) {
  const employee_id = document.getElementById('rtspEmpId').value.trim();
  if (!employee_id) return;
  if (!confirm(`Delete the ${view} capture at ${depth}m? This cannot be undone.`)) return;
  try {
    await api('/enrollment/rtsp/captures', {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ org_id: getOrgId(), employee_id, view, depth_m: parseFloat(depth) }),
    });
    if (_rtspGalleryData[depth]) {
      delete _rtspGalleryData[depth][view];
      if (!Object.keys(_rtspGalleryData[depth]).length) delete _rtspGalleryData[depth];
    }
    _rtspCapturedSet.delete(`${view}@${depth}`);
    _renderRtspGallery();
    _advanceRtspSelection();
    loadDeclaredDistances();
    loadEmployees();
    showMsg('rtspCaptureMsg', `Deleted ${view} at ${depth}m.`, true);
  } catch (e) { showMsg('rtspCaptureMsg', e.message, false); }
}

// --- manual crop editor: freeze a frame, let the admin drag/resize a box
// over it by hand, then submit that exact crop -- the escape hatch for a
// frame automatic person/face detection can't handle. ---
let _rtspCropImage = null;   // the frozen frame, an Image at full resolution
let _rtspCropRect = null;    // {x1,y1,x2,y2} in on-screen canvas-pixel space
let _rtspCropDragMode = null; // 'move' | 'draw' | 'resize-tl' | 'resize-tr' | 'resize-bl' | 'resize-br' | null
let _rtspCropDragStart = null;
const RTSP_CROP_HANDLE_SIZE = 10;
const RTSP_CROP_MAX_DISPLAY_WIDTH = 480;

async function startRtspCapture() {
  if (!_rtspSessionId) { showMsg('rtspCaptureMsg', 'Connect to a source first.', false); return; }
  const distances = _currentDistancesValueFrom('rtspDistancesInput');
  if (!distances) { showMsg('rtspCaptureMsg', 'Enter the distances to use first.', false); return; }
  const depth_m = document.getElementById('rtspDistance').value;
  if (!depth_m) { showMsg('rtspCaptureMsg', 'Choose a distance for this capture.', false); return; }

  const fd = new FormData();
  fd.append('org_id', getOrgId());
  fd.append('session_id', _rtspSessionId);
  try {
    const body = await api('/enrollment/rtsp/grab_frame', { method: 'POST', body: fd });
    const img = new Image();
    img.onload = () => _initRtspCropCanvas(img, body.detected_box);
    img.src = body.image;
  } catch (e) { showMsg('rtspCaptureMsg', e.message, false); }
}

function _initRtspCropCanvas(img, detectedBox) {
  _rtspCropImage = img;
  const canvas = document.getElementById('rtspCropCanvas');
  const scale = Math.min(1, RTSP_CROP_MAX_DISPLAY_WIDTH / img.naturalWidth);
  canvas.width = Math.round(img.naturalWidth * scale);
  canvas.height = Math.round(img.naturalHeight * scale);

  if (detectedBox) {
    _rtspCropRect = {
      x1: detectedBox.x1 * canvas.width, y1: detectedBox.y1 * canvas.height,
      x2: detectedBox.x2 * canvas.width, y2: detectedBox.y2 * canvas.height,
    };
  } else {
    const w = canvas.width * 0.4, h = canvas.height * 0.5;
    _rtspCropRect = {
      x1: (canvas.width - w) / 2, y1: (canvas.height - h) / 2,
      x2: (canvas.width + w) / 2, y2: (canvas.height + h) / 2,
    };
  }
  document.getElementById('rtspCropEditor').style.display = 'block';
  _bindRtspCropCanvasEvents();
  _drawRtspCropCanvas();
}

function _rtspCropHandlePoints(r) {
  return { tl: [r.x1, r.y1], tr: [r.x2, r.y1], bl: [r.x1, r.y2], br: [r.x2, r.y2] };
}

function _drawRtspCropCanvas() {
  const canvas = document.getElementById('rtspCropCanvas');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(_rtspCropImage, 0, 0, canvas.width, canvas.height);
  const r = _rtspCropRect;
  if (!r) return;
  ctx.strokeStyle = '#2A29A3';
  ctx.lineWidth = 2;
  ctx.strokeRect(r.x1, r.y1, r.x2 - r.x1, r.y2 - r.y1);
  ctx.fillStyle = '#2A29A3';
  for (const [hx, hy] of Object.values(_rtspCropHandlePoints(r))) {
    ctx.fillRect(hx - RTSP_CROP_HANDLE_SIZE / 2, hy - RTSP_CROP_HANDLE_SIZE / 2, RTSP_CROP_HANDLE_SIZE, RTSP_CROP_HANDLE_SIZE);
  }
}

function _rtspCropCanvasPos(evt) {
  const canvas = document.getElementById('rtspCropCanvas');
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width, scaleY = canvas.height / rect.height;
  return { x: (evt.clientX - rect.left) * scaleX, y: (evt.clientY - rect.top) * scaleY };
}

function _rtspCropHandleAt(pos) {
  if (!_rtspCropRect) return null;
  for (const [name, [hx, hy]] of Object.entries(_rtspCropHandlePoints(_rtspCropRect))) {
    if (Math.abs(pos.x - hx) <= RTSP_CROP_HANDLE_SIZE && Math.abs(pos.y - hy) <= RTSP_CROP_HANDLE_SIZE) return name;
  }
  return null;
}

function _rtspCropPointInRect(pos, r) {
  return pos.x >= r.x1 && pos.x <= r.x2 && pos.y >= r.y1 && pos.y <= r.y2;
}

function _bindRtspCropCanvasEvents() {
  const canvas = document.getElementById('rtspCropCanvas');
  if (!canvas || canvas.dataset.bound) return;
  canvas.dataset.bound = '1';

  canvas.addEventListener('pointerdown', (evt) => {
    const pos = _rtspCropCanvasPos(evt);
    const handle = _rtspCropHandleAt(pos);
    if (handle) {
      _rtspCropDragMode = 'resize-' + handle;
    } else if (_rtspCropRect && _rtspCropPointInRect(pos, _rtspCropRect)) {
      _rtspCropDragMode = 'move';
    } else {
      _rtspCropDragMode = 'draw';
      _rtspCropRect = { x1: pos.x, y1: pos.y, x2: pos.x, y2: pos.y };
    }
    _rtspCropDragStart = { pos, rect: { ..._rtspCropRect } };
    canvas.setPointerCapture(evt.pointerId);
  });

  canvas.addEventListener('pointermove', (evt) => {
    if (!_rtspCropDragMode) return;
    const pos = _rtspCropCanvasPos(evt);
    const start = _rtspCropDragStart;
    const dx = pos.x - start.pos.x, dy = pos.y - start.pos.y;
    const w = canvas.width, h = canvas.height;
    const clamp = (v, min, max) => Math.max(min, Math.min(max, v));

    if (_rtspCropDragMode === 'move') {
      const rw = start.rect.x2 - start.rect.x1, rh = start.rect.y2 - start.rect.y1;
      const x1 = clamp(start.rect.x1 + dx, 0, w - rw);
      const y1 = clamp(start.rect.y1 + dy, 0, h - rh);
      _rtspCropRect = { x1, y1, x2: x1 + rw, y2: y1 + rh };
    } else if (_rtspCropDragMode === 'draw') {
      _rtspCropRect = {
        x1: Math.min(start.pos.x, pos.x), y1: Math.min(start.pos.y, pos.y),
        x2: Math.max(start.pos.x, pos.x), y2: Math.max(start.pos.y, pos.y),
      };
    } else if (_rtspCropDragMode.startsWith('resize-')) {
      const corner = _rtspCropDragMode.split('-')[1];
      let { x1, y1, x2, y2 } = start.rect;
      if (corner === 'tl') { x1 = clamp(start.rect.x1 + dx, 0, x2 - 20); y1 = clamp(start.rect.y1 + dy, 0, y2 - 20); }
      if (corner === 'tr') { x2 = clamp(start.rect.x2 + dx, x1 + 20, w); y1 = clamp(start.rect.y1 + dy, 0, y2 - 20); }
      if (corner === 'bl') { x1 = clamp(start.rect.x1 + dx, 0, x2 - 20); y2 = clamp(start.rect.y2 + dy, y1 + 20, h); }
      if (corner === 'br') { x2 = clamp(start.rect.x2 + dx, x1 + 20, w); y2 = clamp(start.rect.y2 + dy, y1 + 20, h); }
      _rtspCropRect = { x1, y1, x2, y2 };
    }
    _drawRtspCropCanvas();
  });

  const endDrag = () => { _rtspCropDragMode = null; _rtspCropDragStart = null; };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);
}

async function confirmManualCropRtsp() {
  if (!_rtspCropImage || !_rtspCropRect) return;
  const canvas = document.getElementById('rtspCropCanvas');
  const scaleX = _rtspCropImage.naturalWidth / canvas.width, scaleY = _rtspCropImage.naturalHeight / canvas.height;
  const r = _rtspCropRect;
  const sx = r.x1 * scaleX, sy = r.y1 * scaleY;
  const sw = Math.max(1, (r.x2 - r.x1) * scaleX), sh = Math.max(1, (r.y2 - r.y1) * scaleY);

  const offscreen = document.createElement('canvas');
  offscreen.width = Math.round(sw);
  offscreen.height = Math.round(sh);
  offscreen.getContext('2d').drawImage(_rtspCropImage, sx, sy, sw, sh, 0, 0, offscreen.width, offscreen.height);

  const blob = await new Promise(resolve => offscreen.toBlob(resolve, 'image/jpeg', 0.92));
  if (!blob) { showMsg('rtspCaptureMsg', 'Could not prepare the cropped image.', false); return; }

  const ok = await _submitRtspCapture(blob);
  if (ok) cancelManualCropRtsp();
}

function cancelManualCropRtsp() {
  const editor = document.getElementById('rtspCropEditor');
  if (editor) editor.style.display = 'none';
  _rtspCropImage = null;
  _rtspCropRect = null;
  _rtspCropDragMode = null;
  _rtspCropDragStart = null;
}

// --- guided view/distance prompting: tells the admin exactly what pose to
// ask for next, and auto-advances to the next un-captured combination
// after each successful capture so the whole grid gets covered without
// the admin having to track it by hand.
const RTSP_VIEWS_ORDER = ['front', 'left', 'right', 'top'];
const RTSP_VIEW_DESCRIPTIONS = {
  front: 'FRONT — look straight at the camera',
  left: 'LEFT PROFILE — turn to show the left side of the face',
  right: 'RIGHT PROFILE — turn to show the right side of the face',
  top: 'CHIN DOWN — tilt the chin down slightly',
};

function _updateRtspInstruction() {
  const el = document.getElementById('rtspCaptureInstruction');
  if (!el) return;
  const depth = document.getElementById('rtspDistance').value;
  const view = document.getElementById('rtspView').value;
  if (!depth) { el.textContent = 'Enter the distances to use above first.'; return; }
  const who = document.getElementById('rtspEmpName').value.trim() || 'the employee';
  const already = _rtspCapturedSet.has(`${view}@${depth}`);
  el.innerHTML = `Ask <b>${escapeHtml(who)}</b> to stand at <b>${escapeHtml(depth)}m</b> and face: <b>${escapeHtml(RTSP_VIEW_DESCRIPTIONS[view] || view)}</b>. `
    + (already ? 'Already captured — capturing again will overwrite it.' : 'Then click Capture.');
}

function _nextUncapturedSelection() {
  const distSelect = document.getElementById('rtspDistance');
  const distances = [...distSelect.options].map(o => o.value).filter(Boolean);
  if (!distances.length) return null;
  const currentDepth = distSelect.value || distances[0];
  const depthOrder = [currentDepth, ...distances.filter(d => d !== currentDepth)];
  for (const depth of depthOrder) {
    for (const view of RTSP_VIEWS_ORDER) {
      if (!_rtspCapturedSet.has(`${view}@${depth}`)) return { view, depth };
    }
  }
  return null; // every view x distance combination has been captured
}

function _advanceRtspSelection() {
  const next = _nextUncapturedSelection();
  if (next) {
    document.getElementById('rtspView').value = next.view;
    document.getElementById('rtspDistance').value = next.depth;
  }
  _updateRtspInstruction();
  if (!next) {
    const el = document.getElementById('rtspCaptureInstruction');
    if (el) el.textContent = 'All views captured at every distance for this employee. Disconnect, or pick a combination above to recapture it.';
  }
}
