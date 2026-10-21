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

const matchesEl = document.getElementById('matches');

function renderMatches(matches) {
    matchesEl.replaceChildren();
    for (const m of matches) {
        const row = document.createElement('div');
        row.className = 'match' + (m.checked_in ? ' done' : '');
        const lines = [
            ['Legal name', m.name],
            ['Preferred name', m.preferred_name || '-'],
            ['ID', m.id.slice(0, 8)],
            ['Expected location', m.expected_location || '-'],
        ];
        for (const [k, v] of lines) {
            const line = document.createElement('div');
            const label = document.createElement('strong');
            label.innerText = k + ': ';
            line.append(label, document.createTextNode(v));
            row.append(line);
        }
        const btn = document.createElement('button');
        btn.className = 'btn-checkin';
        btn.innerText = m.checked_in ? 'Already checked in' : 'Check in this person';
        btn.disabled = m.checked_in;
        btn.addEventListener('click', () => checkInById(m.id));
        row.append(btn);
        matchesEl.append(row);
    }
}

function renderResult(endpoint, ok, status, data) {
    if (!ok) {
        const msg = typeof data.detail === 'string' ? data.detail : 'Request failed (' + status + ')';
        const kind = status === 409 ? 'warn' : 'error';
        return showResult(kind, status === 409 ? '!' : '\u2717', status === 404 ? 'Not on list' : msg, status === 404 ? msg : '');
    }
    if (endpoint === 'check_in') {
        matchesEl.replaceChildren();
        return showResult('success', '\u2713', 'Checked in', data.preferred_name || data.name);
    }
    renderMatches(data.matches);
    if (data.matches.length > 1) return showResult('warn', '!', data.matches.length + ' matches', 'Choose the right person below.');
    const m = data.matches[0];
    if (m.checked_in) showResult('warn', '!', 'Already checked in', m.preferred_name || m.name);
    else showResult('success', '\u2713', 'Valid - not yet checked in', m.preferred_name || m.name);
}

async function sendRequest(endpoint, payloadObj) {
    if (endpoint !== 'check_in') matchesEl.replaceChildren();
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

function lookup() {
    const student_number = document.getElementById('studentNum').value.trim();
    const last_name = document.getElementById('lastName').value.trim();
    if (!student_number && !last_name) return showResult('error', '\u2717', 'Enter a student number and/or last name', '');
    const body = {};
    if (student_number) body.student_number = student_number;
    if (last_name) body.last_name = last_name;
    return sendRequest('is_checked_in', body);
}

function checkInById(id) {
    sendRequest('check_in', { id, attendance_mode: document.getElementById('attendance').value });
}

document.getElementById('btnStatus').addEventListener('click', lookup);

// "Check In" looks up first; a single unchecked match is checked in directly via its unique ID.
document.getElementById('btnCheckIn').addEventListener('click', async () => {
    await lookup();
    const rows = matchesEl.querySelectorAll('.match');
    if (rows.length === 1) {
        const btn = rows[0].querySelector('button');
        if (!btn.disabled) btn.click();
    }
});

// Card reader input: magstripe readers type the track data then press Enter.
// Format: %FIRST^LAST^1007600908^2021-10-27?;2176102213325201?
// Only the truncated ID (digits at index -5..-2 of the student number, e.g. 0090) is matched.
// Set `window.DEBUG_SWIPE = true` in the browser console to log swipe parsing.
// Card contents are masked (digits -> #, letters -> A) so no real numbers reach the console.
function debugSwipe(...args) {
    if (window.DEBUG_SWIPE) console.log('[swipe]', ...args);
}

function maskCard(s) {
    return s.replace(/\d/g, '#').replace(/[A-Za-z]/g, 'A');
}

function parseCard(raw) {
    const num = (raw.split('^')[2] || '').trim();
    const id = /^\d{5,}$/.test(num) ? num.slice(-5, -1) : null;
    debugSwipe('raw length', raw.length, 'fields', raw.split('^').length, 'masked', maskCard(raw));
    debugSwipe('field[2] length', num.length, 'masked', maskCard(num), id ? 'truncated_student_number found' : 'REJECTED (need 5+ digits)');
    return id;
}

document.getElementById('cardInput').addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const raw = e.target.value;
    const truncated_student_number = parseCard(raw);
    e.target.value = '';
    if (!truncated_student_number) return showResult('error', '\u2717', 'Unreadable card', 'Swipe again or enter the number manually.');
    const parts = raw.split('^');
    document.getElementById('studentNum').value = (parts[2] || '').trim();
    document.getElementById('lastName').value = (parts[1] || '').trim();
    await sendRequest('is_checked_in', { truncated_student_number });
    const rows = matchesEl.querySelectorAll('.match');
    if (rows.length === 1) {
        const btn = rows[0].querySelector('button');
        if (!btn.disabled) btn.click();
    }
});
