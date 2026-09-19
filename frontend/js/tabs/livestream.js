// js/tabs/livestream.js — Live Stream tab: start/stop/list real
// background stream-processing workers.

async function startStream() {
  const source = document.getElementById('streamSource').value;
  const org_id = getOrgId();
  const cam_id = parseInt(document.getElementById('streamCamId').value);
  const maxFramesVal = document.getElementById('streamMaxFrames').value;
  const payload = { source, org_id, cam_id, user_id: 1, poll_interval_seconds: 0.3 };
  if (maxFramesVal) payload.max_frames = parseInt(maxFramesVal);
  try {
    const body = await api('/streams/start', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    showMsg('streamMsg', `Started stream ${body.stream_id}. It's running in a background thread on the server now.`);
    loadStreams();
  } catch (e) { showMsg('streamMsg', e.message, false); }
}

async function stopStream(streamId) {
  try {
    await api('/streams/stop', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ stream_id: streamId, org_id: getOrgId() }) });
    loadStreams();
  } catch (e) { console.error(e); }
}

async function loadStreams() {
  try {
    const body = await api('/streams/list?org_id=' + getOrgId());
    document.getElementById('streamsTable').innerHTML = body.streams.map(s => `<tr>
      <td>${escapeHtml(s.stream_id.slice(0,8))}…</td><td>${escapeHtml(s.source)}</td>
      <td><span class="badge ${s.is_alive?'MATCH':'VACANT'}">${s.is_alive?'running':'stopped'}</span></td>
      <td>${s.frames_processed}</td><td>${s.source_opened?'yes':'no'}</td>
      <td><button class="secondary" onclick="stopStream('${s.stream_id}')">Stop</button></td>
    </tr>`).join('') || '<tr><td colspan="6">No streams started yet.</td></tr>';
  } catch (e) { console.error(e); }
}
