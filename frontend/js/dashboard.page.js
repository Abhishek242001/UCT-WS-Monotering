// dashboard.page.js — logic specific to dashboard.html: verifying/restoring
// the session on load, signing out, and tab switching.
// Depends on log.js and api.js being loaded first, and the tab modules
// (js/tabs/*.js) being loaded before this file, since restoreSession()
// below calls into them (loadIdentityStatus, loadWorkstations,
// loadEmployees) once the session is confirmed valid.

async function checkBackend() {
  try { await api('/'); document.getElementById('backendStatus').textContent = 'backend connected: ' + API; }
  catch (e) {
    document.getElementById('backendStatus').textContent = 'backend unreachable at ' + API;
    console.error('checkBackend failed:', e.message);
  }
}

// An org-scoped HR_ADMIN can only ever act on their own org, so the
// global Org ID field starts there and is locked -- there's no valid
// other value for them to enter. A SUPER_ADMIN has no fixed org
// (org_id is null), so the field stays editable, defaulting to 1.
// Shared by both the fresh-login path (previously inline in doLogin())
// and the page-reload restore path below, since both end up with the
// same role/org_id/username to apply.
function applySessionIdentity(role, org_id, username) {
  const orgField = document.getElementById('globalOrgId');
  if (role === 'SUPER_ADMIN') {
    orgField.value = '1';
    orgField.disabled = false;
    document.getElementById('whoAmI').textContent = `${username} · SUPER_ADMIN (all orgs)`;
  } else {
    orgField.value = String(org_id);
    orgField.disabled = true;
    document.getElementById('whoAmI').textContent = `${username} · org ${org_id}`;
  }
}

// Runs once on every dashboard.html load. Previously there was no
// equivalent of this: login and dashboard were the same page, so
// "logged in" just meant an in-memory variable was set a moment ago.
// Now that dashboard.html can be reached directly (reload, bookmark,
// browser back/forward), it has to actually verify the stored token
// against the backend before trusting it -- GET /admin/session returns
// {valid:false} (not an error) for a missing/expired token, so that's
// checked explicitly rather than relying on a thrown exception.
async function restoreSession() {
  if (!sessionToken) {
    window.location.href = 'login.html';
    return;
  }
  let body;
  try {
    body = await api('/admin/session');
  } catch (e) {
    console.error('Session check failed:', e.message);
    setSessionToken(null);
    window.location.href = 'login.html';
    return;
  }
  if (!body.valid) {
    setSessionToken(null);
    window.location.href = 'login.html';
    return;
  }
  applySessionIdentity(body.role, body.org_id, body.username);
  checkBackend();
  loadIdentityStatus(); loadWorkstations(); loadEmployees();
}

// Previously this only cleared local state (sessionToken = null) and
// toggled two <div>s -- it never actually told the backend to end the
// session, so the AdminSession row in the database stayed valid until
// it expired on its own (SESSION_TTL_SECONDS). This now calls the real
// POST /admin/logout endpoint first (per app/routers/admin_auth.py, it
// takes {"session_token": "..."} in the JSON body), best-effort: if
// that call fails (e.g. backend briefly unreachable) the user is still
// signed out locally and redirected either way, since being unable to
// reach the server is not a reason to trap someone on a page they just
// clicked "Sign out" from.
async function logout() {
  try {
    await api('/admin/logout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_token: sessionToken }),
    });
  } catch (e) {
    console.error('Server-side logout call failed (signing out locally anyway):', e.message);
  }
  setSessionToken(null);
  window.location.href = 'login.html';
}

function showTab(name) {
  document.querySelectorAll('main section').forEach(s => s.style.display = 'none');
  document.getElementById('tab-'+name).style.display = 'block';
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
}

document.addEventListener('DOMContentLoaded', restoreSession);
