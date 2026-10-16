const SESSION_MS = 2 * 60 * 60 * 1000;   // hard cap on a session
const REFRESH_MS = 50 * 60 * 1000;       // Google ID tokens last 1 hour; renew before that
let idToken = null;
let sessionStart = 0;
let refreshTimer = null;

function show(id, text) {
    const el = document.getElementById(id);
    el.classList.add('visible');
    el.innerText = text;
}

function setStatus(msg) { show('who', msg); }

function endSession(msg) {
    idToken = null;
    sessionStart = 0;
    clearTimeout(refreshTimer);
    setStatus(msg);
}

function scheduleRefresh() {
    clearTimeout(refreshTimer);
    const remaining = sessionStart + SESSION_MS - Date.now();
    if (remaining <= 0) return endSession('Session expired - sign in again');
    // Silently ask Google for a fresh token (needs an active Google session in this browser).
    refreshTimer = setTimeout(() => google.accounts.id.prompt(), Math.min(REFRESH_MS, remaining));
}

window.addEventListener('load', async () => {
    const cfg = await (await fetch('/api/config')).json();
    google.accounts.id.initialize({
        client_id: cfg.client_id,
        hd: cfg.domain,
        auto_select: true,
        callback: (resp) => {
            idToken = resp.credential;
            if (!sessionStart || Date.now() - sessionStart >= SESSION_MS) sessionStart = Date.now();
            setStatus('Signed in (session ends ' + new Date(sessionStart + SESSION_MS).toLocaleTimeString() + ')');
            scheduleRefresh();
        }
    });
    google.accounts.id.renderButton(document.getElementById('signin'), { theme: 'outline', size: 'large' });
});

async function sendRequest(endpoint, payloadObj) {
    if (!idToken) return show('output', 'Sign in first.');
    try {
        const response = await fetch(`/api/${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + idToken },
            body: JSON.stringify(payloadObj)
        });
        if (response.status === 401) return endSession('Session expired - sign in again');
        show('output', JSON.stringify(await response.json(), null, 2));
    } catch (err) {
        show('output', `Error: ${err.message}`);
    }
}

const studentNumber = () => document.getElementById('studentNum').value;

document.getElementById('btnStatus').addEventListener('click', () =>
    sendRequest('is_checked_in', { student_number: studentNumber() }));

document.getElementById('btnCheckIn').addEventListener('click', () =>
    sendRequest('check_in', {
        student_number: studentNumber(),
        location: document.getElementById('location').value,
        attendance_mode: document.querySelector('input[name="attendance"]:checked').value
    }));
