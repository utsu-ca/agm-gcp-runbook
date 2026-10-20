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
    document.getElementById('signin').style.display = '';
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
            document.getElementById('signin').style.display = 'none';
            setStatus('Signed in (session ends ' + new Date(sessionStart + SESSION_MS).toLocaleTimeString() + ')');
            scheduleRefresh();
        }
    });
    google.accounts.id.renderButton(document.getElementById('signin'), { theme: 'outline', size: 'large' });
});

function showResult(kind, icon, title, detail) {
    const box = document.getElementById('result');
    box.className = 'result';
    void box.offsetWidth; // restart animation
    box.classList.add('visible', kind);
    document.getElementById('resultIcon').innerText = icon;
    document.getElementById('resultTitle').innerText = title;
    document.getElementById('resultDetail').innerText = detail || '';
}

function renderResult(endpoint, ok, status, data) {
    if (!ok) {
        const msg = typeof data.detail === 'string' ? data.detail : 'Request failed (' + status + ')';
        const kind = status === 409 ? 'warn' : 'error';
        return showResult(kind, status === 409 ? '!' : '\u2717', status === 404 ? 'Not on list' : msg, status === 404 ? msg : '');
    }
    if (endpoint === 'check_in') return showResult('success', '\u2713', 'Checked in', data.student_number);
    const name = data.preferred_name || data.name || data.student_number;
    if (data.checked_in) showResult('warn', '!', 'Already checked in', name);
    else showResult('success', '\u2713', 'Valid - not yet checked in', name + (data.expected_location ? ' (' + data.expected_location + ')' : ''));
}

async function sendRequest(endpoint, payloadObj) {
    if (!idToken) return showResult('error', '\u2717', 'Sign in first', '');
    try {
        const response = await fetch(`/api/${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + idToken },
            body: JSON.stringify(payloadObj)
        });
        if (response.status === 401) return endSession('Session expired - sign in again');
        const data = await response.json().catch(() => ({}));
        renderResult(endpoint, response.ok, response.status, data);
    } catch (err) {
        showResult('error', '\u2717', 'Error', err.message);
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

// Card reader input: not wired to anything yet. Magstripe readers type the track
// data then press Enter; we only capture it here.
document.getElementById('cardInput').addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    // TODO: parse student number from swipe data and populate #studentNum
    e.target.value = '';
});
