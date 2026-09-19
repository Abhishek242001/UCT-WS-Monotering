// api.js — backend URL resolution, session-token persistence, the fetch
// wrapper, and small shared DOM utilities. Loaded on every page, after
// log.js and before any page-specific script.

// --- environment-aware backend URL resolver (Section 10.2 of the docs) ---
function getBackendBaseUrl() {
  const { protocol, hostname } = window.location;
  if (hostname === "localhost" || hostname === "127.0.0.1") {
    return "http://localhost:8001";
  }
  const match = hostname.match(/^(\d+)-(.+)$/);
  if (match) {
    return `${protocol}//8001-${match[2]}`;
  }
  return `${protocol}//${hostname}:8001`;
}
const API = getBackendBaseUrl();
function getBackendWsUrl() {
  return API.replace(/^http/, "ws");
}
console.log("Page origin:", window.location.origin, "| Resolved backend API base URL:", API);
document.addEventListener("DOMContentLoaded", () => {
  const el = document.getElementById("loginApiTarget");
  if (el) el.textContent = `Backend target: ${API}`;
});

// --- session token ---
// NOTE: previously this was `let sessionToken = null;` with no
// persistence at all, which was only safe because the whole app lived in
// one HTML file and "login" vs. "dashboard" were just two <div>s toggled
// with display:none/block -- there was never a real page navigation to
// survive. Now that login.html and dashboard.html are separate pages, a
// real page load happens between them, so the token has to be persisted
// somewhere or dashboard.html would load with no token at all.
// sessionStorage (not localStorage) is used deliberately: it survives
// navigation and reload within the same tab -- matching "session" -- but
// is cleared when the tab is closed, rather than lingering indefinitely
// like localStorage would.
const SESSION_TOKEN_KEY = "uct_session_token_v1";
let sessionToken = sessionStorage.getItem(SESSION_TOKEN_KEY) || null;

function setSessionToken(token) {
  sessionToken = token;
  if (token) sessionStorage.setItem(SESSION_TOKEN_KEY, token);
  else sessionStorage.removeItem(SESSION_TOKEN_KEY);
}

function authHeaders() {
  return sessionToken ? { "Authorization": "Bearer " + sessionToken } : {};
}

async function api(path, opts = {}) {
  const url = API + path;
  console.log("API request:", opts.method || "GET", url);
  let res;
  try {
    res = await fetch(url, { ...opts, headers: { ...(opts.headers||{}), ...authHeaders() } });
  } catch (networkErr) {
    // fetch() throws a generic, detail-free TypeError for BOTH a CORS
    // rejection and a genuine network/connection failure -- the browser
    // deliberately hides which one it was from JS. Log everything we DO
    // know so the downloaded log is still actionable without devtools.
    console.error(
      "API network error:", opts.method || "GET", url, "-", networkErr.message,
      "| page origin:", window.location.origin,
      "| Usually either the backend is unreachable, or this is a CORS rejection",
      "(check the browser's Network/Console tab for a 'blocked by CORS policy' message).",
      "If it is CORS: the backend's FRONTEND_ORIGIN must include exactly", window.location.origin
    );
    throw new Error(`Could not reach ${url}. This is usually a CORS or network issue -- check the browser console, and confirm the backend's FRONTEND_ORIGIN includes ${window.location.origin}.`);
  }
  let body;
  try { body = await res.json(); } catch { body = null; }
  if (!res.ok) {
    console.error("API error response:", res.status, opts.method || "GET", url, body);
    throw new Error((body && body.detail) || res.statusText);
  }
  console.log("API response:", res.status, opts.method || "GET", url);
  return body;
}

function getOrgId() {
  return parseInt(document.getElementById('globalOrgId').value, 10);
}

// Escapes text before it ever reaches innerHTML. Employee names/IDs,
// workstation names, stream source URLs, and uploaded filenames are all
// admin-supplied and get echoed back by the API -- without this, any of
// them could carry a script tag that runs in another admin's browser
// (a real stored/reflected XSS path, not a theoretical one, since
// sessionToken lives in reachable scope on this same page).
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str === null || str === undefined ? '' : String(str);
  return div.innerHTML;
}

function showMsg(elId, text, ok=true) {
  document.getElementById(elId).innerHTML = `<div class="msg ${ok?'ok':'err'}">${escapeHtml(text)}</div>`;
}
