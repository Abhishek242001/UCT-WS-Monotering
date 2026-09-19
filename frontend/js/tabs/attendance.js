// js/tabs/attendance.js — Attendance tab.

async function recordDetection() {
  try {
    const body = await api('/attendance/record_detection', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
      org_id: getOrgId(),
      employee_id: document.getElementById('attEmpId').value,
      location: document.getElementById('attLocation').value,
    })});
    showMsg('attMsg', `${body.employee_id}: ${body.status} (signed in ${body.sign_in_time})`);
  } catch (e) { showMsg('attMsg', e.message, false); }
}

async function checkToday() {
  try {
    const body = await api(`/attendance/today?org_id=${getOrgId()}&employee_id=` + document.getElementById('attCheckEmpId').value);
    document.getElementById('attToday').innerHTML = `<div class="msg ok">
      Status: <span class="badge ${body.status}">${escapeHtml(body.status)}</span> ·
      Sign-in: ${escapeHtml(body.sign_in_time)} · Sign-out: ${escapeHtml(body.sign_out_time) || '—'} ·
      Last seen: ${escapeHtml(body.last_seen_workstation) || '—'}</div>`;
  } catch (e) { showMsg('attToday', e.message, false); }
}
