'use strict';
// Fitbit connector screen: status, connected account, Sync now, Disconnect, and the guided Google setup.
// Reads only this app's documented relative API. Health data itself is shown by the Health app.
const el = id => document.getElementById(id);
let S = null, SETUP = null, replacing = false, pollTimer = null, chosenRedirect = null, reconnecting = false;

function say(text, kind = '') {
  const s = el('status');
  s.classList.toggle('wz-sr', !kind);
  s.classList.toggle('note', kind === 'note');
  s.textContent = text;
}
async function api(path, data) {
  const init = data === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) };
  const r = await fetch(path, init);
  let body = {};
  try { body = await r.json(); } catch (e) {}
  if (!r.ok) throw new Error(body.error || 'The Fitbit app could not do that. Try again.');
  return body;
}
function h(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; }
const dayLabel = (iso, opts = { day: 'numeric', month: 'short', year: 'numeric' }) => iso ? new Date(iso + 'T12:00:00').toLocaleDateString(undefined, opts) : '';
function ago(iso) {
  if (!iso) return 'never';
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return 'just now'; if (m < 60) return m + ' min ago';
  const hrs = Math.round(m / 60); return hrs < 36 ? hrs + ' h ago' : Math.round(hrs / 24) + ' days ago';
}
const n = (v, one, many) => `${v || 0} ${(v || 0) === 1 ? one : many}`;

// ---------- opening the Health app ----------
// Inside Wizard both shells are same-origin and answer the documented wizard:open event (native bridge v0);
// anywhere else the plain link opens Health in this window.
el('open-health').addEventListener('click', e => {
  try {
    const top = window.top;
    if (top !== window && top.wizardUX && top.location.origin === location.origin) {
      top.dispatchEvent(new CustomEvent('wizard:open', { detail: { app: 'wizard-health', path: '/wizard-health/' } }));
      e.preventDefault();
    }
  } catch (_) { /* not same-origin: follow the link */ }
});

// ---------- copy buttons ----------
async function copyFrom(input, btn) {
  const text = input.value;
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {
    input.focus(); input.select();
    try { ok = document.execCommand('copy'); } catch (_) {}
  }
  const was = btn.textContent;
  btn.textContent = ok ? 'Copied' : 'Selected';
  setTimeout(() => { btn.textContent = was; }, 1500);
  if (!ok) say('Selected. Use your device\u2019s copy.', 'note');
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-copy]');
  if (b) copyFrom(el(b.dataset.copy), b);
  const nx = e.target.closest('[data-next]');
  if (nx) showStep(+nx.dataset.next);
});

// ---------- guided setup ----------
// Default: Google sends the browser to http://localhost:3000/callback and the owner pastes that address back.
// Works from any browser. Alternative when the app is opened over https: Google comes straight back to this box.
function ownRedirect() {
  if (location.protocol !== 'https:') return null;
  return new URL('callback', location.origin + location.pathname.replace(/[^/]*$/, '')).href;
}
const isLocal = u => /^http:\/\/(localhost|127\.0\.0\.1)[:/]/.test(u || '');
function renderRedirect() {
  const own = ownRedirect();
  if (!chosenRedirect) chosenRedirect = SETUP.redirect_uri || SETUP.default_redirect;
  el('v-redirect').value = chosenRedirect;
  el('redir-note').textContent = isLocal(chosenRedirect)
    ? 'After you tap Continue on Google, that page won\u2019t load. That\u2019s expected: you paste its address back here in step 5.'
    : 'After you tap Continue, Google brings you straight back to this box and the app finishes on its own.';
  const other = isLocal(chosenRedirect) ? own : SETUP.default_redirect;
  const btn = el('redir-other');
  btn.hidden = !other || SETUP.keys_saved;
  btn.textContent = isLocal(chosenRedirect) ? 'Use this box\u2019s address instead' : 'Use the localhost address instead (works from any browser)';
  btn.onclick = () => { chosenRedirect = other; renderRedirect(); };
  if (!el('f-redirect').dataset.touched) el('f-redirect').value = chosenRedirect;
}
el('f-redirect').addEventListener('input', () => { el('f-redirect').dataset.touched = '1'; });

function showStep(k) {
  for (const li of document.querySelectorAll('.hl-step')) {
    const on = +li.dataset.step === k;
    li.classList.toggle('open', on);
    li.querySelector('.hl-step-h').setAttribute('aria-expanded', String(on));
  }
  const li = document.querySelector(`.hl-step[data-step="${k}"]`);
  if (li) requestAnimationFrame(() => li.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
}
for (const li of document.querySelectorAll('.hl-step')) li.querySelector('.hl-step-h').onclick = () => showStep(+li.dataset.step);

function link(id, href) { el(id).href = href; }
function renderSetup() {
  const L = SETUP.links;
  link('signup-link', SETUP.signup_url); link('move-link', L.move_account);
  link('project-link', L.project); link('api-link', L.enable_api);
  link('auth-platform-link', L.auth_platform); link('audience-link', L.audience); link('data-access-link', L.data_access);
  link('clients-link', L.clients);
  el('v-name').value = SETUP.suggested_name;
  el('v-app').value = SETUP.suggested_name;
  const ul = el('v-scopes'); ul.replaceChildren();
  SETUP.scopes.forEach((sc, i) => { const li = h('li', 'wz-chip', SETUP.scope_names[i]); li.title = sc; ul.append(li); });
  el('v-scopes-all').value = SETUP.scopes.join('\n');
  renderRedirect();
  const saved = SETUP.keys_saved;
  el('keys-saved').hidden = !saved || replacing;
  el('keys-form').hidden = saved && !replacing;
  el('keys-replace').hidden = !saved || replacing;
  el('keys-test').hidden = !saved;
  el('keys-next').hidden = !saved || SETUP.verified === 'invalid';
  if (saved) {
    el('cid-hint').textContent = SETUP.client_id_hint;
    el('saved-redirect').textContent = SETUP.redirect_uri;
    if (SETUP.verified && !el('test-result').textContent) showTest({ result: SETUP.verified, message: SETUP.verified === 'valid' ? 'Last test: Google accepted these keys.' : SETUP.verified === 'invalid' ? 'Last test: Google did not accept these keys. Replace them.' : 'Last test could not confirm the keys.' });
  }
  for (const li of document.querySelectorAll('.hl-step')) li.classList.toggle('done', saved && +li.dataset.step < 5);
  const local = isLocal(SETUP.redirect_uri || chosenRedirect);
  el('connect-step-4').hidden = !local;
  el('paste-label').firstChild.textContent = local ? 'Address from the browser bar' : 'Didn\u2019t come back to a \u201cFitbit connected\u201d page? Paste the address the browser ended on';
}
function showTest(r) {
  const p = el('test-result');
  p.textContent = r.message;
  p.dataset.kind = r.result;
}

el('keys-form').onsubmit = async e => {
  e.preventDefault();
  await saveKeys(el('f-cid'), el('f-secret'), el('f-redirect'), el('keys-save'));
};
async function saveKeys(cid, secret, redirect, btn) {
  btn.disabled = true;
  try {
    SETUP = await api('api/keys', { client_id: cid.value, client_secret: secret.value, redirect_uri: redirect.value });
    cid.value = ''; secret.value = ''; replacing = false;
    el('test-result').textContent = 'Saved. Testing with Google\u2026';
    renderSetup();
    const r = await api('api/keys/test', {});
    showTest(r);
    SETUP = await api('api/setup');
    renderSetup();
    if (r.result === 'valid') showStep(5);
    await refresh();
    return r;
  } catch (err) { el('test-result').textContent = err.message; el('test-result').dataset.kind = 'invalid'; }
  finally { btn.disabled = false; }
}
el('keys-test').onclick = async () => {
  const b = el('keys-test'); b.disabled = true; el('test-result').textContent = 'Testing with Google\u2026';
  try { showTest(await api('api/keys/test', {})); SETUP = await api('api/setup'); renderSetup(); }
  catch (err) { el('test-result').textContent = err.message; }
  finally { b.disabled = false; }
};
el('keys-replace').onclick = () => {
  replacing = true; el('keys-form').hidden = false; el('keys-saved').hidden = true; el('keys-replace').hidden = true;
  el('f-redirect').value = SETUP.redirect_uri || chosenRedirect; el('f-cid').focus();
};

// ---------- connect (the owner's own browser, then paste-back) ----------
el('connect').onclick = async () => {
  const b = el('connect'); b.disabled = true;
  let w = null;
  try { w = window.open('', '_blank'); } catch (e) {}   // opened inside the tap so phones don't block it
  try {
    const r = await api('api/oauth/start', {});
    const a = el('auth-link'); a.href = r.authorize_url; a.hidden = false;
    if (w) { try { w.opener = null; w.location.href = r.authorize_url; } catch (e) { w = null; } }
    el('connect-note').textContent = w ? (r.paste_back ? 'Google opened in a new tab. After tapping Continue, paste the address below.' : 'Google opened in a new tab. This screen updates by itself once you tap Continue.')
                                       : 'Tap \u201cOpen Google sign-in\u201d to continue.';
    if (r.paste_back) el('f-paste').focus({ preventScroll: true });
    poll(3000, 600);
  } catch (err) { if (w) w.close(); el('connect-note').textContent = err.message; }
  finally { b.disabled = false; }
};
el('paste-form').onsubmit = async e => {
  e.preventDefault();
  const b = el('paste-go'); b.disabled = true;
  try {
    S = await api('api/oauth/finish', { url: el('f-paste').value });
    el('f-paste').value = ''; reconnecting = false;
    say('Fitbit connected. Downloading your history\u2026', 'note');
    await refresh(); poll(3000, 1200);
  } catch (err) { el('connect-note').textContent = err.message; }
  finally { b.disabled = false; }
};
el('reconnect').onclick = () => { reconnecting = true; el('setup').dataset.opened = ''; render(); showStep(5); };

// ---------- sheets ----------
el('open-keys').onclick = () => { el('rk-redirect').value = (SETUP && SETUP.redirect_uri) || ''; WzSheet.open(el('sheet-keys')); };
el('rk-form').onsubmit = async e => {
  e.preventDefault();
  const r = await saveKeys(el('rk-cid'), el('rk-secret'), el('rk-redirect'), e.submitter || el('rk-form').querySelector('button'));
  if (r) { WzSheet.close(el('sheet-keys')); say(r.message, r.result === 'valid' ? 'note' : 'error'); }
};
el('open-disconnect').onclick = () => { el('sd-forget').checked = false; WzSheet.open(el('sheet-disconnect')); };
el('sd-go').onclick = async () => {
  const b = el('sd-go'); b.disabled = true;
  try {
    await api('api/disconnect', { forget_keys: el('sd-forget').checked });
    WzSheet.close(el('sheet-disconnect'));
    say('Fitbit disconnected.', 'note');
    el('setup').dataset.opened = '';
    await refresh();
  } catch (err) { say(err.message, 'error'); }
  finally { b.disabled = false; }
};
el('sync-now').onclick = async () => {
  const b = el('sync-now'); b.disabled = true;
  try { S = await api('api/sync', {}); render(); poll(3000, 400); } catch (err) { say(err.message, 'error'); }
  finally { b.disabled = false; }
};

// ---------- status ----------
const PILL = { not_configured: ['Not connected', 'off'], needs_login: ['Connect again', 'warn'],
  syncing: ['Syncing', 'ok'], connected: ['Connected', 'ok'], rate_limited: ['Paused', 'warn'] };
const live = () => S && ['connected', 'syncing', 'rate_limited'].includes(S.status);
const KIND = { sleep: 'sleeps', exercise: 'workouts', hrv: 'HRV days', rhr: 'resting heart rate days', spo2: 'blood oxygen days',
  resp: 'breathing rate days', temp: 'temperature days', azm: 'Active Zone Minutes days', steps: 'step days', calories: 'calorie days' };
function connLine() {
  const st = S.status, su = S.setup || {};
  if (st === 'not_configured') return su.keys_saved ? 'Keys saved. Connect with Google in step 5.' : 'Set up below so Health can show your sleep, heart and activity.';
  if (st === 'needs_login') return 'Google ended the sign-in. Connect again in step 5 to keep syncing.';
  if (st === 'syncing') {
    const p = su.progress;
    return (su.sync_kind === 'backfill' ? 'Downloading your Fitbit history' : 'Syncing') + (p ? ` (${p.records} ${KIND[p.kind] || p.kind})` : '') + '\u2026';
  }
  if (st === 'rate_limited') return 'Google asked for a pause; syncing resumes on its own.';
  return 'Synced ' + ago(S.last_sync);
}
function render() {
  const [label, cls] = PILL[S.status] || ['Unknown', 'off'];
  const pill = el('conn-pill'); pill.textContent = label; pill.className = 'wz-pill ' + cls;
  el('conn-line').textContent = connLine();
  const c = S.counts || {}, su = S.setup || {};
  el('account').hidden = !live() || reconnecting;
  if (live()) {
    el('acct-sub').textContent = S.history_from ? `History from ${dayLabel(S.history_from)} \u00b7 ${n(c.days, 'day', 'days')}, ${n(c.sleeps, 'sleep', 'sleeps')}, ${n(c.workouts, 'workout', 'workouts')}` : 'Downloading history\u2026';
    el('sync-title').textContent = S.status === 'syncing' ? 'Syncing now' : 'Synced ' + ago(S.last_sync);
    el('sync-sub').textContent = `Checks Google every ${S.sync_every_minutes} minutes` + (S.next_sync && S.status !== 'syncing' ? ' \u00b7 next ' + new Date(S.next_sync).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '');
    el('sync-now').disabled = S.status === 'syncing';
    const miss = su.missing_scopes || [];
    el('scope-warn').hidden = !miss.length;
    el('scope-warn').textContent = miss.length ? `Google isn\u2019t sharing: ${miss.join(', ')}. Tap Connect again and tick every box.` : '';
    const ends = su.signin_ends ? new Date(su.signin_ends) : null;
    el('signin-note').hidden = !ends;
    if (ends) el('signin-note').textContent = `Google ends this sign-in on ${ends.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })} (your project is in Testing). Tap Connect again then; it takes a minute.`;
    el('reconnect').hidden = !(miss.length || (ends && ends - Date.now() < 2 * 864e5));
  }
  el('sync-error').hidden = !(live() && S.error);
  el('sync-error').textContent = S.error || '';
  el('more').hidden = !su.keys_saved && !live();
  const dl = el('details'); dl.replaceChildren();
  const rows = [['Status', label], ['Last sync', S.last_sync ? new Date(S.last_sync).toLocaleString() : 'never'],
    ['History from', S.history_from ? dayLabel(S.history_from) : '\u2013'], ['Latest day', S.latest_date ? dayLabel(S.latest_date) : '\u2013'],
    ['Saved here', `${n(c.days, 'day', 'days')}, ${n(c.sleeps, 'sleep', 'sleeps')}, ${n(c.workouts, 'workout', 'workouts')}`],
    ['Data source', 'Google Health API (Fitbit and Pixel devices)'], ['Activity measured as', 'Active Zone Minutes (daily goal 22)'],
    ['Redirect URL', su.redirect_uri || '\u2013'], ['Shared as', 'health data for Health (read-only, box-only)'], ['App version', S.version]];
  if (S.error) rows.push(['Last problem', S.error]);
  for (const [k, v] of rows) dl.append(h('dt', '', k), h('dd', '', v));
  const setup = !live() || reconnecting;
  el('setup').hidden = !setup;
  if (setup && SETUP) {
    renderSetup();
    if (!el('setup').dataset.opened) { el('setup').dataset.opened = '1'; showStep(su.keys_saved ? 5 : 1); }
  }
}

async function refresh() {
  try {
    S = await api('api/status');
    if (!SETUP || !live() || reconnecting) SETUP = await api('api/setup');
    render();
  } catch (err) { say(err.message, 'error'); }
}
function poll(ms, times) {
  clearTimeout(pollTimer);
  let k = 0;
  const waitingForSignIn = ms < 60000;
  const tick = async () => {
    await refresh();
    k++;
    const fast = S && (S.status === 'syncing' || (waitingForSignIn && k < times && ['not_configured', 'needs_login'].includes(S.status)));
    pollTimer = setTimeout(tick, fast ? 3000 : 60000);
  };
  pollTimer = setTimeout(tick, ms);
}

// Domain-only Back: a reconnect view folds back to the account first; sheets push their own entries through
// wizard-sheet.js; the screen itself is the root.
if (!window.WizardApp || WizardApp.version !== 1) {
  say('Wizard system functions are unavailable. Open Fitbit through Wizard.', 'error');
} else {
  WizardApp.ready({ name: 'wizard-fitbit', version: '0.1.0' });
  WizardApp.back.register({ canGoBack: () => reconnecting && live(), back: () => { if (!(reconnecting && live())) return false; reconnecting = false; render(); return true; } });
}
refresh().then(() => poll(S && S.status === 'syncing' ? 3000 : 60000, S && S.status === 'syncing' ? 1200 : 1e9));
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
