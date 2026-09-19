// log.js — lightweight client-side debug log.
// The browser sandbox means JS cannot write a real file to disk on its
// own; this instead buffers every console.log/info/warn/error call
// (persisted to localStorage so it survives a reload) and offers it as a
// downloadable .txt via the "Download debug log" link on both the login
// page and the dashboard header. This is what gives you a "frontend log
// file" to inspect -- since a CORS rejection is enforced by the browser
// itself, nothing about it ever reaches the backend's log, so this is the
// only place it can be captured.
// Loaded on every page, first, so it can start capturing before anything
// else runs.

const APP_LOG_KEY = "uct_debug_log_v1";
const APP_LOG_MAX_ENTRIES = 500;

function loadAppLog() {
  try { return JSON.parse(localStorage.getItem(APP_LOG_KEY) || "[]"); } catch { return []; }
}
let appLog = loadAppLog();

function pushAppLog(level, args) {
  const msg = args.map(a => {
    if (typeof a === "string") return a;
    try { return JSON.stringify(a); } catch { return String(a); }
  }).join(" ");
  appLog.push({ t: new Date().toISOString(), level, msg });
  if (appLog.length > APP_LOG_MAX_ENTRIES) appLog = appLog.slice(-APP_LOG_MAX_ENTRIES);
  try { localStorage.setItem(APP_LOG_KEY, JSON.stringify(appLog)); } catch { /* storage full/unavailable -- log still shows in devtools console */ }
}
["log", "info", "warn", "error"].forEach(level => {
  const original = console[level].bind(console);
  console[level] = (...args) => { pushAppLog(level, args); original(...args); };
});

function downloadAppLog() {
  const header = `UCT Workstation Monitor -- frontend debug log
page origin: ${window.location.origin}
resolved backend API base URL: ${typeof API !== "undefined" ? API : "(not yet resolved)"}
generated: ${new Date().toISOString()}

`;
  const text = header + appLog.map(l => `${l.t} [${l.level}] ${l.msg}`).join("\n");
  const blob = new Blob([text], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `uct-frontend-log-${Date.now()}.txt`;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}
function clearAppLog() {
  appLog = [];
  try { localStorage.removeItem(APP_LOG_KEY); } catch {}
}
