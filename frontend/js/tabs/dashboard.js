// js/tabs/dashboard.js — Dashboard tab (live identity status table).

async function loadIdentityStatus() {
  try {
    const camId = document.getElementById('dashCamId').value;
    const body = await api(`/workstations/identity_status?org_id=${getOrgId()}&cam_id=${camId}`);
    const rows = body.workstations.map(w => `<tr>
      <td>${escapeHtml(w.name)}</td><td>${escapeHtml(w.occupancy_status)}</td><td>${escapeHtml(w.assigned_employee_id)||'—'}</td>
      <td>${escapeHtml(w.detected_employee_id)||'—'}</td><td><span class="badge ${w.match_status}">${escapeHtml(w.match_status)}</span></td>
      <td>${w.similarity!=null?w.similarity.toFixed(3):'—'}</td></tr>`).join('');
    document.getElementById('statusTable').innerHTML = rows || '<tr><td colspan="6">No workstations yet — add one in the Workstations tab.</td></tr>';
  } catch (e) { showMsg('statusMsg', e.message, false); }
}
