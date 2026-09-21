// js/tabs/livestream.js — Live Stream tab: start/stop/list real
// background stream-processing workers, plus a live visual preview.
//
// The preview shows the MOST RECENTLY STARTED stream only -- the active
// streams table below supports multiple concurrent streams (each with
// its own Stop button), but there is one preview <img>, not one per row.
// Starting a new stream switches what the preview shows.

let _livePreviewWs = null;

function _closeLivePreviewSocket() {
  if (_livePreviewWs) {
    _livePreviewWs.onclose = null;  // this is a deliberate close, not a drop -- don't let onclose fire a stale error message
    _livePreviewWs.close();
    _livePreviewWs = null;
  }
}

function _connectLivePreview(streamId) {
  _closeLivePreviewSocket();
  const img = document.getElementById('livePreviewImg');
  const msg = document.getElementById('livePreviewMsg');
  img.removeAttribute('src');
  msg.textContent = 'Connecting to live preview…';

  const ws = new WebSocket(`${getBackendWsUrl()}/ws/streams/${streamId}?token=${sessionToken}`);
  _livePreviewWs = ws;

  ws.onmessage = (evt) => {
    const data = JSON.parse(evt.data);
    if (data.type === 'frame') {
      img.src = data.image;
      msg.textContent = `Live — frame ${data.frame_number}`;
    } else if (data.type === 'source_status') {
      msg.textContent = data.state === 'reconnecting'
        ? `Camera connection lost. Reconnecting (attempt ${data.attempt})…`
        : 'Camera reconnected.';
    } else if (data.type === 'error') {
      msg.textContent = 'Error: ' + data.message;
    } else if (data.type === 'completed') {
      msg.textContent = `Stream ended (${data.reason}). Last preview frame shown above.`;
    }
  };
  ws.onerror = () => { msg.textContent = 'Live preview connection error.'; };
  ws.onclose = () => { msg.textContent = 'Live preview disconnected.'; };
}

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
    _connectLivePreview(body.stream_id);
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
