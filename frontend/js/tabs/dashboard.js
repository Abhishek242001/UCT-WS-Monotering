// js/tabs/dashboard.js — Dashboard tab (desk status grid + detailed identity status table).

async function loadIdentityStatus() {
  try {
    const camId = document.getElementById('dashCamId').value;
    const body = await api(`/workstations/identity_status?org_id=${getOrgId()}&cam_id=${camId}`);

    // Grid: one tile per desk, colored the same way the existing
    // MATCH/MISMATCH/UNKNOWN badges already are -- a quick glance
    // without opening Live Stream. A VACANT desk shows who's assigned
    // there rather than a match result, since there's no detection to
    // report; an occupied desk shows the match badge's own text
    // (MATCH/MISMATCH/UNKNOWN) so the tile and the detail table below
    // never disagree about what a status means.
    const tiles = body.workstations.map(w => {
      const cls = w.occupancy_status === 'VACANT' ? 'VACANT' : w.match_status;
      const sub = w.occupancy_status === 'VACANT'
        ? (w.assigned_employee_id ? `Vacant, assigned to ${escapeHtml(w.assigned_employee_id)}` : 'Vacant, no one assigned')
        : `${escapeHtml(w.match_status)}${w.detected_employee_id ? ': ' + escapeHtml(w.detected_employee_id) : ''}`;
      return `<div class="status-tile ${cls}">
        <div class="tile-name">${escapeHtml(w.name)}</div>
        <div class="tile-sub">${sub}</div>
      </div>`;
    }).join('');
    document.getElementById('statusGrid').innerHTML = tiles || '<p class="hint">No workstations yet. Add one in the Workstations section.</p>';

    const rows = body.workstations.map(w => `<tr>
      <td>${escapeHtml(w.name)}</td><td>${escapeHtml(w.occupancy_status)}</td><td>${escapeHtml(w.assigned_employee_id)||'—'}</td>
      <td>${escapeHtml(w.detected_employee_id)||'—'}</td><td><span class="badge ${w.match_status}">${escapeHtml(w.match_status)}</span></td>
      <td>${w.similarity!=null?w.similarity.toFixed(3):'—'}</td></tr>`).join('');
    document.getElementById('statusTable').innerHTML = rows || '<tr><td colspan="6">No workstations yet — add one in the Workstations tab.</td></tr>';
  } catch (e) { showMsg('statusMsg', e.message, false); }
}
