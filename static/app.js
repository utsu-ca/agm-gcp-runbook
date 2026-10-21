const SESSION_MS = 2 * 60 * 60 * 1000;   // hard cap on a session
const REFRESH_MS = 50 * 60 * 1000;       // Google ID tokens last 1 hour; renew before that
let idToken = null;
let sessionStart = 0;
let refreshTimer = null;
let testMode = false;   // set from /api/config (TEST_MODE env var)

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
    testMode = !!cfg.test_mode;
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
        const heading = document.createElement('div');
        heading.className = 'match-name';
        heading.innerText = m.preferred_name || m.name;
        row.append(heading);
        const lines = [
            ['Legal name', m.name],
            ['Student #', m.student_number ? '\u2022\u2022\u2022\u2022' + m.student_number.slice(-5, -1) : '-'],
            ['Expected location', m.expected_location || '-'],
            ['Record ID', m.id.slice(0, 8)],
        ];
        if (m.checked_in) lines.splice(3, 0, ['Checked in at', m.current_location || '-']);
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

function displayName(d) {
    const pref = d.preferred_name, legal = d.name;
    return pref && pref !== legal ? pref + ' (' + legal + ')' : (legal || pref || '');
}

function renderResult(endpoint, ok, status, data) {
    if (!ok && status === 409 && data.detail && data.detail.code === 'location_mismatch') {
        return showResult('warn', '!', 'Location differs from expected', 'Expected: ' + (data.detail.expected_location || '-'));
    }
    if (!ok) {
        const msg = typeof data.detail === 'string' ? data.detail : 'Request failed (' + status + ')';
        const kind = status === 409 ? 'warn' : 'error';
        return showResult(kind, status === 409 ? '!' : '\u2717', status === 404 ? 'Not on list' : msg, status === 404 ? msg : '');
    }
    if (endpoint === 'check_in') {
        matchesEl.replaceChildren();
        if (data.location_mismatch) return showResult('warn', '!', 'Checked in (different location)', displayName(data));
        return showResult('success', '\u2713', 'Checked in', displayName(data));
    }
    renderMatches(data.matches);
    if (data.matches.length > 1) return showResult('warn', '!', data.matches.length + ' matches', 'Choose the right person below.');
    const m = data.matches[0];
    if (m.checked_in) showResult('warn', '!', 'Already checked in' + (m.current_location ? ' (' + m.current_location + ')' : ''), displayName(m));
    else showResult('success', '\u2713', 'Valid - not yet checked in', displayName(m));
}

async function sendRequest(endpoint, payloadObj) {
    if (endpoint === 'check_in') {
        const first = await postRequest(endpoint, payloadObj);
        if (first && first.status === 409 && first.data.detail && first.data.detail.code === 'location_mismatch') {
            const ok = window.confirm('Expected location: ' + (first.data.detail.expected_location || '-') +
                '\nChecking in as: ' + payloadObj.attendance_mode +
                '\n\nAcknowledge different location and check in anyway?');
            if (!ok) return renderResult(endpoint, false, 409, first.data);
            return postRequest(endpoint, { ...payloadObj, acknowledge_location_mismatch: true }, true);
        }
        return first && renderResult(endpoint, first.ok, first.status, first.data);
    }
    return postRequest(endpoint, payloadObj, true);
}

async function postRequest(endpoint, payloadObj, render) {
    if (endpoint !== 'check_in') matchesEl.replaceChildren();
    if (!idToken) return void showResult('error', '\u2717', 'Sign in first', '');
    try {
        const response = await fetch(`/api/${endpoint}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + idToken },
            body: JSON.stringify(payloadObj)
        });
        if (response.status === 401) return void endSession('Session expired - sign in again');
        const data = await response.json().catch(() => ({}));
        if (render) renderResult(endpoint, response.ok, response.status, data);
        return { ok: response.ok, status: response.status, data };
    } catch (err) {
        showResult('error', '\u2717', 'Error', err.message);
    }
}

function lookup() {
    const student_number = document.getElementById('studentNum').value.trim();
    const last_name = document.getElementById('lastName').value.trim();
    if (!student_number && !last_name) return showResult('error', '\u2717', 'Enter a student number and/or last name', '');
    const body = {};
    if (/^\d{4}$/.test(student_number)) body.truncated_student_number = student_number;
    else if (student_number) body.student_number = student_number;
    if (last_name) body.last_name = last_name;
    return lookupWithFallback(body, student_number);
}

// Try the full student number first; if not found, retry with the truncated ID derived from it.
async function lookupWithFallback(body, student_number) {
    const truncated = /^\d{5,}$/.test(student_number || '') ? student_number.slice(-5, -1) : null;
    if (!truncated) return sendRequest('is_checked_in', body);
    matchesEl.replaceChildren();
    const first = await postRequest('is_checked_in', body);
    if (!first) return;
    if (first.status === 404) {
        const { student_number: _omit, ...rest } = body;
        return postRequest('is_checked_in', { ...rest, truncated_student_number: truncated }, true);
    }
    return renderResult('is_checked_in', first.ok, first.status, first.data);
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

// Test mode only: standard bank card track 1 "%B<cardnum>^LAST/FIRST^<other data>".
// Returns [number, last name] using the same digit rule as student cards.
function cardFields(raw) {
    const parts = raw.split('^');
    if (!testMode) return [(parts[2] || '').trim(), (parts[1] || '').trim()];
    return [(parts[0] || '').replace(/^%[A-Za-z]?/, '').replace(/\D/g, ''), (parts[1] || '').split('/')[0].trim()];
}

// Test mode only: fake the name for known truncated IDs, since only bank cards are available.
const TEST_NAMES = {
    '0723': { first: 'Zander', last: 'Hawley' },
    '2498': { first: 'Xie', last: 'Lang' },
    '3881': { first: 'Jared', last: 'McLaughlin' },
};

function parseCard(raw) {
    const num = cardFields(raw)[0];
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
    let [num, last] = cardFields(raw);
    if (testMode && TEST_NAMES[truncated_student_number]) last = TEST_NAMES[truncated_student_number].last;
    document.getElementById('studentNum').value = num;
    document.getElementById('lastName').value = last;
    await sendRequest('is_checked_in', { truncated_student_number });
    if (!document.getElementById('autoCheckIn').checked) return;
    const rows = matchesEl.querySelectorAll('.match');
    if (rows.length === 1) {
        const btn = rows[0].querySelector('button');
        if (!btn.disabled) btn.click();
    }
});
