// js/tabs/videoanalysis.js — Video Analysis tab: upload with real
// progress, content-hash dedup, live WebSocket analysis results,
// diagnostics, and clip/annotated-video downloads.

let uploadedVideoId = null;
let pendingFile = null;   // the File object, held while we ask "already uploaded?" before committing to a real upload

async function sha256OfFile(file) {
  // Computed entirely in the browser (Web Crypto API) -- this is what
  // makes duplicate detection actually efficient: we know whether the
  // content already exists BEFORE sending a single byte of a large video
  // over the network, rather than uploading it fully and only then
  // discovering it was a duplicate.
  const buffer = await file.arrayBuffer();
  const hashBuffer = await crypto.subtle.digest('SHA-256', buffer);
  return Array.from(new Uint8Array(hashBuffer)).map(b => b.toString(16).padStart(2, '0')).join('');
}

async function onVideoFileSelected(evt) {
  const file = evt.target.files[0];
  if (!file) return;
  uploadedVideoId = null;
  pendingFile = file;
  document.getElementById('analysisDefaults').style.display = 'none';
  document.getElementById('runAnalysisBtn').style.display = 'none';
  document.getElementById('analysisResultsCard').style.display = 'none';
  document.getElementById('vaDownloadsCard').style.display = 'none';
  document.getElementById('uploadMsg').innerHTML = '';
  document.getElementById('duplicateFoundCard').style.display = 'none';

  document.getElementById('hashCheckMsg').textContent = 'Checking whether this video is already uploaded…';
  let hash;
  try {
    hash = await sha256OfFile(file);
  } catch (e) {
    document.getElementById('hashCheckMsg').textContent = '';
    uploadVideoFile(file);  // hashing failed (e.g. unsupported browser) -- fall back to a normal upload
    return;
  }
  pendingFile._computedHash = hash;

  try {
    const check = await api(`/videos/find_by_hash?org_id=${getOrgId()}&file_hash=${hash}`);
    document.getElementById('hashCheckMsg').textContent = '';
    if (check.found) {
      document.getElementById('duplicateFoundDetails').textContent =
        `Already uploaded as "${check.filename}" (${(check.size_bytes/1024/1024).toFixed(1)} MB, video_id ${check.video_id}).`;
      document.getElementById('duplicateFoundCard').dataset.videoId = check.video_id;
      document.getElementById('duplicateFoundCard').dataset.filename = check.filename;
      document.getElementById('duplicateFoundCard').style.display = 'block';
      return;  // wait for the user to choose "select existing" or "upload anyway"
    }
  } catch (e) {
    document.getElementById('hashCheckMsg').textContent = '';
    // Duplicate check failed for some reason (e.g. not logged in yet) --
    // don't block the user, just proceed to a normal upload.
  }
  uploadVideoFile(file);
}

function useExistingVideo() {
  const card = document.getElementById('duplicateFoundCard');
  uploadedVideoId = card.dataset.videoId;
  card.style.display = 'none';
  showMsg('uploadMsg', `Selected existing video "${card.dataset.filename}". Ready to analyze — no re-upload needed.`);
  document.getElementById('analysisDefaults').style.display = 'block';
  document.getElementById('runAnalysisBtn').style.display = 'inline-block';
  document.getElementById('vaDownloadsCard').style.display = 'block';
}

function dismissDuplicateAndUploadAnyway() {
  document.getElementById('duplicateFoundCard').style.display = 'none';
  if (pendingFile) uploadVideoFile(pendingFile);
}

function uploadVideoFile(file) {
  const wrap = document.getElementById('uploadProgressWrap');
  const bar = document.getElementById('uploadProgressBar');
  const label = document.getElementById('uploadProgressLabel');
  wrap.style.display = 'block';
  bar.style.width = '0%';
  label.textContent = '0%';

  const fd = new FormData();
  fd.append('org_id', getOrgId()); // the video is owned by whichever org you're currently acting as
  fd.append('file', file);

  const xhr = new XMLHttpRequest();
  xhr.open('POST', API + '/videos/upload');
  xhr.setRequestHeader('Authorization', 'Bearer ' + sessionToken);

  // This is the real upload progress — actual bytes sent to the server,
  // not a simulated timer, reported natively by the browser.
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = Math.round((e.loaded / e.total) * 100);
    bar.style.width = pct + '%';
    label.textContent = pct + '%';
  };

  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      const body = JSON.parse(xhr.responseText);
      uploadedVideoId = body.video_id;
      showMsg('uploadMsg', `Uploaded "${body.filename}" (${(body.size_bytes/1024/1024).toFixed(1)} MB). Ready to analyze.`);
      document.getElementById('analysisDefaults').style.display = 'block';
      document.getElementById('runAnalysisBtn').style.display = 'inline-block';
      document.getElementById('vaDownloadsCard').style.display = 'block';
    } else {
      showMsg('uploadMsg', 'Upload failed: ' + xhr.responseText, false);
    }
  };
  xhr.onerror = () => showMsg('uploadMsg', 'Upload failed — network error.', false);
  xhr.send(fd);
}

let _vaRoiPreviewTimer = null;
function previewVideoRoi() {
  clearTimeout(_vaRoiPreviewTimer);
  _vaRoiPreviewTimer = setTimeout(async () => {
    const camId = document.getElementById('vaCamId').value;
    const el = document.getElementById('vaRoiPreview');
    if (!camId) { el.innerHTML = 'No Camera ID entered — a full-frame "Demo-Desk" ROI will be auto-created, with no employee assigned.'; return; }
    const orgIdField = document.getElementById('vaOrgId').value;
    const org_id = orgIdField ? parseInt(orgIdField) : getOrgId();
    el.textContent = 'Checking saved ROIs for this camera…';
    try {
      const body = await api(`/workstations/check?org_id=${org_id}&cam_id=${camId}`);
      if (!body.workstations || body.workstations.length === 0) {
        el.innerHTML = `<b>No saved workstation found</b> for org ${org_id}, camera ${camId} — running analysis will auto-create a full-frame "Demo-Desk" ROI with no employee assigned. Draw one first on the Workstations tab if you want a real desk box and identity matching.`;
      } else {
        el.innerHTML = `<b>Found ${body.workstations.length} saved workstation(s)</b> for camera ${camId}: ` +
          body.workstations.map(w => `${escapeHtml(w.name)} (${w.x1.toFixed(2)},${w.y1.toFixed(2)},${w.x2.toFixed(2)},${w.y2.toFixed(2)})`).join(', ') +
          ' — analysis will match against these and any employees assigned to them.';
      }
    } catch (e) {
      el.textContent = 'Could not check saved ROIs: ' + e.message;
    }
  }, 400);
}

async function runAiAnalysis() {
  if (!uploadedVideoId) return;
  const fd = new FormData();
  const orgId = document.getElementById('vaOrgId').value;
  const camId = document.getElementById('vaCamId').value;
  const userId = document.getElementById('vaUserId').value;
  if (orgId) fd.append('org_id', orgId);
  if (camId) fd.append('cam_id', camId);
  if (userId) fd.append('user_id', userId);
  fd.append('poll_interval_seconds', 0);

  document.getElementById('analysisResultsCard').style.display = 'block';
  document.getElementById('analysisEventLog').innerHTML = '';
  document.getElementById('analysisStatusMsg').textContent = 'Starting analysis…';

  let body;
  try {
    body = await api(`/videos/${uploadedVideoId}/analyze`, { method: 'POST', body: fd });
  } catch (e) {
    document.getElementById('analysisStatusMsg').textContent = 'Failed to start: ' + e.message;
    return;
  }

  const usedDefaults = Object.entries(body.defaults_used).filter(([k, v]) => v).map(([k]) => k);
  document.getElementById('analysisStatusMsg').innerHTML =
    `Analyzing on <b>${escapeHtml(body.workstation_name)}</b> (org=${body.org_id}, cam=${body.cam_id})` +
    (usedDefaults.length ? ` — defaults used for: ${usedDefaults.join(', ')}` : ' — using your specified parameters');
  loadAnalysisHistory();  // the new run shows up immediately, status "running"

  // Same real-time push mechanism a live RTSP camera stream uses —
  // this is not a simulation, it's the identical WebSocket endpoint.
  const ws = new WebSocket(`${getBackendWsUrl()}/ws/streams/${body.stream_id}?token=${sessionToken}`);
  const MAX_EVENT_LINES = 300;
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.type === 'frame') {
      document.getElementById('vaLivePreviewImg').src = msg.image;
    } else if (msg.type === 'progress') {
      if (msg.total_frames) {
        const pct = Math.round((msg.frames_processed / msg.total_frames) * 100);
        document.getElementById('analysisProgressBar').style.width = pct + '%';
        document.getElementById('analysisProgressLabel').textContent = pct + '%';
      } else {
        // Total frame count wasn't available from this video's metadata
        // (common for some CCTV export formats) -- show a count instead
        // of leaving the bar frozen at 0%, which previously looked like
        // analysis had silently stopped.
        document.getElementById('analysisProgressBar').style.width = '100%';
        document.getElementById('analysisProgressLabel').textContent = `${msg.frames_processed} frames processed`;
      }
    } else if (msg.type === 'event') {
      const log = document.getElementById('analysisEventLog');
      const line = document.createElement('div');
      line.className = 'line';
      line.innerHTML = `<span>Frame ${msg.frame_number} — ${escapeHtml(msg.workstation_name)}</span>
        <span class="badge ${msg.event_type}">${escapeHtml(msg.event_type)}</span>`;
      log.prepend(line);
      while (log.children.length > MAX_EVENT_LINES) log.removeChild(log.lastChild);
    } else if (msg.type === 'completed') {
      document.getElementById('analysisStatusMsg').innerHTML += ' — <b>Analysis complete.</b>';
      document.getElementById('analysisProgressBar').style.width = '100%';
      document.getElementById('analysisProgressLabel').textContent = '100%';
      loadIdentityStatus();
      loadAnalysisHistory();  // flips this run's status to "completed" with a real frame count
      ws.close();
    } else if (msg.type === 'error') {
      document.getElementById('analysisStatusMsg').innerHTML += ` — <span style="color:#C0392B">${escapeHtml(msg.message)}</span>`;
    }
  };
  ws.onerror = () => { document.getElementById('analysisStatusMsg').innerHTML += ' — WebSocket error.'; };
}

async function runDiagnostics() {
  if (!uploadedVideoId) return;
  const org_id = document.getElementById('vaOrgId').value || getOrgId();
  const cam_id = document.getElementById('vaCamId').value;
  const num_samples = document.getElementById('diagSamples').value || 8;
  const msgEl = document.getElementById('diagMsg');
  const resultsEl = document.getElementById('diagResults');
  if (!cam_id) { msgEl.textContent = 'Enter a Camera ID above first — diagnostics needs a saved ROI to check against.'; return; }

  msgEl.textContent = 'Sampling frames and running real detection — a few seconds…';
  resultsEl.style.display = 'none';
  let body;
  try {
    body = await api(`/videos/${uploadedVideoId}/diagnostics?org_id=${org_id}&cam_id=${cam_id}&num_samples=${num_samples}`);
  } catch (e) { msgEl.textContent = 'Failed: ' + e.message; return; }

  msgEl.textContent = '';
  document.getElementById('diagFramesSampled').textContent = body.frames_sampled;
  document.getElementById('diagVideoInfo').textContent =
    `${body.video_duration_seconds ?? '?'}s, ${body.video_fps}fps, ${body.video_total_frames ?? 'unknown'} total frames`;
  document.getElementById('diagFramesWithPerson').textContent = `${body.frames_with_at_least_one_person} / ${body.frames_sampled}`;
  document.getElementById('diagConfidence').textContent =
    body.confidence_mean === null ? 'no detections in sampled frames'
    : `${body.confidence_min} / ${body.confidence_mean} / ${body.confidence_max}`;
  document.getElementById('diagOccupancy').textContent =
    Object.entries(body.occupancy_hits_by_workstation).map(([n, c]) => `${escapeHtml(n)}: ${c}/${body.frames_sampled}`).join(', ');
  document.getElementById('diagYoloTime').textContent = `${body.measured_yolo_seconds_per_call}s/call`;
  document.getElementById('diagEstimate').textContent =
    body.extrapolated_full_annotate_seconds_at_detect_every_3_frames === null ? '—'
    : `~${body.extrapolated_full_annotate_seconds_at_detect_every_3_frames}s for the full video at detect_every_n_frames=3`;
  resultsEl.style.display = 'block';
}

async function downloadClip() {
  if (!uploadedVideoId) return;
  const seconds = document.getElementById('clipSeconds').value || 300;
  const el = document.getElementById('clipMsg');
  el.textContent = 'Preparing clip…';
  try {
    const resp = await fetch(`${API}/videos/${uploadedVideoId}/clip?seconds=${seconds}`, {
      headers: { 'Authorization': 'Bearer ' + sessionToken }
    });
    if (!resp.ok) { el.textContent = 'Failed: ' + (await resp.text()); return; }
    const blob = await resp.blob();
    const cd = resp.headers.get('Content-Disposition') || '';
    const match = cd.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : `clip_${seconds}s.mp4`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    el.textContent = `Downloaded "${filename}".`;
  } catch (e) { el.textContent = 'Failed: ' + e.message; }
}

async function downloadAnnotated() {
  if (!uploadedVideoId) return;
  const org_id = document.getElementById('vaOrgId').value || getOrgId();
  const cam_id = document.getElementById('vaCamId').value;
  const max_seconds = document.getElementById('annotateSeconds').value || 60;
  const detect_every_n_frames = document.getElementById('annotateEveryN').value || 3;
  const el = document.getElementById('annotateMsg');
  if (!cam_id) { el.textContent = 'Enter a Camera ID above first — the annotated video needs a saved ROI to draw.'; return; }

  el.textContent = 'Running detection and rendering — this blocks until done (see the processing-time hint above), please wait…';
  const fd = new FormData();
  fd.append('org_id', org_id); fd.append('cam_id', cam_id);
  fd.append('max_seconds', max_seconds); fd.append('detect_every_n_frames', detect_every_n_frames);
  let body;
  try {
    body = await api(`/videos/${uploadedVideoId}/annotate`, { method: 'POST', body: fd });
  } catch (e) { el.textContent = 'Failed: ' + e.message; return; }

  el.innerHTML = `Rendered ${body.frames_processed} frames (${body.seconds_processed}s of video) in ${body.processing_time_seconds}s server time. ` +
    (body.playable_in_browser ? '' : '<b>Note: this server has no ffmpeg for H.264 re-encoding, so the file downloads but likely won\'t preview inline in your browser — open it in VLC or similar.</b> ') +
    'Downloading…';
  try {
    const resp = await fetch(`${API}${body.download_url}`, { headers: { 'Authorization': 'Bearer ' + sessionToken } });
    if (!resp.ok) { el.textContent = 'Rendered, but download failed: ' + (await resp.text()); return; }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `${body.annotated_id}.mp4`; document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    el.textContent += ' Done.';
  } catch (e) { el.textContent = 'Rendered, but download failed: ' + e.message; }
}

async function loadAnalysisHistory() {
  try {
    const body = await api(`/videos/analysis_runs?org_id=${getOrgId()}`);
    const rows = body.runs.map(r => {
      const started = escapeHtml(r.started_at || '').replace('T', ' ').substring(0, 19);
      const frames = r.frames_processed != null ? r.frames_processed : '—';
      return `<tr>
        <td>${escapeHtml(r.filename)}</td>
        <td>${escapeHtml(body.org_id)} / ${escapeHtml(r.cam_id)}</td>
        <td>${started}</td>
        <td><span class="badge ${r.status === 'completed' ? 'MATCH' : 'UNKNOWN'}">${escapeHtml(r.status)}</span></td>
        <td>${frames}</td>
      </tr>`;
    }).join('');
    document.getElementById('historyTable').innerHTML = rows || '<tr><td colspan="5">No analysis runs yet for this org.</td></tr>';
  } catch (e) { showMsg('historyMsg', e.message, false); }
}
