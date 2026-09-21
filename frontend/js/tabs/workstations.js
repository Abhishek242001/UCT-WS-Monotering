// js/tabs/workstations.js — Workstations tab: ROI drawing canvas,
// saving/listing workstations, assigning employees to them.

let roiImg = null, roiStart = null, roiRect = null;
let savedRoiBoxes = [];  // {name, x, y, w, h} in canvas-pixel space, accumulated for the currently loaded image

function loadRoiImage(evt) {
  const file = evt.target.files[0];
  const reader = new FileReader();
  reader.onload = e => {
    roiImg = new Image();
    roiImg.onload = () => drawRoiCanvas();
    roiImg.src = e.target.result;
    savedRoiBoxes = [];
    roiRect = null;
  };
  reader.readAsDataURL(file);
}

function drawRoiCanvas() {
  const canvas = document.getElementById('roiCanvas');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if (roiImg) ctx.drawImage(roiImg, 0, 0, canvas.width, canvas.height);
  ctx.font = '13px sans-serif';
  for (const b of savedRoiBoxes) {
    ctx.strokeStyle = '#1F8A55'; ctx.lineWidth = 2;
    ctx.strokeRect(b.x, b.y, b.w, b.h);
    ctx.fillStyle = '#1F8A55';
    ctx.fillRect(b.x, Math.max(0, b.y - 18), ctx.measureText(b.name).width + 8, 18);
    ctx.fillStyle = '#fff';
    ctx.fillText(b.name, b.x + 4, Math.max(13, b.y - 5));
  }
  if (roiRect) {
    ctx.strokeStyle = '#2A29A3'; ctx.lineWidth = 2;
    ctx.strokeRect(roiRect.x, roiRect.y, roiRect.w, roiRect.h);
  }
}

// This module is loaded (via <script src>) right before </body>, after
// the #roiCanvas element in the markup above it -- same position the
// original inline <script> occupied -- so the element already exists
// when this runs.
const canvas = document.getElementById('roiCanvas');
canvas.addEventListener('mousedown', e => {
  const r = canvas.getBoundingClientRect();
  roiStart = { x: e.clientX - r.left, y: e.clientY - r.top };
});
canvas.addEventListener('mousemove', e => {
  if (!roiStart) return;
  const r = canvas.getBoundingClientRect();
  const x = e.clientX - r.left, y = e.clientY - r.top;
  roiRect = { x: Math.min(roiStart.x,x), y: Math.min(roiStart.y,y), w: Math.abs(x-roiStart.x), h: Math.abs(y-roiStart.y) };
  drawRoiCanvas();
});
canvas.addEventListener('mouseup', () => {
  roiStart = null;
  if (roiRect) {
    const x1 = roiRect.x / canvas.width, y1 = roiRect.y / canvas.height;
    const x2 = (roiRect.x+roiRect.w) / canvas.width, y2 = (roiRect.y+roiRect.h) / canvas.height;
    document.getElementById('roiCoordsPreview').textContent =
      `Normalized ROI: x1=${x1.toFixed(3)} y1=${y1.toFixed(3)} x2=${x2.toFixed(3)} y2=${y2.toFixed(3)}`;
    canvas.dataset.x1=x1; canvas.dataset.y1=y1; canvas.dataset.x2=x2; canvas.dataset.y2=y2;
  }
});

async function saveWorkstation() {
  const org_id = getOrgId();
  const cam_id = parseInt(document.getElementById('roiCamId').value);
  const name = document.getElementById('roiName').value;
  if (!name || canvas.dataset.x1 === undefined) { showMsg('roiMsg','Draw a rectangle and name it first.', false); return; }
  try {
    await api('/workstations/save', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
      org_id, cam_id, workstations: [{ name, x1:+canvas.dataset.x1, y1:+canvas.dataset.y1, x2:+canvas.dataset.x2, y2:+canvas.dataset.y2 }]
    })});
    showMsg('roiMsg', `Saved workstation "${name}". Draw the next box on this same photo, or upload a new one.`);
    savedRoiBoxes.push({ name, x: roiRect.x, y: roiRect.y, w: roiRect.w, h: roiRect.h });
    roiRect = null;
    document.getElementById('roiName').value = '';
    delete canvas.dataset.x1; delete canvas.dataset.y1; delete canvas.dataset.x2; delete canvas.dataset.y2;
    document.getElementById('roiCoordsPreview').textContent = '';
    drawRoiCanvas();
    loadWorkstations();
  } catch (e) { showMsg('roiMsg', e.message, false); }
}

async function loadWorkstations() {
  try {
    const camId = document.getElementById('roiCamId').value;
    const body = await api(`/workstations/check?org_id=${getOrgId()}&cam_id=${camId}`);
    document.getElementById('workstationsTable').innerHTML = body.workstations.map(w =>
      `<tr><td>${escapeHtml(w.name)}</td><td>${w.x1.toFixed(2)}, ${w.y1.toFixed(2)}, ${w.x2.toFixed(2)}, ${w.y2.toFixed(2)}</td></tr>`
    ).join('') || '<tr><td colspan="2">None yet.</td></tr>';
  } catch (e) { console.error(e); }
}

async function assignEmployee() {
  const workstation_name = document.getElementById('assignWsName').value;
  const employee_id = document.getElementById('assignEmpId').value;
  const cam_id = parseInt(document.getElementById('roiCamId').value);
  try {
    await api('/workstations/assign', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
      org_id: getOrgId(), cam_id, workstation_name, employee_id, effective_from: new Date().toISOString().slice(0,10)
    })});
    showMsg('assignMsg', `Assigned ${employee_id} to ${workstation_name}.`);
    loadIdentityStatus();
  } catch (e) { showMsg('assignMsg', e.message, false); }
}
