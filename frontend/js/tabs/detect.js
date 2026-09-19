// js/tabs/detect.js — Try Detection tab: run real detection on one uploaded photo.

async function runDetection() {
  const fd = new FormData();
  fd.append('org_id', getOrgId());
  fd.append('cam_id', document.getElementById('detCamId').value);
  fd.append('workstation_name', document.getElementById('detWsName').value);
  const frame = document.getElementById('detFrame').files[0];
  if (!frame) { showMsg('detResult', 'Choose a photo first.', false); return; }
  fd.append('frame', frame);
  document.getElementById('detResult').innerHTML = '<div class="msg ok">Running YOLO + face matching…</div>';
  try {
    const body = await api('/workstations/simulate_detection', { method:'POST', body: fd });
    document.getElementById('detResult').innerHTML = `
      <div class="msg ok">
        People detected: <b>${body.people_detected}</b> · Occupied: <b>${body.occupied}</b><br>
        Result: <span class="badge ${body.event_type}">${escapeHtml(body.event_type)}</span>
        assigned=${escapeHtml(body.assigned_employee_id)||'—'} detected=${escapeHtml(body.detected_employee_id)||'—'}
        similarity=${body.similarity!=null?body.similarity.toFixed(3):'—'}<br>
        Face backend: ${escapeHtml(body.face_backend)}
      </div>`;
    loadIdentityStatus();
  } catch (e) { showMsg('detResult', e.message, false); }
}
