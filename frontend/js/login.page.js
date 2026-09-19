// login.page.js — logic specific to login.html.
// Depends on log.js and api.js being loaded first.

async function doLogin() {
  const username = document.getElementById('loginUsername').value;
  const password = document.getElementById('loginPassword').value;
  try {
    const body = await api('/admin/login', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ username, password }) });
    setSessionToken(body.session_token);
    // dashboard.html reads its identity (role/org_id/username) back from
    // GET /admin/session on load, so nothing else needs to be handed off
    // here beyond the token itself.
    window.location.href = 'dashboard.html';
  } catch (e) {
    showMsg('loginMsg', 'Login failed: ' + e.message, false);
  }
}

// If a still-valid session token is already stored (e.g. this tab was
// left on login.html, or someone bookmarked it directly), skip the form
// and go straight to the dashboard rather than asking for a password
// again unnecessarily.
document.addEventListener('DOMContentLoaded', async () => {
  if (!sessionToken) return;
  try {
    const body = await api('/admin/session');
    if (body.valid) {
      window.location.href = 'dashboard.html';
    } else {
      setSessionToken(null);
    }
  } catch (e) {
    // Backend unreachable, or something else -- stay on the login form
    // rather than looping; doLogin()'s own error handling covers the
    // actual sign-in attempt.
    console.error('Session check on login page failed:', e.message);
  }
});
