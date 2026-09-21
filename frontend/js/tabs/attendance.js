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

// --- item 10: stats section ---

async function loadLiveNow() {
  try {
    const body = await api(`/attendance/live_now?org_id=${getOrgId()}`);
    const tiles = body.employees.map(e => `<div class="status-tile ${e.status === 'ON_BREAK' ? 'ON_BREAK' : 'MATCH'}">
      <div class="tile-name">${escapeHtml(e.name)} (${escapeHtml(e.employee_id)})</div>
      <div class="tile-sub">${escapeHtml(e.status)}, last seen ${escapeHtml(e.last_seen_workstation) || 'nowhere yet'}</div>
    </div>`).join('');
    document.getElementById('liveNowGrid').innerHTML = tiles || '<p class="hint">No one is present right now.</p>';
  } catch (e) { showMsg('liveNowMsg', e.message, false); }
}

async function loadEmployeeStats() {
  try {
    const empId = document.getElementById('statsEmpId').value;
    if (!empId) { showMsg('statsMsg', 'Enter an employee ID first.', false); return; }
    const source = document.getElementById('statsSource').value;
    const endpoint = source === 'simulated' ? '/attendance/simulated/report' : '/attendance/report';
    const from = document.getElementById('statsFrom').value;
    const to = document.getElementById('statsTo').value;
    let url = `${endpoint}?org_id=${getOrgId()}&employee_id=${encodeURIComponent(empId)}`;
    if (from) url += `&from_=${from}`;
    if (to) url += `&to=${to}`;
    const body = await api(url);

    const rows = body.days.map(d => {
      const hours = d.net_present_seconds != null ? (d.net_present_seconds / 3600).toFixed(2) : '—';
      return `<tr tabindex="0" onclick="document.getElementById('timelineDate').value='${escapeHtml(d.date)}'; loadDailyTimeline();" onkeydown="if(event.key==='Enter'){this.click();}">
        <td>${escapeHtml(d.date)}</td><td>${escapeHtml(d.sign_in_time)||'—'}</td><td>${escapeHtml(d.sign_out_time)||'—'}</td>
        <td><span class="badge ${d.status}">${escapeHtml(d.status)}</span></td><td>${hours}</td>
        <td>${escapeHtml(d.day_classification) || '—'}</td></tr>`;
    }).join('');
    document.getElementById('statsReportTable').innerHTML = rows || '<tr><td colspan="6">No attendance records in this range.</td></tr>';

    // Weekly/period average: only over days with a COMPLETED
    // (net_present_seconds computed) record -- an ongoing, not-yet-
    // signed-out day has no final total yet, and averaging in a partial
    // day would understate every other day's real hours.
    const withHours = body.days.filter(d => d.net_present_seconds != null);
    const avgSeconds = withHours.length
      ? withHours.reduce((sum, d) => sum + d.net_present_seconds, 0) / withHours.length
      : null;
    document.getElementById('statsSummary').innerHTML = avgSeconds != null
      ? `<div class="msg ok">Average over ${withHours.length} completed day(s): <b>${(avgSeconds/3600).toFixed(2)} hours/day</b></div>`
      : '<div class="hint">No completed days in this range yet to average.</div>';
  } catch (e) { showMsg('statsMsg', e.message, false); }
}

function _hhmmssToSeconds(hhmmss) {
  const parts = hhmmss.split(':').map(Number);
  return parts[0]*3600 + (parts[1]||0)*60 + (parts[2]||0);
}

async function loadDailyTimeline() {
  try {
    const empId = document.getElementById('statsEmpId').value;
    const date = document.getElementById('timelineDate').value;
    if (!empId || !date) { showMsg('timelineMsg', 'Enter an employee ID above and pick a date first.', false); return; }
    const source = document.getElementById('statsSource').value;
    const endpoint = source === 'simulated' ? '/attendance/simulated/segments' : '/attendance/segments';
    const body = await api(`${endpoint}?org_id=${getOrgId()}&employee_id=${encodeURIComponent(empId)}&date=${date}`);
    const segs = body.segments;
    document.getElementById('timelineMsg').innerHTML = '';
    if (!segs.length) {
      document.getElementById('timelineBar').innerHTML = '';
      document.getElementById('timelineLegend').innerHTML = '';
      document.getElementById('utilizationStat').innerHTML = '<p class="hint">No segments recorded for this date.</p>';
      return;
    }

    // Segment times are UTC (the backend records everything with
    // datetime.utcnow()) -- toISOString() is always UTC too, so this
    // stays consistent with how segment start/end times were stored,
    // without a timezone mismatch creeping in.
    //
    // An OPEN segment (no end_time) is only assumed "still running
    // until now" when the viewed date IS today -- for a past date, an
    // open segment is a data anomaly (should have been closed by
    // close_stale_sessions), and using TODAY's wall-clock time-of-day
    // as its end would be meaningless, or even show a negative/zero
    // duration if today's clock time happens to be earlier in the day
    // than the segment's own start_time. Falls back to the latest
    // known end time among the day's OTHER segments instead.
    const todayStr = new Date().toISOString().substr(0, 10);
    const nowSeconds = _hhmmssToSeconds(new Date().toISOString().substr(11, 8));
    const starts = segs.map(s => _hhmmssToSeconds(s.start_time));
    const knownEnds = segs.filter(s => s.end_time).map(s => _hhmmssToSeconds(s.end_time));
    const fallbackEnd = knownEnds.length ? Math.max(...knownEnds) : Math.max(...starts);
    const effectiveNow = date === todayStr ? nowSeconds : fallbackEnd;
    const ends = segs.map(s => s.end_time ? _hhmmssToSeconds(s.end_time) : effectiveNow);
    const dayStart = Math.min(...starts);
    const dayEnd = Math.max(...ends);
    const span = Math.max(dayEnd - dayStart, 1);

    let deskSeconds = 0, roomSeconds = 0;
    const blocks = segs.map(s => {
      const segStart = _hhmmssToSeconds(s.start_time);
      const segEnd = s.end_time ? _hhmmssToSeconds(s.end_time) : effectiveNow;
      const duration = Math.max(segEnd - segStart, 0);
      const isRoom = s.location === 'ROOM';
      if (isRoom) roomSeconds += duration; else deskSeconds += duration;
      const leftPct = ((segStart - dayStart) / span) * 100;
      const widthPct = (duration / span) * 100;
      const cls = isRoom ? 'timeline-room' : 'timeline-desk';
      const label = isRoom ? 'In room' : escapeHtml(s.location);
      return `<div class="timeline-block ${cls}" style="left:${leftPct}%; width:${widthPct}%" title="${label}: ${escapeHtml(s.start_time)}–${escapeHtml(s.end_time)||'now'}"></div>`;
    }).join('');
    document.getElementById('timelineBar').innerHTML = blocks;
    document.getElementById('timelineLegend').innerHTML =
      '<span class="legend-item"><span class="legend-swatch timeline-desk"></span>At desk</span>' +
      '<span class="legend-item"><span class="legend-swatch timeline-room"></span>In room, not at desk</span>' +
      '<span class="legend-item"><span class="legend-swatch timeline-gap"></span>Away / not tracked</span>';

    // Utilization here means "share of TRACKED time spent at a desk",
    // not "share of the scheduled workday" -- the latter would need
    // shift-schedule data cross-referenced in, which this view
    // deliberately doesn't attempt yet. Labeled precisely so it isn't
    // read as a claim this doesn't make.
    const totalTracked = deskSeconds + roomSeconds;
    const utilizationPct = totalTracked > 0 ? ((deskSeconds / totalTracked) * 100).toFixed(1) : null;
    document.getElementById('utilizationStat').innerHTML = `<div class="msg ok">
      At desk: <b>${(deskSeconds/3600).toFixed(2)}h</b> · In room (not at desk): <b>${(roomSeconds/3600).toFixed(2)}h</b>
      ${utilizationPct != null ? `· Desk share of tracked time: <b>${utilizationPct}%</b>` : ''}</div>`;
  } catch (e) { showMsg('timelineMsg', e.message, false); }
}
